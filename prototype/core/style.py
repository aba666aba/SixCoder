"""风格与一致性：从既有代码反推规约，生成后做偏离检查。

解决三件事（都是 3B 的典型漂移）：
  1. 命名风格（snake_case / PascalCase）
  2. 注释与 docstring 是否齐全、格式是否一致
  3. 类型标注覆盖率、引号风格、缩进

不阻塞流程：偏离记为 static_rule 证据 + 报告条目（裁判权仍归闸门与人工）。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

SKIP = {".git", "__pycache__", ".pytest_cache", ".sixcoder", "workspaces", "node_modules"}


def _py_files(ws: Path, limit: int = 40) -> list[Path]:
    ws = Path(ws)
    out = []
    for p in sorted(ws.rglob("*.py")):
        try:
            rel = p.relative_to(ws)
        except ValueError:
            continue
        if any(part in SKIP for part in rel.parts[:-1]) or rel.name.startswith("test_"):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def profile(ws: Path) -> dict:
    """统计既有代码的风格特征（无代码时返回默认规约）。"""
    files = _py_files(ws)
    if not files:
        return {"files": 0, "snake": 0, "camel": 0, "doc_ratio": 0.0,
                "annot_ratio": 0.0, "quotes": '"', "indent": 4}

    snake = camel = 0
    funcs = docd = annot = annot_total = 0
    dq = sq = 0
    indents = []
    for p in files:
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
            tree = ast.parse(text)
        except SyntaxError:
            continue
        dq += len(re.findall(r'"[^"\n]*"', text))
        sq += len(re.findall(r"'[^'\n]*'", text))
        for line in text.splitlines():
            m = re.match(r"^( +)\S", line)
            if m:
                indents.append(len(m.group(1)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                funcs += 1
                if re.search(r"[a-z]_[a-z]|^[a-z]+$", node.name):
                    snake += 1
                elif re.search(r"[a-z][A-Z]", node.name):
                    camel += 1
                if ast.get_docstring(node):
                    docd += 1
                params = [a for a in node.args.args if a.arg not in ("self", "cls")]
                annot_total += len(params) + 1
                annot += sum(1 for a in params if a.annotation) + (1 if node.returns else 0)

    indent = 4
    if indents:
        from collections import Counter
        indent = Counter(indents).most_common(1)[0][0]
    return {
        "files": len(files),
        "snake": snake,
        "camel": camel,
        "doc_ratio": round(docd / funcs, 2) if funcs else 0.0,
        "annot_ratio": round(annot / annot_total, 2) if annot_total else 0.0,
        "quotes": '"' if dq >= sq else "'",
        "indent": indent,
    }


def render(prof: dict) -> str:
    """把规约渲染成可进 Prompt 的短文本（≤300 字）。"""
    naming = "snake_case" if prof["snake"] >= prof["camel"] else "camelCase"
    return (
        f"项目规约（从现有 {prof['files']} 个模块反推）：\n"
        f"- 函数命名 {naming}，类命名 PascalCase\n"
        f"- 公开函数带 docstring（现有覆盖率 {prof['doc_ratio']:.0%}）、参数与返回值带类型标注（{prof['annot_ratio']:.0%}）\n"
        f"- 缩进 {prof['indent']} 空格；字符串默认用 {prof['quotes']} 引号\n"
        f"- 不使用 print 输出调试信息；异常不要裸 except/pass 吞掉\n"
        f"- 参数非法抛 ValueError，错误信息用中文说明原因"
    )


def check(ws: Path, changed: dict[str, tuple[str, str]], prof: dict | None = None) -> list[str]:
    """检查本节点改动相对既有规约的偏离（只报事实）。"""
    ws = Path(ws)
    prof = prof or profile(ws)
    expected_naming = "snake" if prof["snake"] >= prof["camel"] else "camel"
    issues: list[str] = []
    for rel, (_base, new) in changed.items():
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(new)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue
                is_camel = bool(re.search(r"[a-z][A-Z]", node.name))
                if expected_naming == "snake" and is_camel:
                    issues.append(f"{rel}::{node.name} 命名偏离（项目用 snake_case）")
                if prof["doc_ratio"] >= 0.5 and not ast.get_docstring(node):
                    issues.append(f"{rel}::{node.name} 缺 docstring（项目内 {prof['doc_ratio']:.0%} 有）")
                missing = [a.arg for a in node.args.args if a.arg not in ("self", "cls") and not a.annotation]
                if prof["annot_ratio"] >= 0.5 and missing:
                    issues.append(f"{rel}::{node.name} 缺类型标注：{', '.join(missing[:3])}")
    return issues[:8]
