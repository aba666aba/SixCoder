"""已安装包的 API 签名索引：冷门 API 的"知识库"，但存的是机器事实而非散文。

为什么不走向量库/文档 RAG：
- 签名与可用名字是**可逐字校验的事实**，散文文档会被小模型复述成"看着像"的调用；
- 索引取自**即将执行这段代码的同一个解释器环境**，不存在版本漂移；
- 有了它，"幻觉 API" 从"运行时才炸"提前成"静态就能拒绝"，这是知识缺口里最便宜的一类错误。

数据来源全部是 AST（不 import、不执行第三方代码）：模块名 → 公开符号名 + 参数签名 + 首行 doc。
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

SKIP = {"site-packages", "__pycache__", "test", "tests", "idlelib", "lib2to3",
        "tkinter", "turtledemo", "ensurepip", "test_importlib"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS modules(module TEXT PRIMARY KEY, path TEXT, mtime REAL);
CREATE TABLE IF NOT EXISTS api(
  id INTEGER PRIMARY KEY AUTOINCREMENT, module TEXT, qualname TEXT, name TEXT,
  kind TEXT, params TEXT, doc TEXT);
CREATE INDEX IF NOT EXISTS ix_api_name ON api(name);
CREATE INDEX IF NOT EXISTS ix_api_mod ON api(module);
"""


@dataclass
class Finding:
    kind: str          # missing_name | missing_attr | unknown_module
    where: str
    detail: str
    suggestion: str = ""

    def __str__(self) -> str:
        s = f"[{self.kind}] {self.where}: {self.detail}"
        return f"{s}（是不是 {self.suggestion}?）" if self.suggestion else s


# ------------------------------------------------------------------ 建索引
def _sig(node) -> str:
    try:
        a = node.args
        parts = [x.arg for x in list(getattr(a, "posonlyargs", [])) + list(a.args)]
        if a.vararg:
            parts.append("*" + a.vararg.arg)
        parts += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            parts.append("**" + a.kwarg.arg)
        return ", ".join(parts)
    except Exception:                                 # noqa: BLE001
        return ""


