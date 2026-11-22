"""SSH 通用凭据：用户名/密码、私钥、agent 三种认证，凭据明文永不落盘。

## 为什么要有这个模块（2026-11-23，用户："有远端服务器密码、用户名呢，写得通用一点"）

deploy.py 初版只认 agent / 私钥路径引用，把密码认证写死成"不支持，会落明文"。
但现实里大量服务器只给**用户名 + 密码**（没有私钥、也不给生成）。要"通用"就得支持它，
又不能倒退到"把密码填进 settings.json"——那条路正是 2026-11-20 安装包夹带 key 事故的根源。

本模块的解法：**配置里只存凭据"引用"，不存凭据本体**。引用有三种：
  · ``env:VARNAME``   从环境变量读（闸道/CI/本机 set 一下即可，最常用）；
  · ``file:PATH``     从工作区外、用户自己管理的本机文件读（引擎只读，绝不创建/写入）；
  · ``prompt``        执行时交互输入（CLI tty 或注入 secret_fn；Web 无 tty 时拒绝改用前两种）。

任何"不带 scheme 的裸值"一律当明文拒绝写入配置——从入口就堵住手滑落盘。

## 密码怎么到 ssh 手里（关键）

Windows OpenSSH **不从 stdin 读密码**（从控制台设备读，管道进去没用）；跨平台唯一可靠的
非交互通道是 ``SSH_ASKPASS``：ssh 执行一个 helper 程序，读它 stdout 的第一行当密码。
- helper 是一个临时目录里的极小脚本，**只含环境变量名，不含密码本体**；
- 密码只放在 ssh 子进程的 env 块里（随机变量名），helper 从那里读出来写 stdout；
- ``SSH_ASKPASS_REQUIRE=force`` 保证有控制台时也会调 helper（Win11/OpenSSH 8.4+ 必需）；
- 用完整个临时目录 rmtree，密码引用置空。全程不进 argv、审计、日志、对外视图。

## 设计纪律
- 纯函数式解析（`normalize` / `resolve` / `describe`），可注入 `secret_fn` 自检；
- fail-closed：引用格式非法、变量未设、文件不存在、无 tty → 全部拒绝，不猜测、不回落；
- 与风险协商门（risk_consensus ≥2）+ 强制人审（approval.force）叠加，本模块只管"取凭据"。
"""
from __future__ import annotations

import getpass
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable

from . import settings_store as _settings

