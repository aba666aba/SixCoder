"""上线机制：把"能上线"里**确定性**的那部分从远端手里拿回来。

为什么单开一层（实测 2026-11-07）：
    · 远端报 9/9 全绿，但真起服务一测：登录不发 token、受保护接口裸奔、列表返回硬编码假数据；
    · 项目没有 README、没有测试、没有 requirements（"pip install -r 后可直接启动"是硬要求）。
这些都不是"模型能力不够"，而是**机制没要求它**。所以这里机械补齐四件事：
    1) 注入三类判定能力：鉴权必须拦得住、页面必须打得开、数据必须真落盘（跨进程回读）；
    2) 生成 README / .gitignore（发布必需，纯模板，不需要模型）；
    3) 从蓝图探针**反向生成** tests/test_api.py（测试由引擎写，作者无法自证清白）；
    4) 上线体检 ship_check：能 import、能跑测试、文档/依赖齐全 —— 结论写进交付报告。
"""
from __future__ import annotations

import json
import re

from pathlib import Path

from . import blueprint as _bp
from . import pyenv as _pyenv
from . import probe as _probe


def _http_caps(bp) -> list:
    return [c for c in bp.caps if str((c.probe or {}).get("kind") or "") == "http"]


def _cap_for(caps: list, needle: str):
    """按"某一步的路径里含 needle"找能力（注册/登录这类锚点）。"""
    for c in caps:
        for st in ((c.probe or {}).get("steps") or []):
            if needle in str((st or {}).get("path") or ""):
                return c
    return None


def _last_step(c) -> dict:
    steps = (c.probe or {}).get("steps") or [{}]
    return (steps[-1] or {})


def _protected(bp) -> list:
    return [c for c in _http_caps(bp) if "${token" in json.dumps(c.probe or {}, ensure_ascii=False)]


