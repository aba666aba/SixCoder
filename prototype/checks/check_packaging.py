"""打包产物自检：一个 EXE + 干净卸载（2026-11-20 用户口径）。

钉住六条：
  1. 只有一个用户入口（EXE）—— 历史上的 bat/ps1 已全部删除，不留第二套启动方式；
  2. 安装包自带运行时 —— 内置便携 Python + 依赖真的能 import（用户不用装 Python）；
  3. 可选组件默认不勾 —— 工具链（Ollama/模型）交给用户自己调，不强制装；
  4. 卸载有勾选框且**默认勾选保留** —— 数据比磁盘值钱，误删一次就没人再信任它；
  5. 卸载不碰用户仓库 —— 只清程序本体与本机数据，绝不 RMDir 用户项目目录；
  6. 静默卸载也要能全清 —— 自动化测试靠 /WIPEDATA，否则"装完卸掉"永远测不干净；
  7. 自选安装目录 + 端口自适应 + 真健康检查 —— 装完双击必须能起前后端，
     端口被占自动 +1，且不能把占端口的别人程序误认成后端（界面全蓝无按钮）。

跑法: python checks/check_packaging.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # 仓库根
PROTO = ROOT / "prototype"
SHELL = ROOT / "shell"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:300]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    cfg = json.loads((ROOT / "packaging" / "electron-builder.json").read_text(encoding="utf-8"))
    nsh = (SHELL / "build" / "installer.nsh").read_text(encoding="utf-8")
    main_cjs = (SHELL / "main.cjs").read_text(encoding="utf-8")

    print("[1] 只剩一个入口：旧脚本全删")
    for rel in ("start.bat", "start.ps1", "stop.bat", "stop.ps1",
                "START_NIGHT.bat", "STOP_NIGHT.bat",
                "packaging/build_installer.ps1", "packaging/assets/env-setup.ps1",
                "packaging/test_uninstall.ps1", "packaging/verify_icon.ps1"):
        ck(f"已删除 {rel}", not (ROOT / rel).exists(), str(ROOT / rel))
    leftovers = [str(p.relative_to(ROOT)) for pat in ("*.bat", "*.ps1", "*.cmd")
                 for p in ROOT.rglob(pat)
                 if ".git" not in p.parts and "node_modules" not in p.parts
                 and "_logs" not in p.parts and ".cache" not in p.parts
                 and "dist-installer" not in p.parts
                 and ".tmp" not in str(p.relative_to(ROOT))
                 # 内置 Python 的第三方库自带 .ps1（playwright 安装脚本）——那不是仓库残留
                 and "site-packages" not in p.parts
                 and "shell" not in p.parts]
    ck("仓库根没有任何 bat/ps1/cmd 残留", not leftovers, leftovers[:6])

    print("[2] 安装包结构：自带运行时 + NSIS")
    win = cfg.get("win", {})
    targets = win.get("target") or []
    ck("目标是 nsis x64", any(t.get("target") == "nsis" and "x64" in (t.get("arch") or [])
                              for t in targets if isinstance(t, dict)), targets)
    extra = {e.get("to") for e in cfg.get("extraResources", [])}
    ck("打包内置 Python", "python" in extra, extra)
    ck("打包后端源码", "prototype" in extra, extra)
    ck("不再打包 env-setup.ps1",
       not any("env-setup" in str(e.get("from", "")) for e in cfg.get("extraResources", [])))
    ck("允许自选安装目录（用户口径：要能自己选路径）",
       cfg.get("nsis", {}).get("allowToChangeInstallationDirectory") is True)
    ck("不交给 electron-builder 删数据（我们自己按勾选决定）",
       cfg.get("nsis", {}).get("deleteAppDataOnUninstall") is False)
    ck("挂上自定义安装/卸载逻辑", cfg.get("nsis", {}).get("include") == "build/installer.nsh")

    print("[3] 安装期：许可勾选与同意合一 + 可选组件默认不勾")
    ck("许可页自带勾选框（两页合一，不在 RTF 页后再起一页）",
       "MUI_LICENSEPAGE_CHECKBOX" in nsh and "我已完整阅读并同意" in nsh)
    ck("已删除独立的「我已同意」自定义页",
       "SixCoderTermsCreate" not in nsh and "SixCoderTermsLeave" not in nsh)
    ck("跳过「为谁安装」页（插在许可与目录之间打断流程）",
       "customInstallMode" in nsh and "isForceCurrentInstall" in nsh)
    ck("有可选组件页", "customPageAfterChangeDir" in nsh and "SixCoderOptCreate" in nsh)
    ck("Ollama 默认不勾", "${NSD_Uncheck} $SC_OllamaHwnd" in nsh)
    ck("模型默认不勾", "${NSD_Uncheck} $SC_ModelHwnd" in nsh)
    ck("已装 Ollama 时不重复下载", 'StrCmp $SC_OllamaFound "1"' in nsh)
    ck("环境准备走 EXE 参数（不是 ps1）", "--env-setup" in nsh and ".ps1" not in nsh)
    ck("环境准备快捷方式在开始菜单", "SixCoder 环境准备.lnk" in nsh)

    print("[4] 卸载期：勾选框 + 默认保留")
    ck("有卸载自定义页", "customUnWelcomePage" in nsh and "un.SixCoderKeepCreate" in nsh)
    ck("勾选框存在", "保留本地运行数据" in nsh)
    ck("默认勾选保留", "${NSD_Check} $SC_KeepDataHwnd" in nsh)
    ck("只删程序本体与缓存", "RMDir /r \"$APPDATA\\SixCoder\"" in nsh)
    ck("按勾选删数据目录", "RMDir /r \"$LOCALAPPDATA\\SixCoder\"" in nsh)
    ck("静默卸载支持 /WIPEDATA", "/WIPEDATA" in nsh)
    ck("停本安装目录下的内置 Python（夜班可能还在跑）",
       "ExecutablePath -like ''$INSTDIR*''" in nsh and "python*" in nsh)
    ck("清开机自启", "CurrentVersion\\Run" in nsh and "DeleteRegValue" in nsh)
    ck("清任务栏固定与 AppUserModelId", "User Pinned" in nsh and "AppUserModelId" in nsh)
    ck("不碰用户项目仓库（没有 F:\\ / products 之类硬编码）",
       "F:\\" not in nsh and "biyeshejishuangxuan" not in nsh)

    print("[5] 壳：环境准备与数据目录都归它管")
    ck("支持 --env-setup", "--env-setup" in main_cjs)
    ck("传 SIXCODER_HOME 给后端", "SIXCODER_HOME" in main_cjs and "DATA_HOME" in main_cjs)
    ck("数据目录在 LocalAppData", "LOCALAPPDATA" in main_cjs)
    ck("有「在浏览器中打开」菜单", "在浏览器中打开" in main_cjs)
    ck("环境脚本是 Python", (PROTO / "tools" / "env_setup.py").is_file())

    print("[5b] 壳：端口自适应 + 后端启动诊断（装完双击必须能起）")
    ck("端口可自动 +1 换端口（8760 被占不死等）",
       "choosePort" in main_cjs and "BASE_PORT + 20" in main_cjs)
    ck("真健康检查（校验 app==='sixcoder'，不把别人的页面当后端）",
       "app === 'sixcoder'" in main_cjs and "probeHealth" in main_cjs)
    ck("不再用裸 TCP connect 判断后端就绪", "waitForPort(" not in main_cjs)
    ck("后端日志落盘（远程机器能拿到真实原因）",
       "BACKEND_LOG_FILE" in main_cjs and "appendFileSync" in main_cjs)
    ck("错误页去掉 Ollama 误导、改为看日志",
       "不影响启动" in main_cjs and "ollama serve" not in main_cjs)
    ck("错误页有重试按钮", "location.reload()" in main_cjs)
    ck("health 带 app 标识（供壳校验）", '"app": "sixcoder"' in
       (PROTO / "server.py").read_text(encoding="utf-8"))

    print("[6] 运行时与产物")
    py = SHELL / "resources" / "python" / "python.exe"
    ck("内置解释器存在", py.is_file(), str(py))
    if py.is_file():
        rc = subprocess.call([str(py), "-c",
                              "import fastapi, uvicorn, pydantic, starlette, libcst, httpx,"
                              " tokenizers, coverage, psutil, pytest"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ck("内置解释器能 import 全部运行时依赖（含 pytest：仓库基线要收集用例）", rc == 0)
    for rel in ("python.exe", "python312.dll", "python312.zip", "python312._pth",
                "vcruntime140.dll", "vcruntime140_1.dll", "sqlite3.dll",
                "libssl-3.dll", "libcrypto-3.dll"):
        ck(f"内置 Python 有 {rel}（别人机器不靠本机运行库）", (py.parent / rel).is_file())
    pth = py.parent / "python312._pth"
    if pth.is_file():
        txt = pth.read_text(encoding="utf-8", errors="replace")
        ck("_pth 用相对路径（不夹带开发机绝对路径）",
           not re.search(r"[A-Za-z]:[\\/]", txt), txt[:200])
        ck("_pth 已开启 site-packages", "Lib\\site-packages" in txt or "Lib/site-packages" in txt)
    reqs = (PROTO / "requirements.txt").read_text(encoding="utf-8")
    ck("pytest 在运行时依赖里（不是 dev-only）", "pytest==" in reqs)
    ck("前端 dist 已构建", (PROTO / "web" / "dist" / "index.html").is_file())
    ck("打包脚本是 Python", (ROOT / "packaging" / "build_installer.py").is_file())
    exes = sorted((ROOT / "dist-installer").rglob("SixCoder-Setup-*.exe")) \
        if (ROOT / "dist-installer").is_dir() else []
    print(f"  提示：现有安装包 {[str(e.relative_to(ROOT)) for e in exes] or '（还没构建）'}")
    ck("源码里 env-setup 只剩一份 Python 实现",
       not [p for p in ROOT.rglob("env-setup.ps1")
            if "dist-installer" not in p.parts])

    print("[7] 源码不能夹带开发机绝对路径（别人装上必须能跑）")
    # 打包会把 prototype/ 原样塞进安装包；任何写死的本机路径在别人机器上都不存在。
    # 排除 _logs（历史日志是数据）、.sixcoder（本机私有配置，不进包）。
    _abs_pats = (b"e:/aaatom", b"e:\\aaatom", b"c:/users/lenovo",
                 b"c:\\users\\lenovo", b"d:/python", b"d:\\python")
    _leaks: list[str] = []
    def _code_lines(p: Path) -> bytes:
        """只取非注释行——注释里引用旧路径做反面说明（patrol.py）不算泄漏。"""
        return "\n".join(ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines()
                         if not ln.lstrip().startswith("#")).encode("utf-8", "ignore").lower()

    for p in (PROTO / "core").rglob("*.py"):
        low = _code_lines(p)
        for pat in _abs_pats:
            if pat in low:
                _leaks.append(f"{p.relative_to(ROOT)} -> {pat.decode()}")
                break
    for p in (PROTO / "tools").rglob("*.py"):
        low = _code_lines(p)
        for pat in _abs_pats:
            if pat in low:
                _leaks.append(f"{p.relative_to(ROOT)} -> {pat.decode()}")
                break
    ck("core/ 与 tools/ 源码无本机绝对路径残留", not _leaks, _leaks[:8])
    wd_src = (PROTO / "tools" / "watchdog_night.py").read_text(encoding="utf-8")
    ck("watchdog 的解释器/工程路径运行时推导（PY=_python_exe / PROTOTYPE=parents[1]）",
       "_python_exe()" in wd_src and "PROTOTYPE = str(_ENGINE)" in wd_src
       and "D:/Python" not in wd_src)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
