"""蓝图：一句话目标 → 一张**可机判**的能力表（DAG）。

为什么需要这一层（实测，16 轮基线）：
    内循环 `company._night_inner` 的 `for rnd in range(1, max_rounds + 1)` 只在
    **一份** contracts 上反复修复；"换一份契约再跑一轮"在产品里没有入口，
    只能靠人写 16 个需求文件、在外层脚本里循环驱动。
    于是天花板不在写代码（20/20 是常事），而在**谁决定下一个切片**。

五条设计约束，每条都对应一种已经见过的失败：
1. 能力必须是**可观测行为**，不是模块或文件。"core.py 写完"无法判定；
   "跑这条命令打印出 3" 能判定。只判退出码的探针一律拒收 —— 空壳 `print(0)`
   也能过（这条由 core/probe.py 执行）。
2. 审计必须**独立上下文**：derive 和 audit 是两次调用。同一个上下文里自问自答，
   实测对"漏了什么"几乎无效 —— 它会证明自己没错。
3. 调度不交给模型：下一步做什么由 core/frontier.py 确定性打分，不花一次远端调用。
4. 降级必须留痕：descope 写进蓝图并且进 diff 日志，绝不静默缺失。
5. 预算换范围：每条能力带花费估算；总预算不够时**主动降级并报告**，而不是跑到钱光。

跑法（人审那一页）: python -c "from core import blueprint; print(blueprint.summary(...))"
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

BLUEPRINT_FILE = ".sixcoder/blueprint.json"
DIFF_FILE = ".sixcoder/blueprint.diff.jsonl"
AUDIT_CACHE_FILE = ".sixcoder/audit_cache.json"
# 改 AUDIT_PROMPT / AUDIT_DIFF_PROMPT 时递增，旧结论自动失效
AUDIT_CACHE_VERSION = "1"
MAX_CAPS = 200                    # 超过说明粒度太细，切片会碎成沙子
# 两级蓝图（开发文档 §18.7 第 1 件）：能力数超过 SUBSYS_TRIGGER_CAPS 时按模块域分组，
# 每组 ≤ SUBSYS_MAX_CAPS 条，切片不跨子系统（接口先冻结）；分组后蓝图能装到
# MAX_CAPS_WITH_SUBSYS（≈ 2 万函数 / 50 万行，L 档下沿）。
SUBSYS_TRIGGER_CAPS = 60          # 能力数超过它 → 自动 split_subsystems
SUBSYS_MAX_CAPS = 40              # 单组能力上限（一张能力表仍可管理、可审计）
MAX_CAPS_WITH_SUBSYS = 2000       # 分组后蓝图的硬上限
PROBE_KINDS = ("cli", "py_call", "pytest", "http")  # 已有执行器的种类（见 core/probe.py）
CAP_ID_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)*$")
DEFAULT_FUNCS = 4                 # 没给估算时按 4 个函数算（宁可保守）
# 每函数花费：两个实测形态反推出来的（302 行/20 函数/¥0.33；200 行/8 函数/¥0.21），
# 落在 ¥0.016~0.026/函数。取上限：预算宁可紧一点，让人看见要砍范围，而不是最后超支。
CNY_PER_FUNC = 0.025
STATUSES = ("absent", "blocked", "partial", "done", "descoped")


# ---------------------------------------------------------------- 数据结构

@dataclass
class Cap:
    """一条能力 = 一个可观测行为 + 观测它的方式。"""
    id: str
    desc: str = ""
    probe: dict = field(default_factory=dict)
    deps: list[str] = field(default_factory=list)
    funcs_est: int = 0
    modules: list[str] = field(default_factory=list)
    budget_cny: float = 0.0
    status: str = "absent"
    why: str = ""
    slice_id: str = ""        # 是哪个切片把它做出来的（溯源：能力 → 片 → 契约 → 文件）
    attempts: int = 0          # 被排进切片几次（1 = 一次通过；>1 = 返工）

    @property
    def funcs(self) -> int:
        return max(1, int(self.funcs_est or DEFAULT_FUNCS))

    @property
    def end_to_end(self) -> bool:
        """端到端 = 用户真能跑起来（命令行）。零件级（py_call）不算。"""
        return str((self.probe or {}).get("kind") or "") == "cli"

    def to_dict(self) -> dict:
        return {"id": self.id, "desc": self.desc, "probe": dict(self.probe or {}),
                "deps": list(self.deps), "funcs_est": self.funcs_est,
                "modules": list(self.modules), "budget_cny": round(self.budget_cny, 4),
                "status": self.status, "why": self.why, "slice_id": self.slice_id,
                "attempts": int(self.attempts or 0)}

    @classmethod
    def from_dict(cls, raw: dict) -> "Cap":
        p = raw.get("probe")
        return cls(
            id=str(raw.get("id") or "").strip(),
            desc=str(raw.get("desc") or "").strip(),
            probe=dict(p) if isinstance(p, dict) else {},
            deps=[str(x).strip() for x in (raw.get("deps") or []) if str(x).strip()],
            funcs_est=int(raw.get("funcs_est") or 0),
            modules=[str(x).strip() for x in (raw.get("modules") or []) if str(x).strip()],
            budget_cny=float(raw.get("budget_cny") or 0.0),
            status=str(raw.get("status") or "absent"),
            why=str(raw.get("why") or ""),
            slice_id=str(raw.get("slice_id") or ""),
            attempts=int(raw.get("attempts") or 0),
        )


@dataclass
class Subsystem:
    """一个子系统 = 一组能力（按落点模块域聚合）+ 对其它子系统的依赖。

    "两级蓝图"的第二级是能力 DAG（就是 bp.caps 那张表）；第一级是子系统 DAG
    （本类）。切片不跨子系统 → 每片只碰一个子系统的文件面，等价于"接口先冻结"：
    写 A 子系统时，B 子系统的文件不会出现在同一片里被顺手改坏。
    """
    name: str
    cap_ids: list[str] = field(default_factory=list)
    deps: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)   # 冻结的对外接口（能力 id）

    def to_dict(self) -> dict:
        return {"name": self.name, "cap_ids": list(self.cap_ids),
                "deps": list(self.deps), "frozen": list(self.frozen)}

    @classmethod
    def from_dict(cls, raw: dict) -> "Subsystem":
        return cls(name=str(raw.get("name") or ""),
                   cap_ids=[str(x) for x in (raw.get("cap_ids") or [])],
                   deps=[str(x) for x in (raw.get("deps") or [])],
                   frozen=[str(x) for x in (raw.get("frozen") or [])])


@dataclass
class Blueprint:
    goal: str = ""
    version: int = 1
    caps: list[Cap] = field(default_factory=list)
    subsystems: list[Subsystem] = field(default_factory=list)
    total_budget_cny: float = 0.0
    created_at: float = 0.0
    notes: list[str] = field(default_factory=list)

    # --- 查询 ---
    def by_id(self, cid: str) -> Cap | None:
        for c in self.caps:
            if c.id == cid:
                return c
        return None

    @staticmethod
    def clip(text: str, limit: int) -> str:
        """超长文本塞进提示词时**保留头尾、砍中间**，不丢结尾。

        长需求（本次 6954 字符）若用 `text[:6000]`，尾部的验收口径与风险应对
        会被整段砍掉 —— 而那些恰恰是判定"做没做完"的依据。
        头部放目标/规模/核心指标，尾部放验收与风险，中间是展开描述，砍中间最安全。
        """
        s = str(text or "")
        if len(s) <= limit:
            return s
        head = int(limit * 0.62)
        tail = limit - head - 60
        return (s[:head] + f"\n\n…（中间省略 {len(s) - head - tail} 字符，控制远端 token）…\n\n"
                + s[-tail:])

    def ids(self) -> list[str]:
        return [c.id for c in self.caps]

    def done(self) -> set[str]:
        return {c.id for c in self.caps if c.status == "done"}

    def pending(self) -> list[Cap]:
        return [c for c in self.caps if c.status not in ("done", "descoped")]

    def subsystem_of(self, cid: str) -> str:
        for s in self.subsystems:
            if cid in s.cap_ids:
                return s.name
        return ""

    # --- 变换（都返回是否真的改动了，便于留痕）---
    def add(self, cap: Cap) -> bool:
        # 分组后蓝图的容量上限抬高：两级蓝图正是为了破"扁平 200 条"这堵墙。
        limit = MAX_CAPS_WITH_SUBSYS if self.subsystems else MAX_CAPS
        if not cap.id or self.by_id(cap.id) or len(self.caps) >= limit:
            return False
        self.caps.append(cap)
        return True

    def drop(self, cid: str) -> bool:
        before = len(self.caps)
        self.caps = [c for c in self.caps if c.id != cid]
        return len(self.caps) != before

    def rewire(self, cid: str, deps: list[str]) -> bool:
        c = self.by_id(cid)
        if not c:
            return False
        new = [d for d in deps if d and d != cid]
        if new == c.deps:
            return False
        c.deps = new
        return True

    # --- 存取 ---
    def to_dict(self) -> dict:
        return {"goal": self.goal, "version": self.version,
                "caps": [c.to_dict() for c in self.caps],
                "subsystems": [s.to_dict() for s in self.subsystems],
                "total_budget_cny": round(self.total_budget_cny, 4),
                "created_at": self.created_at, "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, raw: dict) -> "Blueprint":
        return cls(goal=str(raw.get("goal") or ""),
                   version=int(raw.get("version") or 1),
                   caps=[Cap.from_dict(c) for c in (raw.get("caps") or [])
                         if isinstance(c, dict)],
                   subsystems=[Subsystem.from_dict(s) for s in (raw.get("subsystems") or [])
                               if isinstance(s, dict)],
                   total_budget_cny=float(raw.get("total_budget_cny") or 0.0),
                   created_at=float(raw.get("created_at") or 0.0),
                   notes=[str(x) for x in (raw.get("notes") or [])])


# ---------------------------------------------------------------- 校验

_ERROR_WORDS = ("报错", "错误", "失败", "异常", "不存在", "缺列", "缺文件", "非 0", "非0",
                "退出码", "拒绝", "raise", "error")


def cap_issues(c: Cap) -> list[str]:
    """单条能力能不能被判：每条都是"跑起来会不会白干"的分界线。"""
    out: list[str] = []
    tag = c.id or "(没 id)"
    if not c.id:
        out.append("能力缺 id")
    elif not CAP_ID_RE.match(c.id):
        out.append(f"{tag}: id 必须是小写点分（如 cap.csv.count）")
    if not c.desc.strip():
        out.append(f"{tag}: 缺 desc（写清输入什么、观测到什么）")
    from . import probe as _probe
    for msg in _probe.validate(c.probe):
        out.append(f"{tag}: {msg}")
    for d in c.deps:
        if d == c.id:
            out.append(f"{tag}: 依赖了自己")
    return out


def cycle_caps(bp: Blueprint) -> list[list[str]]:
    """依赖成环的能力（成环 = 永远没有任何一条能被先做）。"""
    graph = {c.id: [d for d in c.deps if bp.by_id(d)] for c in bp.caps}
    WHITE, GREY, BLACK = 0, 1, 2
    color = {k: WHITE for k in graph}
    found: list[list[str]] = []
    stack: list[str] = []

    def visit(n: str) -> None:
        color[n] = GREY
        stack.append(n)
        for d in graph.get(n, []):
            if color.get(d) == GREY:
                i = stack.index(d)
                found.append(stack[i:] + [d])
            elif color.get(d) == WHITE:
                visit(d)
        stack.pop()
        color[n] = BLACK

    for n in list(graph):
        if color[n] == WHITE:
            visit(n)
    return found


def topo(bp: Blueprint) -> tuple[list[str], list[str]]:
    """拓扑序（依赖在前）+ 指向不存在依赖的引用。确定：同输入永远同输出。"""
    have = set(bp.ids())
    order: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()

    def visit(cid: str, path: set[str]) -> None:
        if cid in seen or cid in path:
            return
        c = bp.by_id(cid)
        if not c:
            return
        path.add(cid)
        for d in c.deps:
            if d not in have:
                ref = f"{cid} → {d}"
                if ref not in missing:
                    missing.append(ref)
                continue
            visit(d, path)
        path.discard(cid)
        if cid not in seen:
            seen.add(cid)
            order.append(cid)

    for c in bp.caps:
        visit(c.id, set())
    return order, missing


def validate(bp: Blueprint) -> list[str]:
    """结构校验：能不能开跑。任何一条都意味着"跑起来也是白跑"。"""
    issues: list[str] = []
    if not bp.caps:
        issues.append("蓝图里一条能力都没有（等于没有验收对象）")
        return issues
    _cap_limit = MAX_CAPS_WITH_SUBSYS if bp.subsystems else MAX_CAPS
    if len(bp.caps) > _cap_limit:
        issues.append(f"能力数 {len(bp.caps)} 超过 {_cap_limit}：粒度太细，先合并")
    issues += subsystem_issues(bp)
    seen: set[str] = set()
    for c in bp.caps:
        if c.id in seen:
            issues.append(f"{c.id}: id 重复")
        seen.add(c.id)
        issues += cap_issues(c)
        if c.status not in STATUSES:
            issues.append(f"{c.id}: 未知状态 {c.status}")
    _, missing = topo(bp)
    for ref in missing:
        issues.append(f"依赖不存在：{ref}")
    for ring in cycle_caps(bp):
        issues.append("依赖成环：" + " → ".join(ring))
    if bp.total_budget_cny > 0:
        est = estimate_cost(bp)
        if est > bp.total_budget_cny * 1.0001:
            issues.append(f"估算花费 ¥{est:.2f} 超过总预算 ¥{bp.total_budget_cny:.2f}："
                          "先砍范围（降级是记录在案的决策，不是静默缺失）")
    return issues


# ---------------------------------------------------------------- 两级蓝图（L1 子系统层）
def _cap_domain(cap: Cap) -> str:
    """一条能力属于哪个"模块域"（决定它进哪个子系统）。

    口径：落点模块去掉 .py / __init__ 后的**最后一段**（文件名 stem）。
    `app/auth.py` → `auth`、`app/tickets.py` → `tickets`、`auth.py` → `auth`。
    这是"接口冻结"的最细粒度：一片只碰一个文件域，跨文件的顺带改坏从结构上不可能。
    注：目录级聚合（把 app/auth/ 下多个文件归成一个 auth 子系统）是后续细化，
    文件名粒度已能给出正确的 L1 分组与切片边界，且完全确定性。
    """
    m = (cap.modules or [""])[0] or ""
    m = m.replace("\\", "/")
    if m.endswith(".py"):
        m = m[:-3]
    seg = [p for p in m.split("/") if p and p != "__init__"]
    if seg:
        return seg[-1].lower()
    parts = (cap.id or "").split(".")
    if len(parts) >= 2 and parts[1]:
        return parts[1].lower()
    return "misc"


def _subsystem_cycle(subs: list[Subsystem]) -> list[str] | None:
    """子系统依赖环（返回环上的名字列表；无环返回 None）。"""
    graph = {s.name: [d for d in s.deps if any(o.name == d for o in subs)]
             for s in subs}
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[str, int] = {n: WHITE for n in graph}
    path: list[str] = []

    def visit(n: str):
        color[n] = GREY
        path.append(n)
        for d in graph.get(n, []):
            if color.get(d) == GREY:
                return list(path[path.index(d):])
            if color.get(d) == WHITE:
                r = visit(d)
                if r:
                    return r
        path.pop()
        color[n] = BLACK
        return None

    for n in sorted(graph):
        if color[n] == WHITE:
            r = visit(n)
            if r:
                return r
    return None


def split_subsystems(bp: Blueprint, *, max_caps: int = SUBSYS_MAX_CAPS) -> list[Subsystem]:
    """能力表 → 子系统 DAG（确定性，同输入同输出）。返回分组并写回 bp.subsystems。

    分组：按 _cap_domain 聚合；超 max_caps 的域按序切成 max_caps 一块。
    组间依赖：能力的 deps 落在别的组 → 该组依赖那个组。
    环消解：组间依赖成环（A→B→A）时，把环内组合并成一个（保留字典序最小的名字），
    直到无环 —— 子系统必须是 DAG，否则"先冻结接口"无从谈起。
    """
    groups: dict[str, list[str]] = {}
    order: list[str] = []
    for c in bp.caps:
        d = _cap_domain(c)
        if d not in groups:
            groups[d] = []
            order.append(d)
        groups[d].append(c.id)
    subs: list[Subsystem] = []
    for d in order:
        ids = groups[d]
        if len(ids) <= max_caps:
            subs.append(Subsystem(name=d, cap_ids=list(ids)))
        else:
            for i in range(0, len(ids), max_caps):
                subs.append(Subsystem(name=f"{d}.{i // max_caps + 1}",
                                      cap_ids=list(ids[i:i + max_caps])))
    cap_to_sub = {cid: s.name for s in subs for cid in s.cap_ids}
    for s in subs:
        deps: list[str] = []
        for cid in s.cap_ids:
            c = bp.by_id(cid)
            for d in (c.deps if c else []):
                owner = cap_to_sub.get(d)
                if owner and owner != s.name and owner not in deps:
                    deps.append(owner)
        s.deps = deps
    # 环消解：合并环内组（确定性）
    while True:
        cycle = _subsystem_cycle(subs)
        if not cycle:
            break
        keep = min(cycle)
        merged_caps: list[str] = []
        merged_deps: list[str] = []
        for s in subs:
            if s.name in cycle:
                for cid in s.cap_ids:
                    if cid not in merged_caps:
                        merged_caps.append(cid)
                for d in s.deps:
                    if d not in cycle and d not in merged_deps:
                        merged_deps.append(d)
        subs = [s for s in subs if s.name not in cycle]
        subs.append(Subsystem(name=keep, cap_ids=merged_caps, deps=merged_deps))
        for s in subs:
            if s.name == keep:
                continue
            s.deps = [keep if d in cycle else d for d in s.deps]
    bp.subsystems = subs
    return subs


def subsystem_issues(bp: Blueprint) -> list[str]:
    """子系统结构校验：归属唯一、组间依赖有效、无环、容量不越界。"""
    out: list[str] = []
    if not bp.subsystems:
        return out
    cap_to_sub: dict[str, str] = {}
    names: set[str] = set()
    for s in bp.subsystems:
        if not s.name:
            out.append("子系统缺名")
        if s.name in names:
            out.append(f"子系统重名：{s.name}")
        names.add(s.name)
        if len(s.cap_ids) > SUBSYS_MAX_CAPS:
            out.append(f"{s.name}: 组内能力 {len(s.cap_ids)} 超过 {SUBSYS_MAX_CAPS}")
        for cid in s.cap_ids:
            if cid in cap_to_sub:
                out.append(f"{cid}: 能力归属到多个子系统")
            cap_to_sub[cid] = s.name
        for d in s.deps:
            if not any(o.name == d for o in bp.subsystems):
                out.append(f"{s.name}: 依赖不存在的子系统 {d}")
    for c in bp.caps:
        if c.id not in cap_to_sub:
            out.append(f"{c.id}: 没有子系统归属")
    if _subsystem_cycle(bp.subsystems):
        out.append("子系统依赖成环")
    return out


def fatal(issues: list[str]) -> list[str]:
    """哪些问题属于"不许开跑"（降级/预算类只警告，不算致命）。"""
    return [i for i in issues
            if not i.startswith("估算花费") and "掉队的" not in i]


# ---------------------------------------------------------------- 预算

def estimate_cost(bp: Blueprint) -> float:
    """剩下的活值多少钱（已降级的不算：它们不会被做，也就不该占预算）。"""
    return round(sum(c.budget_cny or c.funcs * CNY_PER_FUNC
                     for c in bp.caps if c.status != "descoped"), 4)


def fill_budget(bp: Blueprint) -> None:
    """没估过的按函数数折价补上（校准值优先，没有就用实测均值）。"""
    per = CNY_PER_FUNC
    try:
        from . import calibration as _cal
        v = float((_cal.current() or {}).get("cost_per_func_cny") or 0.0)
        if v > 0:
            per = v
    except Exception:                                    # noqa: BLE001
        pass
    for c in bp.caps:
        if not c.budget_cny:
            c.budget_cny = round(c.funcs * per, 4)


def within_budget(bp: Blueprint) -> tuple[bool, str]:
    if not bp.total_budget_cny:
        return True, "没设总预算"
    est = estimate_cost(bp)
    left = bp.total_budget_cny - est
    return left >= 0, f"估算 ¥{est:.2f} / 预算 ¥{bp.total_budget_cny:.2f}（余 ¥{left:.2f}）"


def _is_test_cap(c: Cap) -> bool:
    """测试类能力：降级时**最后才动**。

    用户的硬要求里有"健壮性"，而 fit_budget 按"最不关键"排序时，测试能力往往解锁少、
    不是端到端 —— 于是钱一紧就先被砍（实测 run 4：超估算 ¥0.05 就把测试能力降级了）。
    这是价值判断，不是算出来的，所以显式写在这里并留依据。
    """
    probe_kind = str((c.probe or {}).get("kind") or "")
    text = (c.id + " " + (c.desc or "")).lower()
    return probe_kind == "pytest" or "test" in text or "测试" in (c.desc or "")


def fit_budget(bp: Blueprint, *, tolerance: float = 0.10) -> list[str]:
    """估算超预算时**主动砍范围**，从最不关键的能力开始（返回被砍的 id）。

    为什么必须真的动手（实测：估 ¥0.62 / 预算 ¥0.60 时只警告不动手）：
    "超预算"如果只写在备注里，跑到一半钱就没了，剩下的能力全变缺口 ——
    而缺口的代价（人工补）远大于少做一个锦上添花的能力。宁可少做，不可超支。
    容忍带 10%：估算本身是粗的（±30%），为几分钱砍掉能力是自欺。
    """
    if not bp.total_budget_cny:
        return []
    limit = bp.total_budget_cny * (1.0 + max(0.0, tolerance))
    out: list[str] = []
    from . import frontier as _front
    guard = 0
    while estimate_cost(bp) > limit and guard < MAX_CAPS:
        guard += 1
        cands = [c for c in bp.caps if c.status not in ("done", "descoped")]
        needed = {d for c in cands for d in c.deps}      # 被依赖的不许砍，否则下游一起死
        pool = [c for c in cands if c.id not in needed]
        if not pool:
            out.append("")                               # 全是被依赖的，砍不动了
            break
        # 先砍非测试类里最不关键的；测试类留到最后（健壮性是硬要求）
        worst = min(pool, key=lambda c: (_is_test_cap(c), _front.score(bp, c), c.id))
        est = estimate_cost(bp)
        if not descope(bp, worst.id,
                       f"预算不足（估算 ¥{est:.2f} 超 ¥{bp.total_budget_cny:.2f} 的容忍带）"
                       f"→ 主动砍掉最不关键的一条（解锁少/非端到端/最贵；测试能力最后才砍）。"
                       "要保住它就把总预算调高，或把它写成更小的能力"):
            break
        out.append(worst.id)
        bp.notes.append(f"预算收敛：降级 {worst.id}（估算 ¥{est:.2f} → ¥{estimate_cost(bp):.2f}）")
    return out


# ---------------------------------------------------------------- 远端两跳

DERIVE_PROMPT = """你是软件架构师。把下面这一句**目标**变成一张"能力表"，只输出 JSON。

