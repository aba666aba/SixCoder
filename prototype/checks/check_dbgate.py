"""数据库判据自检：静态"游标行"闸门 + 真 sqlite 端到端判据 + 引擎测试生成。

这三条是为一个实测 bug 建的（store.fetch_all 用 dict(row) → 独立验收 TypeError，
而当时的"契约判据 8/8"完全是空的）。这里把"能抓 / 不误杀 / 双向都灵"钉成回归。

跑法: python check_dbgate.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import gates, props  # noqa: E402

FAILS: list[str] = []


# executemany 绑定形态：内层生成器（错） vs 内层 tuple（对）
EXEC_GEN = '''import sqlite3


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    cur = conn.cursor()
    cur.executemany("INSERT INTO people(id,name) VALUES(?,?)",
                    [(r.get(k) for k in ("id", "name")) for r in rows])
    return len(rows)
'''

EXEC_TUPLE = '''import sqlite3


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    cur = conn.cursor()
    cur.executemany("INSERT INTO people(id,name) VALUES(?,?)",
                    [tuple(r.get(k) for k in ("id", "name")) for r in rows])
    return len(rows)
'''


MULTI_REQ = """写一个多表持久化模块。

1) store2.py：SQLite 持久化（conn 为真实 sqlite3 连接）。
   - create_tables(conn: object) -> None：建表 students(sid TEXT PRIMARY KEY, name TEXT, score REAL, bonus REAL, total REAL)、teachers(tid TEXT PRIMARY KEY, name TEXT, quota INTEGER)
   - save_all(conn: object, students: list[dict], teachers: list[dict]) -> int：整体写入并返回写入的总行数
   - load_students(conn: object) -> list[dict]：按 sid 升序读回学生
   - load_teachers(conn: object) -> list[dict]：按 tid 升序读回教师
"""

MULTI_GOOD = '''"""多表 store。"""


def _cols(cur):
    return [d[0] for d in cur.description]


def create_tables(conn):
    for sql in (
        "CREATE TABLE IF NOT EXISTS students (sid TEXT PRIMARY KEY, name TEXT, score REAL, bonus REAL, total REAL)",
        "CREATE TABLE IF NOT EXISTS teachers (tid TEXT PRIMARY KEY, name TEXT, quota INTEGER)",
    ):
        conn.execute(sql)
    conn.commit()


def save_all(conn, students=None, teachers=None):
    create_tables(conn)
    n = 0
    if students:
        conn.executemany("INSERT INTO students (sid,name,score,bonus,total) VALUES (?,?,?,?,?)",
                         [(str(r["sid"]), r["name"], r["score"], r["bonus"], r["total"])
                          for r in students])
        n += len(students)
    if teachers:
        conn.executemany("INSERT INTO teachers (tid,name,quota) VALUES (?,?,?)",
                         [(str(r["tid"]), r["name"], r["quota"]) for r in teachers])
        n += len(teachers)
    conn.commit()
    return n


def load_students(conn):
    cur = conn.execute("SELECT sid,name,score,bonus,total FROM students ORDER BY sid")
    return [dict(zip(_cols(cur), r)) for r in cur.fetchall()]


def load_teachers(conn):
    cur = conn.execute("SELECT tid,name,quota FROM teachers ORDER BY tid")
    return [dict(zip(_cols(cur), r)) for r in cur.fetchall()]
'''

# 反例：第二个读回函数读错了表（真 sqlite 下必然读不到刚写的教师）
MULTI_BAD = MULTI_GOOD.replace(
    'cur = conn.execute("SELECT tid,name,quota FROM teachers ORDER BY tid")',
    'cur = conn.execute("SELECT tid,name,quota FROM students ORDER BY tid")')


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


REQ = """写三个模块。

1) store.py：create_table(conn) -> None：建表 people(id INTEGER PRIMARY KEY, name TEXT,
age INTEGER, email TEXT, birth TEXT)；upsert(conn, rows: list[dict]) -> int：按 id
覆盖写入，返回写入条数；fetch_all(conn) -> list[dict]：按 id 升序返回全部行。

