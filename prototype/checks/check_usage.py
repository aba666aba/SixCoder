"""用量与花费自检：按模型档案的人民币价目算钱，峰谷按北京时间判定。

为什么单开一个：价目表从"美元拍脑袋"换成官方人民币口径后，凡是按旧字段
（cost_usd / offpeak_ratio / offpeak_window）算钱的地方都会静默算错。
这里把"忙时按高峰价、闲时按半价、命中比未命中便宜"钉死。

跑法: python check_usage.py
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db, remote, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def ts(y: int, m: int, d: int, hh: int, mm: int = 0) -> float:
    """UTC 时间戳（用来构造"北京几点"的测试点）。"""
    return float(calendar.timegm((y, m, d, hh, mm, 0, 0, 0, 0)))


def main() -> int:
    print("用量统计自检（人民币价目 + 峰谷判定）")
    remote._ensure_table()
    tag = "usage-check"
    db.execute("DELETE FROM remote_usage WHERE task_id=?", (tag,))

    # 1) 价目来自模型档案，且是人民币口径
    prof = next((x for x in settings_store.models() if x["model"] == "deepseek-flash"), None)
    check("能找到 flash 档案（价格来源）", prof is not None)
    off_p, peak_p = prof["pricing"]["offpeak"], prof["pricing"]["peak"]
    check("高峰价 = 空闲价的 2 倍",
          abs(peak_p["in_miss"] - off_p["in_miss"] * 2) < 1e-9
          and abs(peak_p["out"] - off_p["out"] * 2) < 1e-9, f"{off_p} / {peak_p}")
    check("缓存命中远便宜于未命中（≥10 倍）",
          off_p["in_miss"] / max(off_p["in_hit"], 1e-9) >= 10, str(off_p))

    # 2) 两条记录：忙时 1000in/200out；闲时 2000in（其中 1000 命中）/400out
    remote._record(task_id=tag, node_id="busy", model="deepseek-flash",
                   prompt_tokens=1000, completion_tokens=200, cache_hit_tokens=0)
    db.execute("UPDATE remote_usage SET offpeak=0 WHERE task_id=? AND node_id='busy'", (tag,))
    remote._record(task_id=tag, node_id="off", model="deepseek-flash",
                   prompt_tokens=2000, completion_tokens=400, cache_hit_tokens=1000)
    db.execute("UPDATE remote_usage SET offpeak=1 WHERE task_id=? AND node_id='off'", (tag,))

    s = remote.usage_summary(tag)
    check("总数正确", s["calls"] == 2 and s["prompt_tokens"] == 3000
          and s["completion_tokens"] == 600, str(s)[:140])
    check("忙时单独统计", s["peak"]["calls"] == 1 and s["peak"]["prompt_tokens"] == 1000)
    check("闲时单独统计", s["offpeak"]["calls"] == 1 and s["offpeak"]["prompt_tokens"] == 2000)

    want_busy = 1000 / 1e6 * peak_p["in_miss"] + 200 / 1e6 * peak_p["out"]
    want_off = (1000 / 1e6 * off_p["in_miss"] + 1000 / 1e6 * off_p["in_hit"]
                + 400 / 1e6 * off_p["out"])
    check("忙时成本 = 未命中输入 + 输出（高峰价）",
          abs(s["peak"]["cost_cny"] - want_busy) < 1e-9,
          f"{s['peak']['cost_cny']} vs {want_busy}")
    check("闲时成本用半价", abs(s["offpeak"]["cost_cny"] - want_off) < 1e-9,
          f"{s['offpeak']['cost_cny']} vs {want_off}")
    check("总成本 = 忙 + 闲", abs(s["cost_cny"] - (want_busy + want_off)) < 1e-9, str(s["cost_cny"]))
    check("缓存省下的钱 > 0", s["cache_saved_cny"] > 0, str(s["cache_saved_cny"]))
    check("命中率按命中/总输入算", abs(s["cache_hit_rate"] - round(1000 / 3000, 3)) < 1e-6,
          str(s["cache_hit_rate"]))
    check("按模型分开统计", "deepseek-flash" in (s.get("by_model") or {}), str(s.get("by_model")))
    check("按思考档位分开统计", bool(s.get("by_effort")), str(s.get("by_effort")))
    check("外币字段已移除（不许再冒出 cost_usd）", "cost_usd" not in s)

    # 3) 峰谷判定：官方口径是北京时间工作日 9-12、14-18 为高峰
    print("    高峰窗口:", (s["window"] or {}).get("ranges"))
    old = settings_store.get_remote().get("peak_window")
    try:
        settings_store.update_remote({"peak_window": {
            "ranges": ["09:00-12:00", "14:00-18:00"], "weekdays_only": True,
            "tz": "Asia/Shanghai"}})
        in_peak = remote.is_offpeak(ts(2026, 11, 4, 3))       # 周三 UTC03 = 北京11 → 高峰
        lunch = remote.is_offpeak(ts(2026, 11, 4, 5))         # 周三 UTC05 = 北京13 → 空闲
        night = remote.is_offpeak(ts(2026, 11, 4, 13))        # 周三 UTC13 = 北京21 → 空闲
        sunday = remote.is_offpeak(ts(2026, 11, 8, 3))        # 周日 UTC03 = 北京11 → 空闲
        check("工作日 11:00 是高峰", in_peak is False, f"is_offpeak={in_peak}")
        check("工作日午休 13:00 是空闲", lunch is True, f"is_offpeak={lunch}")
        check("工作日夜间 21:00 是空闲", night is True, f"is_offpeak={night}")
        check("周日 11:00 全天空闲", sunday is True, f"is_offpeak={sunday}")
    finally:
        if old:
            settings_store.update_remote({"peak_window": old})

    # 4) 不同模型的价差不串味：pro 的输入价必须明显高于 flash
    pro = next((x for x in settings_store.models() if x["model"] == "deepseek-v4-pro"), None)
    check("pro 与 flash 是分开的两套价",
          pro is not None and pro["pricing"]["offpeak"]["in_miss"] > off_p["in_miss"],
          f"{pro['pricing']['offpeak'] if pro else None}")

    db.execute("DELETE FROM remote_usage WHERE task_id=?", (tag,))
    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（人民币口径 / 峰谷半价 / 命中折扣 / 按模型与档位分账）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
