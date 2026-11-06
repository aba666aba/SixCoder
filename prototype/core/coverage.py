"""覆盖：项目的**真实进度**（实测出来的），不是计划完成度（自己声明的）。

这个量以前不存在。内循环只知道自己那份契约里 20/20；它不知道自己"少了一个子系统"，
于是计划一旦切错，第 7 轮、第 12 轮照样全绿 —— 验证是局部的，错误是全局的。
这里补上的就是这一条：每轮跑完，重新按**目标**（蓝图）观测一遍整个项目。

顺带白拿一个能力：**项目级回归网**。每次全量重测已通过的能力，
所以"第 9 轮改坏了第 2 轮的东西"会当场变红，而不是等人去读交付报告。

五种状态，每种都有明确含义（不许含糊）：
  done        探针真跑通（唯一的"完成"）
  absent      探针跑了，不通过 —— 功能不存在或没写对
  blocked     只在**报告层**用：依赖没完成但它自己也没通过（观测不跳过，见下）
  descoped    主动放弃（必须有 why）
  unsupported 探针种类还没有执行器（永远不算 done，不假装支持）

**依赖没完成也照样测**（实测 run 7 的教训）：一条 e2e 能力红了，挂在它下面的 7 条 CLI
能力全被判 blocked 不测 → 整轮没有任何信号。依赖只是计划概念，观测要量现实。
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from . import blueprint as _bp
from . import probe as _probe

COVERAGE_FILE = ".sixcoder/coverage.jsonl"
HISTORY_KEEP = 400          # 历史只留最近这么多次观测（够判停滞，不至于无限长）


def run(bp, ws, *, timeout: int = _probe.PROBE_TIMEOUT, only=None, emit=None) -> dict:
    """按蓝图观测整个项目。返回 cov（含每条能力的真实状态）。

    `only` 只测指定能力（增量用）；默认全量重测 —— 全量才有回归网。
    """
    ws = Path(ws).resolve()
    order, _missing = _bp.topo(bp)
    want = set(only) if only is not None else None
    done: set[str] = set()
    caps: dict[str, dict] = {}
    t0 = time.time()
    for cid in order:
        c = bp.by_id(cid)
        if c is None:
            continue
        if c.status == "descoped" and (want is None or cid not in want):
            caps[cid] = {"status": "descoped", "detail": c.why or "已降级"}
            continue
        if want is not None and cid not in want:
            # 不测的能力按已知状态记（不能被当成 done）
            caps[cid] = {"status": c.status if c.status in _bp.STATUSES else "absent",
                         "detail": c.why or "（本次未观测）"}
            if c.status == "done":
                done.add(cid)
            continue
        dep_missing = [d for d in c.deps if d not in done]
        # **照样观测**，不跳过。早先的做法是"依赖没完成就不测，免得假红"，实测（run 7）
        # 代价太大：一条 e2e 能力红了，挂在它下面的 7 条 CLI 能力全部变成 blocked，
        # 于是整轮没有任何信号，只能靠重规划兜 —— 而它们其实可能是各自独立的命令。
        # 依赖只是**计划**的概念；观测要量现实：探针能跑通就算通。
        r = _probe.run(ws, c.probe, timeout=timeout)
        if r.get("ok"):
            status, detail = "done", str(r.get("detail") or "通过")
            done.add(cid)
        elif r.get("invalid"):
            status, detail = "unsupported", "探针不合规：" + str(r.get("detail") or "")
        else:
            status = "absent"
            detail = str(r.get("detail") or "不通过")
            if dep_missing:
                detail += "（依赖未完成：" + ", ".join(dep_missing) + "）"
            if r.get("engine_error"):
                detail = "（探针引擎出错，不是产物的问题）" + detail
        caps[cid] = {"status": status, "detail": detail,
                     "took_s": r.get("took_s"), "evidence": r.get("evidence")}
        if emit:
            emit("coverage", {"cap": cid, "status": status, "detail": detail[:120]})
    total = len([c for c in bp.caps if c.status != "descoped"])
    cov = {"at": round(time.time(), 1), "goal": bp.goal, "version": bp.version,
           "total": total, "done": sorted(done), "caps": caps,
           "took_s": round(time.time() - t0, 1)}
    return cov


def done_count(cov: dict) -> int:
    return len(cov.get("done") or [])


def ratios(cov: dict) -> dict:
    total = int(cov.get("total") or 0)
    d = done_count(cov)
    return {"done": d, "total": total, "pct": round(100.0 * d / total, 1) if total else 0.0}


def record(ws, cov: dict, note: str = "") -> Path:
    """落一条观测历史（停滞判断要用；也是"每轮真实进度"的账）。"""
    p = Path(ws) / COVERAGE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.fromtimestamp(cov.get("at") or time.time()).strftime("%H:%M:%S"),
           "ts": cov.get("at") or round(time.time(), 1),
           "version": cov.get("version"), "done": done_count(cov),
           "total": cov.get("total"),
           "statuses": {k: v.get("status") for k, v in (cov.get("caps") or {}).items()},
           "note": note[:120]}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _trim(p)
    return p


def _trim(p: Path) -> None:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= HISTORY_KEEP:
        return
    try:
        p.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def history(ws, limit: int = 20) -> list[dict]:
    p = Path(ws) / COVERAGE_FILE
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:                                # noqa: BLE001
            continue
    return out[-limit:] if limit else out


def stalled(ws, bp=None, n: int | None = None) -> bool:
    from . import replan as _replan
    hs = history(ws, limit=max(6, (n or _replan.STALL_ROUNDS) + 3))
    pending = len(bp.pending()) if bp is not None else -1
    return _replan.stalled(hs, n or _replan.STALL_ROUNDS, pending=pending)


def mark(bp, cov: dict):
    """把观测结果写回蓝图（返回副本，不动调用方那份）。

    降级状态**保留**：探针通过不算"撤销降级"，那要人来说。
    """
    out = _bp.Blueprint.from_dict(bp.to_dict())
    for cid, row in (cov.get("caps") or {}).items():
        c = out.by_id(cid)
        if c is None:
            continue
        st = row.get("status") or "absent"
        if c.status == "descoped" and st != "descoped":
            continue
        c.status = st if st in _bp.STATUSES else "absent"
        if st != "done":
            c.why = str(row.get("detail") or "")[:200]
        elif c.status == "done":
            c.why = ""
    return out


def report(bp, cov: dict) -> str:
    """给人看：完成了什么、缺什么、为什么缺。"""
    r = ratios(cov)
    caps = cov.get("caps") or {}
    n_absent = sum(1 for v in caps.values() if v.get("status") == "absent")
    n_block = sum(1 for v in caps.values() if v.get("status") == "blocked")
    n_desc = sum(1 for v in caps.values() if v.get("status") == "descoped")
    n_unsup = sum(1 for v in caps.values() if v.get("status") == "unsupported")
    head = (f"覆盖 {r['done']}/{r['total']}（{r['pct']}%）"
            f"｜缺 {n_absent}｜受阻 {n_block}｜降级 {n_desc}"
            + (f"｜无执行器 {n_unsup}" if n_unsup else "")
            + f"｜观测耗时 {cov.get('took_s')}s")
    lines = [head]
    marks = {"done": "✓", "absent": "✗", "blocked": "⊘", "descoped": "降级", "unsupported": "?"}
    for c in bp.caps:
        row = caps.get(c.id) or {}
        st = row.get("status") or c.status
        detail = str(row.get("detail") or "")
        lines.append(f"  {marks.get(st, st):<4}{c.id:<26}{c.desc[:32]:<34}"
                     + ("" if st == "done" else detail[:60]))
    gaps = [c for c in bp.caps if (caps.get(c.id) or {}).get("status") != "done"
            and (caps.get(c.id) or {}).get("status") != "descoped"]
    if gaps:
        lines.append(f"缺口 {len(gaps)} 条（下一步由 frontier 决定先修哪条）")
    return "\n".join(lines)