【什么叫一条能力】
一条能力 = **用户或调用方能观测到的一个行为**。判定标准：做完它之后，立刻能跑一条命令
或调用一次函数，看到确定结果。
  ✓ "给出一个 CSV，统计出记录条数并打印"
  ✓ "add(2, 3) 返回 5"
  ✗ "实现 models.py"（这是文件，不是能力）
  ✗ "搭建项目骨架"（观测不到）
  ✗ "代码结构清晰"（不可判定）

【每条能力必须带】
- id：小写点分，如 `cap.csv.count`，全表唯一
- desc：一句话，写清"输入什么 → 观测到什么"
- probe：怎么观测，四选一
    {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
     "files": {"d.csv": "name,age\\n山田,20\\n佐藤,30\\n"}, "stdout_has": "2"}
    {"kind": "py_call", "module": "calc", "func": "add", "args": [2, 3], "expect": 5}
    {"kind": "pytest", "args": ["tests/test_main.py"],
     "stdout_has": ["test_happy", "test_error"]}
    {"kind": "http", "app": "app.main:app", "steps": [
      {"method": "POST", "path": "/auth/register",
       "json": {"username": "u1", "password": "p1"},
       "expect_status": 201, "expect_json": {"username": "u1"}},
      {"method": "POST", "path": "/auth/login",
       "json": {"username": "u1", "password": "p1"},
       "save": {"token": "token"}, "expect_json": {"username": "u1"}},
      {"method": "GET", "path": "/tickets",
       "headers": {"Authorization": "Bearer ${token}"},
       "expect_status": 200, "expect_json": {}}]}
  · **不许只判退出码**：至少有 stdout_has 或 expect 或 expect_json，否则空壳 `print(0)` 也能过
  · cli 的 **entry 必须是仓库里的脚本文件**（如 main.py）。写 `python` / `pytest` 这类
    解释器命令当 entry，这条能力永远无法被观测（会被当成仓库里一个叫 python 的文件）
  · pytest 必须写明要看到哪些**用例名**：只判"有没有 passed"的话，一个
    `def test_noop(): assert True` 的空壳测试也能过
  · cli 的 `files` 是探针运行时放进临时目录的素材（相对路径，不是真仓库里的文件）
  · 期望抛异常用 `"expect_raises": "ValueError"`
  · **错误路径必须写 `expect_exit`**：不写就默认要求退出码 0 —— 而"文件不存在要报错退出"
    本来就该是非 0，自相矛盾，判据永远过不了。例如：
    {"kind": "cli", "entry": "main.py", "args": ["--file", "nope.csv"],
     "expect_exit": 1, "stdout_has": "文件不存在"}
