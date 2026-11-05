"""代码审查：对现有代码提改进意见，并找出「实现与预期功能不符」之处。

两类来源，结论分开呈现：
1. **确定性规则**（deterministic）：AST/正则可判定，几乎不会误报，可当改进项直接执行
2. **模型语义比对**（model）：只在给了「预期功能」时启用，输出仅为**建议**，
   不计入任何闸门、不作为"通过"依据（docs/13）

输出结构（Finding）：
  severity: P0 必须修 / P1 应该修 / P2 可以更好
  rule:     规则 id 或 "model.expectation"
  file:line
  evidence:  事实（代码片段 / 变异体存活 / 测试缺失）
  expected:  预期（来自规则或用户填的预期功能）
  actual:    实际
  suggestion: 改法（可带 patch 提示）
"""
from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import gates, tools

MAX_FILES = 30
CODE_EXT = {".py", ".java", ".ts", ".js", ".vue", ".go"}


def _read(p: Path) -> str:
    """统一按 utf-8-sig 读：真实仓库里 BOM 很常见，用 utf-8 读会把 BOM 当代码字符导致假语法错。"""
    return p.read_text(encoding="utf-8-sig", errors="replace")


@dataclass
class Finding:
    severity: str
    rule: str
    file: str
    line: int
    title: str
    evidence: str = ""
    expected: str = ""
    actual: str = ""
    suggestion: str = ""
    source: str = "deterministic"

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- 确定性规则

DEBUG_OUT = re.compile(r"^\s*(print\(|console\.log\(|System\.out\.print)")
BARE_EXCEPT = re.compile(r"except\s*:\s*$|except\s+\w+\s*:\s*$")
TODO = re.compile(r"(TODO|FIXME|XXX|HACK|NotImplementedError)")


SKIP_PARTS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv",
              "venv", ".sixcoder", "workspaces", "dist", "build", "target"}


def _py_files(ws: Path, limit: int = MAX_FILES) -> list[Path]:
    """扫描目标仓库的 .py 文件。

    注意：只按【相对路径分段】跳过目录。按绝对路径判断会把
    `.../prototype/workspaces/t-xxx/` 这类任务工作区整个跳掉（实测踩过）。
    """
    ws = Path(ws)
    out = []
    for p in sorted(ws.rglob("*.py")):
        try:
            rel_parts = p.relative_to(ws).parts
        except ValueError:
            continue
        if any(part in SKIP_PARTS for part in rel_parts[:-1]):
            continue
        out.append(p)
        if len(out) >= limit:
            break
    return out


def _rule_suite(ws: Path) -> list[Finding]:
    ws = Path(ws)
    findings: list[Finding] = []
    py = _py_files(ws)
    tested_src = "\n".join(
        _read(p)
        for p in py if p.name.startswith("test_") or p.name.endswith("_test.py"))
    test_reachable = bool(tested_src.strip())

    for p in py:
        rel = p.relative_to(ws).as_posix()
        text = _read(p)
        lines = text.splitlines()
        is_test = p.name.startswith("test_") or p.name.endswith("_test.py")

        for i, line in enumerate(lines, 1):
            if DEBUG_OUT.match(line):
                findings.append(Finding(
                    "P1", "debug-output", rel, i, "残留调试输出",
                    evidence=line.strip()[:120],
                    expected="生产代码不输出调试信息（用 logging）",
                    actual="直接 print / console.log",
                    suggestion="删除该行，或改为 logging.debug(...) 并在配置里关掉"))
            if TODO.search(line):
                findings.append(Finding(
                    "P2", "todo-left", rel, i, "未完成的标记",
                    evidence=line.strip()[:120],
                    expected="交付代码无 TODO/FIXME/NotImplementedError",
                    actual="存在未完成标记",
                    suggestion="实现它，或改写成明确的 NEEDS_HUMAN 说明与负责人"))

        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            findings.append(Finding("P0", "syntax", rel, exc.lineno or 1, "语法错误",
                                    evidence=exc.msg,
                                    expected="文件可被解析",
                                    actual="SyntaxError",
                                    suggestion="修复该处语法后重试"))
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                body_kinds = {type(b).__name__ for b in node.body}
                if body_kinds <= {"Pass"}:
                    findings.append(Finding(
                        "P0", "bare-except", rel, node.lineno, "裸 except/pass 吞异常",
                        evidence=f"except: pass  ({node.lineno} 行)",
                        expected="异常必须记录或重新抛出",
                        actual="静默吞掉，故障不可见",
                        suggestion="改为 except Exception as e: logger.exception(...) / raise"))

            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not is_test:
                missing = [a.arg for a in node.args.args
                           if a.arg not in ("self", "cls") and a.annotation is None]
                if missing and not node.name.startswith("_"):
                    findings.append(Finding(
                        "P2", "missing-annotation", rel, node.lineno,
                        f"函数 {node.name} 缺类型标注",
                        evidence=f"未标注参数：{', '.join(missing[:5])}",
                        expected="公开函数带类型标注（项目规约）",
                        actual="参数无标注",
                        suggestion=f"def {node.name}({', '.join(f'{a}: <type>' for a in missing[:3])}...)"))

                if len(node.body) > 50:
                    findings.append(Finding(
                        "P1", "long-function", rel, node.lineno,
                        f"函数 {node.name} 过长（{len(node.body)} 行）",
                        evidence=f"{len(node.body)} 行 > 阈值 50",
                        expected="函数 ≤50 行，单一职责",
                        actual="函数体过长",
                        suggestion="按职责拆分为私有辅助函数"))

                # 测试可达性：公开函数是否在任何测试里被引用
                if test_reachable and not node.name.startswith("_"):
                    if not re.search(rf"\b{re.escape(node.name)}\s*\(", tested_src):
                        findings.append(Finding(
                            "P1", "untested-symbol", rel, node.lineno,
                            f"函数 {node.name} 没有任何测试引用",
                            evidence="全仓测试文件中未出现该调用",
                            expected="每个公开函数至少 1 个测试",
                            actual="测试未覆盖（行为是否符合预期无从验证）",
                            suggestion=f"在 test_*.py 中补 test_{node.name}_normal / _error"))
    return findings


