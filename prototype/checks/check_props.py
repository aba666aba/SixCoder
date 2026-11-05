"""引擎判据自检：示例抽取、round-trip 性质、反例定位、误报防护。

跑法: python check_props.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import props  # noqa: E402

REQ_ROMAN = """实现 int_to_roman(n: int) -> str 与 roman_to_int(s: str) -> int（1..3999），两者互为逆运算；
int_to_roman(3) == 'III'；roman_to_int('LVIII') == 58；非法输入（如 0）应抛 ValueError。"""

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
    total = 0
    prev = 0
    for ch in reversed(s):
        v = table[ch]
        total = total - v if v < prev else total + v
        prev = max(prev, v)
    return total
'''

STUB = '''def int_to_roman(n: int) -> str:
    """桩。"""
    raise NotImplementedError


def roman_to_int(s: str) -> int:
    """桩。"""
    raise NotImplementedError
'''

WRONG = GOOD.replace('table = {"I": 1', 'table = {"I": 2')


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("引擎判据自检")
    tmp = Path(tempfile.mkdtemp())

    # 1) 抽取
    (tmp / "roman.py").write_text(GOOD, encoding="utf-8")
    syms = props.module_symbols(tmp, "roman.py")
    ok &= check("识别产物符号", {"int_to_roman", "roman_to_int"} <= syms, str(syms))
    checks = props.from_requirement(REQ_ROMAN, syms)
    kinds = {c["kind"] for c in checks}
    ok &= check("抽到示例断言", "call_eq" in kinds, str(checks))
    ok &= check("抽到异常断言", "raises" in kinds, str(checks))
    ok &= check("抽到 round-trip 性质", "roundtrip" in kinds, str(checks))
    print(f"    共 {len(checks)} 条判据: {[c['desc'] for c in checks][:5]}")

    # 2) 正确实现 → 全过
    r = props.run(tmp, "roman.py", checks)
    ok &= check("正确实现全部通过", r["ok"], props.as_feedback(r))
    print(f"    {props.as_feedback(r)}")

    # 3) 桩 → 必须失败且给出具体反例
    (tmp / "roman.py").write_text(STUB, encoding="utf-8")
    r2 = props.run(tmp, "roman.py", checks)
    ok &= check("桩实现被抓住", not r2["ok"], props.as_feedback(r2))
    ok &= check("反例里有具体输入", "NotImplementedError" in props.as_feedback(r2),
                props.as_feedback(r2))
    print(f"    {props.as_feedback(r2).splitlines()[0]}")

    # 4) 逻辑错误 → 必须抓住（round-trip 比示例更强）
    (tmp / "roman.py").write_text(WRONG, encoding="utf-8")
    r3 = props.run(tmp, "roman.py", checks)
    ok &= check("逻辑错误被 round-trip 抓住", not r3["ok"], props.as_feedback(r3))
    print(f"    {props.as_feedback(r3).splitlines()[-1]}")

    # 5) 误报防护：无判据时跳过，不变红
    (tmp / "plain.py").write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
    r4 = props.run(tmp, "plain.py", [])
    ok &= check("无判据时跳过而不误判", r4["ok"] and r4.get("skipped"), str(r4))

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
