"""闸门：语法 / 测试 / 断言有效性 / 反逃避 / 变异测试。

每个闸门返回 GateResult，evidence_kind 决定这条"通过"能算几分。
model_text 永不出现在这里的返回值中——它由 agent 单独记录，永不计入通过（docs/13 §4-S5）。
"""
from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import apikb, tools

API_KB_DB = Path(__file__).resolve().parent.parent / "data" / "api_index.db"


@dataclass
class GateResult:
    gate: str
    ok: bool
    evidence_kind: str
    evidence_ref: str = ""
    detail: str = ""
    elapsed_ms: int = 0


TEST_PAT = re.compile(r"(^|/)test_.*\.py$|.*_test\.py$")
CONFIG_FILES = {"pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", "conftest.py"}
TRIVIAL_ASSERT = re.compile(r"^\s*assert\s+(True|1|0\s*==\s*0)\s*$")
SKIP_MARK = re.compile(r"@pytest\.mark\.skip|pytest\.skip\(|self\.skipTest|@unittest\.skip")

# 「因错误原因而红」：测试自己写错了（缺依赖 import、语法错、fixture 未定义）—— 不算有效的红。
# 注意：`ImportError: cannot import name 'divide' from 'calc'` 恰恰是**有效的红**（被测功能还没实现），
# 这类"收集阶段失败"不能一律判为无效，否则测试先行永远无法通过。
INFRA_ERROR = re.compile(
    r"NameError: name '(pytest|mock|unittest|fixture|approx|hypothesis)' is not defined"
    r"|SyntaxError"
    r"|ImportError while loading conftest"
    r"|fixture '\w+' not found"
    r"|IndentationError",
    re.IGNORECASE,
)


def red_is_meaningful(output: str) -> tuple[bool, str]:
    """测试节点要求「红」，但必须是"功能不存在导致的红"，不是测试自身写错导致的红。"""
    text = output or ""
    m = INFRA_ERROR.search(text)
    if m:
        return False, f"测试因自身缺陷而失败（{m.group(0)}），不是功能缺失导致的红。请修正测试文件本身（例如 import pytest）。"
    return True, ""


def is_test_file(rel: str) -> bool:
    return bool(TEST_PAT.search(rel.replace("\\", "/")))


