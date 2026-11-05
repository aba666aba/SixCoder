"""SixCoder 内核：核心机制的最小实现。

设计约束（来自 docs/13 判定权分离）：
- 任务状态只能由 gate_runs 推进，evidence_kind='model_text' 永不计入通过。

engine_version()：引擎指纹。**任何会改变判据/闸门结果的改动都要让它变**（这里是仓库根
VERSION 文件的头两段），否则库里缓存的测试规格会继续用旧判据，导致"改好了却还失败"。
"""
from __future__ import annotations

from pathlib import Path


def engine_version() -> str:
    """引擎版本指纹：VERSION 主次版本 + 判据相关文件的 mtime 摘要。

    为什么带 mtime：只改代码不改 VERSION 时（开发中很常见），缓存规格也必须失效，
    否则就会出现"我修好了它还是红"的假象（实测踩过）。
    """
    root = Path(__file__).resolve().parent.parent          # prototype/
    ver = "0.0.0"
    vf = root.parent / "VERSION"
    if vf.exists():
        try:
            ver = vf.read_text(encoding="utf-8").strip() or ver
        except OSError:
            pass
    stamp = 0.0
    for name in ("props.py", "gates.py", "contract.py", "oracle.py"):
        p = root / "core" / name
        try:
            stamp += p.stat().st_mtime
        except OSError:
            continue
    return f"{ver}+{int(stamp) % 100000:05d}"


# 兼容别名：db.py 里用 release_version() 更贴合语义
release_version = engine_version
