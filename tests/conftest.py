"""pytest 全局配置（2026-11-24）。

## 这个文件解决什么

实测：系统 temp 里积压了 **10,154 个目录**，几乎全来自测试的 mkdtemp()。
mkdtemp 只负责创建、从不删除 —— 每跑一次 pytest 就多几十个目录，
几个月就把磁盘塞满。

## 第一版为什么失败（记下来，别重犯）

第一版用**前缀白名单**判哪些目录该删，结果漏了 "probejudge-" 这个前缀 ——
跑完两个测试文件，净增 5 个目录没清掉。

**白名单必然漏**：测试随时会加新前缀，维护列表赶不上。
（和 core/ecosystem 那次「三个注册表各维护一份清单」是同一个病。）

## 现在的做法：按"是否本次新生"判定，不靠前缀

    session 开始：记下 temp 根目录下所有条目名
    session 结束：删除「结束后有、开始时没有」的条目

判据只有一条：**这个目录是本次 pytest 会话期间出现的**。
不靠名字，不会漏。

安全边界：
  · 只删 temp 根目录的**直接子项**，不递归扫描别的路径
  · 只删本次新出现的 —— 会话开始前就存在的（可能是别的程序的）一律不动
  · 删失败静默跳过（可能被别人占用），绝不因此让测试失败
  · 如果配置了 keep_temp 环境变量，整个机制关闭（排查问题时用）

新写测试仍推荐 tmp_path fixture 或 tests/_tmp.py 的 temp_ws()，
这是两层保险里的**第二层**。
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


def _snapshot() -> set:
    """temp 根目录下的直接子项名。拿不到就返回空集（不报错）。"""
    try:
        root = Path(tempfile.gettempdir())
        return {p.name for p in root.iterdir()}
    except OSError:
        return set()


@pytest.fixture(scope="session", autouse=True)
def _cleanup_test_temps():
    """会话级兜底：删掉本次会话期间新出现在 temp 根目录的条目。"""
    if os.environ.get("SIXCODER_KEEP_TEMP"):
        yield
        return
    before = _snapshot()
    yield
    after = _snapshot()
    fresh = after - before
    if not fresh:
        return
    root = Path(tempfile.gettempdir())
    removed = 0
    for name in fresh:
        p = root / name
        if not p.is_dir():
            continue                                     # 只清目录，不碰文件
        try:
            shutil.rmtree(p, ignore_errors=True)
            if not p.exists():
                removed += 1
        except OSError:
            pass
    if removed:
        print("\n[pytest] 已清理本次会话新产生的 %d 个临时目录" % removed)
