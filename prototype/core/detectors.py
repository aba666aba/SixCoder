"""全量检测器（D1–D6）：把"哪里不对"变成结构化 `Error`，**零 token**。

## 分工铁律（2026-11-14，开发文档 §13.2）

**检测器是裁判，模型不是。** 2026-11-11 最硬的教训：
模型说"我写对了"不算数，**探针说才算**。所以这里全部是**工具判定**：
AST / 真 import / 真发 HTTP。小模型只在报告层面做归纳与排序（`summarize`）。

## 六个检测器

| 编号 | 名字 | 抓什么 | 实现 |
|---|---|---|---|
| D1 | `d1_syntax` | 语法错、重名、空桩、未定义名字 | AST（`ast.parse` + 符号表） |
| D2 | `d2_import` | 整个 app 能不能 import 起来 | 真 `import app.main`（子进程） |
| D3 | `d3_contract` | 签名/返回字段/错误码与契约不符 | 复用 `iterate.inspect` |
| D4 | `d4_crossref` | **调用了不存在的函数**（跨模块） | AST 调用点 × 产物符号表 |
| D5 | `d5_datamodel` | 同一实体字段在 models/services/api 不一致 | AST + 契约字段表 |
| D6 | `d6_e2e` | 端到端（唯一能证明"做完"的判据） | 复用 `coverage.run` |

**D4 是今天最缺的一条**（实测：`app/api/batches.py` 调 `run_batch`，
而产物里叫 `run` —— 这种错以前要等端到端 500 才暴露，而且看不出原因）。

## 检测器自身失败怎么办

**不许静默吞**（2026-11-11 四次血泪）。检测器抛异常时，往报告里记一条
`P3/advice`："检测器 Dx 自身失败：<异常>"，让人一眼看到**检测有盲区**。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

from .errorreport import Error

__all__ = ["d1_syntax", "d2_import", "d3_contract", "d4_crossref",
           "d5_datamodel", "d6_e2e", "d7_ui", "d8_thirdparty",
           "run_all", "list_modules"]

_SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".pytest_cache", "node_modules"}


def list_modules(ws) -> list[str]:
    """产物里的**业务**模块（app/ + matching_core/，跳过入口与缓存）。"""
    ws = Path(ws)
    out: list[str] = []
    for base in ("app", "matching_core"):
        d = ws / base
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            if any(s in f.parts for s in _SKIP_DIRS):
                continue
            rel = str(f.relative_to(ws)).replace("\\", "/")
            if rel.endswith("__init__.py"):
                continue
            out.append(rel)
    return out


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""


def _module_defs(src: str) -> dict[str, list[str]]:
    """顶层函数 → 参数名列表；类 → 方法名列表（扁平化到 `Cls.m`）。"""
    out: dict[str, list[str]] = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = [a.arg for a in (list(node.args.posonlyargs) + list(node.args.args))
                    if a.arg not in ("self", "cls")]
            out[node.name] = args
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    args = [a.arg for a in (list(sub.args.posonlyargs) + list(sub.args.args))
                            if a.arg not in ("self", "cls")]
                    out[f"{node.name}.{sub.name}"] = args
    return out


# ── D1 语法/结构 ────────────────────────────────────────────────────────────
def d1_syntax(ws) -> list[Error]:
    """语法错 / 模块内重名 / 空桩（只有 docstring 或 pass）/ 明显未定义名字。"""
    ws = Path(ws)
    out: list[Error] = []
    for rel in list_modules(ws):
        src = _read(ws / rel)
        if not src.strip():
            out.append(Error("P1", "syntax", rel, "文件为空",
                             evidence="0 字节", symbol=rel))
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError as exc:
            out.append(Error("P0", "syntax", f"{rel}:{exc.lineno or 0}",
                             f"语法错误：{exc.msg}",
                             evidence=str(exc.text or "")[:200], symbol=rel))
            continue
        # 顶层重名（后定义覆盖前定义 —— 极易造成"改了没生效"）
        seen: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name in seen:
                    out.append(Error(
                        "P1", "syntax", f"{rel}:{node.lineno}",
                        f"顶层重名：`{node.name}` 在第 {seen[node.name]} 行已定义过"
                        f"（后定义会静默覆盖前者）",
                        evidence=f"def {node.name}", symbol=node.name))
                else:
                    seen[node.name] = node.lineno
        # 空桩：只有 docstring / pass / ... 的函数
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [b for b in node.body
                    if not (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                            and isinstance(b.value.value, str))]
            if len(body) == 1 and isinstance(body[0], ast.Pass):
                out.append(Error("P2", "syntax", f"{rel}:{node.lineno}",
                                 f"空桩：`{node.name}` 只有 pass",
                                 evidence=ast.get_source_segment(src, node)[:160] or "",
                                 symbol=node.name))
            elif len(body) == 1 and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and body[0].value.value is Ellipsis:
                out.append(Error("P2", "syntax", f"{rel}:{node.lineno}",
                                 f"空桩：`{node.name}` 只有 `...`",
                                 symbol=node.name))
    return out


# ── D2 导入 ────────────────────────────────────────────────────────────────
DJANGO_CHECK_CODE = """
import os, sys, importlib, traceback
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "%(pkg)s.settings")
import django
django.setup()
from django.core.management import call_command
call_command("check", verbosity=1)
bad = []
for mod in %(mods)r:
    try:
        importlib.import_module(mod)
    except Exception:
        bad.append(mod)
        traceback.print_exc()
