"""控制台编码：把 stdout/stderr 切成 UTF-8，别再让中文把进程打崩。

为什么必须有这个模块（2026-11-23 实测，中文 Windows）：
    默认 `locale.getpreferredencoding(False)` = `cp936`，`sys.stdout.encoding` = `gbk`。
    于是任何一次 `print("✓")`、`print("↔")` 都会抛
    `UnicodeEncodeError: 'gbk' codec can't encode character ...` —— 脚本当场退出，
    退出码非 0，被上层记成"自检失败"。实测 `checks/run_all.py` 一次全量跑下来有
    **7 个脚本因此假红**，而它们单独在 UTF-8 终端里跑全绿。这不是代码 bug，是编码墙。

三条原则：
    1) 只改**本进程**的输出流，不动系统区域设置（改系统编码会影响用户其它软件）；
    2) **永不抛异常**：已经指向无缓冲/已关闭/二进制流时静默退回原来那套编码。
       编码修复本身失败都不该成为新的失败点。
    3) `encode_stdout()` 允许传一个非 sys.stdout 的对象（子进程/测试用）。

谁该调用：任何会 print 中文的**进程入口**（checks/run_all.py、每个 check_*.py 的
`__main__`、prototype/tools/*.py 的命令行入口、cli.py / server.py）。库模块不要调用。
"""
from __future__ import annotations

import io
import sys

TARGET = "utf-8"


def encode_stream(stream, encoding: str = TARGET):
    """把一个文本流原地重配成 `encoding`；做不到就原样返回（绝不抛）。"""
    if stream is None:
        return stream
    # Python 3.7+ 的标准做法。TextIOWrapper.reconfigure 会保留缓冲。
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding=encoding, errors="replace")
            return stream
        except (ValueError, OSError, AttributeError, TypeError):
            pass
    # 退化路径：拿到底层 buffer 重新包一层（无 buffer 的流，比如 StringIO，直接放弃）
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return stream
    try:
        return io.TextIOWrapper(buffer, encoding=encoding, errors="replace",
                                line_buffering=True, write_through=True)
    except (ValueError, OSError, AttributeError, TypeError):
        return stream


def encode_stdout(encoding: str = TARGET) -> None:
    """把本进程的 sys.stdout / sys.stderr 切成 UTF-8。幂等，可重复调用。"""
    for name in ("stdout", "stderr"):
        cur = getattr(sys, name, None)
        if cur is None:
            continue
        new = encode_stream(cur, encoding)
        if new is not cur:
            try:
                setattr(sys, name, new)
            except (AttributeError, TypeError):
                pass


def utf8_env(env: dict | None = None) -> dict:
    """给子进程环境补上 `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8`（不改调用方原环境）。

    为什么子进程也要：父进程自己修好了编码，子进程仍会用默认 cp936 打开 stdout；
    中文 Windows 下"父进程正常、子进程崩溃"是最难查的一类假红。
    """
    out = dict(env if env is not None else {})
    out.setdefault("PYTHONUTF8", "1")
    out.setdefault("PYTHONIOENCODING", "utf-8")
    return out
