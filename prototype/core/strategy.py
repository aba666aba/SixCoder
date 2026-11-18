"""停滞阶梯：把「人推翻框架换方向」机械化。

为什么需要这一层（2026-11-09）：
    旧行为里，连续 7 次 `replan reason=覆盖停滞` 做的是**同一件事**
    （降级卡死 → 审计复评 → version+1），直到 `MAX_REPLANS=6` 直接 break，
    报告只写「重规划次数到顶」。系统从来不问「是不是这套推进框架本身错了」。
    人会在这一步说「别按 76 条平铺了」，然后收缩范围 / 降粒度 / 换模型。

本模块把这件事变成**显式、可观测、可回滚**的状态机：

    stall_streak  0~1  → flat      按原拓扑平铺（不动作）
                  2    → focus     只收缩「孤儿 ∩ 反复失败」的能力（有证据才动）
                  >=3  → escalate  自动策略穷尽 → halt 停机 + 给人工决策信息

为什么收缩必须带失败证据（实测 2026-11-09）：
    真实蓝图 84 条里有 33 条「结构孤儿」（无 deps 也无人依赖），
    其中包含 `cap.algo.solve_student_da` / `blocking_pairs` / `metrics` / `greedy`
    ——都是毕设的核心算法。只按图结构砍会把主干砍掉，比停滞更坏。
    所以：**孤儿只是候选，连续失败才是证据**；没有失败证据就一条都不砍，
    到 escalate 时把孤儿清单交给人工判断。

设计约束：
  · 不认业务词：孤儿 = 图结构，失败证据 = fails 计数；
  · 保住事实：done / 有 deps / 被依赖、以及没有失败证据的能力一律不动；
  · 幂等：重复 apply 不重复降级；
  · 只改蓝图状态与 notes，不碰产物代码、不调远端、不写配置。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

FOCUS_AT = 2
ESCALATE_AT = 3
FOCUS_FAILS = 2            # 孤儿至少连续失败几次才收缩

NEEDHUMAN = "[needhuman] 连续停滞，自动策略已穷尽：请人工决定换方向（收缩需求 / 降切片粒度 / 换模型）"


def choose(stall_streak: int) -> str:
    """连续停滞次数 → 推进策略。负数/非数值一律按 0 处理（不猜）。"""
    try:
        n = max(0, int(stall_streak or 0))
    except (TypeError, ValueError):
        n = 0
    if n >= ESCALATE_AT:
        return "escalate"
    if n >= FOCUS_AT:
        return "focus"
    return "flat"


def _dependents(bp) -> dict[str, int]:
    cnt: Counter = Counter()
    for c in getattr(bp, "caps", []) or []:
        for d in (getattr(c, "deps", None) or []):
            cnt[str(d)] += 1
    return dict(cnt)


def orphans(bp) -> list[str]:
    """孤儿能力：无 deps、无 dependents、且未 done/descoped。纯图结构判据。"""
    dep = _dependents(bp)
    return [c.id for c in (getattr(bp, "caps", []) or [])
            if c.status not in ("done", "descoped")
            and not (getattr(c, "deps", None) or [])
            and dep.get(c.id, 0) == 0]


def shrinkable(bp, fails: dict | None) -> list[str]:
    """可收缩集合 = 孤儿 ∩ 有连续失败证据。fails 为空 → 一条都不砍（安全默认）。"""
    if not fails:
        return []
    return [cid for cid in orphans(bp)
            if int(fails.get(cid) or 0) >= FOCUS_FAILS]


def apply(bp, strategy: str, *, fails: dict | None = None, why: str = "") -> dict:
    """把策略作用到蓝图。返回 {strategy, changed, descoped, halt, orphan_left}。"""
    from . import blueprint as _bp

    name = str(strategy or "flat")
    if name == "flat":
        return {"strategy": name, "changed": 0, "descoped": [],
                "halt": False, "orphan_left": len(orphans(bp))}

    ids = shrinkable(bp, fails)
    changed = 0
    for cid in ids:
        reason = why or (f"连续停滞：{cid} 是孤儿且连续失败 ≥{FOCUS_FAILS} 次，"
                         f"按 {name} 策略收缩范围，预算集中到主干")
        if _bp.descope(bp, cid, reason):
            changed += 1

    halt = name == "escalate"
    left = orphans(bp)
    if halt:
        bp.notes.append(NEEDHUMAN)
        if left:
            bp.notes.append(f"[决策信息] 未收缩的孤儿 {len(left)} 条（含可能是主干的能力，"
                            f"由人工判断是否放弃）：" + "、".join(left[:12]))
    return {"strategy": name, "changed": changed, "descoped": ids[:changed],
            "halt": halt, "orphan_left": len(left)}


# ---------------------------------------------------------------- 框架外提案（提出 ≠ 拍板）
_PROPOSE_INSTRUCTION = """\
你是**策略顾问**，不是实现者。系统已连续停滞，自动策略穷尽，现停机请人换方向。
你的任务是：提出**改变推进框架本身**的候选方向，交人裁决。

