"""测试用的临时目录工具（2026-11-24）。

## 为什么需要

实测：系统 temp 里积压了 **10,154 个目录**，几乎全来自测试的 mkdtemp()。
mkdtemp 只负责"创建"，**从不负责删除** —— 每跑一次 pytest 就多几十个目录，
几个月下来把磁盘塞满。

## 两种正确写法

1. **测试函数里**用 pytest 的 tmp_path fixture（最简单）：
       def test_x(tmp_path):
           (tmp_path / "a.py").write_text("x")
   pytest 自动建、自动清，不用 import 任何东西。

2. **helper 函数里**（拿不到 fixture）用 temp_ws()：
       with temp_ws("mytest") as ws:
           ...

## 为什么不用 atexit 注册清理

atexit 只在进程正常退出时跑；pytest -x 中途停止、Ctrl+C、CI 超时都会跳过。
with 语句（try/finally）在异常路径上也保证清理。
"""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def temp_ws(prefix: str = "sixcoder-test-") -> Iterator[Path]:
    """临时工作目录：离开 with 块时**无论如何**都删掉。

    用法：
        with temp_ws("mytest") as ws:
            (ws / "a.py").write_text("x")
            ...  # 断言放这里

    与 mkdtemp 的区别：mkdtemp 返回路径就撒手了，这个保证清理。
    """
    d = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def make(prefix: str = "sixcoder-test-") -> Path:
    """**不推荐**：直接造一个不会被自动清理的目录。

    只在前两种方式都用不了时用（比如模块级 fixture）。
    用了它就要自己负责删 —— 否则就是在制造垃圾。
    """
    return Path(tempfile.mkdtemp(prefix=prefix))
