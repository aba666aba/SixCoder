"""三档模式的**协作方式**自检（不是权限自检，权限见 check_modes.py）。

用户把两档的上限说清楚了：
    甲方模式 = 写代码的完全体，上限是远端 API 的推理能力 → 任何决策由远端拍板；
               本地只是便宜工人，一旦被硬判据判红就立刻由远端接管写代码。
    开发者模式 = 工具，上限是开发者本人的架构能力 → 实现方案由人给；
               系统不许替人做架构决策（没有方案就抛 needhuman），远端写代码要人批准。
    只读模式 = 不写文件，连夜班都不能跑。

这里把这些差别钉成可执行断言，避免以后为了"跑得动"又悄悄退回"两档一样"。

跑法: python check_policy.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import company, permissions, policy  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("三档模式的协作方式自检")
    orig = permissions.current_mode().id
    try:
        # ---- 甲方模式：远端是最终裁判，代码默认由远端写
        permissions.set_mode("client")
        pol = policy.current()
        print("[1] 甲方模式 = 完全体（远端拍板 + 远端写代码）")
        check("裁判是远端", pol.arbiter == "remote")
        check("实现方案由系统内部定", pol.plan_authority == "remote")
        check("代码由远端整文件写（旧逐符号路径已废弃）", pol.code_author == "remote")
        check("默认不额外花远端调用出方案（省钱）", pol.plan_first is False)
        check("方案从最低档开始（便宜）", pol.thinking_plan == "off")
        check("本地判红立即改由远端重做", pol.code_escalation == policy.FIRST_RED)
        check("次数不设上限，由时间预算兜", pol.remote_code_budget == 0)
        go, why = policy.should_escalate(pol, n_fail=1, hard_evidence=True, used=0)
        check("第一次硬判据红就升级（不等第二次）", go, why)
        go2, why2 = policy.should_escalate(pol, n_fail=1, hard_evidence=False, used=0)
        check("软失败（没吐 JSON 之类）先本地重试", not go2, why2)
        go3, why3 = policy.should_escalate(pol, n_fail=5, hard_evidence=True, used=9999)
        check("不设上限时不会因为次数被拦", go3, why3)

        # ---- 开发者模式：人是上限，系统不许替他定架构
        permissions.set_mode("developer")
        pol = policy.for_mode("developer")
        print("[2] 开发者模式 = 工具（人拍板，本地照方案写）")
        check("裁判是人", pol.arbiter == "human")
        check("实现方案必须人给", pol.plan_authority == "human")
        check("代码由本地写（远端要批准才用）", not pol.remote_writes_code)
        check("不自动开局定方案", not pol.plan_first)
        check("远端写代码需批准（不是 first-red）", pol.code_escalation == policy.ON_APPROVAL)
        go4, why4 = policy.should_escalate(pol, n_fail=1, hard_evidence=True, used=0)
        check("硬判据红也不自动接管（等人批准）", not go4, why4)
        go5, _ = policy.should_escalate(pol, n_fail=2, hard_evidence=True, used=0)
        check("连续失败 2 次才提请远端（且要批准）", go5)

        # ---- 只读模式：连夜班都不许跑
        permissions.set_mode("readonly")
        pol = policy.for_mode("readonly")
        print("[3] 只读模式 = 不写文件")
        check("不升级代码", pol.code_escalation == policy.OFF)
        ws = Path(tempfile.mkdtemp())
        res = company.night(ws, "给 calc.py 加 divide", "qwen2.5:7b", hours=0.01)
        check("夜班被直接拒绝且说明原因", not res.ok and "只读" in (res.reason or ""), res.reason)
        check("拒绝时不写交付物", not (ws / "DELIVERY.md").exists())

        # ---- 方案注入：只在该生效的地方生效
        print("[4] 实现方案（plan）的权威性")
        blk_d = policy.plan_block("clean.py 只做清洗；store.py 用 sqlite3", policy.for_mode("developer"))
        check("开发者方案写明'只能照它做'", "只能照它做" in blk_d, blk_d[:60])
        check("开发者方案没写到的细节不许猜", "不要猜" in blk_d)
        blk_c = policy.plan_block("甲方给的方案", policy.for_mode("client"))
        check("甲方方案里细节由系统自行决定", "自行决定" in blk_c, blk_c[:80])
        check("空方案不产生任何提示词块", policy.plan_block("") == "")

        # ---- 自然语言需求兜底（界面里甲方不会按格式写）
        print("[5] 夜班的契约兜底（只认写全的签名，认不出就拒绝）")
        got = company._single_module_fallback(
            "给 calc.py 增加 divide(a: int, b: int) -> float：返回 a/b；b 为 0 时抛 ValueError。")
        check("写全签名 → 能兜出单模块契约",
              bool(got) and "calc.py" in got
              and [f.name for f in got["calc.py"].funcs] == ["divide"], str(list(got)))
        check("没写类型 → 拒绝（不瞎猜判据）",
              company._single_module_fallback("给 calc.py 增加 divide(a, b)") == {})
        check("没有文件名 → 拒绝", company._single_module_fallback("随便写个函数") == {})
    finally:
        permissions.set_mode(orig)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（三档协作方式互不相同，且都有可执行断言）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
