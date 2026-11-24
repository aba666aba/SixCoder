"""夜班实际花费账本跨中断累计自检（2026-11-07 中断续跑验证时发现的 bug）。

历史事故：`night_auto` 里 `spent = 0.0` 是进程内变量，夜班被中断、同命令续跑时
spent 归零 → 跨多次续跑累计花费对不上。实测证据：中断前 slice_done 报
spent_cny=0.6683，续跑后 slice_start 的 budget_cny 又回到首发值 1.1538。

修复：把累计花费落盘到 .sixcoder/night_spent.json，按 goal 隔离（同一需求续跑累计，
换需求才是新的一届），每次切片/片内自验花完钱都写回。

2026-11-10 预算重设计后本 check 的新职责：
  · 账本仍管「记录实际花费」（报告总消费用），但**不再参与任何停机判断**；
  · 唯一停机线 = 系统总预算 ×1.05（.sixcoder/budget.json），已钉进全自主片循环。

跑法: python checks/check_night_budget.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import auto  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("夜班花费硬上限跨中断累计自检")
    ws = Path(tempfile.mkdtemp(prefix="sixcoder-budget-"))
    goal = "毕业设计双向选题与智能匹配系统（3 万行）"
    other = "另一个完全不同的需求"

    print("[1] 账本读写：首次 / 保存 / 续跑读回")
    check("全新仓库首次读 0", auto._load_spent(ws, goal) == 0.0)
    auto._save_spent(ws, goal, 12.5)
    check("保存 12.5 后读回 12.5", abs(auto._load_spent(ws, goal) - 12.5) < 1e-6,
          str(auto._load_spent(ws, goal)))
    d = json.loads((ws / auto.SPENT_FILE).read_text(encoding="utf-8"))
    check("账本落盘在 .sixcoder 下", (ws / auto.SPENT_FILE).exists()
          and str(auto.SPENT_FILE).startswith(".sixcoder/"), str(d))
    check("账本按 goal 指纹隔离", d.get("goal") == auto._spent_key(goal))
    check("换了需求读 0（新一届夜班）", auto._load_spent(ws, other) == 0.0)

    print("[2] 累加与容错")
    auto._save_spent(ws, goal, 15.75)
    check("同一需求续跑读到累计值", abs(auto._load_spent(ws, goal) - 15.75) < 1e-6)
    (ws / auto.SPENT_FILE).write_text("{坏掉的 json", encoding="utf-8")
    check("账本损坏不炸、回落 0", auto._load_spent(ws, goal) == 0.0)
    auto._save_spent(ws, goal, -3.0)
    check("负数不写坏账本（读回 0）", auto._load_spent(ws, goal) == 0.0)

    print("[3] 主链路真的用上了账本（静态钉住接入点）")
    src = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("spent 初始化改为读账本", "spent = _load_spent(ws, goal)" in src)
    check("不再是裸的 spent = 0.0", "\n    spent = 0.0\n" not in src)
    check("切片花费后写回账本", src.count("_save_spent(ws, goal, spent)") >= 2,
          f"count={src.count('_save_spent(ws, goal, spent)')}")
    # 2026-11-10 预算重设计：用户上限（budget_cny）停机判断已删除——账本只管"记录实际花费"，
    # 停机只剩系统总预算一条线。这里反向钉死，防止旧机制被人加回来。
    check("旧的按钱停机判断已移除", "if budget_cny and spent >= budget_cny:" not in src)
    check("旧的剩余额度扣减已移除", "left_now = max(0.0, budget_cny - spent)" not in src)
    check("续跑时有可观测事件", "budget_resumed" in src)

    print("[4] 唯一停机线已接进全自主模式（系统总预算 ×1.05，用户不可设上限）")
    check("片循环里调用 system_budget_stopped",
          "if _be.system_budget_stopped(ws, \"\"):" in src)
    check("停机时有可观测事件 system_budget_stop", "system_budget_stop" in src)
    import inspect as _insp
    _params = set(_insp.signature(auto.night_auto).parameters)
    check("night_auto 不再收 budget_cny / hours / slice_hours 参数",
          not ({"budget_cny", "hours", "slice_hours"} & _params), str(sorted(_params)))
    # 端到端语义：账本累计值只用于报告实际花费，不再参与任何停机判断
    auto._save_spent(ws, goal, 58.0)
    check("账本累计仍可跨续跑读回（报总消费用）",
          abs(auto._load_spent(ws, goal) - 58.0) < 1e-6)

    print("[5] 夜班开局：项目账本真值同步回镜像（2026-11-19 卡死误导修复）")
    from core import project_ledger as _pl
    import tempfile as _tf
    # 新仓库，账本 seed 一个真值，镜像用另一个 goal 且卡在旧值
    ws5 = Path(_tf.mkdtemp(prefix="sixcoder-syncspent-"))
    g5a, g5b = "第一版需求", "拼了契约文本的第二版需求（goal 漂移）"
    try:
        _pl.seed(ws5, 123.9, "test", count_run=False)
    except Exception:
        pass
    auto._save_spent(ws5, g5a, 50.92)          # 镜像卡在旧值
    check("同步前：漂移的 goal 读到 0（卡死根因复现）", auto._load_spent(ws5, g5b) == 0.0)
    got = auto.sync_spent_from_ledger(ws5, g5b)
    check("同步后：漂移 goal 读到账本真值", abs(got - 123.9) < 1e-6, str(got))
    check("同步后：镜像文件确实写回真值",
          abs(auto._load_spent(ws5, g5b) - 123.9) < 1e-6)
    # 只升不降：真值比镜像小时不回退
    auto._save_spent(ws5, g5b, 200.0)
    got2 = auto.sync_spent_from_ledger(ws5, g5b)
    check("只升不降：镜像高于真值时不回退", abs(got2 - 200.0) < 1e-6, str(got2))
    # 真值取不到（无账本行）时不动镜像
    ws6 = Path(_tf.mkdtemp(prefix="sixcoder-syncspent-empty-"))
    auto._save_spent(ws6, g5a, 33.3)
    got3 = auto.sync_spent_from_ledger(ws6, g5a)
    check("账本无记录时保留镜像原值（不误清）", abs(got3 - 33.3) < 1e-6, str(got3))
    # 接线：company 开局真的调用了同步（AST 钉调用路径）
    csrc = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("company 开局接线 sync_spent_from_ledger",
          "sync_spent_from_ledger(Path(repo), requirement)" in csrc)

    print("[6] 阶梯停机（2026-11-19 用户口径：**以总预算为基数**，每档 +20% 等差）")
    import json as _json
    import tempfile as _tf
    from core import budget_estimate as _be
    _w = Path(_tf.mkdtemp(prefix="budget_step_"))
    (_w / ".sixcoder").mkdir()
    (_w / ".sixcoder" / "budget.json").write_text(
        _json.dumps({"system_budget_cny": 100.0}), encoding="utf-8")
    check("第 1 档阈值 = cap×1.05（105）",
          abs(_be.stop_threshold(_w) - 105.0) < 0.01,
          str(_be.stop_threshold(_w)))
    # 账本 126.93 超过第 2 档 126 → 升档要一步到"能跑"（147），否则重启即停
    _lvl = _be.bump_stop_level(_w, spent=126.93)
    check("跨档时升到能跑的档位（level=2）", _lvl == 2, str(_lvl))
    check("升档后阈值 = 105×(1+0.2×2) = 147（等差）",
          abs(_be.stop_threshold(_w) - 147.0) < 0.01,
          str(_be.stop_threshold(_w)))
    check("升档后 spent 低于阈值（重启能跑）", 126.93 < _be.stop_threshold(_w))
    # 小花费只升一档
    (_w / ".sixcoder" / "budget.json").write_text(
        _json.dumps({"system_budget_cny": 100.0, "stop_level": 0}), encoding="utf-8")
    _lvl2 = _be.bump_stop_level(_w, spent=110)
    check("小花费只升一档（level=1，阈值126）",
          _lvl2 == 1 and abs(_be.stop_threshold(_w) - 126.0) < 0.01,
          "%s/%s" % (_lvl2, _be.stop_threshold(_w)))
    check("展示快照带的阈值随档位走",
          _be.budget_snapshot(_w)["stop_threshold_cny"] == round(_be.stop_threshold(_w), 2),
          str(_be.budget_snapshot(_w).get("stop_threshold_cny")))
    # 接线：company 预算闸触发时升档，余额闸不升
    _csrc2 = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("company 预算闸触发时调 bump_stop_level",
          "bump_stop_level(ws)" in _csrc2)
    check("余额耗尽不升档（_by_balance 守卫）", "_by_balance" in _csrc2)
    import shutil as _sh2
    _sh2.rmtree(_w, ignore_errors=True)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（跨中断累计 / 按 goal 隔离 / 容错 / 主链路接入 / "
          "阶梯停机：以总预算105为基数、每档+20%等差(105/126/147/168)、停一次能重启）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
