"""内置 lint：不装 ruff/flake8 也能给出"违规 + 怎么改"。

为什么自己写而不是装 ruff：
    这套系统要能在**裸 Python** 的甲方机器上跑（README 里写明只要求 Ollama + Python）。
    多一个外部依赖就多一个装不上的失败点。这里只用 ast + 正则，覆盖夜班产物里
    实际出现过的那几类问题，并且**每条都带修复指令**（分级表 L 级要求"报错内嵌修复指令"）。

检查项（都是确定性、零误报空间）：
  bare_except        `except:` 裸捕获           → 指明捕获什么异常
  swallow            `except ...: pass` 吞异常  → 至少 log 或重新抛出
  mutable_default    可变默认参数               → 改成 None + 内部新建
  unused_import      导入未使用                 → 删掉
  star_import        `from x import *`          → 显式导入
  assert_runtime     运行时用 assert 做校验      → 改 if + raise
  print_debug        用 print 当日志             → 用 logging
  todo_marker        临时标记（与债务台账联动）   → 记台账并排期
  shadow_builtin     覆盖内建名                 → 换名
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".tmp"}

RULES: dict[str, str] = {
    "bare_except": "裸 `except:` 会连 KeyboardInterrupt/SystemExit 一起吞掉",
    "swallow": "`except ...: pass` 把错误静默掉，判据再也看不见",
    "mutable_default": "可变默认参数在多次调用间共享同一个对象",
    "unused_import": "导入后从未使用",
    "star_import": "星号导入让依赖不可追踪",
    "assert_runtime": "`assert` 在 -O 下会被整段移除，不能当运行时校验",
    "print_debug": "用 print 当日志，生产环境拿不到级别与去向",
    "todo_marker": "留下临时方案标记",
    "shadow_builtin": "覆盖了内建名字，后面的代码会拿到错的对象",
}

FIXES: dict[str, str] = {
    "bare_except": "改成 `except ValueError:`（写出你能处理的具体异常）",
    "swallow": "改成 `except X as exc: log.warning(...)` 或 `raise`（不许静默）",
    "mutable_default": "改成 `def f(x=None):` 再在函数体里 `if x is None: x = []`",
    "unused_import": "删掉这一行导入",
    "star_import": "改成 `from m import a, b`（显式列出用到的名字）",
    "assert_runtime": "改成 `if not cond: raise ValueError(...)`",
    "print_debug": "改成 `logging.getLogger(__name__).info(...)`",
    "todo_marker": "要么这轮就实现，要么写进临时方案台账并给出到期轮次",
    "shadow_builtin": "换一个名字（如 `list_` 或 `items_`）",
}

_BUILTINS = {"list", "dict", "set", "tuple", "str", "int", "float", "bool", "id", "type",
             "sum", "min", "max", "len", "input", "open", "filter", "map", "next", "all", "any"}
_MARK = re.compile(r"(TODO|FIXME|XXX|HACK|临时方案|先这样)", re.I)


def lint_source(src: str, rel: str = "<mem>") -> list[dict]:
    """对一段源码做 lint，返回 [{rule, line, text, why, fix}]。"""
    out: list[dict] = []

    def add(rule: str, line: int, text: str) -> None:
        out.append({"rule": rule, "file": rel, "line": line, "text": text[:120],
                    "why": RULES[rule], "fix": FIXES[rule]})

    try:
        tree = ast.parse(src or "")
    except SyntaxError as exc:
        out.append({"rule": "syntax", "file": rel, "line": int(exc.lineno or 0),
                    "text": str(exc.msg)[:100], "why": "语法错误", "fix": "先修语法，其余检查跳过"})
        return out

    lines = src.splitlines()

    # 导入与使用（unused_import / star_import）
    imported: dict[str, int] = {}
    used: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported[(a.asname or a.name).split(".")[0]] = n.lineno
        elif isinstance(n, ast.ImportFrom):
            if any(a.name == "*" for a in n.names):
                add("star_import", n.lineno, lines[n.lineno - 1] if n.lineno <= len(lines) else "")
                continue
            for a in n.names:
                imported[a.asname or a.name] = n.lineno
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            used.add(n.id)
        elif isinstance(n, ast.Attribute):
            cur = n
            while isinstance(cur, ast.Attribute):
                cur = cur.value
            if isinstance(cur, ast.Name):
                used.add(cur.id)
    for name, line in imported.items():
        if name not in used and name not in ("annotations",):
            add("unused_import", line, lines[line - 1] if line <= len(lines) else name)

    for n in ast.walk(tree):
        # 裸捕获 / 吞异常
        if isinstance(n, ast.ExceptHandler):
            if n.type is None:
                add("bare_except", n.lineno, lines[n.lineno - 1] if n.lineno <= len(lines) else "")
            if len(n.body) == 1 and isinstance(n.body[0], ast.Pass):
                add("swallow", n.lineno, lines[n.lineno - 1] if n.lineno <= len(lines) else "")
        # 可变默认参数
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in list(n.args.defaults) + [x for x in n.args.kw_defaults if x is not None]:
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    add("mutable_default", n.lineno,
                        lines[n.lineno - 1] if n.lineno <= len(lines) else n.name)
            if n.name in _BUILTINS:
                add("shadow_builtin", n.lineno, f"def {n.name}(...)")
        # 运行时 assert / print
        if isinstance(n, ast.Assert):
            add("assert_runtime", n.lineno, lines[n.lineno - 1] if n.lineno <= len(lines) else "")
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "print":
            add("print_debug", n.lineno, lines[n.lineno - 1] if n.lineno <= len(lines) else "")

    # 临时标记（按行扫，注释里的 ast 看不到）
    for i, line in enumerate(lines, 1):
        if _MARK.search(line) and not line.strip().startswith(("_MARK", "r\"", "'")):
            add("todo_marker", i, line.strip())
    return out


def lint_files(ws: Path, paths: list[str] | None = None) -> list[dict]:
    """对一个工作区的模块做 lint（默认只扫产物，不扫自检自身的风格差异）。"""
    ws = Path(ws)
    files: list[Path] = []
    if paths:
        files = [ws / p for p in paths]
    else:
        for p in ws.rglob("*.py"):
            if any(part in _SKIP_DIRS for part in p.relative_to(ws).parts):
                continue
            if p.name.startswith("test_") or p.name.endswith("_test.py"):
                continue
            files.append(p)
    out: list[dict] = []
    for p in files:
        if not p.exists():
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(p.relative_to(ws)).replace("\\", "/")
        out += lint_source(src, rel)
    return out


def summarize(findings: list[dict], limit: int = 3) -> str:
    """压成一句话（进交付报告/事件），**带修复指令**。"""
    if not findings:
        return "没有 lint 问题"
    by_rule: dict[str, list[dict]] = {}
    for f in findings:
        by_rule.setdefault(f["rule"], []).append(f)
    parts = []
    for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1]))[:limit]:
        one = items[0]
        parts.append(f"{rule}×{len(items)}（如 {one['file']}:{one['line']} → {one['fix']}）")
    return "；".join(parts)
