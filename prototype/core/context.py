"""上下文管理：把"塞整个文件"换成"按预算裁剪 + 结构化大纲"。

实测依据：大文件（>150 行）整段注入会吃掉 3.5K 预算里的大半，模型反而看不到
真正要改的函数；而 Python 代码"签名 + 目标函数体"就够写出正确补丁。

优先级（从高到低）：
  1. 任务契约
  2. 目标函数/类（完整实现）
  3. 同文件其它符号的签名（outline）
  4. import 区（必须保留，否则补丁会丢依赖）
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS_PER_TOKEN = 3.2     # 代码的粗略换算


@dataclass
class View:
    content: str
    outline: str
    trimmed: bool
    chars: int

    @property
    def approx_tokens(self) -> int:
        return int(self.chars / MAX_CHARS_PER_TOKEN)


def outline(text: str, limit: int = 40) -> str:
    """抽出 def/class 签名（带行号），给模型"这个文件有什么"。"""
    rows = []
    for i, line in enumerate(text.splitlines(), 1):
        m = re.match(r"^(\s*)(def|class)\s+(\w+)", line)
        if m:
            rows.append(f"{i:>4}| {line.strip()}")
        if len(rows) >= limit:
            break
    return "\n".join(rows)


def _imports(text: str) -> str:
    lines = text.splitlines()
    keep = [l for l in lines[:60] if re.match(r"^\s*(import|from)\s", l)]
    return "\n".join(keep)


def _functions(text: str) -> dict[str, tuple[int, int]]:
    """函数名 → (起始行, 结束行)，0-based。"""
    import ast
    out: dict[str, tuple[int, int]] = {}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = (node.decorator_list[0].lineno - 1) if getattr(node, "decorator_list", None) else node.lineno - 1
            out[node.name] = (start, node.end_lineno)
    return out


def pick_targets(contract: str, names: list[str]) -> list[str]:
    """从契约里猜要改哪些函数（模型也会在补丁里给出，这里只是给上下文用）。"""
    hits = [n for n in names if n and n in contract]
    return hits[:3]


def build(rel: str, text: str, contract: str, budget_tokens: int = 1200) -> View:
    """按预算构造"目标文件视图"。"""
    if not text.strip():
        return View("（文件不存在，将新建）", "", False, 0)

    budget_chars = int(budget_tokens * MAX_CHARS_PER_TOKEN)
    if len(text) <= budget_chars:
        return View(text, "", False, len(text))

    fn = _functions(text)
    lines = text.splitlines()
    wanted = pick_targets(contract, list(fn.keys()))
    chunks: list[str] = [_imports(text)]
    used = len(chunks[0])

    for name in wanted:
        s, e = fn[name]
        seg = "\n".join(lines[s:e])
        if used + len(seg) > budget_chars:
            break
        chunks.append(f"# ---- {name}（完整实现，行 {s+1}-{e}）----\n{seg}")
        used += len(seg)

    ol = outline(text)
    chunks.append("# ---- 本文件其它符号（只有签名，实现已省略）----\n" + ol)

    content = "\n\n".join(c for c in chunks if c.strip())
    if len(content) > budget_chars + 400:          # 兜底再裁一次
        content = content[: budget_chars + 400] + "\n# …（已按预算截断）"
    return View(content, ol, True, len(content))
