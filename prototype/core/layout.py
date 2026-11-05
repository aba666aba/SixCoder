"""提示词的**缓存友好布局**（确定性执行，本地模型只做分类建议）。

背景（用户三条要求之一）：
    DeepSeek 的前缀缓存是**逐字匹配**：前缀里任何一处变化（时间戳、任务号、轮次、
    随机排序），后面全部算"未命中"。命中价 0.02 元/百万，未命中 1~2 元 → 差 50~100 倍。

所以这里把"提示词怎么拼"从"随手拼"变成**可复现的确定性布局**：
    1. 每个文本块标上 stable / volatile；
    2. stable 块按固定 id 顺序前置，volatile 块一律后置；
    3. stable 块里禁止出现时间戳/任务号/轮次/绝对路径等会变的东西（扫描并剥掉）；
    4. 输出一个 fingerprint（stable 前缀的 sha1），同一夜两次请求指纹一致 = 命中。

"让本地模型规划布局"的边界（用户已确认）：本地模型只负责**给块的分类建议**，
最终排序与拼接由本模块的确定性代码执行 —— 否则每次拼法不同，反而破坏缓存。
"""
from __future__ import annotations

import hashlib
import re

# 会破坏前缀稳定性的模式（在 stable 块里出现就要剥掉或降级为 volatile）
VOLATILE_PATTERNS = (
    (re.compile(r"\b20\d{2}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?\b"), "时间戳"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\b"), "ISO 时间戳"),
    (re.compile(r"\b(第\s*\d+\s*轮|round\s*#?\d+|轮次\s*\d+)\b"), "轮次计数"),
    (re.compile(r"\b(night|t|task|ap)-\d{8,}\b"), "任务号"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "UUID"),
    (re.compile(r"\b(耗|用时|配合|left_min)\s*[:：]?\s*\d+(\.\d+)?\s*(s|秒|min|分钟)"),
     "耗时统计"),
)


def unstable_reasons(text: str) -> list[str]:
    """这段文本里有哪些"每次都会变"的东西。空 = 可当稳定前缀。"""
    out: list[str] = []
    for pat, why in VOLATILE_PATTERNS:
        if pat.search(text or ""):
            out.append(why)
    return out


def sanitize(text: str) -> str:
    """把稳定前缀里的易变内容抹掉（只抹"每次都会变"的那几个，不删信息主体）。"""
    out = text or ""
    for pat, _why in VOLATILE_PATTERNS:
        out = pat.sub("", out)
    return re.sub(r"[ \t]{2,}", " ", out).strip()


class Chunk:
    """一块提示词内容。stable=True 表示"同一夜内应逐字不变"。"""

    __slots__ = ("id", "text", "stable", "why")

    def __init__(self, id: str, text: str, stable: bool, why: str = ""):
        self.id = id
        self.text = text or ""
        self.stable = stable
        self.why = why

    def __repr__(self) -> str:
        return f"Chunk({self.id!r}, stable={self.stable}, {len(self.text)} 字符)"


def plan(chunks: list[Chunk]) -> list[Chunk]:
    """确定性布局：稳定块按 id 排序在前，易变块按 id 排序在后。

    - 稳定块里若扫出易变内容 → 先 sanitize；sanitize 后仍有 → 降级为易变块（保证前缀真的稳定）。
    - 排序用 id（不是插入顺序），避免调用顺序不同导致布局漂移。
    """
    stable: list[Chunk] = []
    volatile: list[Chunk] = []
    for c in chunks:
        if c.stable:
            if unstable_reasons(c.text):
                cleaned = sanitize(c.text)
                if unstable_reasons(cleaned):
                    volatile.append(Chunk(c.id, cleaned, False, "清理后仍含易变内容"))
                    continue
                stable.append(Chunk(c.id, cleaned, True, c.why))
            else:
                stable.append(c)
        else:
            volatile.append(c)
    stable.sort(key=lambda x: x.id)
    volatile.sort(key=lambda x: x.id)
    return stable + volatile


def render(chunks: list[Chunk]) -> str:
    return "\n\n".join(c.text.strip() for c in plan(chunks) if c.text.strip())


def stable_prefix(chunks: list[Chunk]) -> str:
    """只取稳定部分（用于指纹与命中率自检）。"""
    return "\n\n".join(c.text.strip() for c in plan(chunks)
                       if c.stable and c.text.strip())


def fingerprint(chunks: list[Chunk]) -> str:
    return hashlib.sha1(stable_prefix(chunks).encode("utf-8")).hexdigest()[:16]


def audit(chunks: list[Chunk]) -> dict:
    """布局体检：给交付报告/告警用。"""
    ordered = plan(chunks)
    st = [c for c in ordered if c.stable]
    vo = [c for c in ordered if not c.stable]
    return {
        "chunks": len(ordered),
        "stable_chunks": [c.id for c in st],
        "volatile_chunks": [c.id for c in vo],
        "stable_chars": sum(len(c.text) for c in st),
        "volatile_chars": sum(len(c.text) for c in vo),
        "fingerprint": fingerprint(ordered),
        "downgraded": [c.id for c in vo if c.why],
        "stable_ratio": (round(sum(len(c.text) for c in st)
                               / max(1, sum(len(c.text) for c in ordered)), 3)),
    }


def suggest_stable(chunks: list[Chunk]) -> list[dict]:
    """给本地模型看的"分类建议"清单（它只负责建议，不改布局）。

    返回 [{id, chars, looks_stable, reasons}]，本地模型据此确认或推翻；
    任何推翻都必须写进报告（因为推翻会改变指纹，进而影响命中率）。
    """
    out = []
    for c in chunks:
        reasons = unstable_reasons(c.text)
        out.append({"id": c.id, "chars": len(c.text), "looks_stable": not reasons,
                    "reasons": reasons, "current": "stable" if c.stable else "volatile"})
    return out
