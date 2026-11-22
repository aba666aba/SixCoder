"""声明式运维单元：把一组命令打包成一次协商 + 一次人审的受控运维动作。

## 为什么（2026-11-23，用户要"把部署运维从单条命令提升到编排"）

deploy.py 只能下发**单条**白名单命令。真实运维是成组的：
    拉代码 → 重启服务 → 健康检查 →（失败则回滚）
如果每步都单独协商+人审，人会审到麻木，最后要么乱点通过、要么干脆不用。
本模块把整组打包成**一个单元**：一次三方协商 ≥2 + 一次强制人审放行整组，
执行时按序跑，任一步失败按 on_fail 决定中止/继续，全组结束后做健康检查，
健康检查失败自动跑回滚命令。

## 红线（一条不拆，全部继承 deploy）
- 只连 deploy 里预留的目标；**每条命令（含回滚/健康检查）都要过该目标的白名单**；
- 三方协商 ≥2 + 强制人审（force=True，fail-closed），一次覆盖整组；
- 凭据按引用读取（agent / key_ref / password），密码只进 ssh 子进程 env，不落盘；
- 全程审计（每一步 rc/ok + 组结论），凭据不进审计/对外视图；
- 主沙箱仍禁 ssh（本模块走 deploy 的独立通道）。

## 配置
放在 settings.json 的 `ops` 段（与 deploy 平级）：
    ops.enabled / ops.units[]
每个 unit：id / target / steps[{command, reason, on_fail}] / health_check /
          rollback[command] / enabled / note
打包时整个 ops 段清空（同 deploy，见 build_installer._scrub_deploy）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from . import approval as _approval
from . import deploy as _deploy
from . import permissions as _permissions
from . import risk_consensus as _rc
from . import settings_store as _settings
from . import sshcred as _sshcred

OPS_DEFAULT: dict[str, Any] = {"enabled": False, "units": []}
UNIT_FIELDS = ("id", "target", "steps", "health_check", "rollback", "enabled", "note", "timeout")
_ON_FAIL = ("abort", "continue")


def config() -> dict[str, Any]:
    raw = (_settings.load().get("ops") or {})
    return {
        "enabled": bool(raw.get("enabled", False)),
        "units": [u for u in (raw.get("units") or []) if isinstance(u, dict)],
    }


def _write(cfg: dict[str, Any]) -> None:
    data = _settings.load()
    data["ops"] = {"enabled": bool(cfg.get("enabled", False)), "units": cfg.get("units") or []}
    _settings.save(data)


def _norm_steps(raw: Any) -> list[dict]:
    out: list[dict] = []
    for s in (raw or []):
        if not isinstance(s, dict):
            continue
        cmd = str(s.get("command") or "").strip()
        if not cmd:
            continue
        on_fail = str(s.get("on_fail") or "abort").strip().lower()
        out.append({
            "command": cmd[:800],
            "reason": str(s.get("reason") or "").strip()[:300],
            "on_fail": on_fail if on_fail in _ON_FAIL else "abort",
        })
    return out[:20]


def save_units(units: list[dict], enabled: bool | None = None) -> dict[str, Any]:
    """整表写回运维单元（UI/CLI 入口）。规范化 + 去重 id。"""
    cfg = config()
    clean: list[dict] = []
    seen: set[str] = set()
    for u in units or []:
        if not isinstance(u, dict):
            continue
        uid = str(u.get("id") or "").strip()[:32]
        if not uid or uid in seen:
            continue
        seen.add(uid)
        clean.append({
            "id": uid,
            "target": str(u.get("target") or "").strip()[:32],
            "steps": _norm_steps(u.get("steps")),
            "health_check": str(u.get("health_check") or "").strip()[:800],
            "rollback": [str(c).strip()[:800] for c in (u.get("rollback") or []) if str(c).strip()][:20],
            "enabled": bool(u.get("enabled", True)),
            "note": str(u.get("note") or "").strip()[:200],
            "timeout": min(600, max(5, int(u.get("timeout") or 120))),
        })
    cfg["units"] = clean
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    _write(cfg)
    return public_config()


def public_config() -> dict[str, Any]:
    """对外的视图：单元不含凭据，目标引用沿用 deploy 的脱敏视图。"""
    cfg = config()
    dep = _deploy.public_config()
    targets = {t.get("id"): t for t in dep.get("targets", [])}
    units = []
    for u in cfg["units"]:
        row = {k: u.get(k) for k in UNIT_FIELDS}
        t = targets.get(u.get("target")) or {}
        row["target_host"] = t.get("host", "")
        row["target_configured"] = bool(t)
        units.append(row)
    return {"enabled": cfg["enabled"], "units": units, "capability": describe()}


def find_unit(uid: str) -> dict[str, Any] | None:
    for u in config()["units"]:
        if str(u.get("id")) == str(uid):
            return u
    return None


def _validate_all(target: dict, unit: dict) -> tuple[bool, str]:
    """整组命令（步骤 + 健康检查 + 回滚）全部过白名单，一处不过整组拒。"""
    cmds = [s["command"] for s in unit.get("steps", [])]
    if unit.get("health_check"):
        cmds.append(unit["health_check"])
    cmds += list(unit.get("rollback") or [])
    if not cmds:
        return False, "运维单元没有任何命令"
    for c in cmds:
        ok, why = _deploy.check_command(target, c)
        if not ok:
            return False, f"命令 {c[:80]!r} 未过白名单：{why}"
    return True, ""


def _run_ssh(target: dict, command: str, *, ssh: str, helper: Any, ws: Path,
             timeout: int) -> dict[str, Any]:
    """执行单条命令（不含协商/人审，那些在整组层做一次）。复用 deploy 的构造。"""
    auth = str(target.get("auth") or "agent").strip().lower()
    argv = [ssh, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    argv += _sshcred.ssh_auth_options(auth)
    if auth == "key_ref":
        key = str(target.get("key_ref") or "").strip()
        if not Path(key).expanduser().exists():
            return {"ok": False, "rc": -1, "output": "", "why": "私钥文件不存在"}
        argv += ["-i", key]
    try:
        port = int(target.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    if 1 <= port <= 65535 and port != 22:
        argv += ["-p", str(port)]
    wd = str(target.get("workdir") or "").strip()
    remote = f"cd {_deploy._shquote(wd)} && {command}" if wd else command
    argv += [f"{target.get('user')}@{target.get('host')}", remote]

    env = {k: v for k, v in os.environ.items()
           if k in _deploy._SAFE_ENV_KEYS and not _deploy._SECRET_ENV.search(k)}
    if helper is not None:
        env.update(helper.env_patch())
    try:
        p = subprocess.run(argv, cwd=str(ws), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=max(5, int(timeout)), env=env)
        out = ((p.stdout or "") + (p.stderr or ""))[-2000:]
        return {"ok": p.returncode == 0, "rc": p.returncode, "output": out,
                "why": "ok" if p.returncode == 0 else f"rc={p.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "rc": -1, "output": "", "why": f"超时 {timeout}s"}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "rc": -1, "output": "", "why": f"{type(exc).__name__}: {exc}"}


def execute(unit_id: str, *, workspace: str | Path = "", emit=None,
            timeout: float = 600.0, require_consensus: bool = True,
            consensus_fn=None, secret_fn=None,
            interactive: bool | None = None) -> dict[str, Any]:
    """执行一个运维单元：一次协商 + 一次人审覆盖整组，按序执行 + 健康检查 + 失败回滚。"""
    ws = Path(workspace) if workspace else Path(".")
    result: dict[str, Any] = {
        "ok": False, "unit": str(unit_id or ""), "decision": "", "why": "",
        "consensus": None, "steps": [], "health": None, "rollback": [],
    }

    # 1) 总开关 + 权限
    if not config()["enabled"]:
        result["why"] = "运维通道未启用（ops.enabled=false）"
        return result
    try:
        _permissions.require("deploy.exec")
    except PermissionError as exc:
        result["why"] = str(exc)
        return result

    # 2) 单元存在 + 启用 + 目标合法
    unit = find_unit(unit_id)
    if not unit:
        result["why"] = f"没有运维单元 {unit_id!r}"
        return result
    if not unit.get("enabled", True):
        result["why"] = f"运维单元 {unit_id} 已禁用"
        return result
    target = _deploy.find_target(str(unit.get("target") or ""))
    if not target:
        result["why"] = f"单元引用的目标 {unit.get('target')!r} 不在 deploy 预留里"
        return result
    if not target.get("enabled", True):
        result["why"] = f"目标 {unit.get('target')} 已禁用"
        return result
    host, user = str(target.get("host") or ""), str(target.get("user") or "")
    if not _deploy._HOST_RE.match(host) or not _deploy._USER_RE.match(user):
        result["why"] = "目标的 host/user 非法"
        return result

    # 3) 整组命令白名单（一处不过整组拒）
    ok, why = _validate_all(target, unit)
    if not ok:
        result["why"] = why
        _deploy._audit(ws, {"action": "ops_denied", "unit": unit_id, "why": why[:200]})
        return result

    # 4) 一次三方协商（整组作为一个提案）
    if require_consensus:
        cmds = [s["command"] for s in unit["steps"]]
        proposal = {
            "kind": "ops",
            "target": f"{user}@{host}:{target.get('port') or 22}",
            "command": "; ".join(cmds)[:400] + (f" [回滚 {len(unit.get('rollback') or [])} 条]" if unit.get("rollback") else ""),
            "reason": f"运维单元 {unit_id}：" + (unit.get("note") or "")[:200],
            "risk": "成组运维动作（可能含重启/回滚），一次放行覆盖整组；凭据按引用读取，引擎不接触本体。",
            "guardrails": "整组每条命令都过白名单；一次协商+一次强制人审覆盖全组；失败自动回滚；全程审计；凭据不落盘。",
        }
        cons = _rc.decide(proposal, complete_fn=consensus_fn, workspace=str(ws))
        result["consensus"] = {"granted": cons["granted"], "votes": cons["votes"],
                               "tally": cons["tally"], "reason": cons["reason"]}
        if emit:
            emit("risk_consensus", {"votes": cons["votes"], "tally": cons["tally"],
                                    "granted": cons["granted"], "scope": "ops"})
        if not cons["granted"]:
            result["why"] = f"三方协商未通过（≥2 同意）：{cons['reason']}"
            _deploy._audit(ws, {"action": "ops_consensus_denied", "unit": unit_id,
                                "tally": cons["tally"]})
            return result

    # 5) 一次强制人审（整组）
    step_lines = "\n".join(f"  {i+1}. {s['command']}  ({s['on_fail']})"
                           for i, s in enumerate(unit["steps"]))
    ap = _approval.request(
        node="deploy.exec",
        title=f"运维单元：{unit_id} ({user}@{host})",
        detail=(f"整组命令（一次放行覆盖全部）：\n{step_lines}\n"
                f"健康检查：{unit.get('health_check') or '(无)'}\n"
                f"失败回滚：{', '.join(unit.get('rollback') or []) or '(无)'}\n"
                f"原因：{(unit.get('note') or '(无)')[:300]}"),
        workspace=str(ws), emit=emit, interactive=interactive, force=True,
        timeout=float(timeout))
    result["decision"] = ap.get("decision", "")
    if not ap.get("ok"):
        result["why"] = f"人审未通过：{ap.get('reason')}"
        _deploy._audit(ws, {"action": "ops_rejected", "unit": unit_id,
                            "decision": ap.get("decision")})
        return result

    # 6) 找 ssh + 凭据（人审通过后才取）
    ssh = _deploy._find_ssh()
    if not ssh:
        result["why"] = "找不到系统 ssh.exe"
        return result
    auth = str(target.get("auth") or "agent").strip().lower()
    helper: Any = None
    try:
        if auth == "password":
            pwd, perr = _sshcred.resolve_password(target.get("credential"),
                                                  secret_fn=secret_fn, interactive=interactive)
            if not pwd:
                result["why"] = f"密码凭据不可用：{perr}"
                return result
            helper = _sshcred.AskpassHelper(pwd)
        per_step_timeout = int(unit.get("timeout") or 120)

        # 7) 按序执行步骤
        all_ok = True
        for s in unit["steps"]:
            r = _run_ssh(target, s["command"], ssh=ssh, helper=helper, ws=ws,
                         timeout=per_step_timeout)
            result["steps"].append({"command": s["command"], **r})
            _deploy._audit(ws, {"action": "ops_step", "unit": unit_id,
                                "command": s["command"][:200], "rc": r["rc"], "ok": r["ok"]})
            if not r["ok"]:
                all_ok = False
                if s["on_fail"] == "abort":
                    break

        # 8) 健康检查（全步骤成功才做）
        if all_ok and unit.get("health_check"):
            h = _run_ssh(target, unit["health_check"], ssh=ssh, helper=helper, ws=ws,
                         timeout=per_step_timeout)
            result["health"] = {"command": unit["health_check"], **h}
            _deploy._audit(ws, {"action": "ops_health", "unit": unit_id, "ok": h["ok"]})
            all_ok = h["ok"]

        # 9) 失败回滚（任一步失败或健康检查失败）
        if not all_ok and unit.get("rollback"):
            for cmd in unit["rollback"]:
                r = _run_ssh(target, cmd, ssh=ssh, helper=helper, ws=ws,
                             timeout=per_step_timeout)
                result["rollback"].append({"command": cmd, **r})
                _deploy._audit(ws, {"action": "ops_rollback", "unit": unit_id,
                                    "command": cmd[:200], "ok": r["ok"]})

        result["ok"] = all_ok
        result["why"] = ("运维单元执行成功" if all_ok else
                         "运维单元失败（已按配置回滚，见 rollback）" if result["rollback"] else
                         "运维单元失败（未配置回滚）")
    finally:
        if helper is not None:
            helper.cleanup()

    _deploy._audit(ws, {"action": "ops_exec", "unit": unit_id, "decision": ap.get("decision"),
                        "ok": result["ok"], "health_ok": bool((result.get("health") or {}).get("ok"))})
    return result


def describe() -> dict[str, Any]:
    return {
        "enabled": config()["enabled"],
        "quorum": _rc.APPROVE_QUORUM,
        "human_approval_forced": True,
        "approval_scope": "unit",   # 一次协商+一次人审覆盖整组，不是每条
        "rollback_on_fail": True,
        "health_check": True,
        "fail_closed": True,
        "boundary": ("声明式运维单元：整组命令共用一次三方协商≥2+一次强制人审；"
                     "每条命令仍过白名单，失败自动回滚，全程审计，凭据不落盘"),
    }
