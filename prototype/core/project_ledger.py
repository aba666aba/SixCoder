"""项目账本：**一个项目一本账**（2026-11-10 用户定稿）。

为什么要有这个模块（事故背景）：
    `remote_usage` 一张表靠 `task_id` 分区，而全自主夜班传的是**空** task_id，
    于是"某一行属于哪个项目"根本无从判断。11-10 18:05 因此出过事：
    停机线拿**全量历史账本**（所有项目 + 所有实验 ¥110.35）当本项目花费，
    而本项目实际只花 ¥31.01 → 夜班每轮开局就判"超额 5%"收工。

两张表的分工（§9.42）：
    `remote_usage`  明细流水，append-only。以后新行带 `project_id`（历史行为 ''，不可归属）。
    `project_ledger` 项目账本，一门一账。**停机线只读这张**。
                    可随时用 `sum(cost_cny) WHERE project_id=?` 重算校正（它是缓存视图）。

`project_id` 口径：**产物仓绝对路径的 sha1**。
    因为 `.sixcoder/budget.json` 本身就在产物仓里 = 额度天然是"仓库级"；
    同一个仓库换需求不重置额度（换需求 ≠ 换项目）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from . import db

TABLE = "project_ledger"

# 当前进程正在服务的产物仓。
# 为什么需要（2026-11-10 19:44 实测）：`remote.complete` 有 20 多个调用方
# （architect / contractgen / blueprint / briefing / patch …），**绝大多数不传 workspace**，
# 于是明细行的 project_id 一直是空 —— 那一列等于白加。
# 一个夜班进程同一时刻只服务一个产物仓，所以"进程级当前项目"是安全的：
# 显式传的 workspace 优先，没传就落到这个当前值。
_CURRENT: str = ""


def set_current(ws) -> str:
    """标记本进程当前服务的产物仓（任务入口调用一次即可）。"""
    global _CURRENT
    try:
        _CURRENT = str(Path(ws).resolve())
    except Exception:                                    # noqa: BLE001
        _CURRENT = str(ws or "")
    return _CURRENT


def current() -> str:
    """本进程当前服务的产物仓（空 = 未设置）。"""
    return _CURRENT


def project_id_of(ws) -> str:
    """产物仓 → 项目指纹（绝对路径 sha1 前 16 位）。空路径返回 ""。"""
    try:
        p = str(Path(ws).resolve()).replace("\\", "/").lower()
    except Exception:                                    # noqa: BLE001
        p = str(ws or "").replace("\\", "/").lower()
    if not p or p in (".", ""):
        return ""
    return hashlib.sha1(p.encode("utf-8")).hexdigest()[:16]


def ensure_table() -> None:
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE}("
        " project_id TEXT PRIMARY KEY,"
        " repo TEXT DEFAULT '',"
        " total_cny REAL DEFAULT 0,"
        " calls INTEGER DEFAULT 0,"
        " runs INTEGER DEFAULT 0,"
        " seeded_cny REAL DEFAULT 0,"
        " note TEXT DEFAULT '',"
        " created_at TEXT DEFAULT '',"
        " updated_at TEXT DEFAULT '')")


def _row(ws) -> dict | None:
    pid = project_id_of(ws)
    if not pid:
        return None
    ensure_table()
    rows = db.query(f"SELECT * FROM {TABLE} WHERE project_id=?", (pid,))
    return rows[0] if rows else None


def _detail_sum(ws) -> tuple[float, int]:
    """明细里**可归因到本项目**的合计与条数（证据，唯一事实来源）。"""
    pid = project_id_of(ws)
    if not pid:
        return 0.0, 0
    try:
        rows = db.query(
            "SELECT COALESCE(SUM(cost_cny),0) AS s, COUNT(*) AS n FROM remote_usage"
            " WHERE project_id=?", (pid,))
        r = rows[0] if rows else {}
        return float(r.get("s") or 0.0), int(r.get("n") or 0)
    except Exception:                                    # noqa: BLE001
        return 0.0, 0


def total(ws) -> float | None:
    """本项目累计花费（¥）= **人工补账 + 明细可归因合计**。

    **派生值，不做增量累加**（2026-11-10 20:45 定稿）：
      第一版是"每笔调用 `add(+cost)` 累加 + 定期 SET 对账"，两者并存会重复计数
      （实测对账刚把某笔算进去，紧接着那笔的 `add` 又加了一遍，多算 ¥0.0745）。
      改成纯派生后：账本永远等于"证据算出来的数"，不存在第二份可以走样的状态，
      也不需要任何对账逻辑。

    `None` = 账本里还没有这个项目（未迁移 / 首次运行）。
    """
    r = _row(ws)
    if not r:
        return None
    seeded = max(0.0, float(r.get("seeded_cny") or 0.0))
    s, _n = _detail_sum(ws)
    return round(seeded + s, 6)


def calls(ws) -> int:
    """本项目累计调用次数（同样派生自明细）。"""
    _s, n = _detail_sum(ws)
    return n


def add(ws, cost_cny: float, calls: int = 1) -> float:
    """**已废弃**：不再累加。保留签名只为兼容旧调用点（现在只返回派生值）。

    为什么废弃：增量累加 + 定期对账会重复计数（见 `total` 的说明）。
    记账这件事现在完全由"明细行带 project_id"承担 —— 写一行明细就是记一笔账。
    """
    return total(ws) or 0.0


def seed(ws, total_cny: float, note: str = "", count_run: bool = True) -> float:
    """**人工补账**：把迁移前已经花掉的钱一次性写进 `seeded_cny`（幂等、只升不降）。

    只升不降是因为：改小 = 少算 = 停机线失效 = 超支。
    """
    pid = project_id_of(ws)
    if not pid:
        return 0.0
    ensure_table()
    r = _row(ws)
    want = max(0.0, float(total_cny or 0.0))
    repo = ""
    try:
        repo = str(Path(ws).resolve())
    except Exception:                                    # noqa: BLE001
        repo = str(ws or "")
    now = db.now()
    if r is None:
        db.execute(
            f"INSERT INTO {TABLE}(project_id, repo, total_cny, calls, runs, seeded_cny,"
            " note, created_at, updated_at) VALUES(?,?,?,0,?,?,?,?,?)",
            (pid, repo, round(want, 6), 1 if count_run else 0, round(want, 6),
             note[:200], now, now))
        return total(ws) or want
    cur_seeded = max(0.0, float(r.get("seeded_cny") or 0.0))
    if want > cur_seeded:
        db.execute(
            f"UPDATE {TABLE} SET seeded_cny=?, total_cny=?, note=?, updated_at=?"
            " WHERE project_id=?",
            (round(want, 6), total(ws) or want, note[:200], now, pid))
    elif count_run:
        db.execute(f"UPDATE {TABLE} SET runs=runs+1, updated_at=? WHERE project_id=?",
                   (now, pid))
    return total(ws) or 0.0


def bump_to(ws, value: float, note: str = "") -> float:
    """对账：人工补账至少是 `value`（只升不降）。别名到 `seed`，语义统一。"""
    return seed(ws, value, note or "对账：按旧计数器补齐", count_run=False)


def reconcile_from_details(ws) -> float | None:
    """**保留接口，已无实际作用**：账本本来就是从明细派生出来的，随时等于重算结果。

    旧实现用 SET 写库，与增量累加并存时会重复计数（实测多算 ¥0.0745）。
    现在纯派生，调用它只是返回当前值（幂等、零副作用）。
    """
    return total(ws)


def report_lines(ws) -> list[str]:
    """交付报告用：把"这个项目花了多少、算不算得上账"写清楚。"""
    pid = project_id_of(ws)
    r = _row(ws)
    if not pid:
        return ["", "### 项目账本", "", "- 无法确定项目指纹（仓库路径为空），本次不记账"]
    if not r:
        return ["", "### 项目账本", "",
                f"- 项目 `{pid}` 账本还没有记录（首次运行 / 未迁移）"]
    return ["", "### 项目账本（一个项目一本账）", "",
            f"- 项目指纹：`{pid}`（产物仓路径 sha1）",
            f"- 本项目累计：**¥{total(ws) or 0:.4f}**"
            f"｜调用 {calls(ws)} 次｜跑过 {int(r.get('runs') or 0)} 次",
            f"- 其中人工补账 ¥{float(r.get('seeded_cny') or 0):.4f}"
            + (f"（{r.get('note')}）" if r.get("note") else ""),
            "- 口径：停机线只读这张表；**账本是派生值**（人工补账 + 明细可归因合计），"
            "随时等于明细重算结果，不存在第二份会走样的状态"]
