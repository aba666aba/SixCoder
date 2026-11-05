"""高端项目实测的"独立验收 + 质量度量"脚本（判据由我手写，不依赖模型自写测试）。

用法: python hq_verify.py <workspace_dir>
"""
from __future__ import annotations

import ast
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

RUNNER = r'''
import importlib, json, os, sqlite3, sys, tempfile, traceback
ws = sys.argv[1]
sys.path.insert(0, ws)
res = {"checks": [], "ok": True}

def add(name, ok, detail=""):
    res["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:200]})
    if not ok:
        res["ok"] = False

def mod(name):
    try:
        return importlib.import_module(name)
    except Exception as exc:
        add(f"import {name}", False, f"{type(exc).__name__}: {exc}")
        return None

# 边界口径（我手写，不 import 引擎的任何东西）：
# 崩溃型异常（TypeError/AttributeError/KeyError/IndexError/ZeroDivisionError）= 没想到 → 不合格；
# ValueError 等业务异常 = 考虑过并拒绝（契约本来就这么要求）→ 合格。
# 这个口径和引擎侧的边界判据必须一致，否则"严"的方向就会吵架。
CRASH = {"TypeError", "AttributeError", "KeyError", "IndexError", "ZeroDivisionError",
         "RecursionError", "UnboundLocalError", "StopIteration", "OverflowError"}

def no_crash(label, fn):
    """调用 fn()：崩在 CRASH 集合里才算不合格。"""
    try:
        return fn(), True
    except Exception as exc:
        bad = type(exc).__name__ in CRASH
        add(f"{label} 不崩", not bad, f"{type(exc).__name__}: {exc}")
        return None, False

clean = mod("clean")
if clean:
    cases = [("normalize_age(' 42 ') == 42", lambda: clean.normalize_age(" 42 ") == 42),
             ("normalize_age 越界抛错", lambda: (clean.normalize_age("151") if False else None) is None or True)]
    try:
        add("normalize_age(' 42 ') == 42", clean.normalize_age(" 42 ") == 42)
    except Exception as exc:
        add("normalize_age(' 42 ') == 42", False, f"{type(exc).__name__}: {exc}")
    for bad in ("151", "-1", "abc"):
        try:
            clean.normalize_age(bad); add(f"normalize_age({bad!r}) 应抛 ValueError", False, "没有抛")
        except ValueError:
            add(f"normalize_age({bad!r}) 抛 ValueError", True)
        except Exception as exc:
            add(f"normalize_age({bad!r}) 抛 ValueError", False, f"抛了 {type(exc).__name__}")
    try:
        add("parse_date('2024/01/02') == '2024-01-02'", clean.parse_date("2024/01/02") == "2024-01-02")
    except Exception as exc:
        add("parse_date('2024/01/02')", False, f"{type(exc).__name__}: {exc}")
    try:
        add("parse_date('2024-01-02') 保持", clean.parse_date("2024-01-02") == "2024-01-02")
    except Exception as exc:
        add("parse_date('2024-01-02')", False, f"{type(exc).__name__}: {exc}")
    try:
        clean.parse_date("2024-13-40"); add("parse_date 非法日期应抛错", False, "没有抛")
    except ValueError:
        add("parse_date 非法日期抛 ValueError", True)
    except Exception as exc:
        add("parse_date 非法日期抛 ValueError", False, f"抛了 {type(exc).__name__}")
    try:
        add("clean_email(' Foo@Bar.COM ') == 'foo@bar.com'", clean.clean_email(" Foo@Bar.COM ") == "foo@bar.com")
    except Exception as exc:
        add("clean_email", False, f"{type(exc).__name__}: {exc}")
    for bad in ("nodomain@", "@nodomain", "plain"):
        try:
            clean.clean_email(bad); add(f"clean_email({bad!r}) 应抛错", False, "没有抛")
        except ValueError:
            add(f"clean_email({bad!r}) 抛 ValueError", True)
        except Exception as exc:
            add(f"clean_email({bad!r}) 抛 ValueError", False, f"抛了 {type(exc).__name__}")
    if hasattr(clean, "clean_row"):
        try:
            out = clean.clean_row({"age": " 30 ", "date": "2024/01/02", "email": " A@B.COM "})
            add("clean_row 三字段都归一化",
                out.get("age") == 30 and out.get("date") == "2024-01-02" and out.get("email") == "a@b.com", out)
        except Exception as exc:
            add("clean_row", False, f"{type(exc).__name__}: {exc}")
        # 边界：缺字段 / 空行不该崩（崩了说明用了硬下标）。
        # 注意 age=None 抛 ValueError 是**对的**（契约要求无法转整数就抛），
        # 只有崩在 CRASH 集合里才算不合格 —— 见 no_crash 的说明。
        for label, arg in (("clean_row({})", {}),
                           ("clean_row(只有 age)", {"age": " 30 "}),
                           ("clean_row(age 为 None)", {"age": None})):
            got, called = no_crash(label, lambda a=dict(arg): clean.clean_row(a))
            if called:
                add(f"{label} 返回 dict", isinstance(got, dict) or got is None, got)
    else:
        add("存在 clean_row", False, "函数缺失")

store = mod("store")
if store:
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "t.db")
    try:
        conn = sqlite3.connect(db)
        store.create_table(conn)
        rows = [{"id": 1, "name": "a", "age": 10, "email": "a@x.com", "birth": "2000-01-01"},
                {"id": 2, "name": "b", "age": 20, "email": "b@y.com", "birth": "1990-02-02"}]
        n = store.upsert(conn, rows)
        add("upsert 返回写入条数", n == 2, n)
        got = store.fetch_all(conn)
        add("fetch_all 返回 2 行", len(got) == 2, len(got))
        add("fetch_all 按 id 升序", [r.get("id") for r in got] == [1, 2], [r.get("id") for r in got])
        store.upsert(conn, [{"id": 1, "name": "a2", "age": 11, "email": "a@x.com", "birth": "2000-01-01"}])
        got2 = store.fetch_all(conn)
        add("upsert 覆盖同 id 不新增", len(got2) == 2 and got2[0].get("name") == "a2", got2[:1])
        conn.close()
    except Exception as exc:
        add("store 端到端", False, f"{type(exc).__name__}: {exc}")

rep = mod("report")
if rep:
    try:
        out = rep.summarize([{"age": 10, "email": "a@x.com"}, {"age": 20, "email": "b@x.com"},
                             {"age": 30, "email": "c@y.com"}])
        add("summarize count", out.get("count") == 3, out)
        add("summarize avg_age", abs(float(out.get("avg_age", -1)) - 20.0) < 0.05, out)
        add("summarize domains", out.get("domains") == {"x.com": 2, "y.com": 1}, out)
    except Exception as exc:
        add("summarize", False, f"{type(exc).__name__}: {exc}")
    # 边界口径（我手写，不 import 引擎的任何东西）：
    # 这三条对应"示例永远看不见"的现实输入——空表、age 为 NULL、行里根本没有该字段。
    # store.upsert 用 row.get('age') 写库，NULL 是它自己造得出来的。
    for label, arg, want_count in (("summarize([])", [], 0),
                                   ("summarize([{age:None}])", [{"age": None, "email": "a@x.com"}], 1),
                                   ("summarize([{}])", [{}], 1)):
        got, called = no_crash(label, lambda a=list(arg): rep.summarize(a))
        if called:
            add(f"{label} count 正确", isinstance(got, dict) and got.get("count") == want_count, got)

print(json.dumps(res, ensure_ascii=False))
'''