def inject_caps(bp, goal: str = "", ws=None) -> list[str]:
    """按蓝图事实注入三类"上线必过"的能力。返回新增的 id 列表（幂等）。"""
    have = {c.id for c in bp.caps}
    http = _http_caps(bp)
    if not http:
        return []
    created: list[str] = []

    # ① 鉴权：凡是需要 token 的接口，**不带 token 必须 401/403**
    prot = _protected(bp)
    if prot and "cap.security.auth_required" not in have:
        # 优先挑 GET（不会写脏数据）
        pick = next((c for c in prot if str(_last_step(c).get("method") or "").upper() == "GET"),
                    prot[0])
        st = _last_step(pick)
        path = str(st.get("path") or "/")
        if "${" in path:                      # 带路径参数的不适合做裸奔检查，换集合路径
            path = "/" + path.strip("/").split("/")[0]
        step = {"method": str(st.get("method") or "GET").upper(), "path": path,
                "expect_status": [401, 403]}
        bp.add(_bp.Cap(id="cap.security.auth_required",
                       desc=f"未带凭证访问 {path} 必须被拒绝（401/403），不能裸奔",
                       probe={"kind": "http",
                              "app": str((pick.probe or {}).get("app") or ""),
                              "steps": [step]},
                       funcs_est=1, modules=[]))
        created.append("cap.security.auth_required")

    # ② 前端：目标提到页面/前端/看板 → 首页必须真能打开
    ui_words = ("前端", "页面", "界面", "看板", "登录页", "仪表盘", "dashboard", "html", "网站")
    if any(w in (goal or "").lower() for w in ui_words) and "cap.ui.index" not in have:
        bp.add(_bp.Cap(id="cap.ui.index",
                       desc="打开首页 / 返回可渲染的 HTML 页面（不是 404、不是纯 JSON）",
                       probe={"kind": "http",
                              "app": str((http[0].probe or {}).get("app") or ""),
                              "steps": [{"method": "GET", "path": "/", "expect_status": 200,
                                         "body_has_any": ["<html", "<!doctype", "<title"]}]},
                       funcs_est=2, modules=["app/web.py"]))
        created.append("cap.ui.index")

    # ③ 持久化：写进去的数据，**换一个进程**还得读得到
    seen: set = set()
    for c in http:
        st = _last_step(c)
        path = str(st.get("path") or "")
        if str(st.get("method") or "").upper() != "POST" or not path or path == "/":
            continue
        reader = next((x for x in http
                       if str(_last_step(x).get("method") or "").upper() == "GET"
                       and str(_last_step(x).get("path") or "").rstrip("/") == path.rstrip("/")), None)
        if reader is None or path in seen:
            continue
        seen.add(path)
        cid = f"cap.data.persist{path.replace('/', '_')}"
        if cid in have:
            continue
        host = next((x.id for x in _protected(bp)), "")
        steps = []
        # 借一条已有的鉴权链（注册/登录），保证写数据这一端也是合法访问
        if host:
            host_steps = (_protected(bp)[0].probe or {}).get("steps") or []
            steps.extend(json.loads(json.dumps(host_steps[:-1])))
        body = dict(st.get("json") or {"title": "持久化探针", "description": "persist probe"})
        steps.append({"method": "POST", "path": path, "json": body,
                      "expect_status": st.get("expect_status") if isinstance(
                          st.get("expect_status"), int) else [200, 201]})
        steps.append({"method": "GET", "path": path,
                      "body_has": str(body.get("title") or "")})
        bp.add(_bp.Cap(id=cid,
                       desc=f"{path} 写入的数据，换进程回读仍然在（真落盘，不是内存字典）",
                       probe={"kind": "http",
                              "app": str((c.probe or {}).get("app") or ""),
                              "steps": steps, "split": True},
                       deps=[host] if host else [],
                       funcs_est=1, modules=[]))
        created.append(cid)
    # ④ 负向用例：错误处理是目标里的硬要求（"统一错误处理与权限校验"），
    #    但蓝图只排 happy path —— 于是"非法输入返回 500"也能全绿（实测 2026-11-07）。
    posts = [c for c in http if str(_last_step(c).get("method") or "").upper() == "POST"
             and "/auth/" not in str(_last_step(c).get("path") or "")]
    if posts and "cap.api.validation" not in have:
        st = _last_step(posts[0])
        bp.add(_bp.Cap(id="cap.api.validation",
                       desc=f"给 {st.get('path')} 传非法请求体必须被拒绝（422），不能 500 或静默成功",
                       probe={"kind": "http",
                              "app": str((posts[0].probe or {}).get("app") or ""),
                              "steps": [{"method": "POST", "path": str(st.get("path") or ""),
                                         "json": {"__invalid__": True},
                                         "expect_status": [400, 422]}]},
                       funcs_est=1, modules=[]))
        created.append("cap.api.validation")

    if "cap.api.not_found" not in have:
        dyn = None
        for c in http:
            st0 = _last_step(c)
            p = str(st0.get("path") or "")
            # 只挑 **GET + 动态段**：拿 PUT/DELETE 的路径去发 GET 只会得到 405，
            # 那是探针自己造出来的假失败，不是产物的问题（实测 2026-11-07）。
            if str(st0.get("method") or "").upper() != "GET":
                continue
            if "{" in p or "${" in p:
                dyn = p
                break
        if dyn:
            probe_path = re.sub(r"\$\{(\w+)\}", "999999", dyn)
            probe_path = re.sub(r"\{\w+\}", "999999", probe_path)
            probe_path = re.sub(r":\w+(?=/|$)", "999999", probe_path)
            probe_path = re.sub(r"/(\d+)(?=/|$)", "/999999", probe_path)
            bp.add(_bp.Cap(id="cap.api.not_found",
                           desc=f"访问不存在的资源（{probe_path}）必须返回 404，不能 500 或返回空对象",
                           probe={"kind": "http",
                                  "app": str((http[0].probe or {}).get("app") or ""),
                                  "steps": [{"method": "GET", "path": probe_path,
                                             "expect_status": 404}]},
                           funcs_est=1, modules=[]))
            created.append("cap.api.not_found")

    # ⑤ 密码绝不能回显（明文密码出现在响应里 = 直接不能上线，但任何探针都不查它）
    if "cap.security.no_password_leak" not in have:
        reg_cap = _cap_for(http, "/register")
        if reg_cap is not None:
            st = _last_step(reg_cap)
            body = dict(st.get("json") or {})
            bp.add(_bp.Cap(id="cap.security.no_password_leak",
                           desc="注册/登录的响应里不得出现 password 字段（口令不许回显）",
                           probe={"kind": "http",
                                  "app": str((reg_cap.probe or {}).get("app") or ""),
                                  "steps": [{"method": str(st.get("method") or "POST").upper(),
                                             "path": str(st.get("path") or "/auth/register"),
                                             "json": body,
                                             "expect_status": [200, 201, 409],
                                             "body_not_has": ["password"]}]},
                           funcs_est=1, modules=[]))
            created.append("cap.security.no_password_leak")

    # ⑥ 前端不能只是一段静态 HTML：页面里必须真的调用接口
    if "cap.ui.calls_api" not in have and "cap.ui.index" in {c.id for c in bp.caps} | set(created):
        bp.add(_bp.Cap(id="cap.ui.calls_api",
                       desc="首页里的脚本真的会调用后端接口（出现 fetch/XMLHttpRequest/api 路径）",
                       probe={"kind": "http",
                              "app": str((http[0].probe or {}).get("app") or ""),
                              "steps": [{"method": "GET", "path": "/", "expect_status": 200,
                                         "body_has_any": ["fetch(", "XMLHttpRequest", "/api/",
                                                          "/auth/", "/tickets"]}]},
                       funcs_est=1, modules=[]))
        created.append("cap.ui.calls_api")

    # ⑦ 幂等：重复提交同一份注册请求，必须被**明确拒绝**（4xx），不能 500、不能建出两条。
    #    蓝图只排 happy path → "重复用户名触发 IntegrityError 未捕获 → 500" 一路全绿；
    #    用户手抖点两次注册就 500，这是能不能上线的问题（实测 2026-11-07 无人判定）。
    reg = _cap_for(http, "/register")
    login_c = _cap_for(http, "/login")
    if reg is not None and "cap.api.idempotent" not in have:
        first = json.loads(json.dumps(_last_step(reg)))
        for k in ("body_has_any", "body_not_has", "body_has"):
            first.pop(k, None)
        first["expect_status"] = [200, 201]
        dup = json.loads(json.dumps(first))
        dup["expect_status"] = [400, 409]
        dup["body_not_has"] = ["Traceback", "IntegrityError", "sqlalchemy.exc", "UNIQUE constraint"]
        bp.add(_bp.Cap(id="cap.api.idempotent",
                       desc=(f"重复提交同一份 {first.get('path')} 必须被拒绝（4xx 且不抛栈），"
                             "不能 500、也不能建出第二条"),
                       probe={"kind": "http",
                              "app": str((reg.probe or {}).get("app") or ""),
                              "steps": [first, dup]},
                       deps=[reg.id], funcs_est=1, modules=[]))
        created.append("cap.api.idempotent")

    # ⑧ 并发写：同时发 N 个写请求，必须**全部成功**且全部落库（不丢单、不 500）。
    #    单请求验收永远看不出"共享 sqlite 连接被多线程踩"、"读-改-写丢更新"、"全局变量没锁"；
    #    这类 bug 只在并发下出现，而它正是"能不能上线"的分水岭。
    create_c = None
    for c in http:
        st0 = _last_step(c)
        p0 = str(st0.get("path") or "")
        if str(st0.get("method") or "").upper() != "POST" or not p0 or p0 == "/":
            continue
        if "/auth/" in p0 or "{" in p0:
            continue
        create_c = c
        break
    reader_c = None
    if create_c is not None:
        cpath = str(_last_step(create_c).get("path") or "").rstrip("/")
        reader_c = next((x for x in http
                         if str(_last_step(x).get("method") or "").upper() == "GET"
                         and str(_last_step(x).get("path") or "").rstrip("/") == cpath), None)
    if create_c is not None and reader_c is not None and "cap.data.concurrent" not in have:
        N = 8
        exp_c = _last_step(create_c).get("expect_status")
        if isinstance(exp_c, int):
            exp_c = exp_c
        elif isinstance(exp_c, list):
            exp_c = [int(x) for x in exp_c if isinstance(x, int) and x < 400] or [200, 201]
        else:
            exp_c = [200, 201]
        steps: list = []
        hdr: dict = {}
        deps = [create_c.id, reader_c.id]
        # 需要鉴权就先把 token 拿到手（登录本身没通时这条能力会被 deps 挡成 blocked，
        # 不会反过来把"登录没做"误算成"并发不行"）。
        if reg is not None and login_c is not None:
            r0 = json.loads(json.dumps(_last_step(reg)))
            for k in ("body_has_any", "body_not_has", "body_has"):
                r0.pop(k, None)
            r0["expect_status"] = [200, 201, 409]
            l0 = json.loads(json.dumps(_last_step(login_c)))
            l0["expect_status"] = [200, 201]
            l0["save"] = {"token": ["token", "access_token", "jwt"]}
            steps += [r0, l0]
            hdr = {"Authorization": "Bearer ${token}"}
            deps.append(login_c.id)
        body = json.loads(json.dumps(_last_step(create_c).get("json") or {}))
        cpath_s = str(_last_step(create_c).get("path") or "")
        steps.append({"method": "POST", "path": cpath_s, "json": body, "headers": hdr or None,
                      "parallel": N, "expect_status": exp_c})
        steps.append({"method": "GET", "path": str(_last_step(reader_c).get("path") or ""),
                      "headers": hdr or None, "expect_status": 200, "expect_count_min": N})
        bp.add(_bp.Cap(id="cap.data.concurrent",
                       desc=(f"同时发 {N} 个 {cpath_s} 写请求：必须全部成功（{exp_c}），"
                             f"随后能读到至少 {N} 条（不丢单、不 500）"),
                       probe={"kind": "http",
                              "app": str((create_c.probe or {}).get("app") or ""),
                              "steps": steps},
                       deps=[d for d in deps if d], funcs_est=2, modules=[]))
        created.append("cap.data.concurrent")

    # ⑧ 业务不变式 → 负向探针（模型层给语义，引擎按模板执行）
    # 为什么放注入里而不是手工写蓝图（实测 2026-11-09 00:27）：夜班 `replan` 会重建
    # 能力表，手工塞进 blueprint.json 的 `cap.inv.*` 被整表覆盖冲掉了（v38→v39，
    # total 81→76）。注入是幂等的、每轮 replan 后都会补回来 —— 机制该待在这里。
    # 模型层不可用就跳过，绝不让它挡住整条注入（模型是可选的增强，不是依赖）。
    if ws is not None:
        try:
            from . import model as _md
            _m = _md.load(ws)
            if _m is not None:
                have2 = {c.id for c in bp.caps}
                for ic in _md.invariant_probes(_m, bp):
                    if ic["cap_id"] in have2:
                        continue
                    bp.add(_bp.Cap(id=ic["cap_id"], desc=ic["desc"], funcs_est=3,
                                   probe={"kind": "http", "steps": ic["steps"]}))
                    created.append(ic["cap_id"])
        except Exception:                                 # noqa: BLE001
            pass

    return created


