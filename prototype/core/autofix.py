"""两个确定性自动修复（不用模型）：
1) 按契约重写函数参数列表（模型常把 upsert(conn, rows) 写成 upsert(conn)）
2) 补上"用了模块却没 import"的 import 语句（实测 datetime）

都走 libcst 保真渲染，只改参数行/插入 import，其它字节不动。
"""
import ast
import re
from pathlib import Path

import libcst as cst


def contract_sigs(contract: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"([A-Za-z_]\w{2,})\s*\(([^()]*)\)", contract or ""):
        name, raw = m.group(1), m.group(2)
        if name in ("if", "for", "while", "return", "print", "def", "assert", "len"):
            continue
        parts = []
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece or piece in ("self", "cls"):
                continue
            tok = re.split(r"[=:]", piece)[0].strip().lstrip("*")
            if re.match(r"^[A-Za-z_]\w*$", tok):
                parts.append(tok)
        if parts:
            out.setdefault(name, parts)
    return out


def spec_sigs(contract: str) -> dict[str, list[tuple[str, str]]]:
    """从契约文本里抽出"参数名 + 注解原文"，用于**补参数时连注解一起补**。

    为什么需要：`_FixParams` 原来只能沿用已有位置的注解，缺的那个参数补出来是裸名
    （`def upsert(conn, rows)`），随后又被注解率/类型检查挑毛病。
    实测动机：夜班两次抽到 `upsert() takes 1 positional argument` —— 契约里
    `upsert(conn, rows: list[dict]) -> int` 写得清清楚楚，缺的就是确定性的补参数。
    """
    out: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(r"([A-Za-z_]\w{2,})\s*\(([^()]*)\)", contract or ""):
        name, raw = m.group(1), m.group(2)
        if name in ("if", "for", "while", "return", "print", "def", "assert", "len"):
            continue
        pairs: list[tuple[str, str]] = []
        for piece in raw.split(","):
            piece = piece.strip()
            if not piece or piece in ("self", "cls"):
                continue
            head, _, tail = piece.partition(":")
            pname = head.split("=")[0].strip().lstrip("*")
            if not re.match(r"^[A-Za-z_]\w*$", pname):
                pairs = []
                break
            ann = tail.split("=")[0].strip() if tail else ""
            pairs.append((pname, ann))
        if pairs:
            out.setdefault(name, pairs)
    return out


class _FixParams(cst.CSTTransformer):
    def __init__(self, want: dict[str, list[str]], specs: dict[str, list[tuple[str, str]]] | None = None):
        self.want = want
        self.specs = specs or {}
        self.changed: list[str] = []

    def _ann_of(self, name: str, i: int, old):
        """优先用契约里的注解原文（缺参数时也能带上类型），否则沿用旧参数注解。"""
        spec = self.specs.get(name) or []
        if i < len(spec) and spec[i][1]:
            try:
                return cst.Annotation(cst.parse_expression(spec[i][1]))
            except Exception:                          # noqa: BLE001
                pass
        return old.annotation if old is not None else None

    def leave_FunctionDef(self, orig, updated):
        name = updated.name.value
        params = self.want.get(name)
        if not params:
            return updated
        cur = [p.name.value for p in updated.params.params]
        if cur == params:
            return updated
        new_params = []
        for i, pname in enumerate(params):
            old = updated.params.params[i] if i < len(updated.params.params) else None
            new_params.append(cst.Param(cst.Name(pname), annotation=self._ann_of(name, i, old),
                                        default=old.default if old is not None else None))
        self.changed.append(f"{name}: {cur} → {params}")
        return updated.with_changes(params=updated.params.with_changes(params=new_params))


class _AddImport(cst.CSTTransformer):
    """插入 `import mod`（幂等），用于"用了模块却没 import"（实测 datetime）。"""

    def __init__(self, mods: list[str]):
        self.mods = mods
        self.added: list[str] = []

    def leave_Module(self, orig, updated):
        if not self.mods:
            return updated
        body = list(updated.body)
        idx = 0
        if body and isinstance(body[0], cst.SimpleStatementLine) and body[0].body:
            head = body[0].body[0]
            if isinstance(head, cst.Expr) and isinstance(
                    getattr(head, "value", None), (cst.SimpleString, cst.ConcatenatedString)):
                idx = 1
        existing = updated.code
        lines = []
        for m in self.mods:
            if re.search(rf"^\s*(import\s+{re.escape(m)}\b|from\s+{re.escape(m)}\s+import\b)",
                         existing, re.M):
                continue
            lines.append(cst.SimpleStatementLine(body=[cst.Import([cst.ImportAlias(cst.Name(m))])]))
            self.added.append(m)
        if not lines:
            return updated
        return updated.with_changes(body=body[:idx] + lines + body[idx:])


