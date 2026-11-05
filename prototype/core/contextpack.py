"""本地信息提炼：把仓库压成"高密度、结构化"的上下文，再交给远端。

用户的判断（这是这套系统的核心增益点）：
    远端能看多少，决定它能想多深。直接把整个仓库扔进去是最笨的用法 ——
    源码里 90% 的字符对这次决策没有信息量（类型注解、样板、无关函数体）。
    本地应该先**提炼**：签名、docstring、调用关系、失败证据、测试状态，
    让同样的 token 预算装进**更多真正有用的信息**，从而抬高远端的可操控上限。

所以这个模块只做一件事：**压缩**。不判断对错、不调模型。

压缩口径（都是确定性的，同一输入同一输出 → 远端能吃前缀缓存）：
1. 模块摘要：import、顶层常量、每个函数的签名 + docstring 首行 + 行数；
2. 调用关系：谁调用谁（远端做架构决策最需要这个，比源码有用）；
3. 焦点符号：这次要改/被点名的那几个函数给**完整源码**，其余只给签名；
4. 契约摘要：函数 → 参数类型 / 返回 / 异常 / 示例条数；
5. 证据：失败判据 + 反例（已经是最窄形式）；
6. 预算：按字符分配，超了先砍"非焦点函数体"、再砍 docstring，绝不砍签名与证据。
"""
from __future__ import annotations
from . import tokens as _tokens

import ast
import re
from pathlib import Path

# 粗估 token：中文约 0.6 token/字，代码约 0.3；混排取 0.45（只用于预算分配，不用于计费）
TOKEN_PER_CHAR = 0.45


def estimate_tokens(text: str) -> int:
    return int(len(text or "") * TOKEN_PER_CHAR)


def _doc_first_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    return line[:90]


def _sig(node: ast.AST) -> str:
    """函数签名（含类型注解与默认值），一行。"""
    try:
        args = ast.unparse(node.args)
    except Exception:                                    # noqa: BLE001
        args = "..."
    ret = ""
    if getattr(node, "returns", None) is not None:
        try:
            ret = f" -> {ast.unparse(node.returns)}"
        except Exception:                                # noqa: BLE001
            ret = ""
    return f"{node.name}({args}){ret}"