2) report.py：summarize(rows: list[dict]) -> dict：返回 {"count": 人数,
"avg_age": 平均年龄, "domains": 邮箱域名计数字典}。
"""

BUGGY = '''"""store 模块。"""
def create_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER, email TEXT, birth TEXT)")
    conn.commit()

def upsert(conn, rows):
    create_table(conn)
    for row in rows:
        conn.execute("INSERT INTO people(id,name,age,email,birth) VALUES(?,?,?,?,?)", (row.get('id'), row.get('name'), row.get('age'), row.get('email'), row.get('birth')))
    conn.commit()
    return len(rows)

def fetch_all(conn):
    result = conn.execute("SELECT id,name,age,email,birth FROM people ORDER BY id ASC")
    return [dict(row) for row in result]
'''

FIXED = BUGGY.replace(
    "    result = conn.execute(\"SELECT id,name,age,email,birth FROM people ORDER BY id ASC\")\n"
    "    return [dict(row) for row in result]\n",
    "    cur = conn.execute(\"SELECT id,name,age,email,birth FROM people ORDER BY id ASC\")\n"
    "    cols = [d[0] for d in cur.description]\n"
    "    return [dict(zip(cols, r)) for r in cur.fetchall()]\n")

ROW_FACTORY = BUGGY.replace(
    "def fetch_all(conn):\n",
    "def fetch_all(conn):\n    import sqlite3\n    conn.row_factory = sqlite3.Row\n")

CLEAN = '''def clean_row(row: dict) -> dict:
    """清理一行。"""
    return {"name": str(row.get("name", "")).strip()}
'''

REPORT = '''def summarize(rows: list[dict]) -> dict:
    """汇总。"""
    ages = [r["age"] for r in rows if isinstance(r.get("age"), int)]
    return {"count": len(rows), "avg_age": sum(ages) / len(ages) if ages else 0,
            "domains": sorted({str(r.get("email", "")).split("@")[-1] for r in rows})}
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dbgate_"))
    (tmp / "store.py").write_text(BUGGY, encoding="utf-8")
    (tmp / "clean.py").write_text(CLEAN, encoding="utf-8")

    print("[1] 静态闸门：游标行 dict(row)")
    r = gates.gate_db_rows(tmp, ["store.py"])
    check("裸 dict(row) 必须报红", not r.ok, r.detail)
    check("报红要指到函数名", "fetch_all()" in (r.detail or ""), r.detail)
    check("无语法的文件不许误杀", gates.gate_db_rows(tmp, ["clean.py"]).ok)
    (tmp / "good.py").write_text(FIXED, encoding="utf-8")
    check("dict(zip(cols,row)) 不许误杀", gates.gate_db_rows(tmp, ["good.py"]).ok)
    (tmp / "rf.py").write_text(ROW_FACTORY, encoding="utf-8")
    check("设 row_factory 不许误杀", gates.gate_db_rows(tmp, ["rf.py"]).ok)

    # executemany 的每一行必须是序列：内层写成生成器 → sqlite3 ProgrammingError。
    # 实测来源：夜班 bench r1 全绿通过语法与契约判据，只在真连库时炸。
    (tmp / "bind.py").write_text(EXEC_GEN, encoding="utf-8")
    rb = gates.gate_db_rows(tmp, ["bind.py"])
    check("executemany 内层生成器必须报红", not rb.ok, rb.detail)
    check("报红要给出唯一修法", "tuple(" in (rb.detail or ""), rb.detail)
    (tmp / "bind_ok.py").write_text(EXEC_TUPLE, encoding="utf-8")
    check("内层 tuple 不许误杀", gates.gate_db_rows(tmp, ["bind_ok.py"]).ok)

    print("[2] 引擎判据：真 sqlite 端到端")
    from core import contract as cm
    mods = cm.parse_requirement(REQ)
    store = mods.get("store.py")
    check("需求能解析出 store 契约", bool(store))
    chks = props._db_end2end(list(store.funcs), REQ)
    check("能造出端到端判据", len(chks) == 1, str(chks)[:120])
    check("判据覆盖建表/写入/读取三个函数",
          all(n in chks[0].get("covers", []) for n in ("create_table", "upsert", "fetch_all")),
          str(chks[0].get("covers")))
    bad = props.run(tmp, "store.py", chks)
    check("坏实现必须判红", not bad.get("ok"), props.as_feedback(bad))
    (tmp / "store.py").write_text(FIXED, encoding="utf-8")
    good = props.run(tmp, "store.py", chks)
    check("修好后必须判绿", bool(good.get("ok")), props.as_feedback(good))
    (tmp / "store.py").write_text(ROW_FACTORY, encoding="utf-8")
    rf = props.run(tmp, "store.py", chks)
    check("row_factory 写法也必须判绿", bool(rf.get("ok")), props.as_feedback(rf))

    print("[3] 渲染 pytest：断言由引擎写")
    (tmp / "store.py").write_text(FIXED, encoding="utf-8")
    (tmp / "report.py").write_text(REPORT, encoding="utf-8")
    text = props.render_pytest("store", chks)
    check("渲染结果含 db 端到端用例", "db_end2end" in text, text[:80])
    check("渲染结果是合法 python", _parses(text))
    rchecks = props.from_contract(mods.get("report.py"))
    rtext = props.render_pytest("report", rchecks)
    check("keys 类判据能渲染成用例", "test_engine_0_keys" in rtext, rtext[:120])
    check("report 渲染结果是合法 python", _parses(rtext))
    (tmp / "test_store.py").write_text(text, encoding="utf-8")
    (tmp / "test_report.py").write_text(rtext, encoding="utf-8")
    tr = gates.gate_tests(tmp, ["test_store.py", "test_report.py"], timeout=120)
    check("渲染出来的用例在真产物上全绿", tr.ok, tr.detail)

    print("[4] 多表：每个读回函数各一条判据（42/44 那个坑的回归）")
    mmods = cm.parse_requirement(MULTI_REQ)
    store2 = mmods.get("store2.py")
    check("多表需求能解析出契约", bool(store2) and len(store2.funcs) == 4,
          str([f.name for f in getattr(store2, "funcs", [])]))
    (tmp / "store2.py").write_text(MULTI_GOOD, encoding="utf-8")
    chks2 = props._db_end2end(list(store2.funcs), MULTI_REQ)
    check("每个读回函数各一条判据", len(chks2) == 2, str(chks2)[:160])
    # 写死 name == "create_table" 时，契约里的 create_tables 永远拿不到证据
    check("复数建表函数 create_tables 也被认下",
          all(c.get("create") == "create_tables" for c in chks2),
          str([c.get("create") for c in chks2]))
    tmap = {c["reader"]: (c["table"], c["key"]) for c in chks2}
    check("读回函数对上自己的表和主键",
          tmap.get("load_students") == ("students", "sid")
          and tmap.get("load_teachers") == ("teachers", "tid"), str(tmap))
    by_reader = {c["reader"]: c for c in chks2}
    st_args = (by_reader.get("load_students") or {}).get("writer_args")
    te_args = (by_reader.get("load_teachers") or {}).get("writer_args")
    check("多参数写入函数的行放对了参数位（students 在第 1 个、teachers 在第 2 个）",
          bool(st_args) and st_args[0] and not st_args[1]
          and bool(te_args) and not te_args[0] and bool(te_args[1]),
          f"students={st_args} teachers={te_args}")
    check("每条判据都覆盖 建表+写入+自己的读回",
          all("create_tables" in c["covers"] and "save_all" in c["covers"]
              and c["reader"] in c["covers"] for c in chks2),
          str([c["covers"] for c in chks2]))
    g2 = props.run(tmp, "store2.py", chks2)
    check("正确实现必须判绿", bool(g2.get("ok")), props.as_feedback(g2))
    (tmp / "store2.py").write_text(MULTI_BAD, encoding="utf-8")
    b2 = props.run(tmp, "store2.py", chks2)
    check("读错表的实现必须判红", not b2.get("ok"), props.as_feedback(b2))
    (tmp / "store2.py").write_text(MULTI_GOOD, encoding="utf-8")
    text2 = props.render_pytest("store2", chks2)
    check("多表判据渲染成合法 python", _parses(text2), text2[:120])
    check("渲染时把建表函数也调上（否则真跑必炸）", "create_tables(conn)" in text2, text2[:200])
    (tmp / "test_store2.py").write_text(text2, encoding="utf-8")
    tr2 = gates.gate_tests(tmp, ["test_store2.py"], timeout=120)
    check("多表渲染出的用例在真产物上全绿", tr2.ok, tr2.detail)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（含反例：坏实现必红、三种正确写法必绿）")
    return 0


def _parses(text: str) -> bool:
    import ast
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
