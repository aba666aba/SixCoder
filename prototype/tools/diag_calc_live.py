"""真跑一次计算器夜班（在工作区副本上），把**所有事件**打出来，看写作卡在哪。

用户那次：6 轮、12 秒、0 次写作调用、¥0.0045。
本脚本把 emit 全量输出，能直接看到它是"没调用"还是"调用了但立刻失败"。
"""
import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company, contract as cm, remote  # noqa: E402

SRC = Path("D:/Download/cal")
DST = ROOT.parent / "projects" / "_calc_repro2"
if DST.exists():
    shutil.rmtree(DST, ignore_errors=True)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(".git", ".mypy_cache"))
# 把契约固定下来，保证和用户那次同一份
contract_txt = (SRC / ".sixcoder" / "contract.txt").read_text(encoding="utf-8")
REQ = "生成一个计算器"

print(f"报告：远端可用={remote.available()}")
print("阶梯：", [x["profile_id"] + ":" + x["effort"] for x in remote.ladder()])
print(f"工作区副本：{DST}\n")

events = []
_evp = ROOT.parent / "_logs" / "diag_events.jsonl"
_evp.parent.mkdir(parents=True, exist_ok=True)
if _evp.exists():
    _evp.unlink()


def on(kind, payload):
    import json as _j
    events.append(kind)
    line = _j.dumps({"kind": kind, "payload": payload}, ensure_ascii=False, default=str)
    with _evp.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if kind in ("order_failed", "author_feedback", "decision", "isolate_rollback",
                "order_done", "notice", "author_start", "output_budget"):
        print(f"  [{kind}] {line[:260]}", flush=True)


before = remote.usage_summary("")
res = company.night(DST, REQ, "qwen2.5-coder:7b", hours=0.05, use_remote=True,
                    contract_file=str(SRC / ".sixcoder" / "contract.txt"), emit=on,
                    max_cny=1.0)
after = remote.usage_summary("")
print(f"\n结果：{res.verified}/{res.total}｜{res.spent_s:.0f}s｜{res.reason[:160]}")
print(f"本次调用 {after['calls'] - before['calls']} 次，¥{round(after['cost_cny'] - before['cost_cny'], 4)}")
print(f"\n事件种类：{sorted(set(events))}")
print("\ncalc.py 现状：")
print((DST / "calc.py").read_text(encoding="utf-8")[:600])
