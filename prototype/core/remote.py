"""远端协助（DeepSeek）：只在配置了 DeepSeek API key 时可用；本地路径始终优先。

省 token 的四条（对齐 DSH 的 DeepSeek 适配器约定）：
1. **前缀稳定**：system + 任务契约 + 符号清单放在最前面且内容可复用，动态证据放最后。
   DeepSeek 按前缀命中缓存，`prompt_cache_hit_tokens` 会直接计入用量；重复请求同一节点时
   命中率很高（DSH 的实现里 `cacheReadTokens ← prompt_cache_hit_tokens`）。
2. **只发符号级现状**：不带整文件、不带全量测试历史，包体默认上限 14,000 字符。
3. **输出受限**：`max_tokens` 默认 1600，要求只回 JSON 符号编辑，拒绝解释性长文。
4. **不重复问**：同一节点同一次失败证据的包做内容哈希缓存（命中的直接复用上次答复，零 token）。
   实测一个节点常连续失败 3 次而证据相同，这条能省掉 2/3 的远端调用。

用量一律落库（`remote_usage`），可在审计里看到 prompt/completion/cache_hit 与命中率。
"""
from __future__ import annotations

import hashlib
import threading
import json
import os
import time
import urllib.error
import urllib.request

from . import db, settings_store

PRICE_PER_M = {"deepseek-flash": (0.27, 1.10), "deepseek-chat": (0.27, 1.10)}  # 每百万 token：输入(未命中)/输出；命中约 1/10

# 单次调用的**安全上限**（max_tokens 的最终取值）。
#
# 口径（2026-11-05 用户定的，纠正了我之前的错误）：
#   `max_tokens` 是**上限不是消费** —— 计费只按实际生成的 token 算，
#   所以"给大"几乎不花钱，**不该为了省钱去缩它**。
#   实测单次峰值：输出 26 788 token（¥0.107），离 128K 很远。
# 那为什么不用官方的 384K？只有一个理由：**跑飞止损**。
#   模型一旦复读，上限就是唯一的刹车；128K 足够容下任何正常调用（实测峰值的 4.8 倍），
#   又不至于让它疯跑到 384K。
# 注意：这个上限**与"一轮要求写几个函数"（剂量）是两件事**，
#   剂量由 core/dosing 按档案的正文预算与近期一次通过率决定，两者互不影响。
SAFETY_CAP = 128000
SAFETY_MIN_CAP = 128000        # 校准值不允许低于这个（宁可多给，也不要截断）
# 思考强度 → 额外要预留的 reasoning 配额（token）。它**加在正文配额之上**。
# max_tokens 是"思考 + 正文"的总和，把思考算进正文配额里会出现"想得越深、正文越写不出来"。
# 数字按实测：flash 在 max 档想过 22 452 token、high 档想过 15 765。
REASONING_RESERVE = {"none": 0, "low": 12000, "high": 24000, "max": 40000}
# 没配正文配额却又开了思考时，正文至少留这么多
CONTENT_FLOOR = 12000
CONTENT_CEIL = 384000          # 官方单次输出上限（正文部分）


def reasoning_reserve(effort: str | None) -> int:
    """该思考档要预留多少 reasoning 配额 —— **优先用校准值**（core/calibration）。

    内置常量只是兜底：真实值由账本按"该档 reasoning 的 p95 × 1.3"自动算出来，
    所以换模型/换任务规模时不用改代码，跑几轮它自己就校准过去了。
    """
    e = str(effort or "none").lower()
    if e == "none":
        return 0
    try:
        from . import calibration
        got = (calibration.current().get("reasoning_reserve") or {}).get(e)
        if isinstance(got, (int, float)) and got > 0:
            return int(got)
    except Exception:                                    # noqa: BLE001
        pass
    return REASONING_RESERVE.get(e, 0)


