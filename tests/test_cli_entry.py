"""write_cli_entry 接入测试（P1）。

证明：CLI 栈下能生成可用的入口骨架，且与 Web 入口互不干扰。
"""
from __future__ import annotations

import ast
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import auto   # noqa: E402


class _Cap:
    def __init__(self, modules, status="done"):
        self.modules = modules
        self.status = status
        self.probe = {}


class _BP:
    def __init__(self, caps):
        self.caps = caps


def test_generates_entry_from_blueprint_modules():
    ws = Path(tempfile.mkdtemp(prefix="clie-"))
    (ws / ".sixcoder").mkdir()
    r = auto.write_cli_entry(ws, _BP([_Cap(["cmd/add.py", "cmd/list.py"])]))
    assert r.get("ok") is True, r
    assert r["commands"] == {"cmd.add": "add", "cmd.list": "list"}, r["commands"]
    src = (ws / "main.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert "argparse" in src


def test_empty_blueprint_still_writes_runnable_entry():
    """没命令也要生成一个能跑的入口（---help 可用），不能什么都不写。"""
    ws = Path(tempfile.mkdtemp(prefix="clie2-"))
    r = auto.write_cli_entry(ws, _BP([]))
    assert r.get("ok") is True
    src = (ws / "main.py").read_text(encoding="utf-8")
    ast.parse(src)
    assert "argparse" in src


def test_cli_commands_json_is_authoritative():
    """接线产物 cli_commands.json 优先于蓝图。"""
    ws = Path(tempfile.mkdtemp(prefix="clie3-"))
    (ws / ".sixcoder").mkdir()
    (ws / ".sixcoder" / "cli_commands.json").write_text(
        json.dumps({"cmd/wired.py": "wired", "cmd/other.py": ""}), encoding="utf-8")
    r = auto.write_cli_entry(ws, _BP([_Cap(["cmd/frombp.py"])]))
    assert r["commands"].get("cmd.wired") == "wired", r["commands"]
    assert r["commands"].get("cmd.other") == "other", "空命令名应回退成模块名"


def test_skips_main_and_descoped():
    ws = Path(tempfile.mkdtemp(prefix="clie4-"))
    (ws / ".sixcoder").mkdir()
    r = auto.write_cli_entry(ws, _BP([
        _Cap(["main.py"]),
        _Cap(["cmd/skip.py"], status="descoped"),
        _Cap(["cmd/keep.py"]),
    ]))
    assert "cmd.keep" in r["commands"]
    assert "main" not in r["commands"]
    assert "cmd.skip" not in r["commands"], "descoped 的不该进来"


def test_web_entry_untouched():
    """CLI 入口函数不能碰 Web 入口文件。"""
    ws = Path(tempfile.mkdtemp(prefix="clie5-"))
    (ws / ".sixcoder").mkdir()
    auto.write_cli_entry(ws, _BP([]))
    assert not (ws / "app" / "main.py").exists(), "不该写 app/main.py"
    assert (ws / "main.py").exists()
