"""契约反推：从**现有代码**反推结构化契约 —— 存量仓库的入场券（开发文档 §15.2 第 4 条）。

在此之前，产生契约的入口只有两个：contract.parse_requirement(需求文本) 与
contractgen.synthesize(需求)。**没有任何"读现有代码产出契约"的路径** —— 于是对存量
仓库（别人的、或三个月前自己的）只能跑机制，不能让夜班改代码：契约合成器只会
"按需求造新函数"，对着已有 300 个函数的仓库，它要么重写一遍、要么直接拒绝。

本模块把"读代码"变成与 parse_requirement **同构**的入口：

    现有代码 --ast--> Contract(path, module, funcs[FuncSig]) --直接可用-->
        闸门(gates) / 骨架(skeleton) / 判据(props) / 体检(iterate.inspect)

只提取代码里**真实写出**的事实，一个字都不编：
    · 签名（名字 / 参数 / 类型注解 / 默认值）            —— ast
    · 返回类型注解                                        —— ast
    · 异常（函数体内真实 raise 的异常类名）                —— ast
    · 返回键（return 的 dict 字面量的字符串键；            —— ast + 文本
              docstring 里"返回 {…}"写出的键）
    · 示例（docstring 的 doctest：`>>> f(args)` 换行 期望值）—— 文本
    · 类归属（方法所在类 → owner_class）                   —— ast

诚实边界（这是事实陈述，不是免责套话）：
    · 没有 doctest / 注解的函数，反推出的契约只有名字和参数 —— 判据薄
      （examples/keys 为空）。代码里没写的东西不能编。
    · 不解析 import 依赖、不追跨文件调用 —— 那是 arch.py / dataflow.py 的职责，
      契约只管"这个模块对外承诺什么"。
    · 语法错误文件如实进 issues，不 crash（读仓库不该被一个坏文件卡死）。

用法：
    from core import contract_reverse
    contracts = contract_reverse.from_code(Path(repo))       # {相对路径: Contract}
    text = contract_reverse.render(contracts)                # 人读的一页纸
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from .contract import (Contract, Example, FuncSig, Param, KEY_RE, _split_args)

# 忽略的目录（运行期产物 / 依赖 / 前端），与夜班工作区口径一致
SKIP_DIRS = {".sixcoder", ".git", "__pycache__", "node_modules", ".venv", "venv",
             ".tmp", "dist", "build", ".pytest_cache", ".mypy_cache"}
# 这些文件不进契约面（测试 / 打包 / 配置），与 gates.gate_import_ok 的口径一致
NON_CONTRACT_FILES = ("conftest.py", "setup.py",)

# doctest：`>>> f(args)` 后紧跟的输出行（期望值）
_DOCT_RE = re.compile(r"^\s*>>>\s*([A-Za-z_]\w{0,40})\s*\((.*)\)\s*$")


def _ann(node) -> str:
    """注解 → 文本；解析不了给空串（宁可薄，不许编）。"""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:                                     # noqa: BLE001
        return ""


def _default_str(node) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:                                     # noqa: BLE001
        return None


def _first_doc(fn) -> str:
    """docstring 的第一段（到第一个空行）；整段给判据用，不是只给第一行——
    "返回 {…}" 这类关键句常写在第二段之后。"""
    doc = ast.get_docstring(fn) or ""
    if not doc:
        return ""
    parts = re.split(r"\n\s*\n", doc.strip())
    return parts[0].strip()


def _doc_return_keys(doc: str) -> list[str]:
    """docstring 里"返回 {id, name}" 写出的键（与 contract._rederive_keys 同口径）。"""
    if "返回" not in doc:
        return []
    tail = doc.split("返回", 1)[1]
    return [m.group(1) for m in KEY_RE.finditer(tail)]


def _raises(fn) -> list[str]:
    """函数体内真实 raise 的异常类名（含 raise X(...)）。"""
    out: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Raise) and node.exc is not None:
            target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr
            else:
                continue
            if name[:1].isupper() and name not in out:
                out.append(name)
    return out


def _return_keys(fn) -> list[str]:
    """`return {"id": ..., "ok": ...}` 的字符串键（第一层，保持书写顺序）。

    只认**字面量键**：`return {k: v}` 的键是变量，猜它是什么就是编造。
    多个 return 语句取**并集**（都是这个函数对外承诺的形状）。
    """
    keys: list[str] = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k in node.value.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    if k.value not in keys:
                        keys.append(k.value)
    return keys


def _doctests(func_name: str, doc: str) -> list[Example]:
    """docstring 里的 doctest → Example（期望值 = 输出行的字面量拼接）。

    口径：`>>> f(1, 2)` 的下一行（非 `>>>` 开头）就是期望输出。多行输出用
    换行拼接（与真 doctest 语义一致）。没有输出行就不算示例（判据无从判起）。
    """
    out: list[Example] = []
    lines = (doc or "").splitlines()
    i = 0
    while i < len(lines):
        m = _DOCT_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name, args_text = m.group(1), m.group(2)
        i += 1
        expects: list[str] = []
        while i < len(lines) and not _DOCT_RE.match(lines[i]):
            t = lines[i]
            if t.strip().startswith(">>>"):
                break
            if t.strip():
                expects.append(t)
            i += 1
        if not expects:
            continue
        expect = "\n".join(expects)
        # 期望值必须是能过 literal_eval 的字面量，否则判据渲染不出来（宁可不收）
        try:
            ast.literal_eval(expect)                     # noqa: B018
        except (ValueError, SyntaxError):
            # 字符串输出（如 'ok'）literal_eval 认；裸词（如 ok）不认 —— 不算示例
            if not (expect.startswith(("'", '"')) and expect.endswith(("'", '"'))):
                continue
        ex = Example(func=name, args=_split_args(args_text), expect=expect.strip())
        if name == func_name and not any(e.args == ex.args and e.expect == ex.expect
                                         for e in out):
            out.append(ex)
    return out


def _sig_of(fn, *, owner_class: str = "") -> FuncSig:
    """ast 函数定义 → FuncSig（不带示例；示例由调用方按 doc 归属）。"""
    params: list[Param] = []
    all_args = list(getattr(fn.args, "posonlyargs", [])) + list(fn.args.args)
    for a in all_args:
        if a.arg in ("self", "cls"):
            continue
        params.append(Param(name=a.arg, type=_ann(a.annotation) or "object"))
    # 位置参数的默认值"从右往左"贴（ast 语义如此：defaults 对齐末尾）；
    # self/cls 不占默认值槽 —— 上面已经把它们剔掉了，所以这里直接按末尾对齐就是对的。
    defaults = list(fn.args.defaults)
    di = len(params) - len(defaults)
    for slot, d in enumerate(defaults):
        if di + slot < len(params):
            params[di + slot].default = _default_str(d)
    kws = list(fn.args.kwonlyargs)
    for a in kws:
        params.append(Param(name=a.arg, type=_ann(a.annotation) or "object"))
    kd = list(fn.args.kw_defaults)
    k0 = len(params) - len(kws)
    for slot, d in enumerate(kd):
        if d is not None and k0 + slot < len(params):
            params[k0 + slot].default = _default_str(d)
    sig = FuncSig(name=fn.name, params=params, returns=_ann(fn.returns),
                  doc=_first_doc(fn), raises=_raises(fn),
                  keys=_return_keys(fn) or _doc_return_keys(_first_doc(fn)),
                  owner_class=owner_class)
    return sig


def parse_source(path: str, src: str, *, include_private: bool = False
                 ) -> tuple[Contract, list[str]]:
    """单个源文件 → (Contract, issues)。语法错误进 issues、返回空契约（不 crash）。"""
    c = Contract(path=path, module=Path(path).stem, raw=src or "")
    issues: list[str] = []
    try:
        tree = ast.parse(src or "", filename=path)
    except SyntaxError as exc:
        return c, [f"{path}：语法错误（{exc.lineno} 行附近），跳过该文件"]
    cls_name = ""
    for node in tree.body:
        if isinstance(node, (ast.ClassDef,)):
            cls_name = node.name
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if sub.name.startswith("_") and not include_private:
                        continue
                    sig = _sig_of(sub, owner_class=cls_name)
                    sig.examples = _doctests(sub.name, ast.get_docstring(sub) or "")
                    c.funcs.append(sig)
            # 嵌套类里的函数不追（真实项目没有那种写法；追了也是编复杂度）
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") and not include_private:
                continue
            sig = _sig_of(node)
            sig.examples = _doctests(node.name, ast.get_docstring(node) or "")
            c.funcs.append(sig)
        elif isinstance(node, ast.Assign):
            # 模块级常量不是函数；但它常是"字段表"（FIELDS = [...]），不进契约
            continue
    return c, issues


def from_code(ws: Path, paths: list[str] | None = None, *,
              include_private: bool = False) -> tuple[dict[str, Contract], list[str]]:
    """仓库（或指定相对路径列表）→ ({路径: Contract}, issues)。

    · 只扫 .py；跳过 SKIP_DIRS、test_*.py / *_test.py / conftest.py / setup.py；
    · 大文件（> 200 KB）跳过并如实进 issues（反推不是全量审计，别把内存吃穿）。
    """
    root = Path(ws)
    issues: list[str] = []
    out: dict[str, Contract] = {}
    if paths:
        rels = [str(p).replace("\\", "/") for p in paths]
    else:
        rels = []
        for p in sorted(root.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            if any(part in SKIP_DIRS for part in p.relative_to(root).parts[:-1]):
                continue
            base = p.name
            if base.startswith("test_") or base.endswith("_test.py") \
                    or base in NON_CONTRACT_FILES:
                continue
            rels.append(rel)
    for rel in rels:
        fp = root / rel
        if not fp.is_file():
            issues.append(f"{rel}：文件不存在")
            continue
        try:
            if fp.stat().st_size > 200 * 1024:
                issues.append(f"{rel}：超过 200 KB，跳过（反推不是全量审计）")
                continue
            src = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            issues.append(f"{rel}：读不了（{type(exc).__name__}）")
            continue
        con, probs = parse_source(rel, src, include_private=include_private)
        issues.extend(probs)
        out[rel] = con
    return out, issues


def render(contracts: dict[str, Contract]) -> str:
    """反推结果 → 一页纸（人审用；口径与需求契约的 describe 一致，可对照）。"""
    from .contract import describe
    lines = [f"# 契约反推：{len(contracts)} 个模块、"
             f"{sum(len(c.funcs) for c in contracts.values())} 个函数"
             "（从现有代码提取，未做任何猜测）"]
    for rel in sorted(contracts):
        d = describe(contracts[rel])
        lines.append(d)
    return "\n".join(lines)