def safety_cap() -> int:
    """单次调用上限（校准值优先；不低于内置 128k）。"""
    try:
        from . import calibration
        got = int(calibration.current().get("safety_cap") or 0)
        if got > 0:
            return max(SAFETY_MIN_CAP, got)
    except Exception:                                    # noqa: BLE001
        pass
    return SAFETY_CAP


def content_floor() -> int:
    """正文下限（校准值优先）。"""
    try:
        from . import calibration
        got = int(calibration.current().get("content_floor") or 0)
        if got > 0:
            return got
    except Exception:                                    # noqa: BLE001
        pass
    return CONTENT_FLOOR


def max_tokens_for(content_budget: int, effort: str | None) -> int:
    """正文配额 + 思考预留 —— **唯一算法**，`_payload_for` 与 `complete` 都必须用它。

    为什么必须只有一个入口：实测踩过两次。第一次把思考算进正文配额里（正文 0 token），
    第二次在 `_payload_for` 里加了预留、却被 `complete` 里的 `max_out` 覆盖回 8 000
    （思考 6 631 + 正文 1 369，JSON 又被截断）。两条路各写一遍 = 必漏一处。
    """
    e = str(effort or "none").lower()
    reserve = reasoning_reserve(e)
    content = int(content_budget or 0)
    if content <= 0:
        content = content_floor() if reserve else 0
    if not content and not reserve:
        return 0
    content = min(content, CONTENT_CEIL)          # 官方单次输出上限 384K（正文部分）
    desired = max(1, content) + reserve
    # **一步到位**：正常调用永远不该被上限掐住（实测峰值 26 788 << 128K），
    # 所以直接把 max_tokens 顶到安全上限，而不是按需算一个小值再往上抬。
    # 这不是浪费：max_tokens 只是上限，按实际生成计费。
    return max(desired, safety_cap())


def content_truncated(usage: dict | None, content_budget: int) -> bool:
    """判断"正文是不是被配额截断了"。

    不能直接拿 completion_tokens 比：它含思考。正文 = completion − reasoning，
    正文顶到配额才说明"没写完"，这时候重试才有意义（加预算），而不是重问。
    """
    budget = int(content_budget or 0)
    if budget <= 0:
        return False
    usage = usage or {}
    used = int(usage.get("completion_tokens") or 0)
    reason = int(usage.get("reasoning_tokens") or 0)
    content = max(0, used - reason)
    return content >= int(budget * 0.98)


