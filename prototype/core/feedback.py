"""core/feedback.py — 运行中提修改意见（轮边界生效）。

## 解决什么（2026-11-24 用户提出）

造物模式一旦开跑，人就没法再插话：
  - `/v1/night/clarify` 是**开工前**的澄清，文档定稿就冻结；
  - `/v1/night/stop` 只会**杀进程**，等于从头再来；
  - `/v1/questions` 是引擎**找人**问，方向相反。

于是"夜里看着不对劲想让它改个思路"只能等天亮收工后重来，
一整夜的预算和时间白花。

## 怎么解决：文件信箱

夜班是 detached 独立进程，服务器杀得掉它但**说不上话**。
界面本来就从磁盘读状态（coverage.jsonl / budget.json），
那就反过来用同一个思路：**人写磁盘，夜班读**。

    POST /v1/night/feedback  →  追加 .sixcoder/feedback.jsonl
                                          ↓
                        夜班每轮开头读一次 pending()
                                          ↓
                    把未消费的意见并进这一轮的工作单/约束

## 生效时机：轮边界（用户 2026-11-24 定稿选 A）

**不打断正在写的节点**：当前节点写完，下一轮才用新意见。
代价是最多等一个节点（几分钟）；换来的是不会丢弃正在写的东西、
不会让闸门在半成品上判定。

## 三种粒度（分开存、分开处理）

| scope | 含义 | 落到哪 |
|---|---|---|
| `global`   | 影响后续所有模块（"整体别用 React"） | 约束清单，一直生效 |
| `module`   | 只影响某个模块（"user.py 的登录改 JWT"） | 该模块的工作单修订 |
| `forbid`   | 红线（"不许引入新依赖"） | 红线清单，一直生效 |

global 与 forbid 会**用尽即失效**（consume 之后不再生效）还是**长期累积**？
⇒ 长期累积：它们是人类定的规矩，不该被"消费"掉。
module 则是**一次性**的：并进那一轮工作单后即标记 consumed。
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

FEEDBACK_FILE = "feedback.jsonl"
CONSTRAINTS_FILE = "constraints.json"

VALID_SCOPES = ("global", "module", "forbid")


@dataclass
class Feedback:
    """一条人的修改意见。"""
    id: str
    at: float
    scope: str
    text: str
    target: str = ""        # scope=module 时指模块相对路径
    consumed: bool = False
    consumed_round: int = 0
    consumed_at: float = 0.0
    extra: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "id": self.id, "at": self.at, "scope": self.scope,
            "text": self.text, "target": self.target,
            "consumed": self.consumed,
            "consumed_round": self.consumed_round,
            "consumed_at": self.consumed_at,
            **({"extra": self.extra} if self.extra else {}),
        }

    @staticmethod
    def from_json(d: dict) -> "Feedback":
        return Feedback(
            id=str(d.get("id") or ""),
            at=float(d.get("at") or 0),
            scope=str(d.get("scope") or "global"),
            text=str(d.get("text") or ""),
            target=str(d.get("target") or ""),
            consumed=bool(d.get("consumed")),
            consumed_round=int(d.get("consumed_round") or 0),
            consumed_at=float(d.get("consumed_at") or 0),
            extra=dict(d.get("extra") or {}),
        )


def _dir(ws: Path) -> Path:
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path(ws: Path) -> Path:
    return _dir(ws) / FEEDBACK_FILE


def _constraints_path(ws: Path) -> Path:
    return _dir(ws) / CONSTRAINTS_FILE


def append(ws: Path, text: str, scope: str = "global", target: str = "") -> Feedback:
    """人追加一条意见。scope 非法直接报错（不静默降级成 global）。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("意见内容不能为空")
    if scope not in VALID_SCOPES:
        raise ValueError(f"scope 必须是 {VALID_SCOPES} 之一，收到 {scope!r}")
    if scope == "module" and not (target or "").strip():
        raise ValueError("scope=module 时必须给 target（模块相对路径）")

    fb = Feedback(
        id=uuid.uuid4().hex[:12],
        at=time.time(),
        scope=scope,
        text=text,
        target=(target or "").strip().replace("\\", "/"),
    )
    with _path(ws).open("a", encoding="utf-8") as f:
        f.write(json.dumps(fb.to_json(), ensure_ascii=False) + "\n")
    return fb


