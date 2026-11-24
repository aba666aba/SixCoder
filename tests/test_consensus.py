"""协商全票制测试（2026-11-24）。

## 用户定稿

  "所有协商的必须全部同意才行，不是 ≥2 了"
  "协商机制是最后底牌，意见不一致时候需要远端举例说明，其他远端才能考虑到更多问题"

## 改了什么

改前：任一选项 >=2 票即成决议（多数决）；复审 agree >=2 即通过。
改后：**必须三方全票**（写作者 + 两名审核者都投同一选项）；
      复审必须所有在席审核者都 pass。

为什么：多数决让 2:1 强行通过，反对那一票就没人看了。
而协商的价值恰恰在少数派的视角 —— 它可能就是唯一看到
"这个判据和业务互斥"的人。压过去等于白协商。

代价是一票反对就卡住，所以配套要求**反对必须举证**：
dissenting_case（凭什么）+ examples（可复现场景）+ risks_seen（别人可能漏掉的）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import deadlock   # noqa: E402


def _src() -> str:
    return (ROOT / "prototype" / "core" / "deadlock.py").read_text(encoding="utf-8")


def test_decision_requires_unanimous():
    """决议判定必须是全票，不是 >=2。"""
    s = _src()
    # 决议：tally == 总人数
    assert "== _total" in s, "决议判定应改成全票（tally.get(opt) == _total）"
    # 不允许残留旧的多数决写法
    assert "if tally.get(opt, 0) >= 2:" not in s, "仍有旧的 >=2 多数决"


def test_review_requires_unanimous():
    """复审判定也必须是全票。"""
    s = _src()
    assert "agree >= len(votes)" in s, "复审应改成全票"
    assert "resolved = agree >= 2" not in s, "仍有旧的复审多数决"


def test_dissent_requires_evidence():
    """反对必须有举证字段。"""
    s = _src()
    for field in ("dissenting_case", "examples", "risks_seen"):
        assert field in s, "缺少举证字段：" + field


def test_prompt_asks_for_examples():
    """提示词里要明确要求举例。"""
    s = _src()
    assert "反对必须举证" in s, "提示词里没有举证要求段落"
    assert "可复现" in s or "具体场景" in s, "没有要求给具体例子"


def test_env_install_still_unanimous():
    """install_env / install_ecosystem 本来就是 3/3，不能被改坏。"""
    s = _src()
    assert "decide_env_install" in s
    # 全票制的说明还在
    assert "全票" in s


def test_no_evidence_flag_exists():
    """投反对但没举证 -> 要能识别出来（不能悄悄吞掉）。"""
    s = _src()
    assert "no_evidence" in s, "缺少 no_evidence 标记"
