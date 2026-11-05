"""把"拍出来的常数"换成**可配置 + 按真实账本自动校准**。

为什么需要这个模块（用户 2026-11-05 指出的问题）：
    这次为了让自生成跑通，我往代码里塞了一批常数 —— 思考预留 12k/24k/40k、
    上限 128k、写代码封顶 high、单模块预算 ¥0.15。它们全都来自**少数几次观测**，
    换个模型、换个任务规模就可能全错。硬编码这类经验值 = 把过拟合钉进产品。

做法（三件事）：
    1. **可配置**：设置里有一份 `remote.calibration`，用户可以手改，也能关掉自动校准；
    2. **按数据校准**：从远端账本（remote_usage）+ 写作台账（author_attempts.jsonl）
       重新算出该用多少，**有足够样本才改**（默认 ≥20 次），改动幅度小于 15% 不动（防抖）；
    3. **永远兜底**：校准失败/样本不足/读数异常 → 用上次的值或内置默认值，绝不让生产停摆。

校准出来的东西：
    reasoning_reserve[effort]  每个思考档该预留多少 token（按该档 reasoning 的 p95 × 1.3）
    content_floor              正文至少要留多少（按 author 正文的 p90 × 1.2）
    safety_cap                 单次调用上限（不低于 128k，且 ≥ 实测输出 p99 × 3）
    module_budget_cny          单模块预算（按模块花费 p75 × 1.5）
    code_effort_ceiling        写代码允许升到哪个思考档（按各档**成功率**选，不按猜测）
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

# 内置默认值：校准不出东西时用它（这些是"上次实测 + 留足余量"的结果，不是拍脑袋的初值）
DEFAULTS: dict = {
    "source": "defaults",
    "reasoning_reserve": {"low": 12000, "high": 24000, "max": 40000},
    "content_floor": 12000,
    "safety_cap": 128000,
    "module_budget_cny": 0.15,
    "cost_per_func_cny": 0.0,       # 账本学出来的"每函数花费"（0 = 还没数据，用默认估算）
    "code_effort_ceiling": "high",
    "auto": True,
    "min_samples": 20,
    "updated_at": "",
    "basis": {},
}

RESERVE_MIN, RESERVE_MAX = 4000, 120000
CONTENT_MIN, CONTENT_MAX = 2000, 64000
MODBUDGET_MIN, MODBUDGET_MAX = 0.03, 1.0
SAFETY_MIN = 128000
DEFAULT_COST_PER_FUNC = 0.06   # 没数据时按"一个函数约 ¥0.06"估（实测两个小项目分别 ≈ ¥0.017 与 ¥0.08）
CEILINGS = ("none", "low", "high", "max")
ATTEMPTS_FILE = ".sixcoder/author_attempts.jsonl"


def _pct(values: list[float], q: float) -> float:
    """分位数（不插值，取最近的序位；空列表返回 0）。"""
    xs = sorted(float(v) for v in values if v is not None)
    if not xs:
        return 0.0
    i = min(len(xs) - 1, max(0, int(math.ceil(q * len(xs))) - 1))
    return xs[i]


def module_budget_for(funcs: int, cur: dict | None = None) -> float:
    """**按模块大小**给单模块预算（而不是所有模块一个固定值）。

    为什么不能一刀切（2026-11-05 通用性测试）：`csvtool/cli.py` 有 4 个函数、103 行，
    而 `csvtool/numeric.py` 只有 1 个函数、14 行 —— 同一个 ¥0.15 对一个太紧、对一个太松。
    实测结果就是 cli.py 打不动（1/4 通过）、预算被吃穿。

    口径：用账本里学到的"**每函数花费**"（cost ÷ 该次写的函数数），
    乘本次模块的函数数，再乘 1.6 的余量（要允许重试）。
    """
    cal = cur or current()
    per_func = float(cal.get("cost_per_func_cny") or 0) or DEFAULT_COST_PER_FUNC
    want = per_func * max(1, int(funcs or 1)) * 1.6
    lo = float(cal.get("module_budget_cny") or DEFAULTS["module_budget_cny"])
    return round(max(MODBUDGET_MIN, min(MODBUDGET_MAX, max(lo, want))), 3)


def observe(days: int = 14, ws: Path | None = None) -> dict:
    """从远端账本读观测：每档思考的 reasoning / 正文，以及每个模块的花费。"""
    out = {"by_effort": {}, "author_content": [], "module_cost": {}, "calls": 0}
    try:
        from . import db
        rows = db.query(
            "SELECT node_id, effort, completion_tokens, reasoning_tokens, cost_cny "
            "FROM remote_usage WHERE created_at >= datetime('now','localtime', ?)",
            (f"-{int(days)} days",))
    except Exception:                                    # noqa: BLE001
        rows = []
    for r in (rows or []):
        out["calls"] += 1
        eff = str(r.get("effort") or "none").lower()
        comp = int(r.get("completion_tokens") or 0)
        reason = int(r.get("reasoning_tokens") or 0)
        body = max(0, comp - reason)
        if eff != "none":
            out["by_effort"].setdefault(eff, []).append(reason)
        node = str(r.get("node_id") or "")
        if node.startswith("author:"):
            out["author_content"].append(body)
            key = node
            out["module_cost"][key] = out["module_cost"].get(key, 0.0) + float(
                r.get("cost_cny") or 0.0)
    return out


def read_attempts(ws: Path | None = None) -> list[dict]:
    """读"写作尝试台账"（每次 author 调用记一条：档位 / 成败 / 花费）。"""
    if ws is None:
        return []
    p = Path(ws) / ATTEMPTS_FILE
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def record_attempt(ws: Path | None, *, path: str, effort: str, ok: bool,
                   cost_cny: float = 0.0, funcs: int = 0) -> None:
    """记一条写作尝试（给"按成功率校准封顶"和"按模块大小校准预算"提供证据）。"""
    if ws is None:
        return
    try:
        p = Path(ws) / ATTEMPTS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "path": path,
                                "effort": str(effort or "none").lower(),
                                "ok": bool(ok), "funcs": int(funcs or 0),
                                "cost_cny": round(float(cost_cny or 0.0), 5)},
                               ensure_ascii=False) + "\n")
    except Exception:                                    # noqa: BLE001
        pass


def suggest(ws: Path | None = None, days: int = 14, min_samples: int = 20) -> dict:
    """按观测算出建议值 + 依据。样本不够的项**保持原样**（不猜）。"""
    obs = observe(days=days, ws=ws)
    cur = current()
    out = {"reasoning_reserve": dict(cur.get("reasoning_reserve") or {}),
           "content_floor": int(cur.get("content_floor") or DEFAULTS["content_floor"]),
           "safety_cap": int(cur.get("safety_cap") or DEFAULTS["safety_cap"]),
           "module_budget_cny": float(cur.get("module_budget_cny")
                                      or DEFAULTS["module_budget_cny"]),
           "code_effort_ceiling": str(cur.get("code_effort_ceiling") or "high"),
           "basis": {}, "changed": []}

    # 1) 思考预留：**按观测最大值 × 1.2**，不是 p95。
    #
    # 为什么不能用 p95（我第一版就错在这）：这几项是**上限（cap）**，不是目标值，
    # 而 cap 的代价是**不对称**的 ——
    #   · 给低了：那一次调用被截断（正文写不完/JSON 断），这一次的钱全白花，还要重写一轮；
    #   · 给高了：**几乎不花钱**（按实际生成计费），只是允许它想更久，而想多久另有单模块预算兜。
    # 实测（2026-11-05 round-01a 第 6 次）：按 p95×1.3 校准（high 14 300）后，
    # 同一需求从 20/20 掉到 15/20（rounding 又被截断）；而按 max×1.2（high 18 918）就够。
    for eff, xs in (obs.get("by_effort") or {}).items():
        if len(xs) < min_samples:
            out["basis"][f"reserve:{eff}"] = f"样本 {len(xs)} < {min_samples}，沿用现值"
            continue
        peak = max(float(v) for v in xs)
        want = int(max(RESERVE_MIN, min(RESERVE_MAX, peak * 1.2)))
        old = int(out["reasoning_reserve"].get(eff) or 0)
        if old and abs(want - old) / max(1, old) < 0.15:
            out["basis"][f"reserve:{eff}"] = f"峰值={int(peak)}，建议 {want}≈现值 {old}，不动"
            continue
        out["reasoning_reserve"][eff] = want
        out["basis"][f"reserve:{eff}"] = f"n={len(xs)} 峰值={int(peak)} → {old}→{want}"
        out["changed"].append(f"reasoning_reserve.{eff}")

    # 2) 正文下限：按 author 正文 p90 × 1.2
    bodies = obs.get("author_content") or []
    if len(bodies) >= min_samples:
        want = int(max(CONTENT_MIN, min(CONTENT_MAX, _pct(bodies, 0.90) * 1.2)))
        old = int(out["content_floor"])
        if abs(want - old) / max(1, old) >= 0.15:
            out["content_floor"] = want
            out["changed"].append("content_floor")
            out["basis"]["content_floor"] = f"n={len(bodies)} p90={int(_pct(bodies, 0.90))} → {old}→{want}"
        else:
            out["basis"]["content_floor"] = f"p90={int(_pct(bodies, 0.90))}，建议 {want}≈现值 {old}，不动"
    else:
        out["basis"]["content_floor"] = f"样本 {len(bodies)} < {min_samples}，沿用现值"

    # 3) 上限：不低于 128k，且 ≥ 实测单次输出**峰值** × 4（同样是 cap，按峰值不按分位）
    outs = [c for xs in (obs.get("by_effort") or {}).values() for c in xs]
    if outs:
        want = int(max(SAFETY_MIN, max(float(v) for v in outs) * 4))
        if want != int(out["safety_cap"]) and abs(want - int(out["safety_cap"])) / max(
                1, int(out["safety_cap"])) >= 0.15:
            out["safety_cap"] = want
            out["changed"].append("safety_cap")
            out["basis"]["safety_cap"] = f"峰值={int(max(outs))} ×4 → {want}"

    # 4) 单模块预算：按模块总花费 p75 × 1.5
    mods = list((obs.get("module_cost") or {}).values())
    if len(mods) >= max(3, min_samples // 4):
        want = round(max(MODBUDGET_MIN, min(MODBUDGET_MAX, _pct(mods, 0.75) * 1.5)), 3)
        old = float(out["module_budget_cny"])
        if abs(want - old) / max(1e-6, old) >= 0.25:
            out["module_budget_cny"] = want
            out["changed"].append("module_budget_cny")
            out["basis"]["module_budget_cny"] = f"n={len(mods)} p75={_pct(mods, 0.75):.3f} → {old}→{want}"

    # 5) 写代码思考封顶 + 每函数花费：都从"写作尝试台账"里学
    att = read_attempts(ws)
    if att:
        rate: dict[str, list[bool]] = {}
        for a in att:
            rate.setdefault(str(a.get("effort") or "none"), []).append(bool(a.get("ok")))
        good = [(CEILINGS.index(e), e) for e, xs in rate.items()
                if e in CEILINGS and len(xs) >= 3 and sum(xs) / len(xs) >= 0.5]
        if good:
            want = max(good)[1]
            old = str(out["code_effort_ceiling"])
            if want != old:
                out["code_effort_ceiling"] = want
                out["changed"].append("code_effort_ceiling")
            out["basis"]["code_effort_ceiling"] = "各档成功率 " + ", ".join(
                f"{e}={sum(xs)}/{len(xs)}" for e, xs in sorted(rate.items())) + f" → {want}"
        else:
            out["basis"]["code_effort_ceiling"] = "各档样本不足或成功率都 <50%，沿用现值"
        # 每函数花费：cost ÷ funcs（只算记了函数数的样本）
        per = [float(a["cost_cny"]) / int(a["funcs"]) for a in att
               if int(a.get("funcs") or 0) > 0 and float(a.get("cost_cny") or 0) > 0]
        if len(per) >= 3:
            want_pf = round(_pct(per, 0.75), 3)
            old_pf = float(cur.get("cost_per_func_cny") or 0)
            if not old_pf or abs(want_pf - old_pf) / max(1e-6, old_pf) >= 0.2:
                out["cost_per_func_cny"] = want_pf
                out["changed"].append("cost_per_func_cny")
                out["basis"]["cost_per_func_cny"] = (
                    f"n={len(per)} p75=¥{want_pf}/函数（旧 {old_pf or '无'}）")
    return out


def current() -> dict:
    """当前生效的校准值（设置里的 calibration 覆盖内置默认）。"""
    out = {k: (v.copy() if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    try:
        from . import settings_store
        got = (settings_store.get_remote() or {}).get("calibration") or {}
    except Exception:                                    # noqa: BLE001
        got = {}
    if isinstance(got, dict):
        for k, v in got.items():
            if k == "reasoning_reserve" and isinstance(v, dict):
                out["reasoning_reserve"].update({str(a).lower(): int(b) for a, b in v.items()
                                                 if isinstance(b, (int, float))})
            elif k in out and v is not None:
                out[k] = v
    return out


def save(values: dict) -> dict:
    """把校准结果写回设置（只写白名单字段）。返回实际写进去的字典。"""
    cur = current()
    patch = {}
    for k in ("reasoning_reserve", "content_floor", "safety_cap",
              "module_budget_cny", "code_effort_ceiling", "auto", "min_samples"):
        if k in values and values[k] is not None:
            patch[k] = values[k]
    patch["source"] = "measured"
    patch["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    cur.update(patch)
    try:
        from . import settings_store
        settings_store.update_remote({"calibration": cur})
    except Exception:                                    # noqa: BLE001
        pass
    return cur


def recompute(ws: Path | None = None, days: int = 14, force: bool = False) -> dict:
    """按账本重算并写回。返回 {suggested, applied, changed, basis}。

    `auto=False` 时只算不写（除非 force=True），方便"先看数字再决定"。
    """
    cur = current()
    samples = int(cur.get("min_samples") or 20)
    sug = suggest(ws=ws, days=days, min_samples=samples)
    if not force and not bool(cur.get("auto", True)):
        return {"suggested": sug, "applied": cur, "changed": [], "basis": sug.get("basis"),
                "skipped": "auto 关闭（只计算不写入）"}
    applied = save(sug) if sug.get("changed") else cur
    return {"suggested": sug, "applied": applied, "changed": sug.get("changed") or [],
            "basis": sug.get("basis") or {}}
