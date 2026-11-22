"""夜班看门狗：启动/续跑夜班，掉线自动拉起，里程碑留痕。

用户指令（2026-11-08 02:41）：一直盯着直到项目完成，有 bug 就修。
本脚本由会话后台任务托管运行；夜班是它的子进程，退出即续跑（断点续跑由
cli.py night 自带）。

排班（2026-11-10 用户定稿）：**定时开始 + 本次运行时间**，不做峰谷迎合。
    · START_AT   每天固定时刻开始（如 "18:00"），带宽限窗口（差几分钟也立刻开始）
    · RUN_HOURS  本次最多跑多久（用已运行秒数算 → 跨天不会算错）
    · --now      命令行可跳过等待，立刻开始
退出条件：预算兜底线（默认不启用）/ 覆盖 76/76 / 重启 20 次 / 本次运行时间到 /
          **引擎叫人 rc=42**（振荡/停滞/预算/远端配置 → 真的停下，不续跑）。
"""
import json
import subprocess
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# 路径全部运行时推导，绝不写死本机绝对路径（否则别人装上夜班起不来）。
#   · PY：当前解释器的同目录 python.exe（有控制台）；没有就退回当前解释器。
#         注意本看门狗由 pythonw（无控制台）拉起，所以 sys.executable 可能是 pythonw.exe，
#         必须换成 python.exe 再配合 CREATE_NO_WINDOW，避免子进程反复闪黑窗。
#   · PROTOTYPE：本文件 tools/ 的父目录，就是 prototype。
def _python_exe() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")
    return str(cand if cand.is_file() else exe)

PY = _python_exe()
_ENGINE = Path(__file__).resolve().parents[1]
PROTOTYPE = str(_ENGINE)
# **引擎不含项目身份**（2026-11-19）：路径来自 --repo / SIXCODER_REPO /
# _logs/engine.json，三级都没有就明确报错（见 core/runtime_cfg.py）。
if str(_ENGINE) not in sys.path:
    sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import require_repo as _require_repo  # noqa: E402

def _cli_repo() -> str:
    for i, a in enumerate(sys.argv):
        if a == "--repo" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return ""

def _cli_opt(name: str) -> str:
    """读 `--x value` 或 `--x=value`（只透传需要的几个，不做完整参数解析）。"""
    for i, a in enumerate(sys.argv):
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return ""


REPO = str(_require_repo(_cli_repo()))
# 目标来源（2026-11-20）：桌面界面起夜班时，用户的任务文本不能丢。
#   · `--goal-file`        → 全自主（一句话/一段目标）
#   · `--requirement-file` → 甲方需求
#   · 都没给 → 沿用仓库里的 REQUIREMENTS.md（双击 START_NIGHT.bat 的老行为，不能变）
GOAL = _cli_opt("--goal-file") or (REPO + r"/REQUIREMENTS.md")
REQ_FILE = _cli_opt("--requirement-file")
# 日志走运行期数据目录（安装版是 SIXCODER_HOME），绝不写死本机绝对路径。
try:
    from core import paths as _paths                      # noqa: E402
    _LOG_DIR = _paths.logs_dir()
except Exception:                                        # noqa: BLE001
    _LOG_DIR = _ENGINE.parent / "_logs"                   # 退回引擎同级 _logs（相对本文件）

# 每个仓库一份日志 + 一个指针（2026-11-21）：原来所有仓库共用
# `night_20261108_run19.log` 并 append，界面按"最新文件"读尾巴，切仓库时会显示成
# 别的项目的进度；文件也会无限长。现在 = night_<repo标签>_<启动时间>.log，
# 并把路径写进 night_<标签>.current，状态接口按仓库精确取。
try:
    _REPO_TAG = _paths.night_tag(REPO)
except Exception:                                        # noqa: BLE001
    _REPO_TAG = "repo"
