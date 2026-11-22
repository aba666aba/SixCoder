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
# OpenAI 兼容端点的输出配额上限（智谱官方示例 65536，接口硬上限 131072）。
# DS 校准出的 33 万配额直接发给这类端点会 HTTP 400「max_tokens 参数非法」。
OPENAI_OUTPUT_CEIL = 65536
# **总 max_tokens 硬上限**（思考 + 正文之和），按厂商协议族区分。
# 2026-11-09 实锤：断开输出限制后 content=384000 + reserve=40000 = 424000，
# 超过 DeepSeek 的 393216 → HTTP 400「Invalid max_tokens value」，
# 远端**一个字节都没写**（所有 author 调用全废）。
# 所以"不断开限制"≠"不管厂商物理上限"：上限按厂商给，不按任务量给。
MODEL_MAX_TOKENS = {
    "deepseek": 393216,     # DeepSeek 官方报错里给的 valid range 上界
    "glm": 131072,          # GLM-5.x 官方最大输出
    "openai": 65536,        # OpenAI 兼容端点官方示例
}
# 按协议族区分的官方端点：DeepSeek 档案不继承全局（可能是别家 OpenAI 兼容服务）的地址
DEEPSEEK_BASE = "https://api.deepseek.com"
GLM_BASE = "https://open.bigmodel.cn/api/paas/v4"      # 智谱官方（2026-11-09 核对）
OPENAI_COMPAT_BASE = "https://api.openai.com/v1"
GLM_OUTPUT_CEIL = 131072        # 官方 GLM-5.x 最大输出（2026-11-09 核对）


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


