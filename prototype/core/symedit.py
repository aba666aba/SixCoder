"""符号级编辑：模型改符号，文件由 libcst 确定性渲染。

为什么需要它（对应 H2 假设：只做补丁 > 生成整文件）：
  3B 重写整文件时会顺手丢掉 import、改掉注释、破坏其它函数 —— 这些错误与"想得对不对"无关，
  纯粹是"抄写"工作量造成的。把渲染交给确定性渲染器，模型只需要产出它真正该产出的那一个函数体。

降级策略：libcst 不可用、解析失败、定位失败、渲染后语法不过 —— 任何一步失败都返回 (None, 原因)，
调用方立刻回退到原来的文本 patch 路径，绝不把坏结果写盘。
"""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field

try:                                    # libcst 是可选依赖：没有就整体降级
    import libcst as cst
    _HAS_CST = True
except Exception:                       # noqa: BLE001
    cst = None                          # type: ignore[assignment]
    _HAS_CST = False


def available() -> bool:
    return _HAS_CST


# ------------------------------------------------------------------ 结构
@dataclass
class Sym:
    qualname: str
    name: str
    kind: str           # function | class
    lineno: int
    endline: int
    params: str
    returns: str
    doc: str

    def signature(self) -> str:
        ret = f" -> {self.returns}" if self.returns else ""
        return f"{self.kind} {self.qualname}({self.params}){ret}"


@dataclass
class EditReport:
    ok: bool
    new_source: str = ""
    applied: list[str] = field(default_factory=list)
    reason: str = ""


