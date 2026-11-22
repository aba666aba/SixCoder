"""运行期数据目录：引擎代码与用户数据分开（打包成 EXE 后卸载才能干净）。

为什么要有这个模块（2026-11-20 用户口径："控制面板卸载不要有残留，可选要不要保留
仓库的历史"）：只要运行期写入还散落在安装目录里，"卸载时保留历史"就只能靠
卸载器逐个目录挑着删 —— 一定会漏，而且装到只读目录（Program Files）直接崩。
把可变数据收敛到一个目录后：
    · 卸载默认 = 删程序本体 + 删这整个目录（无残留）
    · 卸载勾选"保留历史" = 只删程序本体，目录原样留着，重装后接着跑

优先级：
    1) 环境变量 `SIXCODER_HOME`（打包态由 shell/main.cjs 设成 %LOCALAPPDATA%\\SixCoder）
    2) 仓库根（开发态：`prototype/..`）—— 与历史行为完全一致，零迁移

注意：只放**引擎自身**的账本/日志/数据库。产物仓里的 `.sixcoder/`、`_logs/`
属于用户的项目，永远跟随用户指定的 --repo，绝不进这里。
"""
from __future__ import annotations

import os
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]        # prototype/


def home() -> Path:
    """运行期数据根目录（不创建，纯查询）。"""
    env = str(os.environ.get("SIXCODER_HOME") or "").strip()
    if env:
        return Path(env)
    return ENGINE_ROOT.parent


def ensure(p: Path) -> Path:
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return p


def logs_dir() -> Path:
    """引擎日志目录（watchdog / patrol / night 结果都在这里）。"""
    return ensure(home() / "_logs")


def data_dir() -> Path:
    """引擎自己的数据库/索引目录（sqlite、api 索引）。"""
    return ensure(home() / "data")


def night_tag(repo: str) -> str:
    """仓库 → 稳定短标签（夜班日志/指针用，避免把整个路径塞进文件名）。

    必须**先规范化路径**：同一仓库从界面来是 `F://WorkSpace//Cut`，从命令行来可能是
    `F:/WorkSpace/Cut`，不统一就会算出两个标签 → 日志指针找不到、续跑标记出现两份。
    """
    import hashlib
    norm = str(repo).replace("\\", "/").replace("/", "/").strip().rstrip("/").lower()
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:10]


def night_log(repo: str) -> Path | None:
    """该仓库最近一次夜班的输出日志。

    以前所有仓库共用一个 `night_20261108_run19.log`（append），界面重开时按
    "最新文件"猜，容易把别的仓库的日志显示到当前仓库下面。改成每仓库一个指针文件，
    读不到再退回最新日志（兼容旧版本留下的硬编码文件）。
    """
    try:
        cur = logs_dir() / f"night_{night_tag(repo)}.current"
        p = Path((cur.read_text(encoding="utf-8") or "").strip())
        if p.is_file():
            return p
    except (OSError, ValueError):
        pass
    try:
        cands = sorted(logs_dir().glob("night_*.log"),
                       key=lambda x: x.stat().st_mtime, reverse=True)
        return cands[0] if cands else None
    except OSError:
        return None


def is_packaged() -> bool:
    """是否跑在安装版里（数据目录与代码目录分离）。"""
    return bool(str(os.environ.get("SIXCODER_HOME") or "").strip())
