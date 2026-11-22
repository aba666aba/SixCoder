"""M1/M2 机制自检：不跑模型，只验证机制本身可运转（规则、差分执行、选优逻辑）。

跑法: python check_mechanisms.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import agent, reference  # noqa: E402

IMPL = '''from __future__ import annotations


def clamp(x: int, lo: int, hi: int) -> int:
    """把 x 夹在 [lo, hi]。"""
    return max(lo, min(hi, x))


def safe_div(a: int, b: int) -> float:
    """安全除法，除零返回 0.0。"""
    return a / b if b else 0.0
'''

REF_OK = '''from __future__ import annotations


def clamp(x: int, lo: int, hi: int) -> int:
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def safe_div(a: int, b: int) -> float:
    if b == 0:
        return 0.0
    return a / b
'''

REF_BAD = REF_OK.replace("return a / b", "return a * b")


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("机制自检（M1 参照差分 / M2 候选选优）")

    for p in ("core/agent.py", "core/reference.py"):
        try:
            ast.parse((ROOT / p).read_text(encoding="utf-8"))
            ok &= check(f"{p} 语法", True)
        except SyntaxError as exc:
            ok &= check(f"{p} 语法", False, str(exc))

    pure = reference._pure_funcs(IMPL)
    ok &= check("抽出纯函数并推断参数类型",
                [f["name"] for f in pure] == ["clamp", "safe_div"], str(pure))
    cases = reference.gen_cases(pure, n=8)
    ok &= check("生成小规模样例", len(cases) == 8 and all(len(c) == 2 for c in cases),
                str(cases[:2]))

    tmp = Path(tempfile.mkdtemp())
    (tmp / "impl.py").write_text(IMPL, encoding="utf-8")

    # 参照一致 → 差分应通过
    (tmp / "impl_ref.py").write_text(REF_OK, encoding="utf-8")
    funcs = [f["name"] for f in pure]
    res = reference.differential(tmp, "impl.py", "impl_ref.py", cases, funcs)
    ok &= check("参照一致时差分通过", res["ok"] and res["checked"] == 16, str(res))
    print("    " + reference.as_feedback(res))

    # 参照不一致（故意把除法写成乘法）→ 必须报出具体反例
    (tmp / "impl_ref.py").write_text(REF_BAD, encoding="utf-8")
    res2 = reference.differential(tmp, "impl.py", "impl_ref.py", cases, funcs)
    ok &= check("实现与参照不一致时被抓住", (not res2["ok"]) and bool(res2["divergences"]),
                str(res2))
    fb = reference.as_feedback(res2)
    ok &= check("反馈里带具体反例（函数名+输入）", "safe_div" in fb, fb[:200])
    print("    " + fb.splitlines()[0])

    # M2：质量分选择
    good = [("types", True, "类型检查通过"), ("style", True, "与项目规约一致"),
            ("tests", True, "3 passed")]
    worse = [("types", True, "类型建议 4 条（不阻塞）：x"), ("style", True, "偏离 2 处"),
             ("tests", True, "3 passed")]
    ok &= check("质量分：干净的候选更优", agent._quality(good) < agent._quality(worse),
                f"{agent._quality(good)} vs {agent._quality(worse)}")
    ok &= check("M2 默认关闭（要显式开实验）", agent._bestofn_enabled() is False)
    os.environ["SIXCODER_BESTOFN"] = "1"
    ok &= check("M2 开关可开启", agent._bestofn_enabled() is True)
    os.environ.pop("SIXCODER_BESTOFN")
    ok &= check("M1 默认关闭", agent._ref_enabled() is False)

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
