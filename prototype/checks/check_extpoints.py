"""扩展点（可替换性）自检（开发文档 §8.3）。

判据不是"有没有写抽象类"，而是**换一个实现、调用方一行不改**：
每个实现都必须提供协议要求的全部方法，且参数个数对得上。

跑法: python checks/check_extpoints.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import extpoints  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ep_"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


PROTO = '''class RuleExecutor:
    def execute(self, rules, ctx):
        raise NotImplementedError
'''

GOOD_A = '''class PythonRuleExecutor:
    def execute(self, rules, ctx):
        return []
'''

GOOD_B = '''class DroolsAdapter:
    def execute(self, rules, ctx):
        return []
'''

BAD_B = '''class DroolsAdapter:
    def run(self, rules, ctx):
        return []
'''

EP = {"module": "matching_core/policy.py", "protocol": "RuleExecutor",
      "impls": ["PythonRuleExecutor", "DroolsAdapter"], "invert_dependency": True}


def main() -> int:
    print("[1] 两份实现都完整 → 扩展点成立")
    ws = make({"matching_core/__init__.py": "", "matching_core/policy.py": PROTO,
               "matching_core/py_exec.py": GOOD_A, "matching_core/drools.py": GOOD_B})
    r = extpoints.verify(ws, EP)
    check("可替换性通过", r["ok"], str(r["problems"]))
    check("两个实现都找到了", [d["file"] for d in r["detail"]]
          == ["matching_core/py_exec.py", "matching_core/drools.py"], str(r["detail"]))

    print("[2] 少一个方法 → 必须红")
    ws2 = make({"matching_core/__init__.py": "", "matching_core/policy.py": PROTO,
                "matching_core/py_exec.py": GOOD_A, "matching_core/drools.py": BAD_B})
    r2 = extpoints.verify(ws2, EP)
    check("缺方法被抓到", r2["ok"] is False and any("缺方法" in p for p in r2["problems"]),
          str(r2["problems"]))

    print("[3] 参数个数对不上 → 必须红")
    odd = '''class DroolsAdapter:
    def execute(self, rules):
        return []
'''
    ws3 = make({"matching_core/__init__.py": "", "matching_core/policy.py": PROTO,
                "matching_core/py_exec.py": GOOD_A, "matching_core/drools.py": odd})
    r3 = extpoints.verify(ws3, EP)
    check("参数不匹配被抓到", r3["ok"] is False and any("参数" in p for p in r3["problems"]),
          str(r3["problems"]))

    print("[4] 实现根本不存在 → 必须红")
    ws4 = make({"matching_core/__init__.py": "", "matching_core/policy.py": PROTO,
                "matching_core/py_exec.py": GOOD_A})
    r4 = extpoints.verify(ws4, EP)
    check("缺失实现被抓到", r4["ok"] is False and any("找不到" in p for p in r4["problems"]),
          str(r4["problems"]))

    print("[5] 函数式协议（不是类）也能验")
    ws5 = make({"lib/__init__.py": "",
                "lib/plugin.py": "def render(node, ctx):\n    return ''\n",
                "lib/a.py": "def render_a(node, ctx):\n    return 'a'\n",
                "lib/b.py": "def render_b(node):\n    return 'b'\n"})
    ep = {"module": "lib/plugin.py", "protocol": "render",
          "impls": ["render_a", "render_b"]}
    r5 = extpoints.verify(ws5, ep)
    check("函数协议参数不符要红", r5["ok"] is False, str(r5))

    print("[6] 汇总 + 报告")
    allres = extpoints.verify_all(ws, {"extension_points": [EP]})
    check("verify_all 汇总正确", allres["ok"] and allres["count"] == 1, str(allres)[:120])
    lines = "\n".join(extpoints.report_lines(allres))
    check("报告写清可替换", "可替换" in lines, lines[:120])
    empty = "\n".join(extpoints.report_lines({"count": 0}))
    check("没有扩展点时不假装通过", "不是通过" in empty, empty[:120])

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（可替换才算成立 / 缺方法缺实现参数不符都红 / 报告不吹）")
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
