"""ASGI 入口探测：判据端不再写死 `app/main.py`。

为什么（2026-11-08 用户点名的三个硬编码耦合点之一）：入口路径散布在冒烟判据、
入口判定、门禁等 12+ 处，写死 `app/main.py` 意味着换个布局（入口在仓库根或
src/ 下）整条观测链路就哑了。

分工约定（改这里之前先读）：
  · 生成端（auto.py 写入口、契约提示词、项目宪法）**维持 app/main.py 约定**——
    引擎自己生成的入口本来就在那，约定自洽，动它只会打碎 prompt 缓存与文本一致性；
  · 判据端（冒烟、入口判定、门禁）一律走这里的探测——**别再写死路径**。
"""
from __future__ import annotations

from pathlib import Path

# 探测顺序即优先级：引擎生成的布局排最前，常见变体随后。
_CANDIDATES = ("app/main.py", "main.py", "src/main.py", "src/app/main.py")


def entry_path(ws) -> Path | None:
    """仓库的 ASGI 入口文件（探测不到返回 None = 冒烟不适用，不是错误）。"""
    ws = Path(ws)
    for rel in _CANDIDATES:
        p = ws / rel
        if p.is_file():
            return p
    return None


def entry_rel(ws) -> str:
    """入口的仓库相对路径（正斜杠）；探测不到返回空串。"""
    p = entry_path(ws)
    if p is None:
        return ""
    try:
        return p.relative_to(Path(ws)).as_posix()
    except ValueError:                                   # pragma: no cover
        return ""


def is_entry(module: str, ws=None) -> bool:
    """这个模块路径是不是 ASGI 入口。

    兼容两种口径：写死的 `app/main.py`（生成端约定，永远算）与探测到的实际入口。
    ws 给了就以探测为准——同一仓库里不该有两个"入口"判定口径。
    """
    m = str(module or "").replace("\\", "/").strip()
    if not m:
        return False
    if m == "app/main.py":
        return True
    if ws is not None:
        rel = entry_rel(ws)
        return bool(rel) and m == rel
    return False