# 凭据引用的三种 scheme；裸值（不带 scheme）一律拒绝。
CRED_KINDS = ("env", "file", "prompt")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def normalize_credential(raw: Any) -> str:
    """规范化凭据引用。拒绝明文密码：没有 scheme 的值一律丢弃。

    返回空串 = 未配置（执行时会 fail-closed 拒绝），绝不原样保存可疑输入。
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low in ("prompt", "ask", "interactive"):
        return "prompt"
    if low.startswith("env:"):
        name = s[4:].strip()
        return f"env:{name}" if _ENV_NAME_RE.match(name) else ""
    if low.startswith("file:"):
        path = s[5:].strip()
        if path and len(path) <= 512 and "\n" not in path and "\r" not in path:
            return f"file:{path}"
        return ""
    # 裸值 = 疑似明文密码；绝不写进配置
    return ""


def credential_kind(ref: str) -> str:
    s = str(ref or "").strip().lower()
    if s == "prompt":
        return "prompt"
    if s.startswith("env:"):
        return "env"
    if s.startswith("file:"):
        return "file"
    return ""


def describe_credential(ref: str, reveal: bool = False) -> dict[str, Any]:
    """对外视图：只给 kind / 是否已配置 / 掩码，绝不回密码本体。

    `reveal=True`（仅本机回环接口）额外给引用串本身——它也不是密码，
    但按 key_ref 的同款纪律默认脱敏，避免日志/截图误带路径。
    """
    ref = normalize_credential(ref)
    out: dict[str, Any] = {
        "kind": credential_kind(ref),
        "configured": bool(ref),
        "masked": _settings.mask(ref) if ref else "",
    }
    if reveal:
        out["value"] = ref
    return out


def resolve_password(ref: str, *, secret_fn: Callable[[], str] | None = None,
                     interactive: bool | None = None) -> tuple[str, str]:
    """按引用取密码明文（只在内存）。返回 ``(password, error)``，失败给可读原因。

    `secret_fn` 可注入（自检 / Web 一次性输入通道）；否则 prompt 只在真 tty 下问。
    """
    kind = credential_kind(normalize_credential(ref))
    if not kind:
        return "", "未配置合法凭据引用（只接受 env:VAR / file:PATH / prompt；明文会被拒绝）"
    if kind == "env":
        name = str(ref)[4:]
        val = os.environ.get(name, "") or ""
        if not val:
            return "", f"环境变量 {name} 未设置或为空"
        return val, ""
    if kind == "file":
        raw = str(ref)[5:]
        p = Path(raw).expanduser()
        if not p.is_file():
            return "", "凭据文件不存在（请在本机放好；引擎绝不创建它）"
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception as exc:                         # noqa: BLE001
            return "", f"读取凭据文件失败：{type(exc).__name__}"
        val = lines[0].strip() if lines else ""
        if not val:
            return "", "凭据文件第一行为空"
        return val, ""
    # prompt
    try:
        if secret_fn is not None:
            val = str(secret_fn() or "")
        else:
            if interactive is None:
                interactive = bool(getattr(sys.stdin, "isatty", lambda: False)())
            if not interactive:
                return "", ("凭据方式=prompt 但没有交互终端/注入通道；"
                            "Web/夜班场景请改用 env:VAR 或 file:PATH")
            val = getpass.getpass("SSH 密码：")
        return (val, "") if val else ("", "未提供密码")
    except Exception as exc:                             # noqa: BLE001
        return "", f"读取交互密码失败：{type(exc).__name__}"


class AskpassHelper:
    """临时 SSH_ASKPASS helper：脚本只含环境变量名，密码只进子进程 env 块。

    跨平台：POSIX 用 ``#!/bin/sh`` 启动器；Windows 用 ``.cmd`` 启动器。
    两者都只是 ``exec python helper.py``，由 helper.py 从随机环境变量读密码写 stdout——
    绕开 cmd 的元字符/延迟展开坑，任何密码字符都能原样传递。
    """

    def __init__(self, password: str, *, env_name: str | None = None):
        self.var = env_name or ("SC_ASKPASS_" + uuid.uuid4().hex[:12])
        self._password = password
        self._dir = Path(tempfile.mkdtemp(prefix="sc_askpass_"))
        self.path: Path | None = None
        try:
            self._build()
        except Exception:                                # noqa: BLE001
            self.cleanup()
            raise

    def _build(self) -> None:
        helper_py = self._dir / "_askpass.py"
        helper_py.write_text(
            "import os, sys\n"
            f"sys.stdout.write(os.environ.get({self.var!r}, ''))\n",
            encoding="ascii")
        py = sys.executable.replace("\\", "/")
        h = str(helper_py).replace("\\", "/")
        if os.name == "nt":
            launcher = self._dir / "_askpass.cmd"
            launcher.write_text(f'@echo off\r\n"{py}" "{h}"\r\n', encoding="utf-8")
            self.path = launcher
        else:
            launcher = self._dir / "_askpass.sh"
            launcher.write_text(f'#!/bin/sh\nexec "{py}" "{h}"\n', encoding="utf-8")
            launcher.chmod(0o700)
            self.path = launcher

    def env_patch(self) -> dict[str, str]:
        """只注入 ssh 子进程的环境；调用方在过滤白名单后 update 这份 patch。"""
        return {
            self.var: self._password,
            "SSH_ASKPASS": str(self.path or ""),
            "SSH_ASKPASS_REQUIRE": "force",   # Win11 / OpenSSH 8.4+ 有控制台也要走 helper
            "DISPLAY": os.environ.get("DISPLAY") or ":0",
        }

    def cleanup(self) -> None:
        """用完即焚：清内存引用 + 删整个临时目录。"""
        self._password = ""
        self.path = None
        shutil.rmtree(self._dir, ignore_errors=True)


def ssh_auth_options(auth: str) -> list[str]:
    """认证相关的 ssh 选项。密码认证绝不能带 BatchMode=yes（那会完全禁用密码）。"""
    if str(auth or "").strip().lower() == "password":
        return ["-o", "BatchMode=no",
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "PubkeyAuthentication=no",
                "-o", "NumberOfPasswordPrompts=1"]
    return ["-o", "BatchMode=yes"]


def describe() -> dict[str, Any]:
    """给自检 / UI 的能力说明。"""
    return {
        "auth_modes": ["agent", "key_ref", "password"],
        "credential_schemes": list(CRED_KINDS),
        "plaintext_credential_in_config_rejected": True,
        "password_never_in_argv": True,
        "password_never_in_audit": True,
        "password_never_in_public_view": True,
        "askpass_helper_ephemeral": True,
        "boundary": ("用户名/密码走凭据引用（env:/file:/prompt），明文只进 ssh 子进程 env，"
                     "经一次性 SSH_ASKPASS helper 传递，不落盘/不进 argv/审计/对外视图"),
    }
