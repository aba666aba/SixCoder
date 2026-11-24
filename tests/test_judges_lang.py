"""多语言判据包测试（按技术栈切换验证命令）。

## 证明什么

1. 每种语言都能选到自己的验证命令（不是只支持 Python）
2. 好代码判绿、坏代码判红，且原因具体
3. 工具链没装时明确判"没法验证"，**不是判通过**
4. 不认识的语言明说 UNSUPPORTED，不编一个默认方式
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import judges as J          # noqa: E402
from core import judges_lang as JL    # noqa: E402


def _proj(files: dict) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="langjudge-"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def _ctx(ws: Path, lang: str) -> J.JudgeContext:
    return J.JudgeContext(ws=ws, extras={"lang": lang, "timeout": 120})


def test_good_python_passes():
    ws = _proj({"m.py": "def f():\n    return 1\n"})
    r = JL.judge_typecheck(_ctx(ws, "python"))
    assert r.ok is True, r.detail
    assert r.evidence_kind == "exit_code"


def test_bad_python_fails():
    ws = _proj({"m.py": "def f(\n    return 1\n"})
    r = JL.judge_typecheck(_ctx(ws, "python"))
    assert r.ok is False
    assert "SyntaxError" in r.detail or "Error" in r.detail


def test_good_javascript_passes():
    ws = _proj({"index.js": "const x = 1;\nconsole.log(x);\n"})
    r = JL.judge_typecheck(_ctx(ws, "javascript"))
    assert r.ok is True, r.detail
    assert "node" in r.detail


def test_bad_javascript_fails():
    ws = _proj({"index.js": "const x = ;\n"})
    r = JL.judge_typecheck(_ctx(ws, "javascript"))
    assert r.ok is False
    assert "SyntaxError" in r.detail


def test_good_c_passes():
    ws = _proj({"main.c": '#include <stdio.h>\nint main(void) { return 0; }\n'})
    r = JL.judge_typecheck(_ctx(ws, "c"))
    assert r.ok is True, r.detail
    assert "gcc" in r.detail


def test_bad_c_fails():
    ws = _proj({"main.c": '#include <stdio.h>\nint main(void) { printf("x") return 0; }\n'})
    r = JL.judge_typecheck(_ctx(ws, "c"))
    assert r.ok is False
    assert "error" in r.detail.lower()


def test_unknown_language_is_unsupported_not_pass():
    """不认识的语言必须判红并说 UNSUPPORTED，不能编个默认方式糊弄。"""
    ws = _proj({"x.zzz": "whatever\n"})
    r = JL.judge_typecheck(_ctx(ws, "brainfuck"))
    assert r.ok is False
    assert "不认识的语言" in r.detail


def test_missing_toolchain_is_not_pass():
    """工具链没装 = 没法验证，不是通过。这是 props.py:414 之后的口径。"""
    ws = _proj({"x.rs": "fn main() {}\n"})
    # 本机 rustc / cargo 都没装（2026-11-24 实测）
    r = JL.judge_typecheck(_ctx(ws, "rust"))
    assert r.ok is False, "工具链缺失不该判通过"
    assert r.evidence_kind == "toolchain" or "缺工具链" in r.detail


def test_no_typecheck_cmd_is_explicit_skip():
    """没有类型检查命令时，状态要说清是跳过，不伪装成验证通过。"""
    ws = _proj({"x.rb": "puts 1\n"})
    r = JL.judge_typecheck(_ctx(ws, "ruby"))
    # ruby 没装 -> 工具链缺失优先
    assert r.ok is False


def test_all_judges_applicable_to_every_stack():
    """lang.* 判据用 ANY_STACK：语言由项目决定，不由外层栈决定。"""
    reg = J.build_default_registry()
    JL.register_lang(reg)
    for stack in ("fastapi", "cli", "python", "django", "unknown_stack"):
        keys = {j.key for j in reg.for_stack(stack)}
        assert "lang.typecheck" in keys, f"{stack} 栈缺少 lang.typecheck"
        assert "lang.tests" in keys, f"{stack} 栈缺少 lang.tests"
