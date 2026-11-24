"""Python 库入口（**由引擎生成，不要改**）。

为什么不让你写这个文件：功能是分模块长的（lib/text.py、lib/math.py …），
每加一个模块就重写一次 __init__ → 之前的导出被抹掉。

你只需要在每个功能模块里写实现函数，然后在 __all__ 里登记名字。
引擎会把 lib/ 下的模块都列进 __all__ 的扫描范围；这里用显式列表，
被导入的名字必须真实存在，否则 import 时会报错 —— 这是刻意的：
"导出不存在的东西"应该在 import 阶段就暴露，而不是等到用的时候。
"""
from __future__ import annotations

__version__ = "0.1.0"

__all__: list[str] = []


def __getattr__(name: str):
    """按需从 lib 下的子模块取符号。

    为什么用懒加载而不是 import *：
      · 空壳阶段 lib/ 下可能还没有模块，import * 会直接炸；
      · 懒加载让 "库里还没有东西" 和 "库坏了" 区分得开。
    """
    import importlib
    import pkgutil

    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"{__name__}.{info.name}")
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
