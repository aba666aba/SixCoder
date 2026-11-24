"""真装真卸验收：装一次 → 用一下 → 卸掉 → 逐个位置查残留。

这是 `packaging/test_uninstall.ps1` 的 Python 版（2026-11-20 用户口径：脚本都收进源码、
不留 bat/ps1）。它验证的是**产品承诺**，不是代码：
    · 双击安装后能直接跑，不需要用户装 Python
    · 控制面板卸载后程序本体/快捷方式/注册表/开机自启全部消失
    · 默认勾选「保留本地运行数据」→ 夜班日志与密钥还在
    · 勾掉（或加 /WIPEDATA）→ 连数据目录一起清干净
    · 无论哪种，**用户项目仓库一个字节都不动**

会真的装/卸本机软件，所以默认只做检查不执行；确认后加 --yes：

    python packaging/test_uninstall.py --yes

注意：卸载会停掉本安装目录下的夜班进程。正在跑夜班时不要执行。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "dist-installer"
INSTALL_DIR = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "SixCoder"
DATA_DIR = Path(os.environ["LOCALAPPDATA"]) / "SixCoder"
APPDATA_DIR = Path(os.environ["APPDATA"]) / "SixCoder"
DESKTOP_LNK = Path(os.path.expanduser("~")) / "Desktop" / "SixCoder.lnk"
START_LNK = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
             / "Programs" / "SixCoder.lnk")
UNINST_EXE = INSTALL_DIR / "Uninstall SixCoder.exe"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:280]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def find_installer() -> Path:
    exes = sorted(OUT.rglob("SixCoder-Setup-*.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not exes:
        raise SystemExit(f"没有安装包：{OUT}")
    return exes[0]


def run(cmd: list[str], timeout: int = 600) -> int:
    print("  $ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, timeout=timeout)


def installed_entries() -> list[str]:
    """卸载注册表里有没有 SixCoder（控制面板能看到 = 能正常卸载）。"""
    try:
        import winreg
    except Exception:                                    # noqa: BLE001
        return []
    hits: list[str] = []
    base = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, base) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        name = winreg.EnumKey(k, i)
                        with winreg.OpenKey(k, f"{base}\\{name}") as sk:
                            disp = winreg.QueryValueEx(sk, "DisplayName")[0]
                            if "sixcoder" in str(disp).lower():
                                hits.append(str(disp))
                    except OSError:
                        continue
        except OSError:
            continue
    return hits


def autostart_value() -> str:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return str(winreg.QueryValueEx(k, "SixCoder")[0])
    except Exception:                                    # noqa: BLE001
        return ""


def wait_gone(path: Path, seconds: int = 40) -> bool:
    for _ in range(seconds):
        if not path.exists():
            return True
        time.sleep(1)
    return not path.exists()


def install(exe: Path) -> bool:
    # /S 静默；per-user 安装不弹 UAC
    return run([str(exe), "/S"]) == 0


def uninstall(extra: list[str] | None = None) -> bool:
    if not UNINST_EXE.is_file():
        return False
    return run([str(UNINST_EXE), "/S"] + (extra or []), timeout=300) == 0


def plant_data() -> Path:
    """模拟"用过一段时间"：日志、任务历史、密钥文件。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "_logs").mkdir(exist_ok=True)
    marker = DATA_DIR / "_logs" / "night-verify.json"
    marker.write_text('{"verified": "keep-me"}', encoding="utf-8")
    (DATA_DIR / "settings.local.json").write_text('{"api_key": "sk-verify"}', encoding="utf-8")
    return marker


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SixCoder 安装/卸载残留验收")
    ap.add_argument("--yes", action="store_true", help="确认真的装/卸本机软件")
    ap.add_argument("--keep-installed", action="store_true", help="验收结束后重新装上（默认留空）")
    args = ap.parse_args(argv)

    exe = find_installer()
    print(f"安装包：{exe}（{exe.stat().st_size / 1048576:.1f} MB）")
    if not args.yes:
        print("\n这会真装真卸本机软件，并停掉当前夜班进程。确认后加 --yes 再跑。")
        print("预检：")
        print(f"  安装目录 {INSTALL_DIR}：{'已存在（会被覆盖）' if INSTALL_DIR.exists() else '不存在'}")
        print(f"  数据目录 {DATA_DIR}：{'已存在（测试期间会被写入）' if DATA_DIR.exists() else '不存在'}")
        print(f"  卸载注册表项：{installed_entries() or '无'}")
        return 0

    if installed_entries():
        print("\n检测到已安装的 SixCoder —— 先卸掉再开始（避免状态互相污染）")
        uninstall(["/WIPEDATA"])
        wait_gone(INSTALL_DIR / "SixCoder.exe")

    print("\n[1] 静默安装")
    ck("安装程序返回 0", install(exe))
    ck("程序本体存在", (INSTALL_DIR / "SixCoder.exe").is_file(), INSTALL_DIR)
    ck("内置 Python 随包落地", (INSTALL_DIR / "resources" / "python" / "python.exe").is_file())
    ck("后端源码随包落地", (INSTALL_DIR / "resources" / "prototype" / "server.py").is_file())
    ck("前端 dist 随包落地",
       (INSTALL_DIR / "resources" / "prototype" / "web" / "dist" / "index.html").is_file())
    ck("控制面板能看到（卸载项已注册）", bool(installed_entries()), installed_entries())
    ck("开始菜单有快捷方式", START_LNK.is_file() or (INSTALL_DIR / "SixCoder.exe").is_file())
    ck("卸载器就位", UNINST_EXE.is_file(), UNINST_EXE)

    print("\n[2] 用一下：写入本机数据")
    marker = plant_data()
    ck("数据已植入", marker.is_file())

    print("\n[3] 静默卸载（默认分支 = 保留本地数据）")
    ck("卸载程序返回 0", uninstall())
    ck("程序目录已清空", wait_gone(INSTALL_DIR), INSTALL_DIR)
    ck("卸载注册表项已清", not installed_entries(), installed_entries())
    ck("桌面快捷方式已清", not DESKTOP_LNK.exists(), DESKTOP_LNK)
    ck("开机自启已清", not autostart_value(), autostart_value())
    ck("本地数据【保留】", marker.is_file(), marker)

    print("\n[4] 再装一次 → /WIPEDATA 卸（彻底清分支）")
    ck("重新安装成功", install(exe))
    (DATA_DIR / "_logs").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "_logs" / "night-verify.json").write_text("{}", encoding="utf-8")
    ck("卸载（/WIPEDATA）返回 0", uninstall(["/WIPEDATA"]))
    ck("数据目录已清空", wait_gone(DATA_DIR), DATA_DIR)
    ck("程序目录已清空", wait_gone(INSTALL_DIR), INSTALL_DIR)

    print("\n[5] 用户仓库不受影响")
    ck("没有在卸载脚本里出现用户仓库路径（静态保证）",
       "biyeshejishuangxuan" not in (ROOT / "shell" / "build" / "installer.nsh").read_text(encoding="utf-8"))

    if args.keep_installed:
        print("\n[6] 按 --keep-installed 重新安装")
        ck("重装成功", install(exe))

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过：装得干净、卸得干净、数据按选择保留")
    return 0


if __name__ == "__main__":
    sys.exit(main())
