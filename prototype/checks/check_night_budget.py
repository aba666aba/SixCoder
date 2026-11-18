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

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（跨中断累计 / 按 goal 隔离 / 容错 / 主链路接入 / 唯一停机线=系统预算×1.05）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
