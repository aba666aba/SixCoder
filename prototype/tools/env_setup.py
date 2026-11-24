"""环境准备：安装本地模型运行时（Ollama）并拉取默认代码模型。

为什么用 Python 重写（2026-11-20 用户口径："bat 之类的都清理干净，功能全都集合到源码
里面"）：原来这是 `packaging/assets/env-setup.ps1`，属于安装器私有脚本，改起来要同时
维护 PowerShell 与 Python 两套逻辑、两套排错经验。现在只剩一份：

    SixCoder.exe --env-setup --ollama            # 只装 Ollama
    SixCoder.exe --env-setup --model             # 拉默认模型（缺 Ollama 会先装）
    SixCoder.exe --env-setup --ollama --model    # 两个都要

安装器的可选组件页调用它；用户也可以稍后从开始菜单「SixCoder 环境准备」再跑。
**它不参与夜班主链路**：远端主力写代码时本地只是手脚，不装也能跑（只是没本地兜底）。

`--dry-run` 只打印打算做什么（不联网、不改系统），自检用它。
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_MODEL = "qwen2.5-coder:7b"
OLLAMA_PORT = 11434
OLLAMA_URL = "https://ollama.com/download/OllamaSetup.exe"
HOME = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "SixCoder"
LOG = HOME / "env-setup.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        HOME.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def find_ollama() -> str:
    """Ollama 装在哪：先看标准安装位置，再退化到 PATH（用户可能用 winget 装的）。"""
    cands = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Ollama" / "ollama.exe",
    ]
    for c in cands:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    got = shutil.which("ollama")
    return got or ""


def server_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{OLLAMA_PORT}/api/tags", timeout=3) as r:
            return int(getattr(r, "status", 200)) == 200
    except Exception:                                    # noqa: BLE001
        return False


def wait_server(seconds: int = 40) -> bool:
    for _ in range(seconds):
        if server_up():
            return True
        time.sleep(1)
    return False


def install_ollama(dry: bool) -> str:
    exe = find_ollama()
    if exe:
        log(f"Ollama 已安装：{exe}")
        return exe
    if dry:
        log(f"[dry-run] 会下载 {OLLAMA_URL} 并静默安装")
        return ""
    # 优先 winget（有就快、还能被系统统一管理）；失败再退回官方安装包
    try:
        from core import envinstall as ei                     # noqa: PLC0415
        if ei._resolve_mgr("winget"):
            log("尝试 winget 静默安装 Ollama ...")
            res = ei.install("winget", ["Ollama.Ollama"], timeout=1800)
            exe = find_ollama()
            if exe:
                log(f"winget 安装成功：{exe}")
                return exe
            log(f"winget 未成功（{res.get('detail') or res.get('error') or '未知原因'}），改用官方安装包")
    except Exception as exc:                             # noqa: BLE001
        log(f"winget 路线不可用：{type(exc).__name__}: {exc}")

    dst = Path(os.environ.get("TEMP") or ".") / "OllamaSetup.exe"
    log("下载 Ollama 安装包（约 1.1 GB，国内速度取决于网络）...")
    try:
        with urllib.request.urlopen(OLLAMA_URL, timeout=60) as r, dst.open("wb") as fh:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if total:
                    print(f"\r  已下载 {got / 1048576:.0f} / {total / 1048576:.0f} MB", end="", flush=True)
        print("", flush=True)
    except Exception as exc:                             # noqa: BLE001
        log(f"下载失败：{type(exc).__name__}: {exc}")
        return ""
    log("运行静默安装（Inno Setup：/VERYSILENT /NORESTART /SP-）")
    try:
        subprocess.run([str(dst), "/VERYSILENT", "/NORESTART", "/SP-"], timeout=1800,
                       check=False)
    except Exception as exc:                             # noqa: BLE001
        log(f"安装程序执行失败：{type(exc).__name__}: {exc}")
    time.sleep(3)
    exe = find_ollama()
    log(f"安装后检测：{exe or '仍未找到 ollama.exe'}")
    return exe


def ensure_server(exe: str, dry: bool) -> bool:
    if server_up():
        log("Ollama 服务已在运行")
        return True
    if dry:
        log("[dry-run] 会启动 ollama serve 并等端口 11434")
        return True
    log("启动 ollama serve ...")
    try:
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:                             # noqa: BLE001
        log(f"启动失败：{type(exc).__name__}: {exc}")
        return False
    ok = wait_server(40)
    log("服务已就绪" if ok else "服务 40 秒内没起来")
    return ok


def pull_model(exe: str, model: str, dry: bool) -> bool:
    if dry:
        log(f"[dry-run] 会执行：{exe or 'ollama'} pull {model}")
        return True
    log(f"拉取模型 {model}（约 4.7 GB，可能要很久）...")
    try:
        rc = subprocess.call([exe, "pull", model])
    except Exception as exc:                             # noqa: BLE001
        log(f"拉取失败：{type(exc).__name__}: {exc}")
        return False
    log("模型拉取完成" if rc == 0 else f"模型拉取失败，退出码 {rc}")
    return rc == 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="SixCoder 环境准备（Ollama / 默认模型）")
    ap.add_argument("--ollama", action="store_true", help="安装 Ollama 运行环境")
    ap.add_argument("--model", action="store_true", help="拉取默认代码模型")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不联网不改系统")
    args = ap.parse_args(argv)

    log("=== SixCoder 环境准备 ===")
    if not args.ollama and not args.model:
        log("没有选中任何组件（--ollama / --model），什么都不做")
        return 0
    exe = find_ollama()
    log(f"当前 Ollama：{exe or '未安装'}")
    if args.ollama or (args.model and not exe):
        exe = install_ollama(args.dry_run) or exe
    if args.model:
        if not exe:
            log("拉取模型需要 Ollama，但它不可用 —— 中止")
            return 1
        if ensure_server(exe, args.dry_run):
            pull_model(exe, DEFAULT_MODEL, args.dry_run)
        else:
            return 1
    log(f"=== 结束 ===（日志：{LOG}）")
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
