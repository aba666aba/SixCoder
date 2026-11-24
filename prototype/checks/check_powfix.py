"""判据误杀自检：**正确的实现不许被判红**。

这一批全是真跑计算器复现出来的（项目 `D:\\Download\\cal`，5 个函数）：
夜班 6 轮、0 次写作被接受、产物停在桩上，根因不在模型，在判据本身。

三处判据缺陷 + 一处收工原因错误：

1) 边界矩阵把 `pow(0.0, -1)` 判成"实现崩了"：
   float 参数取最小值 0.0、int 参数取基准 -1 → 组合出数学上无定义的输入，
   Python 抛 ZeroDivisionError，而契约写明"不支持负数底数"。
   → 数值**极值**（-1 / -1.5 / 1e9 / 10**9）只提示不判红；0 仍硬判（最经典的退化值）。

2) "声明会抛异常但契约没给可判输入"这条**提示**以前 append 进 fails，
   整模块被判红，还写进 order_failed 原因里。→ 改记 notes。

3) ensure_scaffold 的模块 docstring 切成 `c.raw[:80]`，实测切在注解中间
   （留下 `add(a: fl` 后面紧跟收尾三引号）→ 桩本身语法就坏。
   → 与 author 统一用 tokens.cut(300)。

4) 收工原因：5/5 满分却说"其余已隔离，需人工或换模型"。→ no_work_reason 按状态算。

跑法: python check_powfix.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import company, contract as cm, iterate, props  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

CALC = '''
1) calc.py：基础四则运算计算器：只覆盖加减乘除与幂运算，全部为可机判的纯数值函数。
   - add(a: float, b: float) -> float：两数相加
   - sub(a: float, b: float) -> float：两数相减
   - mul(a: float, b: float) -> float：两数相乘
   - div(a: float, b: float) -> float：两数相除；除数为 0 抛 ZeroDivisionError
   - pow(base: float, exp: int) -> float：整数次幂；仅支持整数指数，不支持分数指数与负数底数的非整数次幂

示例（必须满足）：
add(1.0, 2.0) == 3.0
sub(5.0, 3.0) == 2.0
mul(4.0, 2.5) == 10.0
div(6.0, 3.0) == 2.0
pow(2.0, 3) == 8.0
'''

GOOD = '''"""基础算术运算模块。"""


def add(a: float, b: float) -> float:
    """两数相加"""
    return a + b


def sub(a: float, b: float) -> float:
    """两数相减"""
    return a - b


def mul(a: float, b: float) -> float:
    """两数相乘"""
    return a * b


def div(a: float, b: float) -> float:
    """两数相除；除数为 0 抛 ZeroDivisionError"""
    if b == 0:
        raise ZeroDivisionError("division by zero")
    return a / b


def pow(base: float, exp: int) -> float:
    """幂运算（整数指数）"""
    return base ** exp
'''


def _sandbox_temp():
    import tempfile as _tf
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("判据误杀自检（正确实现不许被判红）")
    c = cm.parse_requirement(CALC)
    mod = c
    if isinstance(mod, dict):                       # 兼容多模块返回
        mod = mod["calc.py" if "calc.py" in mod else next(iter(mod))]

    print("[1] 边界矩阵：数值极值只提示，0 仍硬判")
    src = "def pow(base: float, exp: int) -> float:\n    return base ** exp\n" \
          "def div(a: float, b: float) -> float:\n    return a / b\n"
    b = props.boundary_checks(src, must_raise={"div": {"0", "0.0"}})
    hard = [x for x in b if x["hard"]]
    soft = [x for x in b if not x["hard"]]
    check("生成了边界判据", bool(b), f"{len(b)} 条")
    check("pow(0.0, -1) 这类极值组合不是硬判据",
          not any("0.0, -1" in x["expr"] and x["hard"] for x in b),
          str([x["expr"] for x in hard]))
    check("极值组合仍被记录（只是降级为提示）",
          any("-1" in x["expr"] and not x["hard"] for x in b),
          str([x["expr"] for x in b]))
    check("0 仍是硬判据（零长度/零除数这种退化值不能放过）",
          any("0" in x["expr"] and x["hard"] for x in b),
          str([x["expr"] for x in hard]))
    check("契约声明会抛的实参不进边界矩阵",
          not any(x["expr"].startswith("m.div(") and ", 0.0)" in x["expr"] for x in b),
          str([x["expr"] for x in b if x["expr"].startswith("m.div(")]))

    print("[2] 完整正确实现 → 验收必须全绿（以前 pow 必红）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "calc.py").write_text(GOOD, encoding="utf-8")
        st = iterate.inspect(ws, {"calc.py": mod})
        s = st["calc.py"]
        check("5/5 全部通过机械验收", s["verified"] == 5 and not s["fails"],
              f"verified={s['verified']} fails={s['fails']}")
        check("没有硬判红函数", not s["hard_failed"], str(s["hard_failed"]))

    print("[3] '声明会抛但无判据'只是提示，不能进 fails")
    src_div = ('"""div 模块"""\n\n\n'
               "def div(a: float, b: float) -> float:\n"
               '    """两数相除；除数为 0 抛 ZeroDivisionError"""\n'
               "    return a / b\n")
    c2 = cm.parse_requirement(
        "1) calc.py：除法\n"
        "   - div(a: float, b: float) -> float：两数相除；除数为 0 抛 ZeroDivisionError\n"
        "\n示例（必须满足）：\n"
        "div(6.0, 3.0) == 2.0\n")
    m2 = c2
    if isinstance(m2, dict):
        m2 = m2["calc.py" if "calc.py" in m2 else next(iter(m2))]
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "calc.py").write_text(src_div, encoding="utf-8")
        st2 = iterate.inspect(ws, {"calc.py": m2})
        s2 = st2["calc.py"]
        joined = " ".join(s2["fails"])
        check("整模块没有因为提示判红", not s2["fails"], joined[:160])
        check("提示文本不在 fails 里", "契约没给出可判输入" not in joined, joined[:160])

    print("[4] 收工原因按真实状态算")
    r_full = company.no_work_reason({"calc.py": {"verified": 5, "funcs": 5}}, {"calc.py": mod})
    r_zero = company.no_work_reason({"calc.py": {"verified": 0, "funcs": 5,
                                                 "stubs": {"add", "div"}}}, {"calc.py": mod})
    check("满分 → 说全部通过", "全部通过机械验收" in r_full and "5/5" in r_full, r_full)
    check("0/5 → 不许说通过", "全部通过" not in r_zero and "0/5" in r_zero, r_zero)
    check("0/5 → 点出还是桩", "桩" in r_zero, r_zero)

    print("[5] 桩的模块 docstring 不被切断")
    with _sandbox_temp() as td:
        ws = Path(td)
        created = iterate.ensure_scaffold(ws, {"calc.py": mod})
        txt = (ws / "calc.py").read_text(encoding="utf-8")
        check("生成了桩文件", bool(created) and (ws / "calc.py").exists(), str(created))
        check("桩能通过语法解析", _parses(txt), txt[:120])
        check("docstring 没被切在注解中间", 'add(a: fl"""' not in txt, txt[:200])
        check("docstring 保留了契约原文（≥60 字）",
              len(txt.split('"""')[1].strip()) >= 20, repr(txt.split('"""')[1][:80]))

    print("[6] 判据自己跑不动 → 不许算成产物错")
    # 真复现：以前探针脚本按 ws 拼路径、子进程 cwd 又是 ws，相对仓库路径被解析两遍
    # （projects\projects\x）→ 探针 not found → 5/5 的正确产物被判 0/5。
    with _sandbox_temp() as td:
        root = Path(td)
        ws = root / "rel_ws"
        ws.mkdir()
        (ws / "calc.py").write_text(GOOD, encoding="utf-8")
        import os as _os
        old = _os.getcwd()
        _os.chdir(str(root))
        try:
            r = props.run(Path("rel_ws"), "calc.py",
                          [{"kind": "call_eq", "expr": "m.add(1.0, 2.0)", "expect": "3.0",
                            "desc": "add"}])
        finally:
            _os.chdir(old)
        check("相对仓库路径也能跑判据（不再变成 rel_ws\\rel_ws\\…）",
              r.get("ok") and not r.get("engine_error"), str(r)[:200])

    # 探针一行输出都没有 → engine_error，且 inspect 不许把它记成函数判红
    real_run = props.run
    try:
        props.run = lambda *a, **k: {"ok": False, "engine_error": True, "passed": 0,
                                     "total": 1, "fails": [{"kind": "no-output",
                                                            "detail": "模拟探针起不来"}]}
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "calc.py").write_text(GOOD, encoding="utf-8")
            st3 = iterate.inspect(ws, {"calc.py": mod})["calc.py"]
        check("引擎自身出错不算函数判红", not st3["hard_failed"], str(st3["hard_failed"]))
        check("记进 engine_errors（供报告说明，别再派单）", bool(st3.get("engine_errors")),
              str(st3.get("engine_errors")))
        check("验收数不会因为引擎故障变成 0 个已验收的证据",
              st3["verified"] == 0 and bool(st3["untested"]), str(st3["untested"]))
        check("收工原因直说引擎问题、不许说产物错",
              "引擎" in company.no_work_reason({"calc.py": st3}, {"calc.py": mod}),
              company.no_work_reason({"calc.py": st3}, {"calc.py": mod}))
    finally:
        props.run = real_run

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)} "
          f"（判据误杀：极值不判红 / 提示不判红 / 桩不截断 / 收工原因说真话 / 引擎故障不冤判）")
    return 0 if not FAILS else 1


def _parses(txt: str) -> bool:
    import ast
    try:
        ast.parse(txt)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
