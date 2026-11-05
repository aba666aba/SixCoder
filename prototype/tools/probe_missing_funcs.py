"""探针：契约函数在文件里"整段消失"时，引擎会不会误判"已完成"。

背景：冒烟里假远端把模块写成了 `def placeholder(): return 1`（契约函数全没了），
结果夜班第 2 轮就收工，理由是"契约内函数已全部通过机械验收（没有可执行的工作单）"，
但验收数是 0/8。这是**循环层的假绿风险**：函数被删/改名 ≠ 已通过。

这个探针只做一件事：把这种情况喂给 iterate.inspect，看它报什么。
"""
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import contract as cm, iterate  # noqa: E402

CONTRACT = (ROOT.parent / "projects" / "ab_contract.txt").read_text(encoding="utf-8")
contracts = cm.parse_requirement(CONTRACT)

root = ROOT / ".tmp"
root.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(dir=str(root)) as td:
    ws = Path(td)
    # 三个模块都只留一个占位函数（契约函数全部消失）
    for name in ("clean.py", "store.py", "report.py"):
        (ws / name).write_text("def placeholder():\n    return 1\n", encoding="utf-8")
    state = iterate.inspect(ws, contracts)
    print("契约函数总数:", sum(len(c.funcs) for c in contracts.values()))
    total_verified = total_funcs = 0
    for path, st in state.items():
        print(f"\n{path}: 存在={st.get('exists')} 符号={sorted(st.get('symbols') or [])}")
        print(f"   funcs={st.get('funcs')} verified={st.get('verified')} "
              f"stubs={st.get('stubs')} hard_failed={st.get('hard_failed')}")
        print(f"   untested={st.get('untested')}")
        total_verified += int(st.get("verified") or 0)
        total_funcs += int(st.get("funcs") or 0)
    print(f"\n合计：验收 {total_verified}/{total_funcs}")

    from core import company
    pending = [p for p in contracts if company.needs_work_state(state, p)]
    print(f"\n夜班会认为还需要干活的模块: {pending}")
    print("→ 如果这里是空的（而验收是 0），就是**循环层假绿**：函数消失被当成已完成。")