LOG = _LOG_DIR / f"night_{_REPO_TAG}_{time.strftime('%Y%m%d_%H%M%S')}.log"
try:
    (_LOG_DIR / f"night_{_REPO_TAG}.current").write_text(str(LOG), encoding="utf-8")
except OSError:
    pass
WLOG = _LOG_DIR / "watchdog.log"
# 「这一夜还没收工」标记：安装器升级时会杀掉安装目录下的 python（夜班跟着死），
# 正常停/跑完时删掉它；后端下次启动看到标记且没有夜班在跑，就自动续跑
# （2026-11-21：用户装一次新版本夜班就没了，反复踩）。
WANT = _LOG_DIR / f"night_{_REPO_TAG}.want"
try:
    WANT.write_text(json.dumps({
        "repo": REPO,
        "args": ["-u", "tools/watchdog_night.py", "--now", "--repo", REPO]
                + (["--requirement-file", REQ_FILE] if REQ_FILE else ["--goal-file", GOAL]),
        "ts": time.time(),
        "log": str(LOG),
    }, ensure_ascii=False), encoding="utf-8")
except OSError:
    pass
SPENT = Path(REPO) / ".sixcoder" / "night_spent.json"
BP = Path(REPO) / ".sixcoder" / "blueprint.json"

MAX_RESTARTS = 20

# 引擎收敛判据叫人的退出码（cli.py 的 HUMAN_STOP_RC 同值，自检钉一致）。
# 见它必须**真的停**：振荡收工走的是 res.ok=True → rc=0，旧口径当"跑完再来一遍"，
# 实测 2026-11-16 五轮花 ¥5 全买了回滚、能力数纹丝不动。
HUMAN_STOP_RC = 42

# ── 停机线（2026-11-10 预算机制重设计：唯一停机线移进引擎）────────────────
# 唯一停机线 = 总花费 ≥ 系统总预算 × 1.05，写在产品仓 .sixcoder/budget.json
# （core/budget_estimate.system_budget_stopped，每轮循环自查）。用户不可设任何上限。
# 看门狗不再给夜班传 --max-cny / --hours（这两个 CLI 参数已删除，传了会报未知参数）。
# 下面这个常量只剩运维兜底用途（保持 0 = 不启用即可）：
#   BUDGET_STOP      看门狗自己的花钱兜底线（¥）   0 = 不启用
BUDGET_STOP = 0.0

# ── 定时（2026-11-10 用户定稿：定时开始 + 本次运行时间）───────────────────
# START_AT      定时开始：每天固定时刻，24 小时制 "HH:MM"。空 = 立即开始。
# START_GRACE_MIN  错过开始时刻的宽限（分钟）：在这个窗口内**仍然立刻开始**，
#               不等到第二天（用户双击的时刻通常比计划时刻晚几十秒，
#               不能因为差 1 分钟就让人白等一天）。
# RUN_HOURS     本次运行时间上限（小时）：跑够就停。0 = 不限。
#
# 为什么**不做峰谷迎合**（用户明确要求）：
#   DeepSeek 的闲时半价按**请求发生的时刻自动生效**，调度器不需要、也不该知道峰谷
#   —— 峰谷规则会变、会跨时区，写死在代码里反而是"过时的硬编码"。
#   固定时刻 + 固定时长，行为可预测、可复现，出了问题一眼能查。
#
# 为什么**不会跨天出问题**（用户明确要求）：
#   运行时长一律用**已运行秒数**算（time.time() - 开始时刻），
#   不用"墙钟截止时刻"算。18:00 起跑 12 小时到次日 06:00，判断依然准确；
#   定时开始也只是"今天/明天的那个时刻"，跨天由加一天解决，不涉及绝对时间比较。
START_AT = "18:00"
START_GRACE_MIN = 90
# 最多等多久才值得等（小时）。超过就是"多半是半夜/深夜重启"→ 立刻开始。
# 2026-11-11 01:24 实测事故：半夜重启被判"等 16 小时 35 分"，整晚白等。
# 6 小时的取法：17:00 双击（等 1 小时）仍然等；凌晨/中午双击（等 6+ 小时）立刻跑。
MAX_WAIT_HOURS = 6.0
RUN_HOURS = 12.0


