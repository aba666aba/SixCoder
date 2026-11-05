r"""追溯矩阵自检：TRACE 必须能回答"这条需求凭什么是真的"。

检查点：
  1. 每个契约函数都有行，且需求出处真的来自需求原文（不是"需求未点名"）
  2. 状态/证据强度与机械体检一致（没判据就是没有判据，不许美化）
  3. 缺口能列出来：需求里没落到函数的条目、有实现但无判据的函数
  4. 歧义能列出来：清洗器处理的字段名与表列对不上（night2/night3 真实出现过）
  5. 汇总数字与逐行一致

跑法: python check_trace.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as cm  # noqa: E402
from core import iterate, trace  # noqa: E402

FAILS: list[str] = []

REQ = """写一条"清洗→入库→汇总"的流水线，模块放仓库根目录：

1) store.py：
   - create_table(conn) -> None：建表 people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER,
     email TEXT, birth TEXT)。
   - upsert(conn, rows: list[dict]) -> int：按 id 覆盖写入，返回写入条数。
   - fetch_all(conn) -> list[dict]：按 id 升序返回全部行。
2) clean.py：
   - clean_row(row: dict) -> dict：把 age 转成 int、email 去空格转小写。
3) report.py：
   - summarize(rows: list[dict]) -> dict：返回 {"count": 人数, "avg_age": 平均年龄,
     "domains": 邮箱域名计数字典}。

所有函数必须线程安全。
"""

STORE = '''def create_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER, email TEXT, birth TEXT)")
    conn.commit()

def upsert(conn, rows):
    for row in rows:
        conn.execute("INSERT INTO people(id,name,age,email,birth) VALUES(?,?,?,?,?)",
                     (row.get("id"), row.get("name"), row.get("age"), row.get("email"), row.get("birth")))
    conn.commit()
    return len(rows)

def fetch_all(conn):
    cur = conn.execute("SELECT id,name,age,email,birth FROM people ORDER BY id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
'''

CLEAN_OK = '''def clean_row(row):
    row = dict(row)
    row["age"] = int(str(row.get("age", "0")).strip())
    row["email"] = str(row.get("email", "")).strip().lower()
    return row
'''

CLEAN_AMBIGUOUS = '''def clean_row(row):
    row = dict(row)
    row["age"] = int(str(row.get("age", "0")).strip())
    if "date" in row:               # 需求里表列叫 birth，这里处理的是 date
        row["date"] = str(row["date"])
    return row
'''

REPORT = '''def summarize(rows):
    ages = [r.get("age") for r in rows if isinstance(r.get("age"), int)]
    dom = {}
    for r in rows:
        e = str(r.get("email") or "")
        if "@" in e:
            dom[e.split("@")[1]] = dom.get(e.split("@")[1], 0) + 1
    return {"count": len(rows), "avg_age": round(sum(ages) / len(ages), 1) if ages else 0,
            "domains": dom}
'''


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def workspace(clean: str = CLEAN_OK) -> Path:
    d = Path(tempfile.mkdtemp(prefix="trace_"))
    (d / "store.py").write_text(STORE, encoding="utf-8")
    (d / "clean.py").write_text(clean, encoding="utf-8")
    (d / "report.py").write_text(REPORT, encoding="utf-8")
    return d


def main() -> int:
    contracts = cm.parse_requirement(REQ)
    print("[1] 需求条目归属")
    att = trace.attribute(REQ, contracts)
    for fn in ("create_table", "upsert", "fetch_all", "clean_row", "summarize"):
        check(f"{fn} 有需求出处", fn in att["by_func"], str(sorted(att["by_func"])))
    check("模块标题行不算缺口",
          not any("store.py" in x and x.strip().endswith("：") for x in att["orphan_lines"]),
          str(att["orphan_lines"]))
    check("没落到函数的条目被列为缺口",
          any("线程安全" in x for x in att["orphan_lines"]), str(att["orphan_lines"]))

    print("[2] 矩阵与证据")
    ws = workspace()
    state = iterate.inspect(ws, contracts)
    text = trace.build(ws, REQ, contracts, state, iterate.last_e2e(ws))
    for fn in ("create_table", "upsert", "fetch_all", "clean_row", "summarize"):
        check(f"表里有 {fn} 这一行", f"::{fn}`" in text)
    check("状态列出现已验收", "已验收" in text)
    check("产物列给出真实行号（L 数字）", bool(re.search(r"L\d+\s*\|", text)), "")
    check("证据强度只用机械口径", "engine_assertion" in text and "model_text" not in text)
    ver = sum(s["verified"] for s in state.values())
    tot = sum(s["funcs"] for s in state.values())
    check(f"汇总数字与体检一致（{ver}/{tot}）",
          f"已验收 {ver} 个" in text, text[text.find("**汇总**"):][:120])

    print("[3] 歧义（字段对不上）")
    check("干净实现不报歧义", "未发现字段级歧义" in text, text[text.find("## 三"):][:160])
    ws2 = workspace(CLEAN_AMBIGUOUS)
    st2 = iterate.inspect(ws2, contracts)
    text2 = trace.build(ws2, REQ, contracts, st2, iterate.last_e2e(ws2))
    check("处理 date 而表列是 birth → 必须点名 date",
          "date" in text2 and "需甲方确认" in text2, text2[text2.find("## 三"):][:240])
    check("歧义不阻塞交付（仍算已验收）",
          any(s["verified"] for s in st2.values()), str({k: v["verified"] for k, v in st2.items()}))

    print("[4] 缺口：有实现但没判据")
    ws3 = workspace()
    (ws3 / "report.py").unlink()
    st3 = iterate.inspect(ws3, {k: v for k, v in contracts.items() if k != "report.py"})
    text3 = trace.build(ws3, REQ, contracts, st3, {})
    check("2.2 小节存在", "### 2.2" in text3)
    check("2.3 e2e 小节存在", "### 2.3" in text3)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（归属、证据、缺口、歧义四类都能如实出表）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
