"""Judge 判据接口契约测试（阶段 1）。

钉住三件事：
  1. 注册表能按栈选对判据（CLI 项目不会被 HTTP 判据误判）；
  2. LegacyJudge 包出来的老闸门**行为与直接调用一致**（回归铁律）；
  3. 没有判据的栈返回空表，**不猜、不默认通过**（幻觉温床的反面）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import gates as G          # noqa: E402
from core import judges as J         # noqa: E402


def _mkws(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="judges-"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def test_registry_selects_by_stack():
    reg = J.build_default_registry()

    web = {j.key for j in reg.for_stack(J.STACK_FASTAPI)}
    cli = {j.key for j in reg.for_stack(J.STACK_CLI)}

    # 栈无关的判据两边都有
    assert "syntax" in web and "syntax" in cli
    assert "anti_cheat" in web and "anti_cheat" in cli
    assert "stub" in web and "stub" in cli

    # HTTP 专属判据**只在 web 栈出现**
    for http_only in ("db_rows", "api", "security", "field_map"):
        assert http_only in web, f"{http_only} 应在 web 栈里"
        assert http_only not in cli, (
            f"{http_only} 是 HTTP 判据，不该出现在 CLI 栈 —— "
            "否则 CLI 项目会被误判为红（这正是固化问题）"
        )


def test_unknown_stack_yields_no_http_judges():
    """没见过的栈：只拿到栈无关判据，不猜。"""
    reg = J.build_default_registry()
    unknown = {j.key for j in reg.for_stack("some_future_stack")}
    assert "syntax" in unknown
    assert "api" not in unknown and "db_rows" not in unknown


def test_empty_registry_returns_empty_not_pass():
    """空注册表 = 没有判据，必须返回空表，绝不能默认"全通过"。"""
    reg = J.JudgeRegistry()
    assert reg.for_stack(J.STACK_FASTAPI) == []


def test_for_stack_is_stable_ordered():
    """顺序必须稳定（按 key 排序）—— 否则每轮提示词漂移，缓存失效。"""
    reg = J.build_default_registry()
    a = [j.key for j in reg.for_stack(J.STACK_FASTAPI)]
    b = [j.key for j in reg.for_stack(J.STACK_FASTAPI)]
    assert a == b
    assert a == sorted(a)


def test_legacy_judge_matches_direct_call():
    """LegacyJudge 包出来必须与直接调闸门**逐字段一致**（回归铁律）。"""
    ws = _mkws({
        "ok.py": "def f():\n    return 1\n",
        "stub.py": "def g():\n    raise NotImplementedError\n",
    })
    reg = J.build_default_registry()
    judge = reg.get("stub")
    assert judge is not None

    direct = G.gate_stub(ws, ["ok.py", "stub.py"])
    wrapped = judge.check(J.JudgeContext(ws=ws, paths=("ok.py", "stub.py")))

    for field_name in ("gate", "ok", "evidence_kind", "evidence_ref", "detail"):
        assert getattr(wrapped, field_name) == getattr(direct, field_name), (
            f"{field_name} 不一致：包装后 {getattr(wrapped, field_name)!r} "
            f"vs 直接 {getattr(direct, field_name)!r}"
        )


def test_completeness_judge_passes_contract():
    """注意：gate_completeness 的抽取规则要求函数名在
    行首 / 分号 / 换行 / 列表符号之后（见 gates.py:294 的正则）。
    "a() 与 b()" 里的 b 前面是"与"，抽不到 —— 这是既有行为，不改（回归铁律）。
    所以测试用换行分隔的契约写法。"""
    ws = _mkws({"m.py": "def a():\n    return 1\n\ndef b():\n    return 2\n"})
    reg = J.build_default_registry()
    judge = reg.get("completeness")

    ok = judge.check(J.JudgeContext(ws=ws, paths=("m.py",), contract="a()\nb()"))
    assert ok.ok is True, ok.detail

    bad = judge.check(J.JudgeContext(ws=ws, paths=("m.py",), contract="a()\nzzz()"))
    assert bad.ok is False, bad.detail
    assert "zzz" in bad.detail


def test_completeness_extraction_follows_existing_rule():
    """钉住既有抽取规则，防止阶段 3 重写时无意改变行为。"""
    from core import gates as GG
    ws = _mkws({"m.py": "def a():\n    return 1\n"})
    # 换行分隔 -> 两个都抽到 -> 缺 zzz 判红
    assert GG.gate_completeness(ws, ["m.py"], "a()\nzzz()").ok is False
    # "与"连接 -> 只抽到 a -> 通过（既有行为，不动）
    assert GG.gate_completeness(ws, ["m.py"], "a() 与 zzz()").ok is True


def test_missing_context_arg_raises_loudly():
    """上下文缺参数时必须报错，不能静默传 None 下去（那会产出假结果）。"""
    reg = J.build_default_registry()
    judge = reg.get("completeness")
    try:
        judge.check(J.JudgeContext(ws=_mkws({}), paths=(), contract=""))  # contract 有默认
    except KeyError:
        pass
    # completeness 的 contract 在 arg_names 里，context 有默认值，所以不该抛；
    # 用真正缺参数的场景：extras 里没有的 arg
    bad_judge = J.LegacyJudge(
        "needs_extras", lambda x: G.GateResult("x", True, "t"),
        frozenset({J.ANY_STACK}), ("ws", "totally_missing"),
    )
    try:
        bad_judge.check(J.JudgeContext(ws=Path(tempfile.mkdtemp())))
    except KeyError as exc:
        assert "totally_missing" in str(exc)
    else:
        raise AssertionError("缺参数却没报错 —— 会静默产出假结果")


def test_applies_to_cannot_be_empty():
    reg = J.JudgeRegistry()
    try:
        reg.register(J.LegacyJudge("bad", lambda *a: None, frozenset()))
    except ValueError as exc:
        assert "applies_to" in str(exc)
    else:
        raise AssertionError("空 applies_to 应报错")


def test_duplicate_key_rejected():
    reg = J.JudgeRegistry()
    reg.register(J.LegacyJudge("dup", lambda *a: None, frozenset({J.ANY_STACK})))
    try:
        reg.register(J.LegacyJudge("dup", lambda *a: None, frozenset({J.ANY_STACK})))
    except ValueError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("重复 key 应报错")