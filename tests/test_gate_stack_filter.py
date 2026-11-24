"""闸门按栈过滤测试（阶段 3 第二块）。

## 解决什么

agent._gate_node 以前是硬编码的闸门序列，其中 API / SQL行 / 安全 三个
假设"这是个 HTTP 服务"。CLI 项目跑它们必然假红。

现在按栈跳过。**跳过不等于通过** —— 必须 emit 说明，且不记 ok=True。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import agent   # noqa: E402

WEB_GATES = {"gate_api", "gate_db_rows", "gate_security"}


class Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def notices(self):
        return [p.get("message", "") for k, p in self.events if k == "notice"]


def _proj(files: dict) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="gateskip-"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def test_cli_project_skips_web_gates():
    ws = _proj({".sixcoder/cli.json": json.dumps({"run": "python x.py"})})
    rec = Recorder()
    skip = agent._stack_gate_skip(ws, rec)
    assert skip == WEB_GATES, f"应跳过全部 Web 闸门，实际 {skip}"


def test_fastapi_project_skips_nothing():
    ws = _proj({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    rec = Recorder()
    skip = agent._stack_gate_skip(ws, rec)
    assert skip == set(), f"FastAPI 项目不该跳过任何闸门，实际 {skip}"


def test_skip_is_announced_not_silent():
    """跳过必须说出来 —— 静默跳过和静默通过一样有害。"""
    ws = _proj({".sixcoder/cli.json": json.dumps({"run": "python x.py"})})
    rec = Recorder()
    agent._stack_gate_skip(ws, rec)
    notices = " ".join(rec.notices())
    assert notices, "跳过没有 emit 任何 notice"
    assert "跳过" in notices
    assert "不等于通过" in notices, "必须说清跳过不等于通过"
    for g in WEB_GATES:
        assert g in notices, f"notice 里没点名 {g}"


def test_explicit_stack_json_respected():
    ws = _proj({
        ".sixcoder/stack.json": json.dumps({"stack": "cli"}),
        "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    rec = Recorder()
    assert agent._stack_gate_skip(ws, rec) == WEB_GATES


def test_django_project_keeps_web_gates():
    ws = _proj({"manage.py": "# django\n", "mysite/settings.py": "SECRET_KEY = \"x\"\n"})
    rec = Recorder()
    assert agent._stack_gate_skip(ws, rec) == set(), "Django 仍是 Web 项目，不该跳过"


def test_stack_detection_failure_falls_back_to_all_gates():
    """栈识别炸了要按全量跑（不猜），并 emit 说明。"""
    from unittest.mock import patch
    ws = _proj({"x.py": "pass\n"})
    rec = Recorder()
    with patch.object(agent, "Path", side_effect=agent.Path):
        # 直接让 judges 抛错
        import core.judges as J
        orig = J.project_stack
        J.project_stack = lambda w: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            skip = agent._stack_gate_skip(ws, rec)
        finally:
            J.project_stack = orig
    assert skip == set(), "识别失败时应跑全量"
    assert any("栈识别失败" in n for n in rec.notices())
