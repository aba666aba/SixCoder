"""常用工具清单 + PATH 绑定：默认不绑定，绑定与否则由用户自己决定（2026-11-20 用户口径）。

为什么单独一层（而不是塞进 recon.TC_SPEC）：
  · recon 的 9 项是**引擎判据**（缺了会影响死局/语言分层判定），随便加 ruby/php 这类
    可选工具会制造"假缺工具链"；本模块是**界面清单**，只负责"有哪些常用工具、
    装没装、要不要写进 PATH"，不参与任何判定。
  · 安装器下载的运行时落在 %LOCALAPPDATA%\\SixCoder\\runtimes，默认**不写 PATH**
    （写 PATH 会污染整台机器，而且卸载很难清干净）。用户点「绑定」才写当前用户的
    Environment\\Path，并记一份清单；卸载时按清单逐条移除，绝不碰用户自己加的条目。

安全边界（自检钉死）：
  1. 只读 HKCU\\Environment，不写 HKLM（per-user 安装不需要管理员权限）；
  2. 绑定目录必须真实存在且里面真有可执行文件；不存在的工具不给绑；
  3. 若目标目录用户自己早加过，标记 added=False —— 解绑时**不删**它；
  4. 解绑/全清只认清单里 added=True 的目录，清单丢了就什么都不动（不猜、不删用户 PATH）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import paths as _paths, recon as _recon, runtimes as _rt

# id → 显示名（recon 的 9 项 + 下面 EXTRA_TOOLS，界面一次性列全）
LABELS: dict[str, str] = {
    "java": "Java（JRE）", "javac": "Java 编译器（javac）",
    "maven": "Maven", "node": "Node.js", "npm": "npm", "npx": "npx",
    "yarn": "Yarn", "pnpm": "pnpm", "python": "Python", "pip": "pip",
    "git": "Git", "docker": "Docker", "go": "Go", "gcc": "GCC（MinGW）",
    "g++": "G++（MinGW）", "cargo": "Cargo（Rust）", "rustc": "rustc",
    "dotnet": ".NET SDK", "php": "PHP", "ruby": "Ruby",
}

# 工具 id → 可绑定的自带运行时 id（None = 用户自己装的，绑其可执行文件所在目录）
TOOL_RUNTIME: dict[str, str | None] = {
    "python": "python", "pip": "python", "java": "java", "javac": "java",
    "go": "go", "gcc": "mingw", "g++": "mingw",
}

# recon.TC_SPEC 没有、但日常常用的工具：只探测 + 展示，不参与引擎判定
# (id, argv, 版本正则, 关键版本, 可执行文件)
EXTRA_TOOLS: list[tuple[str, list[str], str, int, str]] = [
    ("javac",  ["javac", "-version"],   r"javac\s+([\d.]+)", 8, "javac.exe"),
    ("pip",    ["pip", "--version"],    r"pip\s+([\d.]+)", 20, "pip.exe"),
    ("npx",    ["npx", "-v"],           r"([\d.]+)", 1, "npx.cmd"),
    ("yarn",   ["yarn", "-v"],          r"([\d.]+)", 1, "yarn.cmd"),
    ("pnpm",   ["pnpm", "-v"],          r"([\d.]+)", 1, "pnpm.cmd"),
    ("g++",    ["g++", "--version"],    r"\b(\d+\.\d+(?:\.\d+)?)\b", 8, "g++.exe"),
    ("cargo",  ["cargo", "--version"],  r"cargo\s+([\d.]+)", 1, "cargo.exe"),
    ("rustc",  ["rustc", "--version"],  r"rustc\s+([\d.]+)", 1, "rustc.exe"),
    ("dotnet", ["dotnet", "--version"], r"(\d+\.\d+(?:\.\d+)*)", 6, "dotnet.exe"),
    ("php",    ["php", "-v"],           r"PHP\s+([\d.]+)", 7, "php.exe"),
    ("ruby",   ["ruby", "-v"],          r"ruby\s+([\d.]+)", 2, "ruby.exe"),
]


def manifest_path() -> Path:
    return _paths.home() / "pathbinds.json"


def _load_manifest() -> dict:
    try:
        data = json.loads(manifest_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _save_manifest(data: dict) -> None:
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------- 用户 PATH 读写（测试可整体替换这两个函数，绝不碰真机）

def _norm(item: str) -> str:
    return str(item).strip().strip('"').rstrip("\\/").casefold()


def _read_user_path() -> str:
    """当前用户的 PATH（HKCU\\Environment\\Path）；非 Windows 回落进程环境。"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                                winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "Path")
                return str(val or "")
        except Exception:                                # noqa: BLE001
            return ""
    return str(os.environ.get("PATH") or "")


def _read_machine_path() -> str:
    """只读：系统 PATH 里已有的目录也算"已绑定"（显示用，永不写它）。"""
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
                                0, winreg.KEY_READ) as key:
                val, _ = winreg.QueryValueEx(key, "Path")
                return str(val or "")
        except Exception:                                # noqa: BLE001
            return ""
    return ""


