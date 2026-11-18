"""跨模块引用修补：新写的文件调了老模块里的函数，但没 import（或老模块根本没这个函数）。

为什么需要这一层（实测 2026-11-07）：夜班里 `matching_core/da.py` 写了
`run_batch(...)`，而 `run_batch` 明明就定义在 `app/services/batches.py` 里 ——
结构体检只报"本模块没有任何定义/导入"→ 判整文件回滚 → 模型下一轮再写一遍同样缺 import
的代码。**符号在项目里存不存在，是确定性事实**，不该靠模型猜、更不该因此回滚。

三种情况分开处理（口径必须诚实）：
  1. 符号在别的模块存在 → 引擎补 `from <mod> import <name>`（0 token，确定性）；
  2. 符号在项目里根本不存在 → 记一条**接口待办**（谁需要、在哪个文件、行号），
     交给重规划/下一片去补，绝不假装补上了；
  3. 有多个同名提供方 → 不猜，返回候选让调用方记待办（猜错会 import 到不相干的模块）。
"""
from __future__ import annotations

import ast
import re
import sys
import subprocess
from pathlib import Path

SKIP_DIRS = {".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "venv", ".venv", "node_modules", "tests", "test"}

_FINDING = re.compile(r"^(?P<rel>[^:]+):(?P<line>\d+)\s+用了\s+(?P<name>\w+)")


_STDLIB_FALLBACK = {"sqlite3", "threading", "json", "os", "re", "time", "datetime", "pathlib", "typing", "math", "random", "collections", "itertools", "functools", "hashlib", "uuid", "copy", "io"}


def findings(detail: str) -> list[dict]:
    """从结构体检的 detail 里取出 [(rel, line, name)]。"""
    out: list[dict] = []
    for part in str(detail or "").split("；"):
        m = _FINDING.match(part.strip())
        if m:
            out.append({"rel": m.group("rel"), "line": int(m.group("line")),
                        "name": m.group("name")})
    return out


def providers(ws, name: str) -> list[str]:
    """项目里有哪些模块**定义了**这个名字（顶层函数/类/常量）。返回相对路径列表。"""
    ws = Path(ws)
    hits: list[str] = []
    for p in ws.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    hits.append(p.relative_to(ws).as_posix())
                    break
            elif isinstance(node, ast.Assign):
                if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                    hits.append(p.relative_to(ws).as_posix())
                    break
    return sorted(hits)


def record_need(ws, rel: str, items: list[dict], *, reason: str = "") -> Path:
    """把"项目里根本没有这个符号"记成接口待办（追加写，人工与重规划都看得到）。"""
    import json
    import time
    p = Path(ws) / ".sixcoder" / "need_interfaces.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 "file": rel, "symbol": it.get("name"),
                                 "line": it.get("line"), "reason": reason[:120]},
                                ensure_ascii=False) + "\n")
    return p


def module_name(rel: str) -> str:
    """`app/services/batches.py` → `app.services.batches`；`pkg/__init__.py` → `pkg`。"""
    parts = [p for p in str(rel or "").replace("\\", "/").split("/") if p]
    if not parts:
        return ""
    last = parts[-1]
    if last == "__init__.py":
        parts = parts[:-1]
    elif last.endswith(".py"):
        parts[-1] = last[:-3]
    return ".".join(parts)


