"""已知缺口台账：**只记录，不降权**（2026-11-10 用户定稿）。

为什么有这个模块（缺陷 #2）：
    系统没有"给写不出的模块封顶"的策略 —— 真写不出的模块会一路升到最高档反复重试，
    烧光整包预算，把本可完成的模块挤掉（实测 11-10：74% 花费钉在 max）。

    但**直接加"放弃/隔离"是危险的**：阈值定错会把"再试两轮就能写对"的模块误杀，
    重演 11-09 校准器压封顶的恶性闭环（把机制问题记成能力问题）。
    所以用户定的是**保守版**：本模块**只记录**每模块成功率与"连续最高档仍红"，
    **完全不改派单、不改预算消耗** —— 等拿到数据后由用户拍板要不要真的降权。

数据来源：`.sixcoder/author_attempts.jsonl`（写作尝试台账，append-only）。
    每条：{at, path, effort, ok, funcs, cost_cny, kind}
    · kind="module" —— **模块级**写码尝试（函数体主路 / 整文件路）
    · kind="order"  —— **工作单级**（单个函数；只记远端档，本地档不记）
    旧记录没有 kind → 按 "module" 处理（向后兼容）。

口径（全部确定性，同输入同输出）：
    · 成功率      = 成功次数 ÷ 尝试次数（按模块聚合）
    · 最高档连败  = 从最后一次成功之后，**连续在最高档失败**的次数
                   （成功归零；失败但发生在更低档 → 也归零，
                     因为那说明还没顶格，不能算"顶格也写不出"）
    · 已知缺口候选 = 最高档连败 ≥ GAP_STREAK_MIN —— **只是标记，不派单**
"""
from __future__ import annotations

import json
from pathlib import Path

LEDGER = ".sixcoder/author_attempts.jsonl"
TOP_EFFORT = "max"
GAP_STREAK_MIN = 3


def read(ws: Path | None) -> list[dict]:
    """读写作尝试台账（坏行跳过，绝不抛错）。"""
    if ws is None:
        return []
    p = Path(ws) / LEDGER
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _kind(rec: dict) -> str:
    return str(rec.get("kind") or "module")


def summarize(ws: Path | None, streak_min: int = GAP_STREAK_MIN) -> dict[str, dict]:
    """按模块聚合：成功率、各档成败、最高档连败、花费。

    `streak_min` 只决定 `known_gap` 这个**标记**；本模块不改任何派单行为。
    """
    per: dict[str, dict] = {}
    for rec in read(ws):
        path = str(rec.get("path") or "").strip()
        if not path:
            continue
        d = per.setdefault(path, {
            "path": path, "attempts": 0, "successes": 0, "failures": 0,
            "success_rate": 0.0, "cost_cny": 0.0, "funcs": 0,
            "by_effort": {}, "by_kind": {}, "top_fail_streak": 0,
            "known_gap": False, "first_at": "", "last_at": "",
        })
        ok = bool(rec.get("ok"))
        eff = str(rec.get("effort") or "none").lower()
        kind = _kind(rec)
        d["attempts"] += 1
        d["successes" if ok else "failures"] += 1
        d["cost_cny"] = round(d["cost_cny"] + float(rec.get("cost_cny") or 0.0), 5)
        d["funcs"] = max(int(d["funcs"] or 0), int(rec.get("funcs") or 0))
        slot = d["by_effort"].setdefault(eff, {"ok": 0, "fail": 0})
        slot["ok" if ok else "fail"] += 1
        kslot = d["by_kind"].setdefault(kind, {"ok": 0, "fail": 0})
        kslot["ok" if ok else "fail"] += 1
        at = str(rec.get("at") or "")
        if at:
            if not d["first_at"] or at < d["first_at"]:
                d["first_at"] = at
            if at > d["last_at"]:
                d["last_at"] = at
        # 最高档连败：成功归零；失败但在更低档也归零（还没顶格）
        if ok:
            d["top_fail_streak"] = 0
        elif eff == TOP_EFFORT:
            d["top_fail_streak"] += 1
        else:
            d["top_fail_streak"] = 0
    for d in per.values():
        n = int(d["attempts"] or 0)
        d["success_rate"] = round(int(d["successes"]) / n, 3) if n else 0.0
        d["known_gap"] = int(d["top_fail_streak"] or 0) >= int(streak_min)
    return per


def candidates(ws: Path | None, streak_min: int = GAP_STREAK_MIN) -> list[dict]:
    """已知缺口候选（**只是标记**）：按连败次数从多到少。"""
    per = summarize(ws, streak_min)
    out = [d for d in per.values() if d.get("known_gap")]
    out.sort(key=lambda d: (-int(d.get("top_fail_streak") or 0),
                             -float(d.get("cost_cny") or 0.0)))
    return out


def report_lines(ws: Path | None, streak_min: int = GAP_STREAK_MIN) -> list[str]:
    """交付报告里的一节：各模块写作成功率 + 已知缺口候选（明说没有降权）。"""
    per = summarize(ws, streak_min)
    if not per:
        return ["## 各模块写作成功率", "",
                "- 还没有写作尝试记录（台账为空）—— 本夜还没写过任何模块，或台账未开启。"]
    lines = ["## 各模块写作成功率（**只记录，不降权**）", "",
             "| 模块 | 尝试 | 成功 | 成功率 | 最高档连败 | 花费 |", "|---|---|---|---|---|---|"]
    for path in sorted(per):
        d = per[path]
        lines.append(f"| {path} | {d['attempts']} | {d['successes']} | "
                     f"{d['success_rate']:.0%} | {d['top_fail_streak']} | "
                     f"¥{d['cost_cny']:.4f} |")
    cands = [d for d in per.values() if d.get("known_gap")]
    lines.append("")
    if cands:
        names = "、".join(f"`{d['path']}`（最高档连败 {d['top_fail_streak']}）"
                          for d in sorted(cands, key=lambda x: -int(x.get("top_fail_streak") or 0)))
        lines += [f"- **已知缺口候选**：{names}",
                  "",
                  "> 这些是「顶格（max）也写不出」的候选。**本夜仍照常派单、照常消耗预算** —— "
                  "本表只做记录，等数据攒够再决定要不要给它们封顶。"]
    else:
        lines.append("- 暂无「连续顶格仍写不出」的候选模块。")
    return lines
