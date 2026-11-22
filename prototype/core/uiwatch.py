"""界面监督（ui_watch）：把 D7 从**手动跑一次**变成**持续看着**。

## 为什么需要（2026-11-15，用户提的缺口）

D7 能看界面，但**只在你手动跑的时候看**。项目跑起来以后页面白屏两天、
ECharts 空了两天，没人知道 —— 因为探针检查是"抽查"，不是"值班"。

这个模块补的是**持续性**，不是新的检测能力：

| 缺的 | 补的 |
|---|---|
| 检查只在手动触发时发生 | 按轮跑，**每轮记台账**（`.sixcoder/ui_health.jsonl`） |
| 一次性失败分不清抖动还是真坏 | **连续 N 轮失败才算故障**（单次记瞬时） |
| 看不出"哪条旅程坏了" | 每轮**按探针**记录通过/失败 |
| 看不出变慢 | 整轮耗时 > 历史中位数 3× → P3 提示 |

## 判据（写死在代码里，不靠人记）

| 情况 | 判定 |
|---|---|
| 连续 `STREAK_ALERT`(3) 轮有失败 | **持续故障**（P1，要人管） |
| 单轮失败、前后都通过 | **瞬时**（只记录，不告警 —— 网络抖动很常见） |
| 从失败恢复到通过 | 记一条 **recovered** |
| 整轮耗时 > 中位数 × `SLOW_RATIO`(3) | **变慢**（P3 提示） |

## 用法

```bash
# 单次体检（和 D7 一样，只是留台账）
python cli.py ui-watch --repo <仓库> --ui-base http://127.0.0.1:5173

# 值班：每 60 秒一轮，连跑 30 轮（约 30 分钟）
python cli.py ui-watch --repo <仓库> --ui-base http://127.0.0.1:5173 --rounds 30 --interval 60

# 只看台账历史（不跑浏览器）
python cli.py ui-watch --repo <仓库> --history
```

**注意**：长循环请在**自己的终端**跑。AI 会话起的进程会在会话结束时被回收（实测过一次，
¥0.76 白花且输出丢失）。
"""
from __future__ import annotations

import json
import statistics
import time
from pathlib import Path

__all__ = ["watch", "load_records", "judge", "render_history", "HEALTH_FILE",
           "STREAK_ALERT", "SLOW_RATIO"]

HEALTH_FILE = ".sixcoder/ui_health.jsonl"
STREAK_ALERT = 3
SLOW_RATIO = 3.0


def _emit(emit, kind: str, payload: dict) -> None:
    """安全emit：emit 回调可能没传，或抛异常（不能因观测把夜班带崩）。

    2026-11-19 补：watch() 早在 162/180 行就调用 _emit，但本模块一直没定义，
    check_self_undef 实测抓到 —— 一旦 D7 异常或每轮上报走到就是 NameError。
    实现与 fullgen._emit / django_wire._emit 同款（观测失败静默跳过，不炸主流程）。
    """
    if emit:
        try:
            emit(kind, payload)
        except Exception:                                # noqa: BLE001
            pass


def _path(ws) -> Path:
    return Path(ws) / HEALTH_FILE


def load_records(ws, limit: int = 200) -> list[dict]:
    """读台账（最近的 limit 条，按时间升序）。"""
    p = _path(ws)
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                try:
                    out.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out[-limit:]


def _append(ws, rec: dict) -> None:
    p = _path(ws)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _ui_caps(bp) -> list[str]:
    """蓝图里的 ui 类探针 id 列表。"""
    out = []
    for c in (getattr(bp, "caps", None) or []):
        pr = getattr(c, "probe", None) or {}
        if str(pr.get("kind") or "").lower() == "ui":
            out.append(str(getattr(c, "id", "")))
    return out


def judge(records: list[dict], *, streak_alert: int = STREAK_ALERT,
          slow_ratio: float = SLOW_RATIO) -> dict:
    """看台账给判定。`records` 按时间升序，**最后一条是本轮**。"""
    if not records:
        return {"action": "no_data", "why": "台账为空", "streak": 0,
                "last_ok": None, "recovered": False, "slow": False}
    last = records[-1]
    last_ok = bool(last.get("ok"))
    # 末尾连续失败的轮数
    streak = 0
    for r in reversed(records):
        if r.get("ok"):
            break
        streak += 1
    recovered = (last_ok and len(records) >= 2
                 and not records[-2].get("ok"))
    # 耗时：本轮 vs 历史中位数
    done = [float(r.get("elapsed_ms") or 0) for r in records[:-1] if r.get("elapsed_ms")]
    cur_ms = float(last.get("elapsed_ms") or 0)
    slow = bool(done and cur_ms > statistics.median(done) * slow_ratio
                and statistics.median(done) > 0)
    if not last_ok and streak >= streak_alert:
        action, why = "persistent", "连续 %d 轮失败（≥%d）→ 持续故障" % (streak, streak_alert)
    elif not last_ok:
        action, why = "transient", "本轮失败（连续 %d 轮）；单次可能是抖动" % streak
    elif recovered:
        action, why = "recovered", "恢复通过（上轮失败）"
    else:
        action, why = "healthy", "本轮通过（连续通过）"
    return {"action": action, "why": why, "streak": streak,
            "last_ok": last_ok, "recovered": recovered, "slow": slow,
            "cur_ms": int(cur_ms),
            "median_ms": int(statistics.median(done)) if done else 0}


