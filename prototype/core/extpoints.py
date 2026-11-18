"""扩展点：先用接口声明"这里允许换实现"，再用**可替换性**验收。

为什么必须是"可替换性"而不是"有没有写接口"（开发文档 §8.3）：
  写一个 `class Base(ABC)` 很容易，但把它当扩展点用，要求**换一个实现、调用方一行不改**。
  所以判据是结构性的：每个实现都要提供协议要求的全部方法，且参数能对上。
  只声明不验收，就会变成"名义上可扩展、实际换一个就崩"。

数据来自 `.sixcoder/arch.json` 的 `extension_points`：
  {"module": "...", "protocol": "RuleExecutor",
   "impls": ["PythonRuleExecutor", "DroolsAdapter"], "invert_dependency": true}
"""
from __future__ import annotations

import ast
from pathlib import Path

SKIP_DIRS = {".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "venv", ".venv", "node_modules", "tests", "test"}


def _cls_methods(tree, name: str) -> dict[str, int] | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return {m.name: len([a for a in m.args.args if a.arg not in ("self", "cls")])
                    for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return None


def _func_params(tree, name: str) -> int | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return len(node.args.args)
    return None


def _find_symbol(ws: Path, symbol: str) -> tuple[str, dict[str, int]] | tuple[str, int] | None:
    """全仓找一个类/函数，返回 (相对路径, 方法表或参数个数)。"""
    for p in sorted(ws.rglob("*.py")):
        try:
            rel = p.relative_to(ws)
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        ms = _cls_methods(tree, symbol)
        if ms is not None:
            return rel.as_posix(), ms
        n = _func_params(tree, symbol)
        if n is not None:
            return rel.as_posix(), n
    return None


def verify(ws, ep: dict) -> dict:
    """一个扩展点是否成立：协议存在 + 每个实现都提供协议要求的接口。"""
    ws = Path(ws)
    problems: list[str] = []
    proto_name = str(ep.get("protocol") or "").strip()
    mod = str(ep.get("module") or "").strip()
    impls = [str(x) for x in (ep.get("impls") or []) if x]
    if not proto_name or not impls:
        return {"ok": False, "protocol": proto_name, "impls": impls,
                "problems": ["扩展点缺少 protocol 或 impls"]}

    src = None
    if mod and (ws / mod).is_file():
        try:
            src = ast.parse((ws / mod).read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            src = None
    proto_methods = _cls_methods(src, proto_name) if src is not None else None
    proto_arity = None
    if proto_methods is None:
        hit = _find_symbol(ws, proto_name)
        if isinstance(hit, tuple) and isinstance(hit[1], dict):
            proto_methods = hit[1]
            mod = hit[0]
        elif isinstance(hit, tuple):
            proto_arity = hit[1]
            mod = hit[0]
    if proto_methods is None and proto_arity is None:
        return {"ok": False, "protocol": proto_name, "impls": impls,
                "problems": [f"协议 {proto_name} 在仓库里找不到"]}

    detail: list[dict] = []
    for name in impls:
        hit = _find_symbol(ws, name)
        if hit is None:
            problems.append(f"实现 {name} 找不到")
            detail.append({"impl": name, "file": "", "ok": False, "missing": ["<未找到>"]})
            continue
        rel, sig = hit
        if proto_methods is not None:
            if not isinstance(sig, dict):
                problems.append(f"{name} 是函数，但协议 {proto_name} 是类")
                detail.append({"impl": name, "file": rel, "ok": False,
                               "missing": sorted(proto_methods)})
                continue
            missing = [m for m in proto_methods if m not in sig]
            mismatch = [m for m in proto_methods
                        if m in sig and sig[m] != proto_methods[m]]
            if missing:
                problems.append(f"{name} 缺方法 {', '.join(missing[:3])}")
            if mismatch:
                problems.append(f"{name} 参数对不上：{', '.join(mismatch[:3])}")
            detail.append({"impl": name, "file": rel, "ok": not missing and not mismatch,
                           "missing": missing, "mismatch": mismatch})
        else:
            if isinstance(sig, dict):
                problems.append(f"{name} 是类，但协议 {proto_name} 是函数")
                detail.append({"impl": name, "file": rel, "ok": False, "missing": ["<类型不符>"]})
                continue
            ok = int(sig) == int(proto_arity or 0)
            if not ok:
                problems.append(f"{name} 参数个数 {sig} ≠ 协议 {proto_arity}")
            detail.append({"impl": name, "file": rel, "ok": ok, "missing": []})

    return {"ok": not problems, "protocol": proto_name, "module": mod,
            "impls": impls, "detail": detail, "problems": problems}


def verify_all(ws, spec: dict) -> dict:
    eps = spec.get("extension_points") or []
    results = [verify(ws, ep) for ep in eps]
    return {"ok": all(r["ok"] for r in results), "count": len(results),
            "results": results,
            "problems": [p for r in results for p in r["problems"]]}


def report_lines(res: dict) -> list[str]:
    if not res or not res.get("count"):
        return ["", "### 扩展点（可替换性）", "", "- 没有声明扩展点（不是通过，也不是失败）"]
    lines = ["", "### 扩展点（可替换性）", "",
             f"- 声明 {res['count']} 个｜{'全部可替换' if res['ok'] else '**有不成立的**'}"]
    for r in res.get("results") or []:
        mark = "OK" if r["ok"] else "红"
        impls = "、".join(f"{d['impl']}({d['file'] or '缺失'})" for d in r.get("detail") or [])
        lines.append(f"- `{r['protocol']}`：{impls} —— {mark}")
        for p in (r.get("problems") or [])[:3]:
            lines.append(f"  - {p}")
    return lines
