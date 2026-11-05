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
        for cmd in (["git", "init", "-q"],
                    ["git", "-c", "user.name=demo", "-c", "user.email=demo@local", "add", "-A"],
                    ["git", "-c", "user.name=demo", "-c", "user.email=demo@local",
                     "commit", "-q", "-m", "init: calc + tests"]):
            subprocess.run(cmd, cwd=str(DEMO), capture_output=True, text=True)
    return DEMO