def _calls_in(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def module_digest(rel: str, src: str, focus: tuple[str, ...] = ()) -> dict:
    """一个模块的摘要：签名表 + 调用关系 + 焦点函数的完整源码。"""
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {"path": rel, "error": f"语法错误：{exc.msg}", "funcs": [], "callers": {},
                "full": {}, "lines": len(src.splitlines())}
    imports: list[str] = []
    consts: list[str] = []
    funcs: list[dict] = []
    full: dict[str, str] = {}
    body = {n.name: n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            try:
                imports.append(ast.unparse(n))
            except Exception:                            # noqa: BLE001
                pass
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and \
                isinstance(n.targets[0], ast.Name) and n.targets[0].id.isupper():
            consts.append(n.targets[0].id)
    for name, node in body.items():
        funcs.append({
            "name": name,
            "sig": _sig(node),
            "doc": _doc_first_line(node),
            "lines": (getattr(node, "end_lineno", node.lineno) - node.lineno + 1),
            "calls": sorted(c for c in _calls_in(node) if c != name and c in body),
        })
        if name in focus:
            try:
                full[name] = ast.get_source_segment(src, node) or ""
            except Exception:                            # noqa: BLE001
                full[name] = ""
    return {"path": rel, "imports": imports, "consts": consts, "funcs": funcs,
            "full": full, "lines": len(src.splitlines()), "error": ""}


def callers(modules: list[dict]) -> dict[str, list[str]]:
    """跨模块调用关系：被调函数 → 调用它的模块::函数。"""
    out: dict[str, list[str]] = {}
    for m in modules:
        for f in m["funcs"]:
            for callee in f["calls"]:
                out.setdefault(callee, []).append(f"{m['path']}::{f['name']}")
    return out


def contract_digest(contracts: dict) -> list[str]:
    """契约摘要：每个函数一行（签名 + 异常 + 示例条数），比契约原文省 60% 以上。"""
    lines: list[str] = []
    for path, c in (contracts or {}).items():
        lines.append(f"## {path}")
        for f in getattr(c, "funcs", []) or []:
            params = ", ".join(
                f"{p.name}: {getattr(p, 'type', '') or 'object'}"
                for p in (getattr(f, "params", None) or []))
            ret = getattr(f, "returns", "") or "object"
            exs = getattr(f, "examples", None) or []
            raises = getattr(f, "raises", None) or []
            extra = f"｜示例 {len(exs)} 条" + (f"｜异常 {len(raises)}" if raises else "")
            doc = (getattr(f, "doc", "") or "").strip().splitlines()
            head = doc[0][:60] if doc else ""
            lines.append(f"- {f.name}({params}) -> {ret}{extra}"
                         + (f"｜{head}" if head else ""))
        raw = (getattr(c, "raw", "") or "").strip()
        if raw:
            # 只留"表结构/SQL 形态"这类判据生成要用的硬事实，其余由原文兜底
            for m in re.finditer(r"(CREATE TABLE[^;]{0,300})", raw, re.I):
                lines.append(f"- SQL: {m.group(1).strip()[:200]}")
    return lines


def pack_tokens(ws: Path, contracts: dict, paths: list[str] | None = None,
                focus: tuple[str, ...] = (), budget_tokens: int | None = None,
                evidence: str = "", tests: str = "") -> str:
    """按 **token** 预算压缩（用户要求的上限口径）。budget_tokens=None → 用设置里的上限。"""
    from . import settings_store, tokens
    budget = int(budget_tokens if budget_tokens is not None
                 else (settings_store.get_remote().get("max_prompt_tokens") or 950000))
    budget_chars = int(budget / max(tokens.ratio(), 0.2))
    return pack(ws, contracts, paths=paths, focus=focus, budget_chars=budget_chars,
                evidence=evidence, tests=tests)


def pack(ws: Path, contracts: dict, paths: list[str] | None = None,
         focus: tuple[str, ...] = (), budget_chars: int = 0,
         evidence: str = "", tests: str = "") -> str:
    """把工作区压成一段结构化上下文。

    budget_chars=0（默认）表示**不设上限**：用户明确要求"不要为了限流把仓库拆成两份"，
    拆开反而增加远端错判的风险。只有在触到模型物理上限（1M tokens）时，
    才按"非焦点模块只留签名、丢掉注释"的顺序压缩 —— 压缩 ≠ 截断，更 ≠ 拆成两次调用。
    """
    ws = Path(ws)
    paths = paths or list((contracts or {}).keys())
    mods: list[dict] = []
    for rel in paths:
        p = ws / rel
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        mods.append(module_digest(rel, src, focus=focus))

    # 1) 契约摘要（省 60%，且是判据来源，优先级最高）
    sec_contract = contract_digest(contracts)
    # 2) 模块签名表 + 调用关系
    sec_mods: list[str] = []
    for m in mods:
        if m.get("error"):
            sec_mods.append(f"## {m['path']}（{m['error']}）")
            continue
        sec_mods.append(f"## {m['path']}  ({m['lines']} 行"
                        + (f"｜import: {', '.join(i.split()[1] for i in m['imports'] if i.startswith('import '))}"
                           if m["imports"] else "") + ")")
        for f in m["funcs"]:
            pad = "（辅助）" if f["name"].startswith("_") else ""
            doc = f"｜{f['doc']}" if f["doc"] else ""
            sec_mods.append(f"- {f['sig']}{pad}{doc}")
    edges = callers(mods)
    sec_edges = [f"- {callee} ← {', '.join(sorted(set(srcs)))}"
                 for callee, srcs in sorted(edges.items()) if len(set(srcs)) > 1]
    # 3) 焦点函数的完整源码
    sec_full: list[str] = []
    for m in mods:
        for name, code in (m.get("full") or {}).items():
            if code.strip():
                sec_full.append(f"### {m['path']}::{name}\n```python\n{_tokens.cut(code.strip(), 6000)}\n```")

    def build(include_docs: bool, include_full: bool, include_edges: bool) -> str:
        parts = ["# 契约摘要（判据来源，权威）", *sec_contract, ""]
        if include_edges and sec_edges:
            parts += ["# 跨模块调用关系（架构决策用）", *sec_edges, ""]
        parts += ["# 模块签名表", *sec_mods, ""]
        if include_full and sec_full:
            parts += ["# 焦点符号完整源码", *sec_full, ""]
        if tests:
            parts += ["# 现有测试状态", _tokens.cut(tests, 2500), ""]
        if evidence:
            parts += ["# 机械判据给出的失败证据（最窄）",
                      _tokens.cut(evidence, 3000), ""]
        return "\n".join(parts)

    # 预算裁剪（仅在显式设了上限时才做）：超了先丢"非焦点的完整源码"，再丢调用关系。
    # 注意：绝不做"截断到一半"，也绝不把仓库拆成两次调用（用户要求：拆开反而增加错判风险）。
    text = build(True, True, True)
    if budget_chars and len(text) > budget_chars:
        text = build(True, True, False)
    if budget_chars and len(text) > budget_chars:
        text = build(False, False, False)
    if budget_chars and len(text) > budget_chars:
        text = text[:budget_chars] + "\n…（触到物理上限，已压缩；不做拆分调用）"
    return text
