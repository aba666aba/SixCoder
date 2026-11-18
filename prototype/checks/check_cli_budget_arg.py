"""「用户不可设任何花费/时间上限」反向自检（2026-11-10 预算机制重设计）。

定稿口径（用户拍板）：关于花费系统只有「三件事 + 一条线」——
  ① 开局估算（提需求 → 估算多少钱 → 用户充值）；
  ② 记录实际花费；③ 结束报总消费；
  唯一停机线 = 总花费 ≥ 系统总预算 ×1.05（写在 .sixcoder/budget.json，用户不可改）。

用户**不能**再通过任何入口设上限：`--max-cny` / `--hours` / `--slice-hours`、
server 的 `max_cny`/`hours` 字段、界面的「上限」输入框，全部删除。
本 check 反向钉死：任何人把这些入口加回来，自检立刻红。

历史背景：2026-11-09 曾有 `--hours 0` 被 `args.hours or default` 吃掉的回归
（0 是假值 → 用户以为不限时间实际只跑半小时）。该参数整个删除后此类回归不可能再发生。

跑法: python checks/check_cli_budget_arg.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _code_only(text: str) -> str:
    """去掉以 # 或 <!-- --> 注释行，避免把文档里的「已移除」当成还在用。"""
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def main() -> int:
    print("[1] CLI：不再有 --max-cny / --hours / --slice-hours 参数与透传")
    cli = _code_only((ROOT / "cli.py").read_text(encoding="utf-8"))
    for arg in ('"--max-cny"', '"--hours"', '"--slice-hours"'):
        check(f"cli.py 没有 {arg} 参数定义", arg not in cli)
    for kw in ("max_cny=", "hours=", "slice_hours=", "args.max_cny", "args.hours"):
        check(f"cli.py 没有 {kw} 透传", kw not in cli)

    print("[2] 引擎：company.night / night_auto 签名不再收上限参数")
    comp = _code_only((ROOT / "core" / "company.py").read_text(encoding="utf-8"))
    auto = _code_only((ROOT / "core" / "auto.py").read_text(encoding="utf-8"))
    check("company.py 没有 _budget_seconds（时间上限机制已删）",
          "_budget_seconds" not in comp)
    check("company.night 签名没有 max_cny / hours",
          "max_cny" not in comp and "hours: float" not in comp)
    check("night_auto 签名没有 budget_cny / hours / slice_hours",
          all(k not in auto for k in ("budget_cny: float", "hours: float = 0.0,",
                                      "slice_hours")))

    print("[3] server：NightReq 不再有 max_cny / hours 字段与透传")
    srv = _code_only((ROOT / "server.py").read_text(encoding="utf-8"))
    for kw in ("max_cny", "hours:", "req.hours", "req.max_cny"):
        check(f"server.py 没有 {kw}", kw not in srv)

    print("[4] 界面：不再有「上限/预算小时」输入与上传字段")
    ui = (ROOT / "web" / "src" / "App.vue").read_text(encoding="utf-8")
    for kw in ("maxCny", "max_cny", "night.hours", "hours: Number"):
        check(f"App.vue 没有 {kw}", kw not in ui)

    print("[5] 唯一停机线确实存在（正向钉住，防止拆过头）")
    be = (ROOT / "core" / "budget_estimate.py").read_text(encoding="utf-8")
    check("budget_estimate 有 system_budget_stopped（唯一停机判定）",
          "def system_budget_stopped(" in be)
    check("停机阈值是系统预算 ×1.05", "OVER_CAP_RATIO = 1.05" in be)
    check("company.py 轮循环接了系统预算停机",
          "system_budget_stopped(ws, task_id)" in comp)
    check("auto.py 片循环接了系统预算停机",
          "system_budget_stopped(ws, \"\")" in auto)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（用户不可设上限：CLI/引擎/服务端/界面四处入口全删；唯一停机线=系统预算×1.05 已接进两种模式）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
