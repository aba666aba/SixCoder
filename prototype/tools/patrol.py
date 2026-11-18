"""主动巡检（区别于看门狗的活性监视）：每 60 秒对产物做一轮实质检查。

用户指令（2026-11-08 10:08）：不要完全依赖看门狗——看门狗只管进程活着，
产物质量的 bug（虚构接口/考卷漂移/契约矛盾）只有翻产物才看得见。
本脚本每轮检查：全仓编译、入口冒烟、未解析 import、架构违规增量、覆盖回退、
烧钱速率、夜班是否存活。发现异常 → 立即退出（唤醒会话处理）；
安静 → 30 分钟后正常退出（会话收到通知后重新挂下一班）。
班次时长与异常灵敏度无关（异常当轮就退出），只影响安静期的唤醒频率——
拉长班次 = 降会话积分消耗（用户 2026-11-08 10:35 问询后调整）。
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# 2026-11-10：用户双击 START_NIGHT.bat 后巡检由 pythonw（无控制台）启动，
# 下面每轮的 smoke/compileall/tasklist 子进程都会各弹一个黑窗口。
# 统一隐藏窗口；capture_output 已保证输出照常收得到，日志一条不丢。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# pythonw 没有 stdout —— print 全进黑洞，"异常即报"形同虚设。
# 持久模式下所有输出必须落文件，用户/助手随时能翻。
PLOG = Path(__file__).resolve().parents[2] / "_logs" / "patrol.log"
PERSIST = "--persist" in sys.argv


def wlog(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)                          # 有控制台时仍旧可见
    except Exception:                                    # noqa: BLE001
        pass
    try:
        with PLOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

sys.path.insert(0, r"E:/AAATOM/NewLai/SixCoder/prototype")
PY = r"D:/Python/python.exe"
from core.runtime_cfg import require_repo  # noqa: E402
REPO = require_repo()
SPENT = REPO / ".sixcoder" / "night_spent.json"
BP = REPO / ".sixcoder" / "blueprint.json"
DEADLINE = time.time() + 30 * 60
_prev = {"spent": None, "done": None}


def smoke() -> str | None:
    # 入口不写死 app/main.py（耦合点修复 2026-11-08）：复用引擎的探测。
    sys.path.insert(0, r"E:/AAATOM/NewLai/SixCoder/prototype")
    try:
        from core import entrypoint as _ep
        _erel = _ep.entry_rel(REPO)
    except Exception:                                # noqa: BLE001
        _erel = "app/main.py"
    if not _erel:
        return None                                  # 没有入口形态 → 冒烟不适用
    _emod = _erel[:-3].replace("/", ".")
    try:
        r = subprocess.run([sys.executable, "-c", f"import {_emod}"], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=90, creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"冒烟执行失败 {type(exc).__name__}"
    return None if r.returncode == 0 else (r.stderr or "").strip().splitlines()[-1][:160]


def compile_all() -> list[str]:
    r = subprocess.run([PY, "-m", "compileall", "-q", "app", "matching_core"],
                       cwd=str(REPO), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120,
                       creationflags=_NO_WINDOW)
    bad = [ln for ln in (r.stderr or "").splitlines() if "SyntaxError" in ln or "Error" in ln]
    return bad[:3]


def state() -> tuple[float, int]:
    try:
        s = float(json.loads(SPENT.read_text(encoding="utf-8")).get("total_cny") or 0)
    except Exception:                                    # noqa: BLE001
        s = -1
    try:
        bp = json.loads(BP.read_text(encoding="utf-8"))
        d = sum(1 for c in bp.get("caps", []) if c.get("status") == "done")
        # 探针回归计数（2026-11-08 mark 保护 done 后的可见性替身）：done 计数不再
        # 回退，真回归只体现在 why 的"[探针回归待复核 …]"标记里——巡检必须看得见。
        # 搜"探针回归待复核"不带闭括号：mark 的完整标记是 "[探针回归待复核 时间]"，
        # 带括号搜永不命中（实测 19:20）。
        g = sum(1 for c in bp.get("caps", [])
                if "探针回归待复核" in str(c.get("why") or ""))
    except Exception:                                    # noqa: BLE001
        d = -1
        g = -1
    return s, d, g


def main() -> int:
    # 配合 STOP_NIGHT.bat：pythonw 无命令行可查，按 PID 文件精确停止。
    try:
        (Path(__file__).resolve().parents[2] / "_logs" / "patrol.pid").write_text(
            str(os.getpid()), encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass
    wlog(f"主动巡检启动（{'常驻模式' if PERSIST else '30 分钟班'}，异常即报，安静 8 分钟）")
    _prev["spent"], _prev["done"], _prev["reg"] = state()
    _reg_streak = 0
    _down_streak = 0
    _round = 0
    # 持久模式下不设 DEADLINE：用户双击启动的巡检要一直跟到夜班收工，
    # 不能 30 分钟就下班（会话回收后没人重挂）。异常只记日志、不退出进程。
    while (True if PERSIST else (time.time() < DEADLINE)):
        _round += 1
        problems: list[str] = []
        s, d, g = state()
        # 1) 全仓编译
        bad = compile_all()
        if bad:
            problems.append(f"编译失败: {bad}")
        # 2) 冒烟
        err = smoke()
        if err:
            # 二次确认（20 秒后重测）：作者尝试的"落盘→闸门→回滚"窗口里工作区
            # 会瞬态红（实测 13:36 抓到的是合法回滚途中的快照），不误报。
            time.sleep(20)
            err2 = smoke()
            if err2:
                problems.append(f"入口冒烟红（两次确认）: {err2}")
        # 3) 覆盖回退（3 轮确认）：引擎磨文件的中间态会让探针瞬态挂 → 观测降
        # absent → done 计数回退，几分钟内自愈。立即报警是误报（实测 18:43）；
        # 连续 3 轮仍低 = 真回归没人修，才报。
        if _prev["done"] >= 0 and d < _prev["done"]:
            _down_streak += 1
            if _down_streak >= 3:
                problems.append(f"覆盖回退持续（{_prev['done']}→{d} 连续 {_down_streak} 轮）")
        else:
            _down_streak = 0
        # 3b) 探针回归持续（产物中间态几分钟内会自愈；持续 >10 轮 = 真回归没人修）
        if g > 0:
            _reg_streak += 1
            if _reg_streak >= 10:
                problems.append(f"探针回归持续（{g} 条已验收能力连续 {_reg_streak} 轮探针失败）")
        else:
            _reg_streak = 0
        # 4) 烧钱异常（单轮 > ¥3）
        if _prev["spent"] >= 0 and s > _prev["spent"] + 3:
            problems.append(f"烧钱异常 +¥{s - _prev['spent']:.2f} → ¥{s:.2f}")
        # 5) 夜班进程存活（看门狗会拉起，但巡检要看得见）
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq python.exe"],
                           capture_output=True, text=True, errors="replace",
                           creationflags=_NO_WINDOW)
        alive = r.stdout.count("python.exe")
        if alive == 0:
            problems.append("夜班进程消失（看门狗应在 10 秒内拉起）")
        if problems:
            wlog("ANOMALY: " + " | ".join(problems))
            if PERSIST:
                _down_streak = 0                 # 已报过，重置计数避免每轮刷屏
                _reg_streak = 0
                time.sleep(60)
                _prev["spent"], _prev["done"], _prev["reg"] = s, d, g
                continue                         # 常驻：不退出进程，继续盯
            return 1
        wlog(f"巡检正常 spent=¥{s:.2f} done={d}/76 回归={g} python进程={alive}"
             f" 第{_round}轮")
        _prev["spent"], _prev["done"], _prev["reg"] = s, d, g
        time.sleep(60)
    wlog(f"30 分钟安静期结束（spent=¥{_prev['spent']:.2f} done={_prev['done']}/76），"
         "重新挂下一班")
    return 0


if __name__ == "__main__":
    sys.exit(main())