- deps：要调用它的产物才能观测的，才写依赖（id 列表）
- funcs_est：这条能力大约要写几个函数（整数，别虚报）
- modules：大致落在哪些文件（可以空）

【Web / HTTP 项目（看到就照做，这条优先级最高）】
目标里只要出现 Web、网站、页面、前端、HTTP、REST、API、接口、服务、uvicorn、FastAPI、
Flask、Django、登录页、看板 等字样，就**一律用 http 探针，不许用 cli 探针**
（"python main.py register" 对 Web 服务毫无意义，会把整个项目做歪）。规则：
1. `app` 全表固定写同一个：`app.main:app`（产物必须在这个位置暴露名为 `app` 的 ASGI 应用）。
2. 一条能力 = 一条**自包含**的请求链。需要登录态的，就在同一条 steps 里先 register 再 login，
   用 `"save": {"token": "token"}` 配 `${token}` 传递；**不许依赖别的能力留下的数据**
   （每条能力在独立进程里跑，进程之间不共享内存，也不保证顺序）。
3. 判据必须确定：`expect_status` + `expect_json`（值要写死，不许写 "any"/"*"）。
   列表型响应判不了全量时，改判总数/某一条的具体字段，或换成一条按 id 查询的能力。
4. 错误路径要单列能力：未授权 401、参数非法 422、资源不存在 404。
5. 前端：用 `{"kind":"http","app":"app.main:app","method":"GET","path":"/",
   "body_has":["<title 里的字>","登录"]}` 判页面真的挂了什么。