【停滞事实（引擎给的，不是你猜的）】
{done} / {total} 条能力通过；结构孤儿 {orphans} 条；连续停滞 {streak} 次。

【什么叫「框架外」】
不是「把这条能力再写一遍」，也不是「再重试一次」；而是改变游戏规则本身，例如：
收缩验收范围、改切片粒度、换模型/档位、调整验收口径、暂停某类能力、改变技术栈假设。
可以提你认为对的任何方向，不限于上面这些。

【输出格式：JSON，不要代码块外的文字】
{{"proposals":[{{"title":"方向名","why":"为什么当前框架走不通",
  "evidence":"支持这个判断的事实（文件或事件；没有就写 unverified）",
  "risk":"代价/风险","reversible":"能不能回退"}}]}}

【硬约束】
- 最多 3 条；每条必须有 title / why / evidence，缺一条会被机械校验丢弃。
- **只提方案，不执行**：不要输出代码、补丁、函数体；系统不会自动采纳任何一条。
- 最终拍板权在人（`policy.py` 明令不许远端替人拍板），你只负责把可选项摆出来。
- 拿不准的证据写 unverified，不要编造文件路径或数字。
"""


def _extract_json(text: str):
    """从回答里取 JSON；围栏 / 前后闲聊都能容忍，取不到返回 None（不猜）。"""
    t = str(text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", t, re.S)
    if m:
        t = m.group(1)
    try:
        return json.loads(t)
    except (ValueError, TypeError):
        pass
    i, j = t.find("{"), t.rfind("}")
    if i >= 0 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except (ValueError, TypeError):
            pass
    return None


def propose(ws, *, facts: dict | None = None, emit=None,
            complete_fn=None, max_rounds: int = 2) -> list[dict]:
    """停机时让远端带只读工具提出框架外方向；引擎机械校验后只返回「提案」。

    为什么这一层存在（2026-11-09）：`system_issues` 通道早就允许远端指出系统毛病，
    `needhuman` 也早就存在；缺的是**在停机这个决策点上把这些能力用起来**，
    以及给「框架外方向」一个明确的动作空间和接收处。
    `policy.py` 禁止的是「远端替人拍板」，不是「远端提出」——两者不能混为一谈。

    引擎侧只做三件机械的事：丢弃无证据条目、强制 status/auto_apply、限 3 条。
    """
    from . import remotetools as _rtl

    f = facts or {}
    instr = (_PROPOSE_INSTRUCTION
             .replace("{done}", str(f.get("done", "?")))
             .replace("{total}", str(f.get("total", "?")))
             .replace("{orphans}", str(f.get("orphans", "?")))
             .replace("{streak}", str(f.get("streak", "?"))))
    try:
        r = _rtl.ToolLoop(Path(ws), max_rounds=max_rounds, emit=emit,
                          complete_fn=complete_fn).run(instr)
        data = _extract_json(r.get("answer"))
    except Exception as exc:                        # noqa: BLE001
        if emit:
            emit("strategy_proposal_error", {"error": f"{type(exc).__name__}: {exc}"[:200]})
        return []
    raw = (data or {}).get("proposals") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for it in raw[:3]:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        why = str(it.get("why") or "").strip()
        evidence = str(it.get("evidence") or "").strip()
        if not (title and why and evidence):        # 无证据 = 幻觉风险，直接丢
            continue
        # 不做随意长度截断：提案只落盘给人看、不参与判据、不自动执行，
        # 长度由「最多 3 条」+ ToolLoop 轮次/输出预算约束，比拍一个魔法数字更可解释。
        out.append({"title": title, "why": why, "evidence": evidence,
                    "risk": str(it.get("risk") or ""),
                    "reversible": str(it.get("reversible") or ""),
                    "status": "proposed", "auto_apply": False})
    return out


def on_halt(ws, bp, *, emit=None, complete_fn=None, facts: dict | None = None) -> dict:
    """停机钩子：征集提案 → append-only 落盘 → emit。不写蓝图、不自动执行。"""
    f = dict(facts or {})
    if "orphans" not in f:
        try:
            f["orphans"] = len(orphans(bp))
        except Exception:                           # noqa: BLE001
            f["orphans"] = 0
    try:
        ps = propose(ws, facts=f, emit=emit, complete_fn=complete_fn)
        if ps:
            p = Path(ws) / ".sixcoder" / "strategy_proposals.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                for x in ps:
                    fh.write(json.dumps(x, ensure_ascii=False) + "\n")
        if emit:
            emit("strategy_proposal", {"count": len(ps), "auto_apply": False,
                                       "need_human": True})
        return {"proposals": ps, "written": len(ps), "error": ""}
    except Exception as exc:                        # noqa: BLE001
        if emit:
            emit("strategy_proposal_error", {"error": f"{type(exc).__name__}: {exc}"[:200]})
        return {"proposals": [], "written": 0,
                "error": f"{type(exc).__name__}: {exc}"[:200]}
