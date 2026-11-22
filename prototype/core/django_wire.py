"""Django 项目的接线（URLconf 渲染 + 守卫）。

为什么单独一个模块而不是改 `auto.apply_web_wiring`：
那个函数的全部逻辑都建立在 **FastAPI 装饰器**上（`@router.post(...)`、`include_router`、
请求体模型）。Django 的接线是另一套形态：`path()` 条目 + `include()` 前缀。
硬塞进同一个函数只会让两边都不可读；共用的是**上游判据**
（`routefuncs` 点名表、`auto._pick_func` 选函数、`auto._wire_call` 判参数可接），
下游渲染各写各的。

**关键形态差异（2026-11-15 实测踩过）**：Django 视图签名必须是 `(request, **kwargs)`，
而业务函数是 `(body, authorization, topic_id, ...)` —— 中间**必须**有薄适配视图，
把 request 拆成业务函数的参数。直接把业务函数塞进 `path()` 的话，
`request` 会被当成 `body` 传进去 → `AttributeError: 'WSGIRequest' object has no attribute 'get'`。

约定（写进契约与蓝图，遵守即可零摩擦）：
1. **端点统一带尾斜杠**：`/api/v1/topics/`。不带斜杠会被 `APPEND_SLASH` 301 重定向，
   而 301 会把 POST 改成 GET 并丢请求体（探针直接红，且看不出原因）。
2. 每个 app 的 `urls.py` 暴露 `urlpatterns`；`config/urls.py` 用
   `path("api/v1/<seg>/", include("apps.<app>.urls"))` 挂前缀 —— **这是权威映射**，
   本模块从它反推"端点属于哪个 app"，不靠猜。
3. 引擎路由写在各 `apps/*/urls.py` 的标记块里（`MARK_BEGIN` … `MARK_END`），
   作者区在标记块之前，**永不覆盖**。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

MARK_BEGIN = "# --- 引擎补的路由（自动生成，勿手改；作者区在上方）---"
MARK_END = "# --- 引擎路由结束 ---"

INCLUDE_RE = re.compile(
    r'path\(\s*["\'](?P<prefix>[^"\']*)["\']\s*,\s*include\(\s*["\'](?P<mod>[\w.]+)["\']'
)

# 凭据参数名（与 core/auto.py 的口径一致：路由层只能给凭据字符串）
AUTH_NAMES = ("authorization", "token", "auth", "access_token")
BODY_NAMES = ("body", "data", "payload")


def is_django(ws) -> bool:
    """是不是 Django 项目：有 manage.py + 顶层配置包。"""
    ws = Path(ws)
    if not (ws / "manage.py").is_file():
        return False
    return any((ws / d / "settings.py").is_file() for d in ("config", "project", "settings"))


def app_url_map(ws) -> dict[str, str]:
    """从 `config/urls.py` 反推 {**urls 模块相对路径**: 前缀}。**唯一权威映射**。

    **键为什么是 urls 模块、不是 app 名**（2026-11-15 实测事故）：
    一个 app 可以挂多个 url 模块（本项目 `apps.accounts.urls` 挂 `/api/v1/auth/`、
    `apps.accounts.urls_people` 挂 `/api/v1/people/`）。以前用 `setdefault(app, ...)`
    只留第一个前缀 → `/api/v1/people/*` 的端点被报"没有匹配的 app 前缀"，
    **永远接不上**（三条能力结构性全红，还会被反复派单）。
    按模块做键，前缀与文件一一对应，写哪个文件就用哪个前缀，不会串号。
    """
    ws = Path(ws)
    out: dict[str, str] = {}
    for cand in ("config/urls.py", "project/urls.py", "urls.py"):
        p = ws / cand
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        for m in INCLUDE_RE.finditer(src):
            mod = m.group("mod")
            if not mod.startswith("apps."):
                continue
            rel = mod.replace(".", "/") + ".py"
            out.setdefault(rel, "/" + m.group("prefix").strip("/") + "/")
    return out


def _app_of(rel_module: str) -> str:
    """`apps/accounts/urls_people.py` → `accounts`。"""
    parts = str(rel_module or "").split("/")
    return parts[1] if len(parts) > 2 and parts[0] == "apps" else ""


def candidate_modules(ws) -> list[str]:
    """接线候选：Django 的视图写在哪。

    优先 `apps/*/views.py`（Django 惯例），其次 `apps/*/services.py`
    （业务层也可能直接当视图注册），最后 `matching_core/*.py`（纯算法包）。
    """
    ws = Path(ws)
    out: list[str] = []
    for pattern in ("apps/*/views.py", "apps/*/services.py", "matching_core/*.py"):
        for p in sorted(ws.glob(pattern)):
            if p.name == "__init__.py":
                continue
            out.append(str(p.relative_to(ws)).replace("\\", "/"))
    return out


def endpoints_from_caps(caps) -> list[dict]:
    """能力表的 http 探针 → 端点清单（只取期望 2xx 的步骤）。

    **body_keys 必须从探针的 `json` 里取**（2026-11-15 实测事故）：
    蓝图步骤写的是 `"json": {"username": "stu1", ...}`，而这里以前只读
    `st.get("body_keys")`（那个键蓝图上根本不存在）→ 端点清单里 body_keys 恒空
    → `_plan_args` 认为 `username/password/role` 路由层供给不了 → 整条被跳过，
    报"X 里没有可用的处理函数"（**误导性极强**：函数明明在）。
    后果：76 条能力的端点一条都接不上，覆盖卡在 0/76，还看不出是接线层的问题。
    """
    out: dict[tuple, dict] = {}
    for c in caps or []:
        probe = getattr(c, "probe", None) or {}
        if str(probe.get("kind") or "").lower() != "http":
            continue
        for st in probe.get("steps") or []:
            if not isinstance(st, dict):
                continue
            path = str(st.get("path") or "").split("?", 1)[0].strip()
            if not path:
                continue
            status = st.get("expect_status")
            if isinstance(status, int) and status >= 400:
                continue
            key = (str(st.get("method") or "GET").upper(), path)
            _body = st.get("json")
            if not isinstance(_body, dict):
                _body = st.get("body") if isinstance(st.get("body"), dict) else {}
            _keys = [str(k) for k in (st.get("body_keys") or [])]
            if not _keys and _body:
                _keys = [str(k) for k in _body.keys()]
            rec = out.setdefault(key, {"method": key[0], "path": path,
                                       "body_keys": [], "cap": getattr(c, "id", "")})
            for k in _keys:                       # 同端点多次出现 → 取并集
                if k not in rec["body_keys"]:
                    rec["body_keys"].append(k)
            for k, v in _body.items():            # 类型推断用（int/list/str）
                if isinstance(v, bool):
                    rec.setdefault("body_types", {}).setdefault(str(k), "bool")
                elif isinstance(v, int):
                    rec.setdefault("body_types", {}).setdefault(str(k), "int")
                elif isinstance(v, float):
                    rec.setdefault("body_types", {}).setdefault(str(k), "float")
                elif isinstance(v, list):
                    rec.setdefault("body_types", {}).setdefault(str(k), "list")
    return list(out.values())


def _rel_pattern(endpoint: str, prefix: str) -> str | None:
    """端点 → app 内相对 pattern。不属于该 app 返回 None。

    **尾斜杠跟随端点本身**，不强行补：端点写 `/widgets/{id}` 就匹配 `/widgets/7`，
    写 `/widgets/{id}/` 就匹配 `/widgets/7/`。
    强行统一成一种会跟探针（它照蓝图发请求）对不上 → 404。
    """
    ep, pf = endpoint.rstrip("/"), prefix.rstrip("/")
    if ep == pf:
        return ""
    if not ep.startswith(pf + "/"):
        return None
    seg = endpoint[len(prefix):] if endpoint.startswith(prefix) else ep[len(pf) + 1:]
    return re.sub(r"\{(\w+)\}", lambda m: "<int:" + m.group(1) + ">", seg).lstrip("/")



def _name_of(method: str, rel: str, func: str) -> str:
    base = rel.strip("/").replace("<int:", "").replace(">", "").replace("/", "_") or func
    return ("%s_%s" % (method.lower(), base)).replace("-", "_")


def _view_slug(rel: str, fallback: str) -> str:
    """路径 → 视图名（**只按路径，不按方法**）。

    为什么不吃方法（2026-11-15）：同一路径的 GET/POST 现在合成**一个**视图，
    若名字跟着第一条走（`_engine_post_students` 却同时服务 GET），
    读代码的人会以为 GET 是别人处理的 —— 名字骗人比没名字更贵。
    """
    base = rel.strip("/").replace("<int:", "").replace(">", "").replace("/", "_")
    base = (base or fallback).replace("-", "_") or "root"
    return base


def _resource_hint(path: str) -> str:
    """从端点推"资源名"，给"函数名像不像"用：`/widgets/{widget_id}` → `widget`。

    为什么需要（2026-11-15 实测）：`auto._pick_func` 要求**路径末段出现在函数名里**，
    而带参端点的末段是 `{widget_id}` → 永远匹配不上 → `get_widget` 接不上、端点是 404。
    这里把参数段剔掉、末尾单数化，再按"方法动词优先"挑。
    """
    segs = [s for s in str(path or "").strip("/").split("/")
            if s and not s.startswith(("{", "<"))]
    if not segs:
        return ""
    last = segs[-1].lower().replace("-", "_")
    if last.endswith("ies"):
        return last[:-3] + "y"
    if last.endswith("s") and not last.endswith("ss"):
        return last[:-1]
    return last


VERB_HINTS = {
    "GET": ("get_", "list_", "fetch_", "read_", "query_"),
    "POST": ("create_", "add_", "make_", "submit_", "apply_"),
    "PUT": ("update_", "set_", "edit_"),
    "PATCH": ("update_", "patch_", "set_"),
    "DELETE": ("delete_", "remove_", "cancel_"),
}


def _plan_args(src: str, fn: str, path_params: set, body_keys: set):
    """给业务函数规划"怎么用 request 供给参数" → (args 片段, 缺的参数)。

    `缺的参数` 非空 = 这个函数**当不了**这个端点的处理器（宁可 404，
    也不硬接出一个"注册成功但语义是另一件事"的路由）。
    """
    from . import auto as _auto

    sigs = _auto._module_signatures(src)
    params = list(sigs.get(fn) or [])
    defaults, _has_kwargs = _auto._func_param_defaults(src, fn)
    args: list[str] = []
    bad: list[str] = []
    for p in params:
        if p in path_params:
            args.append(f"{p}=kwargs.get(\"{p}\")")
        elif p in AUTH_NAMES:
            args.append(f'{p}=request.headers.get("Authorization", "")')
        elif p in BODY_NAMES:
            args.append(f"{p}=_b")
        elif p in body_keys:
            args.append(f'{p}=_b.get("{p}")')
        elif p in defaults:
            continue                      # 有自己的默认值 → 让函数用自己的（不塞假值）
        else:
            bad.append(p)
    return args, bad


def _render(app: str, entries: list[dict]) -> str:
    """渲染标记块：`json`/`JsonResponse` 的 import + 薄适配视图 + path() 列表。

    薄适配视图只做三件事：解请求体、拼参数、包 JSON。**不含业务判定**。
    """
    # **导入时绑定**处理函数（不是 `_views.xxx` 在函数体里查属性）：
    # 实测 2026-11-15：写成 `_views.ghost(...)` 时，函数不存在要到**请求时**才炸，
    # `manage.py check` 完全看不出来 → 守卫（快照→写→check→回滚）形同虚设。
    # 改成 `from ... import ghost as _h_ghost` 后，缺函数在 **import 阶段**就报错
    # → check 失败 → 自动回滚。**错误要暴露在最早能暴露的地方。**
    # **按处理函数所在的模块 import，不是按 URL 前缀的 app**（2026-11-15 实测）：
    # `/api/v1/prefs/` 的处理函数可能被写在 `apps/accounts/views.py` 里（同一份认证代码），
    # 旧实现一律 `from apps.<app>.views import ...` → ImportError → 整个 URLconf 挂掉
    # → 守卫回滚**所有** app 的路由（all-or-nothing），已能接的也一起丢。
    pairs: list[tuple[str, str, str]] = []      # (dotted_module, func, alias)
    for e in entries:
        mod = str(e.get("module") or "")
        dotted = (mod[:-3] if mod.endswith(".py") else mod).replace("/", ".")
        alias = "_h_%s" % e["slug"]
        if (dotted, e["func"], alias) not in pairs:
            pairs.append((dotted, e["func"], alias))

    # **同一路径的多个方法必须合成一个视图**（2026-11-15 实测）：
    # `path()` 只认第一条匹配，重复的 `path("students/")` 会让第二条永远不生效
    # （实测 accounts/urls_people.py 里两条 `path("students/")`，GET 那条是死路由，
    # 对应能力永远红，而代码看着"接过了"）。合并后按 request.method 分派。
    groups: list[tuple[str, list[dict]]] = []
    _gidx: dict[str, int] = {}
    for e in entries:
        rel = str(e["rel"])
        if rel in _gidx:
            g = groups[_gidx[rel]][1]
            if any(str(x["method"]).upper() == str(e["method"]).upper() for x in g):
                continue                        # 同路径同方法：保留第一条
            g.append(e)
        else:
            _gidx[rel] = len(groups)
            groups.append((rel, [e]))

    lines = [
        MARK_BEGIN,
        "import json as _json",
        "",
        # 业务返回值 → HTTP 响应。**契约允许业务函数用 `status` 表达期望状态码**
        # （契约示例里就这么写：`register(...) -> {..., \"status\": 201}`），
        # 但 HTTP 状态是传输层的事：这里把它取出来当状态码，并从 body 里摘掉。
        # 不做这件事的后果（2026-11-15 实测）：探针要 201，适配视图恒 200 → 必红，
        # 而远端怎么改函数体都改不掉（它对状态码没有控制权）。
        "def _engine_json(_r, _default=200):",
        "    if isinstance(_r, dict) and isinstance(_r.get('status'), int) \\",
        "            and 100 <= int(_r['status']) <= 599:",
        "        _code = int(_r['status'])",
        "        _body = {k: v for k, v in _r.items() if k != 'status'}",
        "        return JsonResponse(_body, status=_code, safe=False)",
        "    return JsonResponse(_r, status=_default, safe=False)",
        "",
        "from django.http import JsonResponse",
        "from django.urls import path  # noqa: F401",
        "from django.views.decorators.csrf import csrf_exempt",
        "",
        # **CSRF**（2026-11-15 实测）：Django 默认带 CsrfViewMiddleware，
        # 而本栈是 Bearer 令牌 API（凭据在 authorization 头里，不用 Cookie 会话）。
        # 不加 csrf_exempt 的后果：探针真发 POST → **403**（Django 的 test Client
        # 默认不校验 CSRF，所以自检全绿、真机全红 —— 这就是"两种客户端不一致"的坑）。
        # 适配视图一律 csrf_exempt；业务函数不碰 CSRF。
        "def _engine_run(_f):",
        "    try:",
        "        return _engine_json(_f())",
        # **异常按类型映射到 HTTP**（2026-11-15）：契约里业务函数只会 raise，
        # 状态码由这一层给。全映射成 422 会让"未授权 401 / 不存在 404"永远红。
        "    except PermissionError as _exc:",
        '        return JsonResponse({"detail": str(_exc)}, status=401)',
        "    except LookupError as _exc:",
        '        return JsonResponse({"detail": str(_exc)}, status=404)',
        "    except ValueError as _exc:",
        '        return JsonResponse({"detail": str(_exc)}, status=422)',
    ]
    for dotted, fn, alias in pairs:
        lines.append("from %s import %s as %s" % (dotted, fn, alias))
    lines.append("")
    for rel, grp in groups:
        lines += [
            "",
            "@csrf_exempt",
            "def _engine_%s(request, **kwargs):" % _view_slug(rel, grp[0]["slug"]),
            "    try:",
            '        _b = _json.loads((request.body or b"{}").decode("utf-8", "replace") or "{}")',
            "    except Exception:",
            "        _b = {}",
            '    _m = (request.method or "GET").upper()',
        ]
        for e in grp:
            lines += [
                '    if _m == "%s":' % str(e["method"]).upper(),
                "        return _engine_run(lambda: _h_%s(%s))"
                % (e["slug"], ", ".join(e["args"])),
            ]
        lines += [
            '    return JsonResponse({"detail": "方法不允许：%s"}, status=405)' % rel,
        ]
    lines += ["", "", "_ENGINE_URLPATTERNS = ["]
    for rel, grp in groups:
        _slug = _view_slug(rel, grp[0]["slug"])
        lines.append('    path("%s", _engine_%s, name="%s"),' % (rel, _slug, _slug))
    lines += ["]", "", "urlpatterns += _ENGINE_URLPATTERNS", MARK_END, ""]
    return "\n".join(lines)


def _strip_block(src: str) -> str:
    """去掉已有的引擎块（重渲染前先清，保证幂等）。"""
    if MARK_BEGIN not in src:
        return src
    head, _, rest = src.partition(MARK_BEGIN)
    rest = rest.split(MARK_END, 1)[1] if MARK_END in rest else ""
    return head.rstrip() + "\n" + rest.lstrip("\n")


def write_blocks(ws, per_app: dict[str, list[dict]]) -> list[str]:
    """把渲染结果写进各 urls 模块，返回被改动的文件清单。

    键是 **urls 模块相对路径**（一个 app 可能有 `urls.py` + `urls_people.py`）。
    """
    ws = Path(ws)
    touched: list[str] = []
    for rm, entries in per_app.items():
        p = ws / rm
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        _app = _app_of(rm) or (rm.split("/")[1] if rm.startswith("apps/") else rm)
        body = _strip_block(src).rstrip() + "\n\n" + _render(_app, entries)
        if body != src:
            p.write_text(body, encoding="utf-8")
            touched.append(str(p.relative_to(ws)).replace("\\", "/"))
    return touched


def check_project(ws, timeout: int = 120) -> tuple[bool, str]:
    """跑 `manage.py check`（URLconf 的语法/导入错误会被它抓到）。"""
    ws = Path(ws)
    try:
        r = subprocess.run([sys.executable, "manage.py", "check"],
                           cwd=str(ws), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "manage.py check 超时（%ss）" % timeout
    if r.returncode == 0:
        return True, (r.stdout or "").strip()[-400:]
    return False, ((r.stdout or "") + (r.stderr or "")).strip()[-1200:]


def wire(ws, bp=None, *, route_funcs: dict | None = None, emit=None) -> list[str]:
    """把能力表的端点接进 Django 的 URLconf。

    守卫（铁律：**每个会写盘的机制都必须自带复验**）：
    快照被改文件 → 写 → `manage.py check` → 失败就恢复快照并返回空清单。
    """
    ws = Path(ws)
    if not is_django(ws):
        return []
    from . import routefuncs as _rf

    prefixes = app_url_map(ws)
    if not prefixes:
        _emit(emit, "notice",
              {"message": "Django 接线跳过：config/urls.py 里没有 apps.* 的 include"})
        return []

    # **降级的能力不接线**（2026-11-15）：它们被显式放弃（同一概念两个名字 / 不可观测），
    # 却仍被收集 → 每轮十几条 "接线跳过" 噪声，还把已放弃的能力算进接线覆盖率。
    caps = [c for c in (getattr(bp, "caps", None) or [])
            if str(getattr(c, "status", "")) != "descoped"] if bp is not None else []
    eps = endpoints_from_caps(caps)
    if not eps:
        return []

    modules = candidate_modules(ws)
    rf = dict(route_funcs or {})
    if not rf:
        try:
            rf = _rf.from_workspace(ws)
        except Exception:                                   # noqa: BLE001
            rf = {}

    # per_app 的键是 **urls 模块相对路径**（`apps/accounts/urls_people.py`）：
    # 一个 app 挂两个前缀时，端点必须写进它自己那个 url 模块，
    # 写错文件不会报错，只会静默把路由挂到别的前缀下（→ 404，最难查）。
    per_app: dict[str, list[dict]] = {}
    skipped: list[str] = []
    already = 0          # 路由已存在的端点数（无需接线，静默跳过）
    for ep in eps:
        urls_rel = next((rm for rm, pf in prefixes.items()
                         if _rel_pattern(ep["path"], pf) is not None), "")
        if not urls_rel:
            skipped.append("%s %s（没有匹配的 app 前缀）" % (ep["method"], ep["path"]))
            continue
        app = _app_of(urls_rel)
        rel = _rel_pattern(ep["path"], prefixes[urls_rel])
        # 路由已在该 app 的 urls 文件里 → 接线层无事可做，静默跳过（不产噪声）
        if _route_exists(ws, urls_rel, rel or ""):
            already += 1
            continue
        path_params = set(re.findall(r"<int:(\w+)>", rel or ""))
        prefer = rf.get(_rf.normalize(ep["path"]), "")
        mod, func = _find_handler(ws, modules, prefer, ep["path"], ep["method"],
                                  set(ep["body_keys"] or []), path_params)
        if not func:
            skipped.append("%s %s（%s 里没有可用的处理函数）"
                           % (ep["method"], ep["path"], app or urls_rel))
            continue
        try:
            src = (ws / mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            skipped.append("%s %s（读不到 %s）" % (ep["method"], ep["path"], mod))
            continue
        args, bad = _plan_args(src, func, path_params, set(ep["body_keys"] or []))
        if bad:
            skipped.append("%s %s（%s 的参数 %s 路由层供给不了）"
                           % (ep["method"], ep["path"], func, ",".join(bad)))
            continue
        per_app.setdefault(urls_rel, []).append({
            "method": ep["method"], "rel": rel, "func": func,
            "name": _name_of(ep["method"], rel, func),
            "slug": _name_of(ep["method"], rel, func),
            "args": args, "module": mod, "app": app,
        })

    if not per_app:
        if already:
            _emit(emit, "django_wire",
                  {"routes": 0, "apps": {}, "touched": 0,
                   "skipped": len(skipped), "already": already})
        for s in skipped:
            _emit(emit, "notice", {"message": "Django 接线跳过：" + s})
        return []

    # ── 守卫：快照 → 写 → 复验 → 失败回滚 ──
    files = {rm: ws / rm for rm in per_app}
    snapshot = {k: (v.read_text(encoding="utf-8-sig", errors="replace") if v.is_file() else "")
                for k, v in files.items()}
    touched = write_blocks(ws, per_app)
    ok, detail = check_project(ws)
    if not ok:
        for rm, src in snapshot.items():
            if files[rm].is_file():
                files[rm].write_text(src, encoding="utf-8")
        _emit(emit, "notice", {"message": "Django 接线复验失败，已回滚：%s" % detail[:300]})
        return []

    routes = sum(len(v) for v in per_app.values())
    _emit(emit, "django_wire",
          {"routes": routes,
           "apps": {rm.split("/")[1] if rm.startswith("apps/") else rm: len(v)
                    for rm, v in per_app.items()},
           "touched": touched, "skipped": len(skipped), "already": already})
    for s in skipped:
        _emit(emit, "notice", {"message": "Django 接线跳过：" + s})
    return touched


def _route_exists(ws, urls_rel: str, rel: str) -> bool:
    """这个相对路径**已经在该 app 的 urls 文件里注册**了吗？

    2026-11-19 实测：本项目的作者区把视图**直接写在** `apps/<app>/urls.py`
    （`path("generate/", _author_generate, ...)`），而 `candidate_modules()` 只搜
    `apps/*/views.py` / `apps/*/services.py` / `matching_core/*.py` → `_find_handler`
    找不到 → 报「X 里没有可用的处理函数」。**这条提示是假的**：处理函数就在
    urls.py 里、路由也是通的。69 个端点全报这条 → 远端被引去改本来正确的产物代码
    （典型的"反馈被包错语义框 = 没到"）。

    **段级比对**（不是字面包含）：探针里的 `${tid}` 是**模板占位符**，真实路由是
    `<int:pk>`；两者必须互相当通配，否则带参数的端点永远匹配不上（94 条里 26 条
    是这种）。规则：段数相同，且每一段「字面相等」或「双方任一是占位/参数段」。

    路由已存在时**静默跳过**：接线层的职责是「缺什么补什么」，已经有了就什么都不做。
    """
    key = str(rel or "").strip().strip("/")

    def _segs(text: str) -> list[str]:
        return [s for s in text.strip("/").split("/") if s != ""]

    want = _segs(key)
    if not want:
        # 根路径：urls.py 里出现 `path("")` / `path("/")` 才算已挂
        try:
            src = (Path(ws) / urls_rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return False
        return bool(re.search(r'path\(\s*["\']\s*/?\s*["\']', src))

    try:
        src = (Path(ws) / urls_rel).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False

    # 抓出所有 path("...") / re_path("...") 的路径字面量
    pats = re.findall(r'(?:re_)?path\(\s*["\']([^"\']*)["\']', src)

    def _wild(seg: str) -> bool:
        return seg.startswith("${") or seg.startswith("<") or seg.startswith(":")

    for pat in pats:
        have = _segs(pat)
        if len(have) != len(want):
            continue
        if all((a == b) or _wild(a) or _wild(b) for a, b in zip(want, have)):
            return True
    return False


def _find_handler(ws, modules, prefer: str, path: str, method: str,
                  body_keys: set, path_params: set | None = None) -> tuple[str, str]:
    """选处理函数：**契约点名优先**，否则按路径末段匹配（复用 `auto._pick_func`）。"""
    from . import auto as _auto

    path_params = set(path_params or ())
    if prefer:
        for rel in modules:
            try:
                src = (Path(ws) / rel).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if re.search(r"^\s*def\s+%s\s*\(" % re.escape(prefer), src, re.M):
                return rel, prefer
    for rel in modules:
        try:
            src = (Path(ws) / rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        fns = _auto._module_signatures(src)
        cand = _auto._pick_func(path, fns, method, src=src)
        if not cand:
            continue
        _args, bad = _plan_args(src, cand, path_params, set(body_keys or []))
        if not bad:
            return rel, cand
    # 兜底：按"资源名"挑（带参端点的末段是 {xxx}，上面那条永远匹配不上）
    hint = _resource_hint(path)
    if hint:
        best: tuple[int, str, str] | None = None
        verbs = VERB_HINTS.get(str(method or "GET").upper(), ())
        for rel in modules:
            try:
                src = (Path(ws) / rel).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            for name in _auto._module_signatures(src):
                if name.startswith("_") or hint not in name:
                    continue
                _args, bad = _plan_args(src, name, path_params, set(body_keys or []))
                if bad:
                    continue
                rank = 0 if name.startswith(verbs) else 1
                cand_key = (rank, len(name), name)
                if best is None or cand_key < (best[0], len(best[1]), best[1]):
                    best = (rank, name, rel)
        if best is not None:
            return best[2], best[1]
    return "", ""


def _emit(emit, kind: str, payload: dict) -> None:
    if emit is None:
        return
    try:
        emit(kind, payload)
    except Exception:                                       # noqa: BLE001
        pass
