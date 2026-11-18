"""临时探针：全量观测（只读），打印 done 数与红名单前几条。"""
import sys
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
bp = BPM.load(WS)
cov = OH.observe(WS, bp, emit=None, only=None)
caps = (cov or {}).get("caps") or {}
done = [c for c, v in caps.items() if (v or {}).get("status") == "done"
        or str((v or {}).get("status") or "").startswith("done")]
red = [(c, (v or {}).get("status")) for c, v in caps.items() if c not in done]
print(f"全量观测：done {len(done)} / {len(caps)}")
print("红的（前 8 条）：")
for c, st in red[:8]:
    print(f"  {c:40s} {st}")