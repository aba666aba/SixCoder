"""自带语言运行时：安装时按需下载，用户勾选哪几个就装哪几个（2026-11-20 用户口径）。

为什么不做进安装包本体：五个运行时解压后接近 1.3 GB，而"使用量最大的版本"半年就变；
塞进包体等于每次更新都让用户重下 600MB+，还立刻过时。所以：
  · 安装器只放**勾选框**，勾中的在安装结束后下载解压（和现有 Ollama 那条一样）；
  · 运行时落在 `%LOCALAPPDATA%\\SixCoder\\runtimes\\<id>-<version>\\`（**不在安装目录**），
    覆盖安装不会碰它，卸载时按"是否保留本地数据"一起决定；
  · 引擎靠 `core/recon.py` 的常见目录探测把它认出来，**不写死 PATH**（用户自己的环境优先）。

许可边界（必须记住）：
  · Python(PSF) / Temurin JDK(GPLv2+CE) / Go(BSD-3) / MinGW-w64(GPL+运行时例外) /
    Git(GPLv2) 可分发；
  · **MSVC 不能随包分发**（微软许可），所以 C/C++ 只提供 MinGW-w64 的 GCC。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

from . import paths as _paths

# 每个版本：kind=exe（官方安装器静默装）| zip（直接解压）
# urls 按顺序尝试（国内镜像在前，官方兜底）
# bin_sub：装完之后可执行文件相对 dest 的子目录（探测时用来定位，也用于确认装成功）
RUNTIMES: dict[str, dict] = {
    "python": {
        "label": "Python",
        "hint": "完整 CPython（能建 venv、能 pip）。3.13 主流，3.12 兼容性最好",
        "versions": {
            "3.13": {
                "label": "Python 3.13.7", "size_mb": 28, "kind": "exe",
                "urls": ["https://www.python.org/ftp/python/3.13.7/python-3.13.7-amd64.exe"],
                "exe_args": ["/quiet", "InstallAllUsers=0", "PrependPath=0",
                             "Include_test=0", "Include_pip=1", "Include_launcher=0",
                             "TargetDir={dest}"],
                "bin_sub": "", "exe": "python.exe", "probe": ["python", "--version"],
            },
            "3.12": {
                "label": "Python 3.12.10", "size_mb": 28, "kind": "exe",
                "urls": ["https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"],
                "exe_args": ["/quiet", "InstallAllUsers=0", "PrependPath=0",
                             "Include_test=0", "Include_pip=1", "Include_launcher=0",
                             "TargetDir={dest}"],
                "bin_sub": "", "exe": "python.exe", "probe": ["python", "--version"],
            },
        },
    },
    "java": {
        "label": "JDK（Java）",
        "hint": "Eclipse Temurin，带 javac（能编译）。21 / 17 都是 LTS",
        "versions": {
            "21": {
                "label": "Temurin JDK 21 LTS", "size_mb": 190, "kind": "zip",
                "urls": [
                    "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/21/jdk/x64/windows/"
                    "OpenJDK21U-jdk_x64_windows_hotspot_21.0.13_9.zip",
                    "https://mirrors.ustc.edu.cn/adoptium/21/jdk/x64/windows/"
                    "OpenJDK21U-jdk_x64_windows_hotspot_21.0.13_9.zip",
                ],
                "bin_sub": "bin", "exe": "javac.exe", "probe": ["java", "-version"],
            },
            "17": {
                "label": "Temurin JDK 17 LTS", "size_mb": 180, "kind": "zip",
                "urls": [
                    "https://mirrors.tuna.tsinghua.edu.cn/Adoptium/17/jdk/x64/windows/"
                    "OpenJDK17U-jdk_x64_windows_hotspot_17.0.21_9.zip",
                ],
                "bin_sub": "bin", "exe": "javac.exe", "probe": ["java", "-version"],
            },
        },
    },
    "go": {
        "label": "Go",
        "hint": "官方发行版，自带编译器（go build 可用）",
        "versions": {
            "1.27": {
                "label": "Go 1.27.4", "size_mb": 75, "kind": "zip",
                "urls": ["https://mirrors.aliyun.com/golang/go1.27.4.windows-amd64.zip",
                         "https://go.dev/dl/go1.27.4.windows-amd64.zip"],
                "bin_sub": "bin", "exe": "go.exe", "probe": ["go", "version"],
            },
            "1.26": {
                "label": "Go 1.26.x", "size_mb": 72, "kind": "zip",
                "urls": ["https://mirrors.aliyun.com/golang/go1.26.5.windows-amd64.zip",
                         "https://go.dev/dl/go1.26.5.windows-amd64.zip"],
                "bin_sub": "bin", "exe": "go.exe", "probe": ["go", "version"],
            },
        },
    },
    "mingw": {
        "label": "C/C++ 编译器（MinGW-w64）",
        "hint": "GCC 16 + gdb，C/C++ 都能编译。MSVC 无法随包分发，需要的话自己装 VS",
        "versions": {
            "16": {
                "label": "MinGW-w64 GCC 16.2.0", "size_mb": 120, "kind": "zip",
                "urls": [
                    "https://github.com/brechtsanders/winlibs_mingw/releases/download/"
                    "16.2.0llvm-23.1.3-14.0.0-r3/"
                    "winlibs-x86_64-posix-seh-gcc-16.2.0-llvm-23.1.3-mingw-w64ucrt-14.0.0-r3.zip",
                ],
                "bin_sub": "bin", "exe": "gcc.exe", "probe": ["gcc", "--version"],
            },
        },
    },
    "git": {
        "label": "Git",
        "hint": "MinGit 便携版，仓库探测/演示仓库/版本历史要用；不装也能启动（只是没有 Git 能力）",
        "versions": {
            "2.56.1": {
                "label": "MinGit 2.56.1", "size_mb": 52, "kind": "zip",
                "urls": [
                    "https://registry.npmmirror.com/-/binary/git-for-windows/"
                    "v2.56.1.windows.1/MinGit-2.56.1-64-bit.zip",
                    "https://github.com/git-for-windows/git/releases/download/"
                    "v2.56.1.windows.1/MinGit-2.56.1-64-bit.zip",
                ],
                "bin_sub": "cmd", "exe": "git.exe", "probe": ["git", "--version"],
            },
        },
    },
}


def root() -> Path:
    """运行时根目录（与安装目录分离，覆盖安装不受影响）。"""
    return _paths.ensure(_paths.home() / "runtimes")


def dest_for(rid: str, ver: str) -> Path:
    return root() / f"{rid}-{ver}"


def ensure_on_path(rid: str) -> str | None:
    """把自带运行时的 bin 目录临时加到**当前进程** PATH（不写系统 PATH）。

    为什么需要：`core/tools.py` / `isolate.py` / `arch.py` 里大量 `["git", ...]`
    是按 PATH 找的；MinGit 装在 `<data>/runtimes/git-*/cmd`，不临时加 PATH 就
    "装了等于没装"。只影响当前进程及其子进程，退出即失效，不污染用户环境。
    """
    meta = RUNTIMES.get(rid)
    if not meta:
        return None
    for ver in meta["versions"].values():
        for dest in sorted(root().glob(f"{rid}-*")):
            exe = dest / (ver.get("bin_sub") or "") / (ver.get("exe") or "")
            if exe.is_file():
                parent = str(exe.parent)
                parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
                if parent not in parts:
                    os.environ["PATH"] = parent + os.pathsep + os.environ.get("PATH", "")
                return str(exe)
    return None


def resolve(spec: str) -> tuple[str, str, dict]:
    """'python@3.13' / 'python' → (rid, ver, 定义)。缺版本取该语言第一个（最新）。"""
    raw = (spec or "").strip().lower()
    rid, _, ver = raw.partition("@")
    if rid not in RUNTIMES:
        raise ValueError(f"未知运行时：{spec}（可选 {', '.join(RUNTIMES)}）")
    vers = RUNTIMES[rid]["versions"]
    ver = ver or next(iter(vers))
    if ver not in vers:
        raise ValueError(f"{rid} 没有 {ver} 版（可选 {', '.join(vers)}）")
    return rid, ver, vers[ver]


def installed(rid: str, ver: str) -> bool:
    """装没装：看关键可执行文件在不在（不跑命令，够快，界面轮询用）。"""
    d = dest_for(rid, ver)
    try:
        _, _, spec = resolve(f"{rid}@{ver}")
        sub = spec.get("bin_sub") or ""
        exe = spec.get("exe") or ""
        return bool(exe) and (d / sub / exe).is_file()
    except Exception:                                    # noqa: BLE001
        return False


def list_all() -> list[dict]:
    """给界面/安装器用的清单（含已装状态与体积）。"""
    out: list[dict] = []
    for rid, meta in RUNTIMES.items():
        items = []
        for ver, spec in meta["versions"].items():
            items.append({"version": ver, "label": spec["label"],
                          "size_mb": spec["size_mb"], "installed": installed(rid, ver),
                          "path": str(dest_for(rid, ver))})
        out.append({"id": rid, "label": meta["label"], "hint": meta["hint"],
                    "versions": items})
    return out


def _download(urls: list[str], dst: Path, log) -> str:
    """按顺序试源下载，返回成功的 url。先写 .part，成功再改名（断网不留半截包）。"""
    tmp = dst.with_suffix(dst.suffix + ".part")
    last = ""
    for url in urls:
        try:
            log(f"下载 {url}")
            with urllib.request.urlopen(url, timeout=60) as r, tmp.open("wb") as fh:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    if total and got % (1024 * 1024 * 8) < 1024 * 256:
                        log(f"  {got // 1048576}/{total // 1048576} MB")
            if tmp.stat().st_size < 1024:
                raise OSError("下载内容过小，疑似错误页")
            tmp.replace(dst)
            return url
        except Exception as exc:                         # noqa: BLE001
            last = f"{type(exc).__name__}: {exc}"
            log(f"  源不可用（{last}），换下一个")
            try:
                tmp.unlink()
            except OSError:
                pass
    raise RuntimeError(f"所有下载源都失败：{last}")


def _strip_extract(zip_path: Path, dest: Path) -> None:
    """解压并剥掉压缩包里的顶层目录（JDK 是 jdk-21.x/，Go 是 go/，MinGW 是 mingw64/）。"""
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.strip("/")]
        tops = {n.split("/", 1)[0] for n in names}
        strip = len(tops) == 1 and all("/" in n for n in names)
        for member in zf.infolist():
            if member.is_dir():
                continue
            rel = member.filename.split("/", 1)[1] if strip and "/" in member.filename else member.filename
            if not rel:
                continue
            target = (dest / rel).resolve()
            if not str(target).startswith(str(dest.resolve())):
                continue                                  # 防 zip slip
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)


def _verify(dest: Path, spec: dict) -> tuple[bool, str]:
    """真跑一次 --version 才算装上（"文件在"不等于"能用"）。"""
    sub = spec.get("bin_sub") or ""
    exe = dest / sub / (spec.get("exe") or "")
    if not exe.is_file():
        return False, f"缺少可执行文件：{exe}"
    argv = [str(exe)] + list(spec.get("probe") or [])[1:]
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60,
                           errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        return True, out.splitlines()[0][:120] if out else "ok"
    except Exception as exc:                             # noqa: BLE001
        return False, f"执行失败：{type(exc).__name__}: {exc}"


def install(spec: str, log=None) -> dict:
    """装一个运行时（幂等）。返回 {ok, id, version, path, detail}。"""
    log = log or (lambda m: print(m, flush=True))
    rid, ver, meta = resolve(spec)
    dest = dest_for(rid, ver)
    if installed(rid, ver):
        ok, det = _verify(dest, meta)
        log(f"{rid} {ver} 已安装（{det}）")
        return {"ok": ok, "id": rid, "version": ver, "path": str(dest),
                "detail": det, "already": True}

    dl_dir = _paths.ensure(_paths.home() / "downloads")
    kind = meta["kind"]
    suffix = ".exe" if kind == "exe" else ".zip"
    pkg = dl_dir / f"{rid}-{ver}{suffix}"
    try:
        url = _download(list(meta["urls"]), pkg, log)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "id": rid, "version": ver, "detail": str(exc)}

    log(f"安装到 {dest}")
    try:
        if kind == "exe":
            dest.mkdir(parents=True, exist_ok=True)
            args = [str(pkg)] + [a.format(dest=str(dest)) for a in meta.get("exe_args", [])]
            r = subprocess.run(args, timeout=1800, check=False,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode not in (0, 3010):
                return {"ok": False, "id": rid, "version": ver,
                        "detail": f"安装器返回 {r.returncode}"}
        else:
            _strip_extract(pkg, dest)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "id": rid, "version": ver,
                "detail": f"解压/安装失败：{type(exc).__name__}: {exc}"}

    ok, det = _verify(dest, meta)
    try:
        pkg.unlink()
    except OSError:
        pass
    log(("装好了：" if ok else "装完但不可用：") + det)
    return {"ok": ok, "id": rid, "version": ver, "path": str(dest),
            "detail": det, "url": url}


def remove(rid: str, ver: str) -> dict:
    """卸掉一个自带运行时（只删 runtimes 下的这个目录，不碰系统环境）。"""
    d = dest_for(rid, ver)
    if not d.is_dir():
        return {"ok": True, "removed": False, "detail": "本来就没装"}
    try:
        shutil.rmtree(d)
        return {"ok": True, "removed": True, "detail": str(d)}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "removed": False, "detail": f"{type(exc).__name__}: {exc}"}


def main(argv: list[str] | None = None) -> int:
    """命令行入口：`python -m core.runtimes list | install python@3.13 | remove go@1.27`。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("list", "ls"):
        for item in list_all():
            print(f"{item['id']:8s} {item['label']}")
            for v in item["versions"]:
                mark = "已装" if v["installed"] else "未装"
                print(f"    @{v['version']:6s} {v['label']:28s} {v['size_mb']:4d}MB  {mark}")
        return 0
    cmd = argv[0]
    if cmd == "install" and len(argv) > 1:
        # 支持一次装多个：安装器勾选多项时只开一个窗口跑一遍
        all_ok = True
        for spec in argv[1:]:
            r = install(spec)
            print(r)
            all_ok = all_ok and bool(r.get("ok"))
        return 0 if all_ok else 1
    if cmd == "remove" and len(argv) > 1:
        rid, _, ver = argv[1].partition("@")
        print(remove(rid, ver or next(iter(RUNTIMES[rid]["versions"]))))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
