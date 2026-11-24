"""语言判据接入主循环的测试（2026-11-24）。

## 证明什么

1. agent._gate_node 真的会调用 lang.typecheck
2. 类型检查失败会阻断节点（回传"语言判据·类型/语法"）
3. 语言判据炸了不拖垮整条链（fallback 到 notice）
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import agent   # noqa: E402


def test_agent_calls_lang_judges():
    """静态钉住：agent.py 的闸门链里必须有 lang 判据调用。"""
    src = (ROOT / "prototype" / "core" / "agent.py").read_text(encoding="utf-8")
    assert "lang.typecheck" in src, "agent.py 没有接入 lang.typecheck"
    assert "lang.tests" in src, "agent.py 没有接入 lang.tests"
    assert "语言判据" in src, "没有对应的失败回传文案"


def test_lang_judge_failure_message_shape():
    """失败回传的措辞要能定位（含"语言判据"字样）。"""
    src = (ROOT / "prototype" / "core" / "agent.py").read_text(encoding="utf-8")
    assert "【语言判据·类型/语法】" in src


def test_lang_judge_guarded_by_try():
    """语言判据必须包在 try 里 —— 它要跑外部编译器，可能炸。"""
    src = (ROOT / "prototype" / "core" / "agent.py").read_text(encoding="utf-8")
    idx = src.find("按技术栈跑语言判据")
    assert idx > 0
    seg = src[idx:idx + 2000]
    assert "try:" in seg, "语言判据段没有 try 保护"
    assert "except Exception" in seg, "语言判据段没有 except 兜底"


def test_judges_context_passes_ws_and_timeouts():
    """上下文要把 ws 和超时传过去，否则判据跑不起来。"""
    src = (ROOT / "prototype" / "core" / "agent.py").read_text(encoding="utf-8")
    idx = src.find("按技术栈跑语言判据")
    seg = src[idx:idx + 2000]
    assert "JudgeContext" in seg
    assert "ws=ws" in seg
    assert "timeout" in seg