def load(ws: Path) -> list[Feedback]:
    """读出全部意见（含已消费的）。文件不存在或坏行都跳过，不抛。"""
    p = _path(ws)
    if not p.exists():
        return []
    out: list[Feedback] = []
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue          # 坏行不影响其它意见（可能正在并发追加）
        if isinstance(d, dict) and d.get("text"):
            out.append(Feedback.from_json(d))
    return out


def pending(ws: Path) -> list[Feedback]:
    """还没被消费的意见。

    **global / forbid 永远算 pending** —— 它们是长期规矩，不是一次性待办。
    module 一次性：并进工作单后就被 consume。
    """
    return [f for f in load(ws) if not f.consumed or f.scope in ("global", "forbid")]


def module_pending(ws: Path) -> list[Feedback]:
    """只取 module 粒度、还没消费的（供派单时按模块匹配）。"""
    return [f for f in pending(ws) if f.scope == "module"]


def rules(ws: Path) -> dict:
    """当前长期约束：{"global": [...], "forbid": [...]}。

    这是给远端 prompt 用的稳定前缀 —— 顺序固定、内容去重，
    避免每轮拼出来的提示词漂移导致缓存失效。
    """
    g, fbd = [], []
    seen_g, seen_f = set(), set()
    for f in load(ws):
        if f.scope == "global" and f.text not in seen_g:
            seen_g.add(f.text)
            g.append(f.text)
        elif f.scope == "forbid" and f.text not in seen_f:
            seen_f.add(f.text)
            fbd.append(f.text)
    return {"global": g, "forbid": fbd}


def consume(ws: Path, ids: list[str], rnd: int = 0) -> int:
    """标记若干条意见已消费。**只动 module 粒度** —— global/forbid 是规矩，不该被消费。

    返回实际消费条数。整文件重写（意见总量很小，不值得做增量）。
    """
    want = set(ids or [])
    if not want:
        return 0
    items = load(ws)
    n = 0
    now = time.time()
    for fb in items:
        if fb.id in want and fb.scope == "module" and not fb.consumed:
            fb.consumed = True
            fb.consumed_round = int(rnd)
            fb.consumed_at = now
            n += 1
    if n:
        tmp = _path(ws).with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for fb in items:
                f.write(json.dumps(fb.to_json(), ensure_ascii=False) + "\n")
        tmp.replace(_path(ws))
    return n


def render_for_prompt(ws: Path, module: str = "") -> str:
    """渲染成可拼进远端提示词的一段文本。

    只输出去重后的内容；没有内容返回空串（调用方据此跳过，不浪费 token）。
    module 给了就额外带上该模块的 module 级意见。
    """
    r = rules(ws)
    mods = [f for f in module_pending(ws)
            if not module or f.target == module]
    if not any((r["global"], r["forbid"], mods)):
        return ""
    lines = ["## 人类中途提的修改意见（必须遵守）"]
    if r["forbid"]:
        lines.append("### 红线（绝对不许违反）")
        lines += [f"- {t}" for t in r["forbid"]]
    if r["global"]:
        lines.append("### 全局调整（影响后续所有模块）")
        lines += [f"- {t}" for t in r["global"]]
    if mods:
        lines.append(f"### 本模块修订（{module or '匹配到的模块'}）")
        lines += [f"- {t}" for t in mods]
    return "\n".join(lines)


def stats(ws: Path) -> dict:
    """给界面用：总数 / 待处理 / 按粒度分布。"""
    items = load(ws)
    pend = pending(ws)
    by_scope = {"global": 0, "module": 0, "forbid": 0}
    for f in items:
        if f.scope in by_scope:
            by_scope[f.scope] += 1
    return {
        "total": len(items),
        "pending": len(pend),
        "by_scope": by_scope,
        "latest_at": max((f.at for f in items), default=0),
    }
