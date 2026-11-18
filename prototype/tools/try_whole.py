"""手工跑一次"整仓会话"（验证/演练用，不参与夜班主循环）。

用法：
    python tools/try_whole.py <repo> --dry           # 只拼提示词，不花钱
    python tools/try_whole.py <repo>                 # 真发一次（花钱，会写盘）
    python tools/try_whole.py <repo> --report FILE   # 用文件的报告文本

为什么单独留一个手工具：整仓会话的单次输入很大（十几万字符），
排查"提示词有没有拼对/远端到底怎么答"时，不能拿夜班去试错。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from core import wholerepo as W                                     # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--dry", action="store_true", help="只拼提示词，不调用远端")
    ap.add_argument("--report", default="", help="报告文本文件（不给就用占位报告）")
    ap.add_argument("--max-out", type=int, default=0)
    ap.add_argument("--extra", default="")
    a = ap.parse_args()

    ws = Path(a.repo)
    text = ""
    if a.report:
        text = Path(a.report).read_text(encoding="utf-8-sig", errors="replace")
    rep = W.SimpleReport(text or "（演练：没有真实报告，只验证通道）", "")

    def emit(tag, data):
        s = json.dumps(data, ensure_ascii=False)
        print("[%s] %s" % (tag, s[:260]))

    kw = {"dry_run": True, "emit": emit} if a.dry else {"emit": emit, "extra": a.extra}
    if a.max_out:
        kw["max_out"] = a.max_out
    r = W.session(ws, rep, **kw)
    if a.dry:
        print("提示词字符数：%d" % r.get("chars", 0))
        return 0
    print(json.dumps({k: v for k, v in r.items() if k != "brief"},
                     ensure_ascii=False, indent=1)[:4000])
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
