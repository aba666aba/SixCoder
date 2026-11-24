"""空壳测试识别：**"测试都过了"这句话什么时候是在说谎**。

## 为什么需要这一层（2026-11-23 实测）

`projects/night3` 的交付报告写「验收通过函数：8/8」，需求原文要求
「每个模块都要有 pytest 测试，覆盖正常与异常路径」。实际产物：

    test_clean.py    全文只有一行 docstring —— **0 个测试函数**
    test_report.py   只有一个断言"返回的 dict 里有 count/avg_age/domains 三个键"

在 night3 里跑 `pytest` 显示 `1 passed`。用户看到"测试通过"，但它什么都没证明：
`summarize([])` 除零崩溃、`clean_row(age=None)` 属性错误，全都没被测到。

同一个坑在前面已经踩过并写进 `gates.gate_assertions`（"无任何断言（恒真测试）"），
但那条闸门只覆盖**已有测试函数**的情况，漏掉了更常见的三种形态：

  1. 测试文件里**一个测试函数都没有**（文档字符串/stub 壳文件）→ pytest 收集到 0 个用例，
     而且如果仓库里没有别的测试，`pytest` 直接返回码 5（no tests ran），有些流程把它当成功；
  2. 测试函数**只有 `assert True` / `assert 1`**（恒真）→ 闸门 1 抓不到，因为技术上"有断言"；
  3. 测试函数体只有 `pass` / 只调用不校验。

这里补上静态判定，产出**可读的问题清单**，由 `gates.gate_assertions` 与
`shipgate` 上线体检共同使用（同一份口径，不各判各的）。

## 边界（诚实说明）

本模块只看 AST，不做语义分析：
    · 它证明"有测试在真的断言"，**不证明断言写对了**（后者要靠变异测试 / oracle）；
    · `assert helper(x)` 这类委托型断言算"有验证"，不深挖 helper 内部；
    · 只认 pytest 风格（`test_*` 函数、`TestXxx` 方法）。其它框架如实返回 unverifiable。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# 恒真断言：技术上"有 assert"，但永远为真，等于没测。
TRIVIAL_ASSERTS = frozenset({"True", "1", "0 == 0", "1 == 1", "None is None"})

# 校验型调用：pytest.raises 这类即使没有 assert 也算一次真验证。
VERIFY_CALLS = frozenset({"raises", "assertRaises", "assertRaisesRegex", "fail",
                          "raises_exception", "assertRaisesMessage"})


@dataclass
class TestFileReport:
    """单个测试文件的静态体检结果。"""

    path: str
    tests: int = 0                       # 测试函数个数
    verified: int = 0                    # 至少有一处真断言的测试函数个数
    problems: list[str] = field(default_factory=list)   # 可读的"哪里是空壳"
    parse_error: str = ""

    @property
    def shell(self) -> bool:
        """整份文件是空壳：没有测试，或者所有测试都没有真断言。"""
        if self.parse_error:
            return True
        return self.tests == 0 or self.verified == 0


def _is_trivial(node: ast.Assert) -> bool:
    """`assert True` / `assert 1` / `assert 0 == 0` 这类恒真断言。"""
    test = node.test
    if isinstance(test, ast.Constant):
        return bool(test.value)
    try:
        return ast.unparse(test).replace(" ", "") in {s.replace(" ", "") for s in TRIVIAL_ASSERTS}
    except Exception:                                    # noqa: BLE001
        return False


def _is_verify_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = getattr(node.func, "attr", None) or getattr(node.func, "id", None) or ""
    return name in VERIFY_CALLS


def _body_is_empty(node: ast.AST) -> bool:
    """函数体除了 docstring 就只剩 pass/...（壳函数）。"""
    body = [b for b in getattr(node, "body", [])
            if not (isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant))]
    if not body:
        return True
    return all(isinstance(b, ast.Pass) or
               (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and
                getattr(b.value, "value", None) is Ellipsis)
               for b in body)


def inspect_test_file(path: Path, rel: str = "") -> TestFileReport:
    """静态体检一个测试文件。解析不了也如实返回（不抛异常）。"""
    name = rel or path.name
    rep = TestFileReport(path=name)
    try:
        src = path.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src)
    except SyntaxError as exc:
        rep.parse_error = f"无法解析（第 {exc.lineno} 行）：{exc.msg}"
        rep.problems.append(f"{name}: {rep.parse_error}")
        return rep
    except OSError as exc:
        rep.parse_error = f"读不了：{exc}"
        rep.problems.append(f"{name}: {rep.parse_error}")
        return rep

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test"):
            continue
        rep.tests += 1
        if _body_is_empty(node):
            rep.problems.append(f"{name}::{node.name} 函数体是空的（只有 pass/docstring）")
            continue
        asserts = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
        real = [a for a in asserts if not _is_trivial(a)]
        verifies = [n for n in ast.walk(node) if _is_verify_call(n)]
        if not real and not verifies:
            if asserts:
                rep.problems.append(f"{name}::{node.name} 只有恒真断言（assert True 之类），等于没测")
            else:
                rep.problems.append(f"{name}::{node.name} 没有任何断言（恒真测试）")
            continue
        rep.verified += 1

    if rep.tests == 0:
        rep.problems.append(f"{name}: 测试文件里一个测试函数都没有（pytest 收集到 0 个用例）")
    return rep


def scan(ws: Path, patterns: tuple[str, ...] = ("test_*.py", "*_test.py")) -> dict:
    """扫整个工作区，返回 {files: [TestFileReport...], tests, verified, shell_files, problems}。

    `shell_files` 是**完全空壳**的测试文件名（一个真断言都没有）；
    `problems` 是可直接塞进交付报告给人看的句子。
    """
    ws = Path(ws)
    seen: set[Path] = set()
    files: list[TestFileReport] = []
    for pat in patterns:
        for p in sorted(ws.glob(pat)):
            if p in seen or p.name.startswith("."):
                continue
            seen.add(p)
            files.append(inspect_test_file(p, p.name))
    tests = sum(f.tests for f in files)
    verified = sum(f.verified for f in files)
    shell_files = [f.path for f in files if f.shell]
    problems = [p for f in files for p in f.problems]
    if not files:
        problems = ["仓库里没有任何测试文件（test_*.py / *_test.py）"]
    elif tests == 0:
        problems = ["有测试文件但一个测试函数都没有"] + problems
    return {
        "files": files,
        "file_count": len(files),
        "tests": tests,
        "verified": verified,
        "shell_files": shell_files,
        "problems": problems,
        # 一句话结论（给交付报告/上线体检直接用）
        "verdict": _verdict(len(files), tests, verified, shell_files),
    }


def _verdict(file_count: int, tests: int, verified: int, shell: list[str]) -> str:
    if file_count == 0:
        return "没有测试"
    if tests == 0:
        return "测试文件是空壳：0 个测试函数"
    if verified == 0:
        return f"测试全是空壳：{tests} 个测试函数没有一个真断言"
    if shell:
        return f"{verified}/{tests} 个测试有真断言，其中 {len(shell)} 个文件完全空壳"
    return f"{verified}/{tests} 个测试有真断言"


def summarize(result: dict, limit: int = 3) -> str:
    """压成一行，给闸门 detail 用。"""
    v = result["verdict"]
    probs = result.get("problems") or []
    if not probs:
        return v
    return v + "：" + "；".join(probs[:limit])