def ensure_env(ws) -> dict:
    """扫产物里用到的环境变量 → 生成 .env / .env.example，并返回默认值。

    为什么必须有（实测 2026-11-07）：设计文档里"JWT 密钥存环境变量"被作者照做了，
    于是 `sqlite3.connect(os.environ["DB_PATH"])` / `os.environ["JWT_SECRET"]` 直接拿 None
    → `TypeError: expected str, bytes or os.PathLike object`，**所有接口一起 500**。
    这不是模型写错，是系统没给运行环境定契约：谁提供变量、缺省值是什么、例子在哪。
    """
    import secrets

    ws = Path(ws)
    names: set[str] = set()
    pats = [r'os\.environ\[\s*["\']([A-Z][A-Z0-9_]*)["\']\s*\]',
            r'os\.getenv\(\s*["\']([A-Z][A-Z0-9_]*)["\']',
            r'environ\.get\(\s*["\']([A-Z][A-Z0-9_]*)["\']',
            r'os\.environ\.get\(\s*["\']([A-Z][A-Z0-9_]*)["\']']
    for p in ws.rglob("*.py"):
        if ".sixcoder" in p.parts:
            continue
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for pat in pats:
            names |= set(re.findall(pat, src))
    if not names:
        return {"ok": True, "names": [], "wrote": []}

    # 缺省值：能猜的给合理值，猜不到的给空串（.env.example 里留占位）
    guess = {}
    for n in names:
        if "SECRET" in n or "KEY" in n or "TOKEN" in n:
            guess[n] = secrets.token_hex(16)
        elif "DB" in n or "DATABASE" in n:
            guess[n] = "app.db"
        elif "HOST" in n:
            guess[n] = "127.0.0.1"
        elif "PORT" in n:
            guess[n] = "8000"
        else:
            guess[n] = ""

    env_file = ws / ".env"
    cur: dict[str, str] = {}
    if env_file.is_file():
        for ln in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in ln and not ln.strip().startswith("#"):
                k, _, v = ln.partition("=")
                cur[k.strip()] = v.strip()
    wrote: list[str] = []
    try:
        if not env_file.is_file() or any(n not in cur for n in names):
            merged = dict(cur)
            for n in sorted(names):
                merged.setdefault(n, guess[n])
            env_file.write_text("\n".join(f"{k}={v}" for k, v in merged.items()) + "\n",
                                encoding="utf-8")
            wrote.append(".env")
            cur = merged
        ex = ws / ".env.example"
        if not ex.is_file():
            ex.write_text("\n".join(f"{n}=" for n in sorted(names)) + "\n", encoding="utf-8")
            wrote.append(".env.example")
    except OSError:
        pass
    return {"ok": True, "names": sorted(names), "wrote": wrote, "values": cur}


