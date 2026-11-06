"""前沿选择：下一步做哪条能力 —— 确定性打分，不花一次远端调用。

为什么调度权必须从模型手里拿走：
    16 轮基线里，"下一轮做什么"是我手写 16 个需求文件决定的。一旦交给远端自由发挥，
    它会按"我讲到哪了"续写，而不是按"还差什么"推进；而"还差什么"这件事，
    模型没有可靠的自评能力（它连自己漏了什么都看不出来，见 blueprint.AUDIT_PROMPT 那段说明）。
    打分是确定性的：同样的蓝图 + 同样的覆盖状态 → 同样的下一刀。可以被自检钉住。

打分（分数越大越先做）：
    score = 3×解锁数 + 2×是否端到端 − 1×相对花费
  · 解锁数：做完它之后，能让多少条待办能力变成"依赖已满足"（先修地基）
  · 端到端：命令行类观测（用户真能跑）优先于 py_call（零件级）—— 竖切优先
            （横切会造出"全都写了一半、一个都跑不起来"的项目，那是自主运行最怕的形态：
              第 8 轮之前没有任何东西能被观测到，等于前面全在盲写）
  · 花费：同分先做便宜的（先拿反馈）
同分再按 id 排序，保证完全确定。
"""
from __future__ import annotations

UNLOCK_W = 3.0
E2E_W = 2.0
COST_W = 1.0


def _done(bp, done) -> set[str]:
    return set(done) if done is not None else bp.done()


def unlocks(bp, cid: str, done=None) -> int:
    """做完 cid 之后，能新解锁多少条（依赖因此全部满足、且自己还没 done）。"""
    have = _done(bp, done)
    n = 0
    for c in bp.caps:
        if c.status in ("done", "descoped") or c.id in have:
            continue
        if cid not in c.deps:
            continue
        if all(d in have or d == cid for d in c.deps):
            n += 1
    return n


def score(bp, cap, done=None) -> float:
    have = _done(bp, done)
    total = sum((c.budget_cny or 1.0) for c in bp.caps) or 1.0
    cost = (cap.budget_cny or 1.0) / total
    return (UNLOCK_W * unlocks(bp, cap.id, have)
            + E2E_W * (1.0 if cap.end_to_end else 0.0)
            - COST_W * cost)


def prereq_ok(bp, cap, done=None) -> bool:
    have = _done(bp, done)
    return all(d in have for d in cap.deps)


def eligible(bp, done=None) -> list:
    """当前就能做、且做了有意义的能力。"""
    have = _done(bp, done)
    out = []
    for c in bp.caps:
        if c.status in ("done", "descoped") or c.id in have:
            continue
        if prereq_ok(bp, c, have):
            out.append(c)
    return out


def rank(bp, done=None) -> list[tuple[float, object]]:
    """[(score, cap)]，分数从高到低；同分按花费低的、再按 id（完全确定）。"""
    have = _done(bp, done)
    rows = [(score(bp, c, have), c) for c in eligible(bp, have)]
    rows.sort(key=lambda t: (-t[0], t[1].budget_cny, t[1].id))
    return rows


def pick(bp, done=None):
    rows = rank(bp, done)
    return rows[0][1] if rows else None


def blocked_caps(bp, done=None) -> list[tuple[object, str]]:
    """永远轮不到的能力：依赖被降级/不存在 → 必须报出来，不许静默挂着。

    这是 autonomy 最容易撒谎的位置：一条能力因为别人被降级而永远做不了，
    如果报告里只写"未完成"，人就会以为是没时间，而不是"链条断了"。
    """
    have = _done(bp, done)
    out = []
    for c in bp.caps:
        if c.status in ("done", "descoped") or c.id in have:
            continue
        lost = []
        unknown = []
        for d in c.deps:
            dep = bp.by_id(d)
            if dep is None:
                unknown.append(d)
            elif dep.status == "descoped":
                lost.append(d)
        if unknown:
            out.append((c, "依赖不存在：" + ", ".join(unknown)))
        elif lost:
            out.append((c, "依赖被降级：" + ", ".join(lost)))
    return out


def why(bp, cap, done=None) -> str:
    """给人看的一句"为什么先做它"（报告/事件用）。"""
    have = _done(bp, done)
    return (f"解锁 {unlocks(bp, cap.id, have)} 条"
            + ("，端到端可观测" if cap.end_to_end else "，零件级")
            + f"，估 ¥{cap.budget_cny:.3f}")