def scan_file(path: str, root: str) -> list[tuple]:
    try:
        src = Path(path).read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    rel = os.path.relpath(path, root).replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = [p for p in rel.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    module = ".".join(parts)
    if not module:
        return []
    rows: list[tuple] = []
    for node in _top_level(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_"):
                continue
            rows.append((module, node.name, node.name, "function", _sig(node),
                         (ast.get_docstring(node) or "").split("\n")[0][:100]))
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            rows.append((module, node.name, node.name, "class", "",
                         (ast.get_docstring(node) or "").split("\n")[0][:100]))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and not sub.name.startswith("_"):
                    rows.append((module, f"{node.name}.{sub.name}", sub.name, "method",
                                 _sig(sub), (ast.get_docstring(sub) or "").split("\n")[0][:100]))
        elif isinstance(node, ast.ImportFrom):
            # 再导出：from _x import y as z —— z 在本模块是公开名字（hmac.compare_digest 就是这种）
            for al in node.names:
                public = al.asname or al.name
                if not public.startswith("_") and (al.asname or not al.name.startswith("_")):
                    rows.append((module, public, public, "reexport", "",
                                 f"from {node.module or '.'} import {al.name}"))
    return rows


def _top_level(tree) -> list:
    """模块级语句，含 try/if 分支里的（版本条件导入非常常见：hmac 就在 try 里导出）。"""
    out, stack = [], list(tree.body)
    while stack:
        node = stack.pop(0)
        if isinstance(node, (ast.If, ast.Try)):
            for attr in ("body", "orelse", "finalbody"):
                stack += list(getattr(node, attr, []) or [])
            for h in getattr(node, "handlers", []) or []:
                stack += list(getattr(h, "body", []) or [])
            continue
        out.append(node)
    return out


# C 扩展与内建模块：源码里没有定义，只能靠 import 后 dir() 取真相（只取核心一小撮，成本可控）
CORE_C_MODULES = ("math", "cmath", "itertools", "functools", "operator", "collections",
                  "time", "struct", "array", "bisect", "heapq", "unicodedata", "statistics",
                  "random", "re", "hashlib", "hmac", "sqlite3", "json", "datetime",
                  "pathlib", "logging", "argparse", "csv", "contextlib", "dataclasses",
                  "typing", "subprocess", "shutil", "tempfile", "textwrap", "uuid")


@lru_cache(maxsize=1)
def _runtime_rows() -> dict[str, list[tuple]]:
    """name → [(module, qualname, kind, params, doc)]，来自运行期 dir() + inspect.signature。"""
    import importlib
    import inspect
    table: dict[str, list[tuple]] = {}
    for name in CORE_C_MODULES:
        try:
            mod = importlib.import_module(name)
        except Exception:                             # noqa: BLE001
            continue
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            try:
                obj = getattr(mod, attr)
            except Exception:                         # noqa: BLE001
                continue
            if not (inspect.isfunction(obj) or inspect.isclass(obj) or callable(obj)):
                continue
            sig, doc = "", ""
            try:
                sig = str(inspect.signature(obj)).strip("()")[:200]
            except Exception:                         # noqa: BLE001
                pass
            doc = (inspect.getdoc(obj) or "").split("\n")[0][:100]
            kind = ("class" if inspect.isclass(obj)
                    else "function" if inspect.isfunction(obj) else "callable")
            table.setdefault(attr, []).append((name, attr, kind, sig, doc))
    return table


@lru_cache(maxsize=512)
def runtime_names(module: str) -> frozenset[str] | None:
    """运行期真相：C 扩展、模块级动态赋值（如 hashlib.sha256 = ...）只有 import 后才可见。

    只在"静态索引说没有"时兜底调用，且只针对这段代码确实要用的模块（测试本来也会导入它）。
    """
    import importlib
    try:
        mod = importlib.import_module(module)
    except Exception:                                 # noqa: BLE001
        return None
    try:
        return frozenset(dir(mod))
    except Exception:                                 # noqa: BLE001
        return None


def _module_of(path: str, root: str) -> str:
    rel = os.path.relpath(path, root).replace("\\", "/")
    if rel.endswith(".py"):
        rel = rel[:-3]
    parts = [p for p in rel.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _walk(roots: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for root in roots:
        rp = Path(root)
        if rp.is_file():
            out.append((str(rp), str(rp.parent)))
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP and not d.startswith(".")]
            for fn in filenames:
                if fn.endswith(".py"):
                    out.append((os.path.join(dirpath, fn), root))
    return out


def build(roots: list[str], db_path: str | Path, limit: int = 0) -> dict:
    t0 = time.perf_counter()
    files = _walk(roots)
    if limit:
        files = files[:limit]
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA)
    known = {r[0]: r[1] for r in conn.execute("SELECT path, mtime FROM modules")}
    n_api = n_skip = 0
    for path, root in files:
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        if abs(known.get(path, -1) - mtime) < 1e-6:
            n_skip += 1
            continue
        rows = scan_file(path, root)
        conn.execute("DELETE FROM api WHERE module IN (SELECT module FROM modules WHERE path=?)",
                     (path,))
        conn.execute("DELETE FROM modules WHERE path=?", (path,))
        module = rows[0][0] if rows else _module_of(path, root)
        if module:
            # 即使没有公开符号也记一行：否则无符号文件每次重建都会被重新扫描
            conn.execute("INSERT OR REPLACE INTO modules(module, path, mtime) VALUES(?,?,?)",
                         (module, path, mtime))
        if rows:
            conn.executemany("INSERT INTO api(module, qualname, name, kind, params, doc)"
                             " VALUES(?,?,?,?,?,?)", rows)
            n_api += len(rows)
    conn.commit()
    total = conn.execute("SELECT count(*) FROM api").fetchone()[0]
    mods = conn.execute("SELECT count(*) FROM modules").fetchone()[0]
    conn.close()
    return {"files": len(files), "skipped": n_skip, "new_api": n_api, "api_total": total,
            "modules": mods, "seconds": round(time.perf_counter() - t0, 1)}


# ------------------------------------------------------------------ 查询
class ApiKB:
    def __init__(self, db_path: str | Path) -> None:
        self.db = str(db_path)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db)

    def available(self) -> bool:
        return Path(self.db).exists()

    def knows(self, module: str, name: str) -> str:
        """yes / no / unknown —— 只有"确定不存在"才允许报警，避免误报。

        规则：静态索引命中 → yes；否则只对"源码索引看不到的核心 C 模块"做运行期兜底，
        其余未命中一律 unknown（第三方 .py 模块的动态名字太多，宁可漏报也不误杀）。
        """
        if name in self.names_of(module):
            return "yes"
        if module not in CORE_C_MODULES and self._has_source(module):
            return "unknown"
        rt = runtime_names(module)
        if rt is None:
            return "unknown"
        return "yes" if name in rt else "no"

    def _has_source(self, module: str) -> bool:
        conn = self._conn()
        try:
            return conn.execute("SELECT 1 FROM modules WHERE module=? LIMIT 1",
                                (module,)).fetchone() is not None
        finally:
            conn.close()

    def lookup(self, name: str, limit: int = 8) -> list[tuple]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT module, qualname, kind, params, doc FROM api WHERE name=?"
                " ORDER BY length(module) LIMIT ?", (name, limit)).fetchall()
            if rows:
                return rows
            cands = [c[0] for c in conn.execute(
                "SELECT DISTINCT name FROM api WHERE kind IN ('function','class','reexport')")]
            for alt in difflib.get_close_matches(name, cands, n=3, cutoff=0.85):
                rows += conn.execute(
                    "SELECT module, qualname, kind, params, doc FROM api WHERE name=?"
                    " AND kind IN ('function','class','reexport')"
                    " ORDER BY length(module) LIMIT ?", (alt, limit)).fetchall()
        finally:
            conn.close()
        if rows:
            return rows
        # 源码索引看不到的（C 扩展 / 内建）：用运行期真相兜底
        return list(_runtime_rows().get(name, []))[:limit]

    def names_of(self, module: str) -> set[str]:
        conn = self._conn()
        try:
            rows = conn.execute("SELECT name FROM api WHERE module=?", (module,)).fetchall()
            subs = conn.execute("SELECT module FROM modules WHERE module LIKE ?",
                                (module + ".%",)).fetchall()
            return {r[0] for r in rows} | {s[0] .split(".")[-1] for s in subs}
        finally:
            conn.close()

    def has_module(self, module: str) -> bool:
        conn = self._conn()
        try:
            return conn.execute("SELECT 1 FROM modules WHERE module=? LIMIT 1",
                                (module,)).fetchone() is not None
        finally:
            conn.close()

    def block_for(self, text: str, limit: int = 12) -> str:
        """从任务描述里认出"像 API 的名字/模块名"，回填真实签名（给模型当事实）。"""
        import re
        names = [w for w in re.findall(r"[A-Za-z_][\w.]{2,}", text or "")]
        seen, lines = set(), []
        # 第一遍：需求里点名写的 module.member（如 hmac.compare_digest）优先精确回填
        for raw in names:
            if "." not in raw:
                continue
            mod, _, member = raw.rpartition(".")
            if not self.has_module(mod) or f"{mod}.{member}" in seen:
                continue
            hit = self._member(mod, member)
            if not hit:
                continue
            seen.add(f"{mod}.{member}")
            m, n, k, p = hit
            lines.append(f"- from {m} import {n}" + (f"   # {k} {n}({p})" if k != "class"
                                                     else f"   # class {n}"))
            if len(lines) >= limit:
                return "\n".join(lines[:limit])
        for raw in names:
            root = raw.split(".")[0]
            tail = raw.split(".")[-1]
            # 1) 命中模块名：把该模块的公开函数/类签名摊开（最有用的一类事实）
            if root not in seen and self.has_module(root):
                seen.add(root)
                rows = self._module_members(root, limit=6)
                if rows:
                    lines.append(f"# {root} 的公开 API（真实签名）")
                    lines += [f"- from {m} import {n}   # {k} {n}({p})"
                              for m, n, k, p in rows]
                    if len(lines) >= limit:
                        break
                    continue
            if tail in seen or len(tail) < 3:
                continue
            rows = self.lookup(tail, limit=2)
            if not rows:
                continue
            # 只回填"任务里确实提到的那个模块"的签名，否则会把 date/category 这类普通词
            # 误当成 API（实测回填出 nntplib.date、unicodedata.category 这种噪音）
            mentioned = [r for r in rows if re.search(rf"\b{re.escape(r[0])}\b", text or "")]
            rows = mentioned or []
            if not rows:
                continue
            seen.add(tail)
            for module, qualname, kind, params, _doc in rows[:1]:
                shown = f"{kind} {qualname}({params})" if kind != "class" else f"class {qualname}"
                lines.append(f"- from {module} import {tail}   # {shown}")
            if len(lines) >= limit:
                break
        return "\n".join(lines[:limit * 2])

    def _member(self, module: str, member: str) -> tuple | None:
        """精确取某个模块的某个成员（源码索引优先，C 扩展走运行期）。"""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT module, name, kind, params FROM api WHERE module=? AND name=?"
                " ORDER BY id LIMIT 1", (module, member)).fetchone()
            if row:
                return row
        finally:
            conn.close()
        for name, entries in _runtime_rows().items():
            if name != member:
                continue
            for m, n, kind, params, _doc in entries:
                if m == module:
                    return (m, n, kind, params)
        return None

    def _module_members(self, module: str, limit: int = 6) -> list[tuple]:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT module, name, kind, params FROM api WHERE module=?"
                " AND kind IN ('function','class') ORDER BY id LIMIT ?",
                (module, limit)).fetchall()
            if rows:
                return rows
        finally:
            conn.close()
        # C 扩展模块没有源码行，走运行期
        out = []
        for name, entries in _runtime_rows().items():
            for m, n, kind, params, _doc in entries:
                if m == module and kind in ("function", "class"):
                    out.append((m, n, kind, params))
                    if len(out) >= limit:
                        return out
        return out


