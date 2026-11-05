"""把"一轮写多少"从硬编码的 16，改成**按验收带宽算出来的动态值**。

为什么这个数本来就不该是常数
    旧的 `contractgen.validate` 写死 `函数数 > 16` 就拒绝。但 16 不是物理上限，
    它是"一轮能**验收**多少"的经验值。而验收带宽随这些事实变化：

    1. **输出配额**：一轮的产出必须能一次性吐完，超了就截断（截断不是能力问题，是配额问题）。
       模型档案给了 `max_output_tokens` 就用它；没给（0 = 自动）就按档位估。
    2. **思考强度会吃掉输出配额**：reasoning token 算在 max_tokens 里，
       所以 `effort=max` 的档位**可写代码量更小**，不是更大。这条最容易搞反。
    3. **每个函数的代价**：示例越多、要抛的异常越多，实现与判据都越长。
    4. **依赖数**：依赖越多，上下文越大，越该保守。
    5. **近期一次通过率**：真实测出来的"没重试就全绿"的比例。低就缩小（fail-closed），
       高才敢放大——**先有证据再扩容**，不许拍脑袋把 16 改成 60。

口径：动态只是把"经验值"换成"可解释的经验值"，不是取消限制。
所以永远 clamp 在 [3, 40]，并且把依据一起写进拒绝信息里，方便追。
"""
from __future__ import annotations

MIN_FUNCS = 3
MAX_FUNCS = 40

# 档位 → 可用输出配额（max_output_tokens=0 即"自动"时用）。数值来自实测：
# 非思考档写一个 40 行函数 ≈ 500~700 token；思考档要额外留出 reasoning 的空间。
_EFFORT_CAPACITY = {"none": 6000, "low": 5000, "high": 4000, "max": 3000}


def capacity_tokens(step: dict | None) -> int:
    """这一档一次能用多少 token 写**正文**（不含思考的预留）。

    档案显式配了 max_output_tokens 就当正文配额；没配就按档位估。
    注意：真正发请求时 `remote` 会把 reasoning 的额度**加在这之上**
    （`REASONING_RESERVE`），否则思考会吃光正文配额 —— 这里算的是"能写多少代码"。
    """
    step = step or {}
    cap = int(step.get("max_output_tokens") or 0)
    if cap > 0:
        return cap
    return _EFFORT_CAPACITY.get(str(step.get("effort") or "none").lower(), 6000)


def per_func_tokens(examples: int = 1, raises: int = 0, lines_hint: int = 0) -> int:
    """一个函数大概要吃多少输出 token（实现 + JSON 包裹 + 判据）。"""
    cost = 150 + 40 * max(0, min(int(examples or 0), 4)) + 30 * max(0, min(int(raises or 0), 3))
    if lines_hint > 0:
        cost = max(cost, int(lines_hint) * 8)
    return max(80, cost)


def recent_first_pass(ws=None) -> float | None:
    """近期"一次通过率"（没有证据就返回 None，绝不编一个数出来）。"""
    if ws is None:
        return None
    try:
        from . import project as _project
        rec = _project.load_progress(ws) or {}
        vals = []
        for item in rec.values():
            if isinstance(item, dict) and item.get("first_pass_rate") is not None:
                vals.append(float(item["first_pass_rate"]))
        if not vals:
            return None
        return round(sum(vals[-20:]) / len(vals[-20:]), 3)
    except Exception:                                    # noqa: BLE001
        return None


def suggest_max_funcs(*, capacity: int = 6000, per_func: int = 0, examples: int = 1,
                      raises: int = 0, deps: int = 0, first_pass: float | None = None,
                      hard_cap: int = MAX_FUNCS) -> tuple[int, list[str]]:
    """算这一轮建议最多几个函数，并给出依据（人话，能进报告）。"""
    per = per_func or per_func_tokens(examples, raises)
    reasons = [f"输出配额 {capacity} token ÷ 每函数约 {per} token"]
    n = max(1, capacity // max(1, per))
    if deps:
        shrink = min(0.4, 0.08 * min(int(deps), 5))
        n = int(n * (1.0 - shrink))
        reasons.append(f"依赖 {deps} 个 → 上下文变大，缩小 {int(shrink * 100)}%")
    if first_pass is not None:
        if first_pass < 0.6:
            n = int(n * 0.6)
            reasons.append(f"近期一次通过率 {first_pass:.0%} 偏低 → 缩小到 60%")
        elif first_pass >= 0.85:
            n = int(n * 1.25)
            reasons.append(f"近期一次通过率 {first_pass:.0%} 高 → 放大到 125%")
        else:
            reasons.append(f"近期一次通过率 {first_pass:.0%} 中等 → 不调整")
    else:
        reasons.append("还没有一次通过率的实测数据 → 按配额保守取")
    n = max(MIN_FUNCS, min(int(hard_cap), MAX_FUNCS, n))
    reasons.append(f"最终夹到 [{MIN_FUNCS}, {min(hard_cap, MAX_FUNCS)}] 区间 → {n}")
    return n, reasons


def context_for(ws=None, step: dict | None = None, deps: int = 0) -> dict:
    """给契约合成用的一组预算参数（公司层组装，contractgen 只管用）。"""
    return {"capacity": capacity_tokens(step), "deps": int(deps or 0),
            "first_pass": recent_first_pass(ws)}


def per_module_cap(total: int, modules: int) -> int:
    """多模块时每个模块分到几个（单模块就是全部）。"""
    m = max(1, int(modules or 1))
    return max(4, min(16, -(-int(total) // m)))