def write_env_block(ws, bp) -> dict:
    """把"运行时需要哪些环境变量"写进 README（部署时不用猜）。"""
    info = ensure_env(ws)
    if not info.get("names"):
        return info
    ws = Path(ws)
    p = ws / "README.md"
    try:
        text = p.read_text(encoding="utf-8", errors="replace") if p.is_file() else ""
    except OSError:
        text = ""
    if "## 环境变量" not in text:
        block = ["", "## 环境变量", "",
                 "启动前需要这些变量（已生成 `.env` 与 `.env.example`）：", "",
                 "| 变量 | 说明 |", "|---|---|"]
        for n in info["names"]:
            block.append(f"| `{n}` | {'JWT/密钥类，已给随机缺省值' if ('SECRET' in n or 'KEY' in n or 'TOKEN' in n) else '按需填写'} |")
        block += ["", "```bash", "cp .env.example .env   # 再按需修改",
                  "uvicorn app.main:app --reload", "```", ""]
        try:
            p.write_text(text.rstrip() + "\n" + "\n".join(block), encoding="utf-8")
        except OSError:
            pass
    return info


def write_docs(ws, bp, coverage: dict | None = None) -> dict:
    """生成 README.md 与 .gitignore（发布必需，纯模板）。"""
    ws = Path(ws)
    wrote: list[str] = []
    done = [c for c in bp.caps if c.status == "done"]
    rows = [f"| `{c.id}` | {c.desc[:40]} | {_probe.describe(c.probe)[:60]} | ✅ |" for c in done]
    req = ws / "requirements.txt"
    deps = req.read_text(encoding="utf-8", errors="replace").strip() if req.exists() else ""
    readme = f"""# {bp.goal.splitlines()[0][:60] if bp.goal else 'SixCoder 产物'}

> 本文件由引擎生成（与代码同源，随验收结果更新）。

## 快速开始

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

- 交互式 API 文档：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /docs` 返回 200 即表示应用已起

## 已验收能力（{len(done)}/{len(bp.caps)}）

| 能力 | 说明 | 观测方式 | 状态 |
|---|---|---|---|
{chr(10).join(rows) if rows else '| - | - | - | - |'}

## 依赖

```
{deps or '（未声明）'}
```

## 架构

- `app/main.py`：ASGI 入口（由引擎生成，聚合各模块的 `router`）
- 各功能模块：按能力分文件，模块内 `router = APIRouter()`
- 数据：SQLite（相对路径，运行目录下生成）

## 验收方式

每条能力都由**真实 HTTP 请求**验证（`core/probe.py` 的 `http` 探针）：
注册 → 登录 → 调接口 → 断言状态码与 JSON 字段；持久化能力用**跨进程回读**验证。
"""
    try:
        (ws / "README.md").write_text(readme, encoding="utf-8")
        wrote.append("README.md")
        gi = ws / ".gitignore"
        if gi.exists():
            try:
                g = gi.read_text(encoding="utf-8", errors="replace")
                if ".env" not in [ln.strip() for ln in g.splitlines()]:
                    gi.write_text(g.rstrip() + "\n.env\n", encoding="utf-8")
            except OSError:
                pass
        if not gi.exists():
            gi.write_text("__pycache__/\n*.pyc\n*.db\n.env\n.pytest_cache/\n.mypy_cache/\n"
                          ".sixcoder/tmp/\n.venv/\n", encoding="utf-8")
            wrote.append(".gitignore")
    except OSError:
        pass
    return {"wrote": wrote}


