"""多服务系统入口（**由引擎生成，不要改**）。

与 FastAPI 入口同一原则：只做装配（路由 + worker 挂载），不写业务。
业务在 app/services/ 下；后台任务在 app/tasks/ 下（由 Celery 发现）。

单个路由模块 import 失败不拖垮整个应用：它自己的端点 404，别的照常服务。
"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

from fastapi import FastAPI

app = FastAPI(title="SixCoder Microservice")

# 自动挂载 app/api/*.py 里定义的 router
_API_DIR = Path(__file__).resolve().parent / "api"
if _API_DIR.is_dir():
    for _f in sorted(_API_DIR.glob("*.py")):
        if _f.name.startswith("_"):
            continue
        _mod = f"{__package__}.api.{_f.stem}"
        try:
            if importlib.util.find_spec(_mod) is None:
                continue
            _m = importlib.import_module(_mod)
        except Exception:                              # noqa: BLE001
            continue                                   # 坏模块跳过，见模块 docstring
        _r = getattr(_m, "router", None)
        if _r is not None:
            try:
                app.include_router(_r)
            except Exception:                          # noqa: BLE001
                continue


@app.get("/health")
def health() -> dict:
    return {"ok": True}
