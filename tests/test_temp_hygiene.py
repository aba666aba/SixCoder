"""临时文件泄漏测试（2026-11-24）。

## 背景

用户问"有没有其他临时文件问题"，扫出来两个真问题：

1. **系统 temp 积压 10,154 个目录**，几乎全来自测试的 mkdtemp()
   mkdtemp 只创建不删除，每跑一次 pytest 多几十个

2. 视频抽帧写 PNG 不删（已在 test_visual_llm 里修并钉住）

## 本文件钉什么

· tests/_tmp.py 的 temp_ws() 真的会清理（含异常路径）
· 引擎的 gc_engine_tmp 真的能清（不靠前缀白名单，按时间）
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))
sys.path.insert(0, str(ROOT / "tests"))

from core import probe   # noqa: E402
import _tmp              # noqa: E402


def test_temp_ws_cleans_after_normal_exit():
    """正常退出时清理。"""
    with _tmp.temp_ws("t_normal-") as ws:
        (ws / "a.txt").write_text("x", encoding="utf-8")
        assert ws.is_dir()
    assert not ws.exists(), "temp_ws 没清干净"


def test_temp_ws_cleans_on_exception():
    """**关键**：异常路径也要清（atexit 做不到这个）。"""
    try:
        with _tmp.temp_ws("t_raise-") as ws:
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not ws.exists(), "异常路径没清干净"


def test_gc_engine_tmp_removes_old_entries():
    """引擎的 .tmp GC 能按时间清老目录。"""
    root = Path(tempfile.mkdtemp(prefix="t_gcroot-"))
    try:
        old = root / "old_thing"
        old.mkdir()
        (old / "f.txt").write_text("x", encoding="utf-8")
        # 把 mtime 改到 100 小时前
        past = time.time() - 100 * 3600
        import os
        os.utime(old, (past, past))

        fresh = root / "fresh_thing"
        fresh.mkdir()

        r = probe.gc_engine_tmp(root=root, max_age_hours=48.0)
        assert r["removed"] >= 1, r
        assert not old.exists(), "老目录该被清掉"
        assert fresh.exists(), "新目录不能动"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_gc_engine_tmp_respects_age_threshold():
    """没到阈值的不能删（夜班正在用）。"""
    root = Path(tempfile.mkdtemp(prefix="t_gc2-"))
    try:
        d = root / "just_created"
        d.mkdir()
        r = probe.gc_engine_tmp(root=root, max_age_hours=48.0)
        assert r["removed"] == 0, r
        assert d.exists(), "新目录被误删了"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)


def test_gc_engine_tmp_never_touches_hsperfdata():
    """JVM 的 hsperfdata_* 是活文件，绝不能碰。"""
    root = Path(tempfile.mkdtemp(prefix="t_gc3-"))
    try:
        h = root / "hsperfdata_Lenovo"
        h.mkdir()
        import os
        past = time.time() - 200 * 3600
        os.utime(h, (past, past))
        probe.gc_engine_tmp(root=root, max_age_hours=1.0)
        assert h.exists(), "hsperfdata 被误删了（JVM 会出问题）"
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
