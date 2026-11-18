"""§20.6 C 档落点 2/3/4：工程级声明的一等公民验收层。

覆盖三类工程产物：数据层（DB 迁移）、运行层（依赖锁定/服务编排/健康检查）、
质量层（可观测性基线）。契约里写一段声明，判据就必须在磁盘上**真跑一遍**兑现它。

为什么单独一层、不走 core/props.py 的 RUNNER 模板：
    props 是**函数级**判据（要 import 被测模块、按示例跑函数）；而迁移 SQL、锁文件、
    compose、健康端点、可观测性文件都是**工程级产物** —— 兑现与否靠的是在磁盘上真跑
    （sqlite3 executescript / 源码扫描 / 缩进结构解析），跟模块能否 import 毫无关系。
    硬塞进 props 会破掉"不 import 被测模块"这条铁律，也让主代理并行改 props.py 时互相踩。

血泪教训（同 §20.4 / auto.py:3214）：合成契约是**模型翻译**，散文段会被改写/丢掉；
    段丢了 = 判据永远不验收 = 声明永远不做。所以 inject_sections 由引擎**确定性**注入，
    不指望模型抄，且没段时逐字返回原文（一个字节都不变）。

零影响原则：老契约没写这五类新段 → parse_sections 返回 [] → 一处判据都不生成 →
    run 不调、state 不塞字段、收工理由不提 —— 现有行为完全不变。
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "EngSpec", "parse_sections", "render_sections", "sections_prompt",
    "inject_sections", "checks_for", "run", "summary",
]


@dataclass
class EngSpec:
    kind: str          # "migration" | "lockfile" | "health" | "compose" | "observability"
    path: str          # 仓库相对路径，或 health 的 "GET /health" 这种端点串
    contains: list[str]
    owner: str = ""    # 声明它的模块；空 = 全局


# 段头别名要宽容：人话写法不止一种。规范渲染只输出每类的第一个名字。
_HEAD_KIND = {
    "数据库迁移": "migration", "迁移": "migration",
    "依赖锁定": "lockfile", "锁文件": "lockfile",
    "服务编排": "compose", "编排": "compose",
    "健康检查": "health", "健康端点": "health",
    "可观测性": "observability", "可观测": "observability",
}
_HEAD_TITLE = {
    "migration": "【数据库迁移】",
    "lockfile": "【依赖锁定】",
    "compose": "【服务编排】",
    "health": "【健康检查】",
    "observability": "【可观测性】",
}
_KIND_CN = {
    "migration": "数据库迁移", "lockfile": "依赖锁定", "compose": "服务编排",
    "health": "健康检查", "observability": "可观测性",
}
_KIND_ORDER = ("migration", "lockfile", "compose", "health", "observability")

# 任意【..】全行头都算段边界（别的段、产物文件段都算），不只认本层五类。
_HEAD_RE = re.compile(r"^[ \t]*【(?P<name>[^】\r\n]+)】[ \t]*$", re.M)
# 条目行：照抄 core/contract.py::_ART_LINE 同款（带扩展名的路径 + 要点）。
_ART_LINE = re.compile(
    r"^[ \t]*[-*][ \t]+`?(?P<path>[\w][\w./\\ -]*?\.[A-Za-z][A-Za-z0-9]{0,5})`?[ \t]*"
    r"(?:[,，;；]?[ \t]*要点\s*[:：][ \t]*(?P<keys>.+?))?[ \t]*$",
    re.M)
# health 的 path 是 "GET /health" 这种带空格的端点串，_ART_LINE 的扩展名要求认不了。
_HEALTH_LINE = re.compile(
    r"^[ \t]*[-*][ \t]+`?(?P<path>(?:[A-Za-z]+\s+)?/[^\s,，;；]+)`?[ \t]*"
    r"(?:[,，;；]?[ \t]*要点\s*[:：][ \t]*(?P<keys>.+?))?[ \t]*$",
    re.M)
# render_sections 会带「（归属：x）」；解析时剥掉，否则要点子串被污染成假红。
_OWNER_TAIL = re.compile(r"[（(]归属\s*[:：][^）)]*[）)]\s*$")

_OBS_MARKS = ("json", "logger", "logging", "trace_id", "metrics", "prometheus", "span")
_SKIP_DIRS = {".sixcoder", "node_modules", "__pycache__", ".git", ".venv", "venv"}


def _module_sections(text: str) -> list[tuple[int, str]]:
    """(位置, 模块路径) 按出现顺序 —— 与 contract._module_sections 同一份逻辑。"""
    from .planner import HEADER_RE, is_module_header
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


def _clip_body(body: str) -> str:
    """段体到下一个【..】头或模块标题就停（照抄 contract.parse_artifacts 的边界逻辑）。

    用 planner.is_module_header 判模块标题：它自带"产物条目行不算模块"的排除，
    段里的 `- migrations/0001_init.sql` 不会被误判成模块而把正文吞掉。
    """
    from .planner import is_module_header
    pos = 0
    for raw in (body or "").splitlines(keepends=True):
        if raw.lstrip().startswith("【") or is_module_header(raw):
            return (body or "")[:pos]
        pos += len(raw)
    return body or ""


def _split_keys(raw: str) -> list[str]:
    raw = _OWNER_TAIL.sub("", raw or "").strip()
    out: list[str] = []
    for k in re.split(r"[,，、;；]", raw):
        k = k.strip().strip("`\"'").strip()
        if k:
            out.append(k)
    return out


def _parse_lines(body: str, kind: str, owner: str) -> list[EngSpec]:
    line_re = _HEALTH_LINE if kind == "health" else _ART_LINE
    out: list[EngSpec] = []
    for m in line_re.finditer(body or ""):
        path = m.group("path").replace("\\", "/").strip().strip("`").strip()
        if not path or ".." in path.split("/"):
            continue
        keys = _split_keys(m.group("keys") or "")
        if not any(x.path == path for x in out):
            out.append(EngSpec(kind=kind, path=path, contains=keys, owner=owner))
    return out


def parse_sections(text: str) -> list[EngSpec]:
    """从需求/契约全文解析五类工程声明段（所有段合并，按 path 去重）。

    段边界四层（对齐 contract.parse_artifacts）：① 所有【..】头切段 ② 下一个头为段尾
    ③ 段体遇模块标题即停 ④ owner 归属用 _module_sections 按段头位置判定。
    """
    text = text or ""
    heads = list(_HEAD_RE.finditer(text))
    if not heads:
        return []
    kinds = [(m.start(), m.end(), _HEAD_KIND.get(m.group("name").strip()))
             for m in heads]
    if not any(k for _, _, k in kinds):
        return []
    sections = _module_sections(text)
    out: list[EngSpec] = []
    for i, (start, end, kind) in enumerate(kinds):
        if not kind:
            continue
        seg_end = kinds[i + 1][0] if i + 1 < len(kinds) else len(text)
        body = _clip_body(text[end:seg_end])
        owner = ""
        for s_pos, s_path in sections:                # 按位置升序
            if s_pos > start:
                break
            owner = s_path
        for spec in _parse_lines(body, kind, owner):
            if not any(x.path == spec.path for x in out):
                out.append(spec)
    return out


def render_sections(specs: list[EngSpec]) -> str:
    """渲染回契约语法（给确定性注入 / 报告展示用）。顺序固定，保证幂等。"""
    specs = [s for s in (specs or []) if getattr(s, "path", "")]
    if not specs:
        return ""
    lines: list[str] = []
    for kind in _KIND_ORDER:
        group = [s for s in specs if s.kind == kind]
        if not group:
            continue
        if lines:
            lines.append("")
        lines.append(_HEAD_TITLE.get(kind, f"【{kind}】"))
        seen: set[str] = set()
        for s in group:
            if s.path in seen:
                continue
            seen.add(s.path)
            line = f"- {s.path}"
            if getattr(s, "contains", None):
                line += "   要点: " + ", ".join(str(k) for k in s.contains)
            if getattr(s, "owner", ""):
                line += f"   （归属：{s.owner}）"
            lines.append(line)
    return "\n".join(lines)


def sections_prompt(specs: list[EngSpec]) -> str:
    """给远端的写作提示：逐段要求 + 交回格式（file: 围栏交完整内容）。没段返回 ""。"""
    specs = [s for s in (specs or []) if getattr(s, "path", "")]
    if not specs:
        return ""
    lines = ["【工程声明（判据会逐条机械验收：声明了就必须兑现）】"]
    for s in specs:
        tip = ("；要点（内容里必须出现这些字）："
               + "、".join(f"「{k}」" for k in s.contains)) if getattr(s, "contains", None) else ""
        lines.append(f"- [{s.kind}] {s.path}{tip}")
    lines += [
        "各类判据要求：",
        " · 数据库迁移 .sql：必须是能在 SQLite 里真跑通的 DDL（CREATE TABLE 等），"
        "同目录数字版本前缀严格连续、不重号。",
        " · 依赖锁定/锁文件：必须真锁版本（pip 的 == 或 npm 的 \"version\"/lockfileVersion/integrity），"
        "只列包名不算。",
        " · 服务编排：必须含 services:，且每个服务含 image: 或 build:。",
        " · 健康检查：产物源码里必须真注册该端点（@app.get/@router.get/path()/add_route）。",
        " · 可观测性：文件里要有结构化日志/指标/追踪的实质代码（去掉 # 注释后仍在）。",
        "输出格式：迁移 .sql、锁文件、compose、可观测性文件都用 ` ```file:<path> ` 围栏交**完整内容**，例如：",
        "```file:migrations/0001_init.sql",
        "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT);",
        "```",
        "引擎会把它们写到对应路径；内容必须自包含、可直接落盘。",
    ]
    return "\n".join(lines)


def checks_for(specs: list[EngSpec]) -> list[dict]:
    """把声明变成判据清单（对齐 props.artifact_checks 的 dict 形态）。每条 hard=True。"""
    out: list[dict] = []
    for s in (specs or []):
        if not getattr(s, "path", ""):
            continue
        out.append({
            "kind": s.kind,
            "path": s.path,
            "contains": [str(k) for k in (getattr(s, "contains", None) or [])],
            "owner": getattr(s, "owner", "") or "",
            "desc": f"工程声明 {s.kind} {s.path}",
            "hard": True,
        })
    return out


def inject_sections(text: str, goal_text: str) -> str:
    """把原始需求里的工程段**确定性地**插进切片契约（幂等）。

    contractgen 是模型翻译，散文段可能被改写/丢掉；判据按它验收 → 段丢了就永远不验收。
    已存在于 text 的（按 kind+path 去重）不重复插；goal_text 没段则逐字返回 text。
    """
    text = text or ""
    goal = parse_sections(goal_text or "")
    if not goal:
        return text
    have = {(s.kind, s.path) for s in parse_sections(text)}
    todo = [s for s in goal if (s.kind, s.path) not in have]
    if not todo:
        return text
    seg = render_sections(todo)
    if not seg:
        return text
    if not text:
        return seg + "\n"
    sep = "\n" if text.endswith("\n") else "\n\n"
    return text + sep + seg + "\n"


# ------------------------------------------------------------------ 判据（真跑、机械可判）
def _read_text(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _version_chain(p: Path) -> list[str]:
    """同目录迁移文件的数字前缀：重号 / 断层都点名。"""
    out: list[str] = []
    try:
        files = sorted(f for f in p.parent.iterdir()
                       if f.is_file() and f.suffix.lower() == ".sql")
    except Exception as exc:                              # noqa: BLE001
        return [f"版本链检查不了：{type(exc).__name__}: {exc}"]
    nums: list[tuple[int, str]] = []
    for f in files:
        m = re.match(r"^(\d+)", f.name)
        if m:
            nums.append((int(m.group(1)), f.name))
    if len(nums) < 2:
        return []
    by_num: dict[int, list[str]] = {}
    for n, name in nums:
        by_num.setdefault(n, []).append(name)
    dup = {n: names for n, names in by_num.items() if len(names) > 1}
    if dup:
        desc = "、".join(f"{n}（{', '.join(names)}）" for n, names in sorted(dup.items()))
        out.append(f"迁移版本链重号：{desc}")
    seq = sorted(by_num)
    gaps = [(a, b) for a, b in zip(seq, seq[1:]) if b != a + 1]
    if gaps:
        desc = "、".join(
            f"{a}→{b}（缺 {a + 1}" + (f"~{b - 1}" if b - a > 2 else "") + "）"
            for a, b in gaps)
        out.append(f"迁移版本链断层：{desc}")
    return out


def _check_migration(p: Path, txt: str) -> list[str]:
    out: list[str] = []
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(txt)
            n = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if n < 1:
                out.append("SQL 执行后没有任何表（DDL 没落地）")
        finally:
            conn.close()
    except sqlite3.Error as exc:
        out.append(f"SQL 执行失败：{exc}")           # 带 SQLite 原文，不吞
    except Exception as exc:                          # noqa: BLE001
        out.append(f"SQL 执行失败：{type(exc).__name__}: {exc}")
    out += _version_chain(p)
    return out


def _check_lockfile(txt: str) -> list[str]:
    if "==" in txt:
        return []
    if any(k in txt for k in ('"version"', "lockfileVersion", "integrity")):
        return []
    return ["只是清单不是锁文件（没找到 == 或 \"version\"/lockfileVersion/integrity）"]


def _check_compose(txt: str) -> list[str]:
    lines = txt.splitlines()
    base = None
    start = -1
    for i, ln in enumerate(lines):
        if re.match(r"^\s*services\s*:\s*(#.*)?$", ln):
            base = len(ln) - len(ln.lstrip())
            start = i
            break
    if base is None:
        return ["缺顶层 services: 键"]
    body: list[str] = []
    for ln in lines[start + 1:]:
        if not ln.strip() or ln.lstrip().startswith("#"):
            body.append(ln)
            continue
        if len(ln) - len(ln.lstrip()) <= base:
            break
        body.append(ln)
    indents = [len(x) - len(x.lstrip()) for x in body
               if x.strip() and not x.lstrip().startswith("#")]
    if not indents:
        return ["services: 下没有任何缩进的服务键"]
    svc_ind = min(indents)
    blocks: dict[str, list[str]] = {}
    cur = ""
    for x in body:
        if not x.strip() or x.lstrip().startswith("#"):
            if cur:
                blocks[cur].append(x)
            continue
        ind = len(x) - len(x.lstrip())
        if ind == svc_ind:
            m = re.match(r"^([\w.-]+)\s*:\s*(#.*)?$", x.strip())
            if m:
                cur = m.group(1)
                blocks[cur] = []
                continue
        if cur:
            blocks[cur].append(x)
    if not blocks:
        return ["services: 下找不到服务键（形如 `  web:`）"]
    out = []
    for name, seg_lines in blocks.items():
        if not re.search(r"^\s*(image|build)\s*:", "\n".join(seg_lines), re.M):
            out.append(f"服务 {name} 缺 image:/build:")
    return out


def _iter_py(ws: Path):
    try:
        for p in Path(ws).rglob("*.py"):
            parts = set(p.parts)
            if parts & _SKIP_DIRS:
                continue
            if any(str(x).startswith(".tmp") for x in p.parts):
                continue
            yield p
    except Exception:                                    # noqa: BLE001
        return


def _check_health(ws: Path, spec: EngSpec) -> list[str]:
    m = re.match(r"^([A-Za-z]+)\s+(/\S*)$", (spec.path or "").strip())
    if not m:
        return ["健康端点没按 METHOD /path 声明（例如 GET /health）"]
    method, route = m.group(1).lower(), m.group(2)
    pats = [
        r"@[A-Za-z_]\w*\." + method + r"\s*\(\s*[\"']" + re.escape(route) + r"[\"']",
        r"\bpath\s*\(\s*[\"']" + re.escape(route) + r"[\"']",
        r"\badd_route\s*\([^)]*[\"']" + re.escape(route) + r"[\"']",
    ]
    for py in _iter_py(ws):
        try:
            t = _read_text(py)
        except Exception:                                # noqa: BLE001
            continue
        if any(re.search(p, t) for p in pats):
            return []
    return [f"健康端点没注册（源码里找不到 @router/@app.{method}(\"{route}\") "
            f"/ path(\"{route}\") / add_route）"]


def _check_observability(txt: str) -> list[str]:
    code = "\n".join(ln for ln in txt.splitlines()
                     if not ln.lstrip().startswith("#"))
    if any(k in code for k in _OBS_MARKS):
        return []
    return ["去掉 # 注释后找不到结构化日志/指标/追踪的实质标记"
            "（json/logger/logging/trace_id/metrics/prometheus/span）"]


def _check_one(ws: Path, spec: EngSpec) -> list[str]:
    """单条声明的失败原因列表；空 = 通过。异常一律转成原因，不吞。"""
    if spec.kind == "health":
        return _check_health(ws, spec)
    p = ws / spec.path
    if not p.is_file():
        return [f"文件不存在：{spec.path}"]
    try:
        txt = _read_text(p)
    except Exception as exc:                              # noqa: BLE001
        return [f"文件读不了：{type(exc).__name__}: {exc}"]
    reasons: list[str] = []
    miss = [str(k) for k in (getattr(spec, "contains", None) or []) if str(k) not in txt]
    if miss:
        reasons.append("要点子串缺失：" + "、".join(miss))
    if spec.kind == "migration":
        reasons += _check_migration(p, txt)
    elif spec.kind == "lockfile":
        reasons += _check_lockfile(txt)
    elif spec.kind == "compose":
        reasons += _check_compose(txt)
    elif spec.kind == "observability":
        reasons += _check_observability(txt)
    return reasons


def run(ws, specs: list[EngSpec]) -> dict:
    """逐条真跑工程判据。返回 {"ok","passed","total","fails","skipped"}。

    每条声明 hard=True：失败即 ok=False（声明了就必须兑现）。单个判据内部异常
    也记进 fails，绝不当通过。specs 为空 → 全 0 / ok=True（零影响）。
    """
    specs = [s for s in (specs or []) if getattr(s, "path", "")]
    if not specs:
        return {"ok": True, "passed": 0, "total": 0, "fails": [], "skipped": 0}
    ws = Path(ws)
    fails: list[dict] = []
    passed = total = skipped = 0
    for s in specs:
        if s.kind not in _KIND_CN:
            skipped += 1
            continue
        total += 1
        try:
            reasons = _check_one(ws, s)
        except Exception as exc:                          # noqa: BLE001
            reasons = [f"判据执行异常：{type(exc).__name__}: {exc}"]
        if reasons:
            fails.append({
                "kind": s.kind,
                "path": s.path,
                "detail": f"{_KIND_CN[s.kind]} {s.path} " + "；".join(reasons[:4]),
            })
        else:
            passed += 1
    return {"ok": not fails, "passed": passed, "total": total,
            "fails": fails, "skipped": skipped}


def summary(result: dict) -> str:
    """给收工理由用：ok 返回 ""；失败点名 kind/path/原因（最多 6 条）。"""
    r = result or {}
    if r.get("ok"):
        return ""
    fails = r.get("fails") or []
    if not fails:
        return "工程声明未兑现"
    bits = [str(f.get("detail") or f"{f.get('kind')} {f.get('path')}")
            for f in fails[:6]]
    return "工程声明未兑现：" + "；".join(bits)
