"""错误报告：编号 / 分级 / 去重 / 状态 / 渲染（纯数据，零依赖）。

## 为什么单独成模块（2026-11-14，开发文档 §13）

新流程的核心是「**本地全量检测 → 一份错误报告 → 整体交给远端修**」。
报告本身必须满足四条硬要求，否则循环不收敛（这是 2026-11-11 踩过的）：

1. **编号跨轮稳定** —— 同一处同一类错永远同一个 `E00NN`。
   否则"上轮修了没有"无从判断（实测 `stuck_same_evidence` 连续 4 次报同一条，
   引擎却以为是新问题）。
2. **去重键明确** —— `(级别, 判据类型, 位置, 符号)`。
   同一函数缺两个参数算一条（不是两条），否则报告越报越长、远端无所适从。
3. **状态机** —— `pending / fixed / wontfix / stale`。
   `stale` = 上轮报过、这轮复现不出来（可能被别处的修复顺带解决了）。
4. **能渲染成给人/给模型看的文本** —— 按级别排序，带证据，能被 **整体** 塞进远端提示。

## 数据形态

一行一条 JSON（jsonl），追加写：`.sixcoder/errors.jsonl`
—— 与 `coverage.jsonl` 同构，坏了能逐行读，不会一份 JSON 全烂。
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

__all__ = ["Error", "Report", "LEVELS", "P_ORDER"]

# 级别：数字越小越致命
LEVELS = {
    "P0": "语法/导入 —— 整个 app 起不来，其它一切判定都无意义",
    "P1": "契约/接口 —— 签名、返回字段、错误码与契约不符，对应对端能力必红",
    "P2": "端到端/语义 —— 路由通但返回不对、跨模块调用/数据模型不一致",
    "P3": "建议 —— 不影响判据，但会拖慢后续（命名、重复实现、死代码）",
}
P_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}

# 判据类型（去重维度之一）
KINDS = ("syntax", "import", "contract", "crossref", "datamodel", "e2e", "advice")


@dataclass
class Error:
    """一条错误。

    `eid` **不落库也能算**（由 level/kind/where/symbol 哈希得到），
    落库只是为了让它**顺序可读**（E0001、E0002…）。
    """
    level: str                     # P0..P3
    kind: str                      # syntax/import/contract/crossref/datamodel/e2e/advice
    where: str                     # "app/services/topics.py:1609" / "POST /api/v1/topics"
    what: str                      # 一句话：要求什么 / 实际什么
    evidence: str = ""             # 证据（探针原文、AST 摘要、diff 片段）
    symbol: str = ""               # 去重用的符号（函数名/字段名/端点）
    status: str = "pending"        # pending/fixed/wontfix/stale
    eid: str = ""                  # 由 make_id 填；跨轮稳定
    first_seen: float = 0.0
    last_seen: float = 0.0

    def key(self) -> tuple:
        """去重键 —— **不含 evidence**（证据会随实现变化，键不能跟着变）。"""
        return (str(self.level), str(self.kind), str(self.where), str(self.symbol))

    def make_id(self) -> str:
        """稳定编号：同一 key 永远同一个 id（跨进程、跨轮）。"""
        raw = "|".join(self.key()).encode("utf-8")
        return "E" + hashlib.sha1(raw).hexdigest()[:5].upper()

    def normalize(self) -> "Error":
        self.level = str(self.level or "P3").upper()
        if self.level not in P_ORDER:
            self.level = "P3"
        self.kind = str(self.kind or "advice").strip().lower()
        self.where = str(self.where or "").strip()
        self.symbol = str(self.symbol or "").strip()
        self.eid = self.eid or self.make_id()
        now = time.time()
        self.first_seen = self.first_seen or now
        self.last_seen = now
        return self


class Report:
    """一份错误报告（多轮之间靠 jsonl 保持编号与状态）。"""

    def __init__(self, ws: Path):
        self.ws = Path(ws)
        self.path = self.ws / ".sixcoder" / "errors.jsonl"
        self.items: list[Error] = []
        self._index: dict[tuple, Error] = {}
        self._hist_index: dict[tuple, Error] = {}   # 历史（只为复用编号）

    # ---- 增 ----
    def add(self, err: Error) -> Error:
        """加一条；同一去重键**只保留一条**（后来者更新 what/evidence/级别）。"""
        err = err.normalize()
        old = self._index.get(err.key())
        if old is not None:
            old.what = err.what or old.what
            old.evidence = err.evidence or old.evidence
            old.level = err.level
            old.last_seen = time.time()
            old.status = "pending"
            return old
        self.items.append(err)
        self._index[err.key()] = err
        return err

    def add_many(self, errs) -> None:
        for e in errs or ():
            self.add(e)

    # ---- 与上一轮对齐 ----
    def load(self) -> "Report":
        """读历史（只为**复用编号**，不复活已 fixed 的）。"""
        try:
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                if not ln.strip():
                    continue
                d = json.loads(ln)
                e = Error(**{k: v for k, v in d.items() if k in Error.__dataclass_fields__})
                self._hist_index[e.key()] = e
        except Exception:                                  # noqa: BLE001
            pass
        return self

    def adopt_ids(self) -> None:
        """把历史编号套到本轮同名条目上（**编号跨轮稳定**的关键）。"""
        for e in self.items:
            old = self._hist_index.get(e.key())
            if old is not None and old.eid:
                e.eid = old.eid
                e.first_seen = old.first_seen or e.first_seen

    def mark_missing(self, prev_keys: set) -> list[Error]:
        """上一轮有、本轮没复现的 → `stale`（不删除，留痕）。"""
        out: list[Error] = []
        for k in prev_keys - set(self._index):
            old = self._hist_index.get(k)
            if not old or old.status in ("fixed", "wontfix"):
                continue
            old.status = "stale"
            old.last_seen = time.time()
            out.append(old)
        return out

    # ---- 查 ----
    def by_level(self, level: str) -> list[Error]:
        return [e for e in self.items if e.level == level]

    def fatal(self) -> list[Error]:
        """P0+P1 —— 收敛判据只看这两档（§13.4）。"""
        return [e for e in self.items if e.level in ("P0", "P1")]

    def sorted(self) -> list[Error]:
        return sorted(self.items, key=lambda e: (P_ORDER.get(e.level, 9),
                                                 str(e.where), str(e.symbol)))

    def stats(self) -> dict:
        n = {k: 0 for k in P_ORDER}
        for e in self.items:
            n[e.level] = n.get(e.level, 0) + 1
        n["total"] = len(self.items)
        n["fatal"] = len(self.fatal())
        return n

    def empty(self) -> bool:
        return not self.items

    # ---- 渲染 ----
    def render(self, limit_per_level: int = 40) -> str:
        """给远端看的文本 —— **按级别排序、带证据、可整体塞进提示**。"""
        if self.empty():
            return "（无错误）"
        lines: list[str] = []
        st = self.stats()
        lines.append("错误报告：共 %d 条（P0=%d P1=%d P2=%d P3=%d）"
                     % (st["total"], st["P0"], st["P1"], st["P2"], st["P3"]))
        lines.append("")
        for level in ("P0", "P1", "P2", "P3"):
            items = self.by_level(level)
            if not items:
                continue
            lines.append("## %s — %s（%d 条）" % (level, LEVELS[level], len(items)))
            for e in items[:limit_per_level]:
                lines.append("")
                lines.append("### %s [%s/%s] %s" % (e.eid, e.level, e.kind, e.where))
                lines.append("- 问题：%s" % (e.what or "（未描述）"))
                if e.evidence:
                    ev = str(e.evidence).strip()
                    for i, ln in enumerate(ev.splitlines()[:8]):
                        lines.append(("  证据：%s" if i == 0 else "        %s") % ln[:300])
            if len(items) > limit_per_level:
                lines.append("")
                lines.append("（同级还有 %d 条未列出，先修上面这些）"
                             % (len(items) - limit_per_level))
            lines.append("")
        return "\n".join(lines).rstrip()

    def render_short(self) -> str:
        """一行摘要，给日志/emit 用。"""
        st = self.stats()
        return ("E=%d（P0=%d P1=%d P2=%d P3=%d）"
                % (st["total"], st["P0"], st["P1"], st["P2"], st["P3"]))

    # ---- 落盘 ----
    def save(self) -> int:
        """追加写本轮全部条目（jsonl，一行一条）。返回写入条数。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                for e in self.sorted():
                    f.write(json.dumps(asdict(e), ensure_ascii=False) + "\n")
            self.adopt_ids()
            return len(self.items)
        except Exception:                                  # noqa: BLE001
            return 0
