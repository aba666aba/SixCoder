"""自检：--slice-hours 的时间闸门必须落进作者自愈循环内部。

真实事故（dev1106c，2026-11-07）：`--slice-hours 0.05`（3 分钟）完全没兜住，
单轮 4 次自愈每次约两分钟（委托本地 7B），跑了十分钟还在 attempt 3。
原因：时间只在外层 round 边界检查，`_author_round` 传下去的 budget_left
还是个静态值，author_module 的自愈循环里根本没有截止点。
"""
from __future__ import annotations

import inspect
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import author, remote  # noqa: E402

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print("  OK  " + msg if cond else "  FAIL " + msg)
    if not cond:
        FAILS.append(msg)


ok("deadline" in inspect.signature(author.author_module).parameters,
   "author_module 接受 deadline 参数")
company_src = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
ok("deadline=_deadline" in company_src and "_deadline - 5" in company_src,
   "_author_round 把截止点传下去，且模块循环也检查")

with tempfile.TemporaryDirectory(prefix="sixcoder_deadline_") as td:
    ws = Path(td)
    (ws / "x.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    calls = {"n": 0}
    orig = remote.complete

    def fake_complete(*a, **k):
        calls["n"] += 1
        return {"ok": True, "answer": "{}", "usage": {}}

    remote.complete = fake_complete
    try:
        expired = author.author_module(ws, "x.py", None, {},
                                       deadline=time.time() - 1, attempts=4)
        ok(expired.get("timed_out") is True, "过期 deadline 返回 timed_out=True")
        ok(calls["n"] == 0, "过期 deadline 在发任何远端请求之前就停")
        ok("时间预算耗尽" in str(expired.get("reason") or ""),
           "收工原因如实写明时间耗尽，不伪装成「代码没写对」")

        calls["n"] = 0
        author.author_module(ws, "x.py", None, {},
                             deadline=time.time() + 300, attempts=1)
        ok(calls["n"] >= 1, "未过期的 deadline 不误杀正常自愈")
    finally:
        remote.complete = orig

if FAILS:
    raise SystemExit(1)
print("全部通过（切片时间闸门）")
