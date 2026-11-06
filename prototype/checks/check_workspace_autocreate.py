"""自检：全自主模式允许 --repo 指向还不存在的目录（从 0 建仓库）。

真实事故（2026-11-06）：`cli.py night --goal ... --repo <新目录>` 直接
「仓库不存在」exit(2)，因为沿用了需求模式「必须指向已有仓库」的旧口径。
目标模式自己规划切片、自己写代码，工作区本来就该由系统创建。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import auto  # noqa: E402

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print("  OK  " + msg if cond else "  FAIL " + msg)
    if not cond:
        FAILS.append(msg)


with tempfile.TemporaryDirectory(prefix="sixcoder_autows_") as td:
    ws = Path(td) / "not" / "created" / "yet"
    res = None
    err = None
    try:
        res = auto.night_auto(ws, "", llm=lambda *a, **k: {"ok": True, "answer": "{}"},
                              runner=lambda *a, **k: {}, recon=lambda *a, **k: {})
    except Exception as exc:                             # noqa: BLE001
        err = exc
    ok(err is None, "不存在的工作区不抛异常" + (f"（{type(err).__name__}: {err}）" if err else ""))
    ok(ws.exists() and ws.is_dir(), "工作区（含多级父目录）被自动创建")
    ok(isinstance(res, dict) and not res.get("ok"), "没给目标时如实失败，不假装完成")

src = (ROOT / "cli.py").read_text(encoding="utf-8")
ok("已新建工作区" in src and "if goal:" in src,
   "CLI 的 --goal 分支保留自动建目录，不回到一律 exit(2)")

if FAILS:
    raise SystemExit(1)
print("全部通过（全自主从 0 建工作区）")
