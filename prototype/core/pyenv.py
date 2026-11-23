"""子进程在用户项目里执行代码的**统一入口**（2026-11-23）。

为什么需要它：内置 Python 是嵌入式 `._pth` 隔离模式——`sys.path` 固定为解释器自带
目录、**不读 PYTHONPATH**、连"脚本所在目录"都不自动加入。任何"起子进程去 import
用户项目"的地方，如果裸写：

    [python, "-c", "import app.main"]          # ← 安装版里必然 ModuleNotFound
    [python, "-B", "main.py"]                  # ← main.py 里的同目录 import 也失败
    [python, "-m", "pytest"]                   # ← tests/ 里 from store import ... 失败

都会静默失败，然后被误判成"产物/代码有问题"（这一轮为此修了 7 处）。

所以把三种形态的构造收在这里，配套 `checks/check_inject_guard.py` 扫描裸写。
新代码用这里的函数，就不会再踩第 8 次。
"""
from __future__ import annotations

from pathlib import Path


def root(ws: str | Path) -> Path:
    """工作区绝对路径（统一 resolve，避免相对路径在不同 cwd 下漂移）。"""
    return Path(ws).resolve()


def path_arg(ws: str | Path) -> str:
    """给 pytest `-o pythonpath=` 用的路径：**必须正斜杠**。

    Windows 反斜杠会被 `os.pathsep`（`;`）的解析逻辑当转义吃掉，导致路径失效
    （实测：反斜杠下 pytest 仍是 `1 error during collection`）。
    """
    return str(root(ws)).replace("\\", "/")


def import_argv(ws: str | Path, module: str) -> list[str]:
    """`python -c "…import <module>"`：先注入工作区再导入。"""
    return [
        _py(),
        "-c",
        f"import sys; sys.path.insert(0, {str(root(ws))!r}); import {module}",
    ]


def script_argv(script: str | Path, args=(), *, root_dir: str | Path | None = None) -> list[str]:
    """`python <script> <args…>` 的等价形态：用 runpy 注入脚本目录再执行。

    `__file__` / `__name__` / `sys.argv` 都与正常解释器一致，`sys.exit(n)` 的退出码
    照常传播；`root_dir` 给了就同时把工作区根也注入（同级包/根模块都能导入）。
    """
    sp = Path(script).resolve()
    extra = [str(root(root_dir))] if root_dir is not None else []
    code = [
        "import sys, runpy",
        f"sys.path[:0] = {([*extra, str(sp.parent)])!r}",
        f"sys.argv = {([str(sp), *[str(a) for a in args]])!r}",
        f"runpy.run_path({str(sp)!r}, run_name='__main__')",
        "",
    ]
    return [_py(), "-B", "-c", "\n".join(code)]


def pytest_argv(ws: str | Path, targets=(), *, quiet: bool = True,
                extra=()) -> list[str]:
    """pytest 调用：必须带 `-o pythonpath=<工作区>`（正斜杠）。"""
    argv = [_py(), "-m", "pytest"]
    if quiet:
        argv.append("-q")
    argv += ["-o", f"pythonpath={path_arg(ws)}"]
    argv += [str(t) for t in (extra or ())]
    argv += [str(t) for t in (targets or ())]
    return argv


def _py() -> str:
    """当前解释器（不写死 `python`：作者/引擎必须跑同一个环境）。"""
    import sys
    return sys.executable


__all__ = ["root", "path_arg", "import_argv", "script_argv", "pytest_argv"]
