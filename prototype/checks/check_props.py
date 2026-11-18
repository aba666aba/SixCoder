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

    # 6) 判据自己的表达式必须是**合法 Python**（2026-11-07 真机事故）
    #    契约 `run_batch_endpoint(batch_id: str)` 声明"非法输入抛 ValueError"，
    #    探针把已带引号的 `"!!!"` 又套了一层 → `m.f(""!"""")` → 求值 SyntaxError
    #    → 判据说"没抛 ValueError" → 这个函数永远过不了（8/9 也算失败，整片白跑）。
    ok &= check("已引号的字面量不会被再套一层",
                props._probe_literal('"!!!"', "str") == '"!!!"',
                props._probe_literal('"!!!"', "str"))
    ok &= check("裸值仍按类型加引号",
                props._probe_literal("-1", "str") == '"-1"',
                props._probe_literal("-1", "str"))
    import ast as _ast
    bad = []
    for c in props.from_requirement(
            "- run(batch_id: str) -> dict：非法输入（如 \"!!!\"）应抛 ValueError。\n",
            {"run"}):
        expr = "m." + str(c.get("expr") or "x") if not str(c.get("expr") or "").startswith("m.") \
            else str(c.get("expr"))
        try:
            _ast.parse(expr, mode="eval")
        except SyntaxError:
            bad.append(expr)
    ok &= check("生成的判据表达式都能解析", not bad, str(bad[:3]))

    # ── 端点返回 JSONResponse：合法写法不许被判红（2026-11-10 23:00 实测）──────
    # 事故：`app/api/*.py` 端点返回 JSONResponse 是 FastAPI 合法写法（引擎自己渲染的
    # api 层也这么写），但契约样例是 `f(...) == {...}` → 被判"应为 dict"，
    # 怎么改都过不了；api 层 6 个函数吃掉 94 次远端调用后 heal_stop。
    print("[N] 返回 Response 对象（FastAPI 端点合法写法）→ 拆 body 再比")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "api_mod.py").write_text(
            "class _Resp:\n"
            "    def __init__(self, body):\n"
            "        self.body = body\n\n"
            "def get_me(token):\n"
            "    return _Resp(b'{\"id\": 7, \"name\": \"a\"}')\n\n"
            "def get_bad(token):\n"
            "    return _Resp(b'not-json')\n",
            encoding="utf-8")
        r_ok = props.run(root, "api_mod.py", [{
            "kind": "call_eq", "expr": "m.get_me('t')",
            "expect": "{'id': 7, 'name': 'a'}", "hard": True,
            "desc": "m.get_me('t') == {'id': 7, 'name': 'a'}"}])
        ok &= check("JSONResponse 里的 JSON 与期望相等 → 判过（不再冤红）",
                    bool(r_ok.get("ok")), str(r_ok.get("fails"))[:160])
        r_bad = props.run(root, "api_mod.py", [{
            "kind": "call_eq", "expr": "m.get_bad('t')",
            "expect": "{'id': 7}", "hard": True, "desc": "m.get_bad('t') == {'id': 7}"}])
        ok &= check("body 不是合法 JSON → 该红还是红（不放松标准）",
                    not bool(r_bad.get("ok")), "竟然判过了")

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
