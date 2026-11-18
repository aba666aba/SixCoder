"""复现「生成一个计算器」这次夜班的崩溃：NameError: name 'Path' is not defined。

现场（D:\\Download\\cal 的 DELIVERY.md）：
  - 收工原因：夜班异常中断：NameError: name 'Path' is not defined
  - 用时 0.0 分钟、远端写代码 0 次
  - 却写着"花费 ¥6.2099（941 次远端调用）" ← 这是**全库累计**，本次一条调用都没有

本脚本只做一件事：把真实 traceback 打出来，定位到底哪一行缺 Path。
"""
import io
import shutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company  # noqa: E402

ws = ROOT.parent / "projects" / "_repro_calc_crash"
if ws.exists():
    shutil.rmtree(ws, ignore_errors=True)
ws.mkdir(parents=True, exist_ok=True)

events = []


def on(kind, payload):
    events.append(kind)
    print(f"  [{kind}] {str(payload)[:110]}")


print(f"复现：{ws}")
try:
    res = company.night(ws, "生成一个计算器", "qwen2.5-coder:7b",
                        use_remote=False, emit=on)
    print(f"\n结果：{res.verified}/{res.total}｜{res.reason[:120]}")
except Exception:
    print("\n=== 真实 traceback ===")
    traceback.print_exc()

print("\n事件顺序：", " → ".join(events[:14]))
