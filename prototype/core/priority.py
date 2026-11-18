"""写码优先级：**核心业务优先**（**先只算、不接入** —— 2026-11-10 用户定稿）。

为什么有这个模块：
    现在的排程（`company._author_round`）是「模块依赖序 + cap_red 集合 + 轮号旋转」。
    轮号旋转每轮把起点挪一格 → **均匀覆盖**，与"核心业务优先跑通"方向相反。

    曾经拿"11-09 实测 3 个模块霸占 6 轮、10 个排不上"当依据，说不能改回按顺序排 ——
    **那是归因错了**（2026-11-10 用写作台账核实）：
    那 12 次连败**全在 11-09 21:48–23:15**、多次 `cost_cny = 0`
    （请求还没产出计费输出就被砍断），**是"限制远端能力"（输出被 1200 配额砍断）
    导致的写不出来，不是排程本身的问题**。那个引擎 bug 已修
    （配额/上下文/升档通道/成功回档），所以那条依据不再成立。

口径（全部确定性，同输入同输出）：

一、**核心优先**
    按**能力依赖图**的拓扑序（`blueprint.topo`）。模块的"解锁位置" =
    它承载的能力里拓扑序最靠前的那个 → 越靠前越核心。
    **核心 = 承载"被别的能力依赖"的能力**（fanout > 0）—— 那是主干承重墙。
    依赖图本身由 `blueprint.derive_fk_deps` 按结构依赖（路径占位符 / 请求体字段）
    机械补齐，不需要额外建模。

二、**不是核心的怎么排**（核心之外的剩余，依次比）
    1. 被依赖度 fanout（越高越像主干）
    2. 端到端红优先（`cap_red`：能力全过但端到端不通 → 这是"流程缺口"，
       比"契约没写完"更接近核心）
    3. 模块角色（data/algorithm 先于 generic/orchestration/ui —— 地基先于门面）
    4. 模块级被依赖数（模块依赖图）
    5. 历史成功率（写作台账）：成功率先做（先摘便宜的果子）；
       **连续顶格仍写不出的靠后** —— #2 的记录**只用于排序，不用于砍**。

三、**公平性保底**（防饿死）
    `starved` 计数：连续 N 轮没被排到的模块会被提权到"非核心里的第一类"。
    这正是轮号旋转本来想解决的问题，但它用的是**"均匀"而不是"保底"**：
    均匀会持续稀释核心优先；保底只在真的被冷落时才动，且不越过核心。

**接入状态（2026-11-11 更新）**：`rank()` 已接进 `company._author_round` 的派单
（§20.4 落点 5）。本模块**仍然不改预算、不砍能力** —— 只决定"先做哪个"，
以及用**落盘的被服务时间**做保底提权（跨重启有效，见 `mark_served`/`starved_map`）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

# 角色优先级：地基先于门面（与 project.order_modules 的口径一致）
ROLE_PRIO = {"data": 0, "algorithm": 1, "generic": 2, "orchestration": 3, "ui": 4}
ROLE_FALLBACK = 2
# 连续多少轮没被排到就算"被冷落"（保底提权阈值）
STARVE_LIMIT = 3
# 没有能力映射时的解锁位置（排最后）
UNLOCK_UNKNOWN = 999999

# 「上次被服务到」的时间账（落盘，跨重启有效）
#
# 为什么必须落盘（2026-11-11 01:10 实测）：夜班**经常重启**（看门狗续跑、
# 崩溃后重拉），如果"连续几轮没被排到"只存在内存里，重启一次计数就清零 →
# 被冷落的模块会**再次饿死**。实测 2026-11-10 21:40 就是这么复发的：
# 轮号旋转被依赖修正还原 → 前 3 个模块霸占 6 轮 → 尾部模块一次没被写过。
SERVED_FILE = ".sixcoder/priority_served.json"
# 一轮约多久（分钟）：夜班一轮（含判据与探针）实测 5–10 分钟。
# 用"分钟/轮"把时间差折成轮数，让 `starved` 与阈值 STARVE_LIMIT 同量纲。
MINUTES_PER_ROUND = 8.0


def load_served(ws: Path) -> dict[str, float]:
    """{模块: 上次被服务到的 unix 时间戳}。读不到就当空。"""
    try:
        p = Path(ws) / SERVED_FILE
        if not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {str(k): float(v) for k, v in (raw or {}).items()}
    except Exception:                                    # noqa: BLE001
        return {}


def save_served(ws: Path, data: dict[str, float]) -> None:
    """落盘"上次被服务到"的时间（原子写）。失败不影响主流程。"""
    try:
        p = Path(ws) / SERVED_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): float(v) for k, v in (data or {}).items()},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(p)
    except Exception:                                    # noqa: BLE001
        pass


def mark_served(ws: Path, paths: list[str], now: float | None = None) -> None:
    """标记这些模块"本轮被服务到了"（保底提权要用）。"""
    if not paths:
        return
    ts = float(now if now is not None else time.time())
    data = load_served(ws)
    for p in paths:
        data[str(p)] = ts
    save_served(ws, data)


def starved_map(ws: Path, now: float | None = None) -> dict[str, int]:
    """{模块: 连续多少"轮"没被排到} —— 按时间差折算（跨重启也有效）。

    从没被服务过的模块算"冷落很久"（给足保底提权），但**没有上限**，
    这样刚建的项目不会因为"没历史"而把所有模块都当成饿死。
    """
    ts = float(now if now is not None else time.time())
    data = load_served(ws)
    out: dict[str, int] = {}
    for path in data:
        mins = max(0.0, (ts - float(data[path]))) / 60.0
        out[str(path)] = int(mins / MINUTES_PER_ROUND)
    return out


def module_deps(contracts: dict) -> dict[str, list[str]]:
    """模块级依赖（对外公开版；内部实现同名私有函数）。"""
    return _module_deps(contracts)


def _load_blueprint(ws: Path):
    """读蓝图；读不到就返回 None（不抛错 —— 这个模块只是"算分"，不该拖垮调用方）。"""
    try:
        from . import blueprint as _bp
        b = _bp.load(ws)
        return b if b and getattr(b, "caps", None) else None
    except Exception:                                    # noqa: BLE001
        return None


def cap_topology(bp) -> tuple[list[str], dict[str, int], dict[str, int]]:
    """能力依赖图 → (拓扑序, 位置表, 被依赖度表)。

    位置越小 = 越靠上游 = 越核心。被依赖度 = 有多少条能力依赖它。
    """
    if bp is None:
        return [], {}, {}
    try:
        from . import blueprint as _bp
        order, _missing = _bp.topo(bp)
    except Exception:                                    # noqa: BLE001
        order = [c.id for c in getattr(bp, "caps", [])]
    pos = {cid: i for i, cid in enumerate(order)}
    fan: dict[str, int] = {}
    for c in getattr(bp, "caps", []):
        for d in (getattr(c, "deps", None) or []):
            fan[str(d)] = fan.get(str(d), 0) + 1
    return order, pos, fan


def cap_module_map(bp, ws: Path) -> dict[str, list[str]]:
    """能力 → 它落在哪些模块上（`cap.modules` + 端点路径反查，两套路径体系都要）。

    为什么两套都要：能力表点名的可能是 `app/api/auth.py`（引擎接线生成），
    而契约层落点是 `app/authz.py`（远端能改的那个）——
    只算一套会让一半能力映射不到模块（实测 71 项红灯只有 4 项能落到工作单）。
    """
    out: dict[str, list[str]] = {}
    if bp is None:
        return out
    try:
        from . import openhouse as _oh
    except Exception:                                    # noqa: BLE001
        return out
    for c in getattr(bp, "caps", []):
        mods: set[str] = set()
        for m in (getattr(c, "modules", None) or []):
            mods.add(str(m))
        try:
            for p in _oh.cap_paths(c):
                for m in _oh.module_candidates(ws, p):
                    mods.add(str(m))
        except Exception:                                # noqa: BLE001
            pass
        for m in mods:
            out.setdefault(m, []).append(c.id)
    return out


def _module_deps(contracts: dict) -> dict[str, list[str]]:
    try:
        from . import project as _proj
        return dict((_proj.build_map(contracts) or {}).get("deps") or {})
    except Exception:                                    # noqa: BLE001
        return {}


def _history(ws: Path) -> dict[str, dict]:
    """写作台账里的每模块成功率（#2 的记录，**只用于排序**）。"""
    try:
        from . import known_gap as _kg
        return _kg.summarize(ws)
    except Exception:                                    # noqa: BLE001
        return {}


