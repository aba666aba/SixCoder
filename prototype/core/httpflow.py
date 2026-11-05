"""真实 HTTP 端到端（骨架）：真起服务、真发请求、真看状态码。

为什么要这一层：契约里的 `serve()` 只返回一个 URL 字符串（"不真跑"），
所以之前的验收最多证明"函数返回了看起来对的字符串"。真正的 Web 层要靠**真连接**验：
拿到产物自己的 `make_handler()`，在我们这边起一个 `ThreadingHTTPServer`，
然后用 urllib 发真实 GET/POST，检查状态码、内容类型、页面内容、JSON 可解析、坏请求不崩。

诚实边界：
    · 契约里没有 `make_handler` / `index_html` → `applicable=False`（不是通过，也不算失败）；
    · routes 的形状按常见约定构造 `{"/": lambda: index_html(title)}`；产物若要求别的形状，
      `make_handler` 会抛错 → 如实报"构造函数抛错"，由人判断是产物问题还是契约没写清；
    · POST 的业务语义各项目不同，所以只断言"返回 JSON、状态码不是 5xx、坏请求后服务还活着"，
      **不猜业务对错**（业务对错归 business_flow 那一层）。

用法：
    from . import httpflow
    res = httpflow.e2e(ws, contracts)
    res = httpflow.available(contracts)     # 能不能测
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RUNNER = r'''
import importlib, json, sys, threading, urllib.error, urllib.request
sys.path.insert(0, sys.argv[1])
spec = json.loads(sys.argv[2])
out = {"checks": [], "error": ""}
# 失败详情只写这么多字符：够人看出是什么响应，又不会把报告撑爆（判据看 bool，不看文字）
PREVIEW = 200


def preview(value):
    return str(value if value is not None else "")[:PREVIEW]


def add(name, ok, detail=""):
    out["checks"].append({"name": name, "ok": bool(ok), "detail": preview(detail)})


def done():
    print(json.dumps(out, ensure_ascii=False))


try:
    server_mod = importlib.import_module(spec["server"])
    webui_mod = importlib.import_module(spec["webui"])
except Exception as exc:
    out["error"] = "import 失败：%s: %s" % (type(exc).__name__, preview(exc))
    done()
    raise SystemExit(0)

from http.server import ThreadingHTTPServer

try:
    handler_cls = server_mod.make_handler({"/": lambda: webui_mod.index_html("e2e")})
except Exception as exc:
    out["error"] = ("make_handler 抛错：%s: %s（可能是 routes 形状不符契约，"
                    "也可能是产物本身有问题）" % (type(exc).__name__, preview(exc)))
    done()
    raise SystemExit(0)

try:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
except Exception as exc:
    out["error"] = "起服务失败：%s: %s" % (type(exc).__name__, preview(exc))
    done()
    raise SystemExit(0)

port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % port


def get(path):
    try:
        with urllib.request.urlopen(base + path, timeout=8) as r:
            return r.status, r.read(20000).decode("utf-8", "replace"), dict(r.headers or {})
    except urllib.error.HTTPError as e:
        return e.code, e.read(20000).decode("utf-8", "replace"), dict(e.headers or {})
    except Exception as exc:
        return 0, "%s: %s" % (type(exc).__name__, preview(exc)), {}


def post(path, payload, raw=None):
    data = raw if raw is not None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read(20000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(20000).decode("utf-8", "replace")
    except Exception as exc:
        return 0, "%s: %s" % (type(exc).__name__, preview(exc))


try:
    code, body, headers = get("/")
    add("GET / 返回 200", code == 200, "状态码 %s" % code)
    add("GET / 真的返回了页面", bool(body.strip()) and "<" in body,
        "页面长度 %d" % len(body or ""))
    ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower()
    add("GET / 的 Content-Type 是 html", "html" in ctype, "Content-Type=%r" % ctype)

    code404, body404, _ = get("/no-such-page-e2e")
    add("GET 未知路径返回 404", code404 == 404, "状态码 %s（内容 %r）" % (code404, preview(body404)))

    code_post, body_post = post("/register_student", {"sid": "e2e1", "name": "E2E", "score": 80, "bonus": 0})
    add("POST 接口不返回 5xx", 0 < code_post < 500, "状态码 %s 内容 %r" % (code_post, preview(body_post)))
    try:
        json.loads(body_post)
        parsed = True
    except Exception:
        parsed = False
    add("POST 返回合法 JSON", parsed, "内容 %r" % preview(body_post))

    code_bad, body_bad = post("/register_student", None, raw=b"{not json at all")
    add("坏 JSON 请求不返回 5xx", 0 < code_bad < 500,
        "状态码 %s 内容 %r" % (code_bad, preview(body_bad)))

    code_again, _, _ = get("/")
    add("坏请求之后服务仍然活着", code_again == 200, "再次 GET 状态码 %s" % code_again)
except Exception as exc:
    out["error"] = "请求阶段异常：%s: %s" % (type(exc).__name__, preview(exc))
finally:
    try:
        srv.shutdown()
    except Exception:
        pass
done()
'''


def _funcs(contracts: dict) -> dict:
    """函数名 → 模块 rel（只收本工作区的契约）。"""
    out: dict = {}
    for rel, c in (contracts or {}).items():
        for f in getattr(c, "funcs", []) or []:
            out.setdefault(getattr(f, "name", ""), rel)
    return out


def available(contracts: dict) -> dict:
    """能不能测真实 HTTP：{ok, server, webui, reason}。"""
    f = _funcs(contracts)
    need = ("make_handler", "index_html")
    missing = [n for n in need if n not in f]
    if missing:
        return {"ok": False, "server": f.get("make_handler", ""), "webui": f.get("index_html", ""),
                "reason": "契约里没有 Web 层所需的函数：" + "、".join(missing)}
    return {"ok": True, "server": Path(f["make_handler"]).stem,
            "webui": Path(f["index_html"]).stem, "reason": ""}


def e2e(ws: Path, contracts: dict, timeout: int = 90) -> dict:
    """真起服务 + 真发请求。返回 {ok, applicable, checks, passed, total, fails, reason}。"""
    av = available(contracts)
    if not av["ok"]:
        return {"ok": False, "applicable": False, "passed": 0, "total": 0,
                "checks": [], "fails": [], "reason": av["reason"]}
    ws = Path(ws).resolve()
    tmp = ws / ".sixcoder" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    runner = tmp / "_http_e2e.py"
    runner.write_text(RUNNER, encoding="utf-8")
    spec = json.dumps({"server": av["server"], "webui": av["webui"]}, ensure_ascii=False)
    try:
        p = subprocess.run([sys.executable, "-B", str(runner), str(ws), spec],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", cwd=str(tmp),
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "applicable": True, "passed": 0, "total": 0, "checks": [],
                "fails": [f"HTTP 端到端超时 {timeout}s（服务没起来或请求被挂住）"]}
    finally:
        try:
            runner.unlink()
        except OSError:
            pass
    lines = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "applicable": True, "passed": 0, "total": 0, "checks": [],
                "fails": ["HTTP 端到端没有输出：" + (p.stderr or "")[-200:]]}
    try:
        got = json.loads(lines[-1])
    except Exception:                                    # noqa: BLE001
        return {"ok": False, "applicable": True, "passed": 0, "total": 0, "checks": [],
                "fails": ["HTTP 端到端输出不可解析：" + lines[-1][:200]]}
    checks = got.get("checks") or []
    passed = sum(1 for c in checks if c.get("ok"))
    fails = [f"{c.get('name')}（{c.get('detail') or ''}）" for c in checks if not c.get("ok")]
    if got.get("error"):
        fails.insert(0, got["error"])
    return {"ok": bool(checks) and not fails, "applicable": True,
            "passed": passed, "total": len(checks), "checks": checks, "fails": fails[:8]}


def report_lines(res: dict) -> list[str]:
    if res.get("applicable") is False:
        return ["- 真实 HTTP 端到端：**测不了**（" + (res.get("reason") or "没有 Web 层") +
                "）—— 不算通过"]
    if res.get("ok"):
        return [f"- **真实 HTTP 端到端：通过**（真起服务真发请求 {res.get('passed')}/"
                f"{res.get('total')} 项：页面 200 / 未知路径 404 / POST 返 JSON / 坏请求不崩）"]
    return ["- **真实 HTTP 端到端：未通过** —— " + "；".join((res.get("fails") or [])[:3])]
