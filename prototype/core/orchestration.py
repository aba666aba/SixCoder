"""多机编排：一次协商 + 一次人审，把同一组命令下发到多台预留服务器。

## 为什么（2026-11-23，甜区收尾：部署运维 80 的最后一块）

ops.py 的运维单元是**单机**的：一个 unit 绑一个 target。真实运维常是同一步骤
打多台（重启整个集群的节点、给所有机器拉同一版代码）。逐台各走一次协商+人审，
人会审到麻木；逐台各调 ops.execute 又失去"整批一致"的语义。
本模块做**多机编排计划**：一个 plan 绑定多个 target，共用一组步骤，
**一次三方协商≥2 + 一次强制人审**放行整批，按序/并发下发，单机失败隔离
（一台挂不拖垮其它），最后汇总 per-host 结果。

## 红线（一条不拆，全部继承 deploy/ops）
- 只连 deploy 里预留且启用的目标；**每条命令（含回滚/健康检查）都要过每个目标自己的白名单**；
- 一次三方协商≥2 + 一次强制人审（force=True，fail-closed）覆盖整批；
- 凭据按引用读取（agent / key_ref / password），密码只进 ssh 子进程 env，不落盘；
  每台各自的凭据独立解析、独立 helper、用完即焚；
- 全程审计（每台每步 rc/ok + 批次结论），凭据不进审计/对外视图；
- 主沙箱仍禁 ssh（走 deploy 的独立通道）。

## 配置
settings.json 的 `orchestration` 段（与 deploy/ops 平级）：
    orchestration.enabled / orchestration.plans[]
每个 plan：id / targets[目标id] / steps[{command, reason, on_fail}] /
          health_check / rollback[command] / strategy(sequential|parallel) /
          fail_fast / enabled / note / timeout
打包时整个 orchestration 段清空（同 deploy/ops，见 build_installer._scrub_deploy）。
"""
from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from . import approval as _approval
from . import deploy as _deploy
from . import permissions as _permissions
from . import risk_consensus as _rc
from . import settings_store as _settings
from . import sshcred as _sshcred

ORCH_DEFAULT: dict[str, Any] = {"enabled": False, "plans": []}
PLAN_FIELDS = ("id", "targets", "steps", "health_check", "rollback", "strategy",
               "fail_fast", "enabled", "note", "timeout")
_ON_FAIL = ("abort", "continue")
_STRATEGIES = ("sequential", "parallel")


def config() -> dict[str, Any]:
    raw = (_settings.load().get("orchestration") or {})
    return {
        "enabled": bool(raw.get("enabled", False)),
        "plans": [p for p in (raw.get("plans") or []) if isinstance(p, dict)],
    }


def _write(cfg: dict[str, Any]) -> None:
    data = _settings.load()
    data["orchestration"] = {
        "enabled": bool(cfg.get("enabled", False)),
        "plans": cfg.get("plans") or [],
    }
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


def save_plans(plans: list[dict], enabled: bool | None = None) -> dict[str, Any]:
    """整表写回编排计划（UI/CLI 入口）。规范化 + 去重 id。"""
    cfg = config()
    clean: list[dict] = []
    seen: set[str] = set()
    for p in plans or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()[:32]
        if not pid or pid in seen:
            continue
        seen.add(pid)
        strategy = str(p.get("strategy") or "sequential").strip().lower()
        targets = [str(t).strip()[:32] for t in (p.get("targets") or []) if str(t).strip()]
        clean.append({
            "id": pid,
            "targets": list(dict.fromkeys(targets))[:32],   # 去重、保序、限 32 台
            "steps": _norm_steps(p.get("steps")),
            "health_check": str(p.get("health_check") or "").strip()[:800],
            "rollback": [str(c).strip()[:800] for c in (p.get("rollback") or []) if str(c).strip()][:20],
            "strategy": strategy if strategy in _STRATEGIES else "sequential",
            "fail_fast": bool(p.get("fail_fast", False)),
            "enabled": bool(p.get("enabled", True)),
            "note": str(p.get("note") or "").strip()[:200],
            "timeout": min(600, max(5, int(p.get("timeout") or 120))),
        })
    cfg["plans"] = clean
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    _write(cfg)
    return public_config()


