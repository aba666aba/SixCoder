"""给 CLI/接口用的"提问 → 回答"薄封装（避免各处重复拼路径）。

跑法示例（CLI）：
    python cli.py questions --repo <工作区>
    python cli.py answer <qid> "跳过" --repo <工作区>
"""
from __future__ import annotations

from pathlib import Path

from . import questions


def _ws(repo: str) -> Path:
    return Path(repo)


def list_pending(repo: str) -> list[dict]:
    return questions.pending(_ws(repo))


def answer(repo: str, qid: str, text: str, by: str = "cli") -> dict:
    return questions.answer(_ws(repo), qid, text, by=by)


def plan_review(repo: str, plan: str, mode: str) -> dict:
    return questions.review_plan(_ws(repo), plan, mode=mode)
