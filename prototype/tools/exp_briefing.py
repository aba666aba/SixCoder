"""实验：模型写的"任务简报"到底有没有用（A/B，同一证据、同一模型）。

问题（甲方提的）：把"这次要做什么"交给模型写，会不会比人写的模板更准？
做法：
  1. 造一个真会失败的场景 —— summarize 还是桩，用引擎判据跑出**真实失败证据**；
  2. 同一份证据下，分别以 SIXCODER_BRIEFING=0 / 1 让执行者改同一张单，各跑 N 次；
  3. 对比：是否产出可渲染的实现 · 是否过契约判据 · 耗时 · 简报是否通过校验。

用法：python tools/exp_briefing.py [--runs 3]
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PROTO = HERE.parent.parent
sys.path.insert(0, str(PROTO))

from core import briefing, contract as cm, iterate, props  # noqa: E402

FIXTURE = PROTO / "tools" / "fixtures" / "hq_requirement.txt"

STUB_REPORT = '''"""report 模块。"""

def summarize(rows: list[dict]) -> dict:
    """summarize(rows: list[dict]) -> dict：返回 {"count": 人数, "avg_age": 平均年龄,
    "domains": 邮箱域名计数字典}"""
    raise NotImplementedError
'''

GOOD_OTHERS = {
    "clean.py": '''def normalize_age(x: str) -> int:
    s = x.strip()
    if not s:
        raise ValueError("空")
    n = int(s)
    if not 0 <= n <= 150:
        raise ValueError("越界")
    return n


def parse_date(s: str) -> str:
    from datetime import datetime
    s = s.strip().replace("/", "-")
    try:
        return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("格式错误")


def clean_email(s: str) -> str:
    v = s.strip().lower()
    if "@" not in v or not v.split("@")[0] or not v.split("@")[1]:
        raise ValueError("非法邮箱")
    return v


def clean_row(row: dict) -> dict:
    out = dict(row)
    out["age"] = normalize_age(str(out.get("age", "")))
    if out.get("birth") is not None:
        out["birth"] = parse_date(str(out["birth"]))
    if out.get("email") is not None:
        out["email"] = clean_email(str(out["email"]))
    return out
''',
    "store.py": '''import sqlite3


def create_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS people(id INTEGER PRIMARY KEY, name TEXT, "
                 "age INTEGER, email TEXT, birth TEXT)")
    conn.commit()


def upsert(conn, rows: list) -> int:
    for r in rows:
        conn.execute("INSERT INTO people(id,name,age,email,birth) VALUES(?,?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET name=excluded.name", 
                     (r.get("id"), r.get("name"), r.get("age"), r.get("email"), r.get("birth")))
    conn.commit()
    return len(rows)


def fetch_all(conn) -> list:
    cur = conn.execute("SELECT id,name,age,email,birth FROM people ORDER BY id")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]
''',
}


def prepare() -> tuple[Path, dict]:
    ws = Path(tempfile.mkdtemp(prefix="brief_"))
    for name, body in GOOD_OTHERS.items():
        (ws / name).write_text(body, encoding="utf-8")
    (ws / "report.py").write_text(STUB_REPORT, encoding="utf-8")
    text = FIXTURE.read_text(encoding="utf-8")
    contracts = cm.parse_requirement(text)
    return ws, contracts


def real_evidence(ws: Path, contracts: dict) -> str:
    """用引擎判据跑一次桩，拿到真实的失败原文（简报必须逐字引用它）。"""
    c = contracts["report.py"]
    f = next(x for x in c.funcs if x.name == "summarize")
    sub = props.from_contract(iterate._single_func_contract(c, f))
    r = props.run(ws, "report.py", sub)
    return props.as_feedback(r)


def run_once(ws: Path, contracts: dict, briefing_on: bool, model: str) -> dict:
    os.environ["SIXCODER_BRIEFING"] = "1" if briefing_on else "0"
    ev = real_evidence(ws, contracts)
    order = {"kind": "implement", "target": "report.py", "symbol": "summarize",
             "acceptance": ev, "detail": "按契约实现 summarize，返回 count/avg_age/domains"}
    t0 = time.time()
    res = iterate.execute_local(ws, order, model, attempts=1)
    ms = int((time.time() - t0) * 1000)
    # 是否过契约判据
    c = contracts["report.py"]
    f = next(x for x in c.funcs if x.name == "summarize")
    after = props.run(ws, "report.py",
                      props.from_contract(iterate._single_func_contract(c, f)))
    return {"briefing_on": briefing_on, "ok": bool(res.get("ok")),
            "reason": str(res.get("reason") or "")[:100], "ms": ms,
            "passes_contract": bool(after.get("ok")),
            "imports_fixed": res.get("imports_fixed") or []}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default="qwen2.5:7b")
    args = ap.parse_args()

    # 简报本身能不能生成（远端写 → 机械校验）
    ws0, cs0 = prepare()
    ev0 = real_evidence(ws0, cs0)
    b = briefing.generate(ws0, "summarize", "summarize(rows: list[dict]) -> dict",
                          ev0, STUB_REPORT, task_id="exp")
    print("简报生成：", "通过校验" if b.get("ok") else f"未通过（{b.get('reason')}）")
    if b.get("ok"):
        print("简报正文：\n" + b.get("text", "")[:600] + "\n")

    rows = []
    for i in range(args.runs):
        for on in (False, True):
            ws, cs = prepare()
            r = run_once(ws, cs, on, args.model)
            rows.append(r)
            print(f"  #{i+1} briefing={'on ' if on else 'off'} "
                  f"渲染={r['ok']} 过判据={r['passes_contract']} {r['ms']}ms "
                  f"{r['reason'][:60]}", flush=True)
            shutil.rmtree(ws, ignore_errors=True)

    print("\n=== 汇总 ===")
    for on in (False, True):
        sub = [x for x in rows if x["briefing_on"] == on]
        got = sum(1 for x in sub if x["ok"])
        passed = sum(1 for x in sub if x["passes_contract"])
        ms = sorted(x["ms"] for x in sub)
        print(f"briefing={'on ' if on else 'off'}: 渲染可落盘 {got}/{len(sub)}｜"
              f"过契约判据 {passed}/{len(sub)}｜中位耗时 {ms[len(ms)//2]}ms")
    os.environ.pop("SIXCODER_BRIEFING", None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