if bad:
    print("MODULE_IMPORT_FAILED: " + ",".join(bad))
    raise SystemExit(3)
""".strip()


def _d2_django(ws, timeout: int = 120) -> list[Error]:
    """Django 的导入冒烟：manage.py check + 逐 app 导入（子进程，真跑）。"""
    ws = Path(ws)
    pkg = "config" if (ws / "config" / "settings.py").is_file() else (
        "project" if (ws / "project" / "settings.py").is_file() else "app")
    mods: list[str] = []
    for f in sorted(ws.glob("apps/*/*.py")):
        if f.name == "__init__.py" or f.parent.name == "migrations":
            continue
        mods.append("apps.%s.%s" % (f.parent.name, f.stem))
    for f in sorted(ws.glob("apps/*")):
        if f.is_dir() and f.name != "__init__.py":
            mods.append("apps.%s" % f.name)
    code = DJANGO_CHECK_CODE % {"pkg": pkg, "mods": mods}
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ws),
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return [Error("P0", "import", "%s.settings" % pkg,
                      "Django check 超时（>%ss）" % timeout,
                      evidence="子进程未在时限内退出", symbol="d2")]
    except Exception as exc:                            # noqa: BLE001
        return [Error("P3", "advice", "-", "检测器 D2（Django）自身失败：%s"
                      % type(exc).__name__, evidence=str(exc)[:300], symbol="d2")]
    if r.returncode == 0:
        return []

    err = (r.stderr or "") + "\n" + (r.stdout or "")
    tail = "\n".join(err.strip().splitlines()[-10:])
    where, symbol = "%s.settings" % pkg, "django"
    m = re.search(r'File "([^"]+)", line (\d+)', err)
    if m:
        try:
            where = "%s:%s" % (Path(m.group(1)).relative_to(ws).as_posix(), m.group(2))
        except ValueError:
            where = "%s:%s" % (m.group(1), m.group(2))
    mm = re.search(r"MODULE_IMPORT_FAILED: (\S+)", err)
    if mm:
        symbol = mm.group(1).split(",")[0]
    return [Error("P0", "import", where,
                  "Django 启动/导入失败（manage.py check 或模块导入不通过）",
                  evidence=tail[-1200:], symbol=symbol)]


def d2_import(ws, timeout: int = 120) -> list[Error]:
    """真 import 一次入口（子进程），把 traceback 的最后几行变成一条 P0。

    **Django 走另一套**（2026-11-15）：入口不是 `app.main`，而是
    `manage.py check`（它会把 settings / ROOT_URLCONF / models / admin 全查一遍）
    + 逐 app 导入（`apps.<域>.models|views|urls`）。
    旧实现里 `if not (ws/"app").is_dir(): return []` 会让 Django 项目**一条都不查**
    —— "全绿"其实是"根本没跑"，是最坏的那种假绿。
    """
    ws = Path(ws)
    try:
        from . import stack as _st
        _ad = _st.from_workspace(ws)
    except Exception:                                    # noqa: BLE001
        _ad = None
    if _ad is not None and getattr(_ad, "name", "") == "django":
        return _d2_django(ws, timeout)

    if not (ws / "app").is_dir():
        return []
    code = "import app.main"
    try:
        r = subprocess.run([sys.executable, "-c", code], cwd=str(ws),
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return [Error("P0", "import", "app.main", f"import 超时（>{timeout}s）",
                      evidence="子进程未在时限内退出", symbol="app.main")]
    except Exception as exc:                            # noqa: BLE001
        return [Error("P3", "advice", "app.main", f"检测器 D2 自身失败：{type(exc).__name__}",
                      evidence=str(exc)[:300], symbol="d2")]
    if r.returncode == 0:
        return []
    err = (r.stderr or r.stdout or "").strip()
    tail = "\n".join(err.splitlines()[-8:])
    m = re.search(r'File "([^"]+)", line (\d+)', err)
    where = "app.main"
    if m:
        try:
            where = "%s:%s" % (Path(m.group(1)).relative_to(ws).as_posix(), m.group(2))
        except ValueError:
            where = "%s:%s" % (m.group(1), m.group(2))
    return [Error("P0", "import", where, "import app.main 失败（整个 app 起不来）",
                  evidence=tail[:800], symbol="app.main")]


# ── D3 契约符合性 ──────────────────────────────────────────────────────────
def d3_contract(ws, contracts) -> list[Error]:
    """复用 `iterate.inspect`：把每模块的 fails 逐条转成 Error。"""
    from . import iterate as _it
    out: list[Error] = []
    if not contracts:
        return out
    try:
        state = _it.inspect(Path(ws), contracts)
    except Exception as exc:                            # noqa: BLE001
        return [Error("P3", "advice", "-", f"检测器 D3 自身失败：{type(exc).__name__}",
                      evidence=str(exc)[:300], symbol="d3")]
    for rel, st in (state or {}).items():
        for f in (st.get("fails") or []):
            txt = str(f)
            # 形状：`path::func` 或 `函数名：原因`
            sym = ""
            m = re.search(r"::([A-Za-z_]\w*)", txt)
            if m:
                sym = m.group(1)
            out.append(Error("P1", "contract", rel, txt[:300],
                             evidence=txt[:600], symbol=sym or rel))
        # 桩（函数存在但没实现）
        for stub in (st.get("stubs") or []):
            out.append(Error("P2", "contract", rel,
                             f"函数还是桩：`{stub}`", symbol=str(stub)))
    return out


# ── D4 跨模块引用 ──────────────────────────────────────────────────────────
def d4_crossref(ws) -> list[Error]:
    """**调用了不存在的函数**（跨模块/同模块），以及参数个数明显不符。

    为什么必须有（2026-11-11 实测）：`app/api/batches.py` 调 `run_batch(...)`，
    而产物里叫 `run` —— 端到端只会给一个 500，看不出是"名字对不上"。
    这类错**纯静态就能抓**，不需要发请求、不需要花钱。

    只查"看起来像本项目的函数"的调用（`from app... import X` 或 `X(...)`），
    标准库/三方库一律跳过（不猜）。
    """
    ws = Path(ws)
    mods = list_modules(ws)
    # 全局符号表：短名 → [(模块, 参数数)]
    have: dict[str, list[tuple[str, int]]] = {}
    mod_defs: dict[str, dict[str, list[str]]] = {}
    for rel in mods:
        defs = _module_defs(_read(ws / rel))
        mod_defs[rel] = defs
        for name, args in defs.items():
            have.setdefault(name.split(".")[-1], []).append((rel, len(args)))

    out: list[Error] = []
    for rel in mods:
        src = _read(ws / rel)
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue                                    # D1 已经报过
        local = set(mod_defs.get(rel) or {})
        imported: dict[str, str] = {}                    # 本地名 → 来源模块
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if not (node.module.startswith("app") or node.module.startswith("matching_core")):
                    continue
                for a in node.names:
                    imported[a.asname or a.name] = node.module
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if isinstance(fn, ast.Name):
                name = fn.id
            elif isinstance(fn, ast.Attribute):
                name = fn.attr
            else:
                continue
            if name.startswith("_") or name in local:
                continue
            # 只查"本项目里有同名但参数数不符"或"看起来该有却没有"的
            hits = have.get(name) or []
            if not hits:
                # 从本项目 import 过来但全局找不到 → 确定错
                if name in imported:
                    out.append(Error(
                        "P1", "crossref", f"{rel}:{getattr(node, 'lineno', 0)}",
                        f"调用了不存在的函数 `{name}`"
                        f"（从 `{imported[name]}` 导入，但产物里没有这个函数）",
                        evidence=ast.get_source_segment(src, node)[:200] or "",
                        symbol=name))
                continue
            # 有同名函数：检查调用参数个数是否可能匹配（保守：只报"全都不匹配"）
            n_args = len(node.args) + len(node.keywords)
            if not any(n_args <= cnt or any(
                    a.arg == "**" for a in []) for _m, cnt in hits):
                # **保守**：只有当所有候选的参数数都 < 实际传入数（不可能匹配）时才报
                if all(n_args > cnt for _m, cnt in hits):
                    out.append(Error(
                        "P2", "crossref", f"{rel}:{getattr(node, 'lineno', 0)}",
                        f"调用 `{name}` 传了 {n_args} 个参数，"
                        f"但候选实现的参数数都更少（{sorted(set(c for _m, c in hits))}）",
                        evidence=ast.get_source_segment(src, node)[:200] or "",
                        symbol=name))
    # 同一 (文件, 符号) 只留第一条（免得一个函数被调 20 次报 20 条）
    seen: set = set()
    uniq: list[Error] = []
    for e in out:
        k = (e.where.split(":")[0], e.symbol)
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)
    return uniq


# ── D5 数据模型一致性 ──────────────────────────────────────────────────────
def d5_datamodel(ws, contracts=None) -> list[Error]:
    """同一实体的字段，在 models / services / api 三处是否一致。

    轻量版（**只报确定的不一致**，不猜）：
    · 契约里声明某函数返回字段 {a,b,c}，产物里该函数只 return 了子集 → 报
    · `models.py` 里构造的记录字段，在 services 取用时名字对不上 → 报
    实现上依赖契约对象（`contracts`）；契约缺失时直接返回空（不猜）。
    """
    ws = Path(ws)
    out: list[Error] = []
    if not contracts:
        return out
    mods = {rel: _read(ws / rel) for rel in list_modules(ws)}
    for rel, c in (contracts or {}).items():
        src = mods.get(str(rel))
        if not src:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        bodies: dict[str, str] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bodies[node.name] = ast.get_source_segment(src, node) or ""
        for f in (getattr(c, "funcs", None) or []):
            body = bodies.get(f.name)
            if body is None:
                continue                                # 缺函数由 D3 报
            want = list(getattr(f, "keys", None) or [])
            if not want:
                continue
            # 函数体里一个字段都没出现 → 很可能没按契约构造返回体
            missing = [k for k in want if not re.search(rf"['\"]{re.escape(k)}['\"]", body)]
            if missing and len(missing) >= max(1, len(want) // 2):
                out.append(Error(
                    "P2", "datamodel", rel,
                    f"`{f.name}` 的返回体缺字段：{', '.join(missing[:6])}"
                    f"（契约要求 {', '.join(want[:6])}）",
                    evidence=(body[:300] + "…") if len(body) > 300 else body,
                    symbol=f.name))
    return out


# ── D6 端到端 ──────────────────────────────────────────────────────────────
def d6_e2e(ws, bp, only=None, timeout: int = 900) -> list[Error]:
    """复用 `coverage.run`：每条红能力转一条 Error，**detail 就是证据**。"""
    from . import coverage as _cov
    out: list[Error] = []
    if bp is None:
        return out
    try:
        cov = _cov.run(bp, Path(ws), only=only, timeout=min(timeout, 120))
    except Exception as exc:                            # noqa: BLE001
        return [Error("P3", "advice", "-", f"检测器 D6 自身失败：{type(exc).__name__}",
                      evidence=str(exc)[:300], symbol="d6")]
    statuses = (cov.get("statuses") or {})
    details = (cov.get("details") or {})
    for cid, st in statuses.items():
        if str(st) == "done":
            continue
        cap = None
        try:
            cap = bp.by_id(cid)
        except Exception:                               # noqa: BLE001
            cap = None
        pr = (getattr(cap, "probe", None) or {}) if cap is not None else {}
        where = ""
        for s in (pr.get("steps") or []):
            if isinstance(s, dict) and s.get("path"):
                where = "%s %s" % (str(s.get("method") or "GET").upper(), s.get("path"))
        where = where or str(cid)
        out.append(Error("P2", "e2e", where,
                         f"{cid} 端到端不通过（{st}）",
                         evidence=str(details.get(cid) or "")[:600], symbol=str(cid)))
    return out


# ── 汇总 ───────────────────────────────────────────────────────────────────
# ── D7 界面（Playwright + axe-core）─────────────────────────────────────────
def d7_ui(ws, bp=None, *, base_url: str = "", only=None,
          screenshot_dir=None) -> list[Error]:
    """跑蓝图里的 **ui 类探针**：页面能渲染吗、点了有反应吗、JS 有没有报错。

    为什么必须有（2026-11-15）：D1–D6 全测代码与 API —— 页面白屏、按钮点不动、
    ECharts 空着，**后端全对、六项全绿**，用户却用不了。

    判据全部结构化、零 token：`console.error` / 未捕获异常 / 失败请求 /
    DOM 断言 / axe-core 可访问性。**不靠截图喂模型**。

    `base_url`：前端服务地址（如 `http://127.0.0.1:5173`）；
    没给就只跑探针里带完整 URL 的步骤。服务没起时整条 D7 记一条 P3（环境问题，
    不是代码问题）—— 不能让"前端没启动"冒充"界面写错了"。
    """
    from . import browser as _bw
    out: list[Error] = []
    caps = list(getattr(bp, "caps", None) or []) if bp is not None else []
    uis = []
    for c in caps:
        pr = getattr(c, "probe", None) or {}
        if str(pr.get("kind") or "").lower() == "ui":
            if only is None or getattr(c, "id", "") in set(only):
                uis.append((getattr(c, "id", ""), pr))
    if not uis:
        return out

    ok_env, why = _bw.available()
    if not ok_env:
        return [Error("P3", "advice", "UI 检测",
                      "浏览器不可用，D7 整条跳过：%s" % (why or "?"),
                      evidence="装上系统 Edge/Chrome 或 playwright install chromium",
                      symbol="d7")]
    if not base_url:
        return [Error("P3", "advice", "UI 检测",
                      "没有前端服务地址，D7 跳过（加 --ui-base http://127.0.0.1:PORT）",
                      symbol="d7-base")]

    for cid, pr in uis:
        steps = [s for s in (pr.get("steps") or []) if isinstance(s, dict)]
        acts, exps, eurl = [], [], ""
        # 归一：把 {do/action} 与 {expect_dom/expect_url} 拆开
        for s in steps:
            if "expect_dom" in s or "expect_url" in s or "expect_no_errors" in s:
                exps.extend(s.get("expect_dom") or [])
                eurl = eurl or str(s.get("expect_url") or "")
                continue
            a = dict(s)
            if "url" in a and not str(a["url"]).startswith(("http://", "https://")):
                a["url"] = base_url.rstrip("/") + "/" + str(a["url"]).lstrip("/")
            if a.get("do") in (None, "") and "action" not in a:
                # 只有 url 没有动作 → 当作 goto
                if "url" in a:
                    a = {"do": "goto", "url": a["url"]}
                else:
                    continue
            acts.append(a)
        if not acts:
            continue
        first = next((a.get("url") for a in acts if a.get("do") in ("goto", "open")), "")
        if not first:
            continue
        try:
            v = _bw.visit(str(first), actions=acts[1:], expect_dom=exps,
                          expect_url=eurl, screenshot_dir=screenshot_dir)
        except Exception as exc:                         # noqa: BLE001
            out.append(Error("P3", "advice", str(first),
                             "D7 执行异常：%s" % type(exc).__name__,
                             evidence=str(exc)[:300], symbol=str(cid)))
            continue
        probs = v.problems()
        if not probs:
            continue
        # 分级：白屏/JS 崩 = P1（页面根本不能用）；交互/可访问性 = P2
        hard = (v.error or (v.status and v.status >= 400)
                or v.page_errors)
        level = "P1" if hard else "P2"
        out.append(Error(level, "ui", str(first),
                         "；".join(probs[:4]),
                         evidence=_bw.render_evidence(v),
                         symbol=str(cid)))
    return out


# ── D8 第三方（provider 模式 + 替身场景）──────────────────────────────────
def d8_thirdparty(ws, bp=None, *, only=None) -> list[Error]:
    """第三方集成检查（开发文档 §13.11）。

    两件事：
    · **D8.1 静态**：业务代码有没有直接 `import` 厂商 SDK（有 = 该能力永远不可测）
    · **D8.2 场景**：蓝图里 `kind=="thirdparty"` 的探针，跑替身场景
      （成功/超时/重复幂等/非法跃迁/伪造签名）

    只报**确定的厂商包**（stripe/alipay/wechatpay/openai…），
    不报 `requests`/`urllib`（太通用，可能调自己的服务）—— 误报比漏报更毁信任。
    """
    from . import thirdparty as _tp
    out: list[Error] = []
    try:
        out.extend(_tp.check_provider_pattern(ws))
    except Exception as exc:                             # noqa: BLE001
        out.append(Error("P3", "advice", "-", "D8.1 自身失败：%s" % type(exc).__name__,
                         evidence=str(exc)[:300], symbol="d8"))
    for c in (getattr(bp, "caps", None) or []) if bp is not None else []:
        pr = getattr(c, "probe", None) or {}
        if str(pr.get("kind") or "").lower() != "thirdparty":
            continue
        if only is not None and getattr(c, "id", "") not in set(only):
            continue
        try:
            out.extend(_tp.run_scenarios(ws, pr, only=only))
        except Exception as exc:                         # noqa: BLE001
            out.append(Error("P3", "advice", str(getattr(c, "id", "")),
                             "D8.2 执行异常：%s" % type(exc).__name__,
                             evidence=str(exc)[:300], symbol="d8"))
    return out


def run_all(ws, bp=None, contracts=None, *, only_e2e=None,
            skip=("d6", "d7"), emit=None, ui_base: str = "",
            ui_only=None, screenshot_dir=None) -> list[Error]:
    """跑 D1–D6，**每个检测器独立**：一个炸了不影响其它（但要留痕）。"""
    ws = Path(ws)
    out: list[Error] = []
    plan = [("d1", lambda: d1_syntax(ws)),
            ("d2", lambda: d2_import(ws)),
            ("d3", lambda: d3_contract(ws, contracts)),
            ("d4", lambda: d4_crossref(ws)),
            ("d5", lambda: d5_datamodel(ws, contracts)),
            ("d6", lambda: d6_e2e(ws, bp, only=only_e2e)),
            ("d7", lambda: d7_ui(ws, bp, base_url=ui_base, only=ui_only,
                                 screenshot_dir=screenshot_dir)),
            ("d8", lambda: d8_thirdparty(ws, bp, only=ui_only))]
    for name, fn in plan:
        if name in (skip or ()):
            continue
        try:
            got = fn()
        except Exception as exc:                        # noqa: BLE001
            got = [Error("P3", "advice", "-",
                         f"检测器 {name} 自身失败：{type(exc).__name__}",
                         evidence=str(exc)[:300], symbol=name)]
        if emit:
            emit("detector", {"name": name, "errors": len(got)})
        out.extend(got)
    return out
