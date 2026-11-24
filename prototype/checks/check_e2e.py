r"""跨模块业务流（e2e）自检：它必须能抓住"每个函数都对、连起来不对"。

反例清单（都来自真实故障类型）：
  1. 清洗器吞字段 → 入库静默丢列
  2. 写入条数口径不对
  3. 读回条数/内容不对
  4. 汇总 count 口径不对
  5. 分组计数丢行（domains 总数 ≠ 人数）
另外必须验证：认不出完整业务流时**如实跳过**，不许编一条假流程自证清白。

跑法: python check_e2e.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as cm  # noqa: E402
from core import e2e  # noqa: E402

FAILS: list[str] = []

REQ = """写三个模块，做一条"清洗→入库→汇总"的流水线。

1) store.py：create_table(conn) -> None：建表 people(id INTEGER PRIMARY KEY, name TEXT,
age INTEGER, email TEXT, birth TEXT)；upsert(conn, rows: list[dict]) -> int：按 id 覆盖写入，
返回写入条数；fetch_all(conn) -> list[dict]：按 id 升序返回全部行。

2) clean.py：clean_row(row: dict) -> dict：清理一行，返回清理后的字典。

3) report.py：summarize(rows: list[dict]) -> dict：返回 {"count": 人数, "avg_age": 平均年龄,
"domains": 邮箱域名计数字典}。
"""

STORE = '''def create_table(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER, email TEXT, birth TEXT)")
    conn.commit()

def upsert(conn, rows):
    for row in rows:
        conn.execute("INSERT INTO people(id,name,age,email,birth) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
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

CLEAN_DROPS = '''def clean_row(row):
    """反例：清洗器只返回"自己关心的"字段，把其它列吞掉。"""
    return {"age": int(str(row.get("age", "0")).strip())}
'''

REPORT_OK = '''def summarize(rows):
    ages = [r.get("age") for r in rows if isinstance(r.get("age"), int)]
    dom = {}
    for r in rows:
        e = str(r.get("email") or "")
        if "@" in e:
            d = e.split("@")[1]
            dom[d] = dom.get(d, 0) + 1
    return {"count": len(rows), "avg_age": round(sum(ages) / len(ages), 1) if ages else 0,
            "domains": dom}
'''

REPORT_BAD_COUNT = REPORT_OK.replace('"count": len(rows)', '"count": len(rows) + 1')


def _mut(src: str, old: str, new: str) -> str:
    """构造反例：替换必须真的发生，否则"反例没被构造出来"本身就是失败项。"""
    out = src.replace(old, new, 1)
    check(f"反例已构造：{(old.strip()[:30] or '')!r} → {(new.strip()[:24] or '')!r}", out != src)
    return out


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def workspace(store: str, clean: str, report: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="e2e_"))
    (d / "store.py").write_text(store, encoding="utf-8")
    (d / "clean.py").write_text(clean, encoding="utf-8")
    (d / "report.py").write_text(report, encoding="utf-8")
    return d


def main() -> int:
    contracts = cm.parse_requirement(REQ)
    print("[1] 认业务流")
    check("需求解析出 3 个模块", set(contracts) >= {"store.py", "clean.py", "report.py"},
          str(list(contracts)))
    p = e2e.build(contracts, REQ)
    check("认出 clean/setup/store/read/agg 五段", bool(p.get("ok")), str(p.get("reason")))
    check("证据覆盖 5 个函数", len(p.get("covers") or []) >= 5, str(p.get("covers")))
    check("样例行是原始文本（模拟 CSV）",
          isinstance(p["rows"][0].get("age"), str), repr(p["rows"][0]))
    check("认不出流程时如实跳过", (lambda q: q.get("ok") and q.get("skipped"))(
        e2e.write(Path(tempfile.mkdtemp()), {"x.py": contracts["clean.py"]}, REQ)))

    print("[2] 正例：一条通的业务流必须全绿")
    ws = workspace(STORE, CLEAN_OK, REPORT_OK)
    r = e2e.write(ws, contracts, REQ)
    check("完整实现 → 通过", bool(r.get("ok")) and bool(r.get("files")), str(r)[:200])
    check("生成的文件留在工作区", (ws / "test_e2e_flow.py").exists())

    print("[3] 反例：每个函数单看都对，连起来错")
    cases = [
        ("清洗器吞字段", STORE, CLEAN_DROPS, REPORT_OK),
        ("upsert 返回条数错", _mut(STORE, "    return len(rows)", "    return 0"),
         CLEAN_OK, REPORT_OK),
        ("count 口径错", STORE, CLEAN_OK,
         _mut(REPORT_OK, '"count": len(rows)', '"count": len(rows) + 1')),
        ("分组计数丢行", STORE, CLEAN_OK,
         _mut(REPORT_OK, '        if "@" in e:', '        if "@" in e and False:')),
        ("读回少读一列", _mut(STORE, "SELECT id,name,age,email,birth", "SELECT id,name"),
         CLEAN_OK, REPORT_OK),
    ]
    for name, st, cl, rp in cases:
        w = workspace(st, cl, rp)
        res = e2e.write(w, contracts, REQ)
        check(f"{name} → 必须判红", not res.get("ok"), str(res.get("reason") or "")[:120])
        check(f"{name} → 不留红测试文件", not (w / "test_e2e_flow.py").exists())

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（5 条跨模块不变式都能抓错，且在 4 个反例上确实转红）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
