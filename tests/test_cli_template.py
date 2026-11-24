"""CLI 骨架测试（P1）。

## 证明什么

1. 骨架能跑（--help / 子命令 / 退出码）
2. 坏模块不拖垮整体（这是 Web 入口"坏模块跳过"原则的对应物）
3. 退出码约定：0 成功 / 1 业务失败 / 2 用法错误
4. 子命令参数能传到模块（实测踩过的坑）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import cli_template as T   # noqa: E402


def _mkproj(tmp: Path, commands: dict) -> Path:
    (tmp / "cmd").mkdir(parents=True, exist_ok=True)
    (tmp / "cmd" / "__init__.py").write_text("", encoding="utf-8")
    (tmp / "main.py").write_text(T.render_entry(commands), encoding="utf-8")
    return tmp


def _run(ws: Path, args: list) -> dict:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run([sys.executable, "main.py"] + args, cwd=str(ws),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30, env=env)
    return {"code": p.returncode, "out": p.stdout or "", "err": p.stderr or ""}


def test_template_renders_valid_python():
    src = T.render_entry({"cmd.a": "a"})
    compile(src, "<skeleton>", "exec")


def test_help_works():
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {})
        r = _run(ws, ["--help"])
        assert r["code"] == 0, r
        assert "usage" in r["out"].lower()


def test_no_args_exits_2():
    """没给子命令 = 用法错误 -> 2。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {})
        assert _run(ws, [])["code"] == 2


def test_unknown_command_exits_2():
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {"cmd.a": "a"})
        assert _run(ws, ["nope"])["code"] == 2


def test_command_runs_and_gets_positional_args():
    """实测踩过的坑：不给位置参数，`add 写测试` 会被判 unrecognized。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {"cmd.add": "add"})
        (ws / "cmd" / "add.py").write_text(
            "def run(args):\n"
            "    print(\"GOT:\" + \"|\".join(args.items))\n"
            "    return 0\n", encoding="utf-8")
        r = _run(ws, ["add", "写测试", "extra"])
        assert r["code"] == 0, r
        assert "GOT:写测试|extra" in r["out"], r


def test_business_failure_exits_1():
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {"cmd.bad": "bad"})
        (ws / "cmd" / "bad.py").write_text(
            "def run(args):\n"
            "    raise ValueError(\"业务失败\")\n", encoding="utf-8")
        r = _run(ws, ["bad"])
        assert r["code"] == 1, r
        assert "业务失败" in r["err"]


def test_broken_module_does_not_kill_others():
    """单个模块 import 失败只让它自己不可用 —— 其它命令照常。"""
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {"cmd.ok": "ok", "cmd.broken": "broken"})
        (ws / "cmd" / "ok.py").write_text(
            "def run(args):\n    print(\"ok works\")\n    return 0\n", encoding="utf-8")
        (ws / "cmd" / "broken.py").write_text("def oops(\n", encoding="utf-8")
        # 好命令照常
        r_ok = _run(ws, ["ok"])
        assert r_ok["code"] == 0
        assert "ok works" in r_ok["out"]
        # 坏命令报不可用，退出 1
        r_bad = _run(ws, ["broken"])
        assert r_bad["code"] == 1
        assert "不可用" in r_bad["err"]
        # 帮助里能看到它（不静默消失）
        r_help = _run(ws, ["--help"])
        assert "broken" in r_help["out"]
        assert "unavailable" in r_help["out"]


def test_return_value_becomes_exit_code():
    with tempfile.TemporaryDirectory() as td:
        ws = _mkproj(Path(td), {"cmd.rc": "rc"})
        (ws / "cmd" / "rc.py").write_text(
            "def run(args):\n    return 3\n", encoding="utf-8")
        assert _run(ws, ["rc"])["code"] == 3
