"""适配层的「状态码与异常映射」自检（2026-11-15）。

背景（真跑暴露）：契约让业务函数返回 `{..., "status": 201}`，而 Django 适配视图
恒返回 200 → 探针要 201/401/404，必然红，远端怎么改函数体都改不掉（它对状态码没控制权）。

钉住四件事：
1. 返回 dict 里 `status` 是整数 → 用它当 HTTP 状态，并从 body 摘掉
2. `PermissionError` → 401
3. `LookupError` → 404
4. `ValueError` → 422
并检查契约规则文本**禁止**再要求把 status 塞进返回值。

跑法：python checks/check_adapter_status.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import contract_rules as CR      # noqa: E402
from core import django_wire as W          # noqa: E402

PY = sys.executable
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


SETTINGS = '''
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "x"
DEBUG = True
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = ["django.contrib.contenttypes", "django.contrib.auth", "apps.demo"]
MIDDLEWARE = []
ROOT_URLCONF = "config.urls"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
'''

URLS = '''
from django.urls import include, path

urlpatterns = [
    path("api/v1/widgets/", include("apps.demo.urls")),
]
'''

VIEWS = '''
def ok_one(body: dict = None, authorization: str = "") -> dict:
    return {"status": 201, "id": 1, "title": "made"}


def no_auth(widget_id: int = 0) -> dict:
    raise PermissionError("认证凭据缺失")


def not_found(widget_id: int = 0) -> dict:
    raise LookupError("不存在")


def bad_input(body: dict = None, authorization: str = "") -> dict:
    raise ValueError("参数不合法")
'''

REQ = r'''
import os, sys, json, django
sys.path.insert(0, __ROOT__)
os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
django.setup()
from django.test import Client

c = Client()


def p(tag, r):
    try:
        print(tag, r.status_code, json.dumps(r.json(), ensure_ascii=False))
    except Exception:
        print(tag, r.status_code, r.content.decode()[:120])


p("OK", c.post("/api/v1/widgets/ok", data="{}", content_type="application/json"))
p("AUTH", c.get("/api/v1/widgets/no-auth/1"))
p("NF", c.get("/api/v1/widgets/not-found/1"))
p("BAD", c.post("/api/v1/widgets/bad", data="{}", content_type="application/json"))
'''


def _mk(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "__init__.py").write_text("", encoding="utf-8")
    (root / "config" / "settings.py").write_text(SETTINGS, encoding="utf-8")
    (root / "config" / "urls.py").write_text(URLS, encoding="utf-8")
    (root / "manage.py").write_text(
        "import os, sys\n"
        "if __name__ == '__main__':\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n", encoding="utf-8")
    (root / "apps").mkdir()
    (root / "apps" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "demo").mkdir()
    (root / "apps" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "demo" / "apps.py").write_text(
        "from django.apps import AppConfig\n\n\n"
        "class DemoConfig(AppConfig):\n    name = 'apps.demo'\n", encoding="utf-8")
    (root / "apps" / "demo" / "views.py").write_text(VIEWS, encoding="utf-8")
    (root / "apps" / "demo" / "urls.py").write_text(
        "from django.urls import path  # noqa: F401\n\nurlpatterns: list = [\n]\n",
        encoding="utf-8")


class _Cap:
    def __init__(self, cid: str, method: str, path: str) -> None:
        self.id = cid
        self.status = "absent"
        self.funcs = 1
        self.modules: list[str] = ["apps/demo/views.py"]
        self.probe = {"kind": "http", "app": "config.asgi:application",
                      "steps": [{"method": method, "path": path}]}


class _BP:
    def __init__(self, caps) -> None:
        self.caps = caps
        self.goal = "adapter"


def main() -> int:
    print("[1] 契约规则：状态码归传输层")
    rules = CR.apply("# 契约\n", "django")
    ck("要求 PermissionError → 401", "PermissionError" in rules and "401" in rules)
    ck("要求 LookupError → 404", "LookupError" in rules and "404" in rules)
    ck("禁止在返回值里塞 status", "在返回值里塞 `status`" in rules, rules[-260:])

    print()
    try:
        import django  # noqa: F401
    except ImportError:
        # 这条自检要真起 django 项目验"状态码归传输层"，引擎内置 Python 不带 django
        # （requirements.txt 也没有——django 是**用户项目**的依赖，不是引擎的）。
        # 缺依赖就如实跳过，别把"没跑成"记成"跑红了"（2026-11-23）。
        print("[2] 适配层：本环境没有 django → SKIP（pip install django 后可跑，不算通过）")
        print()
        print("SKIP：django 适配层未覆盖（缺依赖）；[1] 契约规则已通过")
        return 0
    print("[2] 适配层：真发请求看状态码")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _mk(root)
        bp = _BP([
            _Cap("cap.w.ok", "POST", "/api/v1/widgets/ok"),
            _Cap("cap.w.auth", "GET", "/api/v1/widgets/no-auth/{widget_id}"),
            _Cap("cap.w.nf", "GET", "/api/v1/widgets/not-found/{widget_id}"),
            _Cap("cap.w.bad", "POST", "/api/v1/widgets/bad"),
        ])
        touched = W.wire(root, bp)
        print("      touched =", touched)
        urls = (root / "apps" / "demo" / "urls.py").read_text(encoding="utf-8")
        ck("四条路由都写进去", len(touched) == 1 and urls.count("_engine_") >= 4,
           urls[-400:])

        chk = subprocess.run([PY, "manage.py", "check"], cwd=str(root),
                             capture_output=True, text=True, timeout=120)
        ck("manage.py check 通过", chk.returncode == 0,
           (chk.stdout or "") + (chk.stderr or ""))

        req = root / "_req.py"
        req.write_text(REQ.replace("__ROOT__", repr(str(root))), encoding="utf-8")
        r = subprocess.run([PY, str(req)], cwd=str(root), capture_output=True,
                           text=True, timeout=120)
        out = (r.stdout or "") + (r.stderr or "")
        print("      " + "\n      ".join(out.strip().splitlines()[-6:]))

        ck("业务返回 status=201 → HTTP 201", "OK 201" in out, out[-400:])
        ck("status 已从 body 摘掉",
           "OK 201" in out and '"status"' not in out.split("OK 201", 1)[1][:120], out[-300:])
        ck("PermissionError → 401", "AUTH 401" in out, out[-400:])
        ck("LookupError → 404", "NF 404" in out, out[-400:])
        ck("ValueError → 422", "BAD 422" in out, out[-400:])
        ck("body 里带上原因（远端能看懂）",
           "认证凭据缺失" in out and "不存在" in out, out[-400:])

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