def rank(ws: Path, contracts: dict, bp=None, cap_red=None, starved=None,
         done_modules=None) -> list[dict]:
    """给模块排优先级。**只算，不接** —— 调用方自己决定取前几个。

    参数
      · ws            产物仓（算端点反查与读台账）
      · contracts     契约表（算模块角色与模块依赖）
      · bp            蓝图（不给就从 ws/.sixcoder/blueprint.json 读）
      · cap_red       端到端红灯模块集合（可选）
      · starved       {模块: 连续没被排到的轮数}（可选，保底提权用）
      · done_modules  已完成的模块（可选，默认不排除）
    返回：按优先级从高到低排好的 dict 列表，每条都带**可解释的 reason**。
    """
    ws = Path(ws)
    contracts = contracts or {}
    cap_red = set(cap_red or set())
    starved = dict(starved or {})
    done_modules = set(done_modules or set())
    if bp is None:
        bp = _load_blueprint(ws)

    _order, pos, fan = cap_topology(bp)
    cmap = cap_module_map(bp, ws)
    mdeps = _module_deps(contracts)
    hist = _history(ws)

    try:
        from . import project as _proj
    except Exception:                                    # noqa: BLE001
        _proj = None

    rows: list[dict] = []
    for rel, c in contracts.items():
        if rel in done_modules:
            continue
        cids = cmap.get(rel) or []
        ps = [pos[x] for x in cids if x in pos]
        unlock = min(ps) if ps else UNLOCK_UNKNOWN
        fanout = max([fan.get(x, 0) for x in cids], default=0)
        core = bool(cids) and fanout > 0
        role = "generic"
        if _proj is not None:
            try:
                role = str(_proj.role_of(rel, c) or "generic")
            except Exception:                            # noqa: BLE001
                role = "generic"
        h = hist.get(rel) or {}
        row = {
            "path": rel,
            "core": core,
            "unlock": unlock,
            "fanout": fanout,
            "e2e_red": rel in cap_red,
            "role": role,
            "role_pri": ROLE_PRIO.get(role, ROLE_FALLBACK),
            "mod_deps": len(mdeps.get(rel) or []),
            "success_rate": float(h.get("success_rate") or 0.0),
            "attempts": int(h.get("attempts") or 0),
            "top_fail_streak": int(h.get("top_fail_streak") or 0),
            "starved": int(starved.get(rel) or 0),
            "caps": sorted(cids),
        }
        row["reason"] = _reason(row)
        rows.append(row)

    rows.sort(key=_sort_key)
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def _class_of(row: dict) -> int:
    """0 = 核心；1 = 非核心但被冷落（保底提权）；2 = 其余非核心。"""
    if row.get("core"):
        return 0
    if int(row.get("starved") or 0) >= STARVE_LIMIT:
        return 1
    return 2


