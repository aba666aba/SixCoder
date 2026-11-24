"""关键节点守卫：把"要不要人点头"集中在这五个函数里，别散落在各处。

为什么单独一层：
- 造物模式（auto_approve=True）→ 全部直接放行，夜里无人值守也能跑完；
- 精修模式 → 五个关键节点阻塞等人（`needhuman`），人点通过才继续；
- 审阅模式 → 压根没有写/提交/推送能力，根本走不到这里。

调用点（只有这些，别在别处再写一遍）：
    file.write  → iterate.execute_local（写盘前）
    git.commit  → gitops.commit
    git.push    → gitops.push / merge
    remote.call → remote.complete（把代码发往远端前）
    deliver     → company._night_inner（收尾前）
"""
from __future__ import annotations

import difflib
from pathlib import Path

from . import approval, permissions


def _need(node: str) -> bool:
    return permissions.needs_human(node)


def gate_file_write(ws: Path, rel: str, old: str, new: str, emit=None) -> dict:
    """写盘前：精修模式要人看一眼"改了什么"（给 diff，不给一句"要写文件"）。"""
    if not _need("file.write"):
        return {"ok": True, "decision": "auto"}
    diff = "\n".join(list(difflib.unified_diff(
        (old or "").splitlines(), (new or "").splitlines(),
        fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=2))[:80])
    return approval.request("file.write", f"写入 {rel}",
                            diff or f"{rel}（内容为空或仅换行）", workspace=ws, emit=emit)


def gate_git_commit(ws: Path, message: str, files: list[str] | None = None, emit=None) -> dict:
    if not _need("git.commit"):
        return {"ok": True, "decision": "auto"}
    detail = f"提交信息：{message}\n文件：{', '.join(files or []) or '（全部改动）'}"
    return approval.request("git.commit", "创建 git 提交", detail, workspace=ws, emit=emit)


def gate_git_push(ws: Path, action: str, detail: str = "", emit=None) -> dict:
    if not _need("git.push"):
        return {"ok": True, "decision": "auto"}
    return approval.request("git.push", f"远端操作：{action}", detail, workspace=ws, emit=emit)


def gate_remote_allowed() -> dict:
    """档位层面的远端总开关：甲方强制开、只读强制关、开发者看设置里的开关。

    放在这里（而不是各处自己判断）是为了只有一处口径 —— 用户的说法是
    "这个开关只要精修模式有，甲方是强制开启的，只读是强制关闭的"。
    """
    try:
        from . import policy
        ok, why = policy.remote_allowed()
    except Exception as exc:                             # noqa: BLE001
        return {"ok": True, "reason": f"（mode 检查跳过：{type(exc).__name__}）"}
    return {"ok": bool(ok), "reason": why if not ok else ""}


def gate_remote_call(ws: Path | str, prompt_chars: int, task_id: str = "", emit=None) -> dict:
    # 0) 档位总开关：精修模式关掉远端时，连审批入口都不给（甲方强制开、只读强制关）
    sw = gate_remote_allowed()
    if not sw["ok"]:
        return sw
    if not _need("remote.call"):
        return {"ok": True, "decision": "auto"}
    detail = (f"任务：{task_id or '-'}\n包体：{prompt_chars} 字符\n"
              f"注意：这一步会把相关代码片段发送到 DeepSeek。")
    return approval.request("remote.call", "上报远端模型", detail, workspace=ws, emit=emit)


def gate_deliver(ws: Path, verified: int, total: int, reason: str, emit=None) -> dict:
    if not _need("deliver"):
        return {"ok": True, "decision": "auto"}
    detail = f"机械验收：{verified}/{total}\n收工原因：{reason}"
    return approval.request("deliver", "标记为可交付（写 DELIVERY.md / TRACE.md）",
                            detail, workspace=ws, emit=emit)
