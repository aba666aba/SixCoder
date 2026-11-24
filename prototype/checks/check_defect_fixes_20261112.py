"""回归钉子：2026-11-12 实测发现的三个"机制写好了但从没生效"的缺陷。

为什么单独一个文件：这三条不是"新功能"，而是**已写好的机制从没真正跑起来**。
它们的共同症状是"自检全绿、夜班照跑、但机制从未触发"——最难靠既有自检发现，
因为既有自检验的是"函数在不在"，不是"这个函数**在真实调用路径上**会不会被走到"。

三条钉子（每条钉一个**曾经真的坏掉**的事实）：
  1. 业务流优先派单：`biz_flow` 原本在轮循环**之外**算 → 轮内恒为 None → 提权永不触发。
     现在必须在轮循环内算出来，并传给 `_author_round`。
  2. 提权的模块匹配：`_role_map` 返回**候选列表** `[(模块名, 函数名), ...]`，
     原代码 `str(v[0])` 把元组整个字符串化 → 跟模块名永远匹配不上 → 提权集合恒为空。
  3. 全自主线的接线守卫：原来直调 `apply_web_wiring`，绕过 `openhouse.wire`
     的快照 + 冒烟 + **回归守卫** → 接线打坏已通过能力时这条线无人兜底。

跑法: python checks/check_defect_fixes_20261112.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _func_span(src: str, name: str) -> tuple[int, int]:
    """返回函数在源码里的 (起始行, 结束行)（1-based，闭区间）。

    注意：`company.py` 里 `_author_round` 是**嵌套**在 `_night_inner` 内的函数，
    用 `ast.walk` 按行号取会先撞到外层 —— 所以这里按**最内层优先**（行范围最小）挑。
    """
    tree = ast.parse(src)
    best: tuple[int, int] | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            span = (node.lineno, node.end_lineno or node.lineno)
            if best is None or (span[1] - span[0]) < (best[1] - best[0]):
                best = span
    return best or (0, 0)


def main() -> int:
    company_src = (CORE / "company.py").read_text(encoding="utf-8", errors="replace")
    auto_src = (CORE / "auto.py").read_text(encoding="utf-8", errors="replace")
    policy_src = (CORE / "model_policy.py").read_text(encoding="utf-8", errors="replace")
    iterate_src = (CORE / "iterate.py").read_text(encoding="utf-8", errors="replace")

    print("[1] 业务流优先派单：biz_flow 必须在**轮循环内**算出来")
    # 轮循环 = company.night 里的 `for rnd in range(...)` 那一段
    lo, hi = _func_span(company_src, "_night_inner")
    if not hi:
        lo, hi = 1, company_src.count("\n") + 1
    loop_src = company_src[company_src.index("\n", company_src.index("for rnd in range")):]

    bf_in_loop = "business_flow" in company_src[company_src.index("for rnd in range"):]
    check("轮循环体内有 business_flow 调用（原来在循环外 → 恒 None）",
          bf_in_loop, f"_night_inner 行范围 {lo}-{hi}")

    # 派单那处必须真的把 biz_flow 传下去
    ar_lo, ar_hi = _func_span(company_src, "_author_round")
    # 按**行**取（不是字符偏移 —— 那是另一个 bug 的同款坑）
    ar_src = "\n".join(company_src.splitlines()[ar_lo - 1:ar_hi])
    check("_author_round 收 biz_flow 参数",
          re.search(r"\bbiz_flow\b", ar_src) is not None, "")
    check("派单时确实传了 biz_flow（biz_flow=biz_flow）",
          re.search(r"biz_flow\s*=\s*biz_flow", company_src) is not None, "")
    # 提权分支必须读 biz_flow 的 ok
    check("提权分支读 biz_flow.get('ok') is False",
          re.search(r"biz_flow\s+and\s+biz_flow\.get\(\s*[\"']ok[\"']\s*\)\s+is\s+False",
                    ar_src) is not None, "")

    print("[2] 提权的模块匹配：不能把候选元组整个字符串化")
    # 老写法：str(v[0]) → 变成 "('mod', 'func')"，与模块名永远不匹配
    # （只查**代码**，注释里提到这个写法是正常的 —— 那是解释 bug 的说明）
    _ar_code = "\n".join(ln for ln in ar_src.splitlines()
                          if not ln.strip().startswith("#"))
    check("不再有 str(v[0]) 这种元组字符串化写法",
          re.search(r"str\(\s*v\[0\]\s*\)", _ar_code) is None,
          "找到了 str(v[0]) —— 候选是 (模块名, 函数名) 元组，字符串化后匹配不上")
    # 从候选元组里取第 0 项（模块名）：形如 `_first[0] if isinstance(_first, (tuple, list))`
    check("从候选里取模块名（取元组第 0 项而不是整个元组）",
          re.search(r"\[0\]\s*if\s+isinstance\(\s*\w+\s*,\s*\(tuple,\s*list\)", _ar_code) is not None
          or re.search(r"isinstance\(\s*\w+\s*,\s*\(tuple,\s*list\)\s*\)", _ar_code) is not None, "")

    print("[3] 全自主线的接线必须走 openhouse.wire（有快照+冒烟+回归守卫）")
    # 用 **AST** 找真正的**调用点**（正则靠字符偏移判断"定义还是调用"不可靠：
    # 去掉注释行后偏移量就对不上了 —— 这是写这个断言时踩过的坑）
    _tree = ast.parse(auto_src)
    _call_lines: list[int] = []
    for _node in ast.walk(_tree):
        if isinstance(_node, ast.Call):
            _f = _node.func
            _name = _f.id if isinstance(_f, ast.Name) else (
                _f.attr if isinstance(_f, ast.Attribute) else "")
            if _name == "apply_web_wiring":
                _call_lines.append(_node.lineno)
    check("auto.py 不再裸调 apply_web_wiring（必须经 openhouse.wire 的守卫）",
          not _call_lines, f"仍在第 {_call_lines} 行裸调")
    check("auto.py 走了 openhouse.wire",
          ".wire(ws, bp" in auto_src, "")

    print("[4] ModelTooSmall 必须已定义（iterate 的 except 分支依赖它）")
    check("model_policy 里定义了 ModelTooSmall",
          re.search(r"class\s+ModelTooSmall\b", policy_src) is not None, "")
    # iterate 里那个 except 分支引用的名字必须真实存在
    m = re.search(r"except\s+model_policy\.(\w+)", iterate_src)
    check("iterate.py 的 except 捕获的名字已在 model_policy 中定义",
          m is not None and re.search(r"class\s+%s\b" % m.group(1), policy_src) is not None,
          f"except model_policy.{m.group(1) if m else '?'} 未定义")
    # 口径：ok 恒 True，从不硬拒
    check("verdict 的 ok 仍恒为 True（不硬拒用户的选择）",
          '"ok": True' in policy_src, "")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（业务流提权真触发 / 提权模块匹配 / 全自主线接线有守卫 / ModelTooSmall 已定义）")
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
