"""代码导航：按符号名定位函数/类，解析中文意图，供 AI 对话驱动"跳到文件/调试函数"。

设计原则（和 coderun.py 一脉相承）：
  · 精确定位靠 AST，不靠字符串碰运气：Python 用 ast，JS/TS/Java/Go/C/C++/Rust/PHP/Ruby
    用各语言"定义行"正则退路（AST 跨 10 语言不现实，正则只认 def/function/class 定义行，
    不认注释/字符串里的同名词——行首锚定 + 关键字约束）。
  · 意图解析先用本地规则（快、不花钱、离线可用）：从中文里抠"打开/定位/调试/找/跳" +
    文件名 + 函数名。抠不出明确目标就如实返回候选，让 AI/用户选，绝不瞎跳。
  · 安全：所有路径经 tools.safe_path 校验在仓库内；符号名只当"匹配串"，不当命令执行。
  · 不许静默吞异常：单文件解析失败记 skipped，不假装"没有符号"。

对外三个入口：
  find_symbols(ws, query, *, kind=None, limit=60) -> list[dict]
      query 是符号名（可含文件路径前缀）。返回 [{path, line, name, kind, signature}]。
  locate(ws, path, symbol) -> dict | None
      在单个文件里精确定位一个符号（调试下断点用）。
  parse_intent(text) -> dict
      中文意图 → {action: navigate|debug|search|none, symbol, file_hint, raw}
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

# 支持的源码后缀（与 coderun._RUN_SPEC 对齐，另加 .h/.hpp/.vue/.tsx/.jsx）
_SRC_EXT = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue",
            ".java", ".go", ".c", ".cc", ".cpp", ".h", ".hpp", ".rs",
            ".php", ".rb", ".sh", ".ps1", ".kt", ".swift", ".cs"}
_SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv",
              "venv", ".sixcoder", "_logs", ".tmp", "site-packages"}

# 各语言"定义行"正则：只认行首（允许缩进）的定义声明，避免命中调用/字符串。
# 顺序：class/struct/interface 优先于 function，取最长匹配名。
_DEF_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("class", re.compile(r'^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)')),
    ("class", re.compile(r'^\s*(?:public\s+|private\s+|protected\s+)?(?:abstract\s+)?class\s+(\w+)')),
    ("class", re.compile(r'^\s*struct\s+(\w+)')),
    ("class", re.compile(r'^\s*interface\s+(\w+)')),
    ("class", re.compile(r'^\s*enum\s+(\w+)')),
    ("function", re.compile(r'^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+\*?([A-Za-z_$][\w$]*)')),
    ("function", re.compile(r'^\s*(?:public\s+|private\s+|protected\s+|static\s+)*'
                            r'(?:async\s+)?[\w<>\[\],\s]+?\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:throws\s+\w+\s*)?\{')),
    ("function", re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>')),
    ("function", re.compile(r'^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?function\b')),
    ("function", re.compile(r'^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)')),   # Go
    ("function", re.compile(r'^\s*fn\s+([A-Za-z_]\w*)')),                         # Rust
    ("function", re.compile(r'^\s*def\s+([A-Za-z_]\w*)\s*\(')),                   # Python 退路
    ("function", re.compile(r'^\s*(?:public|private|protected|static|final|abstract)\s+'
                            r'[\w<>\[\],.?\s]+\s+([A-Za-z_$][\w$]*)\s*\(')),     # Java/C#
]


def _signature(line: str) -> str:
    s = line.strip()
    return s[:160] + ("…" if len(s) > 160 else "")


def _iter_sources(ws: Path):
    """遍历仓库里的源码文件（跳过依赖/产物目录），yield (rel, abs_path)。"""
    root = ws.resolve()
    for cur, dirs, files in os_walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        cur_path = Path(cur)
        for name in files:
            p = cur_path / name
            if p.suffix.lower() in _SRC_EXT:
                yield p.relative_to(root).as_posix(), p


def os_walk(root: Path):
    import os
    return os.walk(root)


def _py_symbols(text: str) -> list[dict]:
    """Python：AST 精确给出函数/类的定义行与所在类（方法归属）。"""
    out: list[dict] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            first = node.body[0].lineno if node.body else node.lineno
            out.append({"line": node.lineno, "end_line": getattr(node, "end_lineno", node.lineno),
                        "name": node.name, "kind": kind,
                        "signature": _signature(ast.get_source_segment(text, node) or node.name),
                        "body_line": first})
    return out


def _regex_symbols(text: str) -> list[dict]:
    """其他语言：逐行匹配定义声明。"""
    out: list[dict] = []
    seen: set[tuple[int, str]] = set()
    for i, line in enumerate(text.splitlines(), 1):
        for kind, pat in _DEF_PATTERNS:
            m = pat.match(line)
            if m:
                name = m.group(1)
                if (i, name) not in seen:
                    seen.add((i, name))
                    out.append({"line": i, "end_line": i, "name": name, "kind": kind,
                                "signature": _signature(line), "body_line": i})
                break
    return out


def symbols_in_file(path: Path) -> list[dict]:
    """单文件符号表。读盘失败/语法错误都返回空列表（由调用方记 skipped）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        return []
    if path.suffix.lower() == ".py":
        return _py_symbols(text)
    return _regex_symbols(text)


def _match_score(name: str, query: str) -> int:
    """符号名与查询的匹配打分：精确 > 前缀 > 子串 > 大小写不敏感子串。"""
    q = query.strip()
    if not q:
        return 0
    if name == q:
        return 100
    if name.lower() == q.lower():
        return 90
    if name.startswith(q):
        return 70
    if q.lower() in name.lower():
        return 50
    return 0


