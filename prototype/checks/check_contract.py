"""结构化契约自检：解析真实需求、示例归属、异常/键抽取、下游一致性、无回归。

跑法: python check_contract.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as C  # noqa: E402
from core import gates, props, skeleton  # noqa: E402

REQ = open(str(Path(__file__).resolve().parent.parent / "tools" / "fixtures" / "hq_requirement.txt"), encoding="utf-8").read()


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("结构化契约自检")
    cs = C.parse_requirement(REQ)
    ok &= check("解析出 3 个模块", set(cs) == {"clean.py", "store.py", "report.py"}, str(list(cs)))

    clean = cs["clean.py"]
    ok &= check("clean 有 4 个函数",
                clean.names() == ["normalize_age", "parse_date", "clean_email", "clean_row"],
                str(clean.names()))
    age = clean.by_name("normalize_age")
    ok &= check("参数与类型正确",
                [(p.name, p.type) for p in age.params] == [("x", "str")], str(age.params))
    ok &= check("返回类型正确", age.returns == "int", age.returns)
    ok &= check("异常被抽取", "ValueError" in age.raises, str(age.raises))
    ok &= check("示例被归属", [e.expect for e in age.examples] == ["42"],
                str([e.expect for e in age.examples]))

    store = cs["store.py"]
    ok &= check("store 三个函数参数完整",
                [(f.name, [p.name for p in f.params]) for f in store.funcs] ==
                [("create_table", ["conn"]), ("upsert", ["conn", "rows"]),
                 ("fetch_all", ["conn"])],
                str([(f.name, [p.name for p in f.params]) for f in store.funcs]))
    ok &= check("list[dict] 类型保留",
                store.by_name("upsert").params[1].type == "list[dict]",
                store.by_name("upsert").params[1].type)

    rep = cs["report.py"]
    ok &= check("返回键被抽取", rep.by_name("summarize").keys == ["count", "avg_age", "domains"],
                str(rep.by_name("summarize").keys))

    # 下游一致性：骨架补参数 / 完整性闸门 / 判据生成 都吃同一份契约
    params = skeleton._contract_params(store.raw)
    ok &= check("骨架拿到正确参数", params.get("upsert") == ["conn", "rows"], str(params))

    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    (tmp / "store.py").write_text("def create_table(conn):\n    return 1\n", encoding="utf-8")
    r = gates.gate_completeness(tmp, ["store.py"], store.raw)
    ok &= check("完整性闸门抓缺失函数", (not r.ok) and "upsert" in r.detail, r.detail)

    chk = props.from_contract(clean)
    kinds = {c["kind"] for c in chk}
    ok &= check("判据覆盖 示例/异常", {"call_eq", "raises"} <= kinds, str(kinds))
    ok &= check("report 生成键判据",
                any(c["kind"] == "keys" for c in props.from_contract(rep)), "缺 keys 判据")

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
