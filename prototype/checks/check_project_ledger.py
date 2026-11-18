"""项目账本：**一个项目一本账，账本是从明细派生的**（2026-11-10 定稿）。

两条铁律：
  1. 本项目花费 = **人工补账 + 明细可归因合计**（派生值，不累加）。
     为什么不做增量累加：累加 + 定期对账并存会**重复计数**（实测多算 ¥0.0745）。
  2. 停机线只读本项目账本，**绝不读全量历史账本**（18:05 事故：项目花 ¥31 被判 ¥110 超额）。

事故背景：全自主夜班 `task_id` 传空，`usage_summary("")` 是**所有项目 + 所有实验**的合计；
一旦历史累计跨过停机线，夜班第一轮就"超额 5%"收工，一轮活都没干。
"""
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import budget_estimate as BE                          # noqa: E402
from core import db                                             # noqa: E402
from core import project_ledger as PL                           # noqa: E402
from core import remote as RM                                   # noqa: E402

FAILS = []
_MADE: list[str] = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


def _ws(tag: str) -> Path:
    p = Path(tempfile.mkdtemp(prefix=f"sixcoder-ledger-{tag}-")).resolve()
    (p / ".sixcoder").mkdir(parents=True, exist_ok=True)
    _MADE.append(PL.project_id_of(p))
    return p


def _spend(ws: Path, cost: float) -> None:
    """模拟一笔远端调用：往明细插一行（带本项目指纹）—— 这就是"记账"。"""
    db.execute(
        "INSERT INTO remote_usage(task_id, model, cost_cny, project_id, created_at)"
        " VALUES(?,?,?,?,?)", ("__chk", "m", cost, PL.project_id_of(ws), db.now()))