class _AddFromImport(cst.CSTTransformer):
    """插入 `from mod import name`（幂等）。实测事故：注解写了 Union 却没 import typing,
    导入模块直接 NameError，引擎判据 import 阶段 0/3，白烧三轮。"""

    def __init__(self, pairs: list[tuple[str, str]]):
        self.pairs = pairs
        self.added: list[str] = []

    def leave_Module(self, orig, updated):
        if not self.pairs:
            return updated
        existing = updated.code
        lines = []
        body = list(updated.body)
        idx = 0
        if body and isinstance(body[0], cst.SimpleStatementLine) and body[0].body:
            head = body[0].body[0]
            if isinstance(head, cst.Expr) and isinstance(
                    getattr(head, "value", None), (cst.SimpleString, cst.ConcatenatedString)):
                idx = 1
        for mod, name in self.pairs:
            if re.search(rf"^\s*from\s+{re.escape(mod)}\s+import\b[^\n]*\b{re.escape(name)}\b",
                         existing, re.M) or re.search(rf"^\s*import\s+{re.escape(mod)}\b",
                                                      existing, re.M):
                continue
            lines.append(cst.parse_statement(f"from {mod} import {name}\n"))
            self.added.append(f"{mod}.{name}")
        if not lines:
            return updated
        return updated.with_changes(body=body[:idx] + lines + body[idx:])


KNOWN_IMPORTS: dict[str, str] = {
    "Union": "typing", "Optional": "typing", "Any": "typing", "List": "typing",
    "Dict": "typing", "Tuple": "typing", "Set": "typing", "FrozenSet": "typing",
    "Callable": "typing", "Iterable": "typing", "Iterator": "typing",
    "Sequence": "typing", "Mapping": "typing", "TypeVar": "typing",
    "Generic": "typing", "Literal": "typing", "Protocol": "typing",
    "dataclass": "dataclasses", "field": "dataclasses",
    "Path": "pathlib", "datetime": "datetime", "timedelta": "datetime",
    "defaultdict": "collections", "Counter": "collections", "deque": "collections",
    "Decimal": "decimal", "Fraction": "fractions", "Enum": "enum", "auto": "enum",
}


