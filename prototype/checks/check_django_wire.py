# -*- coding: utf-8 -*-
"""Django 接线判据（真建一个 mini Django 工程，真跑 manage.py check + 真发请求）。

为什么必须真跑：URLconf 的错误**静态看不出来** —— `path()` 拼错、视图不存在、
`urlpatterns +=` 漏了，都只有 import/check 时才炸。而接线是批量写盘，
写坏一次就是整站 500，所以守卫（快照→写→check→回滚）必须被真的触发过一次。
"""
import os
import subprocess
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "")   # 防宿主 settings 干扰

from core import django_wire as W  # noqa: E402

FAILS = []
PY = sys.executable


def ck(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (("  — " + str(detail)[:300]) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def make_project(root: Path) -> Path:
    """最小可用 Django 工程：config/ + apps/demo/。"""
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "apps" / "demo").mkdir(parents=True, exist_ok=True)
    (root / "manage.py").write_text(
        "import os, sys\n"
        "def main():\n"
        "    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')\n"
        "    from django.core.management import execute_from_command_line\n"
        "    execute_from_command_line(sys.argv)\n"
        "if __name__ == '__main__':\n"
        "    main()\n", encoding="utf-8")
    (root / "config" / "__init__.py").write_text("", encoding="utf-8")
    (root / "config" / "settings.py").write_text(
        "SECRET_KEY = 'x'\nDEBUG = True\nALLOWED_HOSTS = ['*']\n"
        "INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth', 'apps.demo']\n"
        "MIDDLEWARE = []\nROOT_URLCONF = 'config.urls'\n"
        "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}\n"
        "USE_TZ = True\nDEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'\n",
        encoding="utf-8")
    (root / "config" / "urls.py").write_text(
        "from django.urls import include, path\n"
        "urlpatterns = [\n"
        "    path('api/v1/widgets/', include('apps.demo.urls')),\n"
        "    path('api/v1/people/', include('apps.demo.urls_people')),\n"
        "]\n", encoding="utf-8")
    (root / "apps" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "demo" / "apps.py").write_text(
        "from django.apps import AppConfig\n\n\n"
        "class DemoConfig(AppConfig):\n"
        "    name = 'apps.demo'\n", encoding="utf-8")
    (root / "apps" / "demo" / "views.py").write_text(
        "from django.http import JsonResponse\n\n\n"
        "def create_widget(body: dict = None, authorization: str = '') -> dict:\n"
        "    return {'id': 1, 'title': (body or {}).get('title', '')}\n\n\n"
        "def get_widget(widget_id: int) -> dict:\n"
        "    return {'id': widget_id, 'title': 'w'}\n\n\n"
        "def teachers() -> dict:\n"
        "    return {'teachers': []}\n", encoding="utf-8")
    (root / "apps" / "demo" / "urls.py").write_text(
        "from django.urls import path  # noqa: F401\n\n"
        "urlpatterns: list = [\n"
        "]\n", encoding="utf-8")
    # 同一个 app 的**第二个** url 模块（真实项目里是 apps.accounts.urls_people）：
    # 端点必须写进它自己那个文件，写进 urls.py 就会挂到别的前缀下 → 404。
    (root / "apps" / "demo" / "urls_people.py").write_text(
        "from django.urls import path  # noqa: F401\n\n"
        "urlpatterns: list = [\n"
        "]\n", encoding="utf-8")
    return root


class _Cap:
    """能力替身。**探针形状照抄真实蓝图**（`steps[].json` 带请求体）。

    以前这里写的是 `body_keys` —— 蓝图上根本没有这个键，于是自检"绿"而生产
    "全跳过"（2026-11-15：76 条端点一条没接上，报"没有可用的处理函数"）。
    自检用的数据形状必须等于真实形状，否则它只是在测自己。
    """

    def __init__(self, cid, method, path, keys=None, status=200, json_body=None):
        step = {"method": method, "path": path, "expect_status": status}
        if json_body is not None:
            step["json"] = json_body
        elif keys:
            step["json"] = {k: "x" for k in keys}       # 与真实蓝图同形
        self.id = cid
        self.probe = {"kind": "http", "steps": [step]}


class _BP:
    def __init__(self, caps):
        self.caps = caps


def run_check(ws: Path) -> tuple[int, str]:
    r = subprocess.run([PY, "manage.py", "check"], cwd=str(ws),
                       capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        ws = make_project(Path(td))

        print("[1] 项目识别与映射")
        ck("is_django 认出 Django 工程", W.is_django(ws) is True)
        ck("非 Django 目录不被误判", W.is_django(PROTO) is False)
        amap = W.app_url_map(ws)
        ck("从 config/urls.py 反推前缀（键是 urls 模块）",
           amap.get("apps/demo/urls.py") == "/api/v1/widgets/", amap)
        ck("同一 app 的第二个 url 模块也进映射（不吞前缀）",
           amap.get("apps/demo/urls_people.py") == "/api/v1/people/", amap)
        ck("候选模块含 apps/*/views.py",
           "apps/demo/views.py" in W.candidate_modules(ws), W.candidate_modules(ws))

        print()
        print("[2] 路径换算（Django 形态）")
        ck("集合端点 → 空 pattern", W._rel_pattern("/api/v1/widgets/", "/api/v1/widgets/") == "")
        ck("带 id 端点 → <int:widget_id>（尾斜杠跟随端点）",
           W._rel_pattern("/api/v1/widgets/{widget_id}",
                          "/api/v1/widgets/") == "<int:widget_id>",
           W._rel_pattern("/api/v1/widgets/{widget_id}",
                          "/api/v1/widgets/"))
        ck("端点自带尾斜杠时保留",
           W._rel_pattern("/api/v1/widgets/{widget_id}/",
                          "/api/v1/widgets/") == "<int:widget_id>/")
        ck("跨 app 的端点返回 None",
           W._rel_pattern("/api/v1/other/", "/api/v1/widgets/") is None)

        print()
        print("[2b] 「路由已存在」识别（2026-11-19：作者区把视图写在 urls.py 里，"
              "接线层原本误报『X 里没有可用的处理函数』）")
        with tempfile.TemporaryDirectory() as _td2:
            w2 = Path(_td2)
            (w2 / "apps" / "demo").mkdir(parents=True)
            (w2 / "apps" / "demo" / "urls.py").write_text(
                "from django.http import JsonResponse\n"
                "from django.urls import path\n\n\n"
                "def _p_list(request):\n    return JsonResponse({'p': []})\n\n\n"
                "def _p_submit(request, pk):\n    return JsonResponse({'id': pk})\n\n\n"
                "urlpatterns: list = [\n"
                "    path(\"\", _p_list),\n"
                "    path(\"tags/\", _p_list),\n"
                "    path(\"<int:pk>/submit/\", _p_submit),\n"
                "]\n", encoding="utf-8")
            U = "apps/demo/urls.py"
            ck("根路径（rel 为空）已挂 → 识别为已存在",
               W._route_exists(w2, U, ""))
            ck("字面路由 tags/ 已挂 → 识别为已存在",
               W._route_exists(w2, U, "tags/"))
            ck("模板占位符 ${pk} 能对上 <int:pk> 参数段",
               W._route_exists(w2, U, "${pk}/submit/"))
            ck("不存在的路由 → 识别为未挂（不误判成已挂）",
               not W._route_exists(w2, U, "ghost/"))
            ck("段数不同不算已挂（submit/ 对 <int:pk>/submit/ 要判未挂）",
               not W._route_exists(w2, U, "submit/"))
            # 全已挂时 wire() 必须静默（零误导性 notice）——这是本轮修的真 bug
            _ev: list = []
            (w2 / "config").mkdir()
            (w2 / "config" / "settings.py").write_text(
                "SECRET_KEY = 'x'\nROOT_URLCONF = 'config.urls'\n", encoding="utf-8")
            (w2 / "manage.py").write_text("", encoding="utf-8")
            (w2 / "config" / "urls.py").write_text(
                "from django.urls import include, path\n"
                "urlpatterns = [path('api/v1/people/', include('apps.demo.urls'))]\n",
                encoding="utf-8")
            _bp2 = _BP([_Cap("cap.people.list", "GET", "/api/v1/people/"),
                        _Cap("cap.people.tags", "GET", "/api/v1/people/tags/"),
                        _Cap("cap.people.submit", "POST",
                             "/api/v1/people/${pk}/submit/")])
            _t = W.wire(w2, _bp2, emit=lambda k, d=None: _ev.append((k, d)))
            _notes = [d for k, d in _ev if k == "notice"]
            ck("全部路由已存在时 wire 不写盘", _t == [], _t)
            ck("全部路由已存在时**不产出**『没有可用的处理函数』误报",
               not any("没有可用的处理函数" in str(x) for x in _notes), str(_notes)[:400])
            _carry = [d for k, d in _ev if k == "django_wire"]
            ck("事件里如实报 already 计数（3 条已挂）",
               bool(_carry) and _carry[0].get("already") == 3,
               str(_carry)[:300])

        print()
        try:
            import django  # noqa: F401
        except ImportError:
            # [3] 写盘后要用 `manage.py check` 做复验守卫；没有 django 时守卫必然失败 →
            # 回滚快照 → touched 为空，看起来像"接线逻辑坏了"，其实是缺依赖（2026-11-23）。
            print("  SKIP  [3]/[4] 真接线需要 django（写盘后 manage.py check 守卫），本环境没有")
            print()
            print("SKIP：django 真接线未覆盖（缺依赖）；[1][2][2b] 已通过")
            return 0
        print("[3] 真接线（写 urls.py → manage.py check）")
        bp = _BP([_Cap("cap.widgets.create", "POST", "/api/v1/widgets/",
                       ["title", "summary"]),
                  _Cap("cap.widgets.get", "GET", "/api/v1/widgets/{widget_id}"),
                  _Cap("cap.people.teachers", "GET", "/api/v1/people/teachers/"),
                  _Cap("cap.widgets.missing", "GET", "/api/v1/widgets/404case/", status=404)])
        touched = W.wire(ws, bp)
        urls = (ws / "apps" / "demo" / "urls.py").read_text(encoding="utf-8")
        urls_people = (ws / "apps" / "demo" / "urls_people.py").read_text(encoding="utf-8")
        print("      touched =", touched)
        print("      ---- urls.py ----")
        for line in urls.strip().splitlines():
            print("      " + line)
        ck("写入了两个 url 模块", sorted(touched) == ["apps/demo/urls.py",
                                                      "apps/demo/urls_people.py"], touched)
        ck("第二个 url 模块真的接上了（写对文件，不是串号）",
           "teachers" in urls_people and "teachers" not in urls, urls_people[:500])
        ck("标记块存在", W.MARK_BEGIN in urls and W.MARK_END in urls)
        # 别名按**条目 slug** 生成（同一函数可能服务多条路由/多个 app，见 2026-11-15 修复）
        ck("接了 create_widget（导入时绑定）",
           "import create_widget as _h_" in urls and "_h_" in urls and "create_widget(" in urls,
           urls[:700])
        ck("接了 get_widget（带 <int:widget_id>）",
           "<int:widget_id>" in urls and "import get_widget as _h_" in urls, urls)
        ck("期望 404 的端点不接线（它是判据不是能力）",
           "404case" not in urls, "404 端点是'应该失败'的用例，不该注册路由")
        code, out = run_check(ws)
        ck("manage.py check 通过", code == 0, out)

        print()
        print("[4] 幂等与作者区保护")
        before = urls
        W.wire(ws, bp)
        after = (ws / "apps" / "demo" / "urls.py").read_text(encoding="utf-8")
        ck("重跑结果一致（幂等，不叠加）", before == after)
        ck("标记块只有一份", after.count(W.MARK_BEGIN) == 1, after.count(W.MARK_BEGIN))

        # 作者自己在标记块上方加了路由 → 不许被覆盖
        src = after.replace("urlpatterns += _ENGINE_URLPATTERNS", "urlpatterns += _ENGINE_URLPATTERNS")
        author = ("from django.urls import path  # noqa: F401\n"
                  "from django.http import HttpResponse\n\n\n"
                  "def _mine(request):\n    return HttpResponse('ok')\n\n\n"
                  "urlpatterns: list = [\n    path('mine/', _mine),\n]\n\n")
        (ws / "apps" / "demo" / "urls.py").write_text(author + W.MARK_BEGIN +
                                                     after.split(W.MARK_BEGIN, 1)[1],
                                                     encoding="utf-8")
        W.wire(ws, bp)
        final = (ws / "apps" / "demo" / "urls.py").read_text(encoding="utf-8")
        ck("作者区（标记块之前）原样保留", "path('mine/', _mine)" in final, final[:400])
        code, out = run_check(ws)
        ck("作者区 + 引擎块共存时 check 仍通过", code == 0, out)

        print()
        print("[5] 守卫：写坏必须回滚")
        good = (ws / "apps" / "demo" / "urls.py").read_text(encoding="utf-8")
        orig = W._find_handler
        W._find_handler = lambda *a, **k: ("apps/demo/views.py",   # noqa: E731
                                          "ghost_view_does_not_exist")
        try:
            touched_bad = W.wire(ws, bp)
        finally:
            W._find_handler = orig
        now = (ws / "apps" / "demo" / "urls.py").read_text(encoding="utf-8")
        ck("接线失败时返回空清单", touched_bad == [], touched_bad)
        ck("回滚到接线前（没有 ghost_view 残留）", now == good and "ghost_view" not in now,
           now[-300:])

        print()
        print("[6] 真发请求（接线真的生效）")
        req = (
            "import os, sys, django\n"
            "sys.path.insert(0, %r)\n"
            "os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'\n"
            "django.setup()\n"
            "from django.test import Client\n"
            # **enforce_csrf_checks=True**：默认 Client 不校验 CSRF，于是"探针真发 POST 得 403"
            # 这类事故在自检里永远看不见（2026-11-15 实测：真机全 403、自检全绿）。
            # 两种客户端口径必须一致 —— 自检要比探针更严，不能更松。
            "c = Client(enforce_csrf_checks=True)\n"
            "r = c.post('/api/v1/widgets/', data='{\"title\": \"T1\"}',"
            " content_type='application/json')\n"
            "print('POST', r.status_code, r.content.decode())\n"
            "r2 = c.get('/api/v1/widgets/7')\n"
            "print('GET', r2.status_code, r2.content.decode())\n"
            # 同一路径 GET+POST 合并成一个视图后，方法不符要 405（而不是命中错的处理函数）
            "r3 = c.put('/api/v1/widgets/7')\n"
            "print('PUT', r3.status_code)\n"
        ) % str(ws)
        p = subprocess.run([PY, "-c", req], cwd=str(ws), capture_output=True,
                           text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        print("      " + "\n      ".join(out.strip().splitlines()[-6:]))
        ck("POST /api/v1/widgets/ 返回 200 且回显 title（CSRF 已豁免）",
           "POST 200" in out and "T1" in out, out[-500:])
        ck("GET /api/v1/widgets/7 返回 200 且 id=7",
           "GET 200" in out and '"id": 7' in out.replace(" ", " "), out[-500:])
        ck("未接的方法返回 405（不是命中错的处理函数）", "PUT 405" in out, out[-300:])

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
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
    sys.exit(main())
