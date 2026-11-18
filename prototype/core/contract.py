"""结构化契约：把"需求里那一段文本"解析成可被闸门/骨架/测试/远端包体共同消费的对象。

为什么必须这么做（本轮实测的教训）：
现在每个模块都在**字符串上做手术**——闸门用正则扣函数名、骨架用正则扣参数、判据用正则扣示例。
结果是：名字 1 字符就漏、dict 字面量被截断、换一种排版就失效。
一次性把文本解析成 Contract，所有下游都读同一份结构，才不会各漏各的。

Contract 的元素：
    Contract(path, module, funcs, families)
    FuncSig(name, params[Param], returns, doc, raises[Exc], examples[Example], keys[Key])
    Param(name, type, default)
    Example(expr, expect)  # expr: "normalize_age(' 42 ')"，expect: "42"
"""
from __future__ import annotations

from . import tokens as _cm_tokens

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

SKIP_NAMES = {"if", "for", "while", "return", "print", "def", "assert", "len",
              "example", "raise", "except", "with", "in", "not", "and", "or"}

# f(args) -> ret ：从契约正文里识别函数签名
SIG_RE = re.compile(r"([A-Za-z_]\w{0,40})\s*\(([^()]*)\)\s*(?:->\s*([^：:；;。\n]+))?")
# 示例行：f(args) == 字面量 / f(args) 应返回 值
EX_RE = re.compile(r"([A-Za-z_]\w{0,40})\s*\(([^()]*)\)\s*(?:==|应当等于|应返回|应等于|返回)\s*"
                   r"(`[^`]+`|\"[^\"]*\"|'[^']*'|[-\w.\[\]{}]+)")
RAISE_RE = re.compile(r"(?:抛|raise|异常)\s*([A-Za-z_]\w*)")
# 类声明：`- class LRU：` / `class LRU:` / `1) class LRU(缓存)` —— 类 API 契约（双模式）
_CLASS_RE = re.compile(r"^(?:[-*\d.、)）\s]*)?class\s+([A-Z][A-Za-z0-9_]{0,40})\b")
KEY_RE = re.compile(r"[\"']([a-z_][\w]{1,30})[\"']\s*:")


@dataclass
class Param:
    name: str
    type: str = "object"
    default: str | None = None


@dataclass
class Example:
    func: str
    args: list[str]
    expect: str            # 源码字面量；空串表示"无期望值"
    kind: str = "eq"       # eq | raises


@dataclass
class FuncSig:
    name: str
    params: list[Param] = field(default_factory=list)
    returns: str = ""
    doc: str = ""
    raises: list[str] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)
    owner_class: str = ""          # 属于哪个类（类 API 契约）；空 = 普通模块函数
    route: str = ""                # 这个函数服务哪个端点（§11.4g 契约点名；空 = 不确定）

    def arity(self) -> int:
        return len(self.params)

    def required(self) -> int:
        return sum(1 for p in self.params if p.default is None)

    def is_method(self) -> bool:
        return bool(self.owner_class) or any(p.name in ("self", "cls") for p in self.params)


@dataclass
class Contract:
    path: str
    module: str
    funcs: list[FuncSig] = field(default_factory=list)
    raw: str = ""
    full_text: str = ""                                    # 整篇需求原文（类 API 示例在里面）
    aliases: dict[str, str] = field(default_factory=dict)   # 别名 → 规范名（date → birth）
    # 产物文件（§20.4 A 档落点 1）：本模块声明"必须产出的文件"（模板/静态资源等）。
    # 空列表 = 没声明 = 完全不影响现有行为（增量字段，不写就不生效）。
    artifacts: list["ArtifactFile"] = field(default_factory=list)

    def by_name(self, name: str) -> FuncSig | None:
        return next((f for f in self.funcs if f.name == name), None)

    def names(self) -> list[str]:
        return [f.name for f in self.funcs]

    def examples(self) -> list[Example]:
        return [e for f in self.funcs for e in f.examples]

    def canonical(self, key: str) -> str:
        """把字段写法归一到规范名（不是别名就原样返回）。"""
        return self.aliases.get(str(key), str(key))


