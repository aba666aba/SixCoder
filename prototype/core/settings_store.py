"""设置存储：远端 API 的 base_url / model / api_key 可随时在设置里更新。

约定（对齐 DSH 的做法）：
- 配置文件只做"最后写入者生效"，每次请求重新读取 → 改设置不需要重启；
- 读取时永远不把 api_key 原样返回（只回掩码 + 是否已配置）；
- 环境变量 DEEPSEEK_API_KEY 优先级低于设置里的显式值？——反过来：设置里显式填了就用设置的，
  否则回落到环境变量，两者都没有就视为"远端不可用"。
"""
from __future__ import annotations

import json
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "data" / "settings.json"

DEFAULTS = {
    "remote": {
        "enabled": True,
        "provider": "deepseek",
        # 空 = 跟随 provider（默认 deepseek）；设 "openai" 走 OpenAI 兼容格式。
        # 档案级 compat 优先于这里，方便一条档案接第三方端点、其余仍是 DS。
        "compat": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key": "",
        # **真正的上限按 token 算**：官方 1M 上下文，输入+输出共用；整文件模式给 32K 输出，
        # 所以输入的安全上限约 950K tokens。字符上限只是辅助（字符→token 比率随内容漂）。
        "max_prompt_tokens": 950000,
        # 这两个数字原来拍的是 4000 / 14000（省钱口径）。官方文档口径（2026-10 页）：
        # 上下文 1M、输出最大 384K；max_tokens 不设时非思考默认 8K、思考 64K（max 档 128K）。
        # 8000 / 200000 只是我们自己的"单次安全上限"，不是模型上限。
        "max_output_tokens": 0,   # 0 = 自动（按任务规模算配额）
        "max_prompt_chars": 2000000,
        # 同一档连续失败几次才升到下一档（用户口径："低级模型多次无法解决才调下一层"）
        "escalate_after": 1,
        # 校准值（可手改）：思考预留 / 正文下限 / 单次上限 / 单模块预算 / 写代码思考封顶。
        # 这些数字**不该硬编码**——它们随模型与任务规模变化，由 core/calibration.py
        # 按真实账本自动重算（auto=True 时每轮收工后尝试一次）。
        "calibration": {
            "source": "defaults",
            "reasoning_reserve": {"low": 12000, "high": 24000, "max": 40000},
            "content_floor": 12000,
            "safety_cap": 128000,
            "module_budget_cny": 0.15,
            "code_effort_ceiling": "high",
            "auto": True,
            "min_samples": 20,
            "updated_at": "",
        },
        # 开发者模式的远端开关：默认**关**（本地模型自己写）。
        # 甲方模式强制开启；只读模式强制关闭 —— 见 core/policy.py 的 remote_allowed()。
        "dev_remote": False,
        # 省钱优先：第 0 档用**本地模型**（免费），本地做不出来再上远端八档。
        # 关掉它就是纯远端阶梯（快、但每单都烧 token）。
        # 甲方模式已经改成"整文件由远端写"，本地不再参与写代码（只做委托与机械验证）。
        # 这个开关保留给实验/开发者场景：打开后阶梯第 0 档是本地模型。
        "local_first": False,
        "thinking": "off",
        # ---------------------------------------------------------------
        # 模型档案（可增删改）：夜班按 priority 升序、每档再按 efforts 顺序组成阶梯。
        # 默认八档（用户指定）：flash:none → low → high → max → pro:none → low → high → max
        # 价格单位：人民币 / 百万 tokens（官方 2026-10 价目表，空闲时段为高峰的一半）
        "models": [
            {
                "id": "flash", "label": "DeepSeek V4.1 Flash", "model": "deepseek-flash",
                "base_url": "", "api_key": "", "enabled": True, "priority": 10,
                "efforts": ["none", "low", "high", "max"],
                # max_output_tokens=0 → 由 tokens.output_budget 按任务规模算（唯一真相源）
                "max_output_tokens": 0, "temperature": 0.2,
                "pricing": {
                    "offpeak": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0},
                    "peak": {"in_hit": 0.04, "in_miss": 2.0, "out": 8.0},
                },
            },
            {
                # pro 默认**关掉**：官方价 flash 高峰 2/8 元 vs pro 9/27 元（3~4 倍），
                # 而架构设计与整模块实现 flash 基本够用。要用就在界面把 enabled 打开。
                "id": "pro", "label": "DeepSeek V4 Pro（默认不启用）", "model": "deepseek-v4-pro",
                "base_url": "", "api_key": "", "enabled": False, "priority": 20,
                "efforts": ["none", "low", "high", "max"],
                "max_output_tokens": 0, "temperature": 0.2,
                "pricing": {
                    "offpeak": {"in_hit": 0.15, "in_miss": 4.5, "out": 13.5},
                    "peak": {"in_hit": 0.30, "in_miss": 9.0, "out": 27.0},
                },
            },
        ],
        # 价目表（美元 / 每百万 token）—— 旧字段，保留给老配置；新逻辑读 models[].pricing
        "pricing": {
            "deepseek-flash": {"in_miss": 0.28, "in_hit": 0.028, "out": 0.42,
                               "offpeak_ratio": 0.5},
            "deepseek-chat": {"in_miss": 0.27, "in_hit": 0.07, "out": 1.10,
                              "offpeak_ratio": 0.5},
        },
        # 高峰时段（官方：北京时间周一至周五 9:00-12:00、14:00-18:00，法定节假日除外；
        # 其余时段含周末全天为空闲，价格减半）
        "peak_window": {"ranges": ["09:00-12:00", "14:00-18:00"], "weekdays_only": True,
                        "tz": "Asia/Shanghai"},
        "offpeak_window": {"start": "00:30", "end": "08:30", "tz": "UTC"},
    }
}