# ---------------------------------------------------------------- 变异存活（强信号）

def _mutation_gaps(ws: Path, max_files: int = 2, max_mutants: int = 2) -> list[Finding]:
    """对关键函数做变异：存活 = 测试没验证这个行为 = 行为可能与预期不符。

    与闸门的 gate_mutation 不同：审查时没有 diff（是全仓），
    所以直接对「函数体行范围」取变异点。
    """
    import shutil

    ws = Path(ws)
    findings: list[Finding] = []
    py = [p for p in _py_files(ws) if not (p.name.startswith("test_") or p.name.endswith("_test.py"))]
    tests = [p.relative_to(ws).as_posix() for p in _py_files(ws)
             if p.name.startswith("test_") or p.name.endswith("_test.py")]
    if not tests or not py:
        return findings

    # 大仓库保护：测试文件多时全量跑一轮要好几分钟，变异阶段直接跳过并如实说明
    if len(tests) > 8:
        findings.append(Finding(
            "P2", "mutation-skipped", "(仓库)", 0,
            f"变异测试已跳过：仓库有 {len(tests)} 个测试文件，全量执行过慢",
            evidence=f"test files = {len(tests)}",
            expected="变异测试只对单个成对测试文件运行",
            actual="已跳过（避免几十分钟的等待）",
            suggestion="在任务工作区里针对具体模块跑变异（闸门侧的 Q6 就是按配对测试文件跑的）"))
        return findings

    import time as _time
    budget_deadline = _time.time() + 120        # 变异阶段总预算 120s

    for p in py[:max_files]:
        if _time.time() > budget_deadline:
            findings.append(Finding(
                "P2", "mutation-budget", "(仓库)", 0, "变异测试超出 120s 预算，已中止",
                evidence="per-repo budget", expected="按预算截断",
                actual="已中止剩余变异", suggestion="缩小审查范围（指定更少的文件）"))
            break
        rel = p.relative_to(ws).as_posix()
        text = _read(p)
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        lines = text.splitlines()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            start, end = node.lineno - 1, node.end_lineno
            muts: list[tuple[int, str, str]] = []
            for idx in range(start, min(end, len(lines))):
                line = lines[idx]
                m = re.search(r"(?<![\w.])(\d+)(?![\w.])", line)
                if m:
                    muts.append((idx, line[:m.start()] + str(int(m.group(1)) + 1) + line[m.end():],
                                 f"L{idx+1} {m.group(1)}→{int(m.group(1))+1}"))
                if "==" in line:
                    muts.append((idx, line.replace("==", "!=", 1), f"L{idx+1} ==→!="))
                if len(muts) >= max_mutants:
                    break
            if not muts:
                continue
            killed, survived = 0, []
            try:
                for idx, mutated, label in muts:
                    patched = list(lines)
                    patched[idx] = mutated
                    tools.write_file(ws, rel, "\n".join(patched) + "\n", backup=False)
                    for sub in ws.rglob("__pycache__"):
                        shutil.rmtree(sub, ignore_errors=True)
                    if not gates.gate_tests(ws, tests, timeout=120).ok:
                        killed += 1
                    else:
                        survived.append(label)
            finally:
                tools.write_file(ws, rel, text, backup=False)
            if killed == 0:
                findings.append(Finding(
                    "P1", "mutation-survived", rel, node.lineno,
                    f"函数 {node.name} 的变异体全部存活（测试没验证它的行为）",
                    evidence=f"变异点 {[m[2] for m in muts]} 全部存活",
                    expected="改动该函数的常量/比较运算应导致测试失败",
                    actual="测试仍然全绿 → 该函数的实际行为无人校验",
                    suggestion=f"为 {node.name} 补断言：正常值、边界值、异常值各一条"))
                break          # 每个文件只报一次
    return findings