# ------------------------------------------------------------------ 产物文件（§20.4 A 档落点 1）
# 需求里的散文（"前端采用 Django 模板…使用 ECharts"）解析不出可判功能点 →
# 模板/静态资源永远不会被派单（实测：app/web.py、app/dashboard.py 在 contract.txt 里 0 次出现，
# 而蓝图里挂着 cap.page.dashboard_chart）。产物文件段把它变成**结构化、可验收**的声明。
@dataclass
class ArtifactFile:
    path: str                                   # 仓库相对路径（app/templates/index.html）
    contains: list[str] = field(default_factory=list)   # 内容要点（子串匹配，与 body_has 同口径）
    owner: str = ""                             # 声明它的小节所属模块；空 = 顶层（全局）


# 段头：【必须产出的文件】/【产物文件】/【必须产出文件】
_ART_HEAD = re.compile(r"^[ \t]*【(?:必须产出的文件|产物文件|必须产出文件)】[ \t]*$", re.M)
# 条目：- app/templates/index.html   要点: 毕业设计选题, 登录
_ART_LINE = re.compile(
    r"^[ \t]*[-*][ \t]+`?(?P<path>[\w][\w./\\ -]*?\.[A-Za-z][A-Za-z0-9]{0,5})`?[ \t]*"
    r"(?:[,，;；]?[ \t]*要点\s*[:：][ \t]*(?P<keys>.+?))?[ \t]*$",
    re.M)


def _parse_art_lines(body: str, owner: str) -> list[ArtifactFile]:
    out: list[ArtifactFile] = []
    for m in _ART_LINE.finditer(body or ""):
        path = m.group("path").replace("\\", "/").strip().strip("`").strip()
        if not path or ".." in path.split("/"):
            continue
        keys = []
        for k in re.split(r"[,，、;；]", m.group("keys") or ""):
            k = k.strip().strip("`\"'").strip()
            if k:
                keys.append(k)
        if not any(a.path == path for a in out):
            out.append(ArtifactFile(path=path, contains=keys, owner=owner))
    return out


def parse_artifacts(text: str) -> list[ArtifactFile]:
    """从需求/契约全文解析【必须产出的文件】段（所有段合并，按 path 去重）。

    段写在哪一节就归那一节所属的模块（owner）；写在任何模块标题之前的归 owner=""（全局）。
    """
    text = text or ""
    heads = [m for m in _ART_HEAD.finditer(text)]
    if not heads:
        return []
    # 模块标题位置（与 parse_requirement 同一份分块逻辑），用来判段归属
    sections = _module_sections(text)
    out: list[ArtifactFile] = []
    for i, h in enumerate(heads):
        seg_end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[h.end():seg_end]
        # 段体在下一个非空行开始遇到【..】头或模块标题就停（防止吞掉后面的契约正文）
        stop = None
        for mm in re.finditer(r"^[ \t]*【(?!必须产出的文件|产物文件|必须产出文件)|^[ \t]*\d+[)）.、][ \t]*[\w./]+\.py", body, re.M):
            stop = mm.start()
            break
        if stop is not None:
            body = body[:stop]
        owner = ""
        for s_pos, s_path in sections:            # sections 按位置升序
            if s_pos > h.start():
                break
            owner = s_path
        for a in _parse_art_lines(body, owner):
            if not any(x.path == a.path for x in out):
                out.append(a)
    return out


def render_artifacts(arts: list[ArtifactFile]) -> str:
    """渲染回契约语法（给切片简报注入 / 报告展示用）。"""
    arts = [a for a in (arts or []) if getattr(a, "path", "")]
    if not arts:
        return ""
    lines = ["【必须产出的文件】"]
    for a in arts:
        line = f"- {a.path}"
        if a.contains:
            line += "   要点: " + ", ".join(a.contains)
        if getattr(a, "owner", ""):
            line += f"   （归属：{a.owner}）"
        lines.append(line)
    return "\n".join(lines)


