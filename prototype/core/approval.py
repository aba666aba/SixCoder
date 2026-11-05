"""人工审核门：开发者模式下，关键节点必须有人点头才继续（`needhuman`）。

为什么要有它（甲方要"自动化"，开发者要"可控"）：
- 甲方模式：全部自动放行，夜里跑完早上看报告；
- 开发者模式：**写文件 / 提交 / 推送 / 上报远端 / 收尾交付** 五个节点必须人工审核，
  不会自动放行——每个节点抛 `needhuman` 并**阻塞**，人点"通过"才继续。

跨进程（这是关键设计）：夜班可能跑在 CLI 进程里，审批却发生在浏览器界面上，
两边的内存不共享。所以待审单据**落盘**在 `<仓库根>/_logs/approvals/`：

    <id>.json           待审单据（谁、哪个节点、改了什么 diff）
    <id>.decision.json  人工决定（approved / rejected + 谁批的 + 时间）

进程 A 写单据后轮询决定文件；进程 B（或人）写决定文件即可放行。落盘还顺带留下审计痕迹。

行为约定（都不许含糊）：
- **没人回答 = 拒绝**（fail-closed）：超时 600 秒，按拒绝处理，绝不因为"没人管"就当成同意；
- 每次审批（请求/通过/拒绝/超时）都写 `.sixcoder/audit.jsonl`，事后能查谁放行了什么；
- 终端里能直接问人（`SIXCODER_APPROVAL=cli` 强制走终端，`web` 强制走界面，默认 auto）。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from . import permissions

DEFAULT_TIMEOUT = 600.0
POLL_INTERVAL = 0.2
REPO = Path(__file__).resolve().parent.parent.parent      # 仓库根


def approvals_dir() -> Path:
    d = REPO / "_logs" / "approvals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _paths(ticket_id: str) -> tuple[Path, Path]:
    d = approvals_dir()
    return d / f"{ticket_id}.json", d / f"{ticket_id}.decision.json"


def _audit(ws: str | Path, entry: dict) -> None:
    if not ws:
        return
    try:
        d = Path(ws) / ".sixcoder"
        d.mkdir(parents=True, exist_ok=True)
        with (d / "audit.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **entry},
                                ensure_ascii=False) + "\n")
    except Exception:                                    # noqa: BLE001
        pass


def _write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return None


def _ticket_view(t: dict) -> dict:
    created = float(t.get("created_at") or 0)
    decided = float(t.get("decided_at") or 0)
    out = dict(t)
    out["seconds_waiting"] = round((decided or time.time()) - created, 1) if created else 0.0
    return out


def request(node: str, title: str, detail: str = "", workspace: str | Path = "",
            emit=None, timeout: float = DEFAULT_TIMEOUT,
            interactive: bool | None = None) -> dict:
    """请求人工审核。返回 {ok, decision, reason, ticket}。

    甲方模式 / 只读模式下（auto_approve=True）直接放行，不打扰人。
    """
    mode = permissions.current_mode()
    if not permissions.needs_human(node):
        return {"ok": True, "decision": "auto", "reason": f"{mode.name}：该节点不需要人工审核",
                "ticket": None}

    if interactive is None:
        interactive = _stdin_is_tty()
    if interactive:
        print(f"\n[需要你确认] {title}")
        if detail:
            print(_indent(detail))
        try:
            ans = input("通过？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("（没读到输入，转到待审列表，等界面/接口审批）")
            interactive = False
        else:
            decision = "approved" if ans in ("y", "yes", "是", "通过") else "rejected"
            _audit(workspace, {"action": "approval", "node": node, "decision": decision,
                               "by": "cli", "title": title})
            return {"ok": decision == "approved", "decision": decision,
                    "reason": "命令行确认" if decision == "approved" else "命令行拒绝",
                    "ticket": None}

    ticket_id = f"ap-{uuid.uuid4().hex[:8]}"
    tpath, dpath = _paths(ticket_id)
    ticket = {"id": ticket_id, "node": node, "title": title, "detail": (detail or "")[:4000],
              "workspace": str(workspace or ""), "created_at": time.time(),
              "mode": mode.id, "pid": os.getpid()}
    _write_json(tpath, ticket)
    _audit(workspace, {"action": "approval_requested", "node": node, "ticket": ticket_id,
                       "title": title})
    if emit:
        emit("approval_pending", _ticket_view(ticket))

    deadline = time.time() + max(1.0, float(timeout))
    decision, by = "", ""
    while time.time() < deadline:
        got = _read_json(dpath)
        if got and got.get("decision") in ("approved", "rejected"):
            decision, by = got["decision"], got.get("by", "human")
            break
        time.sleep(POLL_INTERVAL)
    if not decision:
        decision, by = "timeout", "system"

    ticket.update({"decided": decision, "decided_at": time.time(), "by": by})
    for p in (tpath, dpath):
        try:
            p.unlink()
        except OSError:
            pass
    _audit(workspace, {"action": "approval_decided", "node": node, "ticket": ticket_id,
                       "decision": decision, "by": by})
    if emit:
        emit("approval", _ticket_view(ticket))

    view = _ticket_view(ticket)
    if decision == "approved":
        return {"ok": True, "decision": decision, "reason": f"人工通过（{by}）", "ticket": view}
    reason = ("超时未审核，按拒绝处理（fail-closed）" if decision == "timeout"
              else f"人工拒绝（{by}）")
    return {"ok": False, "decision": decision, "reason": reason, "ticket": view}


def resolve(ticket_id: str, decision: str, by: str = "ui") -> dict:
    """外部（HTTP 接口 / 界面按钮）回填审批结果：写决定文件即可。"""
    if decision not in ("approved", "rejected"):
        return {"ok": False, "reason": "decision 只能是 approved / rejected"}
    tpath, dpath = _paths(ticket_id)
    if not tpath.exists():
        return {"ok": False, "reason": f"没有待审单据 {ticket_id}（可能已超时或已处理）"}
    _write_json(dpath, {"decision": decision, "by": by, "at": time.time()})
    return {"ok": True, "ticket": {"id": ticket_id, "decision": decision, "by": by}}


def pending() -> list[dict]:
    """所有等待中的单据（跨进程可见）。"""
    out = []
    for f in sorted(approvals_dir().glob("ap-*.json")):
        if f.name.endswith(".decision.json"):
            continue
        dec, _ = _paths(f.stem)
        if dpath_decided(f.stem):
            continue
        t = _read_json(f)
        if t:
            out.append(_ticket_view(t))
    return out


def dpath_decided(ticket_id: str) -> bool:
    _, dpath = _paths(ticket_id)
    return dpath.exists()


def _indent(text: str, prefix: str = "    ") -> str:
    return "\n".join(prefix + ln for ln in str(text).splitlines()[:12])


def _stdin_is_tty() -> bool:
    """能不能在命令行里直接问人。

    环境变量 SIXCODER_APPROVAL=cli|web|auto 可强制指定（默认 auto）。
    自动化/夜班场景用 web：绝不能因为"读不到输入"就当成通过。
    """
    forced = (os.environ.get("SIXCODER_APPROVAL") or "auto").strip().lower()
    if forced in ("web", "ui", "api"):
        return False
    if forced in ("cli", "tty", "console"):
        return True
    try:
        import sys
        return bool(getattr(sys.stdin, "isatty", lambda: False)())
    except Exception:                                    # noqa: BLE001
        return False
