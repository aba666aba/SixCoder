"""¥ 硬上限：钱超过预设就停，而不是"只记账不拦人"。

现状问题（自查发现）：`company.py` 里 `cost_cny` 出现 0 次 —— 花费只记录、从不参与决策，
所以"这一夜花了多少"永远只能事后看账单。

设计（最小且可验证）：
  · `cost_spent(task_id)`：读本任务的真实花费（人民币，来自 remote_usage）；
  · `over_cost_cap(task_id, cap)`：超过就返回 (True, 明细)；
  · 夜班每轮结束检查一次，超了立刻收工，收工原因写明"花费到上限"；
  · CLI `--max-cny`、`/v1/night` 的 `max_cny`、环境 `SIXCODER_MAX_CNY` 三个入口；
  · 0 / 不填 = 不设上限（默认行为不变，避免把已有用法搞坏）。
"""
from __future__ import annotations

import os
from pathlib import Path


def _cap_from_env() -> float:
    try:
        return float(os.environ.get("SIXCODER_MAX_CNY") or 0)
    except (TypeError, ValueError):
        return 0.0


def resolve_cap(max_cny: float | None = None) -> float:
    """优先级：显式参数 > 环境变量 > 0（不设上限）。"""
    if max_cny is not None and float(max_cny) > 0:
        return float(max_cny)
    return _cap_from_env()


def cost_spent(task_id: str) -> dict:
    """本任务到目前为止的真实花费（人民币）。取不到就如实说取不到。"""
    try:
        from . import remote
        u = remote.usage_summary(task_id) or {}
        return {"ok": True, "cny": float(u.get("cost_cny") or 0.0),
                "calls": int(u.get("calls") or 0),
                "prompt_tokens": int(u.get("prompt_tokens") or 0),
                "completion_tokens": int(u.get("completion_tokens") or 0)}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "cny": 0.0, "reason": f"{type(exc).__name__}: {exc}"}


def over_cost_cap(task_id: str, cap: float) -> tuple[bool, dict]:
    """钱是不是已经到顶。返回 (是否超限, 明细)。

    cap <= 0 表示不设上限 —— 这时永远返回 False，保证默认行为不变。
    """
    if not cap or float(cap) <= 0:
        return False, {"cap": 0.0, "note": "未设花费上限"}
    spent = cost_spent(task_id)
    if not spent.get("ok"):
        # 取不到花费时**不要误停**：如实标记"没检查成功"，由人看报告
        return False, {"cap": float(cap), "spent": None,
                       "note": f"读不到花费（{spent.get('reason')}），本轮不拦"}
    cny = float(spent["cny"])
    return cny >= float(cap), {"cap": float(cap), "spent": cny, "calls": spent.get("calls"),
                              "note": ("已到花费上限" if cny >= float(cap) else "在上限内")}


def report_line(task_id: str, cap: float) -> str:
    """交付报告里的一行花费说明（有上限/没上限都写清）。

    **必须带 task_id**：`usage_summary("")` 返回的是**全库累计**，
    拿它冒充"本次花费"会把历史总账写进单次报告（实测：一次 0 调用的运行
    报告里写着"花费 ¥6.2099（941 次远端调用）"）。拿不到 task_id 就如实说统计不到。
    """
    if not str(task_id or "").strip():
        return "- 花费：本次未能统计（缺 task_id；不显示累计值以免误导）"
    spent = cost_spent(task_id)
    if not spent.get("ok"):
        return f"- 花费：读不到（{spent.get('reason')}）"
    base = f"- 花费：¥{spent['cny']:.4f}（{spent.get('calls') or 0} 次远端调用）"
    if cap and cap > 0:
        base += f"，上限 ¥{cap:.2f}" + ("，**已触顶**" if spent["cny"] >= cap else "，未触顶")
    else:
        base += "，未设上限"
    return base