def missing_annotation_imports(src: str) -> list[str]:
    """注解里用了、但没 import 的名字 → 应插入的 import 语句（字符串列表）。

    实测事故两连：
      - 模型写 `a: Union[int, str]` 却没 `from typing import Union`；
      - 模型写 `def upsert(conn: sqlite3.Connection)` 却没 `import sqlite3`
        → **导入即 NameError**（注解在运行时求值），引擎判据 import 阶段 0/3，白烧三轮。
    解析顺序（确定性，不猜）：
      1. 名字本身是一个真实可导入的模块（find_spec 能查到）→ `import N`（如 sqlite3）；
      2. 名字在 KNOWN_IMPORTS 里 → `from <模块> import N`（如 Union ← typing）；
      3. 都不是 → 不动（宁可不补，也不凭空造 import）。
    """
    try:
        tree = ast.parse(src or "")
    except SyntaxError:
        return []
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    defined.add(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
    used: set[str] = set()
    for node in ast.walk(tree):
        ann = getattr(node, "annotation", None)
        if ann is None:
            continue
        for sub in ast.walk(ann):
            if isinstance(sub, ast.Name):
                used.add(sub.id)
            elif isinstance(sub, ast.Attribute):
                # `conn: sqlite3.Connection` 的根名字是 Attribute，不是 Name ——
                # 实测就漏在这里：模块级注解用了 sqlite3 却没导入，导入即 NameError。
                root = sub
                while isinstance(root, ast.Attribute):
                    root = root.value
                if isinstance(root, ast.Name):
                    used.add(root.id)
    lines: list[str] = []
    import importlib.util
    for name in sorted(used - defined):
        try:
            is_module = importlib.util.find_spec(name) is not None
        except Exception:                                # noqa: BLE001
            is_module = False
        if is_module:
            lines.append(f"import {name}")
        elif name in KNOWN_IMPORTS:
            lines.append(f"from {KNOWN_IMPORTS[name]} import {name}")
    return lines


def _module_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:                                    # noqa: BLE001
        return set()
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def ensure_test_imports(test_path: Path, impl_path: Path) -> list[str]:
    """测试文件用了实现模块的符号却没 import → 补上（幂等，且只补**真实存在**的符号）。

    实测事故：模型往 `test_calc.py` 追加了 `def test_test_calc_divide_1(): assert divide(4,2)==2`
    但没扩展 `from calc import add, sub` 这行 → 运行时 `NameError: name 'divide' is not defined`
    → 实现节点无论怎么改都红（错在测试文件的 import 上）。

    与"瞎补 import"的区别（那次加出 `from calc import a`）：这里只补实现模块**真实定义**的符号。
    返回实际补上的名字。
    """
    impl_symbols = _module_symbols(impl_path)
    if not impl_symbols:
        return []
    try:
        src = test_path.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src)
    except Exception:                                    # noqa: BLE001
        return []
    stem = impl_path.stem
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    defined.add(a.asname or a.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
    missing = sorted((used & impl_symbols) - defined)
    if not missing:
        return []
    # 合并进已有的 `from <impl> import ...`，没有就新插一行
    # 注意用 [ \w,]（不含 \n）：否则会跨行吞掉后面的空行，把格式搞乱
    pat = re.compile(rf"^([ \t]*)from[ \t]+{re.escape(stem)}[ \t]+import[ \t]+([ \w,]+)[ \t]*$",
                     re.M)
    m = pat.search(src)
    if m:
        existing = [x.strip() for x in m.group(2).split(",") if x.strip()]
        merged = existing + [n for n in missing if n not in existing]
        new_line = f"{m.group(1)}from {stem} import {', '.join(merged)}"
        src = src[:m.start()] + new_line + src[m.end():]
    else:
        insert_at = 0
        lines = src.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if line.strip().startswith(("import ", "from ")) and " import " in line or \
                    line.strip().startswith("import "):
                insert_at = i + 1
        lines.insert(insert_at, f"from {stem} import {', '.join(missing)}\n")
        src = "".join(lines)
    try:
        ast.parse(src)
    except SyntaxError:
        return []
    test_path.write_text(src, encoding="utf-8")
    return missing


# 有些模块"裸 import 没用"：代码里写 `datetime.strptime(...)` 时，需要的是
# `from datetime import datetime`（类），而不是 `import datetime`（模块）。
# 实测事故（night8）：autofix 给它补了 `import datetime`，于是
# `AttributeError: module 'datetime' has no attribute 'strptime'` —— 补了等于没补。
NEEDS_FROM_IMPORT: dict[str, str] = {
    "datetime": "datetime",
    "dataclasses": "dataclass",
    "pathlib": "Path",
}


def fix_file(path: Path, contract: str = "", missing_modules: list[str] | None = None) -> dict:
    """就地修参数与 import，返回 {'params': [...], 'imports': [...]}。"""
    src = path.read_text(encoding="utf-8-sig", errors="replace")
    try:
        tree = cst.parse_module(src)
    except Exception:
        return {"params": [], "imports": [], "error": "来源无法解析"}
    out = {"params": [], "imports": []}
    want = contract_sigs(contract)
    if want:
        tr = _FixParams(want, spec_sigs(contract))
        tree = tree.visit(tr)
        out["params"] = tr.changed
    if missing_modules:
        # 分成两类：需要 `from M import N` 的，和普通 `import M` 的
        from_mods = [m for m in missing_modules if m in NEEDS_FROM_IMPORT]
        plain = [m for m in missing_modules if m not in NEEDS_FROM_IMPORT]
        if plain:
            tr2 = _AddImport(plain)
            tree = tree.visit(tr2)
            out["imports"] = out["imports"] + tr2.added
        if from_mods:
            pairs = [(m, NEEDS_FROM_IMPORT[m]) for m in from_mods]
            tr2b = _AddFromImport(pairs)
            tree = tree.visit(tr2b)
            out["imports"] = out["imports"] + tr2b.added
    # 注解里用了但没 import 的名字（Union / sqlite3 / Path …）
    try:
        lines_ = missing_annotation_imports(tree.code)
        if lines_:
            plain_ann = [ln.split()[1] for ln in lines_ if ln.startswith("import ")]
            from_ann = [(ln.split()[1], ln.split()[-1]) for ln in lines_
                        if ln.startswith("from ")]
            if plain_ann:
                tr3 = _AddImport(plain_ann)
                tree = tree.visit(tr3)
                out["imports"] = out["imports"] + tr3.added
            if from_ann:
                tr3b = _AddFromImport(from_ann)
                tree = tree.visit(tr3b)
                out["imports"] = out["imports"] + tr3b.added
    except Exception:                                    # noqa: BLE001
        pass
    code = tree.code
    try:
        ast.parse(code)
    except SyntaxError:
        return {"params": [], "imports": [], "error": "修复后语法错误，已放弃"}
    if code != src:
        path.write_text(code, encoding="utf-8")
    return out
