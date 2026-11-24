"""栈识别桥接测试（阶段 3 第一块）。

## 解决什么

stack.py 的 from_workspace 只认 fastapi / django。
一个 CLI 项目会被判成 fastapi，于是跑一堆 HTTP 判据、全红 ——
用户看到的却是"你写错了"，其实是引擎不认识这种项目。

judges.project_stack 先认 cli，再交给 stack.py。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import judges as J   # noqa: E402


def _proj(files: dict) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="stackdet-"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def test_explicit_stack_json_wins():
    ws = _proj({
        ".sixcoder/stack.json": json.dumps({"stack": "cli"}),
        # 即使有 fastapi 的实物特征，显式配置也优先
        "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    })
    assert J.project_stack(ws) == "cli"


def test_cli_config_detected_without_stack_json():
    ws = _proj({
        ".sixcoder/cli.json": json.dumps({"run": "python x.py"}),
    })
    assert J.project_stack(ws) == "cli"


def test_fastapi_detected_via_stack_module():
    ws = _proj({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    assert J.project_stack(ws) == "fastapi"


def test_django_detected_via_stack_module():
    ws = _proj({"manage.py": "# django\n", "mysite/settings.py": "SECRET_KEY = \"x\"\n"})
    assert J.project_stack(ws) == "django"


def test_cli_project_gets_no_http_judges():
    """核心断言：CLI 项目绝不能拿到 HTTP 判据。"""
    ws = _proj({".sixcoder/cli.json": json.dumps({"run": "python x.py"})})
    name, js = J.judges_for_project(ws)
    keys = {j.key for j in js}
    assert name == "cli"
    for http_only in ("api", "db_rows", "security", "field_map"):
        assert http_only not in keys, f"{http_only} 不该出现在 CLI 项目上"
    for cli_key in ("cli.exit_code", "cli.stdout", "cli.artifact"):
        assert cli_key in keys, f"{cli_key} 应该出现在 CLI 项目上"


def test_fastapi_project_gets_http_judges_not_cli():
    ws = _proj({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    name, js = J.judges_for_project(ws)
    keys = {j.key for j in js}
    assert name == "fastapi"
    assert "api" in keys
    for cli_key in ("cli.exit_code", "cli.stdout", "cli.artifact"):
        assert cli_key not in keys


def test_judges_for_project_returns_stable_list():
    ws = _proj({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    a = [j.key for j in J.judges_for_project(ws)[1]]
    b = [j.key for j in J.judges_for_project(ws)[1]]
    assert a == b
    assert a == sorted(a)
