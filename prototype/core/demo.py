"""演示仓库生成（server 与 CLI 共用）。"""
from __future__ import annotations

import subprocess
from pathlib import Path

DEMO = Path(__file__).resolve().parent.parent / "demo-repo"

FILES = {
    ".gitignore": "__pycache__/\n*.pyc\n.pytest_cache/\n.sixcoder/\nworkspaces/\n",
    "calc.py": '''"""极简计算模块（演示用）。"""


def add(a: int, b: int) -> int:
    """返回两数之和。"""
    return a + b


def sub(a: int, b: int) -> int:
    """返回两数之差。"""
    return a - b
''',
    "test_calc.py": '''from calc import add, sub


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_sub():
    assert sub(5, 3) == 2
''',
    "README.md": "# demo-repo\n\n演示仓库：验证 SixCoder 的增量开发能力。\n\n```bash\npython -m pytest -q\n```\n",
}


def ensure_demo(reset: bool = False) -> Path:
    if reset and DEMO.exists():
        import shutil
        shutil.rmtree(DEMO, ignore_errors=True)
    if not (DEMO / ".git").exists():
        DEMO.mkdir(parents=True, exist_ok=True)
        for name, content in FILES.items():
            (DEMO / name).write_text(content, encoding="utf-8")
        # Git 不是运行时硬依赖：别人机器可能没装 Git。没有 git.exe 时
        # subprocess 会抛 FileNotFoundError；以前这个异常直接把后端启动打断
        # （错误页只显示"后端未就绪"）。demo 仓库仍可用，只是没有提交历史。
        import shutil
        try:
            from . import runtimes as _runtimes
            _runtimes.ensure_on_path("git")     # 安装器勾选的 MinGit 也能用
        except Exception:                       # noqa: BLE001
            pass
        if shutil.which("git"):
            try:
                for cmd in (["git", "init", "-q"],
                            ["git", "-c", "user.name=demo", "-c", "user.email=demo@local", "add", "-A"],
                            ["git", "-c", "user.name=demo", "-c", "user.email=demo@local",
                             "commit", "-q", "-m", "init: calc + tests"]):
                    subprocess.run(cmd, cwd=str(DEMO), capture_output=True, text=True)
            except OSError:
                # git 存在但执行失败（权限/杀软拦截）也不许拖垮后端起不来
                pass
    return DEMO