def _func_body(text: str, name: str) -> str:
    """取某个函数在文本中的源码片段（用于比对是否被改写）。"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            seg = ast.get_source_segment(text, node) or ""
            return seg
    return ""


# ---------------------------------------------------------------- G1 语法

def gate_syntax(ws: Path, paths: list[str]) -> GateResult:
    t0 = time.time()
    bad = []
    for rel in paths:
        if not rel.endswith(".py"):
            continue
        p = tools.safe_path(ws, rel)
        if not p.exists():
            continue
        try:
            compile(p.read_bytes(), str(p), "exec")
        except SyntaxError as exc:
            bad.append(f"{rel}:{exc.lineno} {exc.msg}")
    return GateResult(
        "syntax", not bad, "exit_code",
        evidence_ref="compile()" if not bad else "compile()",
        detail="语法通过" if not bad else "；".join(bad[:3]),
        elapsed_ms=int((time.time() - t0) * 1000),
    )


# ---------------------------------------------------------------- G5 测试

def gate_tests(ws: Path, targets: list[str] | None = None, timeout: int = 180) -> GateResult:
    t0 = time.time()
    cmd = "python -m pytest -q --tb=short -p no:cacheprovider"
    if targets:
        cmd += " " + " ".join(targets)
    rc, out = tools.run_bash(ws, cmd, timeout=timeout)
    tail = "\n".join(out.strip().splitlines()[-12:])
    ok = rc == 0
    return GateResult("tests", ok, "exit_code", evidence_ref=f"exit={rc}",
                      detail=tail, elapsed_ms=int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- 断言有效性

def gate_assertions(ws: Path, test_paths: list[str]) -> GateResult:
    t0 = time.time()
    problems = []
    checked = 0
    for rel in test_paths:
        p = tools.safe_path(ws, rel)
        if not p.exists() or not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError as exc:
            problems.append(f"{rel}: 无法解析 {exc.msg}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                checked += 1
                asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
                def _is_verify(n) -> bool:
                    if not isinstance(n, ast.Call):
                        return False
                    name = getattr(n.func, "attr", None) or getattr(n.func, "id", None) or ""
                    return name in ("raises", "assertRaises", "fail", "raises_exception")

                verifications = [n for n in ast.walk(node) if _is_verify(n)]
                if not asserts and not verifications:
                    problems.append(f"{rel}::{node.name} 无任何断言（恒真测试）")
    ok = not problems and checked > 0
    return GateResult("assertions", ok, "static_rule", evidence_ref=f"checked={checked}",
                      detail="；".join(problems[:5]) or f"{checked} 个测试均有断言",
                      elapsed_ms=int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- 反逃避（docs/12 §M7）

def gate_anti_cheat(ws: Path, node_kind: str, changed: dict[str, tuple[str, str]]) -> GateResult:
    """changed: {rel: (base_text, new_text)}"""
    t0 = time.time()
    problems: list[str] = []
    for rel, (base, new) in changed.items():
        norm = rel.replace("\\", "/")
        added = [l for l in new.splitlines() if l not in base.splitlines()]

        # 1. 实现节点改测试
        if node_kind != "test" and is_test_file(norm):
            problems.append(f"{norm}: 实现节点不得修改测试文件")
        # 2. 改构建配置绕过
        if Path(norm).name in CONFIG_FILES and node_kind != "config":
            problems.append(f"{norm}: 非 config 节点修改了测试/构建配置")
        # 3. 新增跳过标记
        for l in added:
            if SKIP_MARK.search(l):
                problems.append(f"{norm}: 新增跳过标记 `{l.strip()[:60]}`")
        # 4. 吞异常
        if node_kind != "test":
            try:
                tree = ast.parse(new)
                for h in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]:
                    body_kinds = {type(b).__name__ for b in h.body}
                    if body_kinds <= {"Pass"} and h.type is not None:
                        problems.append(f"{norm}: 裸 except/pass 吞异常")
            except SyntaxError:
                pass
        # 5. 恒真断言
        for l in added:
            if TRIVIAL_ASSERT.match(l):
                problems.append(f"{norm}: 恒真断言 `{l.strip()}`")
        # 6. 删除既有测试函数
        if is_test_file(norm):
            base_tests = set(re.findall(r"def (test_\w+)", base))
            new_tests = set(re.findall(r"def (test_\w+)", new))
            gone = base_tests - new_tests
            if gone:
                problems.append(f"{norm}: 既有测试被删除 {sorted(gone)}")
            # 7. 既有测试的函数体被改写（模型会拿新测试覆写旧测试，导致旧功能失去保护）
            for name in sorted(base_tests & new_tests):
                b = _func_body(base, name)
                n = _func_body(new, name)
                if b and n and b.strip() != n.strip():
                    problems.append(f"{norm}::{name}: 既有测试函数体被修改（只允许新增测试）")
        # 7. 注释掉原有非空行
        if base.strip() and len([l for l in new.splitlines() if l.strip()]) < len(
                [l for l in base.splitlines() if l.strip()]) * 0.5:
            problems.append(f"{norm}: 文件有效行数骤减，疑似注释/删除实现")

    ok = not problems
    return GateResult("anti_cheat", ok, "static_rule",
                      evidence_ref="7 checks",
                      detail="通过 7 项反逃避检查" if ok else "；".join(problems[:5]),
                      elapsed_ms=int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- Q6 变异测试

SIG_PAT = re.compile(r"\b([A-Za-z_]\w{2,})\s*\(([^()]*)\)\s*(?:->\s*([^,;\n]+))?")


def contract_signatures(text: str) -> dict[str, dict]:
    """从需求/契约里抽出"期望的接口"：{函数名: {n_params, required, has_return}}。

    只认像函数调用的写法（`merge_intervals(intervals: list[list[int]]) -> ...`），
    避免把普通名词误当接口。
    """
    out: dict[str, dict] = {}
    for m in SIG_PAT.finditer(text or ""):
        name, params, _ret = m.group(1), m.group(2), m.group(3)
        if name in ("if", "for", "while", "return", "assert", "print", "def", "lambda"):
            continue
        pieces = [p.strip() for p in params.split(",") if p.strip()]
        if not pieces and "(" not in (text or ""):
            continue
        required = sum(0 if ("=" in p or p.startswith("*")) else 1 for p in pieces)
        anns = []
        for p in pieces:
            ann = ""
            if ":" in p:
                ann = p.split(":", 1)[1].split("=")[0]
            anns.append(_norm_ann(ann))
        out.setdefault(name, {"n_params": len(pieces), "required": required, "anns": anns})
    return out


def _norm_ann(text: str) -> str:
    """注解归一化：去掉空格、引号、`typing.` 前缀，便于比较。"""
    s = (text or "").replace(" ", "").replace("'", "").replace('"', "")
    return s.replace("typing.", "").lower()


def gate_completeness(ws: Path, paths: list[str], contract: str) -> GateResult:
    """正向约束：契约里列出的函数/类必须**全部存在**。

    负向闸门检查"已存在的符号是否合规"，这个闸门检查"该有的在不在"——
    实测 clean_row 缺失、create_table 少参数，都属于这类正向缺失。
    """
    t0 = time.time()
    raw = re.findall(r"(?:^|[；;\n]|[-*]\s*)\s*(?:[\w./-]+\.py\s*[：:]\s*)?([A-Za-z_]\w{0,40})\s*\(",
                     contract or "")
    skip = {"if", "for", "while", "return", "print", "def", "assert", "len", "example"}
    want = [w for w in dict.fromkeys(raw) if w not in skip]
    if not want:
        return GateResult("completeness", True, "static_rule", "no-contract",
                          "契约里没有可解析的函数名", int((time.time() - t0) * 1000))
    have: set[str] = set()
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        have |= {n.name for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    missing = [w for w in want if w not in have]
    ms = int((time.time() - t0) * 1000)
    if not missing:
        return GateResult("completeness", True, "static_rule", f"ok/{len(want)}",
                          f"契约要求的 {len(want)} 个函数都存在", ms)
    return GateResult("completeness", False, "static_rule", f"缺 {len(missing)} 个",
                      "缺少契约要求的函数：" + "、".join(missing[:6]), ms)


def gate_signature(ws: Path, paths: list[str], contract: str) -> GateResult:
    """契约签名闸门：产物里的目标函数必须存在，且必填参数不多于契约声明的个数。

    实测动机：`merge_intervals` 节点全绿、状态 done，但产物多要一个参数，
    调用直接 TypeError —— 模型自己写的测试根本调不到这个形态，属于"假绿"。
    """
    t0 = time.time()
    expected = contract_signatures(contract)
    if not expected:
        return GateResult("signature", True, "static_rule", "no-contract",
                          "契约里没有可解析的接口签名，跳过", int((time.time() - t0) * 1000))
    defs: dict[str, tuple[str, int, int]] = {}      # name -> (file, required, total)
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
                total = len(args) + len(node.args.kwonlyargs)
                required = len(args) - len(node.args.defaults)
                required += sum(1 for d in node.args.kw_defaults if d is None)
                anns = []
                for a in args:
                    try:
                        anns.append(_norm_ann(ast.unparse(a.annotation)) if a.annotation else "")
                    except Exception:              # noqa: BLE001
                        anns.append("")
                defs[node.name] = (rel, required, total, anns)
    problems: list[str] = []
    checked = 0
    for name, want in expected.items():
        if name not in defs:
            continue
        checked += 1
        rel, required, total, anns = defs[name]
        if required > want["required"]:
            problems.append(f"{rel}:{name} 需要 {required} 个必填参数，契约只声明 "
                            f"{want['required']} 个（多要求的参数会让调用方 TypeError）")
        elif total < want["required"]:
            problems.append(f"{rel}:{name} 只有 {total} 个参数，契约需要 {want['required']} 个")
        else:
            # 参数个数对得上但类型被改写（实测：list[list[int]] 被写成 list[dict]）也要拦
            for i, want_ann in enumerate(want.get("anns") or []):
                got_ann = anns[i] if i < len(anns) else ""
                if want_ann and got_ann and want_ann != got_ann:
                    problems.append(f"{rel}:{name} 第 {i + 1} 个参数类型被改成 {got_ann}，"
                                    f"契约是 {want_ann}")
    if not problems:
        return GateResult("signature", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个接口与契约一致", int((time.time() - t0) * 1000))
    return GateResult("signature", False, "static_rule", f"{len(problems)} 处",
                      "；".join(problems[:3]), int((time.time() - t0) * 1000))


def _is_stub(fn: ast.AST) -> str:
    """判断函数体是不是"什么都没做"：`pass` / `...` / 只有 docstring / 只 raise NotImplementedError。"""
    body = [n for n in getattr(fn, "body", [])]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    if not body:
        return "函数体为空"
    if len(body) == 1:
        only = body[0]
        if isinstance(only, ast.Pass):
            return "只有 pass"
        if isinstance(only, ast.Expr) and isinstance(only.value, ast.Constant):
            return "只有常量表达式（疑似占位）"
        if isinstance(only, ast.Raise):
            exc = only.exc
            name = ""
            if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                name = exc.func.id
            elif isinstance(exc, ast.Name):
                name = exc.id
            if name == "NotImplementedError":
                return "只 raise NotImplementedError（未实现）"
    return ""


def _dec_name(node: ast.AST) -> str:
    """装饰器名字：`@abstractmethod` 与 `@abc.abstractmethod` 都要认。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Call):
        return _dec_name(node.func)
    return ""