def artifacts_prompt(arts: list[ArtifactFile]) -> str:
    """给作者的产物文件块：清单 + 输出格式（怎么把文件交回来）。"""
    arts = [a for a in (arts or []) if getattr(a, "path", "")]
    if not arts:
        return ""
    lines = ["【必须产出的文件（判据会逐字检查：文件必须存在、要点必须能找到）】"]
    for a in arts:
        tip = ("；要点（内容里必须出现这些字）："
               + "、".join(f"「{k}」" for k in a.contains)) if a.contains else ""
        lines.append(f"- {a.path}{tip}")
    lines += [
        "输出格式：每个文件用 `文件路径` 做围栏信息串给出**完整内容**，例如：",
        "```file:app/templates/index.html",
        "<!doctype html>…（完整文件内容）…",
        "```",
        "引擎会把它们写到对应路径。只写清单里声明的文件；内容必须自包含（HTML 要带完整标签结构）。",
    ]
    if any(str(a.path).lower().endswith((".html", ".htm")) for a in arts):
        lines += [
            "页面实现口径（§20.4/§20.5 定稿）：FastAPI + Jinja2 模板（模板文件放 app/templates/），"
            "静态资源放 app/static/（入口已自动挂 /static），图表用 ECharts 的 CDN 引入"
            "（不引前端工程、不写 app/main.py）；页面路由返回 HTMLResponse/TemplateResponse。",
        ]
    return "\n".join(lines)


def missing_artifacts(ws, arts: list[ArtifactFile]) -> list[dict]:
    """逐条核对产物文件：不存在 / 存在但缺要点。返回 [{path, exists, missing}]，空 = 全齐。"""
    from pathlib import Path as _Path
    ws = _Path(ws)
    out: list[dict] = []
    for a in arts or []:
        p = ws / str(getattr(a, "path", "") or "")
        if not getattr(a, "path", ""):
            continue
        if not p.is_file():
            out.append({"path": a.path, "exists": False, "missing": list(a.contains or [])})
            continue
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except Exception:                                # noqa: BLE001
            txt = ""
        miss = [k for k in (a.contains or []) if str(k) not in txt]
        if miss:
            out.append({"path": a.path, "exists": True, "missing": miss})
    return out


def inject_artifacts(text: str, arts: list[ArtifactFile]) -> str:
    """把产物文件段**确定性地**插进契约文本里归属模块的小节（幂等）。

    为什么引擎插而不是指望合成契约的模型抄：contractgen 是模型翻译，散文段
    可能被改写/丢掉；判据又按它验收 → 段丢了 = 永远不验收 = 永远不做（同 §20.1 证据 2）。
    owner 为空的段插在**最后一个**模块小节里（整合/ui 通常在最后）；已有同 path 段则不重复。
    """
    arts = [a for a in (arts or []) if getattr(a, "path", "")]
    if not arts:
        return text or ""
    text = text or ""
    have = {a.path for a in parse_artifacts(text)}
    todo = [a for a in arts if a.path not in have]
    if not todo:
        return text
    sections = _module_sections(text)                    # [(pos, path)]
    by_owner: dict[str, list[ArtifactFile]] = {}
    for a in todo:
        by_owner.setdefault(getattr(a, "owner", "") or "", []).append(a)
    out = text
    # 从后往前插，位置不失效
    insertions: list[tuple[int, str]] = []
    for owner, group in by_owner.items():
        seg = render_artifacts(group)
        if owner:
            # owner 小节的**下一个**模块标题之前 = 本小节末尾；找不到 owner 就插到文末
            pos = None
            for i, (s_pos, s_path) in enumerate(sections):
                if s_path == owner:
                    pos = sections[i + 1][0] if i + 1 < len(sections) else len(out)
                    break
            insertions.append((len(out) if pos is None else pos, seg))
        else:
            insertions.append((len(out), seg))
    for pos, seg in sorted(insertions, key=lambda x: -x[0]):
        out = out[:pos] + ("\n\n" if pos < len(out) and not out[:pos].endswith("\n") else "\n") \
            + seg + ("\n" if pos < len(out) and not out[pos:].startswith("\n") else "")
    return out


