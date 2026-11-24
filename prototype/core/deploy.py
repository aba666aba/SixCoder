"""受控部署通道：预留服务器信息 + 独立命令行执行 + 强制人审 + 全程审计。

## 为什么要有这个模块（2026-11-23，用户提问："提前预留信息，给命令行操控权限，
## 远端可以自己通过命令行 ssh 连接服务器等"）

前几轮把"线上部署/运维"判成结构性边界，理由是：环境进不去、判据拿不到、凭据不能碰。
用户提出的解法是对的——**用"人工预先授权 + 预留目标信息"把边界变成受控通道**：
人先把服务器信息（host/user/认证方式）填进来、把命令白名单定好，远端就只能在这个
笼子里操作。这不是拆掉安全模型，是**在安全模型之内开一个显式授权的口子**。

## 三条不可动摇的设计红线

1. **不碰主沙箱的 DENY_PATTERNS**。主沙箱明确禁止 ssh/scp/sftp（防外联、防数据外泄），
   那是对普通工具循环的护栏，绝不为了这个功能去放开。本模块是**独立通道**，
   直连系统 ssh.exe，但只连"预先登记在册 + 人审放行"的目标。
2. **绝不落盘凭据**（2026-11-23 扩成通用认证：用户名/密码也能用）。沿用 gitops 红线，
   配置里**只存凭据引用、永不存凭据本体**：
   · `agent`：走本机 SSH agent / 默认密钥，引擎不碰私钥；
   · `key_ref`：**私钥路径引用**（工作区外、用户自管的文件），引擎只读不写、不回显；
   · `password`：**用户名+密码**认证（现实里大量服务器只给密码）。密码用 `credential`
     引用提供：`env:VAR` / `file:PATH` / `prompt`，明文一律拒绝写进配置。执行时经
     一次性 SSH_ASKPASS helper 只注入 ssh 子进程（见 core/sshcred.py），不落盘、
     不进 argv / 审计 / 对外视图。裸写的明文密码在 save_targets 阶段就被丢弃。
3. **每次执行强制人审，fail-closed**。即使是造物模式（auto_approve=True）也要审——
   外联到生产服务器是高危操作，不因为"甲方要全自动"就放行。超时/没人答 = 拒绝。
   （见 approval.request 的 force=True 参数。）

## 命令白名单（防提示注入 / 防远端乱来）

- 每个目标自带 `allow_commands`（前缀白名单）+ `deny_commands`（可选）。
- 远端给的命令必须**整条命中某条 allow 前缀**、且**不含元字符**（; & | ` $() 换行 等），
  否则拒绝。这样即使远端被提示注入，也只能跑人预先核准的那几类命令。
- 每次执行写 `.sixcoder/audit.jsonl`：谁、连哪台、跑什么、人审结果、rc；**不含凭据**。

## 配置存储

放在 settings.json 的 `deploy` 段（与 remote 平级），复用 settings_store 的脱敏 +
镜像 + 打包模板机制。要点：
- `deploy.enabled` 默认 False（总开关，人不开就是死的）；
- `deploy.targets[]` 每个目标：id / host / port / user / auth / key_ref / allow_commands /
  workdir / note。对外视图（给远端/UI）**剥掉 key_ref 明文**，只给掩码 + 是否已配置。
- 打包模板清空整个 deploy 段（见 packaging/build_installer.py 的 _scrub_deploy）。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import approval as _approval
from . import permissions as _permissions
from . import settings_store as _settings
from . import sshcred as _sshcred

# ---------------------------------------------------------------- 配置

DEPLOY_DEFAULT: dict[str, Any] = {
    "enabled": False,          # 总开关：人不开，整条通道是死的
    "targets": [],             # 预留的服务器列表
}

TARGET_FIELDS = ("id", "host", "port", "user", "auth", "key_ref", "credential",
                 "allow_commands", "deny_commands", "workdir", "note", "enabled")

# 命令注入防护：这些元字符一旦出现就拒（分号/管道/反引号/命令替换/重定向/换行）
_SHELL_META = re.compile(r"[;&|`$><\n\r\\]|\$\(|&&|\|\|")
# host / user 只允许安全字符（防 argv 注入；虽然用列表传参不经过 shell，仍然收紧）
_HOST_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,253}$")
# 用户名要通用：允许 @（邮箱式登录名）与 \（DOMAIN\user），仍禁 shell 元字符与空格。
_USER_RE = re.compile(r"^[A-Za-z0-9._@\\\-]{1,128}$")
_AUTH_MODES = ("agent", "key_ref", "password")


def config() -> dict[str, Any]:
    """读 deploy 配置（每次重新读，改设置不用重启）。"""
    data = _settings.load()
    raw = data.get("deploy") or {}
    return {
        "enabled": bool(raw.get("enabled", False)),
        "targets": [t for t in (raw.get("targets") or []) if isinstance(t, dict)],
    }


def _write_config(cfg: dict[str, Any]) -> None:
    data = _settings.load()
    data["deploy"] = {
        "enabled": bool(cfg.get("enabled", False)),
        "targets": cfg.get("targets") or [],
    }
    _settings.save(data)


def save_targets(targets: list[dict], enabled: bool | None = None) -> dict[str, Any]:
    """整表写回部署目标（UI/CLI 的入口）。规范化每个目标，返回脱敏后的表。"""
    cfg = config()
    clean: list[dict] = []
    seen: set[str] = set()
    for t in targets or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()[:32]
        if not tid or tid in seen:
            continue
        seen.add(tid)
        allow = [str(x).strip() for x in (t.get("allow_commands") or []) if str(x).strip()][:40]
        deny = [str(x).strip() for x in (t.get("deny_commands") or []) if str(x).strip()][:40]
        auth = str(t.get("auth") or "agent").strip().lower()
        if auth not in _AUTH_MODES:
            auth = "agent"
        clean.append({
            "id": tid,
            "host": str(t.get("host") or "").strip()[:253],
            "port": int(t.get("port") or 22) if str(t.get("port") or "22").isdigit() else 22,
            "user": str(t.get("user") or "").strip()[:128],
            "auth": auth,
            "key_ref": str(t.get("key_ref") or "").strip()[:512],
            # password 凭据只存引用（env:/file:/prompt）；明文裸值被归一化成空串丢弃
            "credential": _sshcred.normalize_credential(t.get("credential")),
            "allow_commands": allow,
            "deny_commands": deny,
            "workdir": str(t.get("workdir") or "").strip()[:512],
            "note": str(t.get("note") or "").strip()[:200],
            "enabled": bool(t.get("enabled", True)),
        })
    cfg["targets"] = clean
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    _write_config(cfg)
    return public_config()


def public_config(reveal: bool = False) -> dict[str, Any]:
    """给 UI / 远端的视图：默认剥掉 key_ref / credential 明文（只给 bool + 掩码）。

    credential 是凭据**引用**（env:/file:/prompt），不是密码本体；仍按同款纪律脱敏，
    绝不把 file 路径 / 环境变量名回给远端视图（reveal 仅供本机回环接口）。
    """
    cfg = config()
    targets = []
    for t in cfg["targets"]:
        row = {k: t.get(k) for k in TARGET_FIELDS if k not in ("key_ref", "credential")}
        keyref = str(t.get("key_ref") or "")
        row["key_ref_configured"] = bool(keyref)
        row["key_ref_masked"] = _settings.mask(keyref)
        cd = _sshcred.describe_credential(t.get("credential"), reveal=reveal)
        row["credential_kind"] = cd["kind"]
        row["credential_configured"] = cd["configured"]
        row["credential_masked"] = cd["masked"]
        if reveal:
            row["key_ref"] = keyref
            row["credential"] = t.get("credential") or ""
        targets.append(row)
    return {"enabled": cfg["enabled"], "targets": targets}


def find_target(target_id: str) -> dict[str, Any] | None:
    for t in config()["targets"]:
        if str(t.get("id")) == str(target_id):
            return t
    return None


# ---------------------------------------------------------------- 命令白名单

def check_command(target: dict[str, Any], command: str) -> tuple[bool, str]:
    """校验命令是否在该目标的白名单内且无注入元字符。返回 (ok, 原因)。"""
    cmd = str(command or "").strip()
    if not cmd:
        return False, "命令为空"
    if len(cmd) > 800:
        return False, "命令过长（>800 字符）"
    if _SHELL_META.search(cmd):
        return False, "命令含 shell 元字符（; & | ` $() > < 换行等），一律拒绝（防注入）"
    deny = [str(x) for x in (target.get("deny_commands") or [])]
    for d in deny:
        if d and cmd.startswith(d):
            return False, f"命令命中拒绝前缀：{d}"
    allow = [str(x) for x in (target.get("allow_commands") or [])]
    if not allow:
        return False, "该目标未配置任何 allow_commands（无人核准过任何命令）"
    for a in allow:
        if a and cmd.startswith(a.rstrip("*")):
            return True, "命中白名单"
    return False, f"命令不在白名单内。允许的前缀：{allow[:8]}"


# ---------------------------------------------------------------- 执行

def _find_ssh() -> str | None:
    """找系统 ssh 客户端：优先 PATH，再查 Windows/OpenSSH 常见位置。"""
    p = shutil.which("ssh")
    if p:
        return p
    for c in ("C:/Windows/System32/OpenSSH/ssh.exe",
              "C:/Program Files/OpenSSH/ssh.exe", "/usr/bin/ssh"):
        if Path(c).exists():
            return c
    return None


def _audit(ws: Path, entry: dict) -> None:
    """写审计日志（不含凭据）。"""
    if not ws:
        return
    try:
        d = Path(ws) / ".sixcoder"
        d.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        with (d / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(__import__("json").dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                    # noqa: BLE001
        pass


def execute(target_id: str, command: str, *, reason: str = "",
            workspace: str | Path = "", emit=None, timeout: int = 120,
            approval_timeout: float = 600.0,
            require_consensus: bool = True,
            consensus_fn=None,
            secret_fn=None,
            interactive: bool | None = None) -> dict[str, Any]:
    """在预留目标上执行一条白名单命令（三方协商 ≥2 + 强制人审 + 全程审计）。

    返回 {ok, rc, output, target, command, decision, why, consensus}，失败给可读原因，
    绝不抛异常。凭据按认证方式从本机读取：agent / key_ref（私钥路径引用）/ password
    （env:VAR / file:PATH / prompt 引用），引擎不写盘、不回显、不进 argv 与审计。

    `require_consensus`（默认 True）：执行前先过三方风险协商门（≥2 同意才继续，
    见 core/risk_consensus.py）——这是用户定稿："风险操作要协商 ≥2 同意"。
    `consensus_fn`：注入协商提问函数（自检用，避免真调远端）。
    `secret_fn`：注入密码提供函数（() -> str，自检/Web 一次性输入用）；对应
      credential=prompt，绝不影响 env:/file: 两种引用。
    `approval_timeout`：等人审秒数（默认 600，fail-closed：超时=拒绝）。
    """
    ws = Path(workspace) if workspace else Path(".")
    result: dict[str, Any] = {"ok": False, "rc": -1, "output": "",
                              "target": str(target_id or ""), "command": str(command or ""),
                              "decision": "", "why": "", "consensus": None}

    # 1) 总开关 + 权限档位
    cfg = config()
    if not cfg["enabled"]:
        result["why"] = "部署通道未启用：请先在设置里打开 deploy.enabled 并人审预留目标"
        return result
    try:
        _permissions.require("deploy.exec")
    except PermissionError as exc:
        result["why"] = str(exc)
        return result

    # 2) 目标存在 + 启用 + 字段合法
    t = find_target(target_id)
    if not t:
        result["why"] = f"没有预留目标 {target_id!r}（只能连人预先登记在册的服务器）"
        return result
    if not t.get("enabled", True):
        result["why"] = f"目标 {target_id} 已被禁用"
        return result
    host, user = str(t.get("host") or ""), str(t.get("user") or "")
    if not _HOST_RE.match(host):
        result["why"] = "目标 host 为空或含非法字符"
        return result
    if not _USER_RE.match(user):
        result["why"] = "目标 user 为空或含非法字符"
        return result

    # 3) 命令白名单（防注入）
    ok, why = check_command(t, command)
    if not ok:
        result["why"] = why
        _audit(ws, {"action": "deploy_denied", "target": target_id,
                    "command": str(command)[:200], "why": why})
        return result

    # 4) 三方风险协商（用户定稿：风险操作 ≥2 同意即可；协商不过连人审都不惊动）
    if require_consensus:
        from . import risk_consensus as _rc
        proposal = {
            "kind": "deploy",
            "target": f"{user}@{host}:{t.get('port') or 22}",
            "command": command,
            "reason": reason,
            "risk": ("外联到预留服务器执行命令；可能有生产副作用。"
                     "认证凭据由本机按引用读取（agent / 私钥路径 / 密码引用），引擎不接触凭据本体。"),
            "guardrails": ("目标须预先登记+启用；命令整条命中 allow_commands 白名单且"
                           "无 shell 元字符；强制人审；全程审计；凭据明文不落盘、不进 argv/审计。"),
        }
        cons = _rc.decide(proposal, complete_fn=consensus_fn, workspace=str(ws))
        result["consensus"] = {"granted": cons["granted"], "votes": cons["votes"],
                               "tally": cons["tally"], "reason": cons["reason"]}
        if emit:
            emit("risk_consensus", {"votes": cons["votes"], "tally": cons["tally"],
                                    "granted": cons["granted"]})
        if not cons["granted"]:
            result["why"] = f"三方协商未通过（≥2 同意才能执行）：{cons['reason']}"
            _audit(ws, {"action": "deploy_consensus_denied", "target": target_id,
                        "command": command[:200], "tally": cons["tally"]})
            return result

    # 5) 强制人审（即使造物模式 auto_approve=True 也要审；fail-closed）
    ap = _approval.request(
        node="deploy.exec",
        title=f"部署命令：{target_id} ({user}@{host})",
        detail=(f"命令：{command}\n原因：{reason or '(远端未给原因)'}\n"
                f"工作目录：{t.get('workdir') or '(远端默认目录)'}\n"
                f"认证：{t.get('auth')}（凭据由本机读取，不显示）"),
        workspace=str(ws), emit=emit, interactive=interactive, force=True,
        timeout=float(approval_timeout))
    result["decision"] = ap.get("decision", "")
    if not ap.get("ok"):
        result["why"] = f"人审未通过：{ap.get('reason')}"
        _audit(ws, {"action": "deploy_rejected", "target": target_id,
                    "command": command[:200], "decision": ap.get("decision")})
        return result

    # 6) 找 ssh + 认证（三种方式：agent / key_ref / password；凭据永不落盘）
    ssh = _find_ssh()
    if not ssh:
        result["why"] = "找不到系统 ssh.exe（Windows 需启用 OpenSSH 客户端，或装 Git for Windows）"
        return result
    auth = str(t.get("auth") or "agent").strip().lower()
    argv = [ssh, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    argv += _sshcred.ssh_auth_options(auth)   # password 不带 BatchMode=yes（否则密码被禁用）
    try:
        port = int(t.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    if 1 <= port <= 65535 and port != 22:
        argv += ["-p", str(port)]

    helper: _sshcred.AskpassHelper | None = None
    try:
        if auth == "key_ref":
            key = str(t.get("key_ref") or "").strip()
            if not key:
                result["why"] = "目标认证方式=key_ref 但没配私钥路径引用"
                return result
            if not Path(key).expanduser().exists():
                result["why"] = "私钥引用指向的文件不存在（请在本机放好私钥，引擎不会写它）"
                return result
            argv += ["-i", key]
        elif auth == "password":
            # 凭据在人审通过后才读取（fail-closed）；明文只在内存 + 子进程 env。
            pwd, perr = _sshcred.resolve_password(
                t.get("credential"), secret_fn=secret_fn, interactive=interactive)
            if not pwd:
                result["why"] = f"密码凭据不可用：{perr}"
                _audit(ws, {"action": "deploy_credential_failed", "target": target_id,
                            "kind": _sshcred.credential_kind(t.get("credential"))})
                return result
            helper = _sshcred.AskpassHelper(pwd)

        # 远端工作目录：command 已过白名单+元字符校验，workdir 是本地预留的固定串
        wd = str(t.get("workdir") or "").strip()
        remote_cmd = f"cd {_shquote(wd)} && {command}" if wd else command
        argv += [f"{user}@{host}", remote_cmd]

        # 7) 脱敏环境 + 执行（列表传参，不经本地 shell）
        env = {k: v for k, v in os.environ.items()
               if k in _SAFE_ENV_KEYS and not _SECRET_ENV.search(k)}
        if helper is not None:
            env.update(helper.env_patch())   # 密码只进 ssh 子进程 env，不进 argv
        try:
            p = subprocess.run(argv, cwd=str(ws), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=max(5, int(timeout)),
                               env=env)
            out = ((p.stdout or "") + (p.stderr or ""))[-6000:]
            result.update({"rc": p.returncode, "output": out,
                           "ok": p.returncode == 0,
                           "why": "执行成功" if p.returncode == 0 else f"ssh 返回码 {p.returncode}"})
        except subprocess.TimeoutExpired:
            result["why"] = f"执行超时（{timeout}s）"
        except FileNotFoundError:
            result["why"] = "ssh 进程启动失败（路径不存在）"
        except Exception as exc:                          # noqa: BLE001
            result["why"] = f"执行异常：{type(exc).__name__}: {exc}"
    finally:
        if helper is not None:
            helper.cleanup()   # 用完即焚：临时脚本 + 内存里的密码

    _audit(ws, {"action": "deploy_exec", "target": target_id, "command": command[:200],
                "decision": ap.get("decision"), "auth": auth,
                "rc": result["rc"], "ok": result["ok"]})
    return result


def _shquote(s: str) -> str:
    """给远端 shell 的路径加单引号（workdir 是本地预留固定串，仍收紧）。"""
    return "'" + str(s).replace("'", "'\\''") + "'"


_SAFE_ENV_KEYS = {"PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP",
                  "COMSPEC", "USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "LANG", "TERM",
                  "SSH_AUTH_SOCK", "USER", "LOGNAME"}
_SECRET_ENV = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API)", re.I)


def describe() -> dict[str, Any]:
    """给自检/UI 的能力说明。"""
    try:
        from . import risk_consensus as _rc
        consensus = _rc.describe()
    except Exception:                                    # noqa: BLE001
        consensus = {"quorum": 2, "ready": {}, "fail_closed": True}
    cred = _sshcred.describe()
    return {
        "enabled": config()["enabled"],
        "auth_modes": list(_AUTH_MODES),
        "password_auth_supported": True,
        "credential_schemes": cred["credential_schemes"],
        "main_sandbox_ssh_still_denied": True,
        "consensus_required": True,
        "consensus": consensus,
        "human_approval_forced": True,
        "fail_closed": True,
        "credential_persisted": False,   # 配置里只有引用，无凭据本体
        "password_never_in_argv": cred["password_never_in_argv"],
        "password_never_in_audit": cred["password_never_in_audit"],
        "ssh_found": bool(_find_ssh()),
        "boundary": ("独立受控通道：只连预留目标、只跑白名单命令、三方协商≥2+强制人审、"
                     "全程审计；不碰主沙箱、不落凭据。支持用户名+密码，但密码只按引用读取、"
                     "经一次性 SSH_ASKPASS 注入子进程，不进 argv/审计/对外视图"),
    }
