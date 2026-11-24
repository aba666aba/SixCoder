"""看门狗排班自检（定时开始 + 本次运行时间，**不做峰谷迎合**）。

为什么单独一个脚本：
    这条线改的是"什么时候开跑、跑多久"，一旦算错就是两种极端事故 ——
    ① 该跑没跑（白等一天，用户以为系统挂了）；
    ② 该停没停（烧穿预算）。
    跨天是这里最容易错的地方（23:50 开始等 00:00），所以单独钉。

跑法: python check_watchdog_schedule.py
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import os  # noqa: E402
import tempfile  # noqa: E402
from unittest import mock  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def _load():
    """按路径加载看门狗模块（它是脚本不是包；main() 只在 __main__ 下跑，不会被触发）。"""
    spec = importlib.util.spec_from_file_location(
        "watchdog_night", str(ROOT / "tools" / "watchdog_night.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    ok = True
    print("看门狗排班自检（定时开始 + 本次运行时间）")
    wd = _load()

    # ── 1) 时刻解析 ──────────────────────────────────────────────────────────
    print("[1] 时刻解析")
    ok &= check("18:00 → (18, 0)", wd.parse_hhmm("18:00") == (18, 0), str(wd.parse_hhmm("18:00")))
    ok &= check("空 → None（不定时）", wd.parse_hhmm("") is None)
    ok &= check("乱写 → None", wd.parse_hhmm("abc") is None)
    ok &= check("25:00 → None（越界不认）", wd.parse_hhmm("25:00") is None)
    ok &= check("18:70 → None（越界不认）", wd.parse_hhmm("18:70") is None)

    # ── 2) 还要等多久 ────────────────────────────────────────────────────────
    print("[2] 距离定时开始还有多久")
    d = datetime(2026, 11, 10, 17, 0, 0)
    ok &= check("早于开始时刻 → 等到今天那个时刻（3600 秒）",
                wd.seconds_until_start(d, "18:00", 90) == 3600.0,
                str(wd.seconds_until_start(d, "18:00", 90)))
    ok &= check("开始后 5 分钟 → 立刻开始（0 秒）",
                wd.seconds_until_start(datetime(2026, 11, 10, 18, 5, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 18, 5, 0), "18:00", 90)))
    # 2026-11-10 19:37 实测事故：原来这里是"超出 90 分钟宽限 → 等明天 18:00"，
    # 用户手动重启被判"等 22 小时 23 分"，整晚白等。手动双击的语义是**现在就跑**。
    ok &= check("过点 100 分钟 → 仍然立刻开始（不许等明天，事故钉子）",
                wd.seconds_until_start(datetime(2026, 11, 10, 19, 40, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 19, 40, 0), "18:00", 90)))
    ok &= check("当天深夜（23:59）→ 立刻开始",
                wd.seconds_until_start(datetime(2026, 11, 10, 23, 59, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 23, 59, 0), "18:00", 90)))
    ok &= check("不定时 → 立刻开始（0 秒）",
                wd.seconds_until_start(d, "", 90) == 0.0)
    # 2026-11-11 01:24 实测事故（同病根的第二种形态）：**半夜 01:24 重启**，
    # 18:00 还没到 → 旧口径判"等 16 小时 35 分"，整晚白等。
    # 现在口径：离开始时刻**超过 6 小时**就不值得等（多半是深夜/半夜重启）→ 立刻开始。
    ok &= check("半夜 01:24 重启 → 立刻开始（不许等 16 小时，事故钉子）",
                wd.seconds_until_start(datetime(2026, 11, 11, 1, 24, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 11, 1, 24, 0), "18:00", 90)))
    ok &= check("上午 11:00 重启（离 18:00 有 7 小时）→ 立刻开始",
                wd.seconds_until_start(datetime(2026, 11, 10, 11, 0, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 11, 0, 0), "18:00", 90)))
    ok &= check("中午 12:00 重启（离 18:00 正好 6 小时）→ 仍然等（边界内）",
                wd.seconds_until_start(datetime(2026, 11, 10, 12, 0, 0), "18:00", 90) == 21600.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 12, 0, 0), "18:00", 90)))
    ok &= check("傍晚 17:00 双击 → 等 1 小时（早开机的人不用干等）",
                wd.seconds_until_start(datetime(2026, 11, 10, 17, 0, 0), "18:00", 90) == 3600.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 17, 0, 0), "18:00", 90)))

    # ── 3) **跨天**：这是最容易算错的地方 ──────────────────────────────────────
    print("[3] 跨天不算错")
    # 口径已更新（2026-11-11 01:24 事故）：**最多等 MAX_WAIT_HOURS(6) 小时**，
    # 超过就不值得等（多半是深夜/半夜重启）→ 立刻开始。
    # 跨天仍要算对：只对"≤6 小时的等待"成立。
    ok &= check("00:30 等今天 23:00 → 22.5 小时 > 6 小时上限 → 立刻开始",
                wd.seconds_until_start(datetime(2026, 11, 10, 0, 30, 0), "23:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 0, 30, 0), "23:00", 90)))
    ok &= check("00:30 等今天 18:00 → 17.5 小时 > 6 小时上限 → 立刻开始",
                wd.seconds_until_start(datetime(2026, 11, 10, 0, 30, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 0, 30, 0), "18:00", 90)))
    ok &= check("23:59 等今天 18:00 → 立刻（已过当天开始时刻）",
                wd.seconds_until_start(datetime(2026, 11, 10, 23, 59, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 23, 59, 0), "18:00", 90)))
    ok &= check("01:00 等今天 18:00 → 17 小时 > 6 小时上限 → 立刻开始（不是负数）",
                wd.seconds_until_start(datetime(2026, 11, 10, 1, 0, 0), "18:00", 90) == 0.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 1, 0, 0), "18:00", 90)))
    # 跨天边界（≤6 小时的等待，跨天也算得对）
    ok &= check("23:50 等 00:00 → 还差 10 分钟（跨天算对，不多不少）",
                wd.seconds_until_start(datetime(2026, 11, 10, 23, 50, 0), "00:00", 90) == 600.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 23, 50, 0), "00:00", 90)))
    ok &= check("23:59 等 00:00 → 还差 60 秒（不是负数、不是 24 小时）",
                wd.seconds_until_start(datetime(2026, 11, 10, 23, 59, 0), "00:00", 90) == 60.0,
                str(wd.seconds_until_start(datetime(2026, 11, 10, 23, 59, 0), "00:00", 90)))

    # ── 4) 源码层：定时常量在、用已运行秒数、**没有峰谷迎合** ──────────────────
    print("[4] 源码层口径")
    src = (ROOT / "tools" / "watchdog_night.py").read_text(encoding="utf-8", errors="replace")
    ok &= check("定时常量在（START_AT / START_GRACE_MIN / RUN_HOURS）",
                "START_AT" in src and "START_GRACE_MIN" in src and "RUN_HOURS" in src, "")
    ok &= check("运行时长用已运行秒数算（run_started），不是墙钟截止",
                "run_started = time.time()" in src and "run_limit" in src, "")
    ok &= check("旧的墙钟截止（DEADLINE / WATCH_DEADLINE_H）已清掉",
                "DEADLINE" not in src and "WATCH_DEADLINE_H" not in src, "")
    ok &= check("**没有峰谷迎合的逻辑**（不认 offpeak/peak 这些词）",
                "offpeak" not in src.lower() and "peak" not in src.lower(), "")
    ok &= check("支持 --now 立刻开始（不用改常量就能马上跑）", '"--now"' in src, "")
    ok &= check("时长 0 = 不限",
                "RUN_HOURS * 3600 if RUN_HOURS > 0 else float(\"inf\")" in src, "")

    # ── 5) 交付口径：日志要说清"等到了才开始"和"这次跑多久" ──────────────────
    print("[5] 日志口径")
    ok &= check("启动时先报排班（定时开始 + 时长）",
                "排班：定时开始" in src, "")
    ok &= check("等待过程有进度播报（每 10 分钟，别让人以为死了）",
                "等待中" in src, "")
    ok &= check("结束时报本次实跑时长",
                "本次实跑" in src, "")

    # ── 6) 收敛叫人后**真的停**（2026-11-16 空转烧 ¥5 事故）─────────────────
    print("[6] 叫人后真停（不许把 rc=42 当 rc=0 续跑）")
    ok &= check("叫人退出码 42（与 cli.py 同值）",
                wd.HUMAN_STOP_RC == 42, str(wd.HUMAN_STOP_RC))
    ok &= check("rc=42 → 不续跑且留痕",
                wd.after_night_action(42, "")["continue"] is False
                and "停止续跑" in wd.after_night_action(42, "")["log"])
    ok &= check("普通 rc=0/1 → 仍续跑（没误伤正常收工）",
                wd.after_night_action(0, "")["continue"] is True
                and wd.after_night_action(1, "err")["continue"] is True)
    ok &= check("Ollama 掉线仍自愈拉起",
                wd.after_night_action(3, "xx Ollama 不可达 xx")["ollama"] is True)
    ok &= check("叫人优先于自愈（不拉模型、不续跑）",
                wd.after_night_action(42, "Ollama 不可达")["ollama"] is False)

    # 真跑一次 main：假子进程返回 42，断言只拉起 1 次夜班 —— 钉**调用路径**，
    # 不是只检查"函数存在"（旧账：三个机制代码全在、自检全绿，但恒不触发）。
    import unittest.mock as mock

    class _FakeProc:
        def wait(self):
            return 42

        def kill(self):
            pass

    started: list = []
    def _fake_popen(*a, **kw):
        started.append(a)
        return _FakeProc()

    _fake_log = mock.MagicMock()
    _fake_log.read_text.return_value = ""
    with mock.patch.object(wd.subprocess, "Popen", _fake_popen), \
            mock.patch.object(wd.time, "sleep", lambda _s: None), \
            mock.patch.object(wd, "wlog", lambda _m: None), \
            mock.patch.object(wd, "caps_done", lambda: 31), \
            mock.patch.object(wd, "spent_cny", lambda: 1.0), \
            mock.patch.object(wd, "existing_night_pid", lambda: 0), \
            mock.patch.object(wd, "LOG", _fake_log), \
            mock.patch.object(wd, "watchdog_pid_file",
                              lambda: Path(tempfile.gettempdir())
                              / ("wd-test-%d.pid" % os.getpid())), \
            mock.patch.object(sys, "argv", ["watchdog_night.py", "--now"]):
        wd.main()
    ok &= check("真跑 main：rc=42 只启动 1 次夜班，不再续跑（调用路径钉子）",
                len(started) == 1, "启动次数=" + str(len(started)))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（时刻解析 / 等待口径 / 跨天 / 不用墙钟截止 / 不做峰谷迎合 / 日志如实 / 叫人真停）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
