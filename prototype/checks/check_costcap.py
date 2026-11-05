"""¥ 硬上限自检：钱到顶必须真的停下来，且默认行为不能被改坏。

跑法: python check_costcap.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import budget_cap, remote  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("¥ 硬上限自检")
    orig = remote.usage_summary

    print("[1] 不设上限 = 永不拦（默认行为不变）")
    remote.usage_summary = lambda tid="": {"cost_cny": 99.0, "calls": 50,      # type: ignore
                                          "prompt_tokens": 1, "completion_tokens": 1}
    try:
        hit, info = budget_cap.over_cost_cap("t1", 0)
        check("cap=0 → 不拦", not hit, str(info))
        hit2, _ = budget_cap.over_cost_cap("t1", None)                        # type: ignore
        check("cap=None → 不拦", not hit2)
    finally:
        remote.usage_summary = orig                                           # type: ignore

    print("[2] 有上限时按真实花费判")
    for spent, cap, want in ((0.05, 0.1, False), (0.10, 0.1, True),
                             (0.30, 0.1, True), (0.0999, 0.1, False)):
        remote.usage_summary = (lambda s: (lambda tid="": {"cost_cny": s, "calls": 3,  # type: ignore
                                                          "prompt_tokens": 1,
                                                          "completion_tokens": 1}))(spent)
        try:
            hit, info = budget_cap.over_cost_cap("t2", cap)
        finally:
            remote.usage_summary = orig                                       # type: ignore
        check(f"花了 ¥{spent} / 上限 ¥{cap} → {'停' if want else '继续'}",
              hit is want, str(info))

    print("[3] 读不到花费时：**不误停**，但要如实标记")
    def boom(tid=""):                                                         # noqa: ANN001
        raise RuntimeError("db locked")
    remote.usage_summary = boom                                               # type: ignore
    try:
        hit, info = budget_cap.over_cost_cap("t3", 0.1)
        spent_info = budget_cap.cost_spent("t3")
    finally:
        remote.usage_summary = orig                                           # type: ignore
    check("读不到 → 不误停", not hit, str(info))
    check("读不到 → 明细里说明", "读不到" in info.get("note", ""), str(info))
    check("cost_spent 如实返回 ok=False", spent_info["ok"] is False)

    print("[4] 环境变量入口")
    import os
    os.environ["SIXCODER_MAX_CNY"] = "0.5"
    try:
        check("环境变量被读到", abs(budget_cap.resolve_cap() - 0.5) < 1e-9,
              str(budget_cap.resolve_cap()))
        check("显式参数优先于环境变量", abs(budget_cap.resolve_cap(2.0) - 2.0) < 1e-9)
        os.environ["SIXCODER_MAX_CNY"] = "not-a-number"
        check("非法环境变量 → 当作不设上限（不崩）", budget_cap.resolve_cap() == 0.0)
    finally:
        os.environ.pop("SIXCODER_MAX_CNY", None)

    print("[5] 报告行：有上限/没上限/读不到 三种都说清")
    remote.usage_summary = lambda tid="": {"cost_cny": 0.1234, "calls": 7,     # type: ignore
                                          "prompt_tokens": 1, "completion_tokens": 1}
    try:
        with_cap = budget_cap.report_line("t4", 0.1)
        no_cap = budget_cap.report_line("t4", 0)
    finally:
        remote.usage_summary = orig                                           # type: ignore
    check("有上限 → 写上限并标触顶", "上限" in with_cap and "触顶" in with_cap, with_cap)
    check("没上限 → 写未设上限", "未设上限" in no_cap, no_cap)
    check("金额格式正确（四位小数）", "0.1234" in with_cap, with_cap)

    print("[6] 调用链：夜班/CLI/服务端都接上了")
    proto = Path(__file__).resolve().parents[1]
    comp = (proto / "core" / "company.py").read_text(encoding="utf-8")
    check("夜班每轮检查花费", "over_cost_cap(task_id, cost_cap)" in comp)
    check("超限时收工原因写明", "花费到上限" in comp)
    check("交付报告有花费行", "report_line" in comp)
    cli = (proto / "cli.py").read_text(encoding="utf-8")
    check("CLI 有 --max-cny 并透传",
          '"--max-cny"' in cli and "max_cny=getattr(args" in cli, "cli 里没找到参数或透传")
    srv = (proto / "server.py").read_text(encoding="utf-8")
    check("服务端接受 max_cny 并透传", "max_cny: float = 0.0" in srv and "max_cny=req.max_cny" in srv)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（默认不拦 / 到顶即停 / 读不到不误停 / 环境变量 / 报告如实）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
