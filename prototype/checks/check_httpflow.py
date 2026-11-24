"""真实 HTTP 端到端自检：真起服务、真发请求，坏在哪儿必须报在哪儿。

为什么要有这条：契约里的 `serve()` 只返回 URL 字符串（"不真跑"），
所以以前的验收最多证明"函数返回了看起来对的字符串"。这层直接把产物自己的
`make_handler()` 挂到一个真 `ThreadingHTTPServer` 上，用 urllib 发真实请求。

判定口径（正反两面）：
    · 正确实现必须绿（页面 200 / 未知路径 404 / POST 返 JSON / 坏请求不崩 / 崩后还活着）；
    · 4 个反例必须红，而且要红在**对应的那一条**上，不是整体红了就算；
    · 契约里没有 Web 层 → applicable=False，报告写"测不了"，**不算通过**。

跑法: python checks/check_httflow.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import contract as cm, httpflow  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

REQ = """代码规模：S

1) server.py：
   - make_handler(routes: dict) -> type：返回 BaseHTTPRequestHandler 子类，GET 走页面、POST 走接口
   - serve(host: str, port: int, routes: dict) -> str：返回启动信息字符串
2) webui.py：
   - index_html(title: str) -> str：返回以 <!DOCTYPE html> 开头的完整页面
3) api.py：
   - handle(name: str, payload: dict, db: dict) -> dict：返回 {"code": 状态码, "body": 字典}
"""

API = '''"""HTTP 层业务函数（纯函数）。"""


def handle(name, payload, db):
    if name == "register_student":
        if not isinstance(payload, dict) or not payload.get("sid"):
            return {"code": 400, "body": {"ok": False, "error": "bad_payload"}}
        db.setdefault("students", []).append(payload)
        return {"code": 200, "body": {"ok": True, "sid": payload["sid"]}}
    return {"code": 404, "body": {"ok": False, "error": "no_route"}}
'''

WEBUI = '''"""页面字符串。"""


def index_html(title):
    return ("<!DOCTYPE html><html><head><title>%s</title></head>"
            "<body><h1>%s</h1></body></html>" % (title, title))
'''

SERVER = '''"""HTTP 服务层。"""

import json
from http.server import BaseHTTPRequestHandler

import api


def make_handler(routes):
    table = dict(routes or {})

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return None

        def _send(self, status, text, ctype):
            data = str(text).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            entry = table.get(self.path)
            if entry is None:
                self._send(404, "not found", "text/plain")
                return
            page = entry() if callable(entry) else entry
            self._send(200, page, "text/html")

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except Exception:
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                payload = {}
            name = self.path.lstrip("/")
            result = api.handle(name, payload, {})
            self._send(int(result.get("code") or 200),
                       json.dumps(result.get("body") or {}, ensure_ascii=False),
                       "application/json")

    return _Handler


def serve(host, port, routes):
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("port out of range")
    return "http://%s:%d" % (host, port)
'''

GOOD = {"server.py": SERVER, "webui.py": WEBUI, "api.py": API}

EMPTY_PAGE = dict(GOOD, **{"webui.py": '''"""页面字符串。"""


def index_html(title):
    return ""
'''})

NO_404 = dict(GOOD, **{"server.py": SERVER.replace(
    '            if entry is None:\n'
    '                self._send(404, "not found", "text/plain")\n'
    '                return',
    '            if entry is None:\n'
    '                self._send(200, "not found", "text/plain")  # 反例：未知路径也回 200\n'
    '                return')})

POST_CRASH = dict(GOOD, **{"api.py": API.replace(
    'def handle(name, payload, db):',
    'def handle(name, payload, db):\n    raise RuntimeError("反例：接口炸了")')})


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws(files: dict) -> Path:
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix="httpe2e_"))
    for name, src in files.items():
        (ws / name).write_text(src, encoding="utf-8")
    return ws


def _fails(res: dict) -> str:
    return " / ".join(str(f) for f in (res.get("fails") or []))


def main() -> int:
    print("真实 HTTP 端到端自检")
    cs = cm.parse_requirement(REQ)
    check("需求能解析出 3 个模块", len(cs) == 3, str(sorted(cs)))
    av = httpflow.available(cs)
    check("认得出这份契约能测 HTTP", av.get("ok") is True, str(av))

    print("[1] 正确实现：真起服务、真发请求必须绿")
    good = httpflow.e2e(_ws(GOOD), cs)
    check("判绿", bool(good.get("ok")), _fails(good))
    check("适用的链路不会被当成'测不了'", good.get("applicable") is True, str(good)[:160])
    check("请求项数够（≥6，防零请求假绿）", int(good.get("total") or 0) >= 6,
          f"total={good.get('total')}")
    ok_names = " ".join(c.get("name") or "" for c in (good.get("checks") or []) if c.get("ok"))
    for want in ("GET / 返回 200", "未知路径返回 404", "POST 返回合法 JSON",
                 "坏 JSON 请求不返回 5xx", "坏请求之后服务仍然活着"):
        check(f"覆盖并通过对的项：{want}", want in ok_names, ok_names[:200])
    check("报告行写明'真起服务真发请求'", "真起服务真发请求" in httpflow.report_lines(good)[0],
          httpflow.report_lines(good)[0])

    print("[2] 三个反例必须红，且红在对应项上")
    cases = [("空页面", EMPTY_PAGE, "真的返回了页面"),
             ("未知路径回 200", NO_404, "未知路径返回 404"),
             ("接口崩溃", POST_CRASH, "POST 接口不返回 5xx")]
    for label, files, want in cases:
        r = httpflow.e2e(_ws(files), cs)
        check(f"{label}：整体判红", not r.get("ok"), "竟然通过了")
        check(f"{label}：被『{want}』抓到", want in _fails(r), _fails(r)[:180])

    print("[3] 契约里没有 Web 层 → 如实'测不了'，不算通过")
    tiny = cm.parse_requirement("代码规模：S\n\n1) calc.py：\n"
                               "   - add(a: int, b: int) -> int：返回两数之和\n")
    r = httpflow.e2e(_ws({"calc.py": "def add(a, b):\n    return a + b\n"}), tiny)
    check("applicable=False", r.get("applicable") is False, str(r)[:160])
    check("不因为是'跳过'就算绿", r.get("ok") is False, str(r)[:160])
    check("报告写清'测不了'且不算通过",
          "测不了" in httpflow.report_lines(r)[0] and "不算通过" in httpflow.report_lines(r)[0],
          httpflow.report_lines(r)[0])

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（真起服务真发请求 / 三个反例各自红 / 没有 Web 层如实说）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
