"""core/clarify.py — 澄清循环（开发文档 §13.2 阶段①）。

多轮 / 单文档 / 多选 / 双闸门：
  人类一句话需求 → 远端最强思考模型把框架/实现/IO/交互细节一次性全列出、
  每项给多个可选项 → 人类选完落进同一份设计文档（越改越细） →
  直到 人类满意(satisfied) + AI 判定细节足以开发(is_sufficient) 双闸门满足，进阶段②。

`complete` 是可注入的调用（签名 (system, user, effort) -> str），便于自检用假实现。
真实运行时由 `default_complete` 包裹 remote.complete（DeepSeek 用 Max+思考开；
OpenAI 兼容单档、不分"最强"，直接生成）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DesignDoc_like = "DesignDoc"


@dataclass
class Choice:
    key: str
    question: str
    options: list[str]
    picked: str = ""


@dataclass
class DesignDoc:
    requirement: str
    rounds: int = 0
    choices: list[Choice] = field(default_factory=list)
    narrative: str = ""
    satisfied: bool = False
    sufficient: bool = False
    raw_log: list[str] = field(default_factory=list)

    def open_choices(self) -> list[Choice]:
        return [c for c in self.choices if not c.picked]

    def render(self) -> str:
        """把同一份文档渲染成可给人类看的文本（含已选/待选）。"""
        lines = [f"# 设计文档（需求：{self.requirement}）", ""]
        if self.narrative:
            lines.append(self.narrative)
            lines.append("")
        lines.append("## 待定项与选择")
        if not self.choices:
            lines.append("（暂无）")
        for c in self.choices:
            mark = f"→ {c.picked}" if c.picked else "（待选）"
            lines.append(f"- **{c.question}** {mark}")
            for o in c.options:
                lines.append(f"  - {o}")
        lines.append("")
        lines.append(
            f"轮次：{self.rounds} ｜ 人类满意：{self.satisfied} ｜ AI 判定足够：{self.sufficient}"
        )
        return "\n".join(lines)


_CHOICES_RE = re.compile(r"```choices\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _strip_choices(text: str) -> str:
    return _CHOICES_RE.sub("", text).strip()


def _parse_choices(text: str) -> list[Choice]:
    """从模型输出里解析 ```choices json 块；没有就返回空（视为已无待定项）。"""
    m = _CHOICES_RE.search(text)
    if not m:
        return []
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return []
    out: list[Choice] = []
    for item in data:
        opts = item.get("options") or []
        if not opts:
            continue
        out.append(
            Choice(
                key=str(item.get("key", f"q{len(out) + 1}")),
                question=str(item.get("question", "")),
                options=[str(o) for o in opts],
            )
        )
    return out


_SYSTEM = (
    "你是资深架构师。根据用户的一句话需求，产出一份设计文档草案："
    "先写一段实现思路叙述，再用 ```choices 的 JSON 块列出所有还需要人类拍板的待定项，"
    "每项必须有 question（人类看到的问题）和 options（≥2 个可选项）。"
    "若你认为细节已足够开发、无需再问，就输出空的 choices（```choices\n[]\n```）。"
)


def _user_prompt(requirement: str, doc: DesignDoc) -> str:
    if doc.rounds == 0:
        return f"需求：{requirement}\n请产出第 1 版设计文档与待定项。"
    answered = "\n".join(
        f"- {c.question} → {c.picked}" for c in doc.choices if c.picked
    )
    return (
        f"需求：{requirement}\n已有选择：\n{answered}\n"
        f"当前叙述：\n{doc.narrative}\n"
        "请基于已选内容深化叙述，并列出剩余待定项（已选过的不要再问）。"
    )


