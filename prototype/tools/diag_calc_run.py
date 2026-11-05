"""诊断：为什么这次计算器夜班 6 轮、12 秒、0 次写作调用？

现象（_logs/night-night-1793812392.json）：
  · 6 轮、11.9 秒、验收 0/5、calc.py 5 个函数全是桩
  · 远端只花了 ¥0.0045（2 次调用）——说明**根本没有进入"写代码"这条路径**
  · 收工原因是振荡检测（回到第 1 轮的状态）

本脚本按夜班的分支条件逐条打印判定依据，看是哪一条把写作路径挡掉了。
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company, contract as cm, iterate, policy, remote  # noqa: E402

WS = Path("D:/Download/cal")
print(f"工作区：{WS}")
print(f"远端可用：{remote.available()}")
pol = policy.current()
print(f"模式：{pol.name}｜code_author={getattr(pol, 'code_author', '?')}"
      f"｜code_escalation={getattr(pol, 'code_escalation', '?')}")

contract_file = WS / ".sixcoder" / "contract.txt"
if contract_file.exists():
    text = contract_file.read_text(encoding="utf-8", errors="replace")
    print(f"\n锁定的契约（{len(text)} 字符）前 200：\n{text[:200]}")
    contracts = cm.parse_requirement(text)
else:
    contracts = cm.parse_requirement("")
    print("\n没有 .sixcoder/contract.txt")

print(f"\n解析出 {len(contracts)} 个模块：{list(contracts)}")
state = iterate.inspect(WS, contracts) if contracts else {}
for p, st in state.items():
    print(f"  {p}: funcs={st.get('funcs')} verified={st.get('verified')} "
          f"stubs={sorted(st.get('stubs') or [])} missing={st.get('missing')} "
          f"hard_failed={st.get('hard_failed')} untested={st.get('untested')}")
    print(f"    → needs_work_state = {company.needs_work_state(state, p)}")

# 夜班进入写作路径的三个前置条件
print("\n=== 写作路径的前置条件 ===")
use_remote = True
print(f"  use_remote = {use_remote}")
print(f"  _remote_available() = {company._remote_available()}")
print(f"  pol.code_author in ('remote','ladder') = "
      f"{getattr(pol, 'code_author', '') in ('remote', 'ladder')}")

# hours 预算是否把 budget_left 压到 20 秒以下
print("\n=== 如果走 _author_round，它会怎么判 ===")
if not contracts:
    print("  没有契约 → 直接返回空（这就是 0 次写作调用的原因之一）")
else:
    mods = [p for p in sorted(contracts) if company.needs_work_state(state, p)]
    print(f"  需要干活的模块：{mods}")
