"""只重写交付报告（不干活）：按当前工作区的机械体检结果生成 DELIVERY.md + TRACE.md。

用法：python tools/night_report.py <projects 下的目录名> [需求文件]
判据改了之后（例如探针类型修正），用它把报告刷新成与真实状态一致，而不是重跑一整夜。
"""
import sys
from pathlib import Path

def _repo_root() -> Path:
    """仓库根（SixCoder/）。用相对定位，不写死本机路径 —— 换机器/换目录都能用。"""
    return Path(__file__).resolve().parents[2]


def _projects_dir() -> Path:
    """历史产地目录：优先环境变量 SIXCODER_PROJECTS，否则 <仓库根>/projects。"""
    import os
    env = (os.environ.get("SIXCODER_PROJECTS") or "").strip()
    return Path(env) if env else (_repo_root() / "projects")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import company, contract as contract_mod, e2e, iterate, trace  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "night3"
REPO = _projects_dir() / name
REQ = Path(sys.argv[2]) if len(sys.argv) > 2 else (
    Path(__file__).resolve().parent.parent / "tools" / "fixtures" / "hq_requirement.txt")

text = REQ.read_text(encoding="utf-8-sig")
contracts = contract_mod.parse_requirement(text)
state = iterate.inspect(REPO, contracts)
p = company.write_delivery(REPO, text, state, [], "（重算报告，未执行新工作）", 0.0)
tp = trace.write(REPO, text, contracts, state, iterate.last_e2e(REPO) or e2e.write(REPO, contracts, text))
ver = sum(s["verified"] for s in state.values())
tot = sum(s["funcs"] for s in state.values())
print(f"{name}: {ver}/{tot} → {p}")
print(f"  追溯矩阵 → {tp}")
for path, st in state.items():
    print(f"  {path}: {st['verified']}/{st['funcs']}；无判据 {st['untested']}；桩 {sorted(st['stubs'])}")
