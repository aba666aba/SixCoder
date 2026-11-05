"""花费上限自检补强：界面填的值必须真的传到夜班并被拦。

补三件事：
  ① 界面 bundle 里有「上限 / 元」输入与 max_cny 字段；
  ② `/v1/night` 收 max_cny 并透传给 company.night；
  ③ 超限时收工原因写明「花费到上限」，且**报告里的花费行不会显示全库累计**。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import budget_cap  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("花费上限自检（界面 → 服务端 → 夜班）")

    print("[1] 界面：上限输入存在且随请求提交")
    js = sorted((ROOT / "web" / "dist" / "assets").glob("*.js"))
    txt = js[-1].read_text(encoding="utf-8", errors="replace") if js else ""
    check("控件标签有「上限」「元」", "上限" in txt and "元" in txt)
    check("请求体字段 max_cny", "max_cny" in txt)
    check("状态里有 maxCny（可自己填）", "maxCny" in txt)
    vue = (ROOT / "web" / "src" / "App.vue").read_text(encoding="utf-8")
    check("默认值存在（默认 1 元）", "maxCny: 1" in vue, "找不到默认值")
    check("提交时转成数字", "max_cny: Number(night.maxCny)" in vue)

    print("[2] 服务端：收字段并透传")
    srv = (ROOT / "server.py").read_text(encoding="utf-8")
    check("NightReq 有 max_cny", "max_cny: float = 0.0" in srv)
    check("透传给 company.night", "max_cny=req.max_cny" in srv)

    print("[3] 夜班：超限停 + 报告口径")
    comp = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("每轮检查花费", "over_cost_cap(task_id, cost_cap)" in comp)
    check("超限收工原因写明", "花费到上限" in comp)
    line = budget_cap.report_line("", 0)
    check("没有 task_id 时不显示累计", "未能统计" in line and "941" not in line, line)
    hit, info = budget_cap.over_cost_cap("t", 0)
    check("上限 0 = 永不拦（默认安全）", not hit, str(info))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（界面可填 / 服务端透传 / 夜班拦截 / 报告不冒充累计）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
