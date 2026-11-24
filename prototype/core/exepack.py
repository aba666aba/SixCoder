"""把产物仓打成一个能双击运行的 exe —— 造物模式交付的最后一步。

为什么单独一层（2026-11-22 用户口径："造物模式能不能根据需要直接生成 exe"）：
夜班只保证"代码过机械判据"，用户拿到的是源码目录；要变成"双击就能跑"的交付物，
还差打包：找入口 → 生成启动器（ASGI/GUI/CLI 三种）→ PyInstaller onefile →
把产物路径/体积记进状态文件，界面轮询显示。

设计约束：
- **不猜**：入口必须能在仓库里找到（app/main.py、main.py、cli.py…），找不到就明确报"没有入口"；
- **不污染仓库**：中间物落 `build/_pyi`，成品落 `dist/`；
- **可观测**：每一步写日志 + 状态 JSON，打包要 1-3 分钟，界面不能干等；
- **公开接口**：`detect()` / `plan()` / `build()` 都能被自检直接调用（不跑真打包）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

ENTRY_CANDIDATES = (
    "app/main.py", "app/__main__.py", "main.py", "src/main.py",
    "cli.py", "app/cli.py", "manage.py",
)

ASGI_HINTS = ("async def app(", "FastAPI(", "Starlette(", "ASGIApp", "application = ")
GUI_HINTS = ("tkinter", "PyQt5", "PyQt6", "PySide2", "PySide6", "wx.")


# ---------------------------------------------------------------- 检测

def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def detect(repo: Path) -> dict:
    """找入口并判断类型：asgi / gui / cli / none。找不到就明确说没有。"""
    repo = Path(repo)
    for rel in ENTRY_CANDIDATES:
        p = repo / rel
        if not p.is_file():
            continue
        text = _read(p)
        if any(h in text for h in ASGI_HINTS):
            kind = "asgi"
        elif any(h in text for h in GUI_HINTS):
            kind = "gui"
        else:
            kind = "cli"
        return {"entry": rel, "kind": kind, "name": _name(repo), "exists": True}
    # 兜底：任何带 __main__ 守卫的 py（排除测试/构建目录）
    for p in sorted(repo.rglob("*.py")):
        rel = p.relative_to(repo).as_posix()
        if any(x in rel for x in ("test", "build/", "dist/", ".sixcoder/", "node_modules")):
            continue
        if '__name__ == "__main__"' in _read(p):
            return {"entry": rel, "kind": "cli", "name": _name(repo), "exists": True}
    return {"entry": "", "kind": "none", "name": _name(repo), "exists": False}


def _name(repo: Path) -> str:
    raw = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5-]+", "_", repo.name).strip("_")
    return raw or "app"


def entry_module(entry: str) -> str:
    """`app/main.py` → `app.main`（PyInstaller 的 hidden-import 与启动器都要）。"""
    p = Path(entry)
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__main__":
        parts = parts[:-1] or ["__main__"]
    return ".".join(parts)


def launcher_code(info: dict) -> str:
    """生成启动器。

    ASGI 项目没有 `if __name__ == "__main__"`，直接 onefile 起来会立刻退出，
    所以这里生成一个真跑 uvicorn 的入口；CLI/GUI 用 run_module 保持原行为。
    """
    mod = entry_module(info["entry"])
    if info["kind"] == "asgi":
        return (
            '"""自动生成的启动器：起 ASGI 服务（打包用，不参与业务）。"""\n'
            "import uvicorn\n\n"
            f"from {mod} import app\n\n"
            'if __name__ == "__main__":\n'
            '    uvicorn.run(app, host="127.0.0.1", port=8000)\n'
        )
    return (
        '"""自动生成的启动器：按原入口跑（打包用，不参与业务）。"""\n'
        "import runpy\n\n"
        'if __name__ == "__main__":\n'
        f'    runpy.run_module("{mod}", run_name="__main__", alter_sys=True)\n'
    )


# ---------------------------------------------------------------- 解释器 / 依赖

def pick_python(repo: Path) -> str:
    """优先项目自己的 .venv（依赖齐），否则用当前解释器。"""
    for rel in (".venv/Scripts/python.exe", "venv/Scripts/python.exe",
                ".venv/bin/python", "venv/bin/python"):
        p = Path(repo) / rel
        if p.is_file():
            return str(p)
    return sys.executable


def _run(cmd: list[str], cwd: Path, log) -> int:
    log(f"$ {' '.join(cmd)}")
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=1800)
    except Exception as exc:                                # noqa: BLE001
        log(f"!! {type(exc).__name__}: {exc}")
        return -1
    tail = ((p.stdout or "") + (p.stderr or "")).strip().splitlines()[-8:]
    for line in tail:
        log(f"   {line}")
    return p.returncode


def ensure_pyinstaller(py: str, cwd: Path, log) -> bool:
    if _run([py, "-m", "PyInstaller", "--version"], cwd, log) == 0:
        return True
    log("!! 当前解释器没有 PyInstaller，尝试安装（需要网络）")
    if _run([py, "-m", "pip", "install", "--disable-pip-version-check", "-q",
             "pyinstaller>=6,<7"], cwd, log) != 0:
        return False
    return _run([py, "-m", "PyInstaller", "--version"], cwd, log) == 0


# ---------------------------------------------------------------- 打包

def status_file(logs_dir: Path, repo: Path) -> Path:
    from . import paths as _paths
    return Path(logs_dir) / f"pack_{_paths.night_tag(str(repo))}.json"


def read_status(logs_dir: Path, repo: Path) -> dict:
    f = status_file(logs_dir, repo)
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "idle"}


def build(repo: Path, logs_dir: Path, *, name: str = "") -> dict:
    """同步打包（由独立进程调用，界面轮询状态文件）。返回最终状态。"""
    repo = Path(repo)
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"pack_{repo.name}.log"
    state = status_file(logs_dir, repo)
    started = time.time()

    def write(**kw) -> None:
        data = {"state": "running", "repo": str(repo), "started": started,
                "log": str(log_path), **kw}
        try:
            state.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    lines: list[str] = []

    def log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        lines.append(line)
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    info = detect(repo)
    write(state="running", step="detect", **{"entry": info["entry"], "kind": info["kind"]})
    if not info["exists"]:
        log("!! 没有找到入口（app/main.py / main.py / cli.py / 带 __main__ 的脚本）")
        res = {"state": "failed", "reason": "没有入口文件", "repo": str(repo),
               "started": started, "ended": time.time(), "log": str(log_path)}
        write(**res)
        return res

    log(f"入口 {info['entry']}（{info['kind']}）")
    py = pick_python(repo)
    log(f"解释器 {py}")
    if not ensure_pyinstaller(py, repo, log):
        res = {"state": "failed", "reason": "没有 PyInstaller 且安装失败（检查网络/权限）",
               "repo": str(repo), "started": started, "ended": time.time(), "log": str(log_path)}
        write(**res)
        return res

    launcher = repo / ".sixcoder" / "_exe_entry.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(launcher_code(info), encoding="utf-8")
    exe_name = name or info["name"]
    dist = repo / "dist"
    work = repo / "build" / "_pyi"
    cmd = [py, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
           "--name", exe_name, "--distpath", str(dist), "--workpath", str(work),
           "--specpath", str(repo / "build"), "--paths", str(repo),
           "--hidden-import", entry_module(info["entry"]), str(launcher)]
    if info["kind"] == "gui":
        cmd.insert(3, "--noconsole")
    write(state="running", step="build", entry=info["entry"], kind=info["kind"],
          cmd=" ".join(cmd))
    log("开始 PyInstaller（1-3 分钟）")
    rc = _run(cmd, repo, log)
    exe = dist / (exe_name + (".exe" if sys.platform == "win32" else ""))
    if rc != 0 or not exe.is_file():
        res = {"state": "failed", "reason": f"PyInstaller 退出码 {rc}",
               "repo": str(repo), "started": started, "ended": time.time(),
               "log": str(log_path), "entry": info["entry"], "kind": info["kind"]}
        log(f"!! 打包失败 rc={rc}")
        write(**res)
        return res
    size = exe.stat().st_size
    log(f"完成：{exe}（{size / 1048576:.1f} MB）")
    res = {"state": "done", "exe": str(exe), "size": size, "repo": str(repo),
           "entry": info["entry"], "kind": info["kind"], "started": started,
           "ended": time.time(), "log": str(log_path)}
    write(**res)
    return res


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    def opt(name: str) -> str:
        return argv[argv.index(name) + 1] if name in argv else ""

    repo = Path(opt("--repo")).resolve()
    logs = Path(opt("--logs")) if opt("--logs") else None
    if logs is None:
        from . import paths as _paths
        logs = _paths.logs_dir()
    if not repo.is_dir():
        print(f"仓库不存在：{repo}")
        return 2
    res = build(repo, logs, name=opt("--name"))
    print(json.dumps(res, ensure_ascii=False))
    return 0 if res.get("state") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
