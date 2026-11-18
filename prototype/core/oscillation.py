"""振荡检测：识别"改回去又改回来"，而不是只看"这一轮有没有增长"。

现状问题：夜班有"连续 3 轮无增长就收工"和"每档写次数上限"，但这两条都只看
**数量**，看不出**状态又转回去了**。实测过的现象：
  · 第 3 轮把 A 改坏 → 第 4 轮改回 A 的样子 → 第 5 轮又改坏（A→B→A→B）——
    每轮都"有变化"，无增长计数被反复清零，于是整夜原地打转；
  · 花掉的钱全在"重复同一个失败状态"上。

做法（确定性，不猜）：
  1. 每轮把"失败状态"压成一个指纹（哪些函数红、哪些还是桩、哪些没判据、各模块已验收数）；
  2. 指纹历史里**同一个指纹出现第 2 次**就算振荡（回到来过的状态）；
  3. 判定后先跳策略/跳档；策略也用完就收工，并把"回到第几轮的状态"写进报告。

关键取舍：指纹只取**机械事实**（判据结果），不取模型自述、不取时间戳，
否则同一状态会被算成不同指纹，检测就永远失效。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field


def fingerprint(state: dict, cap_cov: dict | None = None) -> str:
    """把一轮的机械失败状态压成指纹。

    只取会影响"下一步该干什么"的事实：红函数、桩、无判据、每模块已验收数。

    2026-11-10：`cap_cov` = 端到端能力覆盖观测结果。
    **必须算进来** —— 只看契约判据时，契约一旦全过指纹就**恒定不变**，
    于是每轮都被判成"振荡"而收工（实测：6 轮里端到端能力集合明明在变，
    却因契约指纹相同被 stop，白跑）。
    端到端在变 = 这一轮的改动有效果 = 不是振荡。
    """
    facts: list[str] = []
    for path in sorted(state or {}):
        st = state.get(path) or {}
        red = sorted(str(x) for x in (st.get("hard_failed") or []))
        stubs = sorted(str(x) for x in (st.get("stubs") or []))
        untested = sorted(str(x) for x in (st.get("untested") or []))
        verified = int(st.get("verified") or 0)
        facts.append(f"{path}|v={verified}|red={','.join(red)}|stub={','.join(stubs)}"
                     f"|un={','.join(untested)}")
    # 端到端能力状态：已通过的能力集合 + 红灯能力集合
    if cap_cov:
        _caps = cap_cov.get("caps") or {}
        _done = sorted(c for c, v in _caps.items()
                        if str((v or {}).get("status") or "") == "done")
        _red = sorted(c for c, v in _caps.items()
                       if str((v or {}).get("status") or "") not in ("done", "descoped"))
        facts.append(f"|cap_done={len(_done)}|cap_red={','.join(_red)}")
    blob = "\n".join(facts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def summarize(state: dict) -> dict:
    """给报告/事件用的可读摘要（不含指纹）。"""
    red = [f"{p}::{f}" for p, s in (state or {}).items()
           for f in (s.get("hard_failed") or [])]
    stubs = [f"{p}::{f}" for p, s in (state or {}).items() for f in (s.get("stubs") or [])]
    verified = sum(int(s.get("verified") or 0) for s in (state or {}).values())
    total = sum(int(s.get("funcs") or 0) for s in (state or {}).values())
    return {"verified": verified, "total": total,
            "red": red[:6], "stubs": stubs[:6],
            "red_count": len(red), "stub_count": len(stubs)}


@dataclass
class Tracker:
    """跨轮记录指纹，判断"是不是回到了来过的状态"。"""

    seen: dict[str, int] = field(default_factory=dict)     # 指纹 → 最早出现的轮次
    history: list[tuple[int, str]] = field(default_factory=list)
    hits: int = 0                                          # 被判定为振荡的次数

    def add(self, round_no: int, fp: str) -> dict:
        """记一轮指纹，返回本轮判定结果。"""
        self.history.append((round_no, fp))
        first = self.seen.get(fp)
        if first is None:
            self.seen[fp] = round_no
            return {"oscillating": False, "fp": fp, "first_round": round_no,
                    "note": "新状态"}
        self.hits += 1
        return {
            "oscillating": True, "fp": fp, "first_round": first,
            "note": (f"回到第 {first} 轮的状态（第 {round_no} 轮与它完全相同）："
                     f"这一轮的改动没有净效果，继续同样的打法不会收敛"),
        }

    def distinct(self) -> int:
        return len(self.seen)

    def to_dict(self) -> dict:
        return {"distinct_states": self.distinct(), "oscillations": self.hits,
                "history": [{"round": r, "fp": fp} for r, fp in self.history[-20:]]}

    @classmethod
    def from_dict(cls, data: dict) -> "Tracker":
        t = cls()
        for item in (data or {}).get("history") or []:
            r = int(item.get("round") or 0)
            fp = str(item.get("fp") or "")
            if fp:
                t.seen.setdefault(fp, r)
                t.history.append((r, fp))
        t.hits = int((data or {}).get("oscillations") or 0)
        return t


def decide_action(osc: dict, strategies_left: int) -> dict:
    """振荡了该怎么办：还有策略就换策略；没有就收工。

    为什么先换策略而不是直接收工：同一状态回到来，最可能的原因是"打法不对"，
    换一种打法（例如从整文件改成函数体、或升一档）还有机会；策略都用完了才是真收工。
    """
    if not osc.get("oscillating"):
        return {"action": "continue", "reason": ""}
    if strategies_left > 0:
        return {"action": "switch_strategy",
                "reason": osc.get("note", "") + "；换一种打法再试"}
    return {"action": "stop",
            "reason": osc.get("note", "") + "；打法已用完，收工（已列入交付报告）"}


def act(osc: dict, act_result: dict, strategy_i: int, n_strategies: int,
        dec_used: dict | None = None, write_used: dict | None = None) -> tuple[str, object]:
    """振荡处置的**唯一入口**：返回 ("stop", 原因) 或 ("work", 新的 strategy_i)。

    为什么要单独一个函数（实测 2026-11-07）：旧实现在"换打法"后直接 `continue`，
    于是这一轮**什么都没干**；下一轮观察到完全相同的状态 → 又振荡 → 又换打法 → 又跳过。
    round 2–6 五轮没有任何 author_start，纯空转后停机（花掉 ¥0.23、零收获）。

    规矩：换打法是为了"换一种干法再试一次"，**必须在本轮就干活**；同时清空决策/写次数
    预算，否则新打法继承"这个动作已经用过了"，想动手也动不了。
    """
    if act_result.get("action") == "stop":
        return ("stop", act_result.get("reason") or "打法已用完")
    new_i = min(int(strategy_i) + 1, max(0, int(n_strategies) - 1))
    for budget in (dec_used, write_used):
        if isinstance(budget, dict):
            budget.clear()
    return ("work", new_i)


def as_json(tracker: Tracker) -> str:
    return json.dumps(tracker.to_dict(), ensure_ascii=False)