# 字段别名：`别名：date → birth` / `date 是 birth 的别名` / `alias: date -> birth`
_ALIAS_LINE = re.compile(
    r"(?:别名|字段别名|alias(?:es)?)\s*[:：]?\s*(?P<body>[^\n]+)", re.I)
_ALIAS_PAIR = re.compile(
    r"(?P<a>[A-Za-z_]\w{1,40})\s*(?:→|->|=>|＝|=)\s*(?P<b>[A-Za-z_]\w{1,40})")
_ALIAS_SENT = re.compile(
    r"`?(?P<a>[A-Za-z_]\w{1,40})`?\s*是\s*`?(?P<b>[A-Za-z_]\w{1,40})`?\s*的?(?:别名|规范名|同义)")
# 人话写法也要认：`date 走 parse_date 后改名成 birth` / `date 改名为 birth` / `映射成 birth`
# 实测事故（实验台 pipeline）：契约里用"改名成 birth"把映射说清楚了，但解析器只认
# 「别名：x → y」这一种写法 → 判成"契约没声明别名" → **整个夜班拒绝开跑**（0/8、
# 一个文件都不写）。把话说清楚了却不让干，这比让模型猜更糟。
_ALIAS_VERB = re.compile(
    r"(?<![\w])(?P<a>[A-Za-z_]\w{1,40})[^。；;，,、\n]{0,24}?"
    r"(?:改名成|改名为|改名叫|重命名为|重命名成|映射成|映射为|改写成|改写为|统一成|统一为)"
    r"\s*[`\"']?(?P<b>[A-Za-z_]\w{1,40})")


def parse_aliases(text: str) -> dict[str, str]:
    """从需求/契约里解析"同一逻辑字段的不同写法"，返回 {别名: 规范名}。

    为什么需要它：实测（night-v2 系列）契约里 clean_row 处理 `date`、表结构却是 `birth`，
    模型很合理地统一成 `date`，于是建表少了 birth 列 —— e2e 红、单函数却全绿。
    这类矛盾不该靠模型猜，也不该靠改一个名字了事，而是**声明一次映射**。
    """
    out: dict[str, str] = {}
    for m in _ALIAS_SENT.finditer(text or ""):
        out[m.group("a")] = m.group("b")
    for m in _ALIAS_VERB.finditer(text or ""):
        out[m.group("a")] = m.group("b")
    for line in (text or "").splitlines():
        m = _ALIAS_LINE.search(line)
        if not m:
            continue
        for pair in _ALIAS_PAIR.finditer(m.group("body")):
            out[pair.group("a")] = pair.group("b")
    # 自反/自指的别名没有意义
    return {k: v for k, v in out.items() if k != v}


def render_aliases(aliases: dict[str, str]) -> str:
    """把别名渲染成给模型看的一句话（进 prompt 与 e2e）。"""
    if not aliases:
        return ""
    pairs = "、".join(f"`{k}` → `{v}`" for k, v in aliases.items())
    return (f"字段别名：{pairs}。别名只出现在**输入**里；清洗/入库/读回的产物必须用规范名，"
            f"否则跨模块会静默丢字段。")


# ------------------------------------------------------------------ 解析
def _split_params(raw: str) -> list[Param]:
    out: list[Param] = []
    for piece in (raw or "").split(","):
        piece = piece.strip()
        if not piece or piece in ("self", "cls"):
            continue
        default = None
        if "=" in piece:
            piece, default = piece.split("=", 1)
            piece = piece.strip()
            default = default.strip() or None
        ptype = "object"
        if ":" in piece:
            piece, ptype = piece.split(":", 1)
            piece = piece.strip()
            ptype = ptype.strip() or "object"
        name = piece.strip().lstrip("*")
        if re.match(r"^[A-Za-z_]\w{0,40}$", name):
            out.append(Param(name=name, type=ptype.strip(), default=default))
    return out