6. modules 要写清分层（如 app/main.py、app/auth.py、app/models.py、app/schemas.py），
   一条能力 3~8 个函数，别把全部路由塞进 main.py。

【硬要求】
1. 每条都必须能**独立观测**：不许出现"写完 A 才能观测 B，但 A 本身观测不到"的链条。
2. 必须包含**端到端**能力：至少一条从入口（命令行/主流程/HTTP 请求链）走完，不是只有零件。
3. 必须包含**非功能类**（按目标判断，缺就补）：数据持久化、错误输入的处理、测试。
4. 粒度：一条能力 ≈ 一次能做完并验收的量（3~8 个函数）。太细会碎，太粗会堵。
5. 依赖不许成环。
6. **逻辑不要都塞进入口脚本**：解析、计算、输出各自落到独立模块（如 csvio.py、agg.py），
   入口脚本只放 CLI 接线。模块超过 6 个函数就再拆 —— 一个文件塞十几个函数时，
   任何一个判据红了都要整文件重写，实测一轮就烧掉整个预算。

【预算】总预算约 ¥@BUDGET@，按 ¥@PER_FUNC@/函数估，大约能承载 @N_FUNCS@ 个函数。
超了就**砍范围**（把锦上添花的能力放进 dropped 并写明原因），不要悄悄超出。

【目标】
@GOAL@

只输出这个 JSON：
{"caps": [{"id": "...", "desc": "...", "probe": {...}, "deps": [], "funcs_est": 4,
            "modules": []}],
  "dropped": [{"what": "...", "why": "预算不够 / 目标没要求"}]}
"""

AUDIT_PROMPT = """你是独立评审，不是这张表的作者。下面是**目标**和拟定好的**能力表**。

你只有一个任务：找出让这个项目"看起来做完了、其实不可用"的问题。
不要评价风格，不要表扬，不要提改进建议以外的意见。只报你能指到具体 id 的问题。

四类问题（只准报这四类）：
1. missing：目标隐含、验收时一定会被试到，但表里没有
2. dup：两条能力观测的是同一个行为（会重复实现，白花预算）
3. broken_dep：依赖不存在，或 B 依赖 A 但 A 自己观测不到
4. fake_probe：探针用空壳实现也能通过（只判退出码 / 只判不抛异常 / 期望值恒真）

判"漏"的方法（重要）：不要问自己"还有没有漏的"——那几乎总会得到"没有"。
改成：**我是验收方，我会一条一条怎么试它**。把你会试的动作列出来，再去表里找对应能力。
找不到的，才算 missing。

判"依赖"的方法：deps 只写**真的**依赖 —— 也就是"不先把它做出来，这条命令就必然跑不通"。
凡是各自独立的命令（比如 `--file 不存在` 该报错、`--top 2` 该只打两行）**不要串成一条链**：
一条红会把整条链锁死，实测一次就废掉 7 条能力的观测。宁可 deps 空着。

顺带检查：有没有一条能力"观测的是另一条能力的总和"（e2e 大杂烩）？如果有，
把它拆成各自独立的命令，或者在 rewire 里把不该有的依赖去掉。

注意：`{"kind":"http", ...}` 是合法且推荐的 Web 观测方式（真发请求、比状态码与 JSON 字段）。
只要它写了 expect_status 或 expect_json / body_has，就**不是** fake_probe；
反过来，Web 项目里出现 `python main.py xxx` 这类 cli 探针，应当报 fake_probe 或 missing。

输出 JSON：
{"issues": [{"kind": "missing|dup|broken_dep|fake_probe", "cap": "涉及的 id",
              "detail": "具体到能直接改的程度"}],
  "add": [{"id": "...", "desc": "...", "probe": {...}, "deps": [], "funcs_est": 3}],
  "drop": ["id"],
  "rewire": {"id": {"deps": ["..."]}}}
没问题就输出 {"issues": []}。add/drop/rewire 只在有对应 issue 时给。

【目标】
@GOAL@

【能力表】
@TABLE@
"""

AUDIT_DIFF_PROMPT = """你是独立评审。上版蓝图**已经评审过**，没变的能力不要重复评审。