def gate_stub(ws: Path, paths: list[str]) -> GateResult:
    """桩闸门：不允许把"未实现"当"完成"交上去。

    实测动机：`roman_to_int` 是 raise NotImplementedError 的桩，节点却全绿 done。
    """
    t0 = time.time()
    problems: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("__") or any(_dec_name(d) in ("abstractmethod", "overload")
                                                for d in node.decorator_list):
                continue
            checked += 1
            why = _is_stub(node)
            if why:
                problems.append(f"{rel}:{node.name} {why}")
    if not problems:
        return GateResult("stub", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个函数都有实际实现", int((time.time() - t0) * 1000))
    return GateResult("stub", False, "static_rule", f"{len(problems)} 处",
                      "；".join(problems[:4]), int((time.time() - t0) * 1000))


def gate_mutation(ws: Path, changed: dict[str, tuple[str, str]],
                  test_targets: list[str] | None, max_mutants: int = 3) -> GateResult:
    """对**本次改动所在的行**做变异，测试必须能杀死至少一个变异体。

    关键设计：
    - 只在 diff 的新增/修改行里取变异点（在无关代码上变异等于没测）
    - 变异前后清 __pycache__（同尺寸同秒的改写会让 CPython 复用旧字节码，变异静默失效）
    """
    import difflib
    import shutil

    t0 = time.time()
    sources = {rel: (base, new) for rel, (base, new) in changed.items()
               if rel.endswith(".py") and not is_test_file(rel)}
    if not sources:
        return GateResult("mutation", False, "mutation", evidence_ref="no-mutant",
                          detail="没有可变异的生产代码文件（测试文件不作变异源）")

    muts: list[tuple[str, list[str], int, str, str]] = []   # (rel, lines, idx, mutated_line, label)
    for rel, (base, new) in sources.items():
        lines = new.splitlines()
        sm = difflib.SequenceMatcher(None, base.splitlines(), lines)
        added: set[int] = set()
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag in ("insert", "replace"):
                added.update(range(j1, j2))          # 0-based
        for idx, line in enumerate(lines):
            if idx not in added:
                continue
            m = re.search(r"(?<![\w.])(\d+)(?![\w.])", line)
            if m:
                muts.append((rel, lines, idx,
                             line[:m.start()] + str(int(m.group(1)) + 1) + line[m.end():],
                             f"L{idx+1} {m.group(1)}→{int(m.group(1))+1}"))
            # 比较 / 逻辑 / 异常路径 都要覆盖：只做 == 和 < 会漏掉 `if lo > hi` 这类
            if "==" in line:
                muts.append((rel, lines, idx, line.replace("==", "!=", 1), f"L{idx+1} ==→!="))
            elif "!=" in line:
                muts.append((rel, lines, idx, line.replace("!=", "==", 1), f"L{idx+1} !=→=="))
            if ">=" in line:
                muts.append((rel, lines, idx, line.replace(">=", ">", 1), f"L{idx+1} >=→>"))
            elif ">" in line:
                muts.append((rel, lines, idx, line.replace(">", "<", 1), f"L{idx+1} >→<"))
            if "<=" in line:
                muts.append((rel, lines, idx, line.replace("<=", "<", 1), f"L{idx+1} <=→<"))
            elif re.search(r"(?<![<>!=])<(?![<=])", line):
                muts.append((rel, lines, idx,
                             re.sub(r"(?<![<>!=])<(?![<=])", "<=", line, count=1), f"L{idx+1} <→<="))
            if re.search(r"\band\b", line):
                muts.append((rel, lines, idx, re.sub(r"\band\b", "or", line, count=1), f"L{idx+1} and→or"))
            if re.search(r"\bor\b", line):
                muts.append((rel, lines, idx, re.sub(r"\bor\b", "and", line, count=1), f"L{idx+1} or→and"))
            if re.match(r"^\s*raise\b", line):
                indent = line[: len(line) - len(line.lstrip())]
                muts.append((rel, lines, idx, indent + "pass", f"L{idx+1} raise→pass"))
            m_arith = re.search(r"(?<![+\-*/])\+(?![+=])", line)
            if m_arith and "+=" not in line:
                muts.append((rel, lines, idx, line[:m_arith.start()] + "-" + line[m_arith.end():],
                             f"L{idx+1} +→-（arith +→-）"))
            m_arith2 = re.search(r"(?<![*])\*(?![*=])", line)
            if m_arith2:
                muts.append((rel, lines, idx, line[:m_arith2.start()] + "/" + line[m_arith2.end():],
                             f"L{idx+1} *→/"))
            for fn in ("min", "max"):
                if f"{fn}(" in line:
                    other = "max" if fn == "min" else "min"
                    muts.append((rel, lines, idx, line.replace(f"{fn}(", f"{other}(", 1),
                                 f"L{idx+1} {fn}→{other}"))
                    break
            if re.match(r"^\s*return\b", line) and "return None" not in line:
                indent = line[: len(line) - len(line.lstrip())]
                muts.append((rel, lines, idx, indent + "return None", f"L{idx+1} return→None"))
            if len(muts) >= max_mutants:
                break
        if len(muts) >= max_mutants:
            break

    muts = muts[:max_mutants]
    if not muts:
        return GateResult("mutation", False, "mutation", evidence_ref="no-mutant",
                          detail="改动行内没有可用的简单变异点（无数字/比较运算）")

    killed, survived = [], []
    try:
        # 基线必须先绿：测试本来就红的话，任何变异体都会"看起来被杀死了"，
        # 这种假杀伤会把"测试根本没盯住代码"掩盖成 100% 覆盖率。
        base_res = gate_tests(ws, test_targets, timeout=120)
        if not base_res.ok:
            return GateResult("mutation", False, "mutation", evidence_ref="baseline-red",
                              detail="测试基线本身就是红的（未变异时就不通过），"
                                     "此时变异结果没有意义：请先修好测试",
                              elapsed_ms=int((time.time() - t0) * 1000))
        for rel, lines, idx, mutated_line, label in muts:
            patched = list(lines)
            patched[idx] = mutated_line
            tools.write_file(ws, rel, "\n".join(patched) + "\n", backup=False)
            shutil.rmtree(ws / "__pycache__", ignore_errors=True)
            for sub in ws.rglob("__pycache__"):
                shutil.rmtree(sub, ignore_errors=True)
            res = gate_tests(ws, test_targets, timeout=120)
            (killed if not res.ok else survived).append(label)
    finally:
        for rel, (base, new) in sources.items():
            tools.write_file(ws, rel, new, backup=False)   # 还原为本次改动后的版本

    rate = len(killed) / len(muts)
    # 从"记账"变"门禁"的切法（按实测定，不是拍脑袋）：
    # 本机 4 个产物实测杀死率 67% / 33% / 33% / 100%。把 50% 当门禁会把
    # 独立验收 19/19 通过的东西判死 —— 那是拿"测试强弱"去罚"实现"。
    # 真正该拦的是**一个变异体都杀不死**：说明改动根本没被测试覆盖，属"假绿"。
    # 想更严可用 SIXCODER_MUTATION_MIN=0.5 自行收紧。
    min_kill = float(os.environ.get("SIXCODER_MUTATION_MIN", "0.0"))
    ok = len(killed) > 0 or len(muts) < 3
    if ok and len(muts) >= 3 and rate < min_kill:
        ok = False
    detail = (f"变异体 {len(muts)} 个（仅取改动行），杀死 {len(killed)}（{rate:.0%}）"
              f"{'，存活：' + '、'.join(survived) if survived else ''}")
    if not ok and len(killed) == 0:
        detail += " —— 测试杀不死任何变异体：改动没被测试真正覆盖（假绿风险）"
    return GateResult("mutation", ok, "mutation",
                      evidence_ref=f"killed={len(killed)}/{len(muts)}",
                      detail=detail, elapsed_ms=int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- G3 类型（mypy）

def gate_types(ws: Path, paths: list[str], timeout: int = 180, node_kind: str = "") -> GateResult:
    """类型闸门：mypy 检查改动文件。mypy 缺失时如实标注跳过，不假装通过。

    两条实测结论决定了它现在的形态（别改成阻塞）：
    1. 把 mypy 当**阻塞**闸门会把"能跑且测试通过"的代码大量判死（`any` 当类型、
       list invariance 之类），通过率 6/8 → 3/8。所以它只做建议。
    2. 但**测试节点**阶段（先写测试、实现还没到位）必然出现
       `Module "calc" has no attribute "divide"` 这类噪音，每条都打在时间线上会淹没有用信息。
       所以测试节点阶段先把这类"实现未到位"的建议压成一行摘要（计数保留、证据不丢），
       实现节点阶段照常列明细。
    """
    t0 = time.time()
    files = [p for p in paths if p.endswith(".py") and (Path(ws) / p).exists()]
    if not files:
        return GateResult("types", True, "static_rule", "no-file", "没有可检查的 .py 文件")
    cmd = ("python -m mypy --ignore-missing-imports --follow-imports=silent "
           "--no-error-summary --cache-dir=.sixcoder/mypy_cache " + " ".join(files))
    rc, out = tools.run_bash(ws, cmd, timeout=timeout)
    if "No module named mypy" in (out or ""):
        return GateResult("types", True, "static_rule", "mypy-missing",
                          "mypy 未安装，已跳过类型检查（不是通过）",
                          int((time.time() - t0) * 1000))
    errors = [l for l in (out or "").splitlines() if ": error:" in l]
    if rc == 0:
        return GateResult("types", True, "static_rule", "mypy-clean", "类型检查通过",
                          int((time.time() - t0) * 1000))
    # 实测教训：把 mypy 当阻塞闸门会把"能跑且测试通过"的代码大量判死
    # （`any` 当类型、list invariance 之类），通过率从 6/8 掉到 3/8。
    # 所以 types 只做**建议**（ok 恒 True），明细进证据与报告，由人决定要不要修。
    if node_kind == "test":
        pending = [e for e in errors if "has no attribute" in e or "Cannot find implementation" in e]
        if pending and len(pending) == len(errors):
            return GateResult("types", True, "static_rule", f"test-first {len(errors)}",
                              f"测试节点阶段：实现尚未到位，{len(errors)} 条类型建议先记下不铺开"
                              f"（实现节点会重新检查）",
                              int((time.time() - t0) * 1000))
    detail = f"类型建议 {len(errors)} 条（不阻塞）：" + "；".join(errors[:3])
    return GateResult("types", True, "static_rule",
                      "mypy-clean" if rc == 0 else f"suggest {len(errors)}",
                      detail, int((time.time() - t0) * 1000))


def gate_api(ws: Path, paths: list[str]) -> GateResult:
    """API 用法闸门：导入的名字 / 模块属性是否真的存在（把"幻觉 API"变成静态错误）。

    只在"确定不存在"时阻塞：静态索引 + 核心 C 模块的运行期 dir() 双重证据，
    拿不准的一律放过（宁可漏报也不误杀）。KB 未建立时如实标注跳过。
    """
    t0 = time.time()
    if not API_KB_DB.exists():
        return GateResult("api", True, "static_rule", "kb-missing",
                          "API 知识库未建立（运行 python -m core.apikb 可生成），已跳过",
                          int((time.time() - t0) * 1000))
    kb = apikb.ApiKB(API_KB_DB)
    findings: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        checked += 1
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for f in apikb.verify_source(src, kb):
            findings.append(f"{rel}: {f}")
    if not findings:
        return GateResult("api", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个文件的 API 用法均可解析",
                          int((time.time() - t0) * 1000))
    return GateResult("api", False, "static_rule", f"{len(findings)} 处",
                      "；".join(findings[:4]), int((time.time() - t0) * 1000))


DB_CALL_HINT = ("execute(", ".fetchall(", ".fetchone(", ".executemany(")


def gate_db_rows(ws: Path, paths: list[str]) -> GateResult:
    """SQL 行形态闸门：游标默认吐裸元组，`dict(row)` 必抛 TypeError。

    为什么单独一条：这是"看起来天经地义、其实只在设过 row_factory 时成立"的假设，
    模型（含 7B）反复中招；静态可判、修法唯一，属于最便宜的真判据。
    证据类别 static_rule：只在**确定**矛盾时阻塞——文件里有数据库调用、全程没有
    row_factory，却又对某个循环变量直接 dict(x)。拿不准一律放过。
    """
    t0 = time.time()
    findings: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        checked += 1
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "row_factory" in src or "sqlite3.Row" in src:
            continue
        if not any(h in src for h in DB_CALL_HINT):
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        loopvars: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.comprehension)):
                tgt = getattr(node, "target", None)
                if isinstance(tgt, ast.Name):
                    loopvars.add(tgt.id)
        if not loopvars:
            continue
        spans = [(getattr(n, "lineno", 0), getattr(n, "end_lineno", 0), n.name)
                 for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) != 1:
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "dict":
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Name) and arg.id in loopvars:
                owner = next((nm for a, b, nm in spans if a <= node.lineno <= b), "")
                where = f"{owner}() " if owner else ""
                findings.append(
                    "%s:%d %s对游标行直接 dict(%s)：未设 row_factory 时行是裸元组，必抛 "
                    "TypeError → 改 dict(zip([d[0] for d in cur.description], %s))"
                    "，或先 conn.row_factory = sqlite3.Row"
                    % (rel, node.lineno, where, arg.id, arg.id))
        # executemany 的每一行必须是**序列**：写成"内层生成器"时 sqlite3 直接
        # ProgrammingError: parameters are of unsupported type。
        # 实测（夜班 bench r1）：`[(row.get(k) for k in cols) for row in rows]` 语法过、
        # 契约判据也过，只有真连库才炸。ast 可精确判，修法唯一（内层包 tuple）。
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "executemany" and len(node.args) >= 2):
                continue
            rows_arg = node.args[1]
            # 只认"外层是列表/生成器推导、内层元素又是生成器"这一种确定形态
            if isinstance(rows_arg, (ast.ListComp, ast.GeneratorExp)) and \
                    isinstance(rows_arg.elt, (ast.GeneratorExp, ast.SetComp, ast.DictComp)):
                findings.append(
                    "%s:%d executemany 的每一行是生成器（sqlite3: parameters are of "
                    "unsupported type）→ 内层包一层 tuple(...)" % (rel, node.lineno))
    if not findings:
        return GateResult("db_rows", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个文件的游标行用法正确",
                          int((time.time() - t0) * 1000))
    return GateResult("db_rows", False, "static_rule", f"{len(findings)} 处",
                      "；".join(findings[:3]), int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- 跨模块字段一致性
def _dict_keys_touched(fn: ast.AST, arg: str = "") -> set[str]:
    """函数体里"按名字读写/判断"的字段名：row['x']=…、row.get('x')、'x' in row。"""
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript):
            val = node.value
            if isinstance(val, ast.Name) and (not arg or val.id == arg):
                sl = node.slice
                if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                    keys.add(sl.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("get", "pop", "setdefault") and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    keys.add(a0.value)
        elif isinstance(node, ast.Compare) and len(node.comparators) == 1:
            left, right = node.left, node.comparators[0]
            for a, b in ((left, right), (right, left)):
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    if isinstance(b, (ast.Name, ast.Call)):
                        keys.add(a.value)
    return keys


def gate_field_map(ws: Path, paths: list[str], table_cols: list[str]) -> list[str]:
    """跨模块字段级歧义：清洗器处理的字段名不在表列里（典型"需求没说清映射"）。

    实测来源：需求写"表列 birth"、清洗函数写"处理 date 字段"，
    两边都"按字面实现正确"，但连起来 birth 永远没被清洗。人类工程师此时会去问需求方。
    这里把这种"对不上"变成**显式问题**：只报"确定对不上"的字段，且不阻塞交付
    （阻塞会逼模型瞎猜，那个更贵）。
    """
    cols = {str(c) for c in (table_cols or [])}
    if not cols:
        return []
    out: list[str] = []
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = [a.arg for a in node.args.args]
            dictish = [a for a in args if a in ("row", "record", "item", "data", "rec")]
            if not dictish:
                continue
            touched = _dict_keys_touched(node, dictish[0])
            if not touched:
                continue
            unknown = sorted(touched - cols)
            if unknown:
                out.append(f"{rel}::{node.name}() 处理了字段 {', '.join(unknown)}，"
                           f"但表列是 {', '.join(sorted(cols))} —— 需求没说清映射"
                           f"（是同一列的别名，还是表该改列名？需甲方确认）")
                missing = sorted(cols - touched)
                if missing:
                    out.append(f"{rel}::{node.name}() 没有处理表列 {', '.join(missing)}"
                               f"（若本意是清洗它们，说明名字对不上）")
    return out


def gate_field_map_result(ws: Path, paths: list[str], contract_text: str) -> GateResult:
    """把字段一致性包装成 GateResult：永远 ok=True（不阻塞），但把问题暴露出来。"""
    t0 = time.time()
    from .props import _table_cols            # 局部导入避免循环依赖
    cols = [c for c, _ in _table_cols(contract_text or "")]
    if not cols:
        return GateResult("field_map", True, "static_rule", "no-schema",
                          "需求里没有建表语句，跳过字段一致性检查",
                          int((time.time() - t0) * 1000))
    issues = gate_field_map(ws, paths, cols)
    return GateResult("field_map", True, "static_rule",
                      f"歧义 {len(issues)} 处" if issues else "ok",
                      "；".join(issues[:3]) or "清洗字段与表列一致",
                      int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- 安全与依赖
SECRET_RE = re.compile(
    r"(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)\s*[:=]\s*"
    r"['\"][A-Za-z0-9_\-]{12,}['\"]", re.I)
UNSAFE_CALLS = {
    "eval": "eval() 直接执行字符串（代码注入风险）",
    "exec": "exec() 直接执行字符串（代码注入风险）",
    "os.system": "os.system 走 shell，命令注入风险",
    "pickle.loads": "pickle.loads 反序列化不受信数据可执行任意代码",
    "yaml.load": "yaml.load 无 Loader 不安全（用 safe_load）",
}


def gate_security(ws: Path, paths: list[str]) -> GateResult:
    """安全闸门：内置 AST 规则（离线可用），装了 bandit 再叠加。

    只报高置信问题：eval/exec、shell=True、不安全反序列化、verify=False、硬编码密钥。
    与 apikb 那条一样，宁少报也不误杀——生成的代码里这几类一旦出现几乎一定是真问题。
    """
    t0 = time.time()
    findings: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        checked += 1
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for m in SECRET_RE.finditer(src):
            line = src[:m.start()].count("\n") + 1
            findings.append(f"{rel}:{line} 疑似硬编码密钥（{m.group(0)[:24]}…）")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                base = node.func.value
                base_name = base.id if isinstance(base, ast.Name) else (
                    base.attr if isinstance(base, ast.Attribute) else "")
                name = f"{base_name}.{node.func.attr}" if base_name else node.func.attr
            if name in UNSAFE_CALLS:
                findings.append(f"{rel}:{node.lineno} {UNSAFE_CALLS[name]}")
            if name in ("subprocess.run", "subprocess.Popen", "subprocess.call",
                        "subprocess.check_output"):
                for kw in (node.keywords or []):
                    if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is True:
                        findings.append(f"{rel}:{node.lineno} subprocess shell=True（命令注入风险）")
            if name in ("requests.get", "requests.post", "httpx.get", "httpx.post",
                        "requests.request"):
                for kw in (node.keywords or []):
                    if kw.arg == "verify" and isinstance(kw.value, ast.Constant) \
                            and kw.value.value is False:
                        findings.append(f"{rel}:{node.lineno} 关闭 TLS 校验 verify=False")
    findings += _bandit_findings(ws, paths)
    if not findings:
        return GateResult("security", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个文件未发现高危模式"
                          + ("（含 bandit 复核）" if _bandit_available() else "（仅内置规则）"),
                          int((time.time() - t0) * 1000))
    return GateResult("security", False, "static_rule", f"{len(findings)} 处",
                      "；".join(findings[:8]), int((time.time() - t0) * 1000))


def _bandit_available() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("bandit") is not None
    except Exception:                                    # noqa: BLE001
        return False


def _bandit_findings(ws: Path, paths: list[str]) -> list[str]:
    """装了 bandit 就叠加它的高危项；没装就如实什么都不加（绝不假装检查过）。"""
    if not _bandit_available():
        return []
    import json as _json
    out: list[str] = []
    for rel in paths:
        if not rel.endswith(".py") or not (Path(ws) / rel).exists():
            continue
        cmd = [sys.executable, "-m", "bandit", "-q", "-f", "json", "-ll", str(Path(ws) / rel)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                  encoding="utf-8", errors="replace")
        except Exception:                                # noqa: BLE001
            return out
        try:
            data = _json.loads(proc.stdout or "{}")
        except Exception:                                # noqa: BLE001
            continue
        for item in (data.get("results") or [])[:6]:
            out.append(f"{rel}:{item.get('line_number')} bandit {item.get('test_id')} "
                       f"{(item.get('issue_text') or '')[:60]}")
    return out


DEP_PIN_RE = re.compile(r"^[A-Za-z0-9_.\-]+\s*(==|~=|>=|<=|>|<)")


def gate_deps(ws: Path) -> GateResult:
    """依赖卫生闸门：清单是否可复现（锁死版本），有 pip-audit 就叠加漏洞扫描。

    诚实边界：**没有联网就没有漏洞库**。这里只做能离线确定的事（清单卫生 + 精确版本），
    装不到 pip-audit 就如实标注"未做漏洞扫描"，绝不写"通过"。
    """
    t0 = time.time()
    ws = Path(ws)
    manifests = [p for p in ("requirements.txt", "requirements-dev.txt") if (ws / p).exists()]
    if not manifests:
        return GateResult("deps", True, "static_rule", "no-manifest",
                          "仓库里没有 requirements.txt（若用 pyproject/Poetry 需补检查）",
                          int((time.time() - t0) * 1000))
    issues: list[str] = []
    total = 0
    for name in manifests:
        text = (ws / name).read_text(encoding="utf-8-sig", errors="replace")
        for i, raw in enumerate(text.splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            total += 1
            if not DEP_PIN_RE.search(line):
                issues.append(f"{name}:{i} 未锁版本（{line[:40]}）→ 换台机器可能装出不同结果")
            elif "==" not in line:
                issues.append(f"{name}:{i} 只有范围约束（{line[:40]}）→ 建议锁到具体版本")
    issues += _pip_audit(ws)
    extra = "（未做漏洞扫描：pip-audit 未安装）" if not _pip_audit_available() else ""
    if not issues:
        return GateResult("deps", True, "static_rule", f"ok/{total}",
                          f"{total} 条依赖已锁版本{extra}", int((time.time() - t0) * 1000))
    return GateResult("deps", False, "static_rule", f"{len(issues)} 处",
                      "；".join(issues[:4]) + extra, int((time.time() - t0) * 1000))


def _pip_audit_available() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("pip_audit") is not None
    except Exception:                                    # noqa: BLE001
        return False


def _pip_audit(ws: Path) -> list[str]:
    if not _pip_audit_available():
        return []
    import json as _json
    try:
        proc = subprocess.run([sys.executable, "-m", "pip_audit", "-f", "json",
                               "--progress-spinner", "off"], cwd=str(ws),
                              capture_output=True, text=True, timeout=180,
                              encoding="utf-8", errors="replace")
    except Exception:                                    # noqa: BLE001
        return []
    try:
        data = _json.loads(proc.stdout or "{}")
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for dep in (data.get("dependencies") or []):
        for v in (dep.get("vulns") or [])[:3]:
            out.append(f"{dep.get('name')}=={dep.get('version')} 漏洞 "
                       f"{v.get('id')}（修复：{v.get('fix_versions') or '无'}）")
    return out


# ---------------------------------------------------------------- 字段名幻觉
def _dictish_params(fn: ast.AST) -> set[str]:
    """函数里"当字典用"的变量名：有 dict 注解的参数 + 实际被字符串下标过的名字。

    为什么两条都要：新写的代码有注解（契约驱动），但存量仓库里常常没有注解；
    而"用字符串下标"本身就证明它在当字典用——否则 `x["k"]` 对列表毫无意义。
    """
    args = list(getattr(fn.args, "posonlyargs", [])) + list(fn.args.args)
    names: set[str] = set()
    dict_src: set[str] = set()
    for a in args:
        ann = ""
        if a.annotation is not None:
            try:
                ann = ast.unparse(a.annotation).replace(" ", "").lower()
            except Exception:                            # noqa: BLE001
                ann = ""
        if ann in ("dict", "list[dict]", "dict[str,any]", "list[dict[str,any]]"):
            names.add(a.arg)
            dict_src.add(a.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
            names.add(node.value.id)
    for node in ast.walk(fn):
        if isinstance(node, (ast.For, ast.comprehension)):
            it = node.iter
            src = ""
            if isinstance(it, ast.Name):
                src = it.id
            elif isinstance(it, ast.Attribute):
                src = it.attr
            if src in dict_src | names:
                tgt = getattr(node, "target", None)
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
    return names


def dict_key_literals(fn: ast.AST) -> set[str]:
    """函数体里对字典做的字符串下标 / get('x')，返回用到的键名。"""
    names = _dictish_params(fn)
    keys: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id in names:
            sl = node.slice
            if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
                keys.add(sl.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in ("get", "pop", "setdefault") \
                and isinstance(node.func.value, ast.Name) and node.func.value.id in names \
                and node.args:
            a0 = node.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                keys.add(a0.value)
    return keys


def _mentions(text: str, word: str) -> bool:
    """需求/契约原文里是否把这个词当作独立单词提过（domains 不算提到 domain）。"""
    return re.search(rf"(?<![A-Za-z_]){re.escape(word)}(?![A-Za-z_])", text or "") is not None


def gate_dict_keys(ws: Path, paths: list[str], allowed: set[str],
                   contract_text: str = "") -> tuple[GateResult, list[str]]:
    """字段名幻觉闸门：函数里下标用的键必须是"契约知道存在的键"。

    实测来源（nights 4/5/6 与多次重跑）：`summarize` 里写了 `row['domain']`，
    而样例行与表里只有 age/email/birth/id/name → 运行时 KeyError。
    模型不是"推理错"，是**凭空造字段名**，这类错误静态就能判。

    两类结果分开返回，避免越权：
      - 阻塞（GateResult.ok=False）：键在契约里**从未出现过**；
      - 提示（advisory）：键在契约原文里被提到过（例如 clean_row 处理的 date 与表列 birth
        对不上，这是"需求没说清"，该问甲方，不该逼模型猜）。
    """
    t0 = time.time()
    allowed = {str(x) for x in (allowed or set())}
    if not allowed:
        return GateResult("dict_keys", True, "static_rule", "no-sample",
                          "契约没给出样例行键名，跳过字段幻觉检查",
                          int((time.time() - t0) * 1000)), []
    blocked: list[str] = []
    advisory: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        checked += 1
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # "这个函数到底在不在操作 dict 行"：找出**行变量名**，找不到就跳过这个函数。
            # 误杀实例：远端写的 _default_conn() 里返回 {'conn': ...}，被判"凭空造字段"。
            # 行变量的判定（任一）：① 参数有 dict 注解；② 对参数做 for/推导式迭代；
            # ③ 参数被 dict(...) 包过（`row = dict(row)` 这种典型写法）。
            args_all = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
            argnames = {a.arg for a in args_all}
            rowvars: set[str] = set()
            for a in args_all:
                ann = ""
                if getattr(a, "annotation", None) is not None:
                    try:
                        ann = ast.unparse(a.annotation).replace(" ", "").lower()
                    except Exception:                    # noqa: BLE001
                        ann = ""
                if "dict" in ann:
                    rowvars.add(a.arg)
            for sub in ast.walk(node):
                if isinstance(sub, ast.For) and isinstance(sub.iter, ast.Name) and \
                        sub.iter.id in argnames:
                    rowvars.add(sub.iter.id)
                if isinstance(sub, ast.comprehension) and \
                        isinstance(sub.iter, ast.Name) and sub.iter.id in argnames:
                    rowvars.add(sub.iter.id)
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) and \
                        sub.func.id == "dict" and sub.args and \
                        isinstance(sub.args[0], ast.Name) and sub.args[0].id in argnames:
                    rowvars.add(sub.args[0].id)
            if not rowvars:
                continue
            unknown = sorted(dict_key_literals(node) - allowed)
            for key in unknown:
                msg = (f"{rel}::{node.name}() 用了字段 {key!r}，但契约里的样例/表只有 "
                       f"{'/'.join(sorted(allowed))}")
                if _mentions(contract_text, key):
                    advisory.append(msg + "（契约原文提到过这个字段，属需求歧义，需甲方确认）")
                else:
                    blocked.append(msg)
    if blocked:
        return GateResult("dict_keys", False, "static_rule", f"{len(blocked)} 处",
                          "；".join(blocked[:4]) + "（这会把运行时 KeyError 提前到静态发现）",
                          int((time.time() - t0) * 1000)), advisory
    return GateResult("dict_keys", True, "static_rule", f"ok/{checked}",
                      f"{checked} 个文件的字典键均来自契约",
                      int((time.time() - t0) * 1000)), advisory


# ---------------------------------------------------------------- 重名定义
def gate_duplicates(ws: Path, paths: list[str]) -> GateResult:
    """同名定义闸门：同一个文件里不许出现两个同名的顶层 def/class。

    实测事故（链路自检）：整文件模式下模型每轮把"当前源码+新函数"整体输出一遍，
    于是 calc.py 里堆了 **7 个 divide**、还多了 def a()/def b()。
    更糟的是"函数存在且能调用"这类判据照样绿 —— 后定义的覆盖前定义的，
    看起来一切正常，实际文件已经烂了。静态就能判，代价几乎为零。
    """
    t0 = time.time()
    findings: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        checked += 1
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        seen: dict[str, int] = {}
        dups: dict[str, list[int]] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen[node.name] = seen.get(node.name, 0) + 1
                dups.setdefault(node.name, []).append(node.lineno)
        for name, lines in dups.items():
            if len(lines) > 1:
                findings.append(f"{rel} 有 {len(lines)} 个同名定义 {name}（第 "
                                f"{', '.join(str(x) for x in lines)} 行）→ 后定义会覆盖前定义")
    if not findings:
        return GateResult("duplicates", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个文件没有重名定义", int((time.time() - t0) * 1000))
    return GateResult("duplicates", False, "static_rule", f"{len(findings)} 处",
                      "；".join(findings[:3]), int((time.time() - t0) * 1000))


def gate_import_ok(ws: Path, paths: list[str], timeout: int = 60) -> GateResult:
    """导入闸门：这个模块**真的能被 import 吗**（跑一句 python -c，确定性）。

    为什么值得单开一道：注解在运行时求值，所以
    `def upsert(conn: sqlite3.Connection)` 少一行 import 会让**导入即 NameError**，
    而 pytest 只会报一堆看不出根因的 collected-error。实测 night8/r2 都栽在这里，
    架构师每轮都得自己推断"少了 sqlite3 导入"。这道闸门把它变成一句话。
    """
    t0 = time.time()
    findings: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists() or p.name.startswith("test_"):
            continue
        checked += 1
        mod = p.stem
        proc = subprocess.run([sys.executable, "-c", f"import {mod}"], cwd=str(ws),
                              capture_output=True, text=True, timeout=timeout,
                              encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                   "PYTHONDONTWRITEBYTECODE": "1"})
        if proc.returncode != 0:
            err = (proc.stderr or "").strip().splitlines()
            tail = next((ln for ln in reversed(err) if ln.strip()), "")
            findings.append(f"{rel} 导入失败：{tail[:160]}")
    if not findings:
        return GateResult("import_ok", True, "exit_code", f"ok/{checked}",
                          f"{checked} 个模块都能干净导入", int((time.time() - t0) * 1000))
    return GateResult("import_ok", False, "exit_code", f"{len(findings)} 处",
                      "；".join(findings[:3]), int((time.time() - t0) * 1000))


def _func_metrics(fn) -> dict:
    """单函数的三个可维护性指标：圈复杂度、最大嵌套深度、行数。"""
    import ast as _ast
    branch = (_ast.If, _ast.For, _ast.AsyncFor, _ast.While, _ast.Try, _ast.With, _ast.AsyncWith,
              _ast.Assert, _ast.IfExp, _ast.BoolOp)
    complexity = 1
    for node in _ast.walk(fn):
        if isinstance(node, branch):
            complexity += 1
        elif isinstance(node, _ast.comprehension):
            for cond in node.ifs:
                complexity += 1
    max_depth = 0

    def walk(node, depth: int) -> None:
        nonlocal max_depth
        max_depth = max(max_depth, depth)
        for child in _ast.iter_child_nodes(node):
            walk(child, depth + 1 if isinstance(child, (_ast.If, _ast.For, _ast.AsyncFor,
                                                        _ast.While, _ast.Try, _ast.With,
                                                        _ast.AsyncWith)) else depth)

    walk(fn, 0)
    span = (getattr(fn, "end_lineno", fn.lineno) or fn.lineno) - fn.lineno + 1
    return {"complexity": complexity, "depth": max_depth, "lines": span}


def gate_quality_metrics(ws: Path, paths: list[str], max_complexity: int = 0,
                         max_depth: int = 0, max_lines: int = 0,
                         level: str = "") -> GateResult:
    """可维护性闸门：圈复杂度 / 嵌套深度 / 单函数行数（全 ast 计算，零误判空间）。

    为什么加：判据只证明"行为对"，不证明"人能接着写"。实测夜班产物里出现过
    单函数 80+ 行、if 套 if 套 for 的写法 —— 那种代码今天能过判据，明天谁都改不动。

    **阈值按代码规模给，不搞一刀切**（见 core/scale.quality_limits）：
      S 12/4/60、M 10/4/50、L 8/3/40、XL 8/3/40。
    实测反例：night-v2 系列里 `normalize_age()` 是个"边界校验簇"，圈复杂度天然 9，
    被一刀切的 8 判红 → 整模块重写，那一次 ¥0.5031。S 级这种复杂度完全可接受。
    环境变量（SIXCODER_MAX_*）仍可覆盖，用于临时排查。
    """
    import os
    if not (max_complexity and max_depth and max_lines):
        try:
            from . import scale as _scale
            lim = _scale.quality_limits(level or _scale.resolve("", ws)["level"])
        except Exception:                                # noqa: BLE001
            lim = {"complexity": 12, "depth": 4, "lines": 60}
        max_complexity = max_complexity or lim["complexity"]
        max_depth = max_depth or lim["depth"]
        max_lines = max_lines or lim["lines"]
    max_complexity = int(os.environ.get("SIXCODER_MAX_COMPLEXITY", max_complexity))
    max_depth = int(os.environ.get("SIXCODER_MAX_DEPTH", max_depth))
    max_lines = int(os.environ.get("SIXCODER_MAX_LINES", max_lines))
    t0 = time.time()
    findings: list[str] = []
    checked = 0
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        checked += 1
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            m = _func_metrics(node)
            if m["complexity"] > max_complexity:
                findings.append(f"{rel}::{node.name}() 圈复杂度 {m['complexity']}"
                                f"（上限 {max_complexity}）")
            if m["depth"] > max_depth:
                findings.append(f"{rel}::{node.name}() 嵌套 {m['depth']} 层（上限 {max_depth}）")
            if m["lines"] > max_lines:
                findings.append(f"{rel}::{node.name}() 有 {m['lines']} 行（上限 {max_lines}，该拆单）")
    if not findings:
        return GateResult("maintainability", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个文件的函数都在阈值内"
                          f"（复杂度≤{max_complexity} 嵌套≤{max_depth} 行数≤{max_lines}）",
                          int((time.time() - t0) * 1000))
    return GateResult("maintainability", False, "static_rule", f"{len(findings)} 处",
                      "；".join(findings[:4]), int((time.time() - t0) * 1000))


def measure_quality(ws: Path, paths: list[str]) -> list[dict]:
    """只测量不判定（给报告/基准用）：列出每个函数的三个指标。"""
    rows: list[dict] = []
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                rows.append({"file": rel, "func": node.name, **_func_metrics(node)})
    return rows


# ---------------------------------------------------------------- 未定义名字
#
# 实测事故（bodies 路径）：交付的 store.py 里 `_row_values` 用的是上一轮的名字
# `_PEOPLE_COLUMNS`，而文件里定义的是 `_FIELDS`；另有一份整模块**没有 import sqlite3**。
# 两者都只在**调用时**才 NameError，所以「导入能过」「单元判据能过」，
# 已验收数照样是绿的 —— 直到 e2e 才炸。静态就能判，代价接近零。
_ALWAYS_BOUND = {
    "__file__", "__name__", "__doc__", "__spec__", "__loader__", "__package__",
    "__builtins__", "__path__", "__dict__", "__annotations__", "__class__", "__module__",
}


def _bind_all(tree: ast.AST) -> set[str]:
    """这个模块里所有被绑定的名字（不看作用域，只看"有没有定义过"）。

    宁可漏报也不误杀：Tuple/List 解包、for 目标、with as、except as、海象、
    lambda/comprehension 形参、global/nonlocal 声明，全部算"有定义"。
    """
    out: set[str] = set()

    def add_target(node: ast.AST) -> None:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name):
                out.add(sub.id)

    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
            a = n.args
            for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                out.add(x.arg)
            if a.vararg:
                out.add(a.vararg.arg)
            if a.kwarg:
                out.add(a.kwarg.arg)
        elif isinstance(n, ast.ClassDef):
            out.add(n.name)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                add_target(t)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            add_target(n.target)
        elif isinstance(n, ast.Import):
            for a in n.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                out.add(a.asname or a.name)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            add_target(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    add_target(it.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, ast.comprehension):
            add_target(n.target)
        elif isinstance(n, ast.Lambda):
            a = n.args
            for x in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
                out.add(x.arg)
            if a.vararg:
                out.add(a.vararg.arg)
            if a.kwarg:
                out.add(a.kwarg.arg)
        elif isinstance(n, ast.NamedExpr):
            add_target(n.target)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out |= set(n.names)
        elif isinstance(n, ast.arguments):        # 兜底：任何位置的形参
            for x in list(n.posonlyargs) + list(n.args) + list(n.kwonlyargs):
                out.add(x.arg)
    return out


def gate_undef(ws: Path, paths: list[str]) -> GateResult:
    """未定义名字闸门：模块里被读取、但**整个模块都没定义**、也不是内建的名字。

    这类名字只会在运行到那一行时抛 NameError，单元判据与导入常常抓不到。
    """
    t0 = time.time()
    import builtins as _b

    findings: list[str] = []
    checked = 0
    known = set(dir(_b)) | _ALWAYS_BOUND
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
            tree = ast.parse(src)
        except (OSError, SyntaxError):
            continue
        if "import *" in src or "import *," in src:
            continue                              # 星号导入 → 静态判不了，放过
        checked += 1
        bound = _bind_all(tree)
        missing: dict[str, int] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                if n.id not in bound and n.id not in known:
                    missing.setdefault(n.id, n.lineno)
        for name, line in sorted(missing.items(), key=lambda kv: kv[1]):
            findings.append(f"{rel}:{line} 用了 {name}，但本模块没有任何定义/导入")
    if not findings:
        return GateResult("undef", True, "static_rule", f"ok/{checked}",
                          f"{checked} 个文件没有未定义名字", int((time.time() - t0) * 1000))
    return GateResult("undef", False, "static_rule", f"{len(findings)} 处",
                      "；".join(findings[:3]), int((time.time() - t0) * 1000))


def gate_structure(ws: Path, paths: list[str],
                   contract_funcs: list[str] | None = None) -> GateResult:
    """交付前结构体检（一次跑完四件事，全静态、几乎不花钱）。

    1. 能不能 parse（语法）；2. 同名重复定义（后定义覆盖前定义）；
    3. 契约函数还是桩（只 raise NotImplementedError）；4. 引用了没定义的名字。

    为什么必须有：实测整文件模式 8/8「已验收」而交付的 store.py **少了一整列**
    —— 单元判据只测空输入，桩/丢常量/少列它都看不见；只有结构体检和 e2e 能看见。
    """
    t0 = time.time()
    want = set(contract_funcs or [])
    probs: list[str] = []
    for rel in paths:
        p = Path(ws) / rel
        if not rel.endswith(".py") or not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError as exc:
            probs.append(f"{rel} 语法错误：第 {exc.lineno} 行 {exc.msg}")
            continue
        seen: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                seen[node.name] = seen.get(node.name, 0) + 1
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if not want or node.name in want:      # 有契约就只盯契约函数
                        why = _is_stub(node)
                        if why:
                            probs.append(f"{rel}::{node.name} {why}")
        for name, cnt in seen.items():
            if cnt > 1:
                probs.append(f"{rel} 有 {cnt} 个同名定义 {name} → 后定义覆盖前定义")
        u = gate_undef(ws, [rel])
        if not u.ok:
            probs.append(u.detail)
    if not probs:
        return GateResult("structure", True, "static_rule", "ok",
                          "结构体检通过（语法/重名/桩/未定义名字）",
                          int((time.time() - t0) * 1000))
    return GateResult("structure", False, "static_rule", f"{len(probs)} 处",
                      "；".join(probs[:3]), int((time.time() - t0) * 1000))


# ---------------------------------------------------------------- 窄反馈
def narrow_feedback(out: str, limit: int = 1100) -> str:
    """把 pytest/构建输出压成模型能用的窄反馈：失败用例 + 断言 + 错误行。"""
    keep: list[str] = []
    for line in (out or "").splitlines():
        t = line.strip()
        if not t:
            continue
        if t.startswith("FAILED "):
            keep.append(t[:160])
        elif t.startswith("E   "):
            keep.append(t[:200])
        elif t.startswith("assert ") or ".py:" in t and " in " in t:
            keep.append(t[:160])
        if len(keep) >= 14:
            break
    if not keep:
        keep = [l.strip()[:160] for l in (out or "").strip().splitlines()[-6:]]
    text = "\n".join(dict.fromkeys(keep))
    return text[:limit]