def parse_body(path: str, body: str) -> Contract:
    """把"某个模块那一段正文"解析成结构化契约。"""
    text = body or ""
    c = Contract(path=path, module=Path(path).stem, raw=text)
    # 先按"；/。/换行/- 项目符号"切成条目，每条尽量对应一个函数
    items = [x.strip(" -\t") for x in re.split(r"[；;。\n]+", text) if x.strip()]
    cur: FuncSig | None = None
    _cls = ""                                  # 当前所在的类（类 API 契约，双模式的一种）
    for item in items:
        m_cls = _CLASS_RE.match(item)
        if m_cls:
            _cls = m_cls.group(1)
            continue
        m = SIG_RE.match(item)
        if m and m.group(1) not in SKIP_NAMES:
            name = m.group(1)
            sig = c.by_name(name)
            if sig is None:
                sig = FuncSig(name=name, params=_split_params(m.group(2)),
                              returns=(m.group(3) or "").strip())
                c.funcs.append(sig)
            if _cls:
                sig.owner_class = _cls
            cur = sig
            # 该条目里可能还带示例/异常/键
            _absorb(cur, item)
            continue
        if cur is not None:
            _absorb(cur, item)
    # 示例也可能写在"示例（必须满足）"段落里，按函数名归属
    for m in EX_RE.finditer(text):
        name = m.group(1)
        if name in SKIP_NAMES:
            continue
        sig = c.by_name(name)
        if sig is None:
            sig = FuncSig(name=name)
            c.funcs.append(sig)
        ex = Example(func=name, args=[a.strip() for a in m.group(2).split(",") if a.strip()],
                     expect=m.group(3).strip())
        if not any(e.args == ex.args and e.expect == ex.expect for e in sig.examples):
            sig.examples.append(ex)
    _rederive_keys(c)
    # 本模块小节里的产物文件段（直接 parse_body 的调用方也拿到同一种结构）
    try:
        _own = parse_artifacts(text)
        if _own:
            c.artifacts = [ArtifactFile(path=a.path, contains=a.contains, owner=path)
                           for a in _own]
    except Exception:                                    # noqa: BLE001
        pass
    return c


def _rederive_keys(c: Contract) -> None:
    """返回键**只从"返回…"之后取**（契约按「；」切段后，入参形状常先出现）。

    实测（实验台 search-index 的 build）：说明里先写"每篇文档形如 {id,text}"、再写
    "返回 {index,docs}"，旧逻辑把 id/text 也当成返回键 → 判据要求返回里必须有 id/text →
    渲染出的测试必红 → 测试文件被删、交付物里一个测试都没有，而验收还写着 3/3 通过。
    """
    for f in getattr(c, "funcs", []) or []:
        doc = f.doc or ""
        if "返回" not in doc:
            continue
        tail = doc.split("返回", 1)[1]
        keys: list[str] = []
        for m in KEY_RE.finditer(tail):
            k = m.group(1)
            if k not in keys:
                keys.append(k)
        if keys:
            f.keys = keys
        elif "dict" not in str(getattr(f, "returns", "") or "").lower():
            # 返回的是 list/str/int 这类**没有键**的东西：之前从入参形状收到的键必须清掉。
            # 实测（实验台 os-kernel 的 rr_order，返回 list[str]）：
            # 判据要求"返回必须包含键 name/burst"，两版正确实现都被判红（0/3）。
            f.keys = []


