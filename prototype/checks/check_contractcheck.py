"""契约自查（示例对不对）自检。

为什么单独一个：实测事故——计算器契约把 `integral([3,0,2])` 的期望写成 0.5（正确是 0.6667），
系统于是"追一个错答案"，架构师每轮都诊断同一件事、远端救单 11 次全失败。
契约自查就是要在**写代码之前**把这种矛盾抓出来。

跑法: python check_contractcheck.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as cm  # noqa: E402
from core import contractcheck  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def c(text: str) -> dict:
    return contractcheck.check(cm.parse_requirement(text))


GOOD = """1) calc.py：
   - integral(coeffs: list[float]) -> list[float]：对多项式求不定积分，常数项取 0
   - derivative(coeffs: list[float]) -> list[float]：对多项式求导
示例（必须满足）：
integral([3.0, 0.0, 2.0]) == [0.0, 3.0, 0.0, 0.6666666666666666]
derivative([3.0, 0.0, 2.0]) == [0.0, 4.0]
"""

DUP = """1) calc.py：
   - integral(coeffs: list[float]) -> list[float]：对多项式求不定积分，常数项取 0
示例（必须满足）：
integral([3.0, 0.0, 2.0]) == [0.0, 3.0, 0.0, 0.6666666666666666]
integral([3.0, 0.0, 2.0]) == [0.0, 3.0, 0.0, 0.5]
"""

CONST = """1) calc.py：
   - integral(coeffs: list[float]) -> list[float]：对多项式求不定积分，常数项取 0
示例（必须满足）：
integral([3.0, 0.0, 2.0]) == [1.0, 3.0, 0.0, 0.6666666666666666]
"""

TYPEBAD = """1) calc.py：
   - sqrt(x: float) -> float：开平方
示例（必须满足）：
sqrt("9") == 3.0
"""

ARITY = """1) calc.py：
   - add(a: float, b: float) -> float：两数相加
示例（必须满足）：
add(1.0) == 3.0
"""

RETBAD = """1) calc.py：
   - sqrt(x: float) -> float：开平方
示例（必须满足）：
sqrt(9.0) == [3.0]
"""

ADV = """1) calc.py：
   - summarize(rows: list[dict]) -> dict：返回 count 与 avg_age，avg_age 保留 1 位小数
示例（必须满足）：
summarize([{"age": 1}]) == {"count": 1, "avg_age": 1.23456}
"""


def main() -> int:
    print("契约自查自检")

    print("[1] 正确契约：不许误报")
    r = c(GOOD)
    check("通过且无矛盾", r["ok"] and not r["hard"], str(r)[:160])
    check("也不该冒出一堆可疑", len(r["advisory"]) == 0, str(r["advisory"]))

    print("[2] 硬矛盾：必须抓到")
    r = c(DUP)
    check("同一参数两个期望 → 矛盾", not r["ok"] and any("两个期望" in h for h in r["hard"]),
          str(r["hard"])[:160])
    check("矛盾里点名函数 integral",
          any("integral" in h for h in r["hard"]), str(r["hard"])[:120])

    r = c(CONST)
    check("声明「常数项取 0」但首项非 0 → 矛盾",
          not r["ok"] and any("常数项" in h for h in r["hard"]), str(r["hard"])[:160])

    r = c(TYPEBAD)
    check("参数类型不符 → 矛盾",
          not r["ok"] and any("与声明" in h for h in r["hard"]), str(r["hard"])[:160])

    r = c(ARITY)
    check("参数个数不符 → 矛盾",
          not r["ok"] and any("参数个数" in h for h in r["hard"]), str(r["hard"])[:160])

    r = c(RETBAD)
    check("期望类型与返回声明不符 → 矛盾",
          not r["ok"] and any("返回声明" in h for h in r["hard"]), str(r["hard"])[:160])

    print("[3] 可疑（advisory）：只提示，不拦")
    r = c(ADV)
    check("保留 N 位小数但示例更长 → 可疑而不是矛盾",
          r["ok"] and any("位小数" in a for a in r["advisory"]), str(r["advisory"])[:160])

    print("[4] 渲染与可疑函数清单")
    r = c(DUP)
    text = contractcheck.render(r)
    check("渲染出【矛盾】标记", "【矛盾】" in text, text[:80])
    check("可疑函数清单包含 integral", "integral" in r["suspicious_funcs"],
          str(r["suspicious_funcs"]))
    check("正确契约的可疑清单为空", c(GOOD)["suspicious_funcs"] == [])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（自相矛盾/类型/个数/承诺冲突必抓；可疑只提示）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
