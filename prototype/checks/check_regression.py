"""回归用例库自检：登记、去重、按需求指纹隔离、合并进闸门判据、清理。

跑法: python check_regression.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import props, regression  # noqa: E402

REQ = "实现 int_to_roman(n: int) -> str 与 roman_to_int(s: str) -> int（1..3999），两者互为逆运算；非法输入应抛 ValueError。"
OTHER = "实现 merge_intervals(intervals: list[list[int]]) -> list[list[int]]，合并重叠区间。"

GOOD = '''def int_to_roman(n: int) -> str:
    """罗马数字。"""
    if not 0 < n < 4000:
        raise ValueError("out of range")
    table = [(1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"),
             (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")]
    out = ""
    for v, s in table:
        while n >= v:
            out += s
            n -= v
    return out


def roman_to_int(s: str) -> int:
    """解析罗马数字。"""
    table = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    total, prev = 0, 0
    for ch in reversed(s):
        v = table[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total
'''

BAD = GOOD.replace('if not 0 < n < 4000:\n        raise ValueError("out of range")', 'if n < 0:\n        return ""')


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("回归用例库自检")
    regression.clear()

    checks = [
        {"kind": "roundtrip", "desc": "roman_to_int(int_to_roman(x)) == x",
         "forward": "m.int_to_roman(X)", "back": "m.roman_to_int(X)", "samples": [1, 4, 1994]},
        {"kind": "raises", "expr": "m.int_to_roman(0)", "exc": "ValueError",
         "desc": "int_to_roman(0) 应当抛 ValueError"},
    ]
    n1 = regression.add(REQ, "roman.py", checks, task_id="t1", node_id="n1", source="remote")
    ok &= check("首次登记 1 组", n1 == 1, str(n1))
    n2 = regression.add(REQ, "roman.py", checks, task_id="t2", node_id="n2", source="remote")
    ok &= check("重复登记被去重", n2 == 0, str(n2))

    got = regression.for_task(REQ, "roman.py")
    ok &= check("同需求同模块能取回", len(got) == 2, str(len(got)))
    ok &= check("不同需求取不到（需求指纹隔离）",
                regression.for_task(OTHER, "roman.py") == [], "应隔离")
    # 模块名允许回退：同一需求下文件可能被重命名，需求指纹已足够窄
    ok &= check("同需求不同模块允许回退命中",
                len(regression.for_task(REQ, "calc.py")) == 2, "回退语义")

    # 合并后必须能挡住"回归"：坏实现不抛 ValueError
    tmp = Path(tempfile.mkdtemp())
    (tmp / "roman.py").write_text(BAD, encoding="utf-8")
    res = props.run(tmp, "roman.py", regression.for_task(REQ, "roman.py"))
    ok &= check("回归判据能抓住退化的实现", not res["ok"], props.as_feedback(res))
    print(f"    {props.as_feedback(res).splitlines()[-1]}")

    (tmp / "roman.py").write_text(GOOD, encoding="utf-8")
    res2 = props.run(tmp, "roman.py", regression.for_task(REQ, "roman.py"))
    ok &= check("正确实现通过回归判据", res2["ok"], props.as_feedback(res2))

    ok &= check("列表能看到登记项", len(regression.listing()) == 1, str(regression.listing()))
    removed = regression.clear()
    ok &= check("可一次性清空", removed == 1 and regression.listing() == [], str(removed))

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