# ---------------------------------------------------------------- 模型语义比对

EXPECT_PROMPT = """你是严格的代码审查员。对比「预期功能」与「现有实现」，只输出 JSON。

输出格式：
{{"findings":[
  {{"severity":"P0|P1|P2","file":"相对路径","line":12,
    "title":"一句话问题","expected":"按预期应该怎样","actual":"现在实际怎样",
    "suggestion":"具体怎么改（可给函数级伪代码）"}}
],"summary":"总体差异概述（2 句内）"}}

审查要求：
1. 只报**能定位到文件行**的具体问题，不要泛泛而谈。
2. 特别关注「实现与预期不符」：缺功能、行为不一致、边界条件缺失、返回类型不对、异常类型不对。
3. 不确定的不要编造（宁可不报）。
4. 最多 6 条，按严重度排序。

【预期功能】
{expectation}

【代码】
{code}
"""


def _model_findings(ws: Path, expectation: str, model: str, max_files: int = 2) -> tuple[list[Finding], dict]:
    """只把最相关的少量文件送给模型，避免 4GB 卡上下文爆炸。"""
    from . import ollama

    ws = Path(ws)
    files = _py_files(ws, limit=max_files)
    if not files:
        return [], {"skipped": "no python files"}
    chunks = []
    for p in files:
        rel = p.relative_to(ws).as_posix()
        text = _read(p)
        chunks.append(f"--- {rel} ---\n" + "\n".join(
            f"{i:>3}| {l}" for i, l in enumerate(text.splitlines()[:160], 1)))
    prompt = EXPECT_PROMPT.format(expectation=expectation, code="\n\n".join(chunks))
    res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                      temperature=0.15, num_predict=700)
    data = None
    for cand in (res["text"], (re.search(r"\{.*\}", res["text"] or "", re.DOTALL) or type(
            "X", (), {"group": lambda s, i: None})()).group(0) if "{" in (res["text"] or "") else None):
        if not cand:
            continue
        try:
            import json
            data = json.loads(cand)
            break
        except Exception:
            data = None
    findings: list[Finding] = []
    if isinstance(data, dict):
        for f in (data.get("findings") or [])[:6]:
            if not isinstance(f, dict):
                continue
            findings.append(Finding(
                severity=str(f.get("severity") or "P1")[:3],
                rule="model.expectation",
                file=str(f.get("file") or "?"),
                line=int(f.get("line") or 0),
                title=str(f.get("title") or "")[:120],
                evidence="模型语义比对",
                expected=str(f.get("expected") or "")[:200],
                actual=str(f.get("actual") or "")[:200],
                suggestion=str(f.get("suggestion") or "")[:400],
                source="model",
            ))
    return findings, {"ms": res["ms"], "tokens": res["eval_count"],
                      "summary": (data or {}).get("summary", "")}


# ---------------------------------------------------------------- 入口

def review(ws: Path, expectation: str = "", model: str | None = None,
           use_model: bool = True) -> dict:
    ws = Path(ws)
    if not ws.exists():
        return {"error": f"路径不存在：{ws}"}

    findings = _rule_suite(ws)
    findings += _mutation_gaps(ws)

    model_meta: dict = {"used": False}
    if use_model and expectation.strip() and model:
        from . import ollama
        if ollama.alive():
            mf, meta = _model_findings(ws, expectation, model)
            findings += mf
            model_meta = {"used": True, **meta}

    order = {"P0": 0, "P1": 1, "P2": 2}
    findings.sort(key=lambda f: (order.get(f.severity, 3), f.file, f.line))
    counts = {k: sum(1 for f in findings if f.severity == k) for k in ("P0", "P1", "P2")}
    by_rule: dict[str, int] = {}
    for f in findings:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1

    return {
        "workspace": str(ws),
        "files_scanned": len(_py_files(ws)),
        "expectation": expectation,
        "findings": [f.to_dict() for f in findings],
        "counts": counts,
        "by_rule": by_rule,
        "model": model_meta,
        "note": "确定性规则结论可直接执行；model.expectation 条目仅为建议（docs/13：不计入闸门）",
    }
