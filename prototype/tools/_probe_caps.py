"""临时探针：验证指定能力的端到端状态（只读，不写账本）。"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import blueprint as BPM
from core import openhouse as OH

# 引擎不含项目身份（2026-11-19）：路径来自 --repo / SIXCODER_REPO / _logs/engine.json
import sys as _sys
_ENGINE = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import repo_root as _repo_root  # noqa: E402


WS = _repo_root()
IDS = sys.argv[1:] or [
    "cap.auth.forbidden_role",
    "cap.topic.create_draft",
    "cap.topic.submit_and_publish",
    "cap.topic.offline",
]
bp = BPM.load(WS)
cov = OH.observe(WS, bp, emit=None, only=set(IDS))
caps = (cov or {}).get("caps") or {}
print(f"探针结果（{len(IDS)} 条）：")
for cid in IDS:
    c = caps.get(cid) or {}
    st = c.get("status") or "?"
    ev = str(c.get("evidence") or c.get("detail") or "")[:110]
    flag = "OK " if st == "done" else "RED"
    print(f"  [{flag}] {cid:38s} {st:8s} {ev}")
print()
print("通过:", sum(1 for cid in IDS if (caps.get(cid) or {}).get("status") == "done"),
      "/", len(IDS))