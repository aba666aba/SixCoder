"""代码导航自检：符号定位 + 中文意图 + 按符号断点（2026-11-20）。

钉住的核心契约（都是实测踩过的）：
  1. Python 用 AST 精确定位（class/function 的定义行 + 方法归属），不是字符串猜；
  2. 其他语言用行首定义正则，不命中调用/注释里的同名词；
  3. 全仓搜索按路径提示过滤、按匹配度排序；
  4. 中文意图解析：debug/navigate/search/none 四类，抠不出就 none（不瞎跳）；
  5. 按符号调试：断点落在函数体首行（不是 def 行），非 Python 明确拒绝；
  6. 安全：路径越界必须被 safe_path 拦住。

跑法: python checks/check_codelocate.py
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import codelocate as cl                        # noqa: E402

FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:260]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def write(ws: Path, rel: str, lines: list[str]) -> Path:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="codeloc_") as td:
        ws = Path(td)
        write(ws, "svc.py", [
            "class Payment:",
            "    def refund(self, amt):",
            "        return amt * -1",
            "",
            "def charge(x):",
            "    return x + 1",
        ])
        write(ws, "app.js", [
            "function greet(n) {",
            "  return n",
            "}",
            "const add = (a, b) => a + b",
            "class Thing {}",
            "// function grep_in_comment() {}",
        ])

        print("[1] Python AST 精确定位")
        syms = cl.symbols_in_file(ws / "svc.py")
        by_name = {s["name"]: s for s in syms}
        ck("class Payment 找到", "Payment" in by_name and by_name["Payment"]["kind"] == "class")
        ck("refund 定位到第 2 行（定义行）", by_name.get("refund", {}).get("line") == 2, by_name.get("refund"))
        ck("charge 定位到第 5 行", by_name.get("charge", {}).get("line") == 5, by_name.get("charge"))
        ck("refund 的 body_line 是第 3 行（断点落点）", by_name.get("refund", {}).get("body_line") == 3)

        print("[2] 其他语言：只认定义行，不认注释")
        js = cl.symbols_in_file(ws / "app.js")
        jn = {s["name"] for s in js}
        ck("greet / add / Thing 都找到", {"greet", "add", "Thing"} <= jn, jn)
        ck("注释里的 grep_in_comment 不算符号", "grep_in_comment" not in jn, jn)
        ck("class Thing 归为 class", any(s["name"] == "Thing" and s["kind"] == "class" for s in js))

        print("[3] locate 单文件 + 越界安全")
        loc = cl.locate(ws, "svc.py", "refund")
        ck("locate refund 成功", bool(loc) and loc["line"] == 2 and loc["path"] == "svc.py", loc)
        ck("locate 不存在的符号返回 None", cl.locate(ws, "svc.py", "no_such_fn") is None)
        try:
            cl.locate(ws, "../../outside.py", "x")
            escaped = False
        except Exception:                               # noqa: BLE001
            escaped = True
        ck("路径越界被拦住", escaped)

        print("[4] 全仓搜索")
        r = cl.find_symbols(ws, "refund")
        ck("搜 refund 命中 svc.py", any(h["path"] == "svc.py" and h["name"] == "refund" for h in r["hits"]), r["hits"][:3])
        r2 = cl.find_symbols(ws, "app.js add")
        ck("带文件提示只在该文件搜", all(h["path"] == "app.js" for h in r2["hits"]) and
           any(h["name"] == "add" for h in r2["hits"]), r2["hits"][:4])
        ck("空查询返回空", cl.find_symbols(ws, "")["hits"] == [])

        print("[5] 中文意图解析")
        cases = [
            ("打开 svc.py 的 refund 函数", "navigate", "refund", "svc.py"),
            ("调试 app.js 的 greet 函数并运行", "debug", "greet", "app.js"),
            ("定位 charge() 并断点", "debug", "charge", ""),
            ("find_symbols 在哪", "search", "find_symbols", ""),
            ("搜索 refund 函数", "search", "refund", ""),
            ("把这段改成异步", "none", "", ""),
            ("帮我看看 svc.py 的 charge 函数", "navigate", "charge", "svc.py"),
        ]
        for text, act, sym, fh in cases:
            got = cl.parse_intent(text)
            ck(f"意图「{text}」→ {act}", got["action"] == act and got["symbol"] == sym and got["file_hint"] == fh, got)

        print("[6] 后端接口按符号调试（真起 bdb 会话）")
        spec = importlib.util.spec_from_file_location("srv", ROOT / "server.py")
        s = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(s)
        r = s.editor_debug_symbol(s.DebugSymbolReq(repo=str(ws), path="svc.py", symbol="refund"))
        ck("debug/symbol 启动成功", bool(r.get("ok")), r)
        ck("断点落在 body_line=3（不是 def 行 2）", r.get("breakpoint") == 3, r.get("breakpoint"))
        ck("target 信息带回给界面", (r.get("target") or {}).get("name") == "refund")
        if r.get("id"):
            s.editor_debug_stop(s.DebugCmdReq(id=r["id"], op="quit"))
        r2 = s.editor_debug_symbol(s.DebugSymbolReq(repo=str(ws), path="app.js", symbol="greet"))
        ck("非 Python 明确拒绝单步（不假装）", not r2.get("ok") and "只支持 Python" in (r2.get("reason") or ""), r2)

        print("[7] navigate 接口：命中就给目标，抠不出就交回填充")
        r = s.editor_navigate(s.NavigateReq(repo=str(ws), text="打开 svc.py 的 charge 函数"))
        ck("导航返回目标", r.get("action") == "navigate" and r["targets"][0]["name"] == "charge", r.get("targets"))
        r = s.editor_navigate(s.NavigateReq(repo=str(ws), text="charge 在哪个文件"))
        ck("搜索返回候选", r.get("action") == "search" and len(r["targets"]) >= 1, r.get("targets"))
        r = s.editor_navigate(s.NavigateReq(repo=str(ws), text="续写这句话"))
        ck("无导航意图 → none（交回 fill）", r.get("action") == "none" and not r.get("targets"), r)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
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
