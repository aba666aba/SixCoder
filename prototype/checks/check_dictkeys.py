"""字段名幻觉闸门 + 示例抽取自检。

两类实测缺陷各一条反例：
  1. 示例抽取：`summarize([{"age": 10, "email": "a@x.com"}]) == {...}` 过去被 `split(",")`
     切成半截 → 这类"返回整个 dict"的函数长期没有可判定示例（而它们最容易写错）。
  2. 字段幻觉：模型写 `row['domain']`，样例与表里只有 age/email/birth/id/name → 运行时 KeyError。
     静态就能判，且必须区分"凭空造字段"（阻塞）与"需求没说清"（提示，不阻塞）。

跑法: python check_dictkeys.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as cm  # noqa: E402
from core import gates, props  # noqa: E402

FAILS: list[str] = []

REQ = """写三个模块：

1) store.py：create_table(conn) -> None：建表 people(id INTEGER PRIMARY KEY, name TEXT,
age INTEGER, email TEXT, birth TEXT)；upsert(conn, rows: list[dict]) -> int：按 id 覆盖写入。

2) clean.py：clean_row(row: dict) -> dict：把 age 转 int，并处理 date 字段（原样返回）。

3) report.py：summarize(rows: list[dict]) -> dict：返回 {"count": 人数, "avg_age": 平均年龄,
"domains": 邮箱域名计数字典}。

示例（必须满足）：
clean_row({"age": " 30 "}) == {"age": 30}
summarize([{"age": 10, "email": "a@x.com"}]) == {"count": 1, "avg_age": 10.0, "domains": {"x.com": 1}}
"""


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    contracts = cm.parse_requirement(REQ)
    print("[1] 示例抽取（嵌套 dict 不再被切碎）")
    summ = contracts["report.py"].by_name("summarize")
    check("summarize 抽到了示例", len(summ.examples) >= 1, str(summ.examples))
    if summ.examples:
        ex = summ.examples[0]
        check("参数是完整的一个列表字面量", len(ex.args) == 1 and ex.args[0].startswith("[{"),
              str(ex.args))
        check("期望值是完整 dict", ex.expect.startswith("{") and ex.expect.endswith("}"), ex.expect)
    checks = props.from_contract(contracts["report.py"])
    check("示例被渲染成 call_eq 判据",
          any(c["kind"] == "call_eq" for c in checks), str([c["kind"] for c in checks]))

    print("[2] 字段幻觉：凭空造字段必须判红")
    ws = Path(tempfile.mkdtemp(prefix="dk_"))
    (ws / "report.py").write_text(
        "def summarize(rows):\n"
        "    count = len(rows)\n"
        "    domains = {r['domain'] for r in rows}\n"      # 造字段
        "    return {'count': count, 'avg_age': 0, 'domains': domains}\n", encoding="utf-8")
    allowed = {"age", "email", "birth", "id", "name", "count", "avg_age", "domains"}
    r, adv = gates.gate_dict_keys(ws, ["report.py"], allowed, REQ)
    check("凭空字段 domain 判红", not r.ok, r.detail)
    check("报红点到了函数名与字段", "summarize()" in r.detail and "domain" in r.detail, r.detail)

    print("[3] 契约提到过但表里没有 → 只提示不阻塞（需求歧义）")
    (ws / "clean.py").write_text(
        "def clean_row(row):\n"
        "    row = dict(row)\n"
        "    if 'date' in row:\n"
        "        row['date'] = str(row['date'])\n"
        "    return row\n", encoding="utf-8")
    r2, adv2 = gates.gate_dict_keys(ws, ["clean.py"], {"id", "name", "age", "email", "birth"}, REQ)
    check("date 不阻塞（不是凭空造的）", r2.ok, r2.detail)
    check("但作为歧义提示出来", any("date" in x for x in adv2), str(adv2))

    print("[4] 正常实现不许误杀")
    (ws / "ok.py").write_text(
        "def summarize(rows):\n"
        "    for row in rows:\n"
        "        row.get('age')\n"
        "        row.get('email')\n"
        "    return {'count': len(rows), 'avg_age': 0, 'domains': {}}\n", encoding="utf-8")
    r3, _ = gates.gate_dict_keys(ws, ["ok.py"], allowed, REQ)
    check("只契约里有的字段 → 绿", r3.ok, r3.detail)
    r4, _ = gates.gate_dict_keys(ws, ["ok.py"], set(), REQ)
    check("没有样例键名时如实跳过", r4.ok and r4.evidence_ref == "no-sample", r4.detail)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（嵌套示例抽得到、造字段必红、需求歧义只提示、正常代码不误杀）")
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
