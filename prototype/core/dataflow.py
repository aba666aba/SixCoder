"""数据流 / 参数来源链（骨架）：回答"这个参数的值从哪来、被改了几次"。

诚实边界：**这不是污点追踪**。系统现在能做的只有三件事：
    1) 参数来源链：谁调用了这个函数、传进去的实参是什么表达式、在第几行；
    2) 变换粗计：参数在函数体内被重新赋值/自增了几次（按名字统计，不做类型推断）；
    3) 缺证据就写缺证据：跨仓库、`getattr` 动态调用、反射 —— 一律记 no_evidence，不猜。

为什么只做到这里：真正的污点追踪要"值的来源标注 + 传播规则 + 净化点 + 汇点匹配"，
在没有中间表示（IR）和分析框架的前提下硬做，只会产出看起来很专业的假结论。
所以先把能核到代码事实的那部分做成机械证据，剩下的在交付报告里如实写"没有证据"。

用法：
    from . import dataflow
    res = dataflow.trace(ws, "clean.py", "clean_row")
    res = dataflow.graph(ws)            # 全工作区调用图统计
"""
from __future__ import annotations

import ast
from pathlib import Path

SKIP_DIRS = {".git", "__pycache__", ".sixcoder", ".mypy_cache", ".pytest_cache",
             "node_modules", ".tmp", "workspaces"}


def _py_files(ws: Path, rels: list[str] | None = None) -> list[Path]:
    ws = Path(ws)
    if rels:
        return [ws / r for r in rels if (ws / r).exists()]
    out: list[Path] = []
    for p in sorted(ws.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.relative_to(ws).parts):
            continue
        out.append(p)
    return out


def _parse(path: Path) -> ast.AST | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return None
    except OSError:
        return None


def _callee_name(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _call_sites(ws: Path, func: str,
                rels: list[str] | None = None) -> tuple[list[dict], int, int]:
    """找到所有调用 func 的位置。

    同时数两种动态调用（**都不算来源证据**，但含义不同）：
      · dynamic：`getattr(x, "func")` —— 名字写死在字符串里，能确认是它；
      · dynamic_unknown：`getattr(x, 变量)` —— 静态无法确认会不会命中，只能说"可能"。
    """
    ws = Path(ws)
    sites: list[dict] = []
    dynamic = 0
    dynamic_unknown = 0
    for path in _py_files(ws, rels):
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _callee_name(node)
            if name == "getattr":
                if len(node.args) >= 2:
                    second = node.args[1]
                    if isinstance(second, ast.Constant) and second.value == func:
                        dynamic += 1
                    elif not isinstance(second, ast.Constant):
                        dynamic_unknown += 1
                continue
            if name != func:
                continue
            try:
                args = [ast.unparse(a) for a in node.args]
            except Exception:                        # noqa: BLE001
                args = ["<不可解析>"] * len(node.args)
            sites.append({"file": path.relative_to(ws).as_posix(),
                          "line": getattr(node, "lineno", 0), "args": args})
    return sites, dynamic, dynamic_unknown


def _func_node(tree: ast.AST, func: str) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            return node
    return None


def _param_names(fn: ast.AST) -> list[str]:
    a = getattr(fn, "args", None)
    if a is None:
        return []
    names = [x.arg for x in list(getattr(a, "posonlyargs", []) or []) + list(a.args or [])]
    skip = {"self", "cls"}
    return [n for n in names if n not in skip]


def _transforms(fn: ast.AST, param: str) -> int:
    """参数在函数体内被重新赋值/自增了几次（粗粒度，只看名字）。"""
    n = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == param:
                    n += 1
        elif isinstance(node, ast.AugAssign):
            if isinstance(node.target, ast.Name) and node.target.id == param:
                n += 1
    return n


def trace(ws: Path, rel: str, func: str, rels: list[str] | None = None) -> dict:
    """某函数的参数来源链：{ok, module, func, params:[{name, callers, transforms, evidence}], notes}。"""
    ws = Path(ws)
    path = ws / rel
    tree = _parse(path) if path.exists() else None
    if tree is None:
        return {"ok": False, "module": rel, "func": func, "params": [],
                "notes": [f"模块不存在或语法错：{rel}"]}
    fn = _func_node(tree, func)
    if fn is None:
        return {"ok": False, "module": rel, "func": func, "params": [],
                "notes": [f"{rel} 里没有函数 {func}"]}
    sites, dynamic, dynamic_unknown = _call_sites(ws, func, rels)
    params = _param_names(fn)
    rows: list[dict] = []
    for idx, p in enumerate(params):
        callers = [{"file": s["file"], "line": s["line"], "expr": s["args"][idx]}
                   for s in sites if idx < len(s["args"])]
        if callers:
            evidence = "calls"
        elif dynamic:
            evidence = "dynamic_only"
        elif dynamic_unknown:
            evidence = "dynamic_maybe"
        else:
            evidence = "no_evidence"
        rows.append({"name": p, "callers": callers[:8], "caller_count": len(callers),
                     "transforms": _transforms(fn, p), "evidence": evidence})
    notes: list[str] = ["只做参数来源链，不是污点追踪（不跟踪值的传播与净化）"]
    if dynamic:
        notes.append(f"发现 {dynamic} 处 getattr 用字符串名调用该函数 —— 能确认是它，但不含实参证据")
    if dynamic_unknown:
        notes.append(f"发现 {dynamic_unknown} 处 getattr 用变量名调用 —— "
                     "静态无法确认会不会命中该函数，只能记'可能'")
    if not sites:
        notes.append("工作区里找不到调用点（跨仓库/动态调用/未接线）→ 该函数没有来源证据")
    return {"ok": any(r["evidence"] == "calls" for r in rows) if rows else False,
            "module": rel, "func": func, "params": rows, "notes": notes}


def graph(ws: Path, rels: list[str] | None = None) -> dict:
    """全工作区调用图统计：定义了多少函数、多少调用点、多少调用找不到定义（跨模块/外部/动态）。"""
    ws = Path(ws)
    files = _py_files(ws, rels)
    defined: set[str] = set()
    calls: list[dict] = []
    for path in files:
        tree = _parse(path)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Call):
                name = _callee_name(node)
                if name:
                    calls.append({"func": name, "file": path.relative_to(ws).as_posix(),
                                  "line": getattr(node, "lineno", 0)})
    unresolved = [c for c in calls if c["func"] not in defined]
    return {"ok": bool(calls), "files": len(files), "defined": len(defined),
            "calls": len(calls), "unresolved": len(unresolved),
            "unresolved_sample": unresolved[:6],
            "notes": ["unresolved = 本工作区没有同名定义：可能是标准库/第三方/动态调用，"
                      "这类**没有来源证据**，不要当成'流到了哪里'的结论"]}


def report_lines(res: dict) -> list[str]:
    if "params" in res:
        rows = res.get("params") or []
        have = [r for r in rows if r.get("evidence") == "calls"]
        miss = [r["name"] for r in rows if r.get("evidence") != "calls"]
        line = (f"- 参数来源链 `{res.get('func')}`：{len(rows)} 个参数，"
                f"{len(have)} 个能核到调用点")
        if miss:
            line += f"，{len(miss)} 个没有证据（{('、'.join(miss))[:60]}）"
        return [line + "；**只做来源链，不是污点追踪**"]
    return [f"- 调用图：{res.get('files')} 个文件、{res.get('defined')} 个定义、"
            f"{res.get('calls')} 个调用点，其中 {res.get('unresolved')} 个找不到定义"
            "（标准库/第三方/动态调用，属无证据，不作结论）"]