def public_config() -> dict[str, Any]:
    """对外的视图：计划不含凭据，目标引用沿用 deploy 的脱敏视图。"""
    cfg = config()
    dep = _deploy.public_config()
    targets = {t.get("id"): t for t in dep.get("targets", [])}
    plans = []
    for p in cfg["plans"]:
        row = {k: p.get(k) for k in PLAN_FIELDS}
        hosts = []
        for tid in p.get("targets", []):
            t = targets.get(tid) or {}
            hosts.append({"id": tid, "host": t.get("host", ""), "configured": bool(t),
                          "enabled": t.get("enabled", True)})
        row["host_details"] = hosts
        plans.append(row)
    return {"enabled": cfg["enabled"], "plans": plans, "capability": describe()}


def find_plan(pid: str) -> dict[str, Any] | None:
    for p in config()["plans"]:
        if str(p.get("id")) == str(pid):
            return p
    return None


def _validate_plan(plan: dict, targets: list[dict]) -> tuple[bool, str]:
    """每条命令（步骤 + 健康检查 + 回滚）对每个目标都过白名单，一处不过整批拒。"""
    cmds = [s["command"] for s in plan.get("steps", [])]
    if plan.get("health_check"):
        cmds.append(plan["health_check"])
    cmds += list(plan.get("rollback") or [])
    if not cmds:
        return False, "编排计划没有任何命令"
    if not targets:
        return False, "编排计划没有目标主机"
    for t in targets:
        tid = t.get("id")
        for c in cmds:
            ok, why = _deploy.check_command(t, c)
            if not ok:
                return False, f"目标 {tid} 的命令 {c[:60]!r} 未过白名单：{why}"
    return True, ""


def _make_helper(target: dict, *, secret_fn, interactive) -> tuple[Any, str]:
    """按目标认证准备一次性凭据 helper；agent/key_ref 返回 None。"""
    auth = str(target.get("auth") or "agent").strip().lower()
    if auth != "password":
        return None, ""
    pwd, perr = _sshcred.resolve_password(target.get("credential"),
                                          secret_fn=secret_fn, interactive=interactive)
    if not pwd:
        return None, perr
    return _sshcred.AskpassHelper(pwd), ""


def _run_host(target: dict, plan: dict, *, ssh: str, ws: Path,
              timeout: int, secret_fn, interactive) -> dict[str, Any]:
    """单机完整流程：steps → health → rollback。一台的失败只记在自己结果里。"""
    tid = str(target.get("id") or "")
    row: dict[str, Any] = {"target": tid, "host": str(target.get("host") or ""),
                           "ok": False, "steps": [], "health": None, "rollback": [], "why": ""}
    helper, herr = _make_helper(target, secret_fn=secret_fn, interactive=interactive)
    if herr:
        row["why"] = f"密码凭据不可用：{herr}"
        return row
    auth = str(target.get("auth") or "agent").strip().lower()
    argv_base = [ssh, "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=15"]
    argv_base += _sshcred.ssh_auth_options(auth)
    if auth == "key_ref":
        key = str(target.get("key_ref") or "").strip()
        if not Path(key).expanduser().exists():
            row["why"] = "私钥文件不存在"
            return row
        argv_base += ["-i", key]
    try:
        port = int(target.get("port") or 22)
    except (TypeError, ValueError):
        port = 22
    if 1 <= port <= 65535 and port != 22:
        argv_base += ["-p", str(port)]
    wd = str(target.get("workdir") or "").strip()
    tip = f"{target.get('user')}@{target.get('host')}"

    def one(command: str) -> dict[str, Any]:
        argv = list(argv_base)
        remote = f"cd {_deploy._shquote(wd)} && {command}" if wd else command
        argv += [tip, remote]
        env = {k: v for k, v in os.environ.items()
               if k in _deploy._SAFE_ENV_KEYS and not _deploy._SECRET_ENV.search(k)}
        if helper is not None:
            env.update(helper.env_patch())
        try:
            p = subprocess.run(argv, cwd=str(ws), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=max(5, int(timeout)), env=env)
            out = ((p.stdout or "") + (p.stderr or ""))[-1500:]
            return {"ok": p.returncode == 0, "rc": p.returncode, "output": out,
                    "why": "ok" if p.returncode == 0 else f"rc={p.returncode}"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "rc": -1, "output": "", "why": f"超时 {timeout}s"}
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "rc": -1, "output": "", "why": f"{type(exc).__name__}: {exc}"}

    try:
        all_ok = True
        for s in plan["steps"]:
            r = one(s["command"])
            row["steps"].append({"command": s["command"], "target": tid, **r})
            if not r["ok"]:
                all_ok = False
                if s["on_fail"] == "abort":
                    break
        if all_ok and plan.get("health_check"):
            h = one(plan["health_check"])
            row["health"] = {"command": plan["health_check"], "target": tid, **h}
            all_ok = h["ok"]
        if not all_ok and plan.get("rollback"):
            for cmd in plan["rollback"]:
                r = one(cmd)
                row["rollback"].append({"command": cmd, "target": tid, **r})
        row["ok"] = all_ok
        row["why"] = ("主机执行成功" if all_ok else
                      "主机失败（已按配置回滚）" if row["rollback"] else "主机失败（未配置回滚）")
    finally:
        if helper is not None:
            helper.cleanup()
    return row


