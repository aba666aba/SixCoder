"""远端可写记忆（§13.4 第 5 步）：让远端把事实写下来，下轮以稳定前缀注入。

为什么需要：L6（开发文档 §11.3）指出，远端在推理中获得的洞察只要没落进代码，
下一轮就消失了——没有累积就没有「越用越强」。这是唯一能让系统跨轮积累的机制。

设计边界（与受保护区、工具循环同源）：
- 记忆只落盘到 `.sixcoder/knowledge.jsonl`（受保护区内、远端工具唯一能写的路径）。
- 每条是结构化 JSON（`ts/type/text/src`），便于稳定前缀注入与去重。
- `render_prefix()` 产出给远端看的稳定前缀块；它只读不写，可每轮注入。
- 不替远端做判断：写什么、信什么由远端在推理中决定，本地只负责「存 + 注入」。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

STORE_REL = ".sixcoder/knowledge.jsonl"

# 记忆类型白名单：事实 / 决策 / 约束 / 坑。其它类型一律拒收（防垃圾注入）。
ALLOWED_TYPES = ("fact", "decision", "constraint", "gotcha")

# 单条 text 上限（稳定前缀要可控，否则污染缓存命中率）。
_MAX_TEXT = 280
_MAX_ITEMS = 80          # 注入前缀时最多取最近的有效条目
_MAX_PREFIX_CHARS = 2200


class MemoryStore:
    """围绕 `.sixcoder/knowledge.jsonl` 的读写器。fail-soft：任何异常都不应打断主流程。"""

    def __init__(self, ws: Path):
        self.ws = Path(ws)
        self.path = self.ws / STORE_REL

    # ----------------------------------------------------------- 写
    def append(self, text: str, kind: str = "fact", src: str = "") -> dict | None:
        """追加一条记忆。返回写下的条目（失败返回 None）。

        kind 非白名单 → 退回 fact；text 超长截断；空文本拒收。
        """
        kind = str(kind).strip().lower() or "fact"
        if kind not in ALLOWED_TYPES:
            kind = "fact"
        text = (text or "").strip()
        if not text:
            return None
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": kind,
            "text": text[:_MAX_TEXT],
            "src": (src or "").strip()[:80],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            return None
        return entry

    # ----------------------------------------------------------- 读
    def read(self) -> list[dict]:
        """解析整份记忆（每行一个 JSON；坏行跳过）。"""
        if not self.path.exists():
            return []
        out: list[dict] = []
        try:
            for line in self.path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:                        # noqa: BLE001
                    continue
                if isinstance(obj, dict) and obj.get("text"):
                    out.append(obj)
        except OSError:
            return []
        return out

    def render_prefix(self, max_items: int = _MAX_ITEMS,
                      max_chars: int = _MAX_PREFIX_CHARS) -> str:
        """产出给远端看的稳定前缀文本块；空记忆返回空串。"""
        items = self.read()[-max_items:]
        if not items:
            return ""
        lines = ["# 项目记忆（远端写入、跨轮复用 · 仅供参照，不覆盖本次契约）"]
        total = 0
        for it in items:
            t = str(it.get("text", "")).strip()
            k = str(it.get("type", "fact"))
            s = (it.get("src") or "").strip()
            line = f"- [{k}] {t}" + (f"  (来源：{s})" if s else "")
            if total + len(line) > max_chars:
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    def size(self) -> int:
        return len(self.read())
