"""重规划：覆盖停滞时，把"现实"写回蓝图。

为什么必须有这一层：
    一份从一句话推出来的蓝图表，无论推得多好，都会在第 6 轮附近被现实推翻
    （"这两条能力其实共用一套解析"、"这条能力没有它想的那么简单"）。
    没有重规划通道，这些发现就只能进报告，进不了下一步 —— 项目会把错误假设一路做到底。
    这正是"计划错误是吸收态"的具体含义。

两条触发条件（都很窄：重规划自己要花一次远端的钱，不许频繁）：
  1. 覆盖停滞：连续 N 次观测都没有新增 done
  2. 卡死：同一能力连续失败 K 次 → 降级（拆小 / 放弃），不许一条能力吃掉全部预算

铁律：
  · **已观测通过（done）的能力不许消失**。删掉它等于把已经证明过的事实抹掉，
    后面的交付报告就会变成谎话。
  · 每次重规划 version+1，diff 追加到 .sixcoder/blueprint.diff.jsonl（人工可查）。
  · 降级必须带原因；原因会出现在 summary、报告和 diff 里。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import blueprint as _bp

STALL_ROUNDS = 2       # 连续几次观测没有新增 done 算停滞
FAIL_DESCOPE = 3       # 同一能力连续几次切片失败就降级


def stalled(history: list[dict], n: int = STALL_ROUNDS, *, pending: int = -1) -> bool:
    """覆盖是否停滞。history 来自 coverage.history()（每条至少含 "done"）。

    判据刻意简单：连续 n 次观测的 done 数一模一样。**不猜原因**（原因由 replan 去查），
    因为"为什么没进展"这个问题一旦靠猜，就会把"还没轮到的能力"当成"卡住的能力"。
    """
    if pending == 0:
        return False
    if n < 1 or len(history) < 3:
        return False
    tail = [int(h.get("done") or 0) for h in history[-3:]]
    return len(set(tail[-n - 1:])) == 1


def diff(old, new) -> dict:
    """两次蓝图之间的差异（只记结构，不记全文）。"""
    old_ids = {c.id: c for c in old.caps}
    new_ids = {c.id: c for c in new.caps}
    added = sorted(set(new_ids) - set(old_ids))
    removed = sorted(set(old_ids) - set(new_ids))
    rewired = {}
    for cid in sorted(set(old_ids) & set(new_ids)):
        a, b = old_ids[cid].deps, new_ids[cid].deps
        if a != b:
            rewired[cid] = [list(a), list(b)]
    descoped = sorted(c.id for c in new.caps
                      if c.status == "descoped" and old_ids.get(c.id)
                      and old_ids[c.id].status != "descoped")
    reopened = sorted(c.id for c in new.caps
                      if c.id in old_ids and old_ids[c.id].status == "descoped"
                      and c.status != "descoped")
    return {"version": [old.version, new.version],
            "caps": [len(old.caps), len(new.caps)],
            "budget": [round(old.total_budget_cny, 4), round(new.total_budget_cny, 4)],
            "added": added, "removed": removed, "rewired": rewired,
            "descoped": descoped, "reopened": reopened}


def _removed_done(old, new) -> list[str]:
    """被删掉的、原本已经观测通过的能力。不等于空 → 这次重规划必须被拒绝。"""
    keep = {c.id for c in new.caps}
    return [c.id for c in old.caps if c.status == "done" and c.id not in keep]


def apply_fails(bp, fails: dict, *, limit: int = FAIL_DESCOPE) -> list[str]:
    """卡死的能力就地降级（带原因）。返回被降级的 id（供报告）。"""
    out: list[str] = []
    for cid, n in (fails or {}).items():
        if int(n or 0) < limit:
            continue
        if _bp.descope(bp, cid, f"连续 {n} 次切片尝试仍未通过 → 降级（避免一条能力吃掉全部预算；"
                                "需要人工决定是拆小还是换做法）"):
            out.append(cid)
    return out


def replan(bp, *, cov: dict | None = None, ws=None, llm=None, reason: str = "",
           fails: dict | None = None, do_audit: bool = True,
           emit=None) -> dict:
    """重规划一次：降级卡死的 → 独立复评未完成部分 → 机械修补 → 记 diff。

    返回 {ok, blueprint, diff, descoped, issues, calls}。ok=False 时蓝图**不回退**
    （宁可保留旧版本继续跑，也不能因为一次重规划调用失败就丢掉已证明的进度）。
    """
    calls = 0
    if bp is None:
        return {"ok": False, "blueprint": bp, "diff": {}, "descoped": [],
                "issues": ["没有蓝图"], "calls": calls}
    old = _bp.Blueprint.from_dict(bp.to_dict())        # 深拷贝（diff 要用）
    descoped = apply_fails(bp, fails or {}, limit=FAIL_DESCOPE)
    issues: list[str] = []

    if do_audit and bp.pending():
        audit_result = _bp.audit(bp, llm=llm, emit=emit)
        calls += 1
        if audit_result.get("ok"):
            notes: list[str] = []
            _bp.repair(bp, audit_result, notes=notes)
            bp.notes.append(f"重规划(v{bp.version}→v{bp.version + 1})：{reason[:80]}"
                            + (f"；审计 {len(audit_result.get('issues') or [])} 条" if notes else ""))
            bp.notes.extend(notes[-6:])
        else:
            issues.append("复评失败：" + str(audit_result.get("error") or "")[:80])
    else:
        bp.notes.append(f"重规划(v{bp.version}→v{bp.version + 1})：{reason[:80]}（未复评）")

    lost = _removed_done(old, bp)
    if lost:
        # 不该发生（repair 里已经拦过），这里是第二道闸：真发生了就把它们放回去。
        for cid in lost:
            src = old.by_id(cid)
            if src:
                bp.add(src)
                bp.notes.append(f"拒绝删除已通过的能力 {cid}（已放回）")

    bp.version = int(bp.version) + 1
    d = diff(old, bp)
    d["at"] = round(time.time(), 1)
    d["reason"] = reason[:200]
    if ws is not None:
        try:
            record_diff(ws, d)
            _bp.save(bp, ws)
        except Exception as exc:                         # noqa: BLE001
            issues.append(f"重规划落盘失败：{type(exc).__name__}")
    issues += _bp.fatal(_bp.validate(bp))
    if emit:
        emit("replan", {"version": bp.version, "added": d.get("added"),
                        "removed": d.get("removed"), "rewired": list(d.get("rewired") or {}),
                        "descoped": d.get("descoped"), "reason": reason[:120]})
    return {"ok": not issues, "blueprint": bp, "diff": d, "descoped": descoped,
            "issues": issues, "calls": calls}


def record_diff(ws, d: dict) -> Path:
    p = Path(ws) / _bp.DIFF_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(d, ensure_ascii=False) + "\n")
    return p


def history(ws) -> list[dict]:
    p = Path(ws) / _bp.DIFF_FILE
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:                                # noqa: BLE001
            continue
    return out