本轮只做了一次重规划（降级 / 补能力 / 重连依赖 / 重开）。只审变更带来的新风险：
1. 新增或重开的能力：探针空壳是不是也能过、目标隐含的验收动作有没有漏
2. rewired 的依赖：有没有成环，有没有把各自独立的命令串成链（一条红锁死一片）
3. removed / descoped：有没有留下指向它的悬空依赖

四类问题同全量评审：missing / dup / broken_dep / fake_probe，只报能指到具体 id 的。
输出 JSON：{"issues":[...],"add":[],"drop":[],"rewire":{}}；没问题输出 {"issues": []}。
**不要重复列出未变更条目的问题**，也不要给改进建议。

【目标】
@GOAL@

【本轮变更】
@DIFF@

【仅变更涉及的能力】
@TABLE@
"""


def _default_llm(prompt: str, *, json_mode: bool = True, task_id: str = "blueprint",
                 node_id: str = "plan", step: str = "strongest",
                 max_out: int | None = None,
                 messages: list[dict] | None = None) -> dict:
    """默认走远端最强档（规格是全链最高杠杆的一步）。测试时整个替换掉。

    `messages`：多轮对话（片内自验那类"跑→看→改"）。给了就把历史原样转给远端，
    否则退化成单轮 prompt。
    """
    from . import remote
    from . import settings_store as _st
    idx = None
    try:
        if str(step).lower() in ("strongest", "best"):
            idx = _st.strongest_step_index()
    except Exception:                                    # noqa: BLE001
        idx = None
    kw = {}
    if messages:
        kw["messages"] = messages            # 多轮（片内自验）历史在变，不能复用
    else:
        kw["reuse_scope"] = f"{task_id}:{node_id}"   # 确定性单轮：同证据复用
    r = remote.complete(prompt, task_id=task_id, node_id=node_id, json_mode=json_mode,
                        step_index=idx, thinking="on", max_out=max_out, **kw)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "远端调用失败", "answer": ""}
    return {"ok": True, "answer": r.get("answer") or "", "usage": r.get("usage") or {},
            "by": r.get("by") or ""}


def _json_of(text: str) -> dict | None:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            d = json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(d, dict):
            return d
    return None


def table_text(bp: Blueprint) -> str:
    """给审计看的紧凑能力表（只给判据需要的字段）。"""
    rows = []
    for c in bp.caps:
        rows.append({"id": c.id, "desc": c.desc, "probe": c.probe,
                     "deps": c.deps, "funcs_est": c.funcs})
    return json.dumps(rows, ensure_ascii=False, indent=1)


def fingerprint(bp: Blueprint) -> str:
    """蓝图指纹：只含会改变评审结论的**结构**字段。

    故意不含 version / notes / created_at / status —— 重规划只改版本号或备注、
    某条能力从 pending 变 done，评审对象并没有变，不该再花一次最强档调用。
    降级 / 删能力这类结构变化由 diff 评审（AUDIT_DIFF_PROMPT）单独覆盖。
    """
    rows = []
    for c in sorted(bp.caps, key=lambda x: str(x.id)):
        rows.append({"id": c.id, "desc": c.desc, "probe": c.probe,
                     "deps": list(c.deps or []), "funcs": c.funcs})
    raw = json.dumps({"goal": bp.goal or "", "caps": rows},
                     ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _diff_changes(diff: dict | None) -> dict | None:
    """从 replan.diff() 里只取会造成新风险的变更；None = 全量评审。"""
    if diff is None:
        return None
    keys = ("added", "removed", "rewired", "descoped", "reopened")
    return {k: diff.get(k) for k in keys if diff.get(k)} or {}


def _audit_cache_load(ws) -> dict:
    try:
        d = json.loads((Path(ws) / AUDIT_CACHE_FILE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _audit_cache_store(ws, key: str, result: dict) -> None:
    """原子写：进程被中断也不会留下 0 字节缓存（docs/26 §7 踩过）。"""
    p = Path(ws) / AUDIT_CACHE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        cache = _audit_cache_load(ws)
        cache[key] = {"at": time.time(), "result": result}
        if len(cache) > 40:
            for k in sorted(cache, key=lambda x: cache[x].get("at") or 0)[:-40]:
                cache.pop(k, None)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:                                    # noqa: BLE001
        pass    # 缓存只是优化，写失败不能拖垮主流程


def _n_funcs_for(budget: float) -> int:
    if budget <= 0:
        return 200
    return max(3, int(budget / CNY_PER_FUNC))


# ---------------------------------------------------------------- 推导 / 审计 / 修补

def _probe_paths(probe: dict) -> list[str]:
    steps = (probe or {}).get("steps") or []
    if steps:
        return [str((s or {}).get("path") or "") for s in steps]
    return [str((probe or {}).get("path") or "")]


def _jtext(x) -> str:
    try:
        return json.dumps(x, ensure_ascii=False)
    except Exception:                                    # noqa: BLE001
        return str(x)


def _cap_for(caps: list, needle: str):
    return next((c for c in caps if any(needle in p for p in _probe_paths(c.probe))), None)


def _auth_step(cap, needle: str, user: str, want_token: bool) -> dict | None:
    """从注册/登录能力里复制出错的一步，换上本链专用的用户名。"""
    for st in ((cap.probe or {}).get("steps") or []):
        st = st or {}
        if needle not in str(st.get("path") or ""):
            continue
        step = {"method": str(st.get("method") or "POST").upper(),
                "path": str(st.get("path") or "")}
        body = st.get("json") if st.get("json") is not None else st.get("data")
        if isinstance(body, dict):
            body = dict(body)
            for k in list(body):
                if "user" in k.lower() or "name" in k.lower() or "email" in k.lower():
                    body[k] = user
                elif "pass" in k.lower():
                    body[k] = "p1"
            step["json"] = body
        if needle == "/register":
            step["expect_status"] = [200, 201, 409]      # 已存在也算链能继续（幂等）
            # 注册响应里的 id 就是后续 /users/{user_id}/... 要用的实体 id
            step["save"] = {"user_id": "id", "uid": "id", "id": "id"}
        if want_token:
            step["save"] = {"token": "token"}
        return step
    return None


def normalize_http(bp: "Blueprint", notes: list[str] | None = None) -> int:
    """把 http 探针改造成**自包含**的请求链（确定性修补，不花 token）。

    为什么必须有（2026-11-07 实测）：蓝图给 Web 项目生成的探针长这样——
        cap.user.login:    POST /auth/login  {"username":"u1"}     # u1 从没注册过
        cap.ticket.create: POST /tickets     Bearer ${token}       # token 从没 save 过
        cap.ticket.update: PUT  /tickets/{ticket_id}               # 字面占位符，id 不存在
    三条都看起来合理，但**永远不可能通过**（每条探针是独立进程，内存不共享）。
    这种探针比没有更糟：它会把"这条能力做不出来"写进蓝图，然后被降级、被报告成需求的问题。
    所以这里机械补齐：同链内先注册再登录、先建资源再改它，用 save/${var} 串起来。
    """
    caps = [c for c in bp.caps if str((c.probe or {}).get("kind") or "") == "http"]
    if not caps:
        return 0
    reg, login = _cap_for(caps, "/register"), _cap_for(caps, "/login")
    changed = 0
    for c in caps:
        probe = c.probe if isinstance(c.probe, dict) else {}
        steps = probe.get("steps")
        if not isinstance(steps, list) or not steps:
            continue
        new = json.loads(json.dumps(steps))
        user = "u_" + re.sub(r"[^a-z0-9]+", "_", str(c.id).lower())[:20]
        pre: list[dict] = []
        joined = _jtext(new)
        need_auth = "${token" in joined
        has_reg = any("/register" in str((s or {}).get("path") or "") for s in new)
        has_login = any("/login" in str((s or {}).get("path") or "") for s in new)
        # ① 链里有登录，却没有注册 → 先注册（否则登录必然 401）
        if has_login and not has_reg and reg is not None:
            st = _auth_step(reg, "/register", user, want_token=False)
            if st:
                pre.append(st)
        # ② 用到 ${token}，却没有登录 → 补注册 + 登录
        if need_auth and not has_login and login is not None:
            if not has_reg and reg is not None:
                st = _auth_step(reg, "/register", user, want_token=False)
                if st:
                    pre.append(st)
            st = _auth_step(login, "/login", user, want_token=True)
            if st:
                pre.append(st)
        # ④ 本链里的账号必须统一：注册用谁，登录就得用谁。
        #    否则链是"注册 u_xxx → 登录 u1"，永远 401（实测 2026-11-07 参考实现 8/9）。
        if has_login:
            for st in new:
                pth = str((st or {}).get("path") or "")
                if "/login" not in pth and "/register" not in pth:
                    continue
                body = st.get("json")
                if isinstance(body, dict):
                    for k in list(body):
                        lk = str(k).lower()
                        if "user" in lk or "name" in lk or "email" in lk:
                            body[k] = user
                        elif "pass" in lk:
                            body[k] = "p1"
                ej = st.get("expect_json")
                if isinstance(ej, dict):
                    for k in list(ej):
                        lk = str(k).lower()
                        if "user" in lk or "name" in lk or "email" in lk:
                            ej[k] = user
        # ③ 路径里有 {var} / :var 占位符 → 先建一个资源，把 id 存进变量
        for i, st in enumerate(new):
            path = str((st or {}).get("path") or "")
            if "${" in path:
                continue          # 已经是 ${var} 形式（上一轮规整过的），别再动
            m = re.search(r"\{(\w+)\}", path) or re.search(r":(\w+)(?=/|$)", path)
            if not m:
                continue
            var = m.group(1)
            base = re.split(r"[{:]", path, maxsplit=1)[0].rstrip("/")
            maker = None
            for cand in caps:
                if cand is c:
                    continue
                for cs in ((cand.probe or {}).get("steps") or []):
                    cs = cs or {}
                    if (str(cs.get("method") or "").upper() == "POST"
                            and str(cs.get("path") or "").rstrip("/") == base):
                        maker = cs
                        break
                if maker:
                    break
            if maker is not None:
                mstep = json.loads(json.dumps(maker))
                mstep["save"] = {var: "id"}
                if "expect_status" not in mstep:
                    mstep["expect_status"] = [200, 201]
                pre.append(mstep)
                new[i]["path"] = re.sub(r"\{%s\}" % re.escape(var), "${%s}" % var, path)
                new[i]["path"] = re.sub(r":%s(?=/|$)" % re.escape(var),
                                        "${%s}" % var, new[i]["path"])
            else:
                # 没有创建该实体的能力（如 /boards/{board_id}/tickets）：
                # 用字面量 1 落死。简报里会把这个确切路径逐字写给作者，
                # 作者照着实现路由即可 —— 比留一个永远 404 的 {board_id} 强得多。
                fixed = re.sub(r"\{\w+\}", "1", path)
                fixed = re.sub(r":\w+(?=/|$)", "1", fixed)
                if fixed != path:
                    new[i]["path"] = fixed
                    if notes is not None:
                        notes.append(f"{c.id}: 占位符 {var} 没有可创建的能力 → 固定成 {fixed}")
        # ⑤ 读类能力需要**种子数据**：全新数据库里表都还没建出来，GET 会直接 500
        #    （"no such table: tickets"）。找同资源的 POST 创建能力，插到最后一步之前。
        last = (new[-1] if new else {})
        lpath = str((last or {}).get("path") or "")
        if str((last or {}).get("method") or "").upper() == "GET" and "/auth/" not in lpath:
            has_post = any(str((sm or {}).get("method") or "").upper() == "POST"
                           and str((sm or {}).get("path") or "") == lpath for sm in new)
            if not has_post:
                creator = None
                for cand in caps:
                    if cand is c:
                        continue
                    for cs in ((cand.probe or {}).get("steps") or []):
                        cs = cs or {}
                        cp = str(cs.get("path") or "")
                        if (str(cs.get("method") or "").upper() == "POST" and cp.startswith("/")
                                and "/auth/" not in cp and cp in lpath):
                            creator = cs
                            break
                    if creator:
                        break
                if creator is not None:
                    seed = json.loads(json.dumps(creator))
                    seed["save"] = {k: ["id", "ticket_id", "pk"] for k in
                                    ("ticket_id", "id", "user_id", "uid", "board_id")}
                    seed.setdefault("expect_status", [200, 201, 409])
                    new.insert(len(new) - 1, seed)
                    # 只判 200 太弱：空实现直接返回 `[]` 也能过（实测 2026-11-07——
                    # 列表返回硬编码的假数据照样绿）。把种子里那个可辨识的值写进判据。
                    _sv = (seed.get("json") or {}).get("title")
                    if _sv and not (new[-1] or {}).get("body_has"):
                        new[-1]["body_has"] = str(_sv)
                    if notes is not None:
                        notes.append(f"{c.id}: 读接口前置一条种子数据（POST {seed.get('path')}）")
        # ⑥ 登录必须**真的发放凭证**：只判 200 时，"只回 username、不发 token" 也能绿，
        #    于是所有受保护接口等于没有鉴权（实测 2026-11-07）。
        _last = (new[-1] if new else {})
        if "/login" in str((_last or {}).get("path") or ""):
            new[-1].setdefault("body_has_any", ["token"])
        if pre or new != steps:
            probe["steps"] = pre + new
            c.probe = probe
            changed += 1
            if notes is not None:
                notes.append(f"{c.id}: http 探针已补齐自包含链（+{len(pre)} 步："
                             + "/".join(str(s.get('path')) for s in pre) + "）")
    return changed


def fill_http_modules(bp: "Blueprint") -> int:
    """给 http 能力**确定性地**分配落点模块（模型写的 modules 经常是空的）。

    为什么必须由引擎定：实测（2026-11-07）目标里写着 FastAPI，契约合成却输出了
    `calc.py` —— 它把推导提示词里的**示例路径**抄了过来，还编了个 `main() == 'ok'`。
    作者照着写两行就能"通过"函数级判据，但 HTTP 探针必然 404，整片白跑。
    落点由 probe 的路径唯一决定（/auth/register → app/auth.py），不留想象空间。
    """
    seg_map = {"boards": "tickets", "board": "tickets", "comments": "tickets",
               "tags": "tickets", "auth": "auth", "users": "users", "user": "users",
               "tickets": "tickets", "dashboard": "dashboard", "": "web"}
    changed = 0
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        if c.modules:
            continue
        # 用**最后一步**的路径定落点（前面几步是自包含补齐的注册/登录，不代表这条能力）
        paths = _probe_paths(c.probe)
        seg = ""
        for p in reversed(paths):
            parts = [x for x in str(p).strip("/").split("/") if x and not x.startswith("$")]
            if parts:
                seg = parts[0]
                break
        c.modules = [f"app/{seg_map.get(seg, seg if re.fullmatch(r'[a-z_]+', seg) else 'web')}.py"]
        changed += 1
    return changed


EXTEND_PROMPT = """你是软件架构师。下面这个项目**已经做完了这些能力**（不要重复）。
现在要把它**继续长到目标规模**：请补充 {need} 条**新能力**，只输出 JSON。

