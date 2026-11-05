"""签名自检：静态判"调用必炸"的签名 + 确定性补参数。

实测动机（夜班 n=3 两次都栽在这里）：
    `upsert() takes 1 positional argument` —— 契约写的是 `upsert(conn, rows: list[dict]) -> int`，
    产物写成 `upsert(conn)`。这不是推理问题，是机械问题：参数名就在契约里，
    引擎完全可以自己补上，不该让模型重写、更不该拖到端到端才以 TypeError 的形态出现。

跑法: python check_sigfix.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import autofix, iterate  # noqa: E402

REQ = (Path(__file__).resolve().parent.parent /
       "tools" / "fixtures" / "hq_requirement.txt").read_text(encoding="utf-8-sig")

STORE_MISSING = '''import sqlite3


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, "
                 "age INTEGER, email TEXT, birth TEXT)")


def upsert(conn: sqlite3.Connection) -> int:
    return 0


def fetch_all(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT id,name,age,email,birth FROM people ORDER BY id ASC")
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
'''

STORE_TOO_MANY = '''import sqlite3


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY)")


def upsert(conn: sqlite3.Connection, name: str, rows: list[dict]) -> int:
    return 0


def fetch_all(conn: sqlite3.Connection) -> list[dict]:
    return []
'''

STORE_OK = '''import sqlite3


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, "
                 "age INTEGER, email TEXT, birth TEXT)")


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    return len(rows)


def fetch_all(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    """多一个可选参数：向后兼容，不该判死。"""
    return []
'''


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def contract_for(module: str):
    from core import contract as cm
    cs = cm.parse_requirement(REQ)
    return cs[module]


def main() -> int:
    ok = True
    print("签名自检")
    c = contract_for("store.py")

    # 1) 静态判：缺参数 / 多必填参数 / 正确（含多一个可选参数）
    bad = iterate._signature_problem(c, STORE_MISSING, "upsert")
    ok &= check("缺参数被判红并点名", "缺 rows" in bad, bad or "（没判红）")
    bad2 = iterate._signature_problem(c, STORE_TOO_MANY, "upsert")
    ok &= check("多出必填参数被判红", "多出必填参数" in bad2, bad2 or "（没判红）")
    ok &= check("正确签名不判红", iterate._signature_problem(c, STORE_OK, "upsert") == "")
    ok &= check("多一个可选参数不判死", iterate._signature_problem(c, STORE_OK, "fetch_all") == "")
    ok &= check("契约里没有的函数不瞎判", iterate._signature_problem(c, STORE_OK, "nope") == "")

    # 2) 契约签名渲染：必须带参数名与注解（autofix 全靠它）
    sig = iterate._sig_text(c, "upsert")
    ok &= check("渲染出契约签名（含注解）",
                sig.startswith("upsert(conn") and "rows: list[dict]" in sig, sig)
    specs = autofix.spec_sigs(sig)
    # conn 在需求里就没写类型（`upsert(conn, rows: list[dict])`），这里不许替它编一个
    ok &= check("签名可解析成参数规格（不编造未声明的类型）",
                specs.get("upsert") == [("conn", ""), ("rows", "list[dict]")],
                str(specs))

    # 3) 确定性补参数：补上名字**和注解**，且不动别的函数
    tmp = Path(tempfile.mkdtemp())
    p = tmp / "store.py"
    p.write_text(STORE_MISSING, encoding="utf-8")
    r = autofix.fix_file(p, contract=sig)
    fixed = p.read_text(encoding="utf-8")
    def_line = next((ln for ln in fixed.splitlines() if "def upsert" in ln), "")
    ok &= check("补上了缺失的参数（原有注解保留、新参数带契约注解）",
                "rows: list[dict]" in def_line and "conn" in def_line, def_line)
    ok &= check("返回改动清单", any("upsert" in x for x in (r.get("params") or [])), str(r))
    ok &= check("修完签名不再判红", iterate._signature_problem(c, fixed, "upsert") == "")

    # 4) 幂等：再修一次不该再动
    r2 = autofix.fix_file(p, contract=sig)
    ok &= check("幂等（第二次不动）", not (r2.get("params") or []), str(r2))

    # 5) 不做坏事：本来正确的文件不被改
    p2 = tmp / "ok.py"
    p2.write_text(STORE_OK, encoding="utf-8")
    before = p2.read_text(encoding="utf-8")
    autofix.fix_file(p2, contract=iterate._sig_text(c, "upsert"))
    ok &= check("正确文件一个字节都不动", p2.read_text(encoding="utf-8") == before)

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