def quality(ws: Path) -> dict:
    """静态质量度量：函数长度、注解率、docstring 率、测试数量、TODO/桩。"""
    skip = {"_verify_run.py", "_props_run.py", "_diff_run.py", "conftest.py"}
    py = [p for p in ws.glob("*.py") if not p.name.startswith("test_") and p.name not in skip]
    funcs = docs = annotated = stubs = loc = 0
    long_funcs = []
    for p in py:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        loc += len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs += 1
                if ast.get_docstring(n):
                    docs += 1
                args = [a for a in list(getattr(n.args, "posonlyargs", [])) + list(n.args.args)
                        if a.arg not in ("self", "cls")]
                if args and all(a.annotation is not None for a in args) and n.returns is not None:
                    annotated += 1
                body = [b for b in n.body if not (isinstance(b, ast.Expr)
                                                  and isinstance(b.value, ast.Constant))]
                if not body or (len(body) == 1 and isinstance(body[0], ast.Pass)):
                    stubs += 1
                ln = (getattr(n, "end_lineno", n.lineno) or n.lineno) - n.lineno + 1
                if ln > 30:
                    long_funcs.append(f"{p.name}:{n.name}({ln})")
    tests = []
    for p in ws.glob("test_*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        tests += [n.name for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test_")]
    return {"modules": [p.name for p in py], "loc": loc, "funcs": funcs,
            "docstring_rate": round(docs / funcs, 2) if funcs else 0,
            "annotation_rate": round(annotated / funcs, 2) if funcs else 0,
            "stub_funcs": stubs, "long_funcs": long_funcs[:5],
            "test_count": len(tests)}


def main() -> int:
    ws = Path(sys.argv[1])
    print(f"验收目标: {ws}")
    proc = subprocess.run([sys.executable, "-c", RUNNER, str(ws)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=180)
    line = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    res = json.loads(line[-1]) if line else {"ok": False, "checks": []}
    passed = sum(1 for c in res["checks"] if c["ok"])
    total = len(res["checks"])
    print(f"\n独立验收：{passed}/{total} 通过  →  {'PASS' if res['ok'] else 'FAIL'}")
    for c in res["checks"]:
        if not c["ok"]:
            print(f"  FAIL {c['name']}   {c['detail']}")
    q = quality(ws)
    print("\n质量度量：")
    for k, v in q.items():
        print(f"  {k}: {v}")
    print("\n汇总:", json.dumps({"accept": {"passed": passed, "total": total, "ok": res["ok"]},
                                "quality": q}, ensure_ascii=False))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
