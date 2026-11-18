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

# 2026-11-09：远端能力闸门的字段（read_ok / max_out …）不在白名单 → 日志里只剩
# path=，**看不出远端是满血还是被掐**。这几个字段必须打出来。
s4 = cli._slim({"path": "app/models.py", "chars": 20857, "read_ok": True,
                "mode": "full", "tiers": ["T0", "T1"]})
ok("read_ok=True" in s4, "read_gate 的 read_ok 会被打印（能看见全文是否给全）")
ok("mode=full" in s4, "read_gate 的 mode 会被打印")
s5 = cli._slim({"path": "app/models.py", "max_out": 1600, "est_tokens": 980,
                "basis": "骨架"})
ok("max_out=1600" in s5, "output_budget 的 max_out 会被打印（能看见配额够不够）")
ok("est_tokens=980" in s5, "output_budget 的 est_tokens 会被打印")
s6 = cli._slim({"path": "app/models.py", "max_out": 6400, "grew_from": 1600,
                "why": "上一版输出顶到配额上限，判为截断而非能力问题"})
ok("grew_from=1600" in s6, "配额放大留痕会被打印")
s7 = cli._slim({"path": "app/services/topics.py", "attempt": 1,
                "only": ["submit_topic"], "evidence": "契约函数缺失：submit_topic"})
ok("evidence=" in s7 and "submit_topic" in s7, "失败证据会被打印（能看见为什么判红）")
ok("only=" in s7, "重试范围会被打印")

src = (ROOT / "cli.py").read_text(encoding="utf-8")
ok('if kind in ("round", "strategy"' not in src,
   "printer 不再用 kind 白名单吞掉新事件")
ok("默认打印" in src and "context_layout" in src,
   "改为默认全打印、只过滤高频噪音")

if FAILS:
    raise SystemExit(1)
print("全部通过（事件可观测性兜底）")