def find_symbols(ws: Path, query: str, *, kind: str | None = None,
                 limit: int = 60) -> dict:
    """全仓按符号名找定义。返回 {query, hits, files, skipped}。

    query 可带路径提示，如 "payment.py refund" → 只在 payment.py 里找 refund。
    """
    ws = Path(ws).resolve()
    q = (query or "").strip()
    if not q:
        return {"query": q, "hits": [], "files": 0, "skipped": []}

    # 拆出"文件提示"和"符号名"：取像文件名/路径的 token 作为 file_hint
    toks = re.findall(r'[\w./\\-]+', q)
    file_hint = ""
    symbol = q
    for t in toks:
        if re.search(r'\.(py|js|ts|jsx|tsx|java|go|c|cc|cpp|h|hpp|rs|php|rb|vue|cs|kt|swift|sh|ps1)$',
                     t, re.I) or "/" in t or "\\" in t:
            file_hint = t.replace("\\", "/").lstrip("./")
            symbol = q.replace(t, "").strip()
            break

    hits: list[dict] = []
    skipped: list[str] = []
    scanned = 0
    for rel, p in _iter_sources(ws):
        if file_hint and file_hint.split("/")[-1].lower() not in rel.lower():
            continue
        scanned += 1
        syms = symbols_in_file(p)
        if not syms and p.stat().st_size > 0 and p.suffix.lower() == ".py":
            # Python 有内容却解析不出 → 可能语法错，留痕（不静默吞）
            try:
                ast.parse(p.read_text(encoding="utf-8", errors="replace"))
            except SyntaxError as exc:
                skipped.append(f"{rel}:{exc.lineno}")
        for s in syms:
            if kind and s["kind"] != kind:
                continue
            score = _match_score(s["name"], symbol) if symbol else 30
            if score == 0 and symbol:
                continue
            hits.append({**s, "path": rel, "score": score})

    hits.sort(key=lambda x: (-x["score"], x["path"], x["line"]))
    return {"query": q, "symbol": symbol, "file_hint": file_hint,
            "hits": hits[:limit], "files": scanned, "skipped": skipped[:20]}


def locate(ws: Path, path: str, symbol: str) -> dict | None:
    """在单个文件里精确定位符号；精确名优先，否则前缀/子串唯一命中。"""
    from . import tools as _tools                        # core/tools.py 的 safe_path 牢笼
    ws = Path(ws).resolve()
    p = _tools.safe_path(ws, path)
    if not p.is_file():
        return None
    syms = symbols_in_file(p)
    rel = p.relative_to(ws).as_posix()
    exact = [s for s in syms if s["name"] == symbol]
    if exact:
        best = exact[0]
    else:
        scored = [( _match_score(s["name"], symbol), s) for s in syms]
        scored = [(sc, s) for sc, s in scored if sc >= 70]
        if not scored:
            return None
        scored.sort(key=lambda x: -x[0])
        best = scored[0][1]
    return {**best, "path": rel}


# ---------------------------------------------------------------- 中文意图解析

_ACTION_WORDS = {
    "debug": ("调试", "断点", "单步", "打个断点", "跟一下", "跑到"),
    "navigate": ("打开", "跳到", "跳转", "定位", "去", "看看", "找到", "查看", "转到", "带我去"),
    "search": ("哪个文件", "在哪", "哪里", "搜", "搜索", "找一下", "找找", "全仓", "有哪些"),
}
_FILE_RE = re.compile(r'([\w./\\-]+\.(?:py|js|ts|jsx|tsx|java|go|c|cc|cpp|h|hpp|rs|php|rb|vue|cs|kt|swift|sh|ps1))', re.I)
_SYMBOL_RE = re.compile(r'(?:函数|方法|类|function|method|class)\s*[「「\'"\s]*([A-Za-z_$][\w$]*)')
_CALL_RE = re.compile(r'([A-Za-z_$][\w$]*)\s*\(\s*\)')


def parse_intent(text: str) -> dict:
    """从中文指令里抠出动作/文件名/符号名。抠不出就 action=none（交回代码填充）。"""
    raw = (text or "").strip()
    if not raw:
        return {"action": "none", "raw": raw, "symbol": "", "file_hint": ""}

    action = "none"
    for act in ("debug", "navigate", "search"):        # debug 优先于 navigate（"调试并打开"）
        if any(w in raw for w in _ACTION_WORDS[act]):
            action = act
            break

    file_hint = ""
    m = _FILE_RE.search(raw)
    if m:
        file_hint = m.group(1).replace("\\", "/")

    symbol = ""
    m = _SYMBOL_RE.search(raw)
    if m:
        symbol = m.group(1)
    else:
        m = _CALL_RE.search(raw)
        if m:
            symbol = m.group(1)
    if not symbol:
        # 退路：去掉动作词/文件名/标点后，若只剩一个像标识符的 token，当符号名
        rest = raw
        for w in ("调试", "断点", "单步", "打开", "跳到", "跳转", "定位", "看看",
                  "找到", "查看", "转到", "带我去", "哪个文件", "在哪", "哪里",
                  "搜", "搜索", "找一下", "找找", "全仓", "有哪些", "函数", "方法", "类"):
            rest = rest.replace(w, " ")
        if file_hint:
            rest = rest.replace(file_hint, " ")
        toks = [t for t in re.findall(r'[A-Za-z_$][\w$]*', rest) if len(t) >= 2]
        if len(toks) == 1:
            symbol = toks[0]

    # 有调试/导航动作却没有任何目标 → 也要如实标出来（界面提示用户说清楚）
    return {"action": action, "raw": raw, "symbol": symbol, "file_hint": file_hint}
