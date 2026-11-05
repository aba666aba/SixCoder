"""思考强度路由：单模型下由系统按"这次干什么、有多难"决定想多深。

为什么有这个模块（2026-11-05 用户口径）：
    远端机制从"**多模型 × 多思考强度**"改成"**单模型 + 按需求自动切思考强度**"。
    于是"这次用哪一档强度"从"阶梯第几档"变成一条**可解释、可复现**的路由规则 ——
    不能写死"总是 none"（省了钱但做不对），也不能写死"总是 max"（想得深但烧钱）。

口径（全部确定性，同输入同输出）：

    kind="spec"/"plan"   → max   规格/方案是全链最高杠杆的一步，想得越深，后面越省
    kind="mechanical"    → none  样板/校验/日志/适配：想也没用，交给便宜路径或本地模型
    kind="code"          → 按难度分 score 选 none / low / high / max

难度分只吃确定性事实（依赖数/函数数/示例数/历史失败/是否桩/文件行数），
因为这些事实本来就在契约与状态里，不需要再猜。
"""
from __future__ import annotations

EFFORTS = ("none", "low", "high", "max")
_ORDER = {e: i for i, e in enumerate(EFFORTS)}
_ALIAS = {"off": "none", "minimal": "low", "medium": "high", "xhigh": "high",
          "disabled": "none", "": "none", "-": "none"}


def norm(value) -> str:
    """把任意写法收敛成 none/low/high/max（非法值当 none，绝不抛错）。"""
    key = str(value if value is not None else "").strip().lower()
    if key in EFFORTS:
        return key
    return _ALIAS.get(key, "none")


def difficulty(*, deps: int = 0, funcs: int = 0, examples: int = 0, fails: int = 0,
               stub: bool = False, lines: int = 0) -> int:
    """难度分（0–9，越大越难）。全部来自已有事实，可复现、可解释、可测。"""
    score = 0
    if deps >= 1:
        score += 1
    if deps >= 3:
        score += 1
    if funcs >= 4:
        score += 1
    if funcs >= 7:
        score += 1
    if examples >= 4:
        score += 1
    if fails >= 1:
        score += 1
    if fails >= 2:
        score += 1
    if stub:
        score += 1
    if lines >= 300:
        score += 1
    return score


def route(kind: str = "code", score: int = 0) -> str:
    """按任务种类 + 难度分给出该用的思考强度。"""
    k = str(kind or "code").strip().lower()
    if k in ("spec", "plan", "architect", "diagnose"):
        return "max"
    if k in ("mechanical", "bodies", "delegate", "format", "linefix"):
        return "none"
    s = max(0, int(score or 0))
    if s >= 5:
        return "max"
    if s >= 3:
        return "high"
    if s >= 1:
        return "low"
    return "none"


def pick(steps: list[dict], want: str) -> int:
    """在一段阶梯（同一个模型的不同强度）里挑下标。

    规则：**取不小于 want 的最低一档**；该模型没配这一档就往上找；到顶就用最高档。
    为什么不是"精确等于"：某个模型可能只开了 none/high（没钱买 max），
    want="max" 时用 high 也比退回 none 更接近意图。
    """
    if not steps:
        return 0
    w = norm(want)
    for i, s in enumerate(steps):
        if norm(s.get("effort")) == w:
            return i
    want_i = _ORDER[w]
    for i, s in enumerate(steps):
        if _ORDER[norm(s.get("effort"))] >= want_i:
            return i
    return len(steps) - 1


def explain(kind: str, score: int, want: str, steps: list[dict], idx: int) -> dict:
    """给事件流/报告用的一句话解释（"为什么这次想这么深"）。"""
    chosen = norm((steps[idx] if 0 <= idx < len(steps) else {}).get("effort"))
    return {"kind": kind, "difficulty": int(score or 0), "want": norm(want),
            "chosen": chosen, "index": int(idx),
            "why": ("规格/方案 → 最深" if norm(want) == "max" and kind in ("spec", "plan")
                    else "机械活 → 不思考" if norm(want) == "none"
                    else f"难度分 {int(score or 0)} → {norm(want)}")}
