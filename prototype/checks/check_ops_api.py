"""夜班启停 + 运维任务 API 自检（替代 START_NIGHT.bat / STOP_NIGHT.bat 后的守门人）。

钉住五条：
  1. 三个新接口**真的注册在 app 上**（不是"函数定义了但没挂路由"——踩过）；
  2. 进程识别只认本引擎脚本，**认不出的一律不动**（停夜班绝不能误杀用户别的 python）；
  3. 运维任务只接受已知 op，未知 op 在 spawn 之前就 400（不能先起进程再报错）；
  4. 已有任务在跑时不重复启动（两个 `full --repair` 同写一个产物仓 = 灾难）；
  5. 运行期数据目录能被 `SIXCODER_HOME` 重定向（安装版靠它做"卸载可选保留"）。

跑法: python checks/check_ops_api.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:300]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_ops", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()

    print("[1] 路由真的挂上了")
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    for p in ("/v1/night/stop", "/v1/ops/start", "/v1/ops/status"):
        ck(f"注册 {p}", p in paths, sorted(paths)[:6])

    print("[2] 进程识别：只认本引擎，认不出不动")
    k = srv._engine_proc_kind
    ck("watchdog_night.py → watchdog", k(r"D:\py\pythonw.exe -u tools/watchdog_night.py --now") == "watchdog")
    ck("patrol.py → patrol", k(r"python tools/patrol.py --persist") == "patrol")
    ck("cli.py night → night", k(r"python -u cli.py night --repo F:\ws\x") == "night")
    ck("cli.py full 不算夜班", k(r"python cli.py full --repo x") == "")
    ck("用户自己的脚本不动", k(r"python my_script.py --night") == "")
    ck("jupyter 之类不动", k(r"python -m jupyter notebook") == "")

    print("[3] 运维任务：未知 op / 坏仓库在起进程之前就拒")
    try:
        srv.ops_start(srv.OpsReq(repo=str(PROTO), op="rm-rf-everything"))
        ck("未知 op 被拒", False, "居然没抛异常")
    except Exception as exc:                            # noqa: BLE001
        code = getattr(exc, "status_code", 0)
        ck("未知 op 被拒（400）", code == 400, f"{type(exc).__name__} {code} {exc}")
    try:
        srv.ops_start(srv.OpsReq(repo=str(PROTO / "__nope__"), op="report"))
        ck("坏仓库被拒", False, "居然没抛异常")
    except Exception as exc:                            # noqa: BLE001
        code = getattr(exc, "status_code", 0)
        ck("坏仓库被拒（400）", code == 400, f"{type(exc).__name__} {code} {exc}")

    print("[4] 已有任务在跑 → 不重复启动")
    from core import paths as _paths
    state = _paths.logs_dir() / "ops.json"
    backup = state.read_text(encoding="utf-8") if state.is_file() else None
    try:
        state.write_text(json.dumps({"op": "report", "repo": str(PROTO), "pid": os.getpid(),
                                     "log": str(_paths.logs_dir() / "ops_report.out"),
                                     "started": 0, "desc": "x"}), encoding="utf-8")
        got = srv.ops_start(srv.OpsReq(repo=str(PROTO), op="report"))
        ck("返回 already 而不是再起一个", bool(got.get("already")), got)
        ck("返回的是在跑的那个 op", got.get("op") == "report", got.get("op"))
    finally:
        if backup is None:
            try:
                state.unlink()
            except OSError:
                pass
        else:
            state.write_text(backup, encoding="utf-8")

    print("[5] 状态：没有任务时如实说没在跑")
    st = srv.ops_status()
    ck("running=False", st.get("running") is False, st)

    print("[6] 运行期数据目录可重定向（安装版靠它）")
    tmp = Path(tempfile.mkdtemp(prefix="schome-"))
    old = os.environ.get("SIXCODER_HOME")
    try:
        os.environ["SIXCODER_HOME"] = str(tmp)
        import importlib
        from core import paths as p2
        importlib.reload(p2)
        ck("home 跟随环境变量", p2.home() == tmp, p2.home())
        ck("logs 落在 home 下", str(p2.logs_dir()).startswith(str(tmp)), p2.logs_dir())
        ck("data 落在 home 下", str(p2.data_dir()).startswith(str(tmp)), p2.data_dir())
        ck("打包态判定为真", p2.is_packaged() is True)
    finally:
        if old is None:
            os.environ.pop("SIXCODER_HOME", None)
        else:
            os.environ["SIXCODER_HOME"] = old
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
