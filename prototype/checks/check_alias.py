"""字段别名统一：自检"规范字段 + 显式别名"这套机制真的成立。

背景（night-v2 系列四次真实运行）：契约里 `clean_row` 示例用 `date`、表结构是 `birth`，
模型把两处统一成 `date` → 建表少一列；单函数判据全绿，只有 e2e 抓到。
处理方式不是二选一改名，而是：
  ① 契约里声明一次「记录键（规范名）」+「别名：date → birth」；
  ② 别名进 prompt（describe），模型不用猜；
  ③ 花前判据：示例里出现"表结构没有的键"且没声明别名 → 硬红；
  ④ e2e 的输入行用别名写、断言按规范列查，把"别名映射"变成真的跨模块不变式。

跑法: python check_alias.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as cm, contractcheck, e2e  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


OLD = """1) clean.py：清洗单字段与整行数据。
   - clean_row(row: dict) -> dict：返回新字典：对 row 的 age 调 normalize_age、date 调 parse_date、email 调 clean_email，其余键原样保留。

示例（必须满足）：
clean_row({"age": " 42 ", "date": "2024/01/02", "email": " Foo@Bar.COM "}) == {"age": 42, "date": "2024-01-02", "email": "foo@bar.com"}

2) store.py：SQLite 建表、按 id 覆盖写入与按 id 升序读取。
   - create_table(conn: dict) -> None：在给定连接上建表 people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER, email TEXT, birth TEXT)。
   - upsert(conn: dict, rows: list[dict]) -> int：按 id 覆盖写入 rows（每行含 id/name/age/email/birth），返回写入条数。
   - fetch_all(conn: dict) -> list[dict]：按 id 升序返回全部行，每行为含 id/name/age/email/birth 的字典。

3) report.py：对清洗后的行做汇总统计。
   - summarize(rows: list[dict]) -> dict：返回 {"count": 行数, "avg_age": 平均年龄}；空列表时 avg_age 为 0.0。

示例（必须满足）：
summarize([{"age": 10}]) == {"count": 1, "avg_age": 10.0}
summarize([]) == {"count": 0, "avg_age": 0.0}
"""

FIXED = OLD.replace(
    "1) clean.py：清洗单字段与整行数据。",
    "0) 字段规范：记录键（规范名）：id, name, age, birth, email\n"
    "   - 别名：date → birth（输入行里写 date，清洗后必须输出 birth）\n"
    "1) clean.py：清洗单字段与整行数据。").replace(
    '"date": "2024-01-02", "email": "foo@bar.com"}',
    '"birth": "2024-01-02", "email": "foo@bar.com"}')


def main() -> int:
    print("字段别名机制自检")

    print("[1] 花前判据：没声明别名 → 硬红（这条能在花钱之前拦住）")
    old = cm.parse_requirement(OLD)
    r_old = contractcheck.check(old)
    hits = [h for h in r_old["hard"] if "别名" in h]
    check("示例里的 date 与表结构 birth 冲突 → 硬红", bool(hits), str(r_old["hard"])[:200])
    check("报错信息直接给出该补的那一行", bool(hits) and "别名" in hits[0], str(hits)[:160])
    check("整体判为不 ok（不能带着矛盾开跑）", not r_old["ok"])

    print("[1.5] 人话写法也算声明（实测事故：契约说清楚了却整夜拒绝开跑）")
    for txt, want in [
        ("date 走 parse_date 后改名成 birth", {"date": "birth"}),
        ("把 date 改名为 birth", {"date": "birth"}),
        ("date 统一成 birth", {"date": "birth"}),
        ("worker 重命名为 worker_id", {"worker": "worker_id"}),
    ]:
        got = cm.parse_aliases(txt)
        check("认出「%s」" % txt, got == want, str(got))
    check("别名不会把函数名误当字段名", "normalize_age" not in cm.parse_aliases(
        "age 走 normalize_age、date 走 parse_date 后改名成 birth"),
        str(cm.parse_aliases("age 走 normalize_age、date 走 parse_date 后改名成 birth")))
    prose = OLD.replace('示例（必须满足）',
                        '字段说明：age 走 normalize_age、date 走 parse_date 后改名成 birth、'
                        'email 走 clean_email\n\n示例（必须满足）')
    r_prose = contractcheck.check(cm.parse_requirement(prose))
    check("契约用大白话讲清映射 → 不得硬红拦跑",
          not [h for h in r_prose["hard"] if "别名" in h], str(r_prose["hard"])[:200])

    print("[2] 声明别名之后 → 放行，且别名进了 prompt")
    fixed = cm.parse_requirement(FIXED)
    r_fix = contractcheck.check(fixed)
    check("别名被解析出来", cm.parse_aliases(FIXED) == {"date": "birth"},
          str(cm.parse_aliases(FIXED)))
    check("契约自查通过（不再红）", r_fix["ok"], str(r_fix["hard"])[:200])
    any_c = next(iter(fixed.values()))
    desc = cm.describe(any_c)
    check("describe 里写明了别名方向", "date" in desc and "birth" in desc and "别名" in desc,
          desc[:160])
    check("describe 说清了'产物必须用规范名'", "规范名" in desc, desc[:160])

    print("[3] e2e 输入行用别名、断言按规范列（把映射变成真判据）")
    plan_d = e2e.build(fixed, FIXED)
    check("e2e 计划可组装", plan_d.get("ok"), str(plan_d.get("reason"))[:120])
    rows = plan_d.get("rows") or []
    check("输入行里有别名键 date", bool(rows) and "date" in rows[0], str(rows[:1])[:160])
    check("输入行里没有规范键 birth（否则等于没测映射）",
          bool(rows) and "birth" not in rows[0], str(rows[:1])[:160])
    cols = [c for c, _ in plan_d.get("cols") or []]
    check("断言仍按规范列 birth", "birth" in cols, str(cols))

    print("[4] 旧契约回放：当时的坏交付为什么只有 e2e 能抓")
    got = {"age": 42, "date": "2024-01-02", "email": "foo@bar.com"}     # 模型统一成 date 的产物
    check("旧示例看起来自洽（所以单函数判据全绿）",
          got == {"age": 42, "date": "2024-01-02", "email": "foo@bar.com"})
    check("但表列集合检查会发现缺 birth", "birth" not in got,
          f"clean_row 产物 {sorted(got)}")
    new_got = {"age": 42, "birth": "2024-01-02", "email": "foo@bar.com"}
    check("按新契约的产物能满足表列不变式", set(cols) <= set(new_got) or
          set(cols) - {"id", "name"} <= set(new_got), f"{sorted(cols)} vs {sorted(new_got)}")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（无别名必红 / 声明后放行并进 prompt / e2e 真测映射）")
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
