"""接口先行：让模型只产出"签名"，文件骨架由引擎渲染。

为什么这么改（实测依据）：
- 3B 写整文件（80–120 行）稳定失败：`storage.py` / `cli.py` 都栽在这里
- 但它写**单个函数体（20–40 行）**的可靠性高得多
- 而且签名是**可判定**的（能被我的闸门检查），正文不是

职责划分：
  模型：给签名 JSON（函数名/参数/类型/返回类型/一句话 docstring）
  引擎：渲染骨架文件（docstring + import + 签名 + raise NotImplementedError）
  模型：之后逐个 replace_function 填函数体
"""
from __future__ import annotations

from . import tokens as _tokens

import re
from dataclasses import dataclass, field
from pathlib import Path

MISSING = "raise NotImplementedError"


@dataclass
class Param:
    name: str
    type: str = "object"
    default: str | None = None


@dataclass
class Sig:
    name: str
    params: list[Param] = field(default_factory=list)
    ret: str = "object"
    doc: str = ""
    is_class: bool = False

    def render(self) -> str:
        if self.is_class:
            lines = [f"class {self.name}:",
                     f'    """{self.doc or self.name}"""', ""]
            if self.params:
                args = ", ".join(
                    f"{p.name}: {p.type}" + (f" = {p.default}" if p.default is not None else "")
                    for p in self.params)
                lines += [f"    def __init__(self, {args}) -> None:",
                          f"        {MISSING}"]
            else:
                lines += ["    pass"]
            return "\n".join(lines) + "\n"
        args = ", ".join(
            f"{p.name}: {p.type}" + (f" = {p.default}" if p.default is not None else "")
            for p in self.params)
        doc = f'    """{self.doc}"""\n' if self.doc else ""
        return f"def {self.name}({args}) -> {self.ret}:\n{doc}    {MISSING}\n"


_IDENT = re.compile(r"^[A-Za-z_]\w{0,40}$")


_BAD_TYPES = {"any", "Any", "object()", "", "NoneType"}


def _clean_type(t: str) -> str:
    """把模型乱写的类型收敛到 Python 合法标注（`any` 不是类型，会挂掉 mypy 与运行）。"""
    t = (t or "").strip()
    if not t or t.lower() in ("any", "object()", "unknown", "?"):
        return "object"
    if t in _BAD_TYPES:
        return "object"
    if any(ch in t for ch in "<>{}&|") or " " in t and "[" not in t:
        return "object"
    return t


def parse_signatures(data: dict, contract: str = "", limit: int = 5) -> list[Sig]:
    """把模型给的 JSON 规范化成 Sig 列表（脏数据直接丢弃，不猜）。"""
    out: list[Sig] = []
    raw = [it for it in (data.get("functions") or []) if isinstance(it, dict)]
    # 契约里点名的函数优先（骨架只该覆盖这次要实现的），其余按原顺序补位
    named = [it for it in raw if str(it.get("name") or "") and str(it.get("name")) in (contract or "")]
    rest = [it for it in raw if it not in named]
    # 契约点名的就只建这几个（多余签名会诱导模型写错模块职责）；没点名时才按顺序补 3 个
    picked = named[:limit] if named else rest[:3]
    for item in picked:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not _IDENT.match(name):
            continue
        params: list[Param] = []
        for i, p in enumerate(item.get("params") or []):
            if isinstance(p, dict):
                pname = str(p.get("name") or f"a{i+1}").strip()
                ptype = _clean_type(str(p.get("type") or "object"))
                pdef = p.get("default")
            else:
                pname, ptype, pdef = f"a{i+1}", "object", None
            if not _IDENT.match(pname):
                pname = f"a{i+1}"
            params.append(Param(pname, ptype, None if pdef in (None, "") else str(pdef)))
        out.append(Sig(
            name=name, params=params,
            ret=_clean_type(str(item.get("returns") or "object")),
            # 同样不硬切：80 字符会把"除数为 0 抛 ZeroDivisionError"截断
            doc=_tokens.cut(str(item.get("doc") or "").strip(), 300),
            is_class=bool(item.get("is_class")) or name[:1].isupper(),
        ))
    # 模型常漏参数（实测契约写 create_table(conn)，模型给 []，结果生成 def create_table()，
    # 整个模块因 TypeError 全灭）→ 用契约里显式写出的参数名补齐
    want = _contract_params(contract)
    for sig in out:
        if not sig.params and want.get(sig.name):
            sig.params = [Param(pn, "object", None) for pn in want[sig.name]]
    return out


def _contract_params(contract: str) -> dict[str, list[str]]:
    "委托结构化契约（正则版保留为兜底）。"
    try:
        from .contract import contract_params as _cp
        got = _cp(contract)
        if got:
            return got
    except Exception:
        pass
    """从契约文本抽 `name(a, b)` 的参数名（只做兜底，不做类型推断）。"""
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


def render_class_skeleton(path: str, class_name: str, methods: list[Sig],
                          contract: str = "") -> str:
    """渲染"类 API"骨架（双模式之一）：需求写的是 class X，产物就该是个类。

    以前一律按 `def name(...)` 平铺渲染，类需求会被写成一堆同名模块函数（还缺 self），
    判据只能靠整表达式，模型看到错骨架更糊涂。这里显式写成类，并按 Python 惯例补 self。
    """
    _head_doc = _tokens.cut(f"{Path(path).stem} 模块。{contract or ''}", 400)
    names = "/".join(s.name for s in methods if s.name != "__init__")
    head = (f'"""{_head_doc}"""\n\n\nclass {class_name}:\n'
            f'    """{class_name}：{_tokens.cut(names, 120)}"""\n')
    blocks: list[str] = []
    for s in methods:
        params = ", ".join(["self"] + [f"{p.name}: {p.type}" if p.type else p.name
                                       for p in s.params if p.name not in ("self", "cls")])
        ret = f" -> {s.ret}" if s.ret else ""
        blk = f"    def {s.name}({params}){ret}:\n"
        if s.doc:
            blk += f'        """{_tokens.cut(s.doc, 300)}"""\n'
        blk += f'        raise NotImplementedError("TODO: {s.name}")\n'
        blocks.append(blk)
    return head + "\n" + "\n".join(blocks) + "\n"


def render_skeleton(path: str, sigs: list[Sig], contract: str = "") -> str:
    """渲染整文件骨架。已存在的 import 由后续步骤补全（引擎会补）。"""
    _head_doc = _tokens.cut(f"{Path(path).stem} 模块。{contract or ''}", 400)
    head = f'"""{_head_doc}"""\n\n'
    body = "\n\n".join(s.render().rstrip("\n") for s in sigs)
    return head + body + "\n"


def is_skeleton(text: str) -> bool:
    """判断文件是否仍是"只有签名"的骨架（用于决定是否需要先生成骨架）。"""
    if not text.strip():
        return True
    if "NotImplementedError" not in text:
        return False
    lines = [l for l in text.splitlines() if l.strip() and not l.strip().startswith(("#", '"""'))]
    real = [l for l in lines if not re.match(r"^\s*(def |class |@)", l)
            and "NotImplementedError" not in l and not re.match(r"^\s*pass\s*$", l)]
    return len(real) <= 2


def stub_names(text: str) -> list[str]:
    """骨架里还未实现（仍是 NotImplementedError）的函数名。"""
    names = []
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s*def (\w+)", line)
        if m:
            current = m.group(1)
            continue
        if current and "NotImplementedError" in line:
            names.append(current)
            current = None
    return names
