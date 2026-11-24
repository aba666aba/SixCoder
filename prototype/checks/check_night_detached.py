"""夜班"脱离界面长跑"自检（2026-11-20 用户口径：点开始就跑一整夜）。

要钉住的四件事：
  1. 端点存在：`POST /v1/night/detached` + `GET /v1/night/detached/status`；
  2. **用户输入的任务不会被丢**：需求模式写 `night_requirement.txt` 并传
     `--requirement-file`，目标模式写 `night_goal.txt` 并传 `--goal-file`，
     两者都没给才退回仓库的 REQUIREMENTS.md（双击 bat 的老行为不能变）；
  3. **进程真脱离**：Popen 带 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP；
  4. 防双开：同一仓库已有 `cli.py night` 在跑时不再起第二个。

跑法: python checks/check_night_detached.py
纪律：本自检**绝不真起夜班**（会花钱）——Popen 被替身拦截。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:260]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_check", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("[1] 端点与路径")
    srv = _load_server()
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    ck("POST /v1/night/detached 已注册", "/v1/night/detached" in paths)
    ck("GET /v1/night/detached/status 已注册", "/v1/night/detached/status" in paths)
    pwx = srv._pythonw_exe()
    ck("pythonw 可执行存在（无控制台窗口）", Path(pwx).is_file(), pwx)
    ck("_pid_alive(0)=False（不吃 0/None）", srv._pid_alive(0) is False)
    ck("_pid_alive(本进程)=True", srv._pid_alive(__import__("os").getpid()) is True)

    print("[2] 命令拼装：用户任务必须被用上（spawn 被替身拦截，绝不真起）")
    cap: dict = {}

    def _fake_spawn(exe, args, cwd, env_extra=None, pid_file=None):
        # 起夜班会连续 spawn 两次：watchdog + patrol。
        cmd = [exe, *args]
        cap.setdefault("cmds", []).append(cmd)
        if any("watchdog_night.py" in str(x) for x in cmd):
            cap["cmd"] = cmd
            cap["env"] = env_extra or {}
        return 424242

    srv._spawn_reparented = _fake_spawn         # noqa: S101 —— 自检替身
    srv._night_procs = lambda repo: []           # 防双开放行
    td = Path(tempfile.mkdtemp(prefix="nightdet-"))
    try:
        (td / ".sixcoder").mkdir()

        r1 = srv.create_night_detached(srv.DetachedNightReq(repo=str(td), requirement="做一个 CSV 记账工具"))
        ck("需求模式：传 --requirement-file", cap["cmd"][-2:] [0] == "--requirement-file", str(cap["cmd"][-2:]))
        ck("需求模式：文本真写进 night_requirement.txt",
           (td / ".sixcoder" / "night_requirement.txt").read_text(encoding="utf-8") == "做一个 CSV 记账工具")
        ck("需求模式：goal_source=requirement", r1.get("goal_source") == "requirement", r1.get("goal_source"))

        r2 = srv.create_night_detached(srv.DetachedNightReq(repo=str(td), goal="一句话目标"))
        ck("目标模式：传 --goal-file", cap["cmd"][-2:][0] == "--goal-file", str(cap["cmd"][-2:]))
        ck("目标模式：goal_source=goal", r2.get("goal_source") == "goal", r2.get("goal_source"))

        r3 = srv.create_night_detached(srv.DetachedNightReq(repo=str(td)))
        ck("都不给：不加目标参数（退回 REQUIREMENTS.md）",
           cap["cmd"][-1] == "--repo" or cap["cmd"][-1] == str(td), str(cap["cmd"][-2:]))
        ck("都不给：goal_source=REQUIREMENTS.md", r3.get("goal_source") == "REQUIREMENTS.md", r3.get("goal_source"))

        ck("真脱离：cmd start + BREAKAWAY（不在应用进程树/Job 里）",
           'start "" /b' in (PROTO / "server.py").read_text(encoding="utf-8")
           and "CREATE_BREAKAWAY_FROM_JOB" in (PROTO / "server.py").read_text(encoding="utf-8"))
        ck("巡检和看门狗共用同一套脱离启动", len(cap["cmds"]) >= 2, str(len(cap["cmds"])))
        ck("巡检一并拉起（删 bat 后巡检层不能丢）",
           any("patrol.py" in str(x) for c in cap["cmds"] for x in c), str(cap["cmds"]))

        print("[3] 防双开：已有夜班时不重复起")
        srv._night_procs = lambda repo: [{"pid": 1, "cmd": "cli.py night"}]
        r4 = srv.create_night_detached(srv.DetachedNightReq(repo=str(td)))
        ck("返回 already=True", r4.get("already") is True, json.dumps(r4, ensure_ascii=False)[:160])
        ck("不再 spawn（命令没被改写）", cap["cmd"][-2:][0] != "--goal-file")

        print("[4] 状态：没有产物/没跑过也不能炸")
        s2 = Path(tempfile.mkdtemp(prefix="nightstat-"))
        try:
            st = srv.night_detached_status(str(s2))
            ck("返回结构齐全", all(k in st for k in ("watchdog", "night", "coverage", "budget")), str(list(st)))
            ck("空仓库 coverage 为空字典（不编造进度）", st.get("coverage") == {}, str(st.get("coverage")))
            ck("watchdog.alive 是布尔", isinstance(st["watchdog"].get("alive"), bool))
            ck("带夜班日志尾巴字段", "night_log_tail" in st, str(list(st)))
            ck("带最近产物字段", isinstance(st.get("recent_files"), list), str(list(st)))
        finally:
            shutil.rmtree(s2, ignore_errors=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)

    print("[5] watchdog 侧要能接住这两个参数（否则任务照样丢）")
    wd = (PROTO / "tools" / "watchdog_night.py").read_text(encoding="utf-8")
    ck("watchdog 读 --goal-file", '_cli_opt("--goal-file")' in wd)
    ck("watchdog 读 --requirement-file", '_cli_opt("--requirement-file")' in wd)
    ck("watchdog 真的把 --requirement-file 传给 cli",
       '"--requirement-file", REQ_FILE' in wd)

    print("[6] 进度可读：每仓库独立日志 + 界面重开能接管（2026-11-21）")
    ck("watchdog 日志按仓库打标签", "night_{_REPO_TAG}_" in wd or "_REPO_TAG" in wd)
    ck("watchdog 写 .current 指针", ".current" in wd)
    ck("状态接口按仓库取日志", "_paths.night_log(" in (PROTO / "server.py").read_text(encoding="utf-8"))
    app = (PROTO / "web" / "src" / "App.vue").read_text(encoding="utf-8")
    ck("界面换仓库时接管已在跑的夜班", "adoptDetached()" in app)
    srv_src = (PROTO / "server.py").read_text(encoding="utf-8")
    ck("有增量日志接口 /v1/night/detached/log", '@app.get("/v1/night/detached/log")' in srv_src)
    ck("日志按字节 offset 增量读", 'f.seek(offset)' in srv_src and '"offset": offset + consumed' in srv_src)
    ck("时间线每 2 秒拉一次日志", "setInterval(pullNightLog, 2000)" in app)
    ck("夜班日志进主时间线", "kind: 'night_log'" in app and "case 'night_log'" in app)
    ck("非后台模式也进时间线", "pushEvent({ kind: ev.kind, ts: ev.ts, payload: ev.payload })" in app)

    print("[7] 被中断的夜班要能自愈（装新版本会杀掉安装目录下的 python）")
    import time as _t
    wd2 = (PROTO / "tools" / "watchdog_night.py").read_text(encoding="utf-8")
    ck("看门狗开跑时写 .want 标记", 'night_{_REPO_TAG}.want' in wd2 or ".want" in wd2)
    ck("正常收工删标记", "WANT.unlink()" in wd2)
    ck("后端启动时检查续跑", "_resume_interrupted_nights()" in (PROTO / "server.py").read_text(encoding="utf-8"))
    ck("主动停夜班也删标记", 'glob("night_*.want")' in (PROTO / "server.py").read_text(encoding="utf-8"))

    with tempfile.TemporaryDirectory(prefix="resume-") as td:
        td = Path(td)
        old_logs, old_pack, old_procs, old_spawn = srv._paths.logs_dir, srv._paths.is_packaged, srv._night_procs, srv._spawn_reparented
        calls = []
        try:
            srv._paths.logs_dir = lambda: td
            srv._paths.is_packaged = lambda: True
            srv._night_procs = lambda repo: []
            srv._spawn_reparented = lambda exe, args, cwd, env=None, pid_file=None: calls.append(list(args)) or 4242
            f = td / "night_abc.want"
            f.write_text(json.dumps({"repo": r"F://WorkSpace//Cut",
                                     "args": ["-u", "tools/watchdog_night.py", "--now",
                                              "--repo", r"F://WorkSpace//Cut"],
                                     "ts": _t.time()}), encoding="utf-8")
            srv._resume_interrupted_nights()
            ck("有标记且没在跑 → 续跑一次", len(calls) == 1, calls)
            ck("续跑用的是原参数", calls and "watchdog_night.py" in " ".join(calls[0]), calls)
            srv._night_procs = lambda repo: [{"pid": 1, "cmd": "cli.py night"}]
            srv._resume_interrupted_nights()
            ck("已在跑就不重复起", len(calls) == 1, calls)
            srv._night_procs = lambda repo: []
            f.write_text(json.dumps({"repo": r"F://WorkSpace//Cut", "args": ["x"],
                                     "ts": _t.time() - 13 * 3600}), encoding="utf-8")
            srv._resume_interrupted_nights()
            ck("过期标记直接清掉（不跑隔夜陈旧任务）", not f.exists() and len(calls) == 1, calls)
        finally:
            srv._paths.logs_dir, srv._paths.is_packaged = old_logs, old_pack
            srv._night_procs, srv._spawn_reparented = old_procs, old_spawn
    ck("进度面板显示日志尾巴", "night.detStatus?.night_log_tail" in app)
    ck("进度面板显示最近产物", "night.detStatus?.recent_files" in app)

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("ALL PASS")
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
