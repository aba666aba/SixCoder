"""栈适配层（D 档核心）：把"支持某个框架"从"改一堆地方"变成"写一个适配器"。

## 为什么要有这一层（§20.7 决策记录）

现状：引擎把 **FastAPI 写死了 74 处**（`core/auto.py` 占 38）。写死意味着
"换个框架 = 改十几处散落的地方"，每处漏一处就是**假绿**（引擎以为接上了，
运行时 404 / NameError）。已经踩过的坑堆成血泪：
`app.include_router` 只认 FastAPI、端点反查只认 `@router.`、import 补齐表
只认 `fastapi.*`、ASGI 探测只认 `FastAPI(`。

## 这一层做三件事

1. **把 7 个接缝收敛成接口**（§20.7 表格）：
   入口样板 / 写码样板 / 骨架生成 / 端点反查 / import 补齐表 / 判据约定 / 启动方式。
   每接缝一个方法，换框架 = 写一个适配器，不动别的地方。
2. **默认仍是 FastAPI，且行为逐字不变**（回归铁律）：
   `fastapi` 适配器把现有逻辑**原样搬进来**，不改一个字节的输出。
   自检 `checks/check_stack.py` 用"旧常量 vs 新适配器"逐字对比钉死这件事。
3. **选栈是配置，不是猜**：
   `.sixcoder/stack.json`（或 settings）写 `{"stack": "fastapi"}`。
   **不写 = fastapi**（老行为零变化）。写错了就起不来 —— 这是刻意的：
   引擎**不猜**栈（猜错 = 整轮白烧钱，见 §20.1 证据 1"需求写 Django、产物是 FastAPI"）。

## 接缝表（谁负责什么）

| 接缝 | 方法 | FastAPI | Django |
|---|---|---|---|
| 入口样板 | `render_entry` | `app/main.py`（ASGI + include_router） | `app/main.py`（`get_asgi_application()`） |
| 写码样板 | `route_hint` / `render_route` | `@router.get(...)` + Pydantic 模型 | `path("...", view)` + JsonResponse |
| 骨架生成 | `scaffold` / `module_dirs` | 无需骨架 | settings.py / urls.py / manage.py |
| 端点反查 | `parse_routes` | 扫 `@router.<verb>("path")` | 扫 `path("path", ...)` |
| import 补齐表 | `auto_import_table` | `fastapi.*` / `pydantic.*` | `django.*` |
| 判据约定 | `probe_app_candidates` | `app.main:app`（ASGI，TestClient 优先） | `app.main:app`（ASGI，httpx 传输） |
| 启动方式 | `start_cmd` | `uvicorn app.main:app` | `python manage.py runserver` |

## 边界（不做）

- 不做通用框架生成器，不做 SSR 框架，不自研打包器。
- 适配器只负责**接线与验收约定**，不负责业务逻辑（业务永远由远端写）。
- 一个栈一个适配器，适配器之间**不互相 import**（避免螺旋依赖）。

## 复用现有抽象

入口探测已经有一层正确的解耦（`core/entrypoint.py`：探测式，不写死）。
栈适配层是同一思路的推广：**生成端约定 + 判据端探测**，两侧都不写死框架名。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# 栈配置文件（放产物仓 .sixcoder/ 下，与 web_routes.json / budget.json 同级）
STACK_FILE = ".sixcoder/stack.json"

# 默认栈：写死不是"支持 FastAPI"的同义词，而是**没有配置时的兜底**。
# 老项目没有 stack.json → 走 fastapi → 行为与本次改动前逐字一致。
DEFAULT_STACK = "fastapi"


@dataclass
class RouteSpec:
    """一条待接线的端点（**栈无关**）。

    这是整个适配层的核心数据结构：契约/探针产出它，适配器把它渲染成
    各栈自己的路由声明。FastAPI 渲染成 `@router.get("/x", ...)`，
    Django 渲染成 `path("x/", view)`。

    字段全部是**栈无关的事实**（方法、路径、绑哪个函数、实参怎么传、
    请求体字段、状态码），不含任何框架特有的东西 —— 这是它能跨栈的前提。
    """
    method: str = "GET"
    path: str = "/"
    func: str = ""                       # 绑定的作者函数名
    args: list = field(default_factory=list)   # [("name", "expr"), ...] 或 [("**", "expr")]
    body_keys: list = field(default_factory=list)
    body_types: dict = field(default_factory=dict)
    body_required: list = field(default_factory=list)
    body_sample: dict = field(default_factory=dict)
    status: int | None = None
    path_params: list = field(default_factory=list)
    header_params: list = field(default_factory=list)
    slug: str = ""                       # 端点的稳定短名（get_api_v1_tickets 之类）

    @property
    def name(self) -> str:
        """端点的稳定标识（跨栈一致，用于记账与去重）。"""
        return f"{self.method.upper()} {self.path}"


# ------------------------------------------------------------------ 适配器基类
class StackAdapter:
    """栈适配器基类。

    所有接缝都有默认实现（= FastAPI 之前的行为），子类只写差异部分。

    **这里刻意不用 `@dataclass`**（2026-11-11 实测踩坑）：dataclass 会给每个
    带默认值的字段生成 `__init__` 参数，子类写 `name = "django"` 只是**类属性**，
    实例化时被 `StackAdapter.__init__(name="fastapi")` 的默认值盖回去 →
    `DjangoAdapter().name == "fastapi"` → 注册表里后注册的 Django 把 FastAPI
    顶掉，**默认栈静默变成 Django**，入口模板、import 表、骨架全错。
    症状是三条互不相干的自检一起红，根因只是这一行装饰器。
    字段全是类属性，谁都不实例化时带状态，就不会有这种覆盖。
    """
    name: str = "fastapi"
    label: str = ""

    # ---- 接缝 1：入口样板 ----
    entry_path: str = "app/main.py"
    entry_import_hint: str = "from fastapi import FastAPI"

    # ---- 接缝 2：写码样板 ----
    route_hint: str = "用 FastAPI 的 APIRouter + 装饰器注册"
    router_decl: str = "router = APIRouter()"
    router_marker: str = "APIRouter("          # 源码里出现它 = 这个模块注册了路由

    # ---- 接缝 3：骨架生成 ----
    module_dirs: tuple = ("app/api", "app", "matching_core")
    module_file_fmt: str = "app/api/{seg}.py"
    module_globs: tuple = ("app/**/*.py",)
    module_prefix: str = "app."

    # ---- 接缝 4：端点反查 ----
    route_decorator_re: str = r"@router\.(\w+)\(\s*[\"']([^\"']+)[\"']"
    route_verb_default: str = "GET"      # 单组正则的兜底动词（Django path() 不声明方法）
    route_hosts: tuple = ("router", "app")

    # ---- 接缝 6：产物形态 ----
    template_dirs: tuple = ("app/templates", "templates")
    static_dirs: tuple = ("app/static", "static")
    static_mount: str = "/static"

    # ---- 接缝 7：启动方式 / 判据约定 ----
    start_cmd: str = "uvicorn app.main:app"
    app_ref: str = "app.main:app"
    probe_app_candidates: tuple = ("main:app", "app.main:app", "app:app",
                                   "server:app", "backend.main:app")
    deps: tuple = ("fastapi", "uvicorn")
    app_marker: str = "FastAPI("

    # ---------------------------------------------------------------- 接缝实现
    def auto_import_table(self) -> dict:
        """接缝 5：名字 → 该补的 import 语句。"""
        from . import auto as _auto
        return dict(_auto._AUTO_IMPORTS)

    def render_entry(self, modules: list) -> str:
        """接缝 1：渲染整个 ASGI 入口文件的内容。"""
        raise NotImplementedError

    def render_route(self, spec: RouteSpec) -> list:
        """接缝 2：把一条端点渲染成源码行（不含 import 头）。"""
        raise NotImplementedError

    def route_prelude(self, specs: list, bound: set) -> list:
        """接缝 2：路由段顶部的 import / 声明（每个模块一份）。"""
        raise NotImplementedError

    def parse_routes(self, src: str) -> list:
        """接缝 4：从源码里反查已注册的端点。返回 [(method, path)]。

        正则可以是**两组**（动词 + 路径，FastAPI 的 `@router.get("/x")`）或
        **单组**（只有路径，Django 的 `path("x/", view)` 不声明 HTTP 方法）。
        2026-11-11 实测：Django 的单组正则走旧的 `group(2)` 会 IndexError，
        端点反查整条链炸掉 —— 字段契约不一致不能靠"约定"兜，得在基类抹平。
        单组时用 `route_verb_default`（默认 GET）补齐。
        """
        out = []
        rx = re.compile(self.route_decorator_re)
        groups = rx.groups
        if groups not in (1, 2):
            return out
        for m in rx.finditer(str(src or "")):
            if groups == 2:
                verb, path = m.group(1).upper(), m.group(2)
            else:
                verb, path = str(self.route_verb_default).upper(), m.group(1)
            path = re.sub(r"\$\{(\w+)\}", r"{\1}", path)
            if path:
                out.append((verb, path))
        return out

    def scaffold(self, ws) -> dict:
        """接缝 3：写栈特有的骨架文件（Django 要 settings/urls/manage；FastAPI 不需要）。"""
        return {"ok": True, "wrote": False, "why": "该栈不需要骨架文件"}

    def module_for(self, seg: str) -> str:
        """一个资源段（segment）该落在哪个模块文件（接缝 3）。"""
        seg = str(seg or "").strip()
        if not seg:
            return ""
        return self.module_file_fmt.format(seg=seg)

    def is_app_source(self, text: str) -> str:
        """这段源码里有没有本栈的应用对象（用于依赖探测）。返回匹配到的标记。"""
        t = str(text or "")
        if self.name == "fastapi":
            if "FastAPI(" in t:
                return "FastAPI("
            if "Starlette(" in t:
                return "Starlette("
            return ""
        return self.app_marker if (self.app_marker and self.app_marker in t) else ""

    def asgi_app(self) -> bool:
        """这个栈的应用是不是标准 ASGI（决定判据端用 TestClient 还是 httpx 传输）。"""
        return True

    def web_rules(self) -> str:
        """这个栈的**能力表探针约定**（喂给远端推导蓝图时的那段规则）。

        返回空串 = 用 blueprint.web_rules 的内置分支（FastAPI/Django）。
        生态包（如前端 SPA）覆写它，就能让自己的探针约定（browser 探针）
        直接进推导提示词 —— 不用改 blueprint（2026-11-19）。
        """
        return ""


# ------------------------------------------------------------------ FastAPI 适配器
# 把现有实现**原样搬进来** —— 输出必须与改动前逐字一致（回归铁律）。
# 搬的时候不"顺手优化"，任何字节差异都会被 checks/check_stack.py 抓住。
class FastAPIAdapter(StackAdapter):
    name = "fastapi"
    label = "FastAPI（默认）"
    entry_path = "app/main.py"
    entry_import_hint = "from fastapi import FastAPI"
    route_hint = "用 FastAPI 的 APIRouter + 装饰器注册"
    router_decl = "router = APIRouter()"
    router_marker = "APIRouter("
    module_dirs = ("app/api", "app", "matching_core")
    module_file_fmt = "app/api/{seg}.py"
    module_globs = ("app/**/*.py",)
    module_prefix = "app."
    route_decorator_re = r"@router\.(\w+)\(\s*[\"']([^\"']+)[\"']"
    route_hosts = ("router", "app")
    template_dirs = ("app/templates", "templates")
    static_dirs = ("app/static", "static")
    static_mount = "/static"
    start_cmd = "uvicorn app.main:app"
    app_ref = "app.main:app"
    probe_app_candidates = ("main:app", "app.main:app", "app:app",
                             "server:app", "backend.main:app")
    deps = ("fastapi", "uvicorn")
    app_marker = "FastAPI("

    def render_entry(self, modules: list) -> str:
        from . import auto as _auto
        return _auto.WEB_ENTRY.replace("{modules!r}", repr(sorted(modules or [])))

    def render_route(self, spec: RouteSpec) -> list:
        """FastAPI 的路由段（**与改动前逐字一致**）。

        原来的实参是拼好的字符串（`ticket_id=_to_int(ticket_id)`），
        这里从 RouteSpec 的 args 还原成同样的文本 —— 顺序、空白、缩进都不能变。
        """
        from . import auto as _auto
        method = spec.method.lower()
        path = spec.path
        bkeys = list(spec.body_keys or [])
        model = ""
        if bkeys:
            fname = f"_Body_{spec.slug}"
            _req = set(spec.body_required or [])
            lines = []
            for k in sorted(bkeys):
                _t = (spec.body_types or {}).get(k) or _auto._py_type(
                    (spec.body_sample or {}).get(k))
                if _t == "any":
                    _t = "Any"
                lines.append(f"    {k}: {_t}" if k in _req else f"    {k}: {_t} | None = None")
            model = f"class {fname}(BaseModel):\n" + "\n".join(lines)
            body_arg = f"body: {fname}"
        else:
            body_arg = "body: dict = Body(default=None)"
        extra = [f"{v}: str" for v in (spec.path_params or [])] + [body_arg]
        st = spec.status
        kw = f", status_code={int(st)}" if isinstance(st, int) else ""
        out = []
        if model:
            out += ["", model]
        _args = ", ".join(_fmt_arg(a) for a in (spec.args or []))
        out += ["", f'@router.{method}("{path}", response_model=None{kw})',
                f"def _engine_{spec.slug}({', '.join(extra)},",
                "                   authorization: str = Header(default=\"\")):",
                ("    _b = body.model_dump()" if bkeys else
                 "    _b = body if isinstance(body, dict) else {}"),
                "    try:",
                f"        return {spec.func}({_args})",
                "    except ValueError as _exc:",
                # 契约里"非法输入抛 ValueError"是通用约定，但旧包装器不接 →
                # 直接 500（实测 2026-11-08：college_id 传了非数字，建专业 500 而非 422）。
                # 400/422 是"用户输入错"，500 是"服务端崩了"，两者归因完全不同：
                # 判成 500 会让 `_cause` 去追服务端 bug，把作者往错方向带。
                "        raise HTTPException(status_code=422, detail=str(_exc))"]
        return out

    def route_prelude(self, specs: list, bound: set) -> list:
        """FastAPI 路由段顶部（import / router 声明 / `_to_int` 助手）。"""
        out = []
        if any("Any" in (s.body_types or {}).values() for s in specs) \
                and "from typing import" not in bound:
            out.append("from typing import Any")
        if any(s.body_keys for s in specs) and "BaseModel" not in bound:
            out.append("from pydantic import BaseModel")
        for name in ("APIRouter", "Body", "Header", "HTTPException"):
            if name not in bound:
                out.append(f"from fastapi import {name}")
        if "router" not in bound:
            out.append("router = APIRouter()")
        if "_to_int" not in bound:
            out += ["", "def _to_int(v):",
                    "    try:", "        return int(v)",
                    "    except (TypeError, ValueError):", "        return v"]
        return out


def _fmt_arg(arg) -> str:
    """RouteSpec.args 里的一项 → 调用实参文本。

    形状：`(name, expr)` → `name=expr`；`("**", expr)` → `**expr`。
    """
    if isinstance(arg, (tuple, list)) and len(arg) == 2:
        name, expr = arg
        return f"**{expr}" if name == "**" else f"{name}={expr}"
    return str(arg)


# ------------------------------------------------------------------ Django 适配器
# 2026-11-15 事故（覆盖恒 0/76 的真凶）：入口模板把 settings 包名写死成 `app`，
# 而 config/ 布局的工程包叫 `config` → `import config.asgi` 立刻
# `ModuleNotFoundError: No module named 'app.settings'`，
# 探针报"没有可用的 ASGI 应用" → **76 条能力全判 absent**、覆盖永远 0/76。
# 而产品代码其实写了大半（241 个函数、只有 6 个空桩）——判据挂了，不是活儿没干。
# 两个坑一起修：①包名按 entry_path 推导（不再写死 app）；
# ②暴露名必须是 `application`（app_ref 就是 `config.asgi:application`，
# 只给 `app` 会 AttributeError，症状一模一样）。
DJANGO_ENTRY = '''"""ASGI 应用入口（**由引擎生成，不要改**）。

