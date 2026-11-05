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


def ladder() -> list[dict]:
    """把档案摊成**阶梯**：按 priority 升序，每个档案内部再按 efforts 升序。

    默认就是用户指定的八档：
        flash:none → flash:low → flash:high → flash:max
        → pro:none → pro:low → pro:high → pro:max
    """
    steps: list[dict] = []
    if get_remote().get("local_first", True):
        # 第 0 档 = 本地模型（0 token）。本地连续失败到阈值才上远端八档。
        steps.append({"profile_id": "local", "label": "本地模型", "model": "",
                      "effort": "-", "max_output_tokens": 0, "temperature": 0.2,
                      "base_url": "", "api_key": "", "pricing": None})
    for p in models():
        if not p.get("enabled", True):
            continue
        for eff in p.get("efforts") or ["none"]:
            steps.append({"profile_id": p["id"], "label": p["label"], "model": p["model"],
                          "effort": eff, "max_output_tokens": p["max_output_tokens"],
                          "temperature": p["temperature"], "base_url": p["base_url"],
                          "api_key": p["api_key"], "pricing": p["pricing"]})
    return steps


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
        elif k == "api_key":
            v = str(v or "").strip()
        elif k == "pricing":
            v = _norm_pricing(v)
            if not v:
                continue
        elif k == "offpeak_window":
            v = _norm_window(v)
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
        "base_url": r.get("base_url", ""),
        "model": r.get("model", ""),
        "key_configured": bool(effective),
        "key_source": "settings" if r.get("api_key") else ("env" if env_key else ""),
        "key_masked": mask(effective),
        "max_output_tokens": int(r.get("max_output_tokens", 0) or 0),
        "max_prompt_chars": int(r.get("max_prompt_chars", 2000000)),
        "max_prompt_tokens": int(r.get("max_prompt_tokens", 950000)),
        "escalate_after": int(r.get("escalate_after", 2)),
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