def after_night_action(rc: int, tail_txt: str = "") -> dict:
    """夜班退出后怎么处置（**纯函数**，自检可直接钉调用结果）。

    返回 `continue`=是否续跑、`ollama`=是否拉起本地模型、`log`=要留痕的话。
    2026-11-16 事故：引擎报 `oscillation action=stop` 后 rc=0，看门狗无条件
    10 秒后续跑 → 每轮 ¥1 买一次整批回滚，跑 5 轮零增长。叫人必须真停。
    """
    if int(rc) == HUMAN_STOP_RC:
        return {"continue": False, "ollama": False,
                "log": (f"夜班叫人 rc={HUMAN_STOP_RC}"
                        "（振荡/停滞/预算/远端配置）→ 看门狗停止续跑，等人工处理")}
    # 自愈：本地规划模型掉线会让夜班秒退（rc=3），盲目重启只会烧光重启上限
    # （实测 2026-11-08 14:39 连烧 17 次）→ 先把 ollama serve 拉起来再续跑。
    if "Ollama 不可达" in str(tail_txt or ""):
        return {"continue": True, "ollama": True, "log": ""}
    return {"continue": True, "ollama": False, "log": ""}


def now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def wlog(msg: str) -> None:
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with WLOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def parse_hhmm(text: str) -> tuple[int, int] | None:
    """解析 "HH:MM" → (时, 分)。空/非法 → None（= 不定时，立即开始）。"""
    t = str(text or "").strip()
    if not t:
        return None
    parts = t.split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def seconds_until_start(now_dt=None, start_at: str = START_AT,
                        grace_min: int = START_GRACE_MIN) -> float:
    """距离"定时开始"还有多少秒（0 = 现在就开始）。

    口径（确定性纯函数，可测）：
      · 算出**下一个**开始时刻（今天的那个；若今天已过 → 明天的那个）；
      · 距它**不超过 MAX_WAIT_HOURS(6) 小时** → 等到那个时刻
        （傍晚 17:00 双击 → 18:00 开跑；23:50 双击 → 00:00 开跑，跨天也算得对）；
      · 超过 6 小时 → **立刻开始**（多半是深夜/半夜重启，等下去整晚白等）。

    **为什么加"最多等 6 小时"**（2026-11-11 01:24 实测事故）：
      2026-11-10 19:37 那次是"过了 18:00 还等 22 小时"；
      这次是**半夜 01:24 重启**，"今天 18:00"还在 16.6 小时后 → 又判"等 16 小时 35 分"，
      同样整晚白等。两次事故同一个病根：**把"定时"当成必须遵守的时刻，
      而不是"最晚什么时候开始"**。手动双击的语义就是"现在就跑"，
      定时只负责"傍晚开机的人不用干等"。

    `grace_min` 保留在签名里只为兼容旧调用/自检，逻辑上不再使用。
    """
    hm = parse_hhmm(start_at)
    if hm is None:
        return 0.0
    cur = now_dt or datetime.now()
    target = cur.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
    if cur >= target:
        target = target + timedelta(days=1)      # 今天已过 → 下一个（跨天也算得对）
    wait = (target - cur).total_seconds()
    if wait > MAX_WAIT_HOURS * 3600:
        return 0.0                       # 离得太远（多半是半夜重启）→ 立刻开始
    return wait