# ------------------------------------------------------------------ 抽取（ast，快）
def outline(source: str) -> list[Sym]:
    """当前文件的符号清单（给模型看的上下文，不是全文）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[Sym] = []

    def walk(stmts, prefix: str) -> None:
        for st in stmts:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append(Sym(f"{prefix}{st.name}", st.name, "function", st.lineno,
                               getattr(st, "end_lineno", st.lineno) or st.lineno,
                               _params(st), _ann(st.returns),
                               (ast.get_docstring(st) or "").split("\n")[0][:80]))
                walk(_blocks(st), "")
            elif isinstance(st, ast.ClassDef):
                out.append(Sym(f"{prefix}{st.name}", st.name, "class", st.lineno,
                               getattr(st, "end_lineno", st.lineno) or st.lineno,
                               "", "", (ast.get_docstring(st) or "").split("\n")[0][:80]))
                walk(_blocks(st), f"{prefix}{st.name}.")
            elif isinstance(st, ast.stmt):
                walk(_blocks(st), prefix)

    walk(ast.parse(source).body, "")
    return out


def _blocks(node) -> list:
    stmts = list(getattr(node, "body", []) or [])
    stmts += list(getattr(node, "orelse", []) or [])
    stmts += list(getattr(node, "finalbody", []) or [])
    for h in getattr(node, "handlers", []) or []:
        stmts += list(getattr(h, "body", []) or [])
    return [s for s in stmts if isinstance(s, ast.stmt)]


def _params(fn) -> str:
    try:
        a = fn.args
        parts = [x.arg for x in list(getattr(a, "posonlyargs", [])) + list(a.args)]
        if a.vararg:
            parts.append("*" + a.vararg.arg)
        parts += [x.arg for x in a.kwonlyargs]
        if a.kwarg:
            parts.append("**" + a.kwarg.arg)
        return ", ".join(parts)
    except Exception:                                # noqa: BLE001
        return ""


def _ann(node) -> str:
    if node is None:
        return ""
    try:
        return ast.unparse(node)[:40]
    except Exception:                                # noqa: BLE001
        return ""


def source_of(source: str, qualname: str) -> str:
    """取出某个符号的源码文本（给模型当"现状"上下文）。"""
    try:
        lines = source.splitlines()
        for s in outline(source):
            if s.qualname == qualname:
                return "\n".join(lines[s.lineno - 1:s.endline])
    except Exception:                                # noqa: BLE001
        pass
    return ""


# ------------------------------------------------------------------ 渲染（libcst）
def _blk(node) -> list:
    if node is None:
        return []
    if isinstance(node, cst.IndentedBlock):
        return list(node.body)
    if isinstance(node, cst.SimpleStatementSuite):
        return [cst.SimpleStatementLine(body=node.body)]
    if isinstance(node, cst.Else):
        return _blk(node.body)
    return [node]


def _container(st) -> list:
    if isinstance(st, cst.If):
        return _blk(st.body) + _blk(st.orelse)
    if isinstance(st, cst.Try):
        out = _blk(st.body) + _blk(st.orelse) + _blk(st.finalbody)
        for h in st.handlers:
            out += _blk(h.body)
        return out
    if isinstance(st, (cst.With, cst.For, cst.While)):
        return _blk(st.body) + _blk(getattr(st, "orelse", None))
    return []


def _iter_defs(stmts, prefix: str):
    for st in stmts:
        if isinstance(st, cst.FunctionDef):
            yield prefix + st.name.value, st
            yield from _iter_defs(_blk(st.body), "")
        elif isinstance(st, cst.ClassDef):
            yield prefix + st.name.value, st
            yield from _iter_defs(_blk(st.body), prefix + st.name.value + ".")
        else:
            yield from _iter_defs(_container(st), prefix)


def find_def(tree, qualname: str):
    for q, node in _iter_defs(tree.body, ""):
        if q == qualname:
            return node
    return None


class _Swap(cst.CSTTransformer):
    def __init__(self, target, new) -> None:
        self.target = target
        self.new = new

    def on_leave(self, original_node, updated_node):
        return self.new if original_node is self.target else updated_node


class _Insert(cst.CSTTransformer):
    def __init__(self, node, after: str | None, at_front: bool = False) -> None:
        self.node = node
        self.after = after
        self.at_front = at_front
        self.hit = False

    def leave_Module(self, orig, updated):
        body = list(updated.body)
        if self.at_front:
            # __future__ 导入必须紧跟模块 docstring 之后
            idx = 0
            if body and isinstance(body[0], cst.SimpleStatementLine) and body[0].body:
                head = body[0].body[0]
                if isinstance(head, cst.Expr) and isinstance(
                        getattr(head, "value", None), (cst.SimpleString, cst.ConcatenatedString)):
                    idx = 1
            body.insert(idx, self.node)
            self.hit = True
            return updated.with_changes(body=body)
        idx = len(body)
        if self.after:
            for i, st in enumerate(body):
                if isinstance(st, (cst.FunctionDef, cst.ClassDef)) and st.name.value == self.after:
                    idx = i + 1
                    break
        body.insert(idx, self.node)
        self.hit = True
        return updated.with_changes(body=body)


def apply_edits(source: str, edits: list[dict]) -> EditReport:
    """按符号应用编辑，返回确定性渲染后的新源码。

    edits 每项：{"op": "replace"|"add", "symbol": "Cls.method" 或 "add" 时为新函数名,
               "code": "def ...\\n    ...", "after": "仅在 add 时给锚点符号名"}
    """
    if not _HAS_CST:
        return EditReport(False, reason="libcst 不可用")
    if not edits:
        return EditReport(False, reason="没有编辑")
    try:
        tree = cst.parse_module(source)
    except Exception as exc:                          # noqa: BLE001
        return EditReport(False, reason=f"原文件无法解析: {exc.__class__.__name__}")

    applied: list[str] = []
    for edit in edits:
        op = (edit.get("op") or "replace").lower()
        code = (edit.get("code") or "").strip("\n")
        symbol = (edit.get("symbol") or "").strip()
        if not code or not symbol:
            return EditReport(False, reason="编辑缺少 symbol 或 code")
        try:
            # 类方法带缩进，必须先 dedent 才能按模块解析；缩进由 libcst 按嵌套层重新生成
            parsed = cst.parse_module(textwrap.dedent(code).strip("\n") + "\n")
        except Exception as exc:                      # noqa: BLE001
            return EditReport(False, reason=f"{symbol}: 生成代码无法解析 ({exc.__class__.__name__})")
        new_node = next((s for s in parsed.body
                         if isinstance(s, (cst.FunctionDef, cst.ClassDef))), None)
        stmt_node = None
        if new_node is None:
            # 允许"加一条模块级语句"（import / from … import / 常量赋值）：
            # 远端常需要补 `from __future__ import annotations` 之类，属于合法符号级编辑
            stmt_node = next((s for s in parsed.body
                              if isinstance(s, (cst.SimpleStatementLine, cst.Import,
                                                cst.ImportFrom))), None)
            if stmt_node is None:
                return EditReport(False, reason=f"{symbol}: code 里没有函数/类/导入语句")

        if op == "add":
            anchor = edit.get("after") or ""
            if stmt_node is not None:
                is_future = "from __future__ import" in code
                lead = () if is_future else (cst.EmptyLine(),)
                node = stmt_node.with_changes(leading_lines=lead)
                tree = tree.visit(_Insert(node, anchor, at_front=is_future))
            else:
                node = new_node.with_changes(leading_lines=(cst.EmptyLine(), cst.EmptyLine()))
                tree = tree.visit(_Insert(node, anchor))
            applied.append(f"add {symbol}")
        else:
            if new_node is None:
                return EditReport(False, reason=f"{symbol}: replace 需要函数/类定义")
            target = find_def(tree, symbol)
            if target is None:
                return EditReport(False, reason=f"定位失败: {symbol}")
            keep = target.leading_lines if hasattr(target, "leading_lines") else ()
            tree = tree.visit(_Swap(target, new_node.with_changes(leading_lines=keep)))
            applied.append(f"replace {symbol}")

    rendered = tree.code
    try:
        ast.parse(rendered)                           # 语法闸门（渲染后必须仍可解析）
    except SyntaxError as exc:
        return EditReport(False, reason=f"渲染结果语法错误: {exc.msg}")
    return EditReport(True, new_source=rendered, applied=applied)