def max_tokens_for(content_budget: int, effort: str | None,
                   ceiling: int = 0) -> int:
    """正文配额 + 思考预留 —— **唯一算法**，`_payload_for` 与 `complete` 都必须用它。

    为什么必须只有一个入口：实测踩过两次。第一次把思考算进正文配额里（正文 0 token），
    第二次在 `_payload_for` 里加了预留、却被 `complete` 里的 `max_out` 覆盖回 8 000
    （思考 6 631 + 正文 1 369，JSON 又被截断）。两条路各写一遍 = 必漏一处。

    `ceiling` = 厂商的**总 max_tokens 硬上限**（思考+正文）。
    2026-11-09 实锤：不夹就会算出 424000 > DeepSeek 的 393216 →
    HTTP 400「Invalid max_tokens value」→ 远端一个字节都没写。
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
    out = max(desired, safety_cap())
    cap = int(ceiling or 0)
    if cap > 0 and out > cap:
        # 超厂商硬上限 → 压回去。**思考预留优先保住**（内容少了可以少写，
        # 思考没了写得更差），所以先削正文，再削总上限。
        out = max(1, cap)
    return out


def _ceiling_for(step: dict | None = None, r: dict | None = None) -> int:
    """这个厂商协议的**总 max_tokens 硬上限**（思考 + 正文）。

    2026-11-09 实锤：不按厂商夹 → 算出 424000 > DeepSeek 的 393216 →
    HTTP 400「Invalid max_tokens value」→ **远端一个字节都没写**，
    看起来像"模型不会写"，其实是请求参数非法。
    """
    c = str(compat_of(step, r) or "deepseek").lower()
    return int(MODEL_MAX_TOKENS.get(c, MODEL_MAX_TOKENS["deepseek"]))


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


def compat_of(step: dict | None = None, r: dict | None = None) -> str:
    """协议口径：**默认 deepseek**，允许显式设 openai 走 OpenAI 兼容格式。

    优先级：档案 compat > 全局 compat > 全局 provider > deepseek。
    这里返回原始值、不擅自收敛 —— 未知协议要由 available() 明确拒绝，
    不能静默当成 deepseek 把专有参数发到别的厂商去。
    """
    for src in (step, r):
        v = str((src or {}).get("compat") or "").strip().lower()
        if v:
            # 厂商名即协议族（2026-11-09）：deepseek / glm / openai。
            # glm 走 OpenAI 的请求格式，但**要单独发 thinking 参数**（官方只认
            # thinking.type + reasoning_effort），所以不能和 openai 混为一族。
            if v in ("glm", "zhipu", "bigmodel"):
                return "glm"
            return v
    v = str((r or {}).get("provider") or "deepseek").strip().lower() or "deepseek"
    return "glm" if v in ("glm", "zhipu", "bigmodel") else v


# 各厂商的环境变量名（厂商是一等公民，key 各自独立）
ENV_KEYS = {
    "deepseek": ("DEEPSEEK_API_KEY",),
    "glm": ("ZHIPUAI_API_KEY", "GLM_API_KEY", "BIGMODEL_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
}


def _key(compat: str = "") -> str:
    """某个协议族的 key：设置里的显式值 > 该厂商的环境变量。

    保持**可无参调用**（自检会 monkeypatch 它）——所以兼容参数必须可选。
    厂商的环境变量各自独立（`ZHIPUAI_API_KEY` / `OPENAI_API_KEY`），
    不跨厂商兜底：把 DS 的 key 发到智谱端点必然 401（实测 2026-11-07）。
    """
    r = settings_store.get_remote()
    fam = str(compat or "").strip().lower()
    if not fam:
        fam = str(r.get("compat") or r.get("provider") or "deepseek").strip().lower()
    if fam in ("glm", "zhipu", "bigmodel"):
        fam = "glm"
    explicit = str(r.get("api_key") or "").strip()
    if explicit and fam in (str(r.get("compat") or r.get("provider") or "deepseek")
                             .strip().lower(), ""):
        return explicit
    for env in ENV_KEYS.get(fam, ()):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    return explicit


def available() -> tuple[bool, str]:
    r = settings_store.get_remote()
    if not r.get("enabled", True):
        return False, "远端协助已在设置里关闭"
    compat0 = compat_of(r=r)
    # 用**无参**调用：`_key` 的公开契约就是"可无参"（自检会 monkeypatch 成 0 参 lambda）。
    # 2026-11-10 实测：这里传了 compat0 → 自检 dispatch 时 `TypeError: <lambda>() takes 0
    # positional arguments but 1 was given`，整条远端可用性判定直接崩。
    if not _key():
        # 只在厂商卡片里填了 key 也算已配置（2026-11-21：状态红点误报"未配置"）
        try:
            from . import settings_store as _ss       # noqa: PLC0415
            if not _ss.any_configured_key():
                return False, (f"未配置 API key（设置 → 远端模型 → 厂商卡片填 apikey，"
                               f"或环境变量 {'/'.join(ENV_KEYS.get(compat0, ('DEEPSEEK_API_KEY',)))}）")
        except Exception:                                # noqa: BLE001
            return False, (f"未配置 API key（设置 → 远端模型 → 厂商卡片填 apikey，"
                           f"或环境变量 {'/'.join(ENV_KEYS.get(compat0, ('DEEPSEEK_API_KEY',)))}）")
    compat = compat_of(r=r)
    if compat not in ("deepseek", "glm", "openai"):
        return False, f"不支持的远端协议 compat={compat}（可选 deepseek / glm / openai）"
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
                     ("effort", "TEXT"), ("cost_cny", "REAL DEFAULT 0"),
                     # 2026-11-10：项目账本要靠这一列归属花费。缺了它，
                     # 任何走 INSERT 的路径都会炸 `no column named project_id`
                     # （自检 check_reuse 就是拿旧 schema 起的库，直接抓到）。
                     ("project_id", "TEXT DEFAULT ''"),
                     # 2026-11-15 成本归因：没有它就算不出"每条能力花了多少"，
                     # 也就无法区分"这钱是不是废单"。值来自 core/attribution 的上下文
                     # （模块 → 能力，按蓝图 cap.modules 机械映射），取不到就留空，绝不猜。
                     ("cap_id", "TEXT DEFAULT ''")):
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


def _reuse_key(scope: str, wire: list[dict], model: str = "") -> str:
    """确定性步骤的「同证据」指纹：认证据内容 + 模型，不认档位 / 配额 / 重试次数。

    必须含 model：换档案（flash ↔ pro，或换到别的厂商）后，同一份证据也绝不能
    复用上一个模型的答复 —— 那会让对照实验测出来的其实是旧模型的结论。
    故意不含 effort、max_tokens、step_index —— 升档重试、配额放大重试问的是同一件事，
    理应复用同一份答复。PROMPT_VERSION 保证改提示词后旧答复自动失效。
    """
    body = "\n".join(f"{m.get('role')}:{m.get('content')}" for m in wire)
    return "reuse:" + hashlib.sha1(
        f"{PROMPT_VERSION}|{scope}|{model}|{body}".encode("utf-8")).hexdigest()


def _purpose_of() -> str:
    """这次调用花在哪一步：归因上下文的 phase（写码/契约/修复…），取不到才用 handoff。"""
    try:
        from . import attribution as _at
        return str((_at.snapshot() or {}).get("phase") or "handoff")
    except Exception:                                    # noqa: BLE001
        return "handoff"


def _cap_id_of(kw: dict) -> str:
    """这笔调用归到哪些能力上：**显式传的优先**，否则读 attribution 上下文。

    没有上下文就写空串 —— 报表里会单列"未归因"，逼着把钩子补全，
    而不是让一笔钱悄悄消失在平均值里。
    """
    if kw.get("cap_id"):
        return str(kw["cap_id"])[:400]
    try:
        from . import attribution as _at
        return str((_at.snapshot() or {}).get("cap_id") or "")[:400]
    except Exception:                                    # noqa: BLE001
        return ""


def _record(**kw) -> float:
    with _REC_LOCK:
        return _record_locked(**kw)


def _record_locked(**kw) -> float:
    _ensure_table()
    step = {"model": kw.get("model", ""), "pricing": kw.get("pricing")}
    cost = cost_cny(step if kw.get("pricing") else None,
                    int(kw.get("prompt_tokens") or 0), int(kw.get("cache_hit_tokens") or 0),
                    int(kw.get("completion_tokens") or 0)) if kw.get("model") else 0.0
    # 明细行带上**项目指纹**：一个项目一本账，明细才是可归因的证据。
    # 拿不到 workspace（引擎自检/无仓库上下文）就留空 —— 绝不瞎归账。
    _pid = ""
    _ws = ""
    try:
        from . import project_ledger as _plid
        _ws = str(kw.get("workspace") or _plid.current() or "")
        _pid = _plid.project_id_of(_ws) if _ws else ""
    except Exception:                                    # noqa: BLE001
        _pid = ""
    db.execute(
        "INSERT INTO remote_usage(task_id, node_id, purpose, model, prompt_tokens,"
        " completion_tokens, cache_hit_tokens, cached_reply, ms, created_at,"
        " reasoning_tokens, offpeak, effort, cost_cny, project_id, cap_id)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("task_id", ""), kw.get("node_id", ""),
         # purpose 缺省不再写死 "handoff"（2026-11-15 实测：1749 次调用全是 handoff，
         # "按用途"这栏完全没用）→ 取归因上下文里的 phase，才看得出钱花在
         # 写码 / 契约 / 修复 / 诊断 哪一步。
         str(kw.get("purpose") or _purpose_of()),
         kw.get("model", ""), kw.get("prompt_tokens", 0), kw.get("completion_tokens", 0),
         kw.get("cache_hit_tokens", 0), 1 if kw.get("cached_reply") else 0,
         kw.get("ms", 0), db.now(),
         int(kw.get("reasoning_tokens") or 0), 1 if is_offpeak() else 0,
         str(kw.get("effort") or ""), round(cost, 6), _pid, _cap_id_of(kw)))
    # 记账就在这一行：**明细行带 project_id 就是记账**。
    # 项目账本不累加，而是从明细派生（`project_ledger.total`）——
    # 增量累加 + 定期对账会重复计数（2026-11-10 20:45 实测多算 ¥0.0745），已改纯派生。
    # 返回本次花费：调用方仍可用它做本轮的临时统计。
    return round(cost, 6)


def usage_summary(task_id: str = "") -> dict:
    """本地用量统计：总量 + 闲时/忙时 + 按**模型档案价目**算人民币花费。

    花费口径按官方公式：命中输入×命中价 + 未命中输入×未命中价 + 输出×输出价，
    空闲时段用空闲价（高峰为北京时间工作日 9-12、14-18，其余半价）。
    """
    _ensure_table()
    where, params = ("WHERE task_id=?", (task_id,)) if task_id else ("", ())
    rows = db.query("SELECT * FROM remote_usage " + where, params)
    # 阶梯只在本次统计里算一次。以前 cost_of / saved / by_model 的占位循环对**每一行**
    # 都调 ladder()（而 ladder 每次重读 settings.json），2000 行 ≈ 6000 次文件 IO，
    # 实测把 GET /v1/settings/remote 拖到超时、前端配置面板根本打不开。
    model_steps = {str(s.get("model") or ""): s
                   for s in ladder() if str(s.get("model") or "").strip()}
    # 价格表：**每个出现过的模型只查一次**。换过厂商后，用量库里的历史模型全都
    # 不在当前阶梯里，cost_cny(None) 会逐行调 pricing_for() → 重读 settings.json，
    # 实测 2112 行产生 16895 次 IO / 9.6s，把设置面板拖到超时。
    # 口径不变：没有档案的历史模型仍不分闲忙、统一用 pricing_for 的顶层价。
    _prices: dict[str, dict] = {}

    def prices_for(model: str) -> dict:
        if model not in _prices:
            step = model_steps.get(model)
            if step and step.get("pricing"):
                _prices[model] = step["pricing"]
            else:
                p = pricing_for(model)
                off = {k: p.get(k) for k in ("in_miss", "in_hit", "out")}
                # pricing_for 顶层 in_miss/in_hit/out 是空闲价，peak 是单独的一整块；
                # 旧美元价目表没有 peak 字段，此时 peak 回落 off（不分闲忙，保持旧行为）。
                peak = p.get("peak")
                _prices[model] = {"offpeak": off,
                                  "peak": peak if isinstance(peak, dict) else off}
        return _prices[model]

    def cost_of(r: dict) -> float:
        price = prices_for(r.get("model") or "")[
            "offpeak" if r.get("offpeak") else "peak"]
        miss = max(0, int(r["prompt_tokens"] or 0) - int(r["cache_hit_tokens"] or 0))
        return (miss / 1e6 * float(price.get("in_miss") or 0)
                + int(r["cache_hit_tokens"] or 0) / 1e6 * float(price.get("in_hit") or 0)
                + int(r["completion_tokens"] or 0) / 1e6 * float(price.get("out") or 0))

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
        if not (r.get("model") or "").strip():
            continue  # 本地手脚档（model 为空）不花钱，也不计缓存省钱
        price = prices_for(r.get("model") or "")[
            "offpeak" if r.get("offpeak") else "peak"]
        hit = r["cache_hit_tokens"] or 0
        saved += hit / 1e6 * (float(price.get("in_miss") or 0) - float(price.get("in_hit") or 0))
    total["cache_saved_cny"] = round(saved, 6)

    # 按模型 / 按档位分开看（"钱花在哪个模型、哪个思考强度上"）
    by_model: dict[str, list] = {}
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


def _payload_for(step: dict, r: dict, messages: list[dict], json_mode: bool,
                  tools: list[dict] | None = None,
                  tool_choice: str = 'auto') -> tuple[dict, str, str]:
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
    _mt = max_tokens_for(cap, effort, _ceiling_for(step, r))
    if _mt:
        payload["max_tokens"] = _mt
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    # 原生函数调用（DeepSeek 官方 OpenAI 兼容格式，官方文档
    # https://api-docs.deepseek.com/guides/tool_calls）：
    #   请求 {"tools":[{"type":"function","function":{name,description,parameters}}]}
    #   响应 choices[0].message.tool_calls[]（function.arguments 是 **JSON 字符串**）
    #   结果回传 {"role":"tool","tool_call_id":<id>,"content":<结果>}
    if tools:
        payload["tools"] = tools
        if tool_choice and tool_choice != "auto":
            payload["tool_choice"] = tool_choice
    compat = compat_of(step, r)
    if compat == "openai":
        # 通用 OpenAI 兼容：不认 thinking / reasoning_effort，也不需要
        # response_format（JSON 由提示词约束 + _json_of 从围栏里兜底解析）。
        # 实测发 DS 的 336136 会 400，官方示例是 65536 → 输出配额按兼容端点封顶。
        payload.pop("thinking", None)
        payload.pop("reasoning_effort", None)
        payload.pop("response_format", None)
        payload["max_tokens"] = max(1, min(int(payload.get("max_tokens") or
                                              OPENAI_OUTPUT_CEIL), OPENAI_OUTPUT_CEIL))
    elif compat == "glm":
        # GLM 官方格式（2026-11-09 核对官方文档）：
        #   thinking.type = enabled / disabled  （GLM-4.5+）
        #   reasoning_effort = max / high / low （GLM-5.2+；**没有 none**）
        # "none"（不思考）= thinking.type=disabled；但 GLM-5.3 起**不再支持禁用**，
        # 官方明确说传 disabled 会请求失败 → 所以 none 档在 GLM 上退化为 enabled + low
        # （官方迁移提示也是这么建议的：thinking 改 enabled，reasoning_effort 设 low）。
        _eff = str(effort or "none").strip().lower()
        if _eff == "none":
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = "low"
        else:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = _eff if _eff in ("max", "high", "low") else "max"
        payload.pop("response_format", None)
        # GLM 输出上限：官方 GLM-5.3/5.2/5.1/5 最大 131072（默认 65536），
        # GLM-4.6/4.5 最大 98304 —— 发超了会 400。DS 的 336136 在这里绝对不能发。
        payload["max_tokens"] = max(1, min(int(payload.get("max_tokens") or
                                              GLM_OUTPUT_CEIL), GLM_OUTPUT_CEIL))
    elif effort == "none":
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = effort
    # DeepSeek 官方：**思考模式 + `tool_choice="required"` 会 400**
    # （官方原话：switch thinking off for those requests）。
    # 所以这类请求把思考关掉 —— 反正模型只是"必须调一个工具"，不需要长思考。
    if (payload.get("thinking") or {}).get("type") == "enabled" and tool_choice == "required":
        payload["thinking"] = {"type": "disabled"}
        payload.pop("reasoning_effort", None)
    # 端点与 key **按协议族隔离**，不许跨厂商兜底。
    # 旧链是 `档案 → 全局 → 官方默认`：用户把全局配成智谱（openai 兼容）后，
    # DeepSeek 档案只要自己没填 base_url，就会继承智谱地址；key 同理。
    # 实测（2026-11-07）：DeepSeek key + deepseek-flash 模型名被发到智谱端点 → 401，
    # 错误信息却说"Key 无效或已被轮换"，把端点错配误诊成 key 失效。
    global_compat = compat_of(r=r)
    explicit_base = str(step.get("base_url") or "").strip().rstrip("/")
    global_base = str(r.get("base_url") or "").strip().rstrip("/")
    if compat == "deepseek":
        # DeepSeek 家族：只用官方端点（或档案显式填的），绝不继承 openai 兼容全局地址
        base = explicit_base or DEEPSEEK_BASE
    elif compat == "glm":
        # GLM 家族：只用智谱端点（或档案显式填的），**绝不继承 openai 全局地址**
        # （那是 OpenAI 的地址/key，发过去必然 401）。
        base = explicit_base or GLM_BASE
    else:
        base = explicit_base or global_base or OPENAI_COMPAT_BASE
    # key 同理：只有"全局服务与本协议一致"时才允许继承全局 key
    key = str(step.get("api_key") or "").strip()
    if not key:
        # 厂商级 key 兜底（厂商是一等公民：key 填在厂商上，模型档案留空即继承）
        try:
            from . import settings_store as _ss
            key = _ss.provider_key(compat)
        except Exception:                                # noqa: BLE001
            key = ""
    if not key and (global_compat == compat or not global_compat):
        # 优先用调用方传入的 r（离线自检可注入，不读本机配置）；
        # 没有才回落到 _key()（含环境变量）。_key 保持无参，自检会 monkeypatch 它。
        key = str(r.get("api_key") or "").strip() or _key()
    return payload, base, key


def _post(payload: dict, base: str, key: str, timeout: int = 300) -> tuple[dict | None, str, int]:
    """发请求，**瞬时错误自动退避重试**（网络不可达 / 429 / 5xx）。

    为什么必须有（实测 2026-11-07）：`_is_transient` 早就写好了，却**从来没人调用**。
    限流一次就被上层当成"作者这一版没写出来"，白白消耗一轮自愈 —— 模型被冤枉成"能力不够"。

    2026-11-09 修（用户反馈：「突然断网然后网络恢复的情况下远端调用没用恢复」）：
    原来重试只有 **3 次**、退避 2s/4s/8s（总窗口 ~14 秒）。断网只要超过 14 秒
    （真实断网通常几十秒到几分钟），这一轮就彻底失败 → 引擎把"网络断了"
    记成"远端写不出来" → **网络恢复后也不会自动回到正轨**。
    现在：
      · 重试次数 3 → 8；
      · **网络类错误用更长的退避**（5/10/20/30/30/30/30 ≈ 175 秒），
        限流/5xx 用原来的短退避（2/4/8/16/20…）—— 网络不通要多等，限流要快点退；
      · 总等待有上限（`SIXCODER_REMOTE_RETRY_WAIT`，默认 240 秒），到点才放弃，
        避免真的没网时无限挂着；
      · 退避带 **±20% 抖动**，多进程/多档位不会同步重试撞上去。
    """
    import os as _os
    import time as _time
    import random as _rand
    tries = max(1, int(_os.environ.get("SIXCODER_REMOTE_RETRY", "8")))
    try:
        wait_budget = float(_os.environ.get("SIXCODER_REMOTE_RETRY_WAIT", "240"))
    except ValueError:
        wait_budget = 240.0
    last = ""
    waited = 0.0
    for attempt in range(1, tries + 1):
        data, err, ms = _post_once(payload, base, key, timeout)
        if data is not None:
            return data, "", ms
        last = err
        if attempt >= tries or not _is_transient(err):
            return None, err, ms
        if waited >= wait_budget:
            return None, (last + f"（瞬时错误持续，等待 {wait_budget:.0f}s 后仍不通）")[:200], ms
        delay = _backoff_delay(attempt, err)
        # 抖动：避免多进程/多阶梯同步重试
        delay = delay * (0.8 + 0.4 * _rand.random())
        waited += delay
        _time.sleep(delay)
    return None, last, 0


# 网络不可达的特征（与"限流/服务错误"分开处理：前者要多等，后者要快点退）
_NET_MARKERS = ("请求失败", "URLError", "WinError", "gaierror", "ConnectionRefused",
                 "ConnectionReset", "ConnectionAborted", "网络", "无法连接", "NameResolution",
                 "Network unreachable", "SSLError", "RemoteDisconnected", "IncompleteRead")


def is_fatal_remote_error(err: str) -> bool:
    """这次远端失败是不是"**配置问题**"（不是模型能力问题，也不是网络抖动）。

    2026-11-09 实测：DeepSeek key 失效 → 每一次 author 调用都 401。
    401/402/403 是**换 key / 充值 / 改端点**才能解决的事：
      · 重试没有意义（同一个坏 key 再试八次还是 401）；
      · 继续跑夜班没有意义（每一张单都会立刻失败，纯白烧本地时间）；
      · 更不能记成"模型不会写"（那是把配置问题记成能力问题）。
    所以上层看到 `remote_fatal=True` 就该**立刻停机并如实报出原因**。

    判据：错误串里带 HTTP 401 / 402 / 403，或"鉴权失败"/"余额不足"字样。
    """
    e = err or ""
    return any(m in e for m in ("HTTP 401", "HTTP 402", "HTTP 403",
                                "鉴权失败", "余额不足", "key 失效", "API key"))


def is_network_error(err: str) -> bool:
    """公开版：这次失败是不是"网络不可达"（断网 / DNS / 连接被拒）。

    上层要靠它把"断网"和"远端答错了"分开记 —— 断网不是模型能力问题。
    """
    return _is_network_err(err)


def _is_network_err(err: str) -> bool:
    """这次失败是不是"网络不通"（断网 / DNS / 连接被拒）而不是"服务说不行"。"""
    e = err or ""
    return any(m in e for m in _NET_MARKERS)


def _backoff_delay(attempt: int, err: str) -> float:
    """这一轮该等多久再重试。

    网络不通 → 等更久（5/10/20/30/30…，一次断网恢复窗口能到 ~3 分钟）；
    限流/5xx → 等短一点（2/4/8/16/20…），早点重试早点抢到额度。
    """
    if _is_network_err(err):
        return float(min(30, 5 * (2 ** (attempt - 1))))
    return float(min(20, 2 ** attempt))


def _post_once(payload: dict, base: str, key: str, timeout: int = 300) -> tuple[dict | None, str, int]:
    """真正发一次请求。返回 (响应JSON, 错误串, 耗时ms)。"""
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
        # 网络类错误加一个明确标记（[网络不可达]）：上层要能把"断网"和
        # "远端答错了"分开记 —— 断网不是模型能力问题，不能记成"模型不会写"。
        msg = f"请求失败：{type(exc).__name__}: {exc}"
        if _is_network_err(msg):
            msg = "[网络不可达] " + msg
        return None, msg, int((time.time() - t0) * 1000)


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
    single = settings_store.single_effort()
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
            # 2026-11-09（断网恢复）：**网络不通时升档没有意义** ——
            # 换档位还是同一个不通的网络，只会把重试次数乘以档位数
            # （8 档 × 8 次 = 64 次无效重试）。网络类失败就停在这一档，
            # 让 `_post` 的长退避窗口去等网络恢复；恢复后下一轮自然就通了。
            if not single and _is_transient(err) and not _is_network_err(err) \
                    and idx + 1 < len(steps):
                continue                                  # 限流/服务抖动 → 升一档再试
            return {"ok": False, "error": err, "answer": "", "tried": tried}
        usage = data.get("usage") or {}
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        cache_hit = int(usage.get("prompt_cache_hit_tokens")
                        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        answer = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        _record(task_id=task_id, node_id=node_id, purpose=cache_key,
                model=step["model"],
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
        # 401 不一定是 key 失效：实测（2026-11-07）DeepSeek key 被发到智谱端点
        # 同样报 401。先让用户核对"端点/协议/key 是不是同一家"，别误导去换 key。
        return (f"鉴权失败（HTTP {code}）：先确认这个模型的「请求网址 / 协议类型 / API key」"
                f"属于同一家服务 —— 端点错配（例如 DeepSeek 模型继承了智谱地址）也会报 401。"
                f"三者一致再考虑 key 失效或已轮换，到「设置 → 远端模型」重新填一把。")
    if code == 402:
        return "余额不足（HTTP 402）：远端账户欠费，请充值或改用本地模型"
    if code == 429:
        return "被限流（HTTP 429）：稍后重试，或减少调用频率"
    return f"HTTP {code}: {detail}" if detail else f"HTTP {code}"


def _fit_messages(msgs: list[dict], limit_chars: int) -> list[dict]:
    """多轮上下文超限时怎么活：**保住第一条**（契约/代码都在里面），丢掉中间轮次。

    为什么不能像单轮那样直接截尾：多轮里最后几条是"我刚跑出来的证据+我刚交的代码"，
    截掉它们等于让模型失忆；而中间轮次的推理价值最低。丢的时候留一句醒目的标记，
    免得模型以为对话没断过。
    """
    joined = sum(len(m.get('content') or '') for m in msgs)
    if joined <= limit_chars or len(msgs) <= 2:
        return msgs
    head = msgs[0]
    tail = list(msgs[1:])
    dropped = 0
    while tail and (len(head.get('content') or '')
                    + sum(len(m.get('content') or '') for m in tail)) > limit_chars:
        tail.pop(0)
        dropped += 1
    if dropped:
        note = {'role': 'user',
                'content': f'…（中间 {dropped} 轮对话因体积上限被省略；以第一条与本条为准）…'}
        return [head, note, *tail]
    return [head, *tail]


def normalize_messages(msgs: list[dict] | None) -> list[dict] | None:
    """把内部消息形状归一成远端要的形状（官方 function calling 口径）。

    **为什么单独抽成函数**（2026-11-10）：以前这段逻辑只存在于 `complete()` 里面，
    自检只能**照抄一遍**来测 —— 于是"归一化漏了某个字段"这类错，自检永远看不见
    （照抄的当然也没漏检查）。抽出来之后，自检调的是**真代码**。

    三个必须原样带过去的字段：
      · `tool_calls`      —— 助手那轮带工具调用（content 为空）必须原样送回，
                              否则 API 找不到对应的 tool_call_id → 400 或丢结果；
      · `tool_call_id`   —— 工具结果与调用配对；
      · `reasoning_content` —— DeepSeek 官方（2026-11 核对 docs）：
                              **请求带 tools 时，必须把上一轮的 reasoning_content
                              原样回传**，否则 API 返回 400；不带 tools 时回传会被忽略。
                              漏了它 = 思考模式 + 工具循环的**第二轮**会 400，
                              而且模型会丢掉自己上一轮的思考链（写码质量下降）。
    """
    if not msgs:
        return None
    out: list[dict] = []
    for _m in msgs:
        _item = {'role': str(_m.get('role') or 'user'),
                 'content': str(_m.get('content') or '')}
        if _m.get('tool_calls'):
            _item['tool_calls'] = _m['tool_calls']
        if _m.get('tool_call_id'):
            _item['tool_call_id'] = _m['tool_call_id']
        if _m.get('reasoning_content'):
            _item['reasoning_content'] = _m['reasoning_content']
        if _item['content'].strip() or _item.get('tool_calls') or _item.get('tool_call_id'):
            out.append(_item)
    return out or None


def reasoning_tokens_of(usage: dict | None) -> int:
    """思考 token 数（**两家字段名不同**，所以要两个都认）。

    DeepSeek：`usage.completion_tokens_details.reasoning_tokens`
    GLM：官方《对话补全》参考里 usage 未必有 completion_tokens_details，
         顶层给 `usage.reasoning_tokens` 的也有。
    读不到 → 0（宁可低估，也不能让解析炸掉）。
    """
    usage = usage or {}
    return int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
               or usage.get("reasoning_tokens") or 0)


def complete(prompt: str = '', task_id: str = '', node_id: str = 'architect',
             workspace: str = '', emit=None, thinking: str | None = None,
             step_index: int | None = None, json_mode: bool = False,
             max_out: int | None = None,
             messages: list[dict] | None = None,
             reuse_scope: str | None = None,
             tools: list[dict] | None = None,
             tool_choice: str = 'auto',
             profile: dict | None = None) -> dict:
    """发一段自由文本提示，返回 {ok, answer, usage}。

    `reuse_scope`：给**确定性步骤**（蓝图/审计/契约/架构问答）用的响应复用开关。
    给了就按「scope + 完整证据内容」算指纹，同一份证据重复问直接复用上次答复，
    记一条 cached_reply=1、0 token 的用量记录。不给就完全不缓存 —— 写代码的自愈
    4 轮靠的正是同证据拿到不同答案，那类调用绝不能复用。详见 checks/check_reuse.py。

    走**单模型 + 思考强度阶梯**（core/settings_store.ladder）：一次运行只用一个模型，
    在这个模型内部按 none → low → high → max 切思考强度（强度由 core/effort.py 按需求选）。
      · step_index=None → 从第 0 档（flash:none）开始；
      · thinking="on"/"off" 是老接口，映射成档位过滤（on→跳过 none 档）；
      · 限流/网络抖动会自动升一档重试；"答得不对"不在这里重试（由夜班按阈值升档）。
      · `messages`：给多轮对话用（片内自验的"跑→看→改"就是多轮）。不给就单轮。

    多轮为什么要单开一个入口（实测动机）：单轮时每一步都要把契约+全部代码+证据重新
    塞进一个巨型提示词，模型看不到自己上一轮交了什么；多轮能让它带着自己的代码继续改，
    代价是历史会涨 —— 所以配了 `_fit_messages` 的丢中间轮策略，绝不静默截最后一轮。
    """
    msgs: list[dict] | None = normalize_messages(messages)
    text = msgs[-1]['content'] if msgs else (prompt or '')
    ok, why = available()
    if not ok:
        return {'ok': False, 'error': why, 'answer': ''}
    # 关键节点守卫：开发者模式在"把代码发往远端"前要人点头（甲方模式自动放行）
    try:
        from . import guards
        g = guards.gate_remote_call(workspace,
                                    len(prompt or '') if not msgs
                                    else sum(len(m['content']) for m in msgs),
                                    task_id, emit=emit)
        if not g.get('ok'):
            return {'ok': False, 'error': f"未获批准：{g.get('reason')}", 'answer': ''}
    except Exception:                                    # noqa: BLE001
        pass
    r = settings_store.get_remote()
    if profile:
        # **指定档案直通**（2026-11-18 多远端协作）：审核者/写作者各自用
        # 自己的档案（不同 key、不同模型）发调用，不走"当前模型"的阶梯。
        # 档案缺的字段按阶梯档位的兜底规则补（_payload_for 里统一处理）。
        _p = dict(profile)
        _p.setdefault("profile_id", str(_p.get("id") or _p.get("model") or "profile"))
        _p.setdefault("effort", "none")
        _p.setdefault("max_output_tokens", 0)
        _p.setdefault("temperature", 0.2)
        _p.setdefault("pricing", None)
        seq = [(0, _p)]
        single = True
        steps = [_p]
    else:
        steps = ladder()
    if not steps:
        return {'ok': False, 'error': '没有任何启用的远端模型档案（设置 → 远端模型档案）',
                'answer': ''}
    single = True if profile else settings_store.single_effort()
    if single:
        # 单档直通分路：只有一个思考强度，忽略 step_index / thinking，
        # 不按难度挑起点，也没有可升可降的档（见 settings_store.single_effort）。
        start = 0
    else:
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
    need, info = _tokens.needs_compression(text)
    if need:
        limit_chars = int((r.get('max_prompt_tokens') or 950000) / max(_tokens.ratio(), 0.2))
        limit_chars = min(limit_chars, int(r.get('max_prompt_chars') or 4000000))
        if msgs:
            # 多轮：丢中间轮次而不是砍最后一轮（最后一轮是刚跑出来的证据）
            msgs = _fit_messages(msgs, limit_chars)
        else:
            text = prompt[:limit_chars]
        if emit:
            emit('context_budget', {**info, 'action': '压缩', 'chars': limit_chars})
    tried = []
    err = ''
    seq = remote_steps[:1] if single else (
        [(i, s) for i, s in remote_steps if i >= start] or remote_steps[:1])
    # 同证据复用：只认证据内容，不认这次用哪个档位 / 给多少配额
    _wire0 = msgs if msgs else [{'role': 'user', 'content': text}]
    _reuse_model = str((seq[0][1] or {}).get("model") or "")
    reuse_key = _reuse_key(reuse_scope, _wire0, _reuse_model) if reuse_scope else None
    if reuse_key:
        _ensure_table()
        prev = db.query("SELECT answer FROM remote_cache WHERE key=?", (reuse_key,))
        if prev and (prev[0]["answer"] or "").strip():
            _record(task_id=task_id, node_id=node_id, purpose=reuse_scope or "",
                    model=str((seq[0][1] or {}).get("model") or ""), cached_reply=True,
                    effort=str((seq[0][1] or {}).get("effort") or ""))
            return {"ok": True, "answer": prev[0]["answer"] or "", "ms": 0,
                    "cached": True, "by": "reuse", "step": start,
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                              "reasoning_tokens": 0, "cache_hit_tokens": 0,
                              "cost_cny": 0.0},
                    "error": "同一份证据已问过远端，直接复用上次答复（0 token）"}
    for idx, step in seq:
        tag = f"{step['profile_id']}:{step['effort']}"
        _wire = _wire0
        payload, base, key = _payload_for(step, r, _wire, json_mode=json_mode,
                                           tools=tools, tool_choice=tool_choice)
        _eff = str(payload.get("reasoning_effort") or "none").lower()
        if max_out:
            # 整文件/架构设计需要长输出；档位默认 8K 只够一个函数。
            # 注意：这里也必须走 max_tokens_for，否则会把 _payload_for 算好的思考预留覆盖掉
            # （实测：只在 _payload_for 加预留时，这条一覆盖，思考又把正文挤成 1 369 token）。
            if str(compat_of(step, r)).lower() == "openai":
                # 兼容端点：只夹到官方示例的 65536，别把 DS 校准的 33 万覆盖回去
                payload['max_tokens'] = max(1, min(int(max_out), OPENAI_OUTPUT_CEIL))
            else:
                payload['max_tokens'] = max_tokens_for(min(int(max_out), CONTENT_CEIL), _eff,
                                                        _ceiling_for(step, r))
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
            # 只有真的产生了 reasoning token 且正文几乎为空，才算「思考吃光配额」。
            # none 档 / 无思考端点本来就可能只回一两个字（实测智谱回「好」= 2 token），
            # 旧口径会把它当配额不足放大重发一次：白烧一次调用，还容易撞上 429。
            # finish_reason=length 仍是硬信号，照旧放大。
            starved = fin == 'length' or (_ct > 0 and _rt > 0 and _body <= 32)
            if not starved or _attempt == 1:
                break
            # 思考吃光 / 顶到上限 → 直接把配额翻倍（再留一档余量），重发一次
            cur = int(payload.get('max_tokens') or 0)
            _ceil = _ceiling_for(step, r)
            payload['max_tokens'] = min(max(cur * 2, cur + 8000, 16000), _ceil)
            if emit:
                emit('budget_grown', {'step': tag, 'from': cur, 'to': payload['max_tokens'],
                                      'why': 'quota_starved',
                                      'observed': {'completion': _ct, 'reasoning': _rt,
                                                   'body': _body, 'finish': fin}})
        if data is None:
            tried.append(f'{tag}失败({err[:40]})')
            # 单档直通没有「升一档」这条路；瞬态错误直接如实返回，不换档重试
            # 2026-11-09（断网恢复）：网络不通时升档无意义（还是同一个不通的网络），
            # 只把重试次数乘以档位数 —— 停在这一档，让 _post 的长退避等网络恢复。
            if (not single and _is_transient(err) and not _is_network_err(err)
                    and idx + 1 < len(steps)):
                continue
            return {'ok': False, 'error': err, 'answer': '', 'tried': tried}
        usage = data.get('usage') or {}
        pt = int(usage.get('prompt_tokens') or 0)
        hit = int(usage.get('prompt_cache_hit_tokens')
                  or (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0)
        ct = int(usage.get('completion_tokens') or 0)
        rt = reasoning_tokens_of(usage)
        answer = ((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        _message = (data.get('choices') or [{}])[0].get('message') or {}
        _tool_calls = list(_message.get('tool_calls') or [])
        # 用真实 usage 校准"token/字符"比率（下次估算更准，不依赖 tokenizer）
        _chars = len(text) if not msgs else sum(len(m['content']) for m in msgs)
        _tokens.observe(_chars, pt)
        _record(task_id=task_id, node_id=node_id, model=step['model'],
                prompt_tokens=pt, completion_tokens=ct, cache_hit_tokens=hit, ms=ms,
                reasoning_tokens=rt, effort=step['effort'], pricing=step.get('pricing'))
        # 只缓存「有内容且成功」的答复：空答/解析失败不能变成下次的假命中
        if reuse_key and answer.strip():
            db.execute("INSERT OR REPLACE INTO remote_cache(key,answer,created_at) VALUES(?,?,?)",
                       (reuse_key, answer, db.now()))
        return {'ok': True, 'answer': answer, 'ms': ms, 'by': tag, 'step': idx,
                'tool_calls': _tool_calls, 'message': _message,
                # 2026-11-09：回传 finish_reason —— 调用方要靠它判"是不是被配额截断"
                # （硬信号），而不是拿自己设的 max_out 去猜正文 token 数（会误判）。
                'finish_reason': str((data.get('choices') or [{}])[0].get('finish_reason') or ''),
                'usage': {'prompt_tokens': pt, 'completion_tokens': ct,
                          'reasoning_tokens': rt, 'cache_hit_tokens': hit,
                          'cost_cny': round(cost_cny(step, pt, hit, ct), 6)}}
    return {'ok': False, 'error': err or '阶梯已走完', 'answer': '', 'tried': tried}
