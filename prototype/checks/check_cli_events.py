"""自检：命令行事件不许打成空行。

真实事故（dev1106a，2026-11-06）：日志里 `scale / governance / round`
后面全是空白——这些事件的字段（verified/total/left_min/strategy 等）不在
_slim 白名单里。白名单永远追不上新事件，所以加兜底：没有任何白名单字段命中时，
打印前几个标量字段，保证命令行始终看得到进度。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cli  # noqa: E402

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print("  OK  " + msg if cond else "  FAIL " + msg)
    if not cond:
        FAILS.append(msg)


s = cli._slim({"verified": 1, "total": 3, "left_min": 5.2, "strategy": "mechanical"})
ok(bool(s.strip()), "新事件的字段不再输出空行")
ok("verified=1" in s and "total=3" in s, "兜底打印了真实进度字段")
ok("strategy=mechanical" in s, "兜底字段覆盖字符串值")

s2 = cli._slim({"path": "cli_core.py", "ok": True, "verified": 1})
ok("path=cli_core.py" in s2, "已有白名单字段仍按原口径优先打印")

s3 = cli._slim({})
ok(s3.strip() == "", "空 payload 仍保持空（不假造内容）")

src = (ROOT / "cli.py").read_text(encoding="utf-8")
ok('if kind in ("round", "strategy"' not in src,
   "printer 不再用 kind 白名单吞掉新事件")
ok("默认打印" in src and "context_layout" in src,
   "改为默认全打印、只过滤高频噪音")

if FAILS:
    raise SystemExit(1)
print("全部通过（事件可观测性兜底）")