def load() -> dict:
    data = json.loads(json.dumps(DEFAULTS))
    if SETTINGS.exists():
        try:
            raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            raw = {}
        for k, v in (raw or {}).items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
    return data


def save(data: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_remote() -> dict:
    return load().get("remote", {})


def _norm_pricing(raw: dict) -> dict:
    """价目表规范化：只保留合法数值字段，非法项丢弃（不让一个手滑把配置写坏）。"""
    out: dict = {}
    for model, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            continue
        row = {}
        for k in ("in_miss", "in_hit", "out"):
            try:
                row[k] = max(0.0, float(spec.get(k)))
            except (TypeError, ValueError):
                pass
        try:
            ratio = float(spec.get("offpeak_ratio"))
            row["offpeak_ratio"] = min(1.0, max(0.0, ratio))
        except (TypeError, ValueError):
            pass
        if row:
            out[str(model)[:64]] = row
    return out


def _norm_window(raw: dict) -> dict:
    def hhmm(v: str, default: str) -> str:
        try:
            h, m = str(v).split(":")
            h, m = int(h), int(m)
            if 0 <= h < 24 and 0 <= m < 60:
                return f"{h:02d}:{m:02d}"
        except Exception:                              # noqa: BLE001
            pass
        return default
    raw = raw or {}
    return {"start": hhmm(raw.get("start", "00:30"), "00:30"),
            "end": hhmm(raw.get("end", "08:30"), "08:30"),
            "tz": str(raw.get("tz") or "UTC")[:16]}


def _norm_peak_window(raw: dict) -> dict:
    """高峰时段（is_offpeak 真正读的字段）：ranges / weekdays_only / tz。

    规范化到可判定形态，非法区间一律丢弃（不许界面手滑把时段写坏导致全天空闲，
    那样成本会静默算错）。空 ranges = 用默认两段。
    """
    raw = raw or {}
    ranges: list[str] = []
    for item in (raw.get("ranges") or []):
        try:
            a, b = str(item).split("-")
            ha, ma = (int(x) for x in a.strip().split(":"))
            hb, mb = (int(x) for x in b.strip().split(":"))
            if not (0 <= ha < 24 and 0 <= ma < 60 and 0 <= hb < 24 and 0 <= mb < 60):
                continue
            if hb * 60 + mb <= ha * 60 + ma:
                continue
            ranges.append(f"{ha:02d}:{ma:02d}-{hb:02d}:{mb:02d}")
        except (ValueError, AttributeError, TypeError):
            continue
    if not ranges:
        ranges = ["09:00-12:00", "14:00-18:00"]
    return {"ranges": ranges[:8], "weekdays_only": bool(raw.get("weekdays_only", True)),
            "tz": str(raw.get("tz") or "Asia/Shanghai")[:32]}


DEFAULT_PEAK_WINDOW = {"ranges": ["09:00-12:00", "14:00-18:00"],
                       "weekdays_only": True, "tz": "Asia/Shanghai"}


EFFORTS = ("none", "low", "high", "max")
EFFORT_ALIAS = {"off": "none", "minimal": "low", "medium": "high", "xhigh": "high",
                "disabled": "none", "enabled": "high", "": "none"}


def _norm_effort(v: str) -> str:
    """思考强度规范化：别名先映射，合法值原样保留，非法值当 none。"""
    key = str(v if v is not None else "").strip().lower()
    if key in EFFORTS:
        return key
    return EFFORT_ALIAS.get(key, "none")


def _norm_efforts(raw) -> list[str]:
    """思考档位列表：只保留 none/low/high/max，去重且按固定强度升序。"""
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    got = {_norm_effort(x) for x in (raw or [])}
    return [e for e in EFFORTS if e in got]


def _norm_model_profile(raw: dict) -> dict:
    """单个模型档案规范化。非法/越界一律收敛，绝不让手滑把配置写坏。"""
    raw = raw or {}

    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, type(default)(v)))
        except (TypeError, ValueError):
            return default

    mid = str(raw.get("id") or "").strip()[:32]
    model = str(raw.get("model") or "").strip()[:64]
    compat = str(raw.get("compat") or "").strip().lower()
    if compat not in ("", "deepseek", "openai"):
        compat = ""                  # 非法值收敛到「未设置」，交给下游回落，不猜协议
    # 档案没写协议时，按**模型名**定族：deepseek-* 一律是 DeepSeek 协议。
    # 不这么定的话，空 compat 会继承全局 compat（可能已被配成 openai），
    # 结果把 DS 的 reasoning_effort 丢掉、还继承别家端点的 base_url ——
    # 实测（2026-11-07）：DeepSeek key 被发到智谱端点，报 401，看起来像 key 失效。
    # 其它模型仍保持空 = 跟随全局，不擅自替用户拍板。
    if not compat and str(raw.get("model") or "").strip().lower().startswith("deepseek-"):
        compat = "deepseek"
    pricing = raw.get("pricing") or {}

    def price_block(b):
        b = b or {}
        return {"in_hit": num(b.get("in_hit"), 0.0, 1e4, 0.0),
                "in_miss": num(b.get("in_miss"), 0.0, 1e4, 0.0),
                "out": num(b.get("out"), 0.0, 1e4, 0.0)}

    return {
        "id": mid or model,
        "label": str(raw.get("label") or mid or model)[:48],
        "model": model,
        "compat": compat,
        "base_url": str(raw.get("base_url") or "").strip()[:200],
        "api_key": str(raw.get("api_key") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "priority": num(raw.get("priority"), -9999, 9999.0, 10.0),
        "efforts": _norm_efforts(raw.get("efforts")) or ["none"],
        "max_output_tokens": num(raw.get("max_output_tokens"), 0, 393216, 0),
        "temperature": num(raw.get("temperature"), 0.0, 2.0, 0.2),
        "pricing": {"offpeak": price_block(pricing.get("offpeak")),
                    "peak": price_block(pricing.get("peak") or pricing.get("offpeak"))},
    }


def _norm_profiles(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        p = _norm_model_profile(item)
        if not p["model"]:
            continue
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
    out.sort(key=lambda p: (p["priority"], p["id"]))
    return out


def models() -> list[dict]:
    """当前所有远端模型档案（按优先顺序）。空配置时回落到内置两条。"""
    got = _norm_profiles(get_remote().get("models"))
    if got:
        return got
    return _norm_profiles(DEFAULTS["remote"]["models"])


def save_models(profiles: list[dict]) -> list[dict]:
    """整表写回（界面的增/删/改最后都收敛到这里）。返回规范化后的结果。"""
    data = load()
    remote = data.setdefault("remote", {})
    clean = _norm_profiles(profiles)
    remote["models"] = clean
    save(data)
    return clean


def active_model() -> dict | None:
    """运行期**唯一**使用的模型档案（单模型）。

    解析顺序（都可解释，绝不抛错）：
      1. `remote.active_model` 填的档案 id；
      2. 档案表里 `model` 字段等于平铺字段 `remote.model` 的那条
         （界面只填 base_url / api_key / model 三个框，所以这条是主路径）；
      3. 第一个 enabled 的档案；
      4. 都没有 → 用平铺字段临时合成一条（保持"只填链接参数就能用"）。
    返回 None 表示"一个可用的远端模型都没有"。
    """
    r = get_remote()
    profs = models()
    want_id = str(r.get("active_model") or "").strip()
    if want_id:
        for p in profs:
            # 显式 pin 的档案**不看 enabled**：enabled 只用于"自动解析"时挑选，
            # 用户明确点了"用这个"就该用它（否则会在界面上出现"点了没反应"）。
            if p["id"] == want_id:
                return p
    flat_model = str(r.get("model") or "").strip()
    if flat_model:
        for p in profs:
            if p.get("model") == flat_model and p.get("enabled", True):
                return p
    for p in profs:
        if p.get("enabled", True):
            return p
    if flat_model or str(r.get("base_url") or "").strip() or str(r.get("api_key") or "").strip():
        return _norm_model_profile({"id": flat_model or "flat", "label": flat_model or "平铺配置",
                                    "model": flat_model or "deepseek-flash",
                                    "compat": r.get("compat") or r.get("provider") or "deepseek",
                                    "base_url": r.get("base_url") or "",
                                    "api_key": r.get("api_key") or "",
                                    "enabled": True})
    return None


def ladder() -> list[dict]:
    """阶梯 = **当前这一个模型**的思考强度序列（none → low → high → max）。

    2026-11-05 用户口径：远端是"**单模型 + 自动切思考强度**"，不是"多模型 × 多强度"。
    所以其它档案虽然可以保存（各自带 base_url / api_key / model，方便随时切换），
    **不参与阶梯**：一次运行只用一个模型，只在这个模型内部换思考强度。

    `local_first=True` 时第 0 档仍是本地模型（它也是"一个模型"，同样不构成多模型）。
    """
    steps: list[dict] = []
    r = get_remote()
    default_compat = str(r.get("compat") or r.get("provider") or "deepseek").strip().lower()
    if r.get("local_first", True):
        # 第 0 档 = 本地模型（0 token 的手脚档，省 token）。本地连续失败到阈值才升到远端主力。
        steps.append({"profile_id": "local", "label": "本地模型", "model": "",
                      "effort": "-", "max_output_tokens": 0, "temperature": 0.2,
                      "compat": "deepseek", "base_url": "", "api_key": "", "pricing": None})
    p = active_model()
    if not p:
        return steps
    for eff in p.get("efforts") or ["none"]:
        steps.append({"profile_id": p["id"], "label": p["label"], "model": p["model"],
                      "effort": eff, "max_output_tokens": p["max_output_tokens"],
                      "temperature": p["temperature"],
                      "compat": p.get("compat") or default_compat,
                      "base_url": p["base_url"], "api_key": p["api_key"],
                      "pricing": p["pricing"]})
    return steps


def ladder_step(idx: int) -> dict | None:
    """取阶梯里第 idx 档（越界返回 None）。

    住在 settings_store 而不是调用方：阶梯是**设置层**的概念，
    规格合成（contractgen）与夜班编排（company）都只是它的使用者。
    （2026-11-05 从 contractgen 搬过来 —— 那边管契约，不该管阶梯。）
    """
    try:
        steps = ladder()
    except Exception:                                    # noqa: BLE001
        return None
    i = int(idx or 0)
    return steps[i] if 0 <= i < len(steps) else None


def single_effort() -> bool:
    """当前是否只有**一个思考强度** → 走「单档直通」分路。

    OpenAI 兼容端点（智谱 / 腾讯混元等）通常不认 reasoning_effort 的多档切换，
    档案只配一档时也一样。用户口径：这种情况直接走另一条分路，
    **不做思维强度提升和降阶**，因为只有一个强度可升可降。
    DS 默认多档阶梯仍走原来的升档 / 难度路由语义。
    """
    try:
        r = get_remote()
        p = active_model()
        if not p:
            return False
        effs = [str(e).strip().lower() for e in (p.get("efforts") or [])
                if str(e or "").strip()]
        compat = str(p.get("compat") or r.get("compat") or r.get("provider")
                     or "deepseek").strip().lower()
        return compat == "openai" or len(effs) <= 1
    except Exception:                                    # noqa: BLE001
        return False


def strongest_step_index() -> int:
    """阶梯里**最强的那一档**的下标：规格应该由它生成。

    为什么不是第 0 档：规格（接口/数据形状/错误语义/示例）决定后面每个模块的对错，
    是整条链上最高杠杆的一步；用最便宜的档位生成规格再固化，
    等于用最弱的算力做最重要的决定（旧行为，2026-11-05 改掉）。
    """
    try:
        steps = ladder()
    except Exception:                                    # noqa: BLE001
        return 0
    idx = [i for i, s in enumerate(steps) if str(s.get("model") or "").strip()]
    return idx[-1] if idx else 0


def update_remote(patch: dict) -> tuple[dict, list[str]]:
    """只写"真的变了"的字段；没变化就不落盘（打开界面/重复保存都不会改参数）。

    返回 ({新的 remote 配置}, [实际改动的字段名])。
    """
    data = load()
    remote = data.setdefault("remote", {})
    changed: list[str] = []
    for k, v in (patch or {}).items():
        if k in ("max_output_tokens", "max_prompt_chars"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            v = max(256, min(4000000, v))
        elif k in ("local_first", "dev_remote"):
            v = bool(v)
        elif k == "escalate_after":
            try:
                v = max(1, min(9, int(v)))
            except (TypeError, ValueError):
                continue
        elif k == "models":
            v = _norm_profiles(v)
            if not v:
                continue
        elif k == "compat":
            # 只认三个值；非法/空一律收敛成「跟随默认」，绝不把不认识的协议写进配置
            v = str(v or "").strip().lower()
            if v not in ("deepseek", "openai"):
                v = ""
        elif k == "api_key":
            v = str(v or "").strip()
        elif k == "active_model":
            # 空 = 自动（按平铺 model / 第一个 enabled 解析）；填了就必须是存在的档案 id
            v = str(v or "").strip()
            if v and not any(p["id"] == v for p in models()):
                continue                              # 不存在的 id 直接忽略，别把配置写坏
        elif k == "pricing":
            v = _norm_pricing(v)
            if not v:
                continue
        elif k == "offpeak_window":
            v = _norm_window(v)
        elif k == "peak_window":
            v = _norm_peak_window(v)
        old = remote.get(k)
        if k == "configured_at":
            remote[k] = v
            continue
        if old == v:
            continue                              # 值没变 → 不动，也不标记
        remote[k] = v
        changed.append(k)
    if changed:
        save(data)
    return remote, changed


def mask(key: str) -> str:
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"



def sanitize_profile(p: dict) -> dict:
    """模型档案脱敏：api_key 永远不放进对外视图（只给 bool + 掩码）。

    实测踩过：/v1/settings/remote 的响应里 
emote.models[i].api_key 带出来了 ——
    即使当时是空串，也意味着只要配过就会泄漏。这里是唯一出口，统一剥掉。
    """
    out = {k: v for k, v in (p or {}).items() if k != "api_key"}
    key = (p or {}).get("api_key") or ""
    out["key_configured"] = bool(key)
    out["key_masked"] = mask(key)
    return out


def public_remote(reveal: bool = False) -> dict:
    """给 UI/CLI 的视图：默认脱敏；reveal=True 时额外返回明文（仅本机回环接口可用）。"""
    r = get_remote()
    import os
    env_key = os.environ.get("DEEPSEEK_API_KEY", "")
    effective = r.get("api_key") or env_key
    out = {
        "enabled": bool(r.get("enabled", True)),
        "provider": r.get("provider", "deepseek"),
        "compat": r.get("compat") or r.get("provider") or "deepseek",
        "base_url": r.get("base_url", ""),
        "model": r.get("model", ""),
        "key_configured": bool(effective),
        "key_source": "settings" if r.get("api_key") else ("env" if env_key else ""),
        "key_masked": mask(effective),
        "max_output_tokens": int(r.get("max_output_tokens", 0) or 0),
        "max_prompt_chars": int(r.get("max_prompt_chars", 2000000)),
        "max_prompt_tokens": int(r.get("max_prompt_tokens", 950000)),
        "escalate_after": int(r.get("escalate_after", 2)),
        "active_model": str((active_model() or {}).get("id") or ""),
        "active_model_pinned": str(r.get("active_model") or ""),
        "local_first": bool(r.get("local_first", True)),
        "dev_remote": bool(r.get("dev_remote", False)),
        "configured_at": r.get("configured_at", ""),
        "key_plain": effective if reveal else "",
        "pricing": r.get("pricing") or {},
        "offpeak_window": r.get("offpeak_window") or {},
        "peak_window": r.get("peak_window") or {},
        # 模型档案 + 由它摊出来的阶梯（界面直接渲染这两块，不用自己算）
        # **api_key 一律剥掉**：实测接口曾把 models[i].api_key 带出来（当时是空串，
        # 但只要配过就会泄漏）。对外只给 key_configured + 掩码。
        "models": [sanitize_profile(p) for p in models()],
        "ladder": [{"profile_id": s["profile_id"], "model": s["model"],
                    "effort": s["effort"]} for s in ladder()],
    }
    return out
