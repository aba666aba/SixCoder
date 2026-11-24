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
try:                                                     # 安装版：数据目录与代码目录分离
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from core import paths as _paths                      # noqa: E402
    PLOG = _paths.logs_dir() / "patrol.log"
except Exception:                                        # noqa: BLE001
    pass
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

PROTO = Path(__file__).resolve().parents[1]          # prototype/（安装版也在 resources 下）
sys.path.insert(0, str(PROTO))
# 解释器用**当前这个**：安装版是 resources\python\python.exe，写死 D:\Python 会指向不存在的路径
PY = sys.executable
from core.runtime_cfg import require_repo  # noqa: E402
REPO = require_repo()
SPENT = REPO / ".sixcoder" / "night_spent.json"
BP = REPO / ".sixcoder" / "blueprint.json"
DEADLINE = time.time() + 30 * 60
_prev = {"spent": None, "done": None, "reg": None}
# 连续多少轮看不到本仓库的夜班/看门狗 → 认定这一班已经跑完，巡检退场。
# 为什么必须有这个出口：看门狗收工（26-11-20 01:17 实测）后巡检 `while True` 还在跑，
# 每分钟对整个产物仓做一次全量编译 + 冒烟，并报"夜班进程消失"的假异常把日志刷了 8700 行。
IDLE_EXIT_ROUNDS = 5


def smoke() -> str | None:
    # 入口不写死 app/main.py（耦合点修复 2026-11-08）：复用引擎的探测。
    # 路径用 PROTO（已在文件顶部按 __file__ 算好），不写死仓库绝对路径 —— 否则安装版直接失效。
    sys.path.insert(0, str(PROTO))
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


def state() -> tuple[float, int, int, int]:
    try:
        s = float(json.loads(SPENT.read_text(encoding="utf-8")).get("total_cny") or 0)
    except Exception:                                    # noqa: BLE001
        s = -1
    try:
        bp = json.loads(BP.read_text(encoding="utf-8"))
        total = len(bp.get("caps", []))
        d = sum(1 for c in bp.get("caps", []) if c.get("status") == "done")
        # 探针回归计数（2026-11-08 mark 保护 done 后的可见性替身）：done 计数不再
        # 回退，真回归只体现在 why 的"[探针回归待复核 …]"标记里——巡检必须看得见。
        # 搜"探针回归待复核"不带闭括号：mark 的完整标记是 "[探针回归待复核 时间]"，
        # 带括号搜永不命中（实测 19:20）。
        g = sum(1 for c in bp.get("caps", [])
                if "探针回归待复核" in str(c.get("why") or ""))
        total = len(bp.get("caps", []))
    except Exception:                                    # noqa: BLE001
        d = -1
        g = -1
        total = 0
    return s, d, g, total


def _pid_alive(pid: int) -> bool:
    """进程还在不在（不依赖 psutil，Windows 用 OpenProcess 探针）。"""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).is_running()
    except Exception:                                    # noqa: BLE001
        pass
    try:                                                 # Windows 兜底
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:                                    # noqa: BLE001
        return False


def claim_single_instance() -> bool:
    """**单实例守卫**（2026-11-19）：patrol 之前每次启动都新起一个、旧的从不退出。

    实测：一天里双击 4 次 START_NIGHT 就留下 4 个常驻 patrol，都在轮询同一个仓库
    （全量编译 + 状态查询，纯重复劳动）。守卫规则与 watchdog_night 一致：
    pid 文件里的巡检**还活着** → 本实例退出；死了/没有 → 接管写自己的 pid。
    """
    pf = Path(__file__).resolve().parents[2] / "_logs" / "patrol.pid"
    try:
        old = int(pf.read_text(encoding="utf-8").strip() or 0)
    except Exception:                                    # noqa: BLE001
        old = 0
    if old and old != os.getpid() and _pid_alive(old):
        wlog(f"已有巡检实例 PID={old} 在跑（防重复守卫）→ 本实例退出")
        return False
    try:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass
    return True