def _absorb(sig: FuncSig, item: str) -> None:
    for m in EX_RE.finditer(item):
        ex = Example(func=sig.name,
                     args=[a.strip() for a in m.group(2).split(",") if a.strip()],
                     expect=m.group(3).strip())
        if not any(e.args == ex.args and e.expect == ex.expect for e in sig.examples):
            sig.examples.append(ex)
    if not sig.doc:
        # 120 字会把长规格切断（实测 auth.login：成功/失败两种返回形状只保住第一种，
        # 于是"返回键"判据按并集硬判，正确实现必红）。规范是模型与判据的共同依据，不能切。
        sig.doc = item[:1200]
    elif _is_constraint(item):
        # 实测：契约按「；」切开后，第二段起（"结果须在 0..150 内"、"每行含 id/name/age/email/birth"）
        # 原来被整段丢掉 → 判据知道边界、模型不知道边界 → 同一函数连炸十几轮。
        # 约束必须跟着函数走，不能丢。
        merged = (sig.doc + "；" + item).strip()
        if len(merged) <= 600:
            sig.doc = merged
    for m in RAISE_RE.finditer(item):
        exc = m.group(1)
        if exc[:1].isupper() and exc not in sig.raises:
            sig.raises.append(exc)
    # 返回键只从"返回…"之后取；带"返回"的这一段是**权威返回形状**，直接覆盖之前
    # 从入参形状里收到的键（实测 search-index 的 build：先出现"每篇文档形如 {id,text}"，
    # 后出现"返回 {index,docs}"，旧逻辑把 id/text 也当返回键 → 判据要求返回里有 id/text
    # → 渲染出的测试必红 → 测试文件被删、交付物里 index.py 一个测试都没有）。
    _is_return = "返回" in item
    _key_src = item.split("返回", 1)[1] if _is_return else item
    _rk = [m.group(1) for m in KEY_RE.finditer(_key_src)]
    if _rk:
        if _is_return:
            sig.keys = list(dict.fromkeys(_rk))
        else:
            for k in _rk:
                if k not in sig.keys:
                    sig.keys.append(k)


# 以这些开头的片段不是"函数规范"，不能并进 doc（示例/模块标题/表头）
_NON_SPEC = ("示例", "例子", "例：", "例:", "必须满足", "返回", "注意", "说明", "输出格式")


def _is_constraint(item: str) -> bool:
    """判断一个被「；」切出来的片段是不是"这个函数的补充约束"。"""
    t = (item or "").strip()
    if not t or len(t) > 200:
        return False
    if t.startswith(_NON_SPEC):
        return False
    if "==" in t or "->" in t:                       # 示例或新签名，不是约束
        return False
    if re.match(r"^\d+\s*[)）.、]", t):               # "2) store.py：..." 模块标题
        return False
    if re.search(r"\.py\s*[：:]", t):                 # 模块标题
        return False
    return True


def _split_args(text: str) -> list[str]:
    """按逗号切参数，但**不进括号/花括号/引号内部**。

    实测 bug：`summarize([{"age": 10, "email": "a@x.com"}])` 用 `split(",")` 会切出
    `[{"age": 10"` 和 `"email": "a@x.com"}]` 两个半截——示例直接报废，
    于是"返回整个 dict"的函数长期没有可判定示例（这类函数恰好最容易写错）。
    """
    out, buf, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf).strip())
    return [x for x in out if x]


def _scan_expect(text: str, i: int) -> str:
    """从 i 开始读一个"期望值字面量"：配平的 {}/[]/() 、引号字符串、或裸 token。"""
    n = len(text)
    while i < n and text[i] in " \t":
        i += 1
    if i >= n:
        return ""
    ch = text[i]
    if ch in "{[(":
        pairs = {"{": "}", "[": "]", "(": ")"}
        close, depth, quote = pairs[ch], 0, ""
        start = i
        while i < n:
            c = text[i]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c == ch:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
            i += 1
        return ""
    if ch in "\"'`":
        quote, start = ch, i
        i += 1
        while i < n:
            if text[i] == quote and text[i - 1] != "\\":
                return text[start:i + 1]
            i += 1
        return ""
    start = i
    while i < n and text[i] not in " \t\n，。；;、":
        i += 1
    return text[start:i]


def _iter_examples_pos(text: str):
    """同 `_iter_examples`，但额外给出每个示例在原文中的**位置**。

    位置用来判断"这个示例属于哪个模块小节"（见 `parse_requirement`）。
    """
    i, n = 0, len(text or "")
    while i < n:
        m = re.compile(r"([A-Za-z_]\w{0,40})\s*\(").search(text, i)
        if not m:
            return
        name = m.group(1)
        j = m.end()
        depth, quote, close_at = 1, "", -1
        while j < n:
            c = text[j]
            if quote:
                if c == quote:
                    quote = ""
            elif c in "\"'":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                depth -= 1
                if depth == 0:
                    close_at = j
                    break
            j += 1
        if close_at < 0:
            return
        args_text = text[m.end():close_at]
        k = close_at + 1
        rest = text[k:k + 24]
        mm = re.match(r"\s*(?:==|应当等于|应返回|应等于|返回)\s*", rest)
        if not mm:
            i = close_at + 1
            continue
        expect = _scan_expect(text, k + mm.end())
        i = k + mm.end()
        if expect and name not in SKIP_NAMES:
            yield name, args_text, expect, m.start()


