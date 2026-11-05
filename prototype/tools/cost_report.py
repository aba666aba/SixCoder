import io
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import remote  # noqa: E402

db = sqlite3.connect("data/proto.db")
db.row_factory = sqlite3.Row

tot = list(db.execute(
    "select count(*) c, round(sum(cost_cny),4) s, min(created_at) t0, max(created_at) t1"
    " from remote_usage"))[0]
print(f"全库：{tot['c']} 次调用｜合计 ¥{tot['s']}｜跨度 {tot['t0']} → {tot['t1']}")

print("\n最贵的 5 次单次调用：")
for r in db.execute("select id, task_id, node_id, model, effort, prompt_tokens,"
                    " completion_tokens, reasoning_tokens, round(cost_cny,4) cny, created_at"
                    " from remote_usage order by cost_cny desc limit 5"):
    print(f"  #{r['id']} {r['created_at']} {r['node_id']} {r['model']}:{r['effort']}"
          f" 入{r['prompt_tokens']} 出{r['completion_tokens']} 推理{r['reasoning_tokens']}"
          f" → ¥{r['cny']}")

print("\n界面用量接口 usage_summary('') 的口径：")
u = remote.usage_summary("")
print(f"  calls={u.get('calls')} 输入={u.get('prompt_tokens')} 输出={u.get('completion_tokens')}"
      f" 命中={u.get('cache_hit_tokens')} 花费=¥{u.get('cost_cny')}")
bm = {k: (v or {}).get("cost_cny") for k, v in (u.get("by_model") or {}).items()}
be = {k: (v or {}).get("cost_cny") for k, v in (u.get("by_effort") or {}).items()}
print(f"  按模型：{bm}")
print(f"  按档位：{be}")

print("\n按任务聚合（前 8 名）：")
for r in db.execute("select task_id, count(*) c, round(sum(cost_cny),4) s,"
                    " sum(completion_tokens) out from remote_usage"
                    " group by task_id order by s desc limit 8"):
    print(f"  {str(r['task_id'])[:26]:26s} {r['c']:3d} 次  出{r['out']:7d}  ¥{r['s']}")