Django 的 ASGI 应用由 `get_asgi_application()` 产出，是标准 ASGI app
（探针可以用 httpx 的 ASGI 传输直接喂它）。
"""
from __future__ import annotations

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "@PKG@.settings")

from django.core.asgi import get_asgi_application

application = get_asgi_application()
app = application          # 兼容 `@PKG@.asgi:app` 这种候选写法
'''

# 2026-11-15 事故：入口模板里 settings 包名被写死成 `app`，而 config/ 布局的
# 工程包叫 `config` —— 于是 `import config.asgi` 立刻
# `ModuleNotFoundError: No module named 'app.settings'`，
# 探针报"没有可用的 ASGI 应用"→ **76 条能力全判 absent**、覆盖恒 0/76，
# 而产品代码其实写了大半（241 个函数、只有 6 个空桩）。
# 两个坑一起修：①包名按 entry_path 推导（不再写死 app）；
# ②暴露名必须是 `application`（app_ref 就是 `config.asgi:application`，
# 只给 `app` 会 AttributeError，症状同样是"没有可用的 ASGI 应用"）。
DJANGO_ENTRY_PKG_TOKEN = "@PKG@"


class DjangoAdapter(StackAdapter):
    """Django 适配器（§20.7 的 7 个接缝逐个落）。

    为什么**现在**做它：文档 §20.7 原本把 Django 记成"当前明确不做"。
    但**理由变了**：那 4 条理由里只有第 2 条（判据要 as_view()）仍然成立，
    而它恰恰是**适配层要解决的事**（判据约定本来就是接缝之一）。
    换句话说：Django 之所以贵，是因为没有适配层；有了适配层它就便宜了。

    与文档"为什么不做 Django"的关系：**不改结论，只改成本**。
    默认栈仍是 fastapi；Django 要显式选（`.sixcoder/stack.json`）。
    """
    name = "django"
    label = "Django（DRF 风格）"
    # **布局口径（2026-11-15 改）**：`config/`（工程配置）+ `apps/<域>/`（按业务域分 app）。
    # 旧口径是单包 `app/`（`app/{seg}.py`、入口 `app/main.py`）—— 那撑不住
    # 任务书那种"9 个业务域 / 19 张表 / 六类规则"的规模：所有模型挤一个 models.py、
    # 所有视图挤一个目录，蓝图也表达不了"这张表属于哪个域"。
    # 单包布局仍可用（`module_file_fmt_flat` 分支），但不作为默认。
    entry_path = "config/asgi.py"
    entry_import_hint = "from django.core.asgi import get_asgi_application"
    route_hint = ("用 Django 的 path()/url() 在各 app 的 urls.py 里注册；"
                  "数据模型用 django.db.models，按域分 app")
    router_decl = "urlpatterns = []"
    router_marker = "path("
    module_dirs = ("apps", "config", "matching_core")
    module_file_fmt = "apps/{seg}/views.py"
    module_file_fmt_flat = "app/{seg}.py"      # 兼容单包布局
    module_globs = ("apps/**/*.py", "matching_core/*.py", "app/**/*.py")
    module_prefix = "apps."
    route_decorator_re = r"path\(\s*[\"']([^\"']+)[\"']"
    route_hosts = ("urlpatterns",)
    template_dirs = ("templates", "apps/templates", "app/templates")
    static_dirs = ("static", "apps/static", "app/static")
    static_mount = "/static"
    start_cmd = "python manage.py runserver"
    app_ref = "config.asgi:application"
    probe_app_candidates = ("config.asgi:application", "app.main:app",
                            "app.main:application")
    deps = ("django",)
    app_marker = "get_asgi_application("

    def parse_routes(self, src: str) -> list:
        """Django 的 `path()` 路径**不带前导斜杠**（`"api/v1/x/"`），
        但探针/契约的口径带（`/api/v1/x/`）。不归一就会永远对不上 → 假红。
        """
        out = []
        for verb, path in super().parse_routes(src):
            path = str(path or "")
            if path and not path.startswith("/"):
                path = "/" + path
            out.append((verb, path))
        return out

    def auto_import_table(self) -> dict:
        """Django 侧的名字 → 该补的 import（+ 与栈无关的 stdlib）。"""
        from . import auto as _auto
        table = {
            "HttpResponse": "from django.http import HttpResponse",
            "JsonResponse": "from django.http import JsonResponse",
            "get_object_or_404": "from django.shortcuts import get_object_or_404",
            "render": "from django.shortcuts import render",
            "path": "from django.urls import path",
            "include": "from django.urls import include",
            "models": "from django.db import models",
            "Model": "from django.db.models import Model",
            "CharField": "from django.db.models import CharField",
            "IntegerField": "from django.db.models import IntegerField",
            "TextField": "from django.db.models import TextField",
            "DateTimeField": "from django.db.models import DateTimeField",
            "BooleanField": "from django.db.models import BooleanField",
            "ForeignKey": "from django.db.models import ForeignKey",
            "settings": "from django.conf import settings",
            "get_asgi_application": "from django.core.asgi import get_asgi_application",
        }
        # 与栈无关的 stdlib 照旧补（sqlite3/json/os/... —— 那些在哪个栈都用得上）
        table.update({k: v for k, v in _auto._AUTO_IMPORTS.items()
                      if v.startswith("import ")})
        return table

    def render_entry(self, modules: list) -> str:
        """Django 的 ASGI 入口：标准 `get_asgi_application()`。

        为什么这样就够：Django 4+ 的 `get_asgi_application()` 产出的就是
        **标准 ASGI app**，探针的 httpx ASGI 传输能直接喂 → 判据层几乎不用改
        （§20.7 理由 1，成立）。

        settings 包名**按 entry_path 推导**（`config/asgi.py` → `config`），
        不再写死 `app`：写死的后果是 `ModuleNotFoundError: app.settings`
        → 探针认不出 ASGI 应用 → 覆盖恒 0/76（2026-11-15 事故）。
        """
        _pkg = self.entry_path.split("/", 1)[0] if "/" in self.entry_path else "app"
        return DJANGO_ENTRY.replace(DJANGO_ENTRY_PKG_TOKEN, _pkg)

    def route_path(self, path: str) -> str:
        """FastAPI 风格 `{id}` → Django 风格 `<int:id>`。

        口径：路径参数名以 `_id` 结尾或叫 `id` → int，其余 → str。
        理由：Django 的 `<int:...>` 转换器会**拒绝**非数字（404），
        而 FastAPI 的 `{id}` 会把 "abc" 也传进去 —— 换栈后行为必须**如实变**，
        不能假装两边一样。
        """
        def _rep(m):
            name = m.group(1)
            kind = "int" if (name == "id" or name.endswith("_id")) else "str"
            return f"<{kind}:{name}>"
        p = re.sub(r"\{(\w+)\}", _rep, str(path or ""))
        if p and not p.startswith("/"):
            p = "/" + p
        return p

    def render_route(self, spec: RouteSpec) -> list:
        """Django 的路由声明：Django 用 `urls.py` 集中注册，模块里放**视图函数**。

        Django 的 view 签名是 `(request, ...)`，返回 `JsonResponse`。
        这里生成的是一个**薄视图**：把 request 里的东西解出来，转调作者的
        业务函数，再包成 JSON —— 业务函数本身仍然是无框架依赖的纯函数
        （这与 FastAPI 路径一致：业务逻辑不依赖框架）。
        """
        from . import auto as _auto
        path = self.route_path(spec.path)
        body_keys = list(spec.body_keys or [])
        lines = ["", f"def _engine_{spec.slug}(request, *args, **kwargs):",
                 "    import json as _json",
                 "    try:",
                 "        _raw = request.body or b\"{}\"",
                 "        _b = _json.loads(_raw.decode(\"utf-8\", \"replace\") or \"{}\")",
                 "    except Exception:",
                 "        _b = {}"]
        # 路径参数（Django 从 kwargs 传进来）
        for name in (spec.path_params or []):
            lines.append(f"    {name} = kwargs.get(\"{name}\")")
        lines.append("    try:")
        args = ", ".join(_fmt_arg(a) for a in (spec.args or []))
        # Django 视图拿不到 FastAPI 的 `_b` 变量名，改写 body 取值
        args = args.replace("_b.get(", "_b.get(").replace("=_b", "=_b")
        lines.append(f"        return JsonResponse({spec.func}({args}))")
        lines.append("    except ValueError as _exc:")
        lines.append("        return JsonResponse({\"detail\": str(_exc)}, status=422)")
        lines.append("")
        lines.append(f"# urls.py 里注册：path({path!r}, views.{spec.func})")
        return lines

    def route_prelude(self, specs: list, bound: set) -> list:
        """Django 路由段顶部：视图要用的 import（不生成 router 声明）。"""
        out = []
        if "JsonResponse" not in bound:
            out.append("from django.http import JsonResponse")
        return out

    def scaffold(self, ws) -> dict:
        """写 Django 骨架：manage.py / app/settings.py / app/urls.py / app/__init__.py。

        为什么必须由引擎写：Django 的 settings 里 `INSTALLED_APPS` / `ROOT_URLCONF`
        / `DATABASES` 是**接线事实**（不是业务设计），让远端猜就会漏；
        漏一个 → `manage.py check` 直接报错 → 整轮白烧。
        """
        ws = Path(ws)
        wrote = []

        def _w(rel: str, text: str) -> None:
            p = ws / rel
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
                wrote.append(rel)
            except OSError:
                pass

        # 布局判定：有 config/ 或 apps/ → 新布局（工程配置与业务域分开）；
        # 否则退回单包 app/（老行为，零变化）。
        split_layout = (ws / "config").is_dir() or (ws / "apps").is_dir()

        _manage, _settings = django_skeleton_for("config" if (
            ws / "config").is_dir() or (ws / "apps").is_dir() else "app")

        if not (ws / "manage.py").exists():
            _w("manage.py", _manage)

        if split_layout:
            # 不建 app/：那会在 config/ 布局里造出一个孤立的包，
            # 而 ROOT_URLCONF 指向 config.urls → app/ 永远不会被导入（纯垃圾）。
            if not (ws / "config" / "__init__.py").exists():
                _w("config/__init__.py", "")
            if not (ws / "apps" / "__init__.py").exists():
                _w("apps/__init__.py", "")
            # settings/urls 若缺失才写（已有工程的配置是权威，不覆盖）
            if not (ws / "config" / "settings.py").exists():
                _w("config/settings.py", _settings)
            if not (ws / "config" / "urls.py").exists():
                _w("config/urls.py", DJANGO_URLS)
        else:
            if not (ws / "app" / "__init__.py").exists():
                _w("app/__init__.py", "")
            if not (ws / "app" / "settings.py").exists():
                _w("app/settings.py", _settings)
            if not (ws / "app" / "urls.py").exists():
                _w("app/urls.py", DJANGO_URLS)

        return {"ok": True, "wrote": bool(wrote), "files": wrote,
                "why": f"Django 骨架（{len(wrote)} 个文件，"
                       f"{'config/+apps/' if split_layout else 'app/'} 布局）"}


DJANGO_MANAGE = '''#!/usr/bin/env python
"""Django 管理入口（**由引擎生成，不要改**）。"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "app.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
'''

DJANGO_SETTINGS = '''"""Django 设置（**由引擎生成，不要改**）。

接线事实都在这里（不是业务设计）：INSTALLED_APPS / ROOT_URLCONF / DATABASES。
业务模型请写在自己的模块里（app/models.py）。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "sixcoder-local-dev-key")
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "app",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "app.urls"
WSGI_APPLICATION = "app.wsgi.application"
ASGI_APPLICATION = "app.main.app"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "app" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {"context_processors": []},
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
'''

DJANGO_URLS = '''"""URL 路由（**由引擎生成接线部分，业务路由请写在自己的模块里**）。"""
from __future__ import annotations

from django.urls import path

urlpatterns = [
]
'''

def django_skeleton_for(pkg: str) -> tuple[str, str]:
    """按包名生成 (manage.py, settings.py) 文本。

    **为什么不能直接复用模板**：`DJANGO_SETTINGS` 里 `ROOT_URLCONF="app.urls"`、
    `INSTALLED_APPS` 有 `"app"` —— 直接写进 `config/settings.py` 会让
    `manage.py check` 找不到模块（2026-11-15：我第一版就写了个不存在的
    `.split_package()`，纯属想当然）。所以这里**逐个键替换 + 断言替换过**，
    换不掉就说明模板变了，宁可报错也不写半对的配置。
    """
    manage = DJANGO_MANAGE.replace('"app.settings"', '"%s.settings"' % pkg)
    assert pkg in manage, "manage.py 模板变了：找不到 app.settings"

    s = DJANGO_SETTINGS
    pairs = [
        ('"app"', '"%s"' % pkg),
        ('"app.urls"', '"%s.urls"' % pkg),
        ('"app.wsgi.application"', '"%s.wsgi.application"' % pkg),
        ('"app.main.app"', '"%s.asgi.application"' % pkg),
        ('BASE_DIR / "app" / "templates"', 'BASE_DIR / "templates"'),
        ("app/models.py", "apps/<域>/models.py"),
    ]
    for old, new in pairs:
        if old in s:
            s = s.replace(old, new)
    if pkg != "app":     # pkg == "app" 时"残留 app"就是正确结果，不能反过来判自己
        assert '"app."' not in s and '"app"' not in s, "settings 模板变了：app.* 没替换干净"
    return manage, s



# ------------------------------------------------------------------ 注册表
_ADAPTERS: dict = {}


def register(adapter: StackAdapter) -> StackAdapter:
    """注册一个栈适配器（同名覆盖）。"""
    _ADAPTERS[adapter.name] = adapter
    return adapter


register(FastAPIAdapter())
register(DjangoAdapter())


def _register_ecosystem_adapters() -> list:
    """把生态包里的栈适配器注册进来（2026-11-24 打通）。

    ## 为什么必须有这一步

    data/ecosystem/ 下有 8 个栈包（cli_tool / electron / frontend_spa /
    go_service / microservice / node_express / py_lib / spring_boot），
    每个都写好了 adapter（识别 + 探针约定）。

    但核对时发现：**它们一个都没进 _ADAPTERS** —— 上面那两行只注册了
    fastapi / django。于是主流程根本不知道这些栈存在：
      用户选 electron -> _ADAPTERS 里没有 -> 回退 fastapi 适配器
      -> 一个 Electron 项目被按 app/models.py 生成

    ## 安全边界

    · **加载失败不影响启动**：单个包坏了只跳过它，主流程照常
    · **同名不覆盖内置**：fastapi / django 已有，生态包不能顶掉它们
    · **没有生态包目录时零影响**：返回空列表，行为与改动前逐字一致

    返回成功注册的名字列表（供日志/自检用）。
    """
    added = []
    try:
        from . import ecosystem as _eco
        rows = _eco.list_packs() or []
    except Exception:                                    # noqa: BLE001
        return added
    for row in rows:
        if str(row.get("kind") or "") != "stack":
            continue
        name = str(row.get("name") or "").strip()
        if not name or name in _ADAPTERS:
            continue                                     # 不覆盖内置
        try:
            ad, _why = _eco.load_adapter(name)
        except Exception:                                # noqa: BLE001
            continue
        if ad is None:
            continue
        try:
            register(ad)
            added.append(name)
        except Exception:                                # noqa: BLE001
            continue
    return added


ECOSYSTEM_STACKS = _register_ecosystem_adapters()


def names() -> list:
    """已注册的栈名。"""
    return sorted(_ADAPTERS)


def get(name: str) -> StackAdapter:
    """按名字取适配器；未知栈**不猜**，回退默认栈。

    为什么回退而不是报错：选栈写错时让引擎起不来 = 整轮白烧钱；
    回退到 fastapi 至少能跑（而且 stack.json 里写错的名字会被报告点名）。
    """
    key = str(name or "").strip().lower()
    if key in _ADAPTERS:
        return _ADAPTERS[key]
    return _ADAPTERS[DEFAULT_STACK]


def detect(ws) -> str:
    """按**仓库里的实物**判栈（不看配置）。

    为什么必须自动判（2026-11-15 事故）：`from_workspace` 原来只看
    `.sixcoder/stack.json`，缺文件就静默回默认栈 fastapi ——
    而产物已经是 `manage.py` + `config/` + `apps/<域>/` 的 Django 仓
    → 蓝图按 `app.main:app` 规划、接线按 `app/` 扫候选、生成按 `app/services/` 写
    → **一条都落不下，还全绿**。

    判据（先强特征，后弱特征，取不到就 fastapi）：
      · **生态包**（2026-11-19）：先按各包 detect_markers 判 —— 它们用的是比
        "有个 main.py"更具体的特征（如 celery 配置、electron/main.cjs），
        普通 FastAPI/Django 仓不会命中，所以不会被抢判；
      · Django：有 `manage.py`，且存在 `config/settings.py` / `project/settings.py` /
        任意 `<pkg>/settings.py`
      · FastAPI：有 `app/main.py`（且没有 manage.py）
    """
    ws = Path(ws)
    try:
        # **生态包强特征优先**（2026-11-19）：像 microservice（docker-compose/celery）
        # 这类项目同时有 `app/main.py`，若先跑内置 fastapi 分会话规则就永远抢不到。
        # 强特征（docker-compose / celery 配置）比"有个 main.py"更具体，先判。
        # 普通 FastAPI 项目没有这些 marker，不会被抢。
        try:
            from . import ecosystem as _eco
            for _row in (_eco.list_packs() or []):
                if _row.get("kind") == "stack" and _eco.detect(_row["name"], ws):
                    return str(_row["name"])
        except Exception:                                # noqa: BLE001
            pass
        if (ws / "manage.py").is_file():
            for cand in ("config/settings.py", "project/settings.py"):
                if (ws / cand).is_file():
                    return "django"
            if list(ws.glob("*/settings.py")):
                return "django"
        # **前端项目**（2026-11-24 P2）：vite/next 配置 + React/Vue 依赖。
        #
        # 为什么必须在这里判（实测）：生成出前端骨架后，project_stack 仍返回 fastapi，
        # 于是后续按 Web 三分层走、跑 HTTP 判据 —— 明明是个 React 项目，
        # 引擎却拿它当后端。前端特征比"有个 app/main.py"更能定性。
        if _looks_like_frontend(ws):
            # 返回**生态包名**，不是裸 "frontend"。
            # 生态包 frontend_spa 才是真正有适配器/模板/探针的那个；
            # 这里返回 "frontend" 会让下游查不到适配器（2026-11-24 实测：
            # 两个名字并存时，layerplan 按 frontend 匹配、adapters 按 frontend_spa 注册，
            # 结果两边都对不上）。
            return "frontend_spa"
        if (ws / "app" / "main.py").is_file():
            return "fastapi"
    except OSError:
        pass
    return DEFAULT_STACK


def _looks_like_frontend(ws: Path) -> bool:
    """前端项目的实物特征。

    判据（满足任一即算）：
      · vite.config.{js,ts,mjs} / next.config.{js,mjs} / svelte.config.js
      · package.json 里 dependencies/devDependencies 含 react / vue / svelte
    为什么不用"有 index.html"：FastAPI 模板也可能带一个，误判代价大。
    """
    ws = Path(ws)
    for name in ("vite.config.js", "vite.config.ts", "vite.config.mjs",
                 "next.config.js", "next.config.mjs", "svelte.config.js"):
        if (ws / name).is_file():
            return True
    pkg = ws / "package.json"
    if not pkg.is_file():
        return False
    try:
        data = json.loads(pkg.read_text(encoding="utf-8-sig"))
    except Exception:                                    # noqa: BLE001
        return False
    if not isinstance(data, dict):
        return False
    deps = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        d = data.get(key)
        if isinstance(d, dict):
            deps.update(d)
    return any(k in deps for k in ("react", "react-dom", "vue", "svelte", "next",
                                   "@vitejs/plugin-react", "vite"))


def from_workspace(ws) -> StackAdapter:
    """按 `.sixcoder/stack.json` 选栈；**文件缺失或读坏时按实物自动判**。

    显式配置永远优先（人写的最权威）；没有配置才看仓库实物 ——
    这样"新克隆一个 Django 仓、还没写配置"也不会悄悄按 FastAPI 跑。
    """
    try:
        p = Path(ws) / STACK_FILE
        if not p.is_file():
            return get(detect(ws))
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        name = str((data or {}).get("stack") or "")
    except Exception:                                    # noqa: BLE001
        return get(detect(ws))
    if not name:
        return get(detect(ws))
    return get(name)


def write_choice(ws, name: str) -> dict:
    """把选栈结果写进产物仓（幂等）。"""
    key = str(name or "").strip().lower()
    if key not in _ADAPTERS:
        return {"ok": False, "why": f"没有名为 {key!r} 的栈（可用：{', '.join(names())}）"}
    try:
        p = Path(ws) / STACK_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"stack": key}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "why": f"写不了：{exc}"}
    return {"ok": True, "stack": key}


# 进程级"当前栈"：夜班/全量流程一开始 `activate(ws)` 一次，
# 之后所有 `_stack()`（不带 ws 的调用点，全仓几十处）都拿到正确栈。
# 为什么必须有（2026-11-15 实锤）：`current(None)` 恒返回 DEFAULT_STACK(fastapi)
# → Django 项目上 `_ensure_router_decl` 照样补 `router = APIRouter()`、
#   `_ensure_imports` 照样用 fastapi 的表 → 远端写的 Django 代码 import 就炸。
# 这正是"机制存在但恒走默认值"那一类缺陷：检查 `_stack()` 写对了，运行时永远是 fastapi。
_ACTIVE: "StackAdapter | None" = None


def activate(ws) -> StackAdapter:
    """把某个工作区的栈设为**进程默认**（夜班/全量入口各调一次）。"""
    global _ACTIVE
    _ACTIVE = from_workspace(ws)
    return _ACTIVE


def deactivate() -> None:
    global _ACTIVE
    _ACTIVE = None


def active_name() -> str:
    return _ACTIVE.name if _ACTIVE is not None else DEFAULT_STACK


def current(ws=None) -> StackAdapter:
    """当前生效的栈：给了 ws 就按 ws 判；没给就取进程级 `_ACTIVE`，再退默认栈。"""
    if ws is None:
        return _ACTIVE if _ACTIVE is not None else _ADAPTERS[DEFAULT_STACK]
    return from_workspace(ws)