def _iter_examples(text: str):
    """手写扫描器：找出 `name(args) == 字面量` 形式的可判定示例。

    比正则可靠：参数与期望值都按括号配平来读，因此
    `summarize([{"age": 10}]) == {"count": 1}` 这种嵌套结构能完整取到。
    """
    for name, args_text, expect, _pos in _iter_examples_pos(text):
        yield name, args_text, expect


def _module_sections(text: str) -> list[tuple[int, str]]:
    """按出现顺序给出 (位置, 模块路径)：示例用它判断自己属于哪一节。

    实测 2026-11-09 夜班卡死：app/api/topics.py 与 app/services/topics.py
    都有 create_topic_draft，旧实现按"第一个同名函数"认领示例 →
    api 拿到两条（一条不含 status/source、一条含）→ 契约自查判
    "同一组参数两个期望值" → 判定"需要甲方拍板" → **夜班拒绝开跑，0 产出**。
    """
    from .planner import is_module_header, HEADER_RE
    out: list[tuple[int, str]] = []
    pos = 0
    for raw in (text or "").splitlines(keepends=True):
        m = HEADER_RE.match(raw) if is_module_header(raw) else None
        if m:
            path = m.group(1).lstrip("./")
            if path:
                out.append((pos, path))
        pos += len(raw)
    return out


def parse_requirement(requirement: str) -> dict[str, Contract]:
    """从整篇需求解析出 {模块路径: Contract}（复用 planner 的模块分块逻辑）。

    注意：模块块在"示例（必须满足）"处被截断（否则最后一个模块会吞掉全篇），
    所以示例要**单独全文扫描**，再按函数名归属到对应模块的契约上。
    """
    from .planner import _module_blocks

    text = requirement or ""
    blocks = _module_blocks(text)
    out = {path: parse_body(path, body) for path, body in blocks.items()}
    # 全篇原文挂在每份契约上：类 API 的"整表达式"示例常写在模块块之外，
    # 只读模块块会一条都取不到（实测：stateful-class 的 LRU(1).keys() == [] 丢了）。
    for c in out.values():
        c.full_text = text
    # **端点 → 函数名（契约点名，§11.4g）**：挂到 FuncSig.route 上。
    # 为什么挂在这里：契约文本里签名行本来就写着"这个函数服务哪个端点"，
    # 那是**唯一权威**。挂到契约对象上之后，写码（提示里点名）、
    # 体检（按契约判）、接线（按名接）三处消费**同一份**，不再各猜各的。
    # 实测不挂的代价：契约点名 `teachers_me`，远端却写成 `teacher_me`
    # → 点名找不到 → 接线退回字符串猜 → 接到"负载建议"函数上
    # → 端到端返回错结构 → 覆盖永远不涨。
    try:
        from . import routefuncs as _rfm
        _rfs = _rfm.parse(text)
    except Exception:                                    # noqa: BLE001
        _rfs = {}
    if _rfs:
        _by_fn: dict[str, str] = {}
        for _p, _fn in _rfs.items():
            _by_fn.setdefault(_fn, _p)
        for _c in out.values():
            for _f in _c.funcs:
                if not getattr(_f, "route", "") and _f.name in _by_fn:
                    try:
                        _f.route = _by_fn[_f.name]
                    except Exception:                    # noqa: BLE001
                        pass
    # 字段别名是**跨模块**的：解析一次，挂到每份契约上（模型与判据都要用同一份）
    aliases = parse_aliases(text)
    for c in out.values():
        if aliases:
            c.aliases = dict(aliases)
    # 全篇示例归属：**按示例所在的小节归模块**，不能按函数名乱认领。
    # 同名函数跨模块时（api 版不含 status/source、services 版含），
    # 按名字认领会把两节的示例塞进同一个模块 → 契约自查误判"同一组参数两个期望值"
    # → 夜班拒绝开跑（实测 2026-11-09：0 产出，远端一次都没被调用）。
    sections = _module_sections(text)
    for name, args_text, expect, pos in _iter_examples_pos(text):
        owner = None
        sig = None
        for s_pos, s_path in sections:                # sections 按位置升序
            if s_pos > pos:
                break
            c = out.get(s_path)
            if c is not None and c.by_name(name) is not None:
                owner = c
                sig = c.by_name(name)
        if sig is None:
            # 小节里没声明这个函数（需求级示例 / 跨模块引用）→ 退回旧口径
            owner = next((c for c in out.values() if c.by_name(name)), None)
            if owner is None:
                continue
            sig = owner.by_name(name)
        ex = Example(func=name, args=_split_args(args_text), expect=expect.strip())
        if not any(e.args == ex.args and e.expect == ex.expect for e in sig.examples):
            sig.examples.append(ex)
    # 产物文件段（§20.4 落点 1）：按位置归属模块小节；顶层全局段归**最后一个**模块
    # （整合/ui 通常在最后）。去重按 path —— parse_body 已收过的不再重复挂。
    try:
        _arts = parse_artifacts(text)
    except Exception:                                    # noqa: BLE001
        _arts = []
    if _arts and out:
        _order = list(out)
        for a in _arts:
            owner = a.owner if a.owner in out else (_order[-1] if not a.owner else None)
            if owner is None:
                continue
            _c = out[owner]
            if not any(x.path == a.path for x in _c.artifacts):
                _c.artifacts.append(a)
    return out


