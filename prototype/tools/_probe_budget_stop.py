"""临时探针：停机判定实际看到的金额是多少（只读）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import budget_estimate as BE, remote as RM

# 引擎不含项目身份（2026-11-19）：路径来自 --repo / SIXCODER_REPO / _logs/engine.json
import sys as _sys
_ENGINE = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import repo_root as _repo_root  # noqa: E402


WS = str(_repo_root() or "")
for tid in ("5770e09935017482", "night", ""):
    try:
        s = RM.usage_summary(tid) if tid else RM.usage_summary()
    except Exception as exc:                              # noqa: BLE001
        print(f"task={tid!r:20} 出错 {type(exc).__name__}: {exc}")
        continue
    print(f"task={tid!r:20} cost_cny={s.get('cost_cny')}  turns={s.get('turns')}  tokens={s.get('total_tokens')}")

print()
cap = BE.load_system_budget(WS)
print("system_budget_cny =", cap, " OVER_CAP_RATIO =", BE.OVER_CAP_RATIO,
      " 停机线 =", cap * BE.OVER_CAP_RATIO)
print("system_budget_stopped(5770e09935017482) =",
      BE.system_budget_stopped(WS, "5770e09935017482"))