def _write_user_path(value: str) -> None:
    """只写 HKCU\\Environment\\Path（REG_EXPAND_SZ），并通知新进程生效。"""
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_READ) as key:
            winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)
        # 广播 WM_SETTINGCHANGE：新开的资源管理器/终端能立刻看到，老窗口仍需重开
        try:
            import ctypes
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, None)
        except Exception:                                # noqa: BLE001
            pass
    else:
        os.environ["PATH"] = value


def _path_entries() -> list[str]:
    return [x for x in (_read_user_path() + os.pathsep + _read_machine_path()).split(os.pathsep) if x.strip()]


def _runtime_bind_dir(rid: str | None) -> str:
    if not rid:
        return ""
    for ver in _rt.RUNTIMES.get(rid, {}).get("versions", {}):
        if not _rt.installed(rid, ver):
            continue
        spec = _rt.RUNTIMES[rid]["versions"][ver]
        d = _rt.dest_for(rid, ver) / (spec.get("bin_sub") or "")
        if d.is_dir():
            return str(d)
    return ""


def _probe(argv: list[str], ver_re: str) -> tuple[str | None, str | None]:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=8,
                           errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = ((r.stdout or "") + "\n" + (r.stderr or ""))[:4000]
        m = re.search(ver_re, out)
        found = shutil_which(argv[0])
        return (m.group(1) if m else None), found
    except Exception:                                    # noqa: BLE001
        return None, None


def shutil_which(cmd: str) -> str | None:
    import shutil
    return shutil.which(cmd)


def state(overrides: dict | None = None) -> list[dict]:
    """常用工具全量清单（含未安装项）；每项带 bound（默认 False）。"""
    manifest = _load_manifest()
    entries = {_norm(x) for x in _path_entries()}
    items: list[dict] = []

    for t in _recon.toolchains(overrides):
        rid = TOOL_RUNTIME.get(t["id"])
        bind_dir = _runtime_bind_dir(rid) if rid else ""
        if not bind_dir and t.get("path"):
            bind_dir = str(Path(t["path"]).parent)   # 用户自己装的工具也允许绑定
        items.append(_item(t["id"], t.get("version"), t.get("path"), t.get("ok"),
                           bind_dir, entries, manifest))

    for tid, argv, ver_re, need, exe in EXTRA_TOOLS:
        ver, path = _probe(argv, ver_re)
        rid = TOOL_RUNTIME.get(tid)
        bind_dir = _runtime_bind_dir(rid) if rid else (str(Path(path).parent) if path else "")
        # 自带运行时优先（哪怕 PATH 里另有同名工具，也允许绑我们装的那份）
        if not bind_dir and path:
            bind_dir = str(Path(path).parent)
        if not bind_dir and rid:
            bind_dir = str((_paths.home() / "runtimes" / rid).resolve())
        major = re.match(r"\d+", str(ver or ""))
        ok = bool(ver) and bool(major) and int(major.group(0)) >= need
        items.append(_item(tid, ver, path, ok, bind_dir, entries, manifest))

    return items


def _item(tid: str, version: str | None, path: str | None, ok: bool | None,
          bind_dir: str, entries: set[str], manifest: dict) -> dict:
    """一条清单：installed 看"有没有版本"，bound 看"目录在不在用户/系统 PATH"。"""
    d = _norm(bind_dir) if bind_dir else ""
    in_path = bool(d) and d in entries
    # 三种状态分开：我们绑定的可以解绑；系统里本来就在 PATH 的不能解绑（那不是我们加的）
    added = bool((manifest.get(tid) or {}).get("added"))
    if added:
        label = "已绑定"
    elif in_path:
        label = "系统 PATH"
    else:
        label = "未绑定"
    return {
        "id": tid, "label": LABELS.get(tid, tid),
        "installed": bool(version), "ok": bool(ok),
        "version": version, "path": path,
        "bind_dir": bind_dir or None,
        "bound": in_path,
        "added": added,
        "bind_label": label,
        "bindable": bool(bind_dir and Path(bind_dir).is_dir() and d != _norm(str(_paths.home()))),
        "source": "自带运行时" if bind_dir and "runtimes" in str(bind_dir) else "系统/自定义",
    }