# ------------------------------------------------------------------ 便捷视图
def contract_params(contract: str | Contract) -> dict[str, list[str]]:
    """{函数名: [参数名]}（骨架用它补参数）。"""
    c = contract if isinstance(contract, Contract) else parse_body("m.py", contract)
    return {f.name: [p.name for p in f.params] for f in c.funcs if f.params}


def check_expr(example: Example) -> str:
    return f"m.{example.func}({', '.join(example.args)})"


def doc_text(f) -> str:
    """函数规范正文（散文部分）：去掉 doc 开头重复的 "name(params) -> ret：" 前缀。

    这段文字是"模型必须知道、判据也在用"的约束来源（0..150、每行含哪些列、其余键原样保留）。
    """
    txt = (getattr(f, "doc", "") or "").strip()
    if not txt:
        return ""
    pat = r"^\s*" + re.escape(getattr(f, "name", "")) + r"\s*\([^)]*\)\s*(?:->\s*[^\s：:]+)?\s*[：:]\s*"
    m = re.match(pat, txt)
    return txt[m.end():].strip() if m else txt


def describe(c: Contract) -> str:
    lines = [f"# {c.path}（{len(c.funcs)} 个函数）"]
    alias_line = render_aliases(getattr(c, "aliases", {}) or {})
    if alias_line:
        lines.append("  " + alias_line)
    for f in c.funcs:
        params = ", ".join(f"{p.name}: {p.type}" if p.type != "object" else p.name for p in f.params)
        ret = f" -> {f.returns}" if f.returns else ""
        extra = []
        if f.raises:
            extra.append("抛 " + "/".join(f.raises))
        if f.examples:
            extra.append(f"{len(f.examples)} 个示例")
        if f.keys:
            extra.append("键 " + "/".join(f.keys[:4]))
        # 类 API（双模式）：签名必须显示成 `LRU.get(...)`，否则模型会写成模块函数
        _shown = f"{f.owner_class}." if getattr(f, "owner_class", "") else ""
        lines.append(f"  {_shown}{f.name}({params}){ret}"
                     + ("｜" + "；".join(extra) if extra else ""))
        body = doc_text(f)
        if body:
            # 散文规范必须进 prompt：判据是从它来的，不告诉模型就是让它猜边界
            lines.append(f"      规范：{_cm_tokens.cut(body, 500)}")
    return "\n".join(lines)
