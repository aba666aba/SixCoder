"""模型写的"任务简报"：把提示词里"要做什么"这一段交给模型写，但**规则骨架不许它动**。

甲方的直觉：问题的精确描述由"看过证据的模型"来写，会比人预先写死的模板更准。
这个判断是对的——但有个前提：**约束不能被模型改写**。

所以这里做了分工（对齐 docs/13 判定权分离）：

  引擎持有（不许模型改）              模型撰写（每单一份，机械校验后注入）
  ──────────────────────           ──────────────────────────────────
  输出格式（JSON/符号级 edits）        这个符号具体该怎么改
  铁律（只改目标符号、不整文件）       必须满足什么（从契约/证据里提炼）
  契约原文（签名/类型/异常/示例）      证据指向的真实错误是哪一条
  判据与闸门                          一句话做法（how）

校验不过就**丢弃**（回落到纯模板，行为与今天完全一致）——宁可不用，也不放一条幻觉指令进提示词。

校验项（全部机械可判）：
  1. symbol 必须等于工作单的 symbol；
  2. briefing 里引用的每条 evidence 必须是**证据原文的子串**（防编造）；
  3. 不许出现"输出整文件 / 删测试 / 忽略判据 / 跳过测试"这类改写规则的指令；
  4. 长度上限（防长篇水话挤掉真实上下文）。
"""
from __future__ import annotations

from . import tokens as _tokens  # noqa: E402

import hashlib
import json
import re
from pathlib import Path

from . import remote

MAX_CHARS = 700
FORBIDDEN = (
    "输出整文件", "整文件输出", "输出整个文件", "不需要 docstring", "不用写 docstring",
    "删除测试", "删掉测试", "去掉测试", "忽略判据", "跳过测试", "不用管测试",
    "忽略错误", "改其它文件", "改其他文件", "整个文件重写", "whole file", "delete test",
    "ignore test", "skip test",
)
PHRASE_KEYS = ("must", "evidence", "how")

PROMPT = """你在为一个"单符号改写"任务写**任务简报**。执行者是本地小模型，只能改一个符号。
请只输出 JSON（不要解释、不要围栏）：

{{"symbol": "<目标符号名>", "must": ["这条实现必须满足的要求（从契约/证据里提炼，2-4 条）"],
 "evidence": ["证据原文里的一句（必须逐字取自下面给出的证据，不要改写）"],
 "how": "一句话说清怎么做（提示关键 API/写法，≤60 字）"}}

硬性要求：
- evidence 里的每一条都必须是下面"失败证据"原文的**逐字子串**，否则整份简报会被丢弃。
- 不要写"输出整文件""删测试""忽略判据"这类话——那会改掉任务的规则，简报会被丢弃。
- 不要重复契约原文的大段内容，只说"这次要做什么"。

【目标符号】{symbol}
【契约】{contract}
【失败证据】
{evidence}
【目标符号现状】
{snippet}
"""


def _cache_key(*parts: str) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


_CACHE: dict[str, dict] = {}


def validate(briefing: dict, symbol: str, evidence_text: str) -> tuple[bool, str]:
    """机械校验：见模块文档的 4 条。返回 (通过?, 原因)。"""
    if not isinstance(briefing, dict):
        return False, "不是 JSON 对象"
    got = str(briefing.get("symbol") or "").strip()
    if symbol and got != symbol:
        return False, f"symbol 不符（{got or '空'} ≠ {symbol}）"
    ev = evidence_text or ""
    items = briefing.get("evidence")
    if not isinstance(items, list) or not items:
        return False, "没有引用任何证据"
    for item in items:
        s = str(item).strip()
        if not s:
            return False, "evidence 里有空项"
        if s not in ev:
            return False, f"evidence 不是原文子串（可能是编的）：{s[:40]}"
    blob = json.dumps(briefing, ensure_ascii=False)
    low = blob.lower()
    for bad in FORBIDDEN:
        if bad.lower() in low:
            return False, f"出现改写规则的指令：{bad}"
    if len(blob) > MAX_CHARS:
        return False, f"太长（{len(blob)} > {MAX_CHARS}）"
    if not str(briefing.get("how") or "").strip() and not briefing.get("must"):
        return False, "既没 how 也没 must，等于没写"
    return True, "ok"


def render(briefing: dict) -> str:
    """把已校验的简报渲染成提示词里的一段（不含任何引擎规则）。"""
    lines = ["【任务简报（模型撰写 · 已机械校验：证据逐字取自原文）】"]
    for item in (briefing.get("must") or [])[:4]:
        lines.append(f"- 必须：{str(item)[:160]}")
    for item in (briefing.get("evidence") or [])[:2]:
        lines.append(f"- 证据：{str(item)[:160]}")
    how = str(briefing.get("how") or "").strip()
    if how:
        lines.append(f"- 做法：{how[:160]}")
    return "\n".join(lines)


def generate(ws: Path, symbol: str, contract: str, evidence: str, snippet: str = "",
             author_model: str = "", task_id: str = "") -> dict:
    """生成并校验简报。任何一步不过 → {"ok": False}，调用方直接用模板（fail-safe）。"""
    if not symbol:
        return {"ok": False, "reason": "没有目标符号，简报无用"}
    if not (evidence or "").strip():
        return {"ok": False, "reason": "没有失败证据，简报无从引用"}
    key = _cache_key(symbol, contract[:400], evidence[:400])
    if key in _CACHE:
        return _CACHE[key]

    prompt = PROMPT.format(symbol=symbol, contract=(contract or "")[:800],
                           evidence=_tokens.cut(evidence or "", 1200), snippet=_tokens.cut(snippet or "", 1500))
    # 简报优先让远端写（质量高、便宜、快）；远端不可用再退回本地
    result = {"ok": False, "reason": "未生成"}
    ok_r, why = remote.available()
    if ok_r:
        r = remote.complete(prompt, task_id=task_id, node_id=f"briefing:{symbol}",
                            workspace=str(ws), reuse_scope=f"{task_id}:briefing")
        if r.get("ok"):
            data = _parse(r.get("answer") or "")
            passed, why2 = validate(data, symbol, evidence)
            if passed:
                result = {"ok": True, "briefing": data, "by": "remote",
                          "text": render(data)}
            else:
                result = {"ok": False, "reason": f"远端简报未通过校验：{why2}"}
    else:
        result = {"ok": False, "reason": f"远端不可用（{why}），简报跳过"}
    _CACHE[key] = result
    return result


def _parse(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict):
                return obj
        except Exception:                                # noqa: BLE001
            continue
    return {}
