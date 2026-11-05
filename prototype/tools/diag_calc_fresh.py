"""从零再来一遍（副本里删掉 calc.py）：验证脚手架 docstring 不再被截断、
pow 的语义不再漂移（契约只排除"负数底的**非整数**次幂"）。

不动客户目录：全部在 projects/_calc_fresh 副本里做。
"""
import io
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company, contract as cm, props  # noqa: E402

SRC = Path("D:/Download/cal")
DST = ROOT.parent / "projects" / "_calc_fresh"
if DST.exists():
    shutil.rmtree(DST, ignore_errors=True)
shutil.copytree(SRC, DST, ignore=shutil.ignore_patterns(
    ".git", ".mypy_cache", "__pycache__", "calc.py", "test_calc.py",
    "DELIVERY.md", "TRACE.md"))
REQ = "生成一个计算器"
print("工作区：", DST)

res = company.night(DST, REQ, "qwen2.5-coder:7b", hours=0.08, use_remote=True,
                    max_cny=0.2, emit=lambda k, p: (k in ("order_failed", "decision",
                                                          "order_done", "stop")
                                                    and print(f"  [{k}] {str(p)[:200]}")))
print(f"\n结果：{res.verified}/{res.total}｜{res.reason[:120]}")

txt = (DST / "calc.py").read_text(encoding="utf-8") if (DST / "calc.py").exists() else ""
print("\n--- calc.py ---")
print(txt)

c = cm.parse_requirement((DST / ".sixcoder" / "contract.txt").read_text(encoding="utf-8"))
mod = c["calc.py"]
full_pow = [f for f in mod.funcs if f.name == "pow"][0].doc
import ast
have = {n.name: (ast.get_docstring(n) or "") for n in ast.parse(txt).body
        if isinstance(n, ast.FunctionDef)}
print("pow 契约全文：", full_pow)
print("pow 实际 docstring：", have.get("pow"))
print("docstring 完整？", have.get("pow") == full_pow)

sys.path.insert(0, str(DST))
import importlib
m = importlib.import_module("calc")
for expr, fn, want in [
    ("pow(2.0, 3)", lambda: m.pow(2.0, 3), 8.0),
    ("pow(-2.0, 2)", lambda: m.pow(-2.0, 2), 4.0),      # 负底数 + 整数次幂 → 契约支持
    ("pow(0.0, 0)", lambda: m.pow(0.0, 0), 1.0),
    ("div(1, 0)", lambda: m.div(1, 0), "ZeroDivisionError"),
]:
    try:
        got = fn()
        print(f"{expr} = {got}   {'OK' if got == want else '与期望不同（期望 %r）' % (want,)}")
    except Exception as e:
        ok = isinstance(want, str) and type(e).__name__ == want
        print(f"{expr} 抛 {type(e).__name__}   {'OK' if ok else '与期望不同（期望 %r）' % (want,)}")