def restore_constants_from_history(ws, rel: str, names: list[str]) -> list[str]:
    """从 git 历史恢复被重写丢掉的**模块常量**（只恢复项目自己的旧内容，零发明）。

    实测 2026-11-08 11:50：作者重写 models.py 连丢 `_ALLOWED_ROLES`/
    `_REQUIRED_FIELDS`（用的地方还在）→ auth 链 NameError → 全部 register 探针红 →
    已完成能力被观测打回。git 历史里定义完好 —— 恢复它和 author_gone_restored
    同类：确定性、零 token。
    """
    ws = Path(ws)
    p = ws / rel
    if not p.is_file() or not names:
        return []
    try:
        src = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    restored: list[str] = []
    commits = subprocess.run(
        ["git", "log", "--all", "--format=%H", "-S", names[0], "--", rel],
        cwd=str(ws), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=60).stdout.split()
    for commit in commits[:8]:
        old = subprocess.run(["git", "show", f"{commit}:{rel}"], cwd=str(ws),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=60).stdout
        if not old:
            continue
        for name in names:
            if name in restored:
                continue
            if not re.match(rf"^[A-Z_][A-Z0-9_]*$", name):
                continue                                  # 只恢复常量命名形态
            if re.search(rf"^\s*{name}\s*=", src, re.M):
                continue                                  # 现文件已定义
            m = re.search(rf"^({name}\s*=\s*[^=\n][^\n]*(?:\n[^\n=][^\n]*)*?)(?=\n\n|\n(?:def |class |@)|\Z)",
                          old, re.M)
            if not m:
                continue
            snippet = m.group(1).rstrip()
            try:
                ast.parse(snippet)
            except SyntaxError:
                continue
            # 插到首个 def/class 之前（常量区）
            ins = re.search(r"^(def |class |@)", src, re.M)
            pos = ins.start() if ins else len(src)
            src = src[:pos] + snippet + "\n\n\n" + src[pos:]
            restored.append(name)
        if restored:
            break
    if restored:
        try:
            ast.parse(src)
        except SyntaxError:
            return []
        p.write_text(src, encoding="utf-8")
    return restored


def patch(ws, rel: str, detail: str) -> dict:
    """给 rel 补上"用了但没 import"的跨模块符号。

    返回 {added:[{name,module}], unknown:[{name,line}], ambiguous:[{name,candidates}], wrote:bool}。
    unknown 非空 = 项目里真没有这个符号，调用方必须记接口待办（不许当成功）。
    """
    ws = Path(ws)
    p = ws / rel
    added: list[dict] = []
    unknown: list[dict] = []
    ambiguous: list[dict] = []
    items = [f for f in findings(detail) if f["rel"] == rel]
    if not items or not p.is_file():
        return {"added": [], "unknown": items, "ambiguous": [], "wrote": False}
    src = p.read_text(encoding="utf-8-sig", errors="replace")
    lines = src.splitlines()
    inserts: list[str] = []
    for f in items:
        name = f["name"]
        if re.search(rf"^\s*(from\s+\S+\s+)?import\s+.*\b{re.escape(name)}\b", src, re.M):
            continue
        # 标准库模块：确定性补 import（零风险——模块必然存在）。实测 2026-11-08
        # 11:16：作者写线程安全连接连漏 4 次 `import sqlite3`，heal_stop 卡死；
        # 这类修复不该花远端一次调用。
        if name in getattr(sys, "stdlib_module_names", ()) or name in _STDLIB_FALLBACK:
            stmt = f"import {name}"
            if stmt not in inserts:
                inserts.append(stmt)
                added.append({"name": name, "module": "stdlib"})
            continue
        cands = [c for c in providers(ws, name) if c != rel]
        if not cands:
            unknown.append({"name": name, "line": f["line"]})
            continue
        if len(cands) > 1:
            # 同一子系统优先（同前缀的最可能是它要调的那个）
            pref = rel.rsplit("/", 1)[0] if "/" in rel else ""
            same = [c for c in cands if c.startswith(pref + "/")] if pref else []
            if len(same) != 1:
                ambiguous.append({"name": name, "candidates": cands[:4]})
                continue
            cands = same
        mod = module_name(cands[0])
        stmt = f"from {mod} import {name}"
        if stmt in inserts:
            continue
        inserts.append(stmt)
        added.append({"name": name, "module": cands[0]})
    if inserts:
        pos = 0
        for i, ln in enumerate(lines[:80]):
            t = ln.strip()
            if t.startswith("import ") or t.startswith("from "):
                pos = i + 1
        lines[pos:pos] = inserts
        new_src = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
        try:
            ast.parse(new_src)
        except SyntaxError:
            return {"added": [], "unknown": unknown, "ambiguous": ambiguous, "wrote": False}
        p.write_text(new_src, encoding="utf-8")
        return {"added": added, "unknown": unknown, "ambiguous": ambiguous, "wrote": True}
    return {"added": [], "unknown": unknown, "ambiguous": ambiguous, "wrote": False}
