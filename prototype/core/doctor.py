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
CACHE_RECENT_CALLS = 20           # 巡检窗口：最近多少次**调用**（不是任务）
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


def weighted_trend(runs: list[dict]) -> dict:
    """**token 加权的**命中率趋势：前半个窗口 vs 后半个窗口。

    为什么不用"按次中位数"（2026-11-19 实测教训）：同一晚的调用是**两类人群**——
    大调用（whole_repo 17 万 token、命中 70%）和小调用（handoff 6 千 token、命中
    2%，因为前缀第一次见）。按"次"取中位数会被小调用淹没（恒 2%），
    而按 token 加权才等于"真正省了多少全价 token"——这才是钱的口径。

    窗口内对半分：`now` = 后半（最近），`prev` = 前半。掉 ≥ CACHE_DROP_ALERT 算下滑。
    用半窗而不是"最后一次"，避免最后恰好来了一次小调用就误报。
    """
    rows = [r for r in runs if int(r.get("prompt") or 0) > 0]
    if len(rows) < CACHE_TREND_MIN_RUNS:
        return {"enough": False,
                "note": f"只有 {len(rows)} 次运行（<{CACHE_TREND_MIN_RUNS}），不谈趋势"}

    def _w(rs: list[dict]) -> float:
        p = sum(int(r.get("prompt") or 0) for r in rs)
        h = sum(int(r.get("hit") or 0) for r in rs)
        return (h / p) if p else 0.0

    half = max(1, len(rows) // 2)
    prev_rows, now_rows = rows[:-half], rows[-half:]
    prev, now = _w(prev_rows), _w(now_rows)
    drop = prev - now
    return {"enough": True, "prev": round(prev, 4), "now": round(now, 4),
            "drop": round(drop, 4), "regressed": drop >= CACHE_DROP_ALERT,
            "runs": len(rows), "overall": round(_w(rows), 4),
            "last_task": str(now_rows[-1].get("task") or "")}


def cache_doctor(ws: Path, runs: list[dict] | None = None) -> list[Finding]:
    """缓存命中率巡检。runs 不传就去用量库取最近 N 次**调用**。

    **口径（2026-11-19 修正，重要）**：原来按 `task_id` 分组、取最后 10 个任务。
    但每次夜班重启都会新建一个 `task_id`，且它往往只含 1 次调用（handoff 6.1k
    token）——单次调用的前缀是第一次见，命中率天然≈0（实测恒 128/6130 = 2.1%）。
    于是"最近 2%、中位数 2%"这个数字**永远是 2%**：缓存真的掉了它也是 2%，
    缓存很健康它还是 2%（同一晚真实按调用算 68.6%）。**测的是影子不是业务事实**，
    正是 project 铁律「判定层必须看业务事实」那类缺陷。

    现在改为**按调用聚合**（`ORDER BY id DESC LIMIT CACHE_RECENT_CALLS`），
    并把每个 purpose 的命中率一起报出来——这样"哪个阶段在烧全价 token"一眼可见。
    """
    from . import remote
    out: list[Finding] = []
    if runs is None:
        runs = []
        try:
            rows = remote.usage_summary("")            # 触发建表
            _ = rows
            from . import db
            data = db.query("SELECT purpose, prompt_tokens p, cache_hit_tokens h"
                            " FROM remote_usage WHERE prompt_tokens > 0"
                            " ORDER BY id DESC LIMIT ?", (CACHE_RECENT_CALLS,))
            runs = [{"task": r["purpose"] or "(未归因)", "prompt": r["p"],
                     "hit": r["h"]} for r in reversed(data)]
        except Exception as exc:                       # noqa: BLE001
            return [Finding("cache", "info", "读不到用量库，跳过缓存巡检",
                            evidence=f"{type(exc).__name__}")]
    t = weighted_trend(runs)
    if not t.get("enough"):
        out.append(Finding("cache", "info", t.get("note", "数据不足"), ""))
        return out

    # 按 purpose 汇总，找出"最烧全价"的阶段（只报有足够样本的）
    agg: dict[str, list[int]] = {}
    for r in runs:
        p, h = int(r.get("prompt") or 0), int(r.get("hit") or 0)
        if p <= 0:
            continue
        agg.setdefault(str(r.get("task") or "?"), [0, 0])
        agg[str(r.get("task") or "?")][0] += p
        agg[str(r.get("task") or "?")][1] += h
    worst = ""
    if agg:
        items = [(name, h / p) for name, (p, h) in agg.items()]
        items.sort(key=lambda x: x[1])
        worst = "；".join("%s %.0f%%" % (n[:16], r * 100) for n, r in items[:3])

    if t["regressed"]:
        out.append(Finding(
            "cache", "warn",
            f"缓存命中率下滑：后半窗口 token 加权 {t['now']:.0%} vs 前半 "
            f"{t['prev']:.0%}（差 {t['drop']*100:.0f} 个百分点，"
            f"窗口 {t['runs']} 次调用）",
            fix=("把易变内容移出稳定前缀：时间戳/轮次/任务 id/UUID/耗时都不能进前缀"
                 "（`core/layout.py` 的 unstable_reasons 会给具体位置）；"
                 "稳定块按 id 排序、易变块放最后"),
            evidence=("按阶段：%s（最近一次属于 %s）"
                      % (worst or "无", str(t["last_task"])[:24]))))
    else:
        out.append(Finding("cache", "info",
                           f"缓存命中率正常：token 加权 {t['now']:.0%}"
                           f"（前半 {t['prev']:.0%}，窗口 {t['runs']} 次调用）",
                           evidence=("按阶段：%s" % worst) if worst else ""))
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


def balance_doctor(ws: Path) -> list[Finding]:
    """**官方余额**巡检（2026-11-11）。

    为什么要有：余额是**账号的客观事实**（DeepSeek 官方 `GET /user/balance`），
    而引擎自算的"已花费"是账本口径 —— 两者会打架（实测同一天三个数都不一样）。
    余额见底却没被发现，引擎会一直烧到 402/401 才发现。

    口径：查不到就**如实报"查不到"**，不猜、不拿估算冒充（info 级，不是 warn）。
    """
    out: list[Finding] = []
    try:
        from . import balance as _bal
        b = _bal.query()
    except Exception as exc:                             # noqa: BLE001
        return [Finding("balance", "info", f"官方余额查不到（{type(exc).__name__}）",
                        fix="查不到不影响运行；需要精确余额时手动跑 tools/ds_balance.py",
                        evidence="core/balance.py")]
    if not b.get("ok"):
        return [Finding("balance", "info", f"官方余额查不到（{b.get('detail') or '未知原因'}）",
                        fix="不影响运行；需要精确余额时手动跑 tools/ds_balance.py",
                        evidence="core/balance.py")]
    tot = b.get("total")
    cur = b.get("currency") or ""
    if b.get("available") is False:
        out.append(Finding("balance", "error", f"官方余额不可用（{tot} {cur}）",
                           fix="立即充值；余额闸会直接收工",
                           evidence="DeepSeek /user/balance"))
    elif tot is not None and float(tot) < 10:
        out.append(Finding("balance", "warn", f"官方余额偏低：{tot} {cur}",
                           fix="余额低于 ¥10，考虑充值（余额耗尽会直接收工）",
                           evidence="DeepSeek /user/balance"))
    else:
        out.append(Finding("balance", "info", f"官方余额：{tot} {cur}",
                           fix="余额正常", evidence="DeepSeek /user/balance"))
    return out


# ------------------------------------------------------------------ 统一入口
def run(ws: Path, level: str = "M", runs: list[dict] | None = None) -> dict:
    """跑所有巡检代理，返回 {findings, warn, error, ok}。"""
    ws = Path(ws)
    findings = cache_doctor(ws, runs) + doc_doctor(ws, level) + balance_doctor(ws)
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
