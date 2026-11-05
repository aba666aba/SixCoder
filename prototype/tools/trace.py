"""生成/刷新追溯矩阵 TRACE.md（不跑夜班，只按当前产物出表）。

用法：python tools/trace.py <projects 下的目录名> [需求文件]
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
from core import contract as contract_mod, e2e, gates, iterate, trace  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "night3"
REPO = _projects_dir() / name
REQ = Path(sys.argv[2]) if len(sys.argv) > 2 else (
    Path(__file__).resolve().parent.parent / "tools" / "fixtures" / "hq_requirement.txt")

text = REQ.read_text(encoding="utf-8-sig")
contracts = contract_mod.parse_requirement(text)
state = iterate.inspect(REPO, contracts)
e2e_res = e2e.write(REPO, contracts, text)
path = trace.write(REPO, text, contracts, state, e2e_res)

sec = gates.gate_security(REPO, list(contracts.keys()))
fmap = gates.gate_field_map_result(REPO, list(contracts.keys()), text)
deps = gates.gate_deps(REPO)

ver = sum(s["verified"] for s in state.values())
tot = sum(s["funcs"] for s in state.values())
print(f"{name}: 已验收 {ver}/{tot}")
print(f"  e2e      {e2e.as_feedback(e2e_res)}")
print(f"  安全     {'OK' if sec.ok else 'FAIL'} {sec.detail[:120]}")
print(f"  字段歧义 {fmap.detail[:160]}")
print(f"  依赖     {'OK' if deps.ok else 'FAIL'} {deps.detail[:120]}")
print(f"  → {path}")