def _key() -> str:
    r = settings_store.get_remote()
    return (r.get("api_key") or "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()


def available() -> tuple[bool, str]:
    r = settings_store.get_remote()
    if not r.get("enabled", True):
        return False, "远端协助已在设置里关闭"
    if not _key():
        return False, "未配置 DeepSeek API key（设置 → 远端模型，或环境变量 DEEPSEEK_API_KEY）"
    if r.get("provider", "deepseek") != "deepseek":
        return False, f"暂不支持 provider={r.get('provider')}（当前仅 deepseek）"
    return True, ""


def _ensure_table() -> None:
    db.execute(
        "CREATE TABLE IF NOT EXISTS remote_usage("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, node_id TEXT, purpose TEXT,"
        " model TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, cache_hit_tokens INTEGER,"
        " cached_reply INTEGER, ms INTEGER, created_at TEXT,"
        " reasoning_tokens INTEGER DEFAULT 0, offpeak INTEGER DEFAULT 0)")
    # 老库补列（NULL 允许），便于升级不丢历史
    for col, ddl in (("reasoning_tokens", "INTEGER DEFAULT 0"), ("offpeak", "INTEGER DEFAULT 0"),
                     ("effort", "TEXT"), ("cost_cny", "REAL DEFAULT 0")):
        try:
            db.execute(f"ALTER TABLE remote_usage ADD COLUMN {col} {ddl}")
        except Exception:                              # noqa: BLE001
            pass
    # 去重缓存要存"答复本身"，否则同证据第二次调用拿不到内容、无法落盘（实测踩过）
    db.execute("CREATE TABLE IF NOT EXISTS remote_cache("
               "key TEXT PRIMARY KEY, answer TEXT, created_at TEXT)")


def _parse_hhmm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(text).split(":")
        return int(h) % 24, int(m) % 60
    except Exception:                                  # noqa: BLE001
        return default


def is_offpeak(when: float | None = None) -> bool:
    """是否处于**空闲时段**（半价）。

    官方口径（2026-10 价目页）：高峰 = 北京时间周一至周五 9:00–12:00、14:00–18:00
    （中国法定节假日除外）；其余时段、含周末全天为空闲。
    这里按设置的 `peak_window` 判：不在高峰区间内就算空闲。
    """
    r = settings_store.get_remote()
    win = r.get("peak_window") or {}
    ranges = win.get("ranges") or ["09:00-12:00", "14:00-18:00"]
    weekdays_only = bool(win.get("weekdays_only", True))
    ts = when if when is not None else time.time()
    # 北京时间 = UTC+8（不依赖本机时区设置）
    bj = time.gmtime(ts + 8 * 3600)
    cur = bj.tm_hour * 60 + bj.tm_min
    if weekdays_only and bj.tm_wday >= 5:              # 周六/周日
        return True
    for item in ranges:
        try:
            a, b = str(item).split("-")
        except ValueError:
            continue
        sh, sm = _parse_hhmm(a.strip(), (9, 0))
        eh, em = _parse_hhmm(b.strip(), (12, 0))
        if sh * 60 + sm <= cur < eh * 60 + em:
            return False
    return True


def pricing_for(model: str) -> dict:
    """模型价格：优先取模型档案里的峰值/空闲两套价，回落旧价目表（美元口径已废弃）。"""
    for p in settings_store.models():
        if p.get("model") == model:
            off = p["pricing"]["offpeak"]
            return {"in_miss": off["in_miss"], "in_hit": off["in_hit"], "out": off["out"],
                    "peak": p["pricing"]["peak"], "currency": "CNY"}
    r = settings_store.get_remote()
    table = r.get("pricing") or {}
    return table.get(model) or table.get("deepseek-flash") or {
        "in_miss": 1.0, "in_hit": 0.02, "out": 4.0, "currency": "CNY"}


def cost_cny(step: dict | None, prompt: int, hit: int, comp: int,
             when: float | None = None, offpeak: bool | None = None) -> float:
    """按档案价目表算这一次调用花了多少**人民币**（空闲时段用空闲价）。

    公式（官方）：命中输入 × 命中价 + 未命中输入 × 未命中价 + 输出 × 输出价。
    """
    off = is_offpeak(when) if offpeak is None else bool(offpeak)
    if step and step.get("pricing"):
        price = step["pricing"]["offpeak" if off else "peak"]
    else:
        price = pricing_for((step or {}).get("model") or "")
    miss = max(0, int(prompt or 0) - int(hit or 0))
    return (miss / 1e6 * float(price.get("in_miss") or 0)
            + int(hit or 0) / 1e6 * float(price.get("in_hit") or 0)
            + int(comp or 0) / 1e6 * float(price.get("out") or 0))


def ladder(profile: str = "") -> list[dict]:
    """八档阶梯（档案摊出来的）：flash:none/low/high/max → pro:none/low/high/max。

    `profile` 非空时只返回该模型档案的档位（用右上角「远端模型」下拉指定这一夜用谁），
    这样"同一需求、不同远端模型"的结果可以放在一起比较，而不是混在一条阶梯里。
    """
    steps = settings_store.ladder()
    pid = str(profile or "").strip()
    return [x for x in steps if str(x.get("profile_id")) == pid] if pid else steps


def step_at(i: int, profile: str = "") -> dict | None:
    steps = ladder(profile)
    if not steps:
        return None
    return steps[max(0, min(int(i), len(steps) - 1))]


def _cost(p: dict, prompt: int, hit: int, comp: int, offpeak: bool) -> float:
    ratio = float(p.get("offpeak_ratio") or 1.0) if offpeak else 1.0
    miss = max(0, prompt - hit)
    usd = (miss / 1e6 * float(p.get("in_miss") or 0)
           + hit / 1e6 * float(p.get("in_hit") or 0)
           + comp / 1e6 * float(p.get("out") or 0))
    return usd * ratio


_REC_LOCK = threading.Lock()

PROMPT_VERSION = "2"      # 改动 system 提示词时递增：让去重缓存失效，避免复用旧提示词下的答复


def _cache_key(packet: str, model: str, max_tokens: int = 0, thinking: str = "") -> str:
    return hashlib.sha1(
        f"{PROMPT_VERSION}|{model}|{max_tokens}|{thinking}|{packet}".encode("utf-8")).hexdigest()


def _record(**kw) -> None:
    with _REC_LOCK:
        _record_locked(**kw)


def _record_locked(**kw) -> None:
    _ensure_table()
    step = {"model": kw.get("model", ""), "pricing": kw.get("pricing")}
    cost = cost_cny(step if kw.get("pricing") else None,
                    int(kw.get("prompt_tokens") or 0), int(kw.get("cache_hit_tokens") or 0),
                    int(kw.get("completion_tokens") or 0)) if kw.get("model") else 0.0
    db.execute(
        "INSERT INTO remote_usage(task_id, node_id, purpose, model, prompt_tokens,"
        " completion_tokens, cache_hit_tokens, cached_reply, ms, created_at,"
        " reasoning_tokens, offpeak, effort, cost_cny) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("task_id", ""), kw.get("node_id", ""), kw.get("purpose", "handoff"),
         kw.get("model", ""), kw.get("prompt_tokens", 0), kw.get("completion_tokens", 0),
         kw.get("cache_hit_tokens", 0), 1 if kw.get("cached_reply") else 0,
         kw.get("ms", 0), db.now(),
         int(kw.get("reasoning_tokens") or 0), 1 if is_offpeak() else 0,
         str(kw.get("effort") or ""), round(cost, 6)))


