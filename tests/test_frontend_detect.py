"""前端栈识别测试（P2）。

## 背景

做 P2 时先写了一套自己的前端骨架模板，后来发现**生态包已经提供了**
（prototype/stacks/frontend_spa/templates/，连 react-router-dom 都配好了），
于是撤销重复实现，只保留真正的缺口修复。

## 保留下来的两个修复

1. stack.detect 原来只认 manage.py / app/main.py，不认前端特征
   -> 生成出的 React 项目被当成 fastapi
2. judges.project_stack 调 from_workspace().name，而它会回退到 fastapi 适配器
   -> detect 明明返回 frontend_spa，name 却是 fastapi
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import judges, stack   # noqa: E402


def _mk(files: dict) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="fedet-"))
    for rel, txt in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    return ws


def test_vite_config_means_frontend():
    ws = _mk({"vite.config.js": "export default {}",
              "package.json": json.dumps({"dependencies": {"react": "^18"}})})
    assert stack.detect(ws) == "frontend_spa"
    assert judges.project_stack(ws) == "frontend_spa"


def test_react_dependency_means_frontend():
    ws = _mk({"package.json": json.dumps({"dependencies": {"react": "^18", "react-dom": "^18"}})})
    assert stack.detect(ws) == "frontend_spa"


def test_vue_dependency_means_frontend():
    ws = _mk({"package.json": json.dumps({"dependencies": {"vue": "^3"}})})
    assert stack.detect(ws) == "frontend_spa"


def test_fastapi_still_detected():
    """关键回归：加了前端识别不能把 FastAPI 抢走。"""
    ws = _mk({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
    assert stack.detect(ws) == "fastapi"
    assert judges.project_stack(ws) == "fastapi"


def test_django_still_detected():
    ws = _mk({"manage.py": "# django\n",
              "mysite/settings.py": "SECRET_KEY = 'x'\n"})
    assert stack.detect(ws) == "django"


def test_plain_package_json_is_not_frontend():
    """只有 package.json 但没 react/vue 依赖 -> 不是前端（可能是 Node 后端）。"""
    ws = _mk({"package.json": json.dumps({"dependencies": {"express": "^4"}})})
    assert stack.detect(ws) != "frontend_spa"


def test_project_stack_returns_pack_name_not_default():
    """核心回归：project_stack 不能用 from_workspace().name
    （那条路会回退到 fastapi 适配器，把 frontend_spa 抹成 fastapi）。"""
    ws = _mk({"vite.config.js": "export default {}"})
    got = judges.project_stack(ws)
    assert got == "frontend_spa", f"应返回生态包名，实际 {got}"