def _own_night_alive() -> int:
    """本仓库自己的夜班/看门狗进程数（**不是**数所有 python.exe）。

    为什么要改（2026-11-20 实测）：原来用 `tasklist | count python.exe`，
    那是"整台机器上有几个 python"—— 与本班夜班毫无关系。夜班一收工就恒为 0，
    巡检于是每分钟报一次"夜班进程消失"，判据自己制造了 8700 行假异常。
    而且夜班实际由 pythonw 拉起（cli.py night / watchdog_night.py），
    数 python.exe 本来也数不到它 —— 两头都错。
    """
    try:
        import psutil
    except Exception:                                    # noqa: BLE001
        return -1                                        # 未知：调用方不当成"已退场"
    key = str(REPO).replace("\\", "/").lower().rstrip("/")
    n = 0
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or "").replace("\\", "/")
        except Exception:                                # noqa: BLE001
            continue
        low = cmd.lower()
        if key and key not in low:
            continue                                     # 不是本仓库的夜班（别的项目在跑，别算进来）
        if "watchdog_night.py" in low or ("cli.py" in low and " night" in low):
            n += 1
    return n


def _clear_own_pid() -> None:
    """退场时删掉自己的 pid 文件，别留一个指向死进程的残骸。"""
    try:
        pf = Path(__file__).resolve().parents[2] / "_logs" / "patrol.pid"
        if pf.is_file() and pf.read_text(encoding="utf-8").strip() == str(os.getpid()):
            pf.unlink()
    except Exception:                                    # noqa: BLE001
        pass


def main() -> int:
    # 单实例守卫：**必须在写 pid 之前**（否则自己先覆盖了 pid 文件，就查不到别人）
    if not claim_single_instance():
        return 0
    wlog(f"主动巡检启动（{'常驻模式' if PERSIST else '30 分钟班'}，异常即报，安静 8 分钟）")
    _prev["spent"], _prev["done"], _prev["reg"], _total = state()
    _reg_streak = 0
    _down_streak = 0
    _idle = 0                     # 连续多少轮看不到本仓库的夜班
    _round = 0
    # 持久模式下不设 DEADLINE：用户双击启动的巡检要一直跟到夜班收工，
    # 不能 30 分钟就下班（会话回收后没人重挂）。异常只记日志、不退出进程。
    while (True if PERSIST else (time.time() < DEADLINE)):
        _round += 1
        problems: list[str] = []
        s, d, g, total = state()
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
        # 5) 本仓库夜班是否还在跑：精确匹配 watchdog_night.py / cli.py night
        alive = _own_night_alive()
        if alive == 0:
            _idle += 1
            if PERSIST and _idle >= IDLE_EXIT_ROUNDS:
                wlog(f"本仓库夜班与看门狗均已退场（连续 {_idle} 轮）"
                     f" → 巡检收工（spent=¥{s:.2f} done={d}/{total}）")
                _clear_own_pid()
                return 0
            wlog(f"本仓库暂无夜班在跑（{_idle}/{IDLE_EXIT_ROUNDS} 轮），"
                 f"可能已收工；继续观察")
        else:
            _idle = 0
        if problems:
            wlog("ANOMALY: " + " | ".join(problems))
            if PERSIST:
                _down_streak = 0                 # 已报过，重置计数避免每轮刷屏
                _reg_streak = 0
                time.sleep(60)
                _prev["spent"], _prev["done"], _prev["reg"] = s, d, g
                continue                         # 常驻：不退出进程，继续盯
            return 1
        wlog(f"巡检正常 spent=¥{s:.2f} done={d}/{total} 回归={g} 夜班进程={alive}"
             f" 第{_round}轮")
        _prev["spent"], _prev["done"], _prev["reg"] = s, d, g
        time.sleep(60)
    wlog(f"30 分钟安静期结束（spent=¥{_prev['spent']:.2f} done={_prev['done']}/{_total}），"
         "重新挂下一班")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