def usage_summary(task_id: str = "") -> dict:
    """本地用量统计：总量 + 闲时/忙时 + 按**模型档案价目**算人民币花费。

    花费口径按官方公式：命中输入×命中价 + 未命中输入×未命中价 + 输出×输出价，
    空闲时段用空闲价（高峰为北京时间工作日 9-12、14-18，其余半价）。
    """
    _ensure_table()
    where, params = ("WHERE task_id=?", (task_id,)) if task_id else ("", ())
    rows = db.query("SELECT * FROM remote_usage " + where, params)

    def cost_of(r: dict) -> float:
        step = next((s for s in ladder() if s["model"] == (r.get("model") or "")), None)
        return cost_cny(step, r["prompt_tokens"] or 0, r["cache_hit_tokens"] or 0,
                        r["completion_tokens"] or 0, offpeak=bool(r.get("offpeak")))

    def agg(subset: list[dict]) -> dict:
        prompt = sum(r["prompt_tokens"] or 0 for r in subset)
        hit = sum(r["cache_hit_tokens"] or 0 for r in subset)
        comp = sum(r["completion_tokens"] or 0 for r in subset)
        reason = sum(r.get("reasoning_tokens") or 0 for r in subset)
        cost = sum(cost_of(r) for r in subset)
        return {"calls": len(subset), "prompt_tokens": prompt,
                "completion_tokens": comp, "cache_hit_tokens": hit,
                "reasoning_tokens": reason,
                "cache_hit_rate": round(hit / prompt, 3) if prompt else 0.0,
                "cached_replies": sum(1 for r in subset if r["cached_reply"]),
                "cost_cny": round(cost, 6)}

    busy = [r for r in rows if not r.get("offpeak")]
    off = [r for r in rows if r.get("offpeak")]
    total = agg(rows)
    # 缓存省下来的钱：命中部分若按未命中价计会多花多少
    saved = 0.0
    for r in rows:
        step = next((s for s in ladder() if s["model"] == (r.get("model") or "")), None)
        if not step:
            continue
        price = step["pricing"]["offpeak" if r.get("offpeak") else "peak"]
        hit = r["cache_hit_tokens"] or 0
        saved += hit / 1e6 * (float(price.get("in_miss") or 0) - float(price.get("in_hit") or 0))
    total["cache_saved_cny"] = round(saved, 6)

    # 按模型 / 按档位分开看（"钱花在哪个模型、哪个思考强度上"）
    by_model: dict[str, dict] = {}
    for r in rows:
        key = r.get("model") or "?"
        by_model[key] = agg([r] + [])          # 占位，下面重算
    by_model = {}
    for r in rows:
        by_model.setdefault(r.get("model") or "?", []).append(r)
    total["by_model"] = {k: agg(v) for k, v in by_model.items()}
    by_effort: dict[str, list] = {}
    for r in rows:
        by_effort.setdefault(r.get("effort") or "(旧记录)", []).append(r)
    total["by_effort"] = {k: agg(v) for k, v in by_effort.items()}

    total["peak"] = agg(busy)
    total["offpeak"] = agg(off)
    total["window"] = settings_store.get_remote().get("peak_window") or {}
    total["ladder"] = [{"model": s["model"], "effort": s["effort"]} for s in ladder()]
    total["pricing_note"] = ("价格来自模型档案（设置 → 远端模型档案）；"
                             "空闲时段为高峰的一半，高峰=北京时间工作日 9-12、14-18")
    return total