def _pyval(v, user_key: str = "_u") -> str:
    """把 JSON 值渲染成 Python 字面量；username/user/email 换成随机值。

    为什么要随机（实测 2026-11-07）：注册用固定用户名，第二次跑测试就是 409，
    测试变成"只能跑一次"——CI 里等于没用。
    """
    if isinstance(v, dict):
        parts = []
        for k, val in v.items():
            if str(k).lower() in ("username", "user", "email", "account") and isinstance(val, str):
                parts.append(f"{k!r}: {user_key}")
            else:
                parts.append(f"{k!r}: {_pyval(val, user_key)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(v, list):
        return "[" + ", ".join(_pyval(x, user_key) for x in v) + "]"
    return repr(v)


def _test_code(bp) -> str:
    """从蓝图探针反向生成 pytest（测试由引擎写，作者不能自己判自己）。"""
    out = ['"""端到端测试（**由引擎从蓝图生成**，与验收判据同源，不要手改）。"""',
           "from __future__ import annotations", "", "import asyncio", "import re",
           "import uuid", "",
           "import httpx", "from fastapi.testclient import TestClient", "",
           "from httpx import ASGITransport", "", "from app.main import app", "",
           "",
           "def _c():", "    return TestClient(app)", "",
           "",
           "def _race(method, path, n, **kw):",
           '    """并发发 n 次同一个请求（sync 端点会被 FastAPI 丢进线程池，真并行）。"""',
           "    async def one(cli, out, i):",
           "        out[i] = await cli.request(method, path, **kw)",
           "",
           "    async def go():",
           "        out = [None] * n",
           '        async with httpx.AsyncClient(transport=ASGITransport(app=app),',
           '                                     base_url="http://t") as cli:',
           "            await asyncio.gather(*[one(cli, out, i) for i in range(n)])",
           "        return out",
           "",
           "    return asyncio.run(go())",
           "",
           "",
           "def _count(r):",
           "    try:",
           "        p = r.json()",
           "    except Exception:",
           "        return -1",
           "    if isinstance(p, list):",
           "        return len(p)",
           "    if isinstance(p, dict):",
           '        for k in ("total", "count", "total_count", "size"):',
           "            if isinstance(p.get(k), int):",
           "                return p[k]",
           '        for k in ("items", "data", "results", "list", "tickets", "rows"):',
           "            if isinstance(p.get(k), list):",
           "                return len(p[k])",
           "    return -1", "",
           "",
           "def _sub(obj, vars):",
           '    """把 $ + {var} 形式的占位符换成运行时值（token / id 之类）。"""',
           "    if isinstance(obj, str):",
           '        return re.sub(r"\\$\\{(\\w+)\\}", lambda m: str(vars.get(m.group(1))), obj)',
           "    if isinstance(obj, dict):",
           "        return {k: _sub(v, vars) for k, v in obj.items()}",
           "    if isinstance(obj, list):",
           "        return [_sub(v, vars) for v in obj]",
           "    return obj", ""]
    for c in _http_caps(bp):
        steps = (c.probe or {}).get("steps") or []
        if not steps:
            continue
        name = "test_" + re.sub(r"[^a-z0-9]+", "_", str(c.id).lower()).strip("_")
        body = ["", f"def {name}():", "    c = _c()", "    vars = {}", '    _u = "u_" + uuid.uuid4().hex[:8]']
        for st in steps:
            st = st or {}
            method = str(st.get("method") or "GET").lower()
            path = str(st.get("path") or "/")
            kw = [f'_sub({json.dumps(path, ensure_ascii=False)}, vars)']
            if st.get("json") is not None:
                kw.append("json=_sub(" + _pyval(st["json"]) + ", vars)")
            if st.get("headers"):
                kw.append("headers=_sub(" + json.dumps(st["headers"], ensure_ascii=False) + ", vars)")
            par = st.get("parallel")
            if isinstance(par, int) and par > 1:
                kwp = [f'_sub({json.dumps(path)}, vars)']
                if st.get("json") is not None:
                    kwp.append("json=_sub(" + _pyval(st["json"]) + ", vars)")
                if st.get("headers"):
                    kwp.append("headers=_sub(" + json.dumps(st["headers"], ensure_ascii=False) + ", vars)")
                body.append(f"    _rs = _race(\"{method}\", "
                            + ", ".join([kwp[0], str(par)] + kwp[1:]) + ")")
                stt_p = st.get("expect_status")
                if isinstance(stt_p, list):
                    body.append(f"    assert all(r.status_code in {stt_p} for r in _rs), "
                                "[r.text[:120] for r in _rs]")
                elif isinstance(stt_p, int):
                    body.append(f"    assert all(r.status_code == {stt_p} for r in _rs), "
                                "[r.text[:120] for r in _rs]")
                else:
                    body.append("    assert all(200 <= r.status_code < 300 for r in _rs), "
                                "[r.text[:120] for r in _rs]")
                continue
            body.append(f"    r = c.{method}(" + ", ".join(kw) + ")")
            stt = st.get("expect_status")
            if isinstance(stt, list):
                body.append(f"    assert r.status_code in {stt}, r.text[:200]")
            elif isinstance(stt, int):
                body.append(f"    assert r.status_code == {stt}, r.text[:200]")
            else:
                body.append("    assert 200 <= r.status_code < 300, r.text[:200]")
            for k, src in (st.get("save") or {}).items():
                cands = src if isinstance(src, list) else [src]
                body.append("    try:")
                body.append("        _p = r.json()")
                body.append("    except Exception:")
                body.append("        _p = {}")
                body.append(f"    vars[{k!r}] = " + " or ".join(
                    f'_p.get("{str(x).split(".")[-1]}")' for x in cands))
            if st.get("body_has"):
                body.append("    assert " + json.dumps(str(st["body_has"]), ensure_ascii=False) + " in r.text")
            ec = st.get("expect_count")
            ecmin = st.get("expect_count_min")
            if ec is not None:
                body.append(f"    assert _count(r) == {int(ec)}, "
                            f"f\"应有 {int(ec)} 条，实际 {{_count(r)}} 条（并发写丢了 / 没落库）\"")
            elif ecmin is not None:
                body.append(f"    assert _count(r) >= {int(ecmin)}, "
                            f"f\"至少该有 {int(ecmin)} 条，实际 {{_count(r)}} 条"
                            f"（并发写丢了 / 没落库）\"")
            anyw = st.get("body_has_any")
            if anyw:
                ws_ = [anyw] if isinstance(anyw, str) else list(anyw or [])
                if ws_:
                    body.append("    assert any(" + json.dumps(ws_[0], ensure_ascii=False) +
                                " in r.text for w in " + json.dumps(ws_, ensure_ascii=False) + ")")
        out += body
    return "\n".join(out) + "\n"


def write_tests(ws, bp) -> dict:
    """生成 tests/test_api.py（引擎所有）。返回 {path, tests}。"""
    ws = Path(ws)
    code = _test_code(bp)
    n = code.count("\ndef test_")
    d = ws / "tests"
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "test_api.py").write_text(code, encoding="utf-8")
        init = d / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "why": str(exc)[:120], "tests": 0}
    return {"ok": True, "path": str(d / "test_api.py"), "tests": n}


