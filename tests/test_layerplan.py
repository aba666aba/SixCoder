"""LayerPlan 生成计划测试（P0）。

## 证明什么

1. 三种既有栈（fastapi/django/frontend）的落点解析与旧逻辑**逐字一致**
2. 新增的 cli 计划能独立工作（证明抽象真的可扩展）
3. 取不到计划时返回 None，调用方回退到旧逻辑（行为不变的保证）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import fullgen, layerplan   # noqa: E402


class FakeCap:
    def __init__(self, modules, status="done"):
        self.modules = modules
        self.status = status


class FakeBP:
    def __init__(self, caps):
        self.caps = caps


def _mkws(files: dict) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="lp-"))
    for rel, txt in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt, encoding="utf-8")
    return ws


FASTAPI_WS = {
    "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
    "app/models.py": "x=1\n",
    "app/schemas.py": "y=2\n",
    "app/services/s1.py": "a=1\n",
    "app/api/a1.py": "b=1\n",
}

DJANGO_WS = {
    "manage.py": "# django\n",
    "mysite/settings.py": "SECRET_KEY='x'\n",
    "apps/core/views.py": "v=1\n",
    "apps/core/models.py": "m=1\n",
}


def test_fastapi_plan_matches_legacy():
    ws = _mkws(FASTAPI_WS)
    bp = FakeBP([FakeCap(["app/models.py", "app/services/s1.py", "app/api/a1.py"])])
    plan = layerplan.plan_for(ws)
    assert plan is not None and plan.stack == "fastapi"
    ctx = layerplan.LayerContext(ws=ws, blueprint=bp)
    for layer in ("models", "services", "api"):
        legacy = fullgen._layer_targets(ws, bp, layer, ("app/models.py", "app/schemas.py"))
        assert plan.targets(ctx, layer) == legacy, f"{layer} 落点与旧逻辑不一致"


def test_django_plan_matches_legacy():
    ws = _mkws(DJANGO_WS)
    bp = FakeBP([FakeCap(["apps/core/views.py", "apps/core/models.py"])])
    plan = layerplan.plan_for(ws)
    assert plan is not None and plan.stack == "django"
    ctx = layerplan.LayerContext(ws=ws, blueprint=bp)
    for layer in ("models", "services", "api"):
        legacy = fullgen._layer_targets(ws, bp, layer, None)
        assert plan.targets(ctx, layer) == legacy, f"{layer} 落点与旧逻辑不一致"


def test_django_skips_models_layer():
    """Django 不重写 models（会冲掉已有模型和迁移）—— 这是刻意行为。"""
    ws = _mkws(DJANGO_WS)
    plan = layerplan.plan_for(ws)
    ctx = layerplan.LayerContext(ws=ws, blueprint=FakeBP([]))
    assert plan.targets(ctx, "models") == []


def test_cli_plan_is_independent():
    """CLI 计划是新加的能力：证明抽象可扩展，不是只为既有三种写的。"""
    ws = _mkws({"main.py": "print(1)\n", ".sixcoder/stack.json": '{"stack":"cli"}'})
    reg = layerplan.default_registry()
    plan = reg.get("cli")
    assert plan is not None, "cli 生成计划未注册"
    bp = FakeBP([FakeCap(["main.py", "cmd/add.py"]), FakeCap(["unused.js"])])
    ctx = layerplan.LayerContext(ws=ws, blueprint=bp)
    got = plan.targets(ctx, "cli")
    assert got == ["cmd/add.py", "main.py"], f"CLI 落点不对: {got}"


def test_frontend_plan_only_components():
    ws = _mkws({"package.json": "{}", "src/App.jsx": "x\n"})
    plan = layerplan.default_registry().get("frontend")
    bp = FakeBP([FakeCap(["src/App.jsx", "src/util.ts", "app/models.py"])])
    ctx = layerplan.LayerContext(ws=ws, blueprint=bp)
    got = plan.targets(ctx, "ui")
    assert got == ["src/App.jsx", "src/util.ts"], got
    assert plan.targets(ctx, "models") == [], "前端不该有 models 层"


def test_unknown_stack_returns_none():
    """取不到计划返回 None —— 调用方据此回退旧逻辑，这是行为不变的保证。"""
    ws = _mkws({"x.zzz": "?"})
    plan = layerplan.plan_for(ws)
    assert plan is None or plan.stack in ("fastapi",), (
        "未知栈不该蹦出一个奇怪的计划"
    )


def test_duplicate_plan_rejected():
    reg = layerplan.PlanRegistry()
    reg.register(layerplan.FASTAPI_PLAN)
    try:
        reg.register(layerplan.FASTAPI_PLAN)
    except ValueError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复注册应报错")


def test_plan_without_stack_rejected():
    reg = layerplan.PlanRegistry()
    bad = layerplan.SimplePlan(stack="", layers=(), resolver=lambda c, l: [])
    try:
        reg.register(bad)
    except ValueError as exc:
        assert "stack" in str(exc)
    else:
        raise AssertionError("空 stack 应报错")
