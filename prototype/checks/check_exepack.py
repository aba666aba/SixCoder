"""check_exepack.py — 造物模式"直接生成 exe"的能力自检（2026-11-22 用户口径）。

钉住：
  1. 入口识别：CLI / ASGI / GUI / 没有入口，四种都要判对（尤其 ASGI 不能漏，
     否则打出来的 exe 双击就退出）；
  2. 启动器：ASGI 生成 uvicorn 启动器，CLI 用 runpy 保持原行为；
  3. 状态机：没有入口 → failed 且给人话；有入口但没 PyInstaller/网络 → failed 不假装成功；
  4. 接口与界面：POST /v1/night/package、GET /v1/night/package/status、
     按钮「打包 exe」、收工自动打包开关；
  5. 打包走独立进程（_spawn_reparented），不占后端线程、关窗不死。

真打包不在这里跑（1-3 分钟）：设 SIXCODER_PACK_E2E=1 才额外跑一次 hello 工程。
"""
import json
import os
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
from core import exepack  # noqa: E402

FAILS: list[str] = []
PASSED = 0


def ck(name: str, cond, extra="") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILS.append(name)
        print(f"  ✗ {name}" + (f"  <- {extra}" if extra else ""))


def repo_with(files: dict[str, str]) -> Path:
    d = Path(tempfile.mkdtemp(prefix="exepack-"))
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return d


print("[1] 入口识别")
cli = repo_with({"main.py": 'print("hi")\n'})
asgi = repo_with({"app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n"})
gui = repo_with({"main.py": "import tkinter\nprint(1)\n"})
none = repo_with({"lib/util.py": "X = 1\n"})
ck("CLI 入口", exepack.detect(cli)["entry"] == "main.py")
ck("ASGI 入口优先 app/main.py", exepack.detect(asgi)["entry"] == "app/main.py")
ck("ASGI 判定", exepack.detect(asgi)["kind"] == "asgi", exepack.detect(asgi))
ck("GUI 判定", exepack.detect(gui)["kind"] == "gui", exepack.detect(gui))
ck("没有入口要如实报", exepack.detect(none)["exists"] is False, exepack.detect(none))

print("[2] 启动器与模块名")
ck("entry_module 去掉 .py", exepack.entry_module("app/main.py") == "app.main")
ck("__main__.py → 包名", exepack.entry_module("app/__main__.py") == "app")
asgi_code = exepack.launcher_code(exepack.detect(asgi))
ck("ASGI 启动器跑 uvicorn", "uvicorn.run(app" in asgi_code and "from app.main import app" in asgi_code,
   asgi_code)
cli_code = exepack.launcher_code(exepack.detect(cli))
ck("CLI 启动器 runpy 保持原行为", "runpy.run_module" in cli_code, cli_code)

print("[3] 失败要说人话，不假装成功")
logs = Path(tempfile.mkdtemp(prefix="packlog-"))
res = exepack.build(none, logs)
ck("没有入口 → failed", res.get("state") == "failed", res)
ck("原因里写清楚", "没有入口" in (res.get("reason") or ""), res.get("reason"))
ck("状态文件落盘", exepack.read_status(logs, none).get("state") == "failed",
   exepack.read_status(logs, none))

print("[4] 接口 / 界面")
srv = (PROTO / "server.py").read_text(encoding="utf-8")
ck("有 POST /v1/night/package", '@app.post("/v1/night/package")' in srv)
ck("有 GET /v1/night/package/status", '@app.get("/v1/night/package/status")' in srv)
ck("打包走脱离进程", '"-m", "core.exepack"' in srv and "_spawn_reparented" in srv)
ck("已在打包时返回 already", '"already": True' in srv)
app = (PROTO / "web" / "src" / "App.vue").read_text(encoding="utf-8")
ck("界面有「打包 exe」按钮", "打包 exe" in app and "startPack()" in app)
ck("有收工自动打包开关", "收工后自动打包" in app and "maybeAutoPack" in app)
ck("按需判断（exe/程序/安装包）", "PACK_HINT" in app and "exe|可执行" in app)
ck("结果能打开所在目录", "openPackDir" in app and "showInFolder" in app)

print("[5] 依赖随包")
req = (PROTO / "requirements.txt").read_text(encoding="utf-8")
ck("PyInstaller 进运行时依赖（离线也能打）", "pyinstaller" in req.lower(), req[-120:])

if os.environ.get("SIXCODER_PACK_E2E") == "1":
    print("[6] 真打一个（SIXCODER_PACK_E2E=1）")
    demo = repo_with({"main.py": 'print("e2e ok")\n'})
    r = exepack.build(demo, logs)
    ck("真打包成功", r.get("state") == "done", r)
    ck("exe 存在且有体积", Path(r.get("exe") or "").is_file() and (r.get("size") or 0) > 0, r)
else:
    print("[6] 真打包已跳过（设 SIXCODER_PACK_E2E=1 可跑）")

print()
if FAILS:
    print("失败：", FAILS)
    sys.exit(1)
print(f"全部通过（{PASSED} 条）：入口识别 / 启动器 / 失败人话 / 接口界面 / 依赖随包")
