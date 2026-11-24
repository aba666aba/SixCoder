"""预算闸门自检：只保留"总的停机线"，且现在**断开**（2026-11-09 用户指令）。

用户指令原文：
  · "总预算是不是暂时也束缚了能力，先断开，后期调整好再接上"
  · "改成只有总的停机线而且先断开，用户自行控制成本，
     等我改好这个机制之后再连接进系统"

因此系统里必须**只剩**一条预算闸门（总停机线），其余片级/轮级限额全部失效：
  · --max-cny 0  → 不限花费（全局 + 每片 + 蓝图降级 + funcs_cap 都不受限）
  · --hours 0    → 不限时间（全局 + 每片自愈时限都不受限）
  · 看门狗 BUDGET_STOP=0 → 不按钱停机

跑法: python checks/check_budget_switch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import auto, budget_cap, company, project  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 显式 0 = 不限（不回退环境变量，否则断不开）")
    check("resolve_cap(0) == 0（不限花费）", budget_cap.resolve_cap(0) == 0.0,
          str(budget_cap.resolve_cap(0)))
    check("resolve_cap(60) == 60（显式上限仍然生效）", budget_cap.resolve_cap(60) == 60.0)
    check("resolve_cap(None) 才查环境变量（默认行为不变）",
          budget_cap.resolve_cap(None) == budget_cap._cap_from_env())

    print("[2] 时间预算：0 = 不限（原来 max(60, hours*3600) 把 0 当 60 秒）")
    check("_budget_seconds(0) 是不限", company._budget_seconds(0) == float("inf"),
          str(company._budget_seconds(0)))
    check("_budget_seconds(12) == 43200", company._budget_seconds(12) == 43200.0,
          str(company._budget_seconds(12)))
    check("_budget_seconds(0.5) == 1800", company._budget_seconds(0.5) == 1800.0)

    print("[3] 预算不够就缩范围的路径必须失效")
    fit = project.budget_fits({}, 0)
    check("budget_fits(max_cny=0) → 够（不缩范围）", bool(fit.get("ok")), str(fit.get("note")))
    check("slice_budget(total=0) → 0（每片无上限）", auto.slice_budget(0, 0, 3, 5) == 0.0,
          str(auto.slice_budget(0, 0, 3, 5)))
    src_auto = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("片级时间预算不再兜底（hours=0 → 不限）",
          "hours=hours or DEFAULT_SLICE_HOURS" not in src_auto,
          "仍用 DEFAULT_SLICE_HOURS 兜底 = 每片 30 分钟就停自愈")
    check("预算断开时蓝图清零（续跑也不留旧上限）",
          "bp.total_budget_cny = 0.0" in src_auto)

    print("[4] 看门狗：只剩总停机线，且已断开")
    w = (ROOT / "tools" / "watchdog_night.py").read_text(encoding="utf-8")
    check("MAX_CNY = 0（总花费停机线断开）", "MAX_CNY = 0.0" in w)
    check("HOURS = 0（总时长停机线断开）", "HOURS = 0.0" in w)
    check("BUDGET_STOP = 0（看门狗不按钱停机）", "BUDGET_STOP = 0.0" in w)
    check("启动参数用开关（不再写死 60/12）", '"--max-cny", str(MAX_CNY)' in w
          and '"--hours", str(HOURS)' in w, "仍写死预算")
    check("花钱停机判断受开关控制", "if BUDGET_STOP and spent_cny() >= BUDGET_STOP" in w)

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（只剩总停机线 / 已断开 / 片级限额全部失效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
