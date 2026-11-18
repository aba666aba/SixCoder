"""收敛判据：什么时候继续、什么时候**换策略**、什么时候**停下叫人**。

## 为什么单独成模块（2026-11-14，开发文档 §13.4）

"反复这样"最容易出的问题是**不收敛**：同一条错误反复报、远端反复改、
永远不动（2026-11-11 实测 `stuck_same_evidence` ×4 卡死一轮，
以及 2 小时零增长只空转、花掉 ¥5）。

**判据写死在代码里，不靠人记**：

| 条件 | 动作 |
|---|---|
| P0+P1 比上轮**少** | `continue`（在收敛） |
| P0+P1 连续 **2** 轮不降 | `switch_strategy`（改打法，不许继续烧钱） |
| P0 连续 **3** 轮不清零 | `stop_human`（语法都修不好 = 提示词/上下文有结构问题） |
| 端到端 done 连续 **3** 轮不涨 | `stop_human`（说明检测有盲区，或远端改不动） |
| P0+P1 == 0 且 done == total | `done` |

**为什么"不降"要换策略而不是继续**：今天实测，空转时每小时烧 ¥2.1（写码时 ¥0.48），
**继续循环不是"多花点钱"，是"把钱烧在同一个死循环上"**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["Verdict", "judge", "NON_IMPROVE_LIMIT", "P0_STUCK_LIMIT", "DONE_STUCK_LIMIT"]

NON_IMPROVE_LIMIT = 2      # P0+P1 连续不降几轮 → 换策略
P0_STUCK_LIMIT = 3         # P0 连续几轮不清零 → 叫人
DONE_STUCK_LIMIT = 3       # 端到端 done 连续几轮不涨 → 叫人


@dataclass
class Verdict:
    action: str                     # continue / switch_strategy / stop_human / done
    why: str = ""
    fatal: int = 0                  # 本轮 P0+P1
    prev_fatal: int = -1            # 上轮 P0+P1
    done: int = -1                  # 本轮端到端 done
    trend: list[int] = field(default_factory=list)   # 历史 fatal 序列

    def as_dict(self) -> dict:
        return {"action": self.action, "why": self.why, "fatal": self.fatal,
                "prev_fatal": self.prev_fatal, "done": self.done,
                "trend": list(self.trend)}


def judge(history: list[dict], *, fatal: int, done: int = -1,
          total: int = -1) -> Verdict:
    """给一次判定。

    `history`：**之前各轮**的统计（不含本轮），每项至少含 `fatal`（P0+P1），
    可选 `p0`、`done`。按时间升序。
    """
    hist = list(history or ())
    fatal = max(0, int(fatal))
    prev_fatal = int(hist[-1].get("fatal", -1)) if hist else -1
    trend = [int(h.get("fatal", 0)) for h in hist] + [fatal]

    # ① 完全通过
    if fatal == 0 and (total < 0 or done >= total):
        return Verdict("done", "P0+P1 清零" +
                       ("且端到端全通过" if total >= 0 and done >= total else ""),
                       fatal, prev_fatal, done, trend)

    # ② P0 连续多轮不清零 → 叫人（语法都修不好，是提示词/上下文的结构问题）
    p0s = [int(h.get("p0", 0)) for h in hist]
    if "p0" in (hist[-1] if hist else {}):
        p0s_after = [int(h.get("p0", 0)) for h in hist[-(P0_STUCK_LIMIT - 1):]] + [fatal]
        if len(p0s_after) >= P0_STUCK_LIMIT and all(x > 0 for x in p0s_after):
            return Verdict("stop_human",
                           f"P0（语法/导入）连续 {P0_STUCK_LIMIT} 轮不清零 → "
                           f"不是远端不会写，是提示词/上下文有结构问题，叫人",
                           fatal, prev_fatal, done, trend)

    # ③ 端到端 done 连续多轮不涨 → 叫人（检测盲区 or 远端改不动）
    if done >= 0:
        dones = [int(h.get("done", -1)) for h in hist[-(DONE_STUCK_LIMIT - 1):]] + [done]
        if len(dones) >= DONE_STUCK_LIMIT and all(x >= 0 for x in dones) \
                and max(dones) == min(dones):
            return Verdict("stop_human",
                           f"端到端 done 连续 {DONE_STUCK_LIMIT} 轮不涨（{done}）"
                           f"→ 检测有盲区或远端改不动，叫人",
                           fatal, prev_fatal, done, trend)

    # ④ 不降 → 换策略
    if len(trend) >= NON_IMPROVE_LIMIT:
        tail = trend[-NON_IMPROVE_LIMIT:]
        if all(tail[i] >= tail[i - 1] for i in range(1, len(tail))):
            return Verdict("switch_strategy",
                           f"P0+P1 连续 {NON_IMPROVE_LIMIT} 轮不降（{tail}）"
                           f"→ 换打法（拆小批次/点名具体改法），不许继续循环",
                           fatal, prev_fatal, done, trend)

    # ⑤ 在降 → 继续
    if prev_fatal >= 0:
        return Verdict("continue",
                       f"P0+P1 {prev_fatal} → {fatal}（在收敛）",
                       fatal, prev_fatal, done, trend)
    return Verdict("continue", f"首轮：P0+P1={fatal}", fatal, prev_fatal, done, trend)