def main() -> int:
    a, b = _ws("A"), _ws("B")

    print("[1] 两个项目交错记账，互不污染（账本 = 人工补账 + 明细合计）")
    PL.seed(a, 10.0, "补账 A")
    PL.seed(b, 20.0, "补账 B")
    _spend(a, 1.5)
    _spend(b, 2.0)
    _spend(a, 0.25)
    check("项目 A = 10 + 1.75 = 11.75", abs((PL.total(a) or 0) - 11.75) < 1e-9, str(PL.total(a)))
    check("项目 B = 20 + 2.00 = 22.00", abs((PL.total(b) or 0) - 22.00) < 1e-9, str(PL.total(b)))
    check("两个项目指纹不同", PL.project_id_of(a) != PL.project_id_of(b))
    check("调用次数也是派生的（A 有 2 条明细）", PL.calls(a) == 2, str(PL.calls(a)))

    print("[2] 账本是派生值：重复读不改数，明细变了才变")
    v1, v2 = PL.total(a), PL.total(a)
    check("重复读取结果一致（无副作用）", abs(v1 - v2) < 1e-12, f"{v1} / {v2}")
    check("reconcile 是幂等的空操作（纯派生，不再写库）",
          abs((PL.reconcile_from_details(a) or 0) - v1) < 1e-12, str(PL.reconcile_from_details(a)))
    check("add() 已废弃：不再累加，只返回当前值",
          abs(PL.add(a, 999.0) - v1) < 1e-12, str(PL.add(a, 999.0)))
    check("恶意 add 没有污染账本", abs((PL.total(a) or 0) - v1) < 1e-9, str(PL.total(a)))

    print("[3] 人工补账只升不降（改小 = 少算 = 停机线失效 = 超支）")
    PL.seed(a, 5.0, "补一个更小的数")
    check("补账改小无效（还是 10）", abs((PL.total(a) or 0) - 11.75) < 1e-9, str(PL.total(a)))
    PL.seed(a, 40.0, "补账改大")
    check("补账改大生效（40 + 1.75 = 41.75）", abs((PL.total(a) or 0) - 41.75) < 1e-9,
          str(PL.total(a)))

    print("[4] 停机线读本项目账本，不读全量历史账本")
    (b / ".sixcoder" / "budget.json").write_text(
        '{"system_budget_cny": 30.0}', encoding="utf-8")        # 停机线 = 31.5
    _orig = RM.usage_summary
    try:
        RM.usage_summary = lambda task_id="": {"cost_cny": 110.35}   # 全量历史账本很大
        check("全量 ¥110 / 本项目 ¥22 → 不停机", BE.system_budget_stopped(b, "") is False)
        check("project_spent_cny 返回本项目 ¥22",
              abs(BE.project_spent_cny(b) - 22.0) < 1e-9, str(BE.project_spent_cny(b)))
        _spend(b, 10.0)                                          # 本项目 → 32 ≥ 31.5
        check("本项目 ¥32 ≥ 停机线 ¥31.5 → 停机", BE.system_budget_stopped(b, "") is True)
    finally:
        RM.usage_summary = _orig

    print("[5] 旧计数器兼容：账本还没记录时退回 night_spent.json")
    c = _ws("C")
    (c / ".sixcoder" / "night_spent.json").write_text(
        '{"goal":"g","total_cny":31.0083}', encoding="utf-8")
    check("账本无记录 → 退回计数器 ¥31.0083",
          abs(BE.project_spent_cny(c) - 31.0083) < 1e-9, str(BE.project_spent_cny(c)))
    check("读函数没有偷偷建账（不污染真账本）", PL.total(c) is None, str(PL.total(c)))
    PL.seed(c, BE.project_spent_cny(c), "显式补账")
    check("显式补账后账本 = 31.0083", abs((PL.total(c) or 0) - 31.0083) < 1e-9, str(PL.total(c)))

    print("[6] 明细行必须带项目指纹（列有了但写入不带值 = 白加，踩过）")
    RM._record(task_id="__chk_ws", node_id="n", purpose="__chk", model="", workspace=str(a))
    rows = db.query("SELECT project_id FROM remote_usage WHERE task_id='__chk_ws'")
    check("有 workspace → 明细行带 project_id",
          bool(rows) and rows[0].get("project_id") == PL.project_id_of(a), str(rows[:1]))
    RM._record(task_id="__chk_none", node_id="n", purpose="__chk", model="")
    rows2 = db.query("SELECT project_id FROM remote_usage WHERE task_id='__chk_none'")
    check("无 workspace 也无 current → 留空（不瞎归账）",
          bool(rows2) and (rows2[0].get("project_id") or "") == "", str(rows2[:1]))
    PL.set_current(a)
    try:
        RM._record(task_id="__chk_cur", node_id="n", purpose="__chk", model="")
        rows3 = db.query("SELECT project_id FROM remote_usage WHERE task_id='__chk_cur'")
        check("不传 workspace 但设了 current → 也能归账",
              bool(rows3) and rows3[0].get("project_id") == PL.project_id_of(a), str(rows3[:1]))
    finally:
        PL.set_current("")

    print("[7] 源码钉子")
    root = Path(__file__).resolve().parents[1]
    src = (root / "core" / "remote.py").read_text(encoding="utf-8")
    check("_record_locked 写入 project_id",
          "project_id)" in src and "kw.get(\"workspace\")" in src)
    _ask_seg = src.split("def ask(")[1].split("\ndef ")[0]
    check("ask() 里不引用不存在的 workspace 变量（曾差点 NameError）",
          "workspace" not in _ask_seg)
    check("不再有增量累加（_pladd.add 已移除）", "_pladd.add" not in src)
    bs = (root / "core" / "budget_estimate.py").read_text(encoding="utf-8")
    check("停机线走 project_ledger", "from . import project_ledger as _pl" in bs)
    au = (root / "core" / "auto.py").read_text(encoding="utf-8")
    check("夜班入口设置了「当前项目」", "_pl_cur.set_current(ws)" in au)
    pl = (root / "core" / "project_ledger.py").read_text(encoding="utf-8")
    check("账本是派生值（total = seeded + 明细合计）",
          "_detail_sum(ws)" in pl and "seeded + s" in pl)

    # 清理测试数据（账本行 + 明细行），不污染真实库
    for pid in _MADE:
        if pid:
            try:
                db.execute(f"DELETE FROM {PL.TABLE} WHERE project_id=?", (pid,))
                db.execute("DELETE FROM remote_usage WHERE project_id=?", (pid,))
            except Exception:                                  # noqa: BLE001
                pass
    try:
        db.execute("DELETE FROM remote_usage WHERE task_id LIKE '__chk%'")
    except Exception:                                          # noqa: BLE001
        pass
    for p in (a, b, c):
        shutil.rmtree(p, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