def _trim(text: str, limit: int) -> str:
    """超长时保留头（格式要求/契约）与尾（最近失败证据），中间截断。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.62)
    tail = limit - head - 40
    return (text[:head] + "\n\n…（中间省略 "
            f"{len(text) - head - tail} 字符，控制远端 token）…\n\n" + text[-tail:])


def _payload_for(step: dict, r: dict, messages: list[dict], json_mode: bool) -> tuple[dict, str, str]:
    """按"阶梯档位"拼请求体，返回 (payload, base_url, key)。

    思考强度走官方参数：none → `thinking:{type:disabled}`；low/high/max →
    `thinking:{type:enabled}` + `reasoning_effort:<档位>`。
    """
    effort = str(step.get("effort") or "none").lower()
    payload = {
        "model": step.get("model") or r.get("model") or "deepseek-flash",
        "messages": messages,
        "temperature": float(step.get("temperature") if step.get("temperature") is not None
                             else 0.2),
        "stream": False,
    }
    # 输出配额只有一个真相源：调用方按任务规模算好的 max_out（tokens.output_budget）。
    # 档案里的 max_output_tokens 默认 0 = 不设，只有运维显式配了才用；
    # 以前这里兜了个 8000，等于第二个真相源，会把小任务的配额顶大、大任务顶小。
    cap = int(step.get("max_output_tokens") or r.get("max_output_tokens") or 0)
    # **思考也要占 max_tokens**：官方口径里 max_tokens 是"思考 + 正文"的**总和**。
    # 实测踩过（2026-11-05 甲方模式第一夜）：flash:max 档 8000 配额被 reasoning 吃满，
    # 正文 0 token → 契约合成返回空 → 整夜白跑（两次调用各 ¥0.03）。
    # 所以开思考时必须把 reasoning 的额度**加在正文配额之上**，否则"越想越写不出来"。
    _mt = max_tokens_for(cap, effort)
    if _mt:
        payload["max_tokens"] = _mt
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if effort == "none":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effort
    base = (step.get("base_url") or r.get("base_url") or "https://api.deepseek.com").rstrip("/")
    key = step.get("api_key") or _key()
    return payload, base, key


def _post(payload: dict, base: str, key: str, timeout: int = 300) -> tuple[dict | None, str, int]:
    """发一次请求。返回 (响应JSON, 错误串, 耗时ms)。"""
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), "", int((time.time() - t0) * 1000)
    except urllib.error.HTTPError as exc:
        return None, http_error_hint(exc), int((time.time() - t0) * 1000)
    except Exception as exc:                            # noqa: BLE001
        return None, f"请求失败：{type(exc).__name__}: {exc}", int((time.time() - t0) * 1000)


_TRANSIENT = ("限流", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "请求失败", "timed out",
              "Timeout", "超时")


def _is_transient(err: str) -> bool:
    return any(t in (err or "") for t in _TRANSIENT)


def ask(packet_md: str, task_id: str = "", node_id: str = "",
        purpose: str = "handoff", step_index: int = 0) -> dict:
    """把接力包发给 DeepSeek（从阶梯第 step_index 档起），返回 {ok, answer, usage, error}。"""
    ok, why = available()
    if not ok:
        return {"ok": False, "error": why, "answer": ""}
    r = settings_store.get_remote()
    limit = int(r.get("max_prompt_chars") or 200000)
    body_md = _trim(packet_md, limit)
    steps = ladder() or []
    if not steps:
        return {"ok": False, "error": "没有任何启用的远端模型档案（设置 → 远端模型档案）",
                "answer": ""}

    # 前缀稳定：静态说明在前（多次请求逐字相同 → 命中缓存），动态证据在后
    system = (
        "你是代码修复顾问。只输出 JSON 符号级编辑，不要解释、不要围栏以外的文字。\n"
        '格式：{"edits":[{"op":"replace","symbol":"<限定名>","code":"def ..."}]}\n'
        "硬性要求：code 必须含 def/class 行；类方法写 Cls.method；不要输出整文件；"
        "不要改与被测行为无关的接口；不确定就少改。\n"
        "**顺序与注解**：本文件很可能没有 `from __future__ import annotations`，"
        "所以函数注解里若引用后定义的类会 NameError。要么把被引用的类放在前面，"
        "要么额外用 add 加一条 `from __future__ import annotations`。\n"
    )
    tried: list[str] = []
    err = ""
    for idx in range(max(0, int(step_index)), len(steps)):
        step = steps[idx]
        tag = f"{step['profile_id']}:{step['effort']}"
        cache_key = _cache_key(body_md, step["model"], int(step["max_output_tokens"]),
                               step["effort"])
        _ensure_table()
        prev = db.query("SELECT answer FROM remote_cache WHERE key=?", (cache_key,))
        if prev:
            _record(task_id=task_id, node_id=node_id, purpose=cache_key, model=step["model"],
                    cached_reply=True, effort=step["effort"], pricing=step.get("pricing"))
            return {"ok": True, "answer": prev[0]["answer"] or "", "cached": True, "by": tag,
                    "step": idx, "usage": usage_summary(task_id),
                    "error": "同一份证据已问过远端，直接复用上次答复（0 token）"}
        payload, base, key = _payload_for(step, r, [
            {"role": "system", "content": system},
            {"role": "user", "content": body_md}], json_mode=True)
        data, err, ms = _post(payload, base, key)
        if data is None:
            tried.append(f"{tag}失败({err[:40]})")
            if _is_transient(err) and idx + 1 < len(steps):
                continue                                  # 限流/网络抖动 → 升一档再试
            return {"ok": False, "error": err, "answer": "", "tried": tried}
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        cache_hit = int(usage.get("prompt_cache_hit_tokens")
                        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        answer = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        _record(task_id=task_id, node_id=node_id, purpose=cache_key, model=step["model"],
                prompt_tokens=prompt_tokens, completion_tokens=completion,
                cache_hit_tokens=cache_hit, ms=ms, reasoning_tokens=reasoning,
                effort=step["effort"], pricing=step.get("pricing"))
        if answer.strip():
            db.execute("INSERT OR REPLACE INTO remote_cache(key, answer, created_at) VALUES(?,?,?)",
                       (cache_key, answer, db.now()))
        return {"ok": True, "answer": answer, "ms": ms, "cached": False, "by": tag, "step": idx,
                "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion,
                          "cache_hit_tokens": cache_hit,
                          "cache_hit_rate": round(cache_hit / prompt_tokens, 3) if prompt_tokens else 0.0,
                          "reasoning_tokens": reasoning,
                          "cost_cny": round(cost_cny(step, prompt_tokens, cache_hit, completion), 6)},
                "summary": usage_summary(task_id)}
    return {"ok": False, "error": err or "阶梯已走完", "answer": "", "tried": tried}


def http_error_hint(exc) -> str:
    """把 HTTP 错误翻译成"能直接照做"的一句话（401/402/403/429）。"""
    code = getattr(exc, "code", 0)
    try:
        detail = (exc.read() or b"").decode("utf-8", "replace")[:200]
    except Exception:                                    # noqa: BLE001
        detail = ""
    if code in (401, 403):
        return (f"Key 无效或已被轮换（HTTP {code}）：请到「设置 → 远端模型」重新填一把可用的 "
                f"DeepSeek Key（旧的作废 Key 留在本地设置里也会报这个错）")
    if code == 402:
        return "余额不足（HTTP 402）：远端账户欠费，请充值或改用本地模型"
    if code == 429:
        return "被限流（HTTP 429）：稍后重试，或减少调用频率"
    return f"HTTP {code}: {detail}" if detail else f"HTTP {code}"


def complete(prompt: str, task_id: str = '', node_id: str = 'architect',
             workspace: str = '', emit=None, thinking: str | None = None,
             step_index: int | None = None, json_mode: bool = False,
             max_out: int | None = None) -> dict:
    """发一段自由文本提示，返回 {ok, answer, usage}。

    走**单模型 + 思考强度阶梯**（core/settings_store.ladder）：一次运行只用一个模型，
    在这个模型内部按 none → low → high → max 切思考强度（强度由 core/effort.py 按需求选）。
      · step_index=None → 从第 0 档（flash:none）开始；
      · thinking="on"/"off" 是老接口，映射成档位过滤（on→跳过 none 档）；
      · 限流/网络抖动会自动升一档重试；"答得不对"不在这里重试（由夜班按阈值升档）。
    """
    ok, why = available()
    if not ok:
        return {'ok': False, 'error': why, 'answer': ''}
    # 关键节点守卫：开发者模式在"把代码发往远端"前要人点头（甲方模式自动放行）
    try:
        from . import guards
        g = guards.gate_remote_call(workspace, len(prompt or ''), task_id, emit=emit)
        if not g.get('ok'):
            return {'ok': False, 'error': f"未获批准：{g.get('reason')}", 'answer': ''}
    except Exception:                                    # noqa: BLE001
        pass
    r = settings_store.get_remote()
    steps = ladder()
    if not steps:
        return {'ok': False, 'error': '没有任何启用的远端模型档案（设置 → 远端模型档案）',
                'answer': ''}
    start = 0 if step_index is None else max(0, min(int(step_index), len(steps) - 1))
    if thinking and str(thinking).lower() == 'on' and step_index is None:
        start = next((i for i, s in enumerate(steps) if s['effort'] != 'none'), 7)
    # 阶梯第 0 档是**本地模型**（model 为空、effort 为 "-"）：它不该被发到远端。
    # 实测踩过：按档位下标直接发请求，payload 里出现 reasoning_effort="-" →
    # HTTP 422 unknown variant `-`。这里只保留真正的远端档，并保留原始下标。
    remote_steps = [(i, s) for i, s in enumerate(steps) if str(s.get('model') or '').strip()]
    if not remote_steps:
        return {'ok': False, 'error': '阶梯里没有可用的远端模型档案（只有本地档）',
                'answer': ''}
    # token 预算（真正的物理上限）：先按校准比率估算，超了才压缩；绝不拆成两次调用。
    from . import tokens as _tokens
    text = prompt
    need, info = _tokens.needs_compression(text)
    if need:
        limit_chars = int((r.get('max_prompt_tokens') or 950000) / max(_tokens.ratio(), 0.2))
        limit_chars = min(limit_chars, int(r.get('max_prompt_chars') or 4000000))
        text = prompt[:limit_chars]
        if emit:
            emit('context_budget', {**info, 'action': '压缩', 'chars': limit_chars})
    tried = []
    err = ''
    seq = [(i, s) for i, s in remote_steps if i >= start] or remote_steps[:1]
    for idx, step in seq:
        tag = f"{step['profile_id']}:{step['effort']}"
        payload, base, key = _payload_for(step, r, [{'role': 'user', 'content': text}],
                                          json_mode=json_mode)
        _eff = str(payload.get("reasoning_effort") or "none").lower()
        if max_out:
            # 整文件/架构设计需要长输出；档位默认 8K 只够一个函数。
            # 注意：这里也必须走 max_tokens_for，否则会把 _payload_for 算好的思考预留覆盖掉
            # （实测：只在 _payload_for 加预留时，这条一覆盖，思考又把正文挤成 1 369 token）。
            payload['max_tokens'] = max_tokens_for(min(int(max_out), CONTENT_CEIL), _eff)
        # 「配额不够」要归因到配额，不要归因到模型，并且**当场自动放大重试一次**。
        # 实测（2026-11-05）：high 档预留 8000 时出现过「思考 11 000、正文 0」，
        # 白烧 ¥0.048；max 档也出现过「思考 22 452、正文 4 336」把正文挤到写不完。
        # 归因错一次的代价是"重写整个模块"，比多给配额贵得多。
        for _attempt in range(2):
            data, err, ms = _post(payload, base, key)
            if data is None:
                break
            choice = (data.get('choices') or [{}])[0]
            fin = str(choice.get('finish_reason') or '')
            u = data.get('usage') or {}
            _ct = int(u.get('completion_tokens') or 0)
            _rt = int((u.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0)
            _body = max(0, _ct - _rt)
            starved = (_ct > 0 and _body <= 32) or fin == 'length'
            if not starved or _attempt == 1:
                break
            # 思考吃光 / 顶到上限 → 直接把配额翻倍（再留一档余量），重发一次
            cur = int(payload.get('max_tokens') or 0)
            payload['max_tokens'] = max(cur * 2, cur + 8000, 16000)
            if emit:
                emit('budget_grown', {'step': tag, 'from': cur, 'to': payload['max_tokens'],
                                      'why': 'quota_starved',
                                      'observed': {'completion': _ct, 'reasoning': _rt,
                                                   'body': _body, 'finish': fin}})
        if data is None:
            tried.append(f'{tag}失败({err[:40]})')
            if _is_transient(err) and idx + 1 < len(steps):
                continue
            return {'ok': False, 'error': err, 'answer': '', 'tried': tried}
        usage = data.get('usage') or {}
        pt = int(usage.get('prompt_tokens') or 0)
        hit = int(usage.get('prompt_cache_hit_tokens')
                  or (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
        ct = int(usage.get('completion_tokens') or 0)
        rt = int((usage.get('completion_tokens_details') or {}).get('reasoning_tokens') or 0)
        answer = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        # 用真实 usage 校准"token/字符"比率（下次估算更准，不依赖 tokenizer）
        _tokens.observe(len(text), pt)
        _record(task_id=task_id, node_id=node_id, model=step['model'],
                prompt_tokens=pt, completion_tokens=ct, cache_hit_tokens=hit, ms=ms,
                reasoning_tokens=rt, effort=step['effort'], pricing=step.get('pricing'))
        return {'ok': True, 'answer': answer, 'ms': ms, 'by': tag, 'step': idx,
                'usage': {'prompt_tokens': pt, 'completion_tokens': ct,
                          'reasoning_tokens': rt, 'cache_hit_tokens': hit,
                          'cost_cny': round(cost_cny(step, pt, hit, ct), 6)}}
    return {'ok': False, 'error': err or '阶梯已走完', 'answer': '', 'tried': tried}