【已经做完的（不要重复，也不要换个说法重做）】
{have}

【补充要求】
1. 必须落在目标里**还没被覆盖**的部分（前端页面、评论、标签、负责人指派、优先级、
   密码修改、分页/排序、导出、审计日志、健康检查 …… 按目标实际缺的挑）。
2. 每条能力都要能**独立观测**（方法/路径/请求体/期望结果都写死），写法与之前一致：
   给 http 探针（写 steps，必要时自带注册/登录前置），或 py_call / pytest。
3. 粒度 3~8 个函数，别把 20 个函数塞进一条。
4. **不要和已有能力重复观测同一个行为**；也不要再造一遍"注册/登录"。
5. 前端类能力用 `{{"kind":"http","app":"app.main:app","method":"GET","path":"/",
   "body_has":["<html"]}}` 这种形状；多条前端能力请分别落在不同路径（/tickets 页面、/login 页面…）。

【目标】
{goal}

只输出 JSON：
{{"caps":[{{"id":"...","desc":"...","probe":{{...}},"deps":[],"funcs_est":4,"modules":[]}}],
  "dropped":[{{"what":"...","why":"..."}}]}}
"""


def extend(bp: "Blueprint", goal: str, *, need: int = 4, llm=None, ws=None) -> dict:
    """给"已完成"的蓝图**追加新能力**（规模机制）。

    为什么需要（实测 2026-11-07）：目标写着"XL / ≥10000 行"，蓝图只排了 9 条能力，
    做完 9 条就报 100% —— 交付物 400 行。**规模不是模型能力问题，是规划机制问题**：
    没有人按"还差多少行"去要新能力。这里补这一环：按缺口要能力，反复调用就能长上去。
    """
    llm = llm or _default_llm
    have = "\n".join(f"- {c.id}：{c.desc[:60]}（{describe_probe(c.probe)}）"
                     for c in bp.caps) or "（还没有）"
    prompt = (EXTEND_PROMPT.replace("{need}", str(max(1, int(need))))
              .replace("{have}", have[:4000])
              .replace("{goal}", Blueprint.clip(goal or bp.goal, 4000)))
    r = llm(prompt, json_mode=True, task_id="blueprint-extend", node_id="plan", max_out=12000)
    if not r.get("ok"):
        return {"ok": False, "why": f"远端调用失败：{r.get('error')}", "added": []}
    data = _json_of(r.get("answer") or "") or {}
    raw = data.get("caps") or []
    added: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        c = Cap.from_dict(item)
        if not c.id or bp.by_id(c.id):
            continue
        bp.add(c)
        added.append(c.id)
    fill_budget(bp)
    if ws is not None:
        save(bp, ws)
    return {"ok": True, "added": added, "dropped": data.get("dropped") or []}


def describe_probe(p) -> str:                                # 小工具，避免上层重复实现
    try:
        from . import probe as _p
        return _p.describe(p if isinstance(p, dict) else {})
    except Exception:                                        # noqa: BLE001
        return ""


def derive(goal: str, *, budget_cny: float = 0.0, llm=None, do_audit: bool = True,
           max_repair: int = 1, emit=None, ws=None) -> dict:
    """目标 → 蓝图（derive 一次 + audit 一次 + 机械修补）。

    返回 {ok, blueprint, issues, notes, audit, calls, problems}。
    ok=False 时**不跑**：蓝图不合格就开跑，等于把错误固化进后面每一轮。
    """
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "blueprint": None, "problems": ["目标是空的"], "calls": 0}
    llm = llm or _default_llm
    calls = 0
    notes: list[str] = []
    bp = Blueprint(goal=goal, version=1, total_budget_cny=float(budget_cny or 0.0),
                   created_at=time.time())

    prompt = (DERIVE_PROMPT.replace("@GOAL@", Blueprint.clip(goal, 6000))
              .replace("@BUDGET@", f"{bp.total_budget_cny:.2f}")
              .replace("@PER_FUNC@", f"{CNY_PER_FUNC:.4f}")
              .replace("@N_FUNCS@", str(_n_funcs_for(bp.total_budget_cny))))
    # **推导要有自愈**（实测 exp1e）：唯一一次没有重试的远端调用就是这里，
    # 一旦输出被截断/解析不出 JSON，整次运行直接以"空蓝图"失败、一分钱没花却什么都没做。
    # 口径与契约合成一致：解析不出来 → 加大正文配额重试一次，并明确"只输出 JSON"。
    data = None
    out_budget = 12000
    for _attempt in range(2):
        r = llm(prompt if _attempt == 0 else
                prompt + "\n\n【上一次输出没解析成 JSON（可能被截断）】这次只输出 JSON 本体："
                         "不要解释、不要 Markdown 围栏外的文字、不要省略。",
                json_mode=True, task_id="blueprint", node_id="plan", max_out=out_budget)
        calls += 1
        if not r.get("ok"):
            return {"ok": False, "blueprint": None, "calls": calls,
                    "problems": [f"能力表推导失败：{r.get('error')}"]}
        data = _json_of(r.get("answer") or "")
        if data and (data.get("caps") or []):
            break
        out_budget = min(64000, out_budget * 2)
    raw_caps = (data or {}).get("caps") or []
    if not raw_caps:
        return {"ok": False, "blueprint": None, "calls": calls,
                "problems": ["远端没给出任何能力（空蓝图不能开跑；已重试一次并加大配额）"]}
    for raw in raw_caps:
        if not isinstance(raw, dict):
            continue
        c = Cap.from_dict(raw)
        if not c.id:
            notes.append(f"丢掉一条没有 id 的能力：{str(raw.get('desc') or '')[:40]}")
            continue
        if bp.by_id(c.id):
            notes.append(f"{c.id}: id 重复，保留第一条")
            continue
        bp.add(c)
    for d in (data or {}).get("dropped") or []:
        if isinstance(d, dict) and d.get("what"):
            notes.append(f"推导阶段就砍掉：{d.get('what')}（{d.get('why') or '未说明'}）")
    fill_budget(bp)
    if emit:
        emit("blueprint", {"phase": "derived", "caps": len(bp.caps),
                           "version": bp.version, "notes": notes[-4:]})

    ok_budget, budget_why = within_budget(bp)
    if not ok_budget:
        notes.append("预算不足：" + budget_why + " → 需要降级（见 descope）")

    audit_result: dict = {"issues": [], "add": [], "drop": [], "rewire": {}}
    for _ in range(max(0, int(max_repair)) + (1 if do_audit else 0)):
        if not do_audit:
            break
        audit_result = audit(bp, llm=llm, emit=emit, ws=ws)
        if not (audit_result.get("cached") or audit_result.get("skipped")):
            calls += 1            # 缓存命中不算一次远端调用（不许把省钱写成调用过）
        if not audit_result.get("ok"):
            notes.append("审计失败（按未审计处理，风险自负）："
                         + str(audit_result.get("error") or "")[:80])
            break
        _issues = audit_result.get("issues") or []
        if not _issues and not (audit_result.get("add") or audit_result.get("drop")
                                or audit_result.get("rewire")):
            notes.append("审计：未发现问题")
            break
        changed = repair(bp, audit_result, notes=notes)
        if not changed:
            break

    # http 探针必须能被真的跑通（自包含）：模型写出来的十条里有九条缺注册/缺 token/
    # 缺占位符实体。这是确定性的活，交给机械修补，不要让远端重写（它只会再犯一次）。
    _n_http = normalize_http(bp, notes)
    if _n_http and emit:
        emit("probe_normalized", {"probes": _n_http})
    _n_mod = fill_http_modules(bp)
    if _n_mod and emit:
        emit("modules_assigned", {"caps": _n_mod,
                                  "modules": sorted({m for c in bp.caps for m in (c.modules or [])})})

    issues = validate(bp)
    bad = fatal(issues)
    # 推导/审计过程中被砍掉的范围、被拒收的能力，都要留在蓝图里（人能看见、报告能引用）
    bp.notes.extend(n for n in notes if n not in bp.notes)
    return {"ok": not bad, "blueprint": bp, "issues": issues, "problems": bad,
            "notes": notes, "audit": audit_result, "calls": calls}


def audit(bp: Blueprint, *, llm=None, emit=None, ws=None, diff=None) -> dict:
    """独立评审：**新上下文**，只看目标 + 能力表。

    两道省钱的闸（实测 blueprint-audit 曾占最近 500 次调用花费的三成）：
      1. 指纹没变 → 直接复用上次评审结论，一次远端调用都不发；
      2. 重规划走 diff 模式：没有结构变更就跳过，有变更只把变更条目 +
         涉及到的那几条能力交给评审，不重审整张表。
    返回里带 cached / skipped，调用方据此决定 calls 计不计（不许把缓存算成"调用过"）。
    """
    llm = llm or _default_llm
    changes = _diff_changes(diff)
    empty = {"ok": True, "issues": [], "add": [], "drop": [], "rewire": {}}
    if diff is not None and not changes:
        out = {**empty, "skipped": True, "cached": True}
        if emit:
            emit("blueprint", {"phase": "audited", "issues": 0, "skipped": True,
                               "why": "重规划没有结构变更，跳过复评"})
        return out

    dpart = hashlib.sha1(json.dumps(changes or {}, ensure_ascii=False,
                                    sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    ckey = f"{AUDIT_CACHE_VERSION}|{'diff' if diff is not None else 'full'}|{fingerprint(bp)}|{dpart}"
    if ws is not None:
        hit = _audit_cache_load(ws).get(ckey)
        if isinstance(hit, dict) and isinstance(hit.get("result"), dict):
            out = dict(hit["result"])
            out["cached"] = True
            if emit:
                emit("blueprint", {"phase": "audited", "cached": True,
                                   "why": "蓝图指纹未变，复用上次评审（0 调用）"})
            return out

    if diff is not None:
        ids: set[str] = set()
        for v in (changes or {}).values():
            if isinstance(v, dict):
                ids.update(str(k) for k in v)
            elif isinstance(v, (list, tuple)):
                ids.update(str(x) for x in v)
        rows = [{"id": c.id, "desc": c.desc, "probe": c.probe,
                 "deps": c.deps, "funcs_est": c.funcs}
                for c in bp.caps if c.id in ids]
        prompt = (AUDIT_DIFF_PROMPT.replace("@GOAL@", Blueprint.clip(bp.goal or "", 4000))
                  .replace("@DIFF@", json.dumps(changes, ensure_ascii=False,
                                                indent=1, default=str)[:6000])
                  .replace("@TABLE@", json.dumps(rows, ensure_ascii=False,
                                                 indent=1, default=str)[:12000]))
    else:
        prompt = (AUDIT_PROMPT.replace("@GOAL@", Blueprint.clip(bp.goal or "", 4000))
                  .replace("@TABLE@", table_text(bp)))
    r = llm(prompt, json_mode=True, task_id="blueprint-audit", node_id="plan")
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "审计调用失败",
                "issues": [], "add": [], "drop": [], "rewire": {}}
    d = _json_of(r.get("answer") or "") or {}
    out = {"ok": True,
           "issues": [i for i in (d.get("issues") or []) if isinstance(i, dict)],
           "add": [a for a in (d.get("add") or []) if isinstance(a, dict)],
           "drop": [str(x) for x in (d.get("drop") or []) if str(x).strip()],
           "rewire": {str(k): v for k, v in (d.get("rewire") or {}).items()
                      if isinstance(v, dict)}}
    if ws is not None and out["ok"]:
        _audit_cache_store(ws, ckey, out)
    if emit:
        emit("blueprint", {"phase": "audited", "issues": len(out["issues"]),
                           "add": [a.get("id") for a in out["add"]][:6],
                           "drop": out["drop"][:6],
                           "mode": "diff" if diff is not None else "full"})
    return out


def repair(bp: Blueprint, audit_result: dict, *, notes: list[str] | None = None) -> bool:
    """把审计意见**机械地**写回蓝图。返回是否有改动。

    一条纪律：**已观测通过（done）的能力不许删**。删掉等于把"已经证明过的事实"抹掉，
    后面的报告会变成谎话。
    """
    notes = notes if notes is not None else []
    changed = False
    done = bp.done()
    for cid in audit_result.get("drop") or []:
        if cid in done:
            notes.append(f"审计建议删掉 {cid}，但它已经观测通过 → 不删")
            continue
        if bp.drop(cid):
            notes.append(f"按审计删掉能力 {cid}")
            changed = True
    for raw in audit_result.get("add") or []:
        c = Cap.from_dict(raw)
        if not c.id:
            continue
        if bp.by_id(c.id):
            continue
        errors = cap_issues(c)
        if errors:
            notes.append(f"审计新增的 {c.id} 不合规，未采纳：{errors[0]}")
            continue
        c.budget_cny = c.budget_cny or round(c.funcs * CNY_PER_FUNC, 4)
        if bp.add(c):
            notes.append(f"按审计补上能力 {c.id}：{c.desc[:40]}")
            changed = True
    for cid, patch in (audit_result.get("rewire") or {}).items():
        deps = patch.get("deps")
        if isinstance(deps, list) and bp.rewire(cid, [str(x) for x in deps]):
            notes.append(f"按审计重连依赖 {cid} → {deps}")
            changed = True
    for issue in audit_result.get("issues") or []:
        kind = str(issue.get("kind") or "")
        cap = str(issue.get("cap") or "")
        detail = str(issue.get("detail") or "")
        if kind == "fake_probe":
            # 探针假 → 能力保留，但必须显式标出来（不许当成能用的能力）
            c = bp.by_id(cap)
            if c and c.status != "done":
                c.why = (c.why + "；" if c.why else "") + f"审计判探针可假通过：{detail}"[:160]
                changed = True
        notes.append(f"审计[{kind}] {cap}: {detail}"[:200])
    return changed


def descope(bp: Blueprint, cid: str, why: str) -> bool:
    """主动放弃一条能力 —— 必须带原因，并且会出现在报告和 diff 里。"""
    c = bp.by_id(cid)
    if not c or c.status == "done":
        return False
    c.status = "descoped"
    c.why = (why or "未说明原因")[:200]
    bp.notes.append(f"降级 {cid}：{c.why}")
    return True


# ---------------------------------------------------------------- 存取 / 展示

def save(bp: Blueprint, ws) -> Path:
    p = Path(ws) / BLUEPRINT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(bp.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)
    return p


def load(ws) -> Blueprint:
    p = Path(ws) / BLUEPRINT_FILE
    if not p.exists():
        return Blueprint()
    try:
        return Blueprint.from_dict(json.loads(p.read_text(encoding="utf-8-sig")))
    except Exception:                                    # noqa: BLE001
        return Blueprint()


def unresolved(bp: Blueprint) -> list[Cap]:
    return [c for c in bp.caps if c.status != "done"]


def summary(bp: Blueprint) -> str:
    """人审那一页：一屏看完目标、能力、缺口、预算。"""
    n = len(bp.caps)
    done = len(bp.done())
    desc = sum(1 for c in bp.caps if c.status == "descoped")
    ok, why = within_budget(bp)
    lines = [f"目标：{bp.goal}", "",
             f"版本 v{bp.version}   能力 {n}（完成 {done} / 缺 {n - done - desc} / 降级 {desc}）   "
             f"{why}" + ("" if ok else "   ⚠ 超预算，先砍范围"), ""]
    head = f"{'状态':<9}{'能力':<26}{'观测方式':<38}{'依赖':<20}{'估算':<7}"
    lines.append(head)
    lines.append("-" * 96)
    from . import probe as _probe
    for c in bp.caps:
        obs = _probe.describe(c.probe)[:36]
        deps = ",".join(d.replace("cap.", "") for d in c.deps)[:18] or "-"
        st = {"absent": "缺", "blocked": "受阻", "partial": "半", "done": "✓",
              "descoped": "降级"}.get(c.status, c.status)
        if c.why and c.status in ("descoped", "blocked"):
            st = f"{st}*"
        lines.append(f"{st:<9}{c.id[:24]:<26}{obs:<38}{deps:<20}¥{c.budget_cny:<6.3f}")
    gaps = unresolved(bp)
    if gaps:
        lines += ["", f"缺口 {len(gaps)} 条（* = 有原因，见备注）："]
        for c in gaps[:12]:
            lines.append(f"  - {c.id}: {c.desc[:60]}")
    if bp.notes:
        lines += ["", "备注："]
        for x in bp.notes[-10:]:
            lines.append(f"  · {x}")
    return "\n".join(lines)