def wait_until_start() -> None:
    """等到定时开始时刻（已在宽限窗口内或未定时刻 → 立刻返回）。"""
    _wait = seconds_until_start()
    if _wait <= 0:
        return
    _h = int(_wait // 3600)
    _m = int((_wait % 3600) // 60)
    wlog(f"定时开始 {START_AT}（宽限 {START_GRACE_MIN} 分钟）→ 等 {_h} 小时 {_m} 分")
    _tick = 0
    while _wait > 0:
        time.sleep(min(30.0, _wait))
        _wait -= 30.0
        _tick += 30
        if _tick % 600 == 0:                      # 每 10 分钟报一次，别让人以为死了
            wlog(f"等待中：还差 {int(_wait // 3600)} 小时 {int((_wait % 3600) // 60)} 分")
    wlog(f"到点 {START_AT} → 开始工作")


def spent_cny() -> float:
    try:
        return float(json.loads(SPENT.read_text(encoding="utf-8")).get("total_cny") or 0)
    except Exception:                                    # noqa: BLE001
        return 0.0


def caps_done() -> int:
    try:
        bp = json.loads(BP.read_text(encoding="utf-8"))
        return sum(1 for c in bp.get("caps", []) if c.get("status") == "done")
    except Exception:                                    # noqa: BLE001
        return -1


def existing_night_pid() -> int:
    """已有夜班进程的 PID（0 = 无）。

    为什么必须有（2026-11-08 18:33 实测事故）：会话里手动挂的夜班 + 看门狗自己
    spawn 的夜班**同时写 blueprint.json.tmp** → PermissionError + 蓝图 done 9→3
    （双实例互覆盖）。spawn 前先查系统里有没有 `cli.py night` 在跑；有就转监视，
    绝不并行开第二个实例。
    """
    try:
        # CREATE_NO_WINDOW：看门狗由 pythonw 启动时，这个每 10 秒一次的 PowerShell
        # 查询否则会**每轮弹一个黑窗口**（2026-11-10 用户实测"命令行反复跳出来又关上"）。
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "Where-Object { $_.CommandLine -like '*cli.py night*' } | "
             "Select-Object -First 1).ProcessId"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for ln in reversed((r.stdout or "").strip().splitlines()):
            if ln.strip().isdigit():
                return int(ln.strip())
    except Exception:                                    # noqa: BLE001
        pass
    return 0


def watchdog_pid_file() -> Path:
    try:
        from core import paths as _paths                  # noqa: E402
        return _paths.logs_dir() / "watchdog.pid"
    except Exception:                                    # noqa: BLE001
        return Path(__file__).resolve().parents[2] / "_logs" / "watchdog.pid"


def _pid_alive(pid: int) -> bool:
    """pid 还活着吗（用 psutil 精确判断；拿不到 psutil 就当死了，宁可双开不可锁死）。"""
    try:
        import psutil
        p = psutil.Process(int(pid))
        return p.is_running() and "python" in (p.name() or "").lower()
    except Exception:                                    # noqa: BLE001
        return False


def claim_single_instance() -> bool:
    """**单实例守卫**（2026-11-18）：一天里多次双击 START_NIGHT 会留下多个
    旧看门狗（STOP 只杀 pid 文件里最后一个，CIM 匹配不到无命令行的 pythonw）
    —— 实测一天叠了 6 个；18:00 定时点每个都可能起夜班（叠发事故 11-17 出过）。

    规则：pid 文件里的看门狗**还活着** → 本实例退出（防叠发）；
    死了/没有 → 接管，写下自己的 pid。返回 False = 本实例该退出。
    """
    pf = watchdog_pid_file()
    try:
        old = int(pf.read_text(encoding="utf-8").strip() or 0)
    except Exception:                                    # noqa: BLE001
        old = 0
    if old and old != os.getpid() and _pid_alive(old):
        wlog(f"已有看门狗实例 PID={old} 在跑（防叠发守卫）→ 本实例退出")
        return False
    try:
        pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass
    return True


def main() -> int:
    # **单实例守卫**必须在写 pid 之前：两个看门狗并发启动时，后到的看到
    # 先到的 pid 还活着就退出（写 pid 会互相覆盖，守卫就失效了）。
    if not claim_single_instance():
        return 0
    wlog(f"看门狗启动：重启上限 {MAX_RESTARTS}，"
         f"兜底花钱线 ¥{BUDGET_STOP if BUDGET_STOP else '不启用'}，覆盖目标 76/76"
         f"（唯一停机线在 .sixcoder/budget.json：系统预算 ×1.05）")
    _dur = (f"本次最多跑 {RUN_HOURS} 小时" if RUN_HOURS > 0 else "时长不限")
    wlog(f"排班：定时开始 {START_AT}（宽限 {START_GRACE_MIN} 分），{_dur}"
         "（时长按已运行秒数算，跨天不会算错）")
    if "--now" in sys.argv:
        wlog("命令行给了 --now → 跳过等待，立刻开始")
    else:
        wait_until_start()
    # 本次运行时间一律用**已运行秒数**算，不用墙钟截止时刻 → 跨天不会算错。
    # 边界说明：本时长在**两次夜班之间**检查；单次夜班内部由引擎自己的停机线管。
    run_started = time.time()
    run_limit = RUN_HOURS * 3600 if RUN_HOURS > 0 else float("inf")
    restarts = 0
    while restarts < MAX_RESTARTS:
        _ran = time.time() - run_started
        if _ran >= run_limit:
            wlog(f"本次运行时间到（上限 {RUN_HOURS} 小时，实跑 {_ran / 3600:.2f} 小时）→ 结束")
            break
        if BUDGET_STOP and spent_cny() >= BUDGET_STOP:
            wlog(f"预算到停机线（¥{spent_cny():.2f} ≥ ¥{BUDGET_STOP}）→ 结束")
            break
        done = caps_done()
        if done == 76:
            wlog("覆盖 76/76 → 项目完成 → 结束")
            break
        _alive = existing_night_pid()
        if _alive:
            # 已有夜班在跑（比如会话里手动挂的）：本班转监视，绝不双开。
            wlog(f"已有夜班实例 PID={_alive} → 监视模式（10 秒后复查）")
            time.sleep(10)
            continue
        wlog(f"第 {restarts + 1} 次启动夜班（已花 ¥{spent_cny():.2f}，覆盖 {done}/76）")
        # 2026-11-10：用户双击 START_NIGHT.bat 时看门狗由 pythonw（无控制台）启动，
        # 而 PY 是 python.exe（有控制台）——父进程没控制台时每个子进程都会**新建一个
        # 黑窗口**，夜班退出再拉起 → 命令行窗口反复闪。CREATE_NO_WINDOW 让子进程
        # 复用/不创建控制台；stdout 仍重定向到 LOG 文件，日志一条不丢。
        _no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # Windows only
        with LOG.open("a", encoding="utf-8") as lf:
            _goal_args = (["--requirement-file", REQ_FILE] if REQ_FILE
                          else ["--goal-file", GOAL])
            proc = subprocess.Popen(
                [PY, "-u", "cli.py", "night", "--repo", REPO] + _goal_args,
                cwd=PROTOTYPE, stdout=lf, stderr=subprocess.STDOUT,
                creationflags=_no_window)
            try:
                rc = proc.wait()
            except KeyboardInterrupt:
                proc.kill()
                rc = -9
        restarts += 1
        try:
            tail_txt = LOG.read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            tail_txt = ""
        act = after_night_action(rc, tail_txt)
        if not act["continue"]:
            # 叫人收工：留痕并**真的停**，绝不当 rc=0 的"跑完再来一遍"（空转事故）。
            wlog(act["log"])
            break
        wlog(f"夜班退出 rc={rc}（第 {restarts} 次），10 秒后续跑")
        if act["ollama"]:
            wlog("检测到 Ollama 掉线 → 启动 ollama serve")
            subprocess.Popen(["ollama", "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            time.sleep(8)
        time.sleep(10)
    wlog(f"看门狗结束：重启 {restarts} 次，本次实跑 "
         f"{(time.time() - run_started) / 3600:.2f} 小时，"
         f"最终花费 ¥{spent_cny():.2f}，覆盖 {caps_done()}/76")
    try:
        WANT.unlink()          # 正常收工：不再需要"被中断后续跑"的标记
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
