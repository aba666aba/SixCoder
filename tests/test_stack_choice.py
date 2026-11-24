"""技术栈决策落盘测试（澄清 → stack.json）。

## 解决什么

clarify 的提示词现在强制第一项是技术栈，用户选完后必须写进
.sixcoder/stack.json —— 否则后面的生成、判据、闸门都在按默认 fastapi 猜。

用户明明选了 CLI，生成侧仍按 Web 三分层走，这是最隐蔽的一类错。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import auto, clarify   # noqa: E402


class _C:
    def __init__(self, key, picked, options=None, question="?"):
        self.key = key
        self.picked = picked
        self.options = options or []
        self.question = question


class _D:
    def __init__(self, choices):
        self.choices = choices


def _ws() -> Path:
    d = Path(tempfile.mkdtemp(prefix="stackpick-"))
    (d / ".sixcoder").mkdir(parents=True, exist_ok=True)
    return d


def _stack_of(ws: Path) -> str | None:
    p = ws / ".sixcoder" / "stack.json"
    if not p.exists():
        return None
    return str(json.loads(p.read_text(encoding="utf-8")).get("stack") or "") or None


def test_cli_choice_is_persisted():
    """关键回归：cli 有 layerplan 支持但没有 stack 适配器，
    早先会被判"不认识的栈"。必须能落盘。"""
    ws = _ws()
    r = auto._persist_stack_choice(ws, _D([_C("stack", "cli：命令行工具，单层入口+子命令")]))
    assert r.get("ok") is True, r
    # 2026-11-24 打通：短名 cli 会映射到生态包名 cli_tool（真适配器/真探针所在）
    assert r.get("stack") == "cli_tool", r
    assert _stack_of(ws) == "cli_tool"


def test_fastapi_choice_persisted_via_adapter():
    ws = _ws()
    r = auto._persist_stack_choice(ws, _D([_C("stack", "fastapi：Python Web 后端")]))
    assert r.get("ok") is True
    assert _stack_of(ws) == "fastapi"


def test_colon_variants_accepted():
    """中英文冒号都要认（模型输出不稳定）。"""
    for text in ("cli：说明", "cli: 说明", "CLI：说明"):
        ws = _ws()
        r = auto._persist_stack_choice(ws, _D([_C("stack", text)]))
        assert r.get("ok") is True, f"{text!r} -> {r}"
        assert _stack_of(ws) == "cli_tool", f"{text!r} -> {_stack_of(ws)}"


def test_unsupported_choice_is_not_persisted():
    """\"不支持：...\" 是边界提示，不是可落盘的栈。"""
    ws = _ws()
    r = auto._persist_stack_choice(ws, _D([_C("stack", "不支持：桌面程序需要 GUI 框架")]))
    assert r.get("ok") is False
    assert "超出引擎能力" in r.get("why", "")
    assert _stack_of(ws) is None


def test_unknown_stack_rejected():
    ws = _ws()
    r = auto._persist_stack_choice(ws, _D([_C("stack", "rusty：Rust 后端")]))
    assert r.get("ok") is False
    # 2026-11-24：文案从"不认识"改成"引擎还没有 X"，
    # 并且现在会登记 pending 请求（让协商阶段去补全）。
    assert "引擎还没有" in r.get("why", ""), r.get("why")
    assert r.get("pending_ecosystem") is True, "应标记为待协商"
    assert _stack_of(ws) is None


def test_no_choice_returns_reason():
    ws = _ws()
    assert auto._persist_stack_choice(ws, _D([])).get("ok") is False
    assert auto._persist_stack_choice(ws, _D([_C("stack", "")])).get("ok") is False
    assert _stack_of(ws) is None


def test_clarify_prompt_mentions_stack_first():
    """提示词必须要求第一项是技术栈，否则模型不会稳定产出。"""
    sys_txt = clarify._SYSTEM
    assert "stack" in sys_txt
    assert "第一项" in sys_txt
    assert "不支持" in sys_txt, "要告诉模型超出能力时怎么写"


def test_user_prompt_lists_available_stacks():
    """可用栈从 layerplan 动态取 —— 加一个 LayerPlan 可选范围自动变宽。"""
    from core.clarify import DesignDoc
    prompt = clarify._user_prompt("做个工具", DesignDoc(requirement="做个工具"))
    for st in ("cli", "fastapi", "django", "frontend"):
        assert st in prompt, f"提示词里没有 {st}"