def bind(tool_id: str) -> dict:
    """把工具目录写进当前用户 PATH（幂等）。返回 {ok, id, bound, detail}。"""
    item = next((x for x in state() if x["id"] == tool_id), None)
    if not item:
        return {"ok": False, "id": tool_id, "detail": f"未知工具：{tool_id}"}
    d = str(item.get("bind_dir") or "")
    if not d or not Path(d).is_dir():
        return {"ok": False, "id": tool_id, "detail": f"{item['label']} 尚未安装，无法绑定 PATH"}
    if len(d) < 4 or Path(d).parent == Path(d):
        return {"ok": False, "id": tool_id, "detail": "拒绝绑定盘符根目录"}

    entries = [x.strip().strip('"') for x in _read_user_path().split(os.pathsep) if x.strip()]
    # 系统 PATH 里已有也算"已绑定"：不重复写，也不把它记成我们加的（解绑不能删用户自己的）
    already = any(_norm(x) == _norm(d) for x in entries) or _norm(d) in {
        _norm(x) for x in _read_machine_path().split(os.pathsep) if x.strip()
    }
    if not already:
        entries.append(d)
        # 去重（大小写不敏感），不改用户原有条目的顺序与写法
        seen: set[str] = set()
        unique: list[str] = []
        for x in entries:
            if _norm(x) not in seen:
                seen.add(_norm(x))
                unique.append(x)
        try:
            _write_user_path(os.pathsep.join(unique))
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "id": tool_id, "detail": f"写用户 PATH 失败：{exc}"}

    manifest = _load_manifest()
    # 幂等的关键：重复点「绑定」不能把"我们自己加的"改写成"用户自己加的"，
    # 否则之后解绑会拒绝删除、PATH 里留下永久残条目（check_pathbind 抓到的真 bug）。
    prev = (manifest.get(tool_id) or {}).get("added")
    added = bool(prev) if prev is not None else (not already)
    manifest[tool_id] = {"dir": d, "added": added}
    _save_manifest(manifest)
    return {"ok": True, "id": tool_id, "bound": True,
            "detail": "目录已在 PATH 中" if already else f"已加入当前用户 PATH：{d}"}


def unbind(tool_id: str) -> dict:
    """只移除我们加过的那条；清单里 added=False（用户自己加的）一律不动。"""
    manifest = _load_manifest()
    rec = manifest.get(tool_id)
    if not rec:
        return {"ok": True, "id": tool_id, "removed": False, "detail": "没有本程序添加的绑定"}
    if not rec.get("added"):
        manifest.pop(tool_id, None)
        _save_manifest(manifest)
        return {"ok": True, "id": tool_id, "removed": False,
                "detail": "该目录是你自己加入 PATH 的，未改动"}

    target = _norm(str(rec.get("dir") or ""))
    entries = [x.strip().strip('"') for x in _read_user_path().split(os.pathsep) if x.strip()]
    kept = [x for x in entries if _norm(x) != target]
    if len(kept) != len(entries):
        try:
            _write_user_path(os.pathsep.join(kept))
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "id": tool_id, "detail": f"从 PATH 移除失败：{exc}"}
    manifest.pop(tool_id, None)
    _save_manifest(manifest)
    return {"ok": True, "id": tool_id, "removed": True, "detail": "已从当前用户 PATH 移除"}


def unbind_all(home: str | None = None) -> dict:
    """卸载期调用：按清单清掉所有我们写进 PATH 的条目（与"是否保留数据"无关）。"""
    if home:
        os.environ["SIXCODER_HOME"] = str(home)
    manifest = _load_manifest()
    if not manifest:
        return {"ok": True, "removed": 0, "detail": "没有需要清理的 PATH 绑定"}
    targets = {_norm(str((r or {}).get("dir") or "")) for tid, r in manifest.items()
               if (r or {}).get("added")}
    targets.discard("")
    entries = [x.strip().strip('"') for x in _read_user_path().split(os.pathsep) if x.strip()]
    kept = [x for x in entries if _norm(x) not in targets]
    err = ""
    if len(kept) != len(entries):
        try:
            _write_user_path(os.pathsep.join(kept))
        except Exception as exc:                         # noqa: BLE001
            err = str(exc)
    p = manifest_path()
    try:
        p.unlink(missing_ok=True)
    except OSError as exc:                               # noqa: BLE001
        err = err or str(exc)
    return {"ok": not err, "removed": len(entries) - len(kept),
            "detail": err or f"已清理 {len(entries) - len(kept)} 条 PATH 绑定"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0].startswith("--home="):
        os.environ["SIXCODER_HOME"] = argv.pop(0).split("=", 1)[1]
    if argv and argv[0] == "--home" and len(argv) > 1:
        argv.pop(0)
        os.environ["SIXCODER_HOME"] = argv.pop(0)
    cmd = argv[0] if argv else "list"
    if cmd in ("list", "ls"):
        for it in state():
            flags = ("已绑定" if it["bound"] else "未绑定")
            print(f"{it['id']:8s} {it['label']:18s} {flags:6s} "
                  f"{it.get('version') or '未安装':10s} {it.get('bind_dir') or ''}")
        return 0
    if cmd == "bind" and len(argv) > 1:
        print(bind(argv[1]))
        return 0
    if cmd == "unbind" and len(argv) > 1:
        print(unbind(argv[1]))
        return 0
    if cmd in ("unbind-all", "uninstall"):
        print(unbind_all())
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
