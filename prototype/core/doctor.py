"""定期巡检代理：cache-doctor（缓存命中率趋势）+ Doc-gardening（文档腐烂）。

为什么把两个放一起：它们是**同一个形状**——
    定期扫一遍、给出可执行的修补建议、把结论写进报告。
分级表里 L 级要求"后台 Agent 定期扫描并开修复 PR"，这一层是它的最小可信实现。

两个代理：
  · `cache_doctor`：从用量库算命中率趋势（最近 N 次 vs 中位数）。命中率掉了就点名，
    并给具体建议（前缀里混进了时间戳/轮次/任务 id 是头号原因，见 core/layout.py）。
  · `doc_doctor`：扫领域指南与治理文件——超长（>300 行）、引用了不存在的文件/函数、
    AGENTS.md 里列的目录已不存在、文档里留有 TODO。

纪律：
  · 只报告**能核对的事**（数据库行、文件是否存在、行数），不猜"文档写得好不好"；
  · 一条建议必须带"怎么改"；给不出改法的不报；
  · 数据不足（没有用量、没有文档）就说"数据不足"，不是"通过"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import scale as _scale

CACHE_TREND_MIN_RUNS = 4          # 少于 4 次运行不谈趋势
CACHE_DROP_ALERT = 0.15           # 命中率比中位数低 15 个百分点就点名
GUIDE_MAX_LINES = 300
REF_RE = re.compile(r"`((?:core|tools|checks)/[A-Za-z_0-9]+\.py)`")
DIR_RE = re.compile(r"^-\s+`([^`]+)`\s+—", re.M)


@dataclass
class Finding:
    doctor: str
    level: str            # info / warn / error
    what: str
    fix: str = ""
    evidence: str = ""

    def as_dict(self) -> dict:
        return {"doctor": self.doctor, "level": self.level, "what": self.what,
                "fix": self.fix, "evidence": self.evidence}


# ------------------------------------------------------------------ 缓存医生
def hit_trend(runs: list[dict]) -> dict:
    """命中率趋势：输入 [{task, hit, prompt}]，输出中位数/最近/是否下滑。

    抽成纯函数是为了能自检：不用真数据库也能验证"掉多少才报警"。
    """
    rows = [r for r in runs if int(r.get("prompt") or 0) > 0]
    if len(rows) < CACHE_TREND_MIN_RUNS:
        return {"enough": False, "note": f"只有 {len(rows)} 次运行（<{CACHE_TREND_MIN_RUNS}），"
                                         f"不谈趋势"}
    rates = [(int(r.get("hit") or 0) / int(r.get("prompt") or 1), r.get("task", ""))
             for r in rows]
    vals = sorted(x[0] for x in rates)
    mid = vals[len(vals) // 2]
    last_rate, last_task = rates[-1]
    drop = mid - last_rate
    return {"enough": True, "median": round(mid, 3), "last": round(last_rate, 3),
            "last_task": last_task, "drop": round(drop, 3),
            "regressed": drop >= CACHE_DROP_ALERT, "runs": len(rows)}


def cache_doctor(ws: Path, runs: list[dict] | None = None) -> list[Finding]:
    """缓存命中率巡检。runs 不传就去用量库取最近 10 次。"""
    from . import remote
    out: list[Finding] = []
    if runs is None:
        runs = []
        try:
            rows = remote.usage_summary("")            # 触发建表
            _ = rows
            from . import db
            data = db.query("SELECT task_id, sum(prompt_tokens) p, sum(cache_hit_tokens) h,"
                            " min(created_at) t FROM remote_usage GROUP BY task_id"
                            " ORDER BY t DESC LIMIT 10")
            runs = [{"task": r["task_id"], "prompt": r["p"], "hit": r["h"]} for r in data]
            runs = list(reversed(runs))
        except Exception as exc:                       # noqa: BLE001
            return [Finding("cache", "info", "读不到用量库，跳过缓存巡检",
                            evidence=f"{type(exc).__name__}")]
    t = hit_trend(runs)
    if not t.get("enough"):
        out.append(Finding("cache", "info", t.get("note", "数据不足"), ""))
        return out
    if t["regressed"]:
        out.append(Finding(
            "cache", "warn",
            f"缓存命中率下滑：最近一次 {t['last']:.0%} vs 近 {t['runs']} 次中位数 "
            f"{t['median']:.0%}（差 {t['drop']*100:.0f} 个百分点）",
            fix=("把易变内容移出稳定前缀：时间戳/轮次/任务 id/UUID/耗时都不能进前缀"
                 "（`core/layout.py` 的 unstable_reasons 会给具体位置）；"
                 "稳定块按 id 排序、易变块放最后"),
            evidence=str(t["last_task"])[:60]))
    else:
        out.append(Finding("cache", "info",
                           f"缓存命中率正常：最近 {t['last']:.0%}，"
                           f"近 {t['runs']} 次中位数 {t['median']:.0%}"))
    return out


# ------------------------------------------------------------------ 文档园丁
def doc_doctor(ws: Path, level: str = "M") -> list[Finding]:
    """文档腐烂巡检：指南超长、引用失效、目录清单过期、文档里留 TODO。"""
    ws = Path(ws)
    out: list[Finding] = []
    lv = str(level).upper()
    if lv == "S":
        return [Finding("doc", "info", "S 级不建 docs 知识库，跳过文档巡检")]

    gdir = ws / "docs" / "guides"
    if gdir.exists():
        for f in sorted(gdir.glob("*.md")):
            n = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
            if n > GUIDE_MAX_LINES:
                out.append(Finding(
                    "doc", "warn", f"领域指南过长：{f.name} 有 {n} 行（上限 {GUIDE_MAX_LINES}）",
                    fix="按模块拆成短篇，每篇只讲一个模块的约定；"
                        "AGENTS.md 里只放目录指针",
                    evidence=str(f.relative_to(ws))))
    else:
        out.append(Finding("doc", "info", "没有 docs/guides（M 级会生成，当前为空）"))

    # 引用失效：文档里点名的代码文件必须存在
    for doc in [ws / "AGENTS.md"] + sorted((ws / "docs").rglob("*.md") if (ws / "docs").exists()
                                           else []):
        if not doc.exists():
            continue
        txt = doc.read_text(encoding="utf-8", errors="replace")
        for ref in sorted(set(REF_RE.findall(txt))):
            if not (ws / ref).exists():
                out.append(Finding(
                    "doc", "warn", f"{doc.name} 引用了不存在的文件 `{ref}`",
                    fix="改指针，或把文件补回来；过期的指针比没有指针更糟",
                    evidence=str(doc.relative_to(ws))))
        # AGENTS.md 里列的目录必须存在
        if doc.name == "AGENTS.md":
            for d in sorted(set(DIR_RE.findall(txt))):
                if d in (".", "") or d.startswith("（"):
                    continue
                if not (ws / d).exists():
                    out.append(Finding(
                        "doc", "warn", f"AGENTS.md 列出的目录 `{d}` 已不存在",
                        fix="重新生成治理文件（开跑时会按当前目录重建），或手工删掉这一行",
                        evidence="AGENTS.md"))
        if re.search(r"\bTODO\b|\bFIXME\b", txt):
            out.append(Finding("doc", "info", f"{doc.name} 里还有 TODO/FIXME",
                               fix="要么写清，要么删掉；文档里的待办最容易永远留着",
                               evidence=str(doc.relative_to(ws))))
    if not out:
        out.append(Finding("doc", "info", "文档健康：没有超长指南、没有失效引用"))
    return out


# ------------------------------------------------------------------ 统一入口
def run(ws: Path, level: str = "M", runs: list[dict] | None = None) -> dict:
    """跑所有巡检代理，返回 {findings, warn, error, ok}。"""
    ws = Path(ws)
    findings = cache_doctor(ws, runs) + doc_doctor(ws, level)
    warn = [f for f in findings if f.level in ("warn", "error")]
    return {"findings": [f.as_dict() for f in findings], "warn": len(warn),
            "error": sum(1 for f in findings if f.level == "error"),
            "ok": not warn, "level": str(level).upper()}


def report_lines(result: dict) -> list[str]:
    """交付报告里的巡检小节（每条都带怎么改）。"""
    if not result:
        return []
    fs = result.get("findings") or []
    if not fs:
        return []
    lines = ["", "### 定期巡检（cache-doctor / Doc-gardening）", ""]
    for f in fs:
        mark = {"error": "**未通过**", "warn": "需处理", "info": "正常"}.get(f["level"], "—")
        line = f"- [{f['doctor']}] {mark}：{f['what']}"
        if f.get("fix"):
            line += f"　→ {f['fix']}"
        if f.get("evidence"):
            line += f"（依据：`{f['evidence']}`）"
        lines.append(line)
    return lines