def start(
    requirement: str,
    complete: Callable[[str, str, str], str],
    effort: str = "max",
) -> DesignDoc:
    """第 1 版设计文档（含全量待定项 + 多选）。"""
    out = complete(_SYSTEM, _user_prompt(requirement, DesignDoc(requirement)), effort)
    doc = DesignDoc(requirement=requirement, rounds=1)
    doc.narrative = _strip_choices(out)
    doc.choices = _parse_choices(out)
    doc.raw_log.append(out)
    return doc


def apply_choices(
    doc: DesignDoc,
    selections: dict[str, str],
    complete: Callable[[str, str, str], str],
    effort: str = "max",
) -> DesignDoc:
    """把人类选择落进同一份文档，返回更细版本（可能再追问）。同一份文档，迭代累积。"""
    for c in doc.choices:
        if c.key in selections and selections[c.key] in c.options:
            c.picked = selections[c.key]
    doc.rounds += 1
    out = complete(_SYSTEM, _user_prompt(doc.requirement, doc), effort)
    doc.narrative = _strip_choices(out) or doc.narrative
    existing = {c.key for c in doc.choices}
    for nc in _parse_choices(out):
        if nc.key not in existing:
            doc.choices.append(nc)
            existing.add(nc.key)
    doc.raw_log.append(out)
    return doc


def is_sufficient(
    doc: DesignDoc,
    complete: Callable[[str, str, str], str],
    effort: str = "max",
) -> bool:
    """AI 判定技术细节是否足以开发（其余可自主决定）。无待定项即足够。"""
    if not doc.open_choices():
        return True
    prompt = (
        f"需求：{doc.requirement}\n设计文档：\n{doc.render()}\n"
        "基于已选内容，剩余待定项是否都可由你自主合理决定、足以开始开发？"
        "只回答 YES 或 NO。"
    )
    ans = complete(_SYSTEM, prompt, effort)
    return "YES" in ans.upper()[:8]


def clarify_loop(
    requirement: str,
    complete: Callable[[str, str, str], str],
    human_answer: Callable[[DesignDoc], tuple[dict[str, str], bool]],
    effort: str = "max",
    max_rounds: int = 8,
) -> DesignDoc:
    """完整澄清循环。human_answer(doc) -> (selections, satisfied)。双闸门满足或到上限即停。"""
    doc = start(requirement, complete, effort)
    while doc.rounds < max_rounds:
        if not doc.open_choices():
            doc.sufficient = is_sufficient(doc, complete, effort)
        selections, satisfied = human_answer(doc)
        doc.satisfied = satisfied
        if selections:
            doc = apply_choices(doc, selections, complete, effort)
        else:
            doc.rounds += 1
        doc.sufficient = doc.sufficient or is_sufficient(doc, complete, effort)
        if doc.satisfied and doc.sufficient:
            break
    return doc


def default_complete(system: str, user: str, effort: str) -> str:
    """包裹远端：DeepSeek 用思考强度 Max+思考开；OpenAI 兼容单档直接生成。

    延迟 import，避免自检（check_clarify）也拉起远端栈。
    """
    from core import remote  # noqa: WPS433
    from core import settings_store  # noqa: WPS433

    # remote.complete 的契约：单轮用 prompt（system+user 拼成一段），
    # 思考强度走 step_index（映射到阶梯档位），没有 system/user/effort 形参。
    prompt = f"{system}\n\n{user}"
    body: dict = {"prompt": prompt, "node_id": "clarify"}
    if effort and effort != "auto":
        # effort 字符串（none/low/high/max）映射到阶梯档位 index；
        # 找不到该档（如 OpenAI 单档只有 none）就退到最强档 index。
        norm_eff = str(effort).strip().lower()
        idx = None
        for i, s in enumerate(settings_store.ladder()):
            if str(s.get("effort") or "").lower() == norm_eff:
                idx = i
                break
        if idx is None:
            idx = settings_store.strongest_step_index()
        body["step_index"] = idx
    result = remote.complete(**body)
    if isinstance(result, dict):
        return result.get("answer") or result.get("text") or ""
    return str(result)
