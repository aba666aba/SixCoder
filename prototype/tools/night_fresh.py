"""从零起跑一次夜班（清空 workspace）：测"整套机制端到端"的真实水平。

用法：python tools/night_fresh.py <目录名> [--no-remote] [--hours 0.5]
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
from core import company  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "night3"
use_remote = "--no-remote" not in sys.argv
hours = 0.5
model = "qwen2.5:7b"
if "--model" in sys.argv:
    model = sys.argv[sys.argv.index("--model") + 1]
if "--hours" in sys.argv:
    hours = float(sys.argv[sys.argv.index("--hours") + 1])
REPO = _projects_dir() / name
REQ = Path(__file__).resolve().parent.parent / "tools" / "fixtures" / "hq_requirement.txt"


def on(kind, payload):
    if kind == "round":
        print(f"--- 第{payload['round']}轮｜已验收 {payload['verified']}/{payload['total']}"
              f"｜剩余 {payload['left_min']}min", flush=True)
    elif kind == "architect":
        print(f"  [架构师] {(payload.get('root_cause') or '')[:80]}", flush=True)
    elif kind == "strategy":
        print(f"  [策略] {payload.get('strategy')}", flush=True)
    elif kind in ("order_done", "order_failed"):
        mark = "OK " if kind == "order_done" else "FAIL"
        detail = str(payload.get("applied") or payload.get("reason") or "")[:60]
        print(f"  {mark} {payload['order'][:58]} {detail}", flush=True)


if REPO.exists():
    for f in sorted(REPO.rglob("*"), reverse=True):
        if f.is_file():
            f.unlink()
        elif f.is_dir():
            try:
                f.rmdir()
            except OSError:
                pass
else:
    REPO.mkdir(parents=True, exist_ok=True)

res = company.night(REPO, REQ.read_text(encoding="utf-8-sig"), model,
                    hours=hours, use_remote=use_remote, max_rounds=10, emit=on)
print()
print(f"最终：验收 {res.verified}/{res.total}｜用时 {res.spent_s}s｜收工：{res.reason}")
print("交付物:", res.delivery_path)
for path, st in (res.modules or {}).items():
    print(f"  {path}: {st['verified']}/{st['funcs']} 验收；桩 {st['stubs']}")