# ------------------------------------------------------------------ 幻觉 API 检查

def _bound_names(tree) -> set:
    """收集"已被绑定"的名字：导入、赋值、参数、except 目标、def/class。"""
    bound: set = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                bound.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                bound.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(n.name)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                a = n.args
                for arg in (list(getattr(a, "posonlyargs", [])) + list(a.args)
                            + list(a.kwonlyargs) + ([a.vararg] if a.vararg else [])
                            + ([a.kwarg] if a.kwarg else [])):
                    if arg is not None:
                        bound.add(arg.arg)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            bound.add(n.name)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            bound.update(n.names)
    return bound


def verify_undeclared_modules(source: str, kb) -> list:
    """抓"用了标准库模块却没 import"：只有该名字确实是可索引模块时才报，避免误伤。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    bound = _bound_names(tree)
    out, seen = [], set()
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)):
            continue
        name = n.value.id
        if name in bound or name in seen or not kb.has_module(name):
            continue
        seen.add(name)
        out.append(Finding("missing_import", name,
                           "用到了模块 %s 但没有 import（运行会 NameError）" % name,
                           "import " + name))
    return out


def verify_source(source: str, kb: ApiKB) -> list[Finding]:
    """静态检查：导入的名字、模块属性是否真的存在（把幻觉 API 变成可拒绝的错误）。

    只在"确定不存在"时报警：静态索引看不到的（C 扩展、动态赋值）用运行期 dir() 兜底，
    兜底也不确定就放过 —— 宁可漏报也不误报，否则闸门会把好代码判死。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[Finding] = []
    imports: dict[str, str] = {}          # 本地别名 → 真实模块名
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                imports[al.asname or al.name.split(".")[0]] = al.name
        elif isinstance(node, ast.ImportFrom):
            if not node.module or not kb.has_module(node.module):
                continue
            names = kb.names_of(node.module)
            for al in node.names:
                if al.name == "*" or al.name in names:
                    continue
                if kb.knows(node.module, al.name) != "no":
                    continue
                near = difflib.get_close_matches(al.name, sorted(names), n=1, cutoff=0.7)
                out.append(Finding("missing_name", f"{node.module}",
                                   f"该模块没有公开符号 `{al.name}`", near[0] if near else ""))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)):
            continue
        mod = imports.get(node.value.id)
        if not mod:
            continue
        # 纯 C 模块（math/itertools…）没有索引条目，但运行期 dir() 可判定，仍要抓幻觉
        if mod not in CORE_C_MODULES and not kb.has_module(mod):
            continue
        # 属性判定只对"没有源码可索引"的纯 C 模块做（math/itertools…）：
        # 有 .py 源码的模块里成员可能由运行期构造（datetime.strptime、hashlib.sha256），
        # 静态看不到就判死会误伤好代码（实测），这类漏报交给运行时测试兜底。
        if kb._has_source(mod):
            continue
        if kb.knows(mod, node.attr) != "no":
            continue
        names = kb.names_of(mod)
        near = difflib.get_close_matches(node.attr, sorted(names), n=1, cutoff=0.7)
        out.append(Finding("missing_attr", f"{mod}.{node.attr}",
                           f"模块 `{mod}` 没有属性 `{node.attr}`", near[0] if near else ""))
    out.extend(verify_undeclared_modules(source, kb))
    return out


def _imported_full(tree, alias: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for al in node.names:
                if (al.asname or al.name.split(".")[0]) == alias:
                    yield (al.name,)


def describe(findings: list[Finding]) -> str:
    if not findings:
        return "API 用法检查通过（无幻觉符号）"
    return "；".join(str(f) for f in findings[:4])


if __name__ == "__main__":
    import sys
    roots = sys.argv[1:] or [str(Path(os.__file__).parent)]
    print(build(roots, Path(__file__).resolve().parent.parent / "data" / "api_index.db"))