def execute(plan_id: str, *, workspace: str | Path = "", emit=None,
            timeout: float = 600.0, require_consensus: bool = True,
            consensus_fn=None, secret_fn=None,
            interactive: bool | None = None) -> dict[str, Any]:
    """执行一个编排计划：一次协商 + 一次人审覆盖所有主机，按策略下发，失败隔离。"""
    ws = Path(workspace) if workspace else Path(".")
    result: dict[str, Any] = {
        "ok": False, "plan": str(plan_id or ""), "decision": "", "why": "",
        "consensus": None, "strategy": "", "hosts": [],
    }

    # 1) 总开关 + 权限
    if not config()["enabled"]:
        result["why"] = "编排通道未启用（orchestration.enabled=false）"
        return result
    try:
        _permissions.require("deploy.exec")
    except PermissionError as exc:
        result["why"] = str(exc)
        return result

    # 2) 计划存在 + 启用 + 目标合法
    plan = find_plan(plan_id)
    if not plan:
        result["why"] = f"没有编排计划 {plan_id!r}"
        return result
    if not plan.get("enabled", True):
        result["why"] = f"编排计划 {plan_id} 已禁用"
        return result
    targets: list[dict] = []
    for tid in plan.get("targets", []):
        t = _deploy.find_target(str(tid))
        if not t:
            result["why"] = f"计划引用目标 {tid!r} 不在 deploy 预留里"
            return result
        if not t.get("enabled", True):
            result["why"] = f"目标 {tid} 已禁用"
            return result
        host, user = str(t.get("host") or ""), str(t.get("user") or "")
        if not _deploy._HOST_RE.match(host) or not _deploy._USER_RE.match(user):
            result["why"] = f"目标 {tid} 的 host/user 非法"
            return result
        targets.append(t)

    # 3) 每条命令对每个目标都过白名单
    ok, why = _validate_plan(plan, targets)
    if not ok:
        result["why"] = why
        _deploy._audit(ws, {"action": "orch_denied", "plan": plan_id, "why": why[:200]})
        return result

    strategy = str(plan.get("strategy") or "sequential")
    result["strategy"] = strategy
    host_list = ", ".join(f"{t.get('user')}@{t.get('host')}" for t in targets)

    # 4) 一次三方协商（整批作为一个提案）
    if require_consensus:
        cmds = [s["command"] for s in plan["steps"]]
        proposal = {
            "kind": "orchestration",
            "target": host_list[:300],
            "command": "; ".join(cmds)[:400] + f" → {len(targets)} 台主机",
            "reason": f"编排计划 {plan_id}：{(plan.get('note') or '')[:200]}",
            "risk": (f"成组命令下发到 {len(targets)} 台服务器（{strategy} 策略，"
                     f"{'首台失败即停' if plan.get('fail_fast') else '单机失败隔离'}）；"
                     "一次放行覆盖整批；凭据按引用读取，引擎不接触本体。"),
            "guardrails": ("每台每条命令都过各自目标白名单；一次协商+一次强制人审覆盖整批；"
                           "单机失败隔离不拖垮其它；失败自动回滚；全程审计；凭据不落盘。"),
        }
        cons = _rc.decide(proposal, complete_fn=consensus_fn, workspace=str(ws))
        result["consensus"] = {"granted": cons["granted"], "votes": cons["votes"],
                               "tally": cons["tally"], "reason": cons["reason"]}
        if emit:
            emit("risk_consensus", {"votes": cons["votes"], "tally": cons["tally"],
                                    "granted": cons["granted"], "scope": "orchestration"})
        if not cons["granted"]:
            result["why"] = f"三方协商未通过（≥2 同意）：{cons['reason']}"
            _deploy._audit(ws, {"action": "orch_consensus_denied", "plan": plan_id,
                                "tally": cons["tally"]})
            return result

    # 5) 一次强制人审（整批）
    step_lines = "\n".join(f"  {i+1}. {s['command']}  ({s['on_fail']})"
                           for i, s in enumerate(plan["steps"]))
    ap = _approval.request(
        node="deploy.exec",
        title=f"编排计划：{plan_id} ({len(targets)} 台, {strategy})",
        detail=(f"目标主机：\n{host_list}\n"
                f"统一命令（一次放行覆盖全部主机）：\n{step_lines}\n"
                f"健康检查：{plan.get('health_check') or '(无)'}\n"
                f"失败回滚：{', '.join(plan.get('rollback') or []) or '(无)'}\n"
                f"执行策略：{strategy}；失败处理：{'首台失败即停' if plan.get('fail_fast') else '单机失败隔离'}\n"
                f"原因：{(plan.get('note') or '(无)')[:300]}"),
        workspace=str(ws), emit=emit, interactive=interactive, force=True,
        timeout=float(timeout))
    result["decision"] = ap.get("decision", "")
    if not ap.get("ok"):
        result["why"] = f"人审未通过：{ap.get('reason')}"
        _deploy._audit(ws, {"action": "orch_rejected", "plan": plan_id,
                            "decision": ap.get("decision")})
        return result

    # 6) 找 ssh（人审通过后才准备）
    ssh = _deploy._find_ssh()
    if not ssh:
        result["why"] = "找不到系统 ssh.exe"
        return result
    per_timeout = int(plan.get("timeout") or 120)

    # 7) 按策略下发（审计统一在主线程写，避免并发写文件竞态）
    if strategy == "parallel":
        with ThreadPoolExecutor(max_workers=min(16, len(targets))) as pool:
            futs = {pool.submit(_run_host, t, plan, ssh=ssh, ws=ws, timeout=per_timeout,
                                secret_fn=secret_fn, interactive=interactive): t
                    for t in targets}
            for fut in as_completed(futs):
                row = fut.result()
                result["hosts"].append(row)
    else:
        # sequential：按序下发；fail_fast=True 时一台失败即停后续
        for t in targets:
            row = _run_host(t, plan, ssh=ssh, ws=ws, timeout=per_timeout,
                            secret_fn=secret_fn, interactive=interactive)
            result["hosts"].append(row)
            if not row["ok"] and plan.get("fail_fast"):
                break

    # 8) 汇总审计（每台每步）
    for h in result["hosts"]:
        for s in h["steps"]:
            _deploy._audit(ws, {"action": "orch_step", "plan": plan_id, "target": h["target"],
                                "command": s["command"][:200], "rc": s["rc"], "ok": s["ok"]})
        if h["health"]:
            _deploy._audit(ws, {"action": "orch_health", "plan": plan_id,
                                "target": h["target"], "ok": h["health"]["ok"]})

    all_ok = bool(result["hosts"]) and all(h["ok"] for h in result["hosts"])
    attempted = len(result["hosts"])
    if all_ok:
        result["why"] = f"编排成功：{attempted}/{len(targets)} 台全部通过"
    else:
        failed = [h["target"] for h in result["hosts"] if not h["ok"]]
        result["why"] = f"编排部分失败：{', '.join(failed)}（单机失败已隔离，其它主机不受影响）"
    result["ok"] = all_ok
    _deploy._audit(ws, {"action": "orch_exec", "plan": plan_id, "decision": ap.get("decision"),
                        "strategy": strategy, "ok": all_ok,
                        "hosts_ok": sum(1 for h in result["hosts"] if h["ok"]),
                        "hosts_total": len(targets)})
    return result


def describe() -> dict[str, Any]:
    return {
        "enabled": config()["enabled"],
        "quorum": _rc.APPROVE_QUORUM,
        "human_approval_forced": True,
        "approval_scope": "plan",   # 一次协商+一次人审覆盖整批主机
        "strategies": list(_STRATEGIES),
        "failure_isolation": True,
        "rollback_on_fail": True,
        "health_check": True,
        "fail_closed": True,
        "boundary": ("多机编排：整批主机共用一次三方协商≥2+一次强制人审；"
                     "每台每条命令过各自白名单，单机失败隔离不拖垮其它，失败回滚，"
                     "全程审计，凭据不落盘"),
    }
