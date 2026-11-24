"""属性驱动与硬指标自检：边界矩阵、变异零杀、可维护性、以及"边界判红不许被 e2e 洗绿"。

为什么单独一个脚本：这几条机制的价值全在"抓真错、不误杀"两个方向，任何一边偏了都白做。
这里用**真实产物**（night2 的 clean/store/report）做反向验证，跑法:

    python check_bounds.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as contract_mod  # noqa: E402
from core import gates, iterate, props  # noqa: E402

REQ = (Path(__file__).resolve().parent.parent /
       "tools" / "fixtures" / "hq_requirement.txt").read_text(encoding="utf-8-sig")

# 真实产物（projects/night2）：契约判据全绿、独立验收 19/19，但
#   · summarize([{"age": None}]) → TypeError（它自己的 upsert 就会写进 None 的 age）
# 这正是"示例驱动"看不见、边界矩阵一抓就中的错。
CLEAN = '''def normalize_age(x: str) -> int:
    if not isinstance(x, str):
        raise ValueError("bad")
    v = x.strip()
    try:
        v = int(v)
    except ValueError:
        raise ValueError("bad")
    if not 0 <= v <= 150:
        raise ValueError("range")
    return v


def parse_date(s: str) -> str:
    from datetime import datetime
    for fmt in ["%Y-%m-%d", "%Y/%m/%d"]:
        try:
            return datetime.strptime(s.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    raise ValueError("bad date")


def clean_email(s: str) -> str:
    s = s.strip().lower()
    if s.count("@") != 1 or not s.split("@")[0] or not s.split("@")[1]:
        raise ValueError("bad email")
    return s


def clean_row(row: dict) -> dict:
    out = dict(row)
    if "age" in row:
        out["age"] = normalize_age(row["age"])
    if "date" in row:
        out["date"] = parse_date(row["date"])
    if "email" in row:
        out["email"] = clean_email(row["email"])
    if "birth" in row:
        out["birth"] = parse_date(row["birth"])
    return out
'''

STORE = '''import sqlite3


def create_table(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, "
                 "age INTEGER, email TEXT, birth TEXT)")
    conn.commit()


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> int:
    create_table(conn)
    for r in rows:
        conn.execute("INSERT INTO people(id,name,age,email,birth) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET name=excluded.name, age=excluded.age, "
                     "email=excluded.email, birth=excluded.birth",
                     (r.get("id"), r.get("name"), r.get("age"), r.get("email"), r.get("birth")))
    conn.commit()
    return len(rows)


def fetch_all(conn: sqlite3.Connection) -> list[dict]:
    cur = conn.execute("SELECT id,name,age,email,birth FROM people ORDER BY id ASC")
    return [dict(zip([d[0] for d in cur.description], row)) for row in cur.fetchall()]
'''

# 崩溃型：age 为 None 时 sum() 直接 TypeError（真实产物里的写法）
REPORT_CRASH = '''def summarize(rows: list[dict]) -> dict:
    count = len(rows)
    avg_age = round(sum(row.get("age", 0) for row in rows) / count, 1) if count > 0 else 0.0
    domains = {}
    for row in rows:
        d = str(row.get("email") or "").split("@")[-1].lower()
        if d:
            domains[d] = domains.get(d, 0) + 1
    return {"count": count, "avg_age": avg_age, "domains": domains}
'''

# 稳健型：同一个需求，但对缺字段/空值给出确定的业务语义（这才是交付该有的样子）
REPORT_ROBUST = REPORT_CRASH.replace(
    'avg_age = round(sum(row.get("age", 0) for row in rows) / count, 1) if count > 0 else 0.0',
    'ages = [row["age"] for row in rows if isinstance(row.get("age"), int)]\n'
    '    avg_age = round(sum(ages) / len(ages), 1) if ages else 0.0')

COMPLEX = '''def messy(items: list[dict]) -> int:
    total = 0
    for it in items:
        if it.get("a"):
            if it.get("b"):
                if it.get("c"):
                    for x in (it.get("d") or []):
                        if x:
                            if x > 1:
                                for y in range(x):
                                    if y % 2:
                                        if y % 3:
                                            if y % 5:
                                                total += 1
    return total
'''

WEAK_TEST = '''from calc import divide


def test_divide():
    assert divide(4, 2) == 2
'''

RICH_CALC = '''def divide(a: int, b: int) -> float:
    if b == 0:
        raise ValueError("div by zero")
    return a / b
'''

RETRY = '''def retry_times() -> int:
    return 3
'''


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("属性驱动与硬指标自检")
    tmp = Path(tempfile.mkdtemp())
    (tmp / "clean.py").write_text(CLEAN, encoding="utf-8")
    (tmp / "store.py").write_text(STORE, encoding="utf-8")
    (tmp / "report.py").write_text(REPORT_CRASH, encoding="utf-8")

    # 1) 边界矩阵：标注齐全的函数都要拿到判据，且不能只喂给排在前面的函数
    checks = props.boundary_checks(CLEAN + "\n\n" + REPORT_CRASH)
    names = {c["desc"].split(" ")[0] for c in checks}
    want = {"normalize_age", "parse_date", "clean_email", "summarize"}
    ok &= check("四个函数都拿到边界判据（不再被总预算截断）", want <= names, str(sorted(names)))
    ok &= check("边界判据默认是硬判据", all(c.get("hard") for c in checks), str(checks[:1]))
    # 降级开关：只有"空字典行"这一条变提示，别的仍然硬判（默认严格，甲方可关）
    import os as _os
    _os.environ["SIXCODER_BOUNDARY_NO_EMPTY_DICT"] = "1"
    soft = props.boundary_checks(REPORT_CRASH)
    _os.environ.pop("SIXCODER_BOUNDARY_NO_EMPTY_DICT", None)
    soft_empty = [c for c in soft if "[{}]" in str(c.get("expr", ""))]
    others = [c for c in soft if "[{}]" not in str(c.get("expr", ""))]
    ok &= check("空字典行可降级为提示", bool(soft_empty) and all(not c["hard"] for c in soft_empty),
                str([c["desc"] for c in soft_empty]))
    ok &= check("降级只影响空字典行这一条", all(c["hard"] for c in others), "")

    # 2) 崩溃型：空输入不崩但 None 崩 → 必须判红，且点名到具体输入
    r = props.run(tmp, "report.py", [c for c in checks if c["desc"].startswith("summarize")])
    ok &= check("None 年龄被边界矩阵判红", not r["ok"], props.as_feedback(r))
    ok &= check("反例给出具体输入", "None" in props.as_feedback(r), props.as_feedback(r))

    # 3) 稳健型：同一个函数补上确定语义 → 必须放行（不许把正确写法误杀）
    (tmp / "report.py").write_text(REPORT_ROBUST, encoding="utf-8")
    r = props.run(tmp, "report.py", [c for c in checks if c["desc"].startswith("summarize")])
    ok &= check("补上确定的空值语义后转绿", r["ok"], props.as_feedback(r))

    # 4) 业务异常算合格、崩溃算不合格（边界判据的判定口径）
    biz = ("def f(x: str) -> int:\n"
           "    if not x:\n        raise ValueError('empty')\n"
           "    return int(x)\n")
    crash = ("def f(x: str) -> int:\n"
             "    return {}['k'] + int(len(x))\n")
    for label, src, want_ok in (("业务异常", biz, True), ("崩溃异常", crash, False)):
        d = Path(tempfile.mkdtemp())
        (d / "m.py").write_text(src, encoding="utf-8")
        cs = props.boundary_checks(src)
        rr = props.run(d, "m.py", cs)
        ok &= check(f"{label} → {'放行' if want_ok else '判红'}", bool(rr["ok"]) == want_ok,
                    props.as_feedback(rr))

    # 5) 假绿回归：e2e 跑通不许把边界判红的函数洗成绿
    #    （这就是 night2/night3 曾经报 8/8 的原因）
    (tmp / "report.py").write_text(REPORT_CRASH, encoding="utf-8")
    contracts = contract_mod.parse_requirement(REQ)
    state = iterate.inspect(tmp, contracts)
    st = state["report.py"]
    e2e = iterate.last_e2e(tmp)
    ok &= check("业务流本身是通的（否则下面的判绿不成立）",
                bool(e2e.get("ok")) and not e2e.get("skipped"), str(e2e)[:200])
    ok &= check("边界判红的 summarize 没被 e2e 重新算绿",
                "summarize" in (st.get("hard_failed") or []), str(st.get("verified_names")))
    ok &= check("它也不在已验收名单里",
                "summarize" not in (st.get("verified_names") or []), str(st.get("verified_names")))
    ok &= check("同一工地里 clean/store 仍然是绿的（没有一并误杀）",
                len(state["clean.py"]["verified_names"]) == 4 and
                len(state["store.py"]["verified_names"]) == 3,
                f"{state['clean.py']['verified_names']} / {state['store.py']['verified_names']}")

    # 5b) 假绿洞二（2026-11-10 修）：**判据引擎自己没跑起来**的函数，
    #     不许被 e2e 业务流洗成"已验收"。
    #     以前这类函数和"契约太薄造不出判据"的函数一起塞进 untested，
    #     e2e 一跑通就被算成已验收 → 模块判绿、标 done、再也不碰，
    #     而它从头到尾**没有任何机械判据验证过**（机制问题被记成产物合格）。
    #     这跟 [5] 是两个洞：[5] 是"判红被洗绿"，这里是"根本没判过也被洗绿"。
    d2 = Path(tempfile.mkdtemp())
    (d2 / "clean.py").write_text(CLEAN, encoding="utf-8")
    (d2 / "store.py").write_text(STORE, encoding="utf-8")
    (d2 / "report.py").write_text(REPORT_CRASH, encoding="utf-8")
    _real_run = props.run

    def _run_break_summarize(ws, rel, checks, *a, **k):
        """只让 summarize 的判据起不来（模拟探针路径 bug），其余照常跑。"""
        if any("summarize" in str((c or {}).get("expr") or "") for c in (checks or [])):
            return {"ok": False, "engine_error": True, "passed": 0, "total": 1,
                    "fails": [{"kind": "no-output", "detail": "模拟探针起不来"}]}
        return _real_run(ws, rel, checks, *a, **k)

    try:
        props.run = _run_break_summarize
        st_e = iterate.inspect(d2, contracts)
    finally:
        props.run = _real_run
    rep = st_e["report.py"]
    _cov = (iterate.last_e2e(d2) or {}).get("covers") or []
    ok &= check("前提：e2e 业务流确实覆盖 summarize（否则下面几条是空断言）",
                "summarize" in _cov, str(_cov))
    ok &= check("判据引擎故障 → 记成 engine_errors（不是产物错）",
                bool(rep.get("engine_errors")), str(rep.get("engine_errors")))
    ok &= check("且点名到具体函数 engine_failed（可精准归因，不搞模块级一刀切）",
                "summarize" in (rep.get("engine_failed") or []), str(rep.get("engine_failed")))
    ok &= check("它没被 e2e 洗成已验收（假绿洞已堵）",
                "summarize" not in (rep.get("verified_names") or []),
                str(rep.get("verified_names")))
    ok &= check("它仍留在 untested（没证据就不许算通过）",
                "summarize" in (rep.get("untested") or []), str(rep.get("untested")))
    ok &= check("同一工地里 clean/store 没被误伤（精准归因，不连坐）",
                len(st_e["clean.py"]["verified_names"]) == 4 and
                len(st_e["store.py"]["verified_names"]) == 3,
                f"{st_e['clean.py']['verified_names']} / {st_e['store.py']['verified_names']}")

    # 6) 可维护性硬指标：圈复杂度超标必红、正常实现不误杀
    d = Path(tempfile.mkdtemp())
    (d / "messy.py").write_text(COMPLEX, encoding="utf-8")
    q = gates.gate_quality_metrics(d, ["messy.py"])
    ok &= check("圈复杂度超标判红", not q.ok, q.detail)
    (d / "messy.py").write_text(CLEAN, encoding="utf-8")
    q = gates.gate_quality_metrics(d, ["clean.py"])
    ok &= check("正常实现不被可维护性误杀", q.ok, q.detail)

    # 7) 变异门禁：零杀才拦，弱测试只记分不判死
    d = Path(tempfile.mkdtemp())
    (d / "calc.py").write_text(RICH_CALC, encoding="utf-8")
    (d / "test_calc.py").write_text(WEAK_TEST, encoding="utf-8")
    mm = gates.gate_mutation(d, {"calc.py": ("", RICH_CALC)}, ["test_calc.py"])
    ok &= check("弱测试只要还能杀一个变异体就不判死", mm.ok, mm.detail)
    # 变异体全存活 = 测试根本没盯住这段代码 → 必须拦
    (d / "retry.py").write_text(RETRY, encoding="utf-8")
    (d / "test_retry.py").write_text("def test_nothing():\n    assert 1 == 1\n", encoding="utf-8")
    mz = gates.gate_mutation(d, {"retry.py": ("", RETRY)}, ["test_retry.py"])
    ok &= check("变异体全存活（测试没盯住）判红", not mz.ok, mz.detail)
    # 基线测试本来就红时，任何变异都会"显得被杀死了" —— 必须识别出来，不能报 100%
    (d / "test_retry.py").write_text("from retry import retry_times\n\n\n"
                                     "def test_x():\n    assert retry_times() == 99\n",
                                     encoding="utf-8")
    mr = gates.gate_mutation(d, {"retry.py": ("", RETRY)}, ["test_retry.py"])
    ok &= check("基线测试是红的时指出变异结果无意义",
                mr.evidence_ref == "baseline-red", f"{mr.evidence_ref} / {mr.detail}")

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
