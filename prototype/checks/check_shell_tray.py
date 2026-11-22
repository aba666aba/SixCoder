"""check_shell_tray.py — 关闭收进托盘、托盘里才退出（2026-11-21 用户口径）。

用户原话："能不能加上关闭自动到托盘，托盘退出"。

钉住六件事（静态，秒级）：
  1. 外壳真的引入了 Tray / nativeImage；
  2. 窗口 close 被拦下 → hide（不是 quit），且 before-quit 时才放行；
  3. 托盘菜单有「显示主窗口」「退出 SixCoder」，左键点图标能回来；
  4. window-all-closed 不再无条件 quit（否则收进托盘等于退出）；
  5. 托盘图标打进安装包（asar 里的 build/icon.ico），不会出现空白图标；
  6. 启动时就创建托盘，用户不必先隐藏一次才发现。

跑法: python checks/check_shell_tray.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN = (ROOT / "shell" / "main.cjs").read_text(encoding="utf-8")
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


print("[1] 引入托盘能力")
ck("require 里有 Tray", "Tray" in MAIN.split("= require('electron')")[0])
ck("用了 nativeImage", "nativeImage" in MAIN)
ck("有 trayIconPath() 兜底找图标", "function trayIconPath" in MAIN)

print("[2] 关闭 = 收进托盘")
close_block = MAIN.split("win.on('close'", 1)[-1][:260] if "win.on('close'" in MAIN else ""
ck("窗口 close 被拦截", "e.preventDefault()" in close_block, close_block[:80])
ck("拦截后 hideToTray", "hideToTray(" in close_block)
ck("真退出时才放行", "if (isQuitting) return" in close_block)
ck("hideToTray 里是 hide 不是 close", "win.hide()" in MAIN.split("function hideToTray", 1)[-1][:200])
ck("before-quit 置 isQuitting", "isQuitting = true" in MAIN.split("app.on('before-quit'", 1)[-1][:200])

print("[3] 托盘菜单与交互")
tray_block = MAIN.split("function createTray", 1)[-1][:1200]
ck("菜单有「显示主窗口」", "显示主窗口" in tray_block)
ck("菜单有「退出 SixCoder」", "退出 SixCoder" in tray_block)
ck("退出走 isQuitting + app.quit", "isQuitting = true; app.quit()" in tray_block)
ck("左键点图标回到窗口", "tray.on('click', showWindow)" in tray_block)
ck("支持双击", "double-click', showWindow" in tray_block)
ck("第一次收托盘给一次提示", "displayBalloon" in MAIN)

print("[4] 收进托盘不等于退出")
wac = MAIN.split("app.on('window-all-closed'", 1)[-1][:160]
ck("window-all-closed 有条件", "if (isQuitting) app.quit()" in wac, wac[:100])
ck("不再无条件 app.quit()", "=> app.quit()" not in wac.replace("if (isQuitting) app.quit()", ""))

print("[5] 图标随包分发")
cfg = json.loads((ROOT / "packaging" / "electron-builder.json").read_text(encoding="utf-8"))
ck("files 含 build/icon.ico", "build/icon.ico" in cfg.get("files", []), cfg.get("files"))
ck("图标文件存在", (ROOT / "shell" / "build" / "icon.ico").is_file())

print("[6] 启动即常驻")
ck("whenReady 里 createTray()", "createTray()" in MAIN.split("await createWindow()", 1)[-1][:200])
ck("二次启动复用同一个窗口", "showWindow()" in MAIN.split("app.on('second-instance'", 1)[-1][:120])

print()
if FAILS:
    print("失败：", FAILS)
    sys.exit(1)
print(f"全部通过（{PASSED} 条）：托盘常驻 / 关闭隐藏 / 托盘退出 / 图标随包")