def watch(ws, bp, *, base_url: str, rounds: int = 1, interval_s: int = 0,
          only=None, emit=None, screenshot_dir=None) -> dict:
    """跑 `rounds` 轮界面检查，每轮记台账，最后按台账给判定。

    **没有 ui 探针时直接返回 `no_ui`**（不报错：这个项目本来就没界面，
    不该因为"没界面"而红）。
    """
    from . import detectors as _det
    ws = Path(ws)
    caps = _ui_caps(bp)
    if not caps:
        return {"ok": True, "action": "no_ui", "why": "蓝图里没有 ui 类探针（项目无界面）",
                "rounds": 0, "caps": 0}
    if not base_url:
        return {"ok": False, "action": "no_base",
                "why": "没有前端服务地址（--ui-base）", "rounds": 0, "caps": len(caps)}
    if screenshot_dir is None:
        screenshot_dir = ws / ".sixcoder" / "screenshots"

    rounds = max(1, int(rounds or 1))
    ran = 0
    for i in range(rounds):
        t0 = time.time()
        try:
            errs = _det.d7_ui(ws, bp, base_url=base_url, only=only,
                              screenshot_dir=screenshot_dir)
        except Exception as exc:                         # noqa: BLE001
            errs = []
            _emit(emit, "notice", {"message": "D7 执行异常：%s: %s"
                                   % (type(exc).__name__, exc)})
        elapsed = int((time.time() - t0) * 1000)
        by_sym = {str(getattr(e, "symbol", "")): e for e in errs}
        per = []
        for cid in caps:
            e = by_sym.get(cid)
            per.append({"cap": cid, "ok": e is None,
                        "level": getattr(e, "level", "") if e else "",
                        "what": (getattr(e, "what", "") or "")[:300] if e else ""})
        n_fail = sum(1 for x in per if not x["ok"])
        rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "ok": n_fail == 0,
               "n_caps": len(caps), "n_fail": n_fail, "per": per,
               "elapsed_ms": elapsed, "base_url": base_url,
               "round": i + 1}
        _append(ws, rec)
        ran += 1
        if emit:
            _emit(emit, "ui_round", {"round": i + 1, "caps": len(caps),
                                     "fail": n_fail, "ms": elapsed})
        if i < rounds - 1 and interval_s > 0:
            time.sleep(max(0, int(interval_s)))

    hist = load_records(ws, limit=200)
    v = judge(hist)
    v.update({"ok": v["action"] in ("healthy", "recovered"),
              "rounds": ran, "caps": len(caps),
              "base_url": base_url})
    return v


def render_history(ws, limit: int = 20) -> str:
    """台账摘要（人读）：最近 N 轮的通过率与失败项。"""
    recs = load_records(ws, limit=limit)
    if not recs:
        return "（还没有界面监督记录）"
    v = judge(recs)
    lines = ["界面监督台账：共 %d 轮（最近 %d 条）" % (len(load_records(ws)), len(recs)),
             "判据：%s —— %s" % (v["action"], v["why"])]
    if v.get("median_ms"):
        lines.append("耗时：本轮 %dms，历史中位数 %dms%s"
                     % (v["cur_ms"], v["median_ms"], "（偏慢）" if v["slow"] else ""))
    lines.append("")
    for r in recs[-8:]:
        mark = "✓" if r.get("ok") else "✗"
        lines.append("  %s %s  %d/%d 通过  %sms"
                     % (mark, r.get("at", ""), (r.get("n_caps") or 0) - (r.get("n_fail") or 0),
                        r.get("n_caps") or 0, r.get("elapsed_ms") or 0))
        for x in (r.get("per") or []):
            if not x.get("ok"):
                lines.append("      × %s [%s] %s" % (x.get("cap"), x.get("level"),
                                                    (x.get("what") or "")[:120]))
    return "\n".join(lines)