def ship_check(ws, bp) -> dict:
    """上线体检：能 import、测试能跑、文档与依赖齐全。结论进交付报告。"""
    import subprocess
    import sys

    ws = Path(ws)
    rows: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        rows.append({"name": name, "ok": bool(ok), "detail": detail[:200]})

    # 1) 应用能 import（ASGI 对象存在）
    try:
        p = subprocess.run([sys.executable, "-c",
                            "from app.main import app; print(len(getattr(app, 'routes', [])))"],
                           cwd=str(ws), capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        add("应用可导入", p.returncode == 0,
            (p.stdout or p.stderr or "").strip()[:160])
    except Exception as exc:                             # noqa: BLE001
        add("应用可导入", False, f"{type(exc).__name__}: {exc}")

    # 2) 文档齐全
    for f in ("README.md", "requirements.txt"):
        add(f"{f} 存在", (ws / f).is_file(), str(ws / f))

    # 2b) /docs 真能打开（目标里"OpenAPI 文档"是硬要求）
    _code = ("from fastapi.testclient import TestClient\n"
             "from app.main import app\n"
             "print(TestClient(app).get('/docs').status_code)\n")
    try:
        p = subprocess.run([sys.executable, "-c", _code], cwd=str(ws),
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace")
        got = (p.stdout or "").strip().splitlines()
        add("/docs 可打开", p.returncode == 0 and bool(got) and got[-1].startswith("2"),
            got[-1] if got else (p.stderr or "")[-120:])
    except Exception as exc:                             # noqa: BLE001
        add("/docs 可打开", False, f"{type(exc).__name__}: {exc}")

    # 2c) **真起 uvicorn**（不是 TestClient）：目标要求"pip install 后可直接 uvicorn 启动"
    import os as _os
    import time as _time
    import urllib.request as _url
    port = 18000 + (_os.getpid() % 500)
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
             "--port", str(port), "--log-level", "warning"],
            cwd=str(ws), env=_probe._env(ws),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        okp, det = False, "启动超时"
        for _ in range(40):
            _time.sleep(0.5)
            if proc.poll() is not None:
                det = "进程退出：" + (proc.stderr.read().decode("utf-8", "replace")[-160:]
                                     if proc.stderr else "")
                break
            try:
                with _url.urlopen(f"http://127.0.0.1:{port}/docs", timeout=2) as r:
                    okp, det = (r.status == 200), f"/docs {r.status}（: {port}）"
                    break
            except Exception as exc:                     # noqa: BLE001
                det = f"{type(exc).__name__}: {exc}"[:100]
        add("uvicorn 真启动", okp, det)
    except Exception as exc:                             # noqa: BLE001
        add("uvicorn 真启动", False, f"{type(exc).__name__}: {exc}")
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:                            # noqa: BLE001
                pass

    # 3) 测试能跑
    if (ws / "tests" / "test_api.py").is_file():
        try:
            # 必须带 `-o pythonpath=`：否则 tests/test_api.py 里的 `from app…` 在
            # `._pth` 隔离下导入失败，被误判成"测试不通过"（2026-11-23）。
            p = subprocess.run(_pyenv.pytest_argv(ws, ["tests"]),
                               cwd=str(ws), capture_output=True, text=True, timeout=300,
                               encoding="utf-8", errors="replace",
                               env={**__import__("os").environ, "PYTHONDONTWRITEBYTECODE": "1"})
            tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()
            add("测试通过", p.returncode == 0, (tail[-1] if tail else "")[:160])
        except Exception as exc:                         # noqa: BLE001
            add("测试通过", False, f"{type(exc).__name__}: {exc}")
    else:
        add("测试通过", False, "没有 tests/test_api.py")

    ok = all(r["ok"] for r in rows)
    return {"ok": ok, "rows": rows}
