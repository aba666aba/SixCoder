"""风险操作协商门：任何危险动作（部署 ssh / 命令行补软件 / …）都要三方 ≥2 同意。

## 为什么（2026-11-23 用户定稿）

用户原话："这个通道和其他的命令行行为要谨慎，不是有个协商机制吗，
这些风险操作要通过协商 >=2 同意即可，包括一些软件不全通过命令行补充的都是。"

意思很明确：
  1. 部署通道（deploy.py）和**其它命令行行为**都是风险操作；
  2. 复用已有的三方协商机制（deadlock.py 那一套：writer + reviewer1 + reviewer2）；
  3. 门槛是 **≥2 同意即可**（多数决，不是 3/3 全票——3/3 仍留给装 pip 包那类
     更危险的，见 deadlock.decide_env_install）；
  4. "软件不全、通过命令行补充"这类也算——即 envinstall / 工具链补齐等。

## 与现有机制的关系（不重复造轮子）

- **投票远端**复用 `multiremote.complete_as(role, ...)`：三方 key 角色解析、审核者用最强
  思考档、writer 走老路径，全部沿用；不新写远端口。
- **门槛**：≥2 approve（与 deadlock 的 unlock 多数决一致，但这里是"放行操作"不是
  "解锁重试"）。审核者调用失败 / 解析不出 / 弃权 = **不算同意**（fail-closed），
  这与 deadlock.py "审核者调用失败 ≠ 同意" 完全同口径。
- **人工审批门（approval.py）**仍保留，但两者定位不同：
  · 协商门 = **远端三方**的独立判断（≥2 即放行，符合"≥2 同意即可"）；
  · 人工门 = 人点头的可选第二道闸（deploy.py 里 force=True 那条）。
  本模块只负责协商门这一层；调用方（deploy / envinstall）决定要不要叠加人工门。

## 设计纪律

- **纯决策、可注入**：`complete_fn(role, prompt) -> {ok, answer}` 可注入，自检不真调远端。
- **fail-closed 三处**：① 三方配置不全 → 拒绝；② 审核者失败/弃权 → 不算票；③ 不足
  2 票 → 拒绝。任何异常都不可能导致"没人同意却放行"。
- **全程审计**：提案、票型、门槛结论都落 `.sixcoder/risk_consensus.jsonl`（不含凭据）。
- **凭据不进提示词**：只描述"用 agent 认证"这类事实，绝不把 key_ref / 密码塞给审核者。
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

# 门槛：三方里 ≥2 同意即放行（用户口径）。writer 是提案人默认同意。
APPROVE_QUORUM = 2
ROLES = ("writer", "reviewer1", "reviewer2")

_VERDICT_HINT = (
    '只输出一个 JSON：{"decision":"approve|reject","reason":"<一句话>"}。'
    'approve=这个风险操作可以做；reject=不该做。没有充分理由就 reject（谨慎优先）。'
)


def build_packet(proposal: dict[str, Any]) -> str:
    """构造给审核者的最小事实集：做什么、为什么、风险面、边界。不含任何凭据。"""
    p = proposal or {}
    lines = [
        "# 风险操作放行裁决",
        "",
        "有一步操作要执行，请你独立判断该不该放行。事实如下（不含任何凭据/密钥）：",
        "",
        f"- 操作类型：{str(p.get('kind') or '未知')}",
        f"- 目标：{str(p.get('target') or '(未指定)')[:200]}",
        f"- 具体命令/动作：{str(p.get('command') or p.get('action') or '(未指定)')[:400]}",
        f"- 理由：{str(p.get('reason') or '(未给)')[:400]}",
        f"- 风险面：{str(p.get('risk') or '外部副作用/不可逆操作')[:400]}",
        f"- 引擎已做的护栏：{str(p.get('guardrails') or '白名单 + 审计')[:400]}",
        "",
        "判断要点：这个操作是否必要、是否超出授权范围、是否有更安全的替代？",
        "拿不准或信息不足就 reject。",
        "",
        _VERDICT_HINT,
    ]
    return "\n".join(lines)


def _parse_verdict(text: str) -> dict:
    """从答复抠 decision JSON（模型爱加围栏/说明，兜住）。抠不出 = 弃权。"""
    if not text:
        return {}
    s, e = text.find("{"), text.rfind("}")
    if s < 0 or e <= s:
        return {}
    try:
        d = json.loads(text[s:e + 1])
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


def _default_ask(role: str, prompt: str) -> dict:
    """真的问三方远端（复用 multiremote 的角色通道）。"""
    try:
        from . import multiremote
        return multiremote.complete_as(
            role, prompt=prompt, task_id="risk_consensus",
            node_id="risk_%s" % role, json_mode=True)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def risk_ready() -> dict[str, bool]:
    """三方角色是否可用（writer + 两名审核者）。不可用时协商门必然拒绝。"""
    try:
        from . import multiremote
        rs = multiremote.roles() or {}
        return {r: bool(rs.get(r)) for r in ROLES}
    except Exception:                                    # noqa: BLE001
        return {r: False for r in ROLES}


def decide(proposal: dict[str, Any], *,
           complete_fn: Callable[[str, str], dict] | None = None,
           ask: Callable[[str, str], dict] | None = None,
           workspace: str | Path = "") -> dict[str, Any]:
    """对风险操作做三方协商，≥2 同意才放行。纯决策（除审计外无副作用）。

    Args:
        proposal: {kind, target, command/action, reason, risk, guardrails}。
        complete_fn / ask: 注入的提问函数 (role, prompt) -> {ok, answer}；自检用。
            `ask` 是 complete_fn 的别名，二者取其一。缺省走 multiremote.complete_as。
        workspace: 审计日志写到 <ws>/.sixcoder/risk_consensus.jsonl。

    Returns:
        {ok(是否放行), granted, votes, tally, decision, reason, quorum, packet}
    """
    fn = ask or complete_fn or _default_ask
    packet = build_packet(proposal)
    votes: dict[str, str] = {}
    reasons: dict[str, str] = {}
    errors: dict[str, str] = {}
    tally = {"approve": 0, "reject": 0, "abstain": 0}

    # ① 三方可用性检查（缺配置 = 没有独立同意 = 拒绝）——但注入 fn 时跳过（自检可控）
    if fn is _default_ask:
        ready = risk_ready()
        if not all(ready.values()):
            out = {"ok": False, "granted": False, "votes": {}, "tally": tally,
                   "decision": "reject", "quorum": APPROVE_QUORUM,
                   "reason": "三方协商不可用（缺 writer/reviewer 角色配置）→ 拒绝",
                   "packet": packet}
            _audit(workspace, proposal, out)
            return out

    # ② 三方独立投票：writer 是提案人（默认 approve），两名审核者独立裁决
    for role in ROLES:
        if role == "writer":
            votes[role] = "approve"
            reasons[role] = "提案人默认同意；放行由两名审核者的独立票决定"
            continue
        try:
            r = fn(role, packet) or {}
        except Exception as exc:                         # noqa: BLE001
            r = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        v = _parse_verdict(str(r.get("answer") or "")) if r.get("ok") else {}
        decision = str(v.get("decision") or "").strip().lower()
        if decision not in ("approve", "reject"):
            decision = "abstain"                        # 失败/解析不出/弃权 = 不算同意
        votes[role] = decision
        reasons[role] = str(v.get("reason") or "")[:300]
        if not r.get("ok"):
            errors[role] = str(r.get("error") or "")[:200]

    tally = {k: sum(1 for v in votes.values() if v == k)
             for k in ("approve", "reject", "abstain")}
    granted = tally["approve"] >= APPROVE_QUORUM
    out = {
        "ok": granted,
        "granted": granted,
        "votes": votes,
        "tally": tally,
        "decision": "approve" if granted else "reject",
        "reason": (f"三方 {tally['approve']}/3 同意（门槛 ≥{APPROVE_QUORUM}）→ 放行"
                   if granted else
                   f"仅 {tally['approve']}/3 同意（门槛 ≥{APPROVE_QUORUM}）→ 拒绝"
                   + ("；原因：" + "; ".join(reasons[r] for r in ROLES if reasons.get(r))
                      if any(reasons.values()) else "")),
        "reasons": reasons,
        "errors": errors,
        "quorum": APPROVE_QUORUM,
        "packet": packet,
    }
    _audit(workspace, proposal, out)
    return out


def _audit(workspace: str | Path, proposal: dict, out: dict) -> None:
    """审计（不含凭据：只记 kind/target/动作摘要 + 票型 + 结论）。"""
    if not workspace:
        return
    try:
        d = Path(workspace) / ".sixcoder"
        d.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "action": "risk_consensus",
            "kind": str((proposal or {}).get("kind") or "")[:40],
            "target": str((proposal or {}).get("target") or "")[:80],
            "votes": out.get("votes"),
            "tally": out.get("tally"),
            "granted": out.get("granted"),
        }
        with (d / "risk_consensus.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:                                    # noqa: BLE001
        pass


def describe() -> dict[str, Any]:
    return {
        "quorum": APPROVE_QUORUM,
        "roles": list(ROLES),
        "fail_closed": True,
        "abstain_counts_as_reject": True,
        "ready": risk_ready(),
        "boundary": ("风险操作（部署/命令行补软件等）需三方 ≥2 同意；"
                     "审核者失败/弃权不算同意；三方不可用一律拒绝"),
    }
