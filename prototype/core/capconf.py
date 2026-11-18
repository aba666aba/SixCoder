"""能力描述 ↔ 探针断言的**取值冲突**检测（§11.4m，2026-11-11）。

为什么要有这个模块：
    实测（切片 s33）：能力描述写 `status=pending_teacher`，
    而引擎合成契约时取 `status='created'`（契约文本里明写"覆盖能力描述中的 pending_teacher"）。
    **两处口径不一致** → 下游全都可能读到矛盾的说法：
      · 契约合成：远端看到能力描述与路由探针，私自"覆盖"了一个值；
      · 诊断架构师：拿到"返回的 status 是 'created' 而不是要求的 'created'"
        —— **一句自相矛盾的话**（实测日志原文），因为它同时读到了两个口径；
      · 写码：远端按契约写，探针按契约判 → 这条反而对；
        但**能力表里的 desc 仍是旧的** → 报告/摘要/下一个切片又拿到旧口径。
    根子：**同一个字段有两个权威源**。

口径（纯函数、零 token、确定性）：
    · 能力描述里 `key=value` 形式的**字面量** vs 探针 `expect_json` 里的同名 key；
    · 只有**字符串值**不同才算冲突（数字/布尔不同 = 真 bug，不许"对齐"掉）；
    · 冲突时给出**以探针为准**的明确口径 —— 探针是**唯一会真正跑**的东西，
      它说了算；能力描述是人话，写错不影响判据但会误导所有读它的人。

不做的事：不改能力表（那是蓝图的事实来源，改它要经重规划），
只**把冲突显式化**，让简报/契约/诊断都看到同一个口径。
"""
from __future__ import annotations

import re

# 能力描述里 `key=value` 的取法：值可以是带引号的字面量，**也可以是裸标识符**
# （实测能力描述写的是 `status=pending_teacher`，没引号）。
# 但要排除**布尔/数字** —— 那两种不同 = 真 bug，不许"对齐"掉。
_DESC_PAIR = re.compile(
    r"(?<![\w.])(\w+)\s*=\s*"
    r"('[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_]*)"
)

# 这些形态**不算**字符串取值（数字/布尔/None → 数字错是真 bug）
_NOT_STR = {"True", "False", "None"}


def _unquote(v: str) -> str:
    v = str(v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
        return v[1:-1]
    return v


def desc_pairs(desc: str) -> dict[str, str]:
    """从能力描述里抽 `key=value`（只收字符串形态的值；数字/布尔/None 跳过）。"""
    out: dict[str, str] = {}
    for m in _DESC_PAIR.finditer(str(desc or "")):
        k, v = m.group(1), m.group(2)
        if v in _NOT_STR:
            continue
        out[k] = _unquote(v)
    return out


def probe_literals(probe: dict) -> dict[str, str]:
    """从探针的 `expect_json` 里抽字符串字面量（跨所有步骤合并）。"""
    out: dict[str, str] = {}
    if not isinstance(probe, dict):
        return out
    for st in (probe.get("steps") or []):
        ej = (st or {}).get("expect_json")
        if not isinstance(ej, dict):
            continue
        for k, v in ej.items():
            if isinstance(v, str):
                out.setdefault(str(k), v)
    return out


def conflicts(cap) -> dict[str, tuple[str, str]]:
    """返回 `key -> (能力描述里的值, 探针里的值)`（只列真正冲突的字符串取值）。

    `cap` 是能力对象（要有 `.desc` 与 `.probe`）。
    """
    d = desc_pairs(getattr(cap, "desc", "") or "")
    p = probe_literals(getattr(cap, "probe", {}) or {})
    out: dict[str, tuple[str, str]] = {}
    for k, dv in d.items():
        if k in p and p[k] != dv:
            out[k] = (dv, p[k])
    return out


def describe_conflict(cap) -> str:
    """一行说明冲突（没有冲突返回空串）。

    例：`口径：status 以探针断言 'created' 为准（能力描述写的 'pending_teacher' 已被覆盖）`
    """
    cf = conflicts(cap)
    if not cf:
        return ""
    parts = []
    for k, (dv, pv) in sorted(cf.items()):
        parts.append(f"{k} 以探针断言 {pv!r} 为准（能力描述写的 {dv!r} 已被覆盖）")
    return "口径：" + "；".join(parts)