def _sort_key(row: dict) -> tuple:
    cls = _class_of(row)
    core = bool(row.get("core"))
    return (
        cls,
        row["unlock"] if core else 0,              # 核心内：解锁位置越靠前越优先
        -row["starved"] if cls == 1 else 0,         # 保底：冷落越久越优先
        -row["fanout"],                             # 被依赖度
        0 if row.get("e2e_red") else 1,             # 端到端红优先
        row["role_pri"],                            # 角色：地基先于门面
        -row["mod_deps"],                           # 模块级被依赖数
        -row["success_rate"],                       # 成功率高的先摘
        row["top_fail_streak"],                     # 连续顶格仍写不出的靠后
        row["path"],                                # 稳定 tie-break
    )


def _reason(row: dict) -> str:
    """为什么排这个位置（可解释 —— 排程理由必须能给人看）。"""
    if row.get("core"):
        return (f"核心：承载 {row['fanout']} 条被依赖的能力，"
                f"解锁位置 #{row['unlock']}")
    bits = []
    if int(row.get("starved") or 0) >= STARVE_LIMIT:
        bits.append(f"已连续 {row['starved']} 轮没排到（保底提权）")
    if row.get("e2e_red"):
        bits.append("端到端红（流程缺口）")
    if row.get("fanout"):
        bits.append(f"被依赖度 {row['fanout']}")
    if row.get("role") in ("data", "algorithm"):
        bits.append(f"角色 {row['role']}（地基）")
    if row.get("attempts"):
        bits.append(f"历史成功率 {row['success_rate']:.0%}"
                    f"（{row['attempts']} 次）")
    if int(row.get("top_fail_streak") or 0) >= STARVE_LIMIT:
        bits.append(f"连续顶格仍写不出 {row['top_fail_streak']} 次 → 靠后")
    return "；".join(bits) or "非核心普通模块"


def report_lines(ranked: list[dict], top: int = 20) -> list[str]:
    """给人看的一节：优先级表（**只算，不接**）。"""
    if not ranked:
        return ["## 写码优先级（核心优先 · 只算不接）", "",
                "- 没有可排的模块（契约为空，或全部已完成）。"]
    lines = ["## 写码优先级（核心优先 · **只算不接**）", "",
             "| # | 模块 | 类别 | 解锁 | 成功率 | 理由 |", "|---|---|---|---|---|---|"]
    for r in ranked[:max(1, int(top or 20))]:
        cls = {0: "**核心**", 1: "保底提权", 2: "非核心"}[_class_of(r)]
        unlock = "—" if r["unlock"] == UNLOCK_UNKNOWN else f"#{r['unlock']}"
        rate = f"{r['success_rate']:.0%}" if r.get("attempts") else "—"
        lines.append(f"| {r['rank']} | `{r['path']}` | {cls} | {unlock} | "
                     f"{rate} | {r['reason']} |")
    n_core = sum(1 for r in ranked if r.get("core"))
    n_starved = sum(1 for r in ranked if _class_of(r) == 1)
    lines += ["", f"- 核心 {n_core} 个 · 保底提权 {n_starved} 个 · "
                  f"其余 {len(ranked) - n_core - n_starved} 个"]
    lines.append("- **本节只算不接**：不改派单、不改预算消耗。逻辑打磨好后再接进排程。")
    return lines
