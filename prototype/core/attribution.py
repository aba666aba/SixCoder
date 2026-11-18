"""成本归因：让每一笔远端花费都能落到**能力**上。

为什么必须有（2026-11-15 用户定调）：
> 代码能力看远端，本地需要干的是同样质量下最少的钱完成任务。

那"最少的钱"必须可度量。`remote_usage` 原来只有 `task_id/purpose/project_id`，
**没有 `cap_id`** → 算不出"每条能力花了多少"，于是：
① 无法知道钱花在哪个能力上；② 无法分辨"这笔钱是不是废单"；③ 优化无法证明有效。

用法（只在派活的边界上用，不许散落）：
    with attribution.unit(module="app/services/topics.py", phase="author"):
        author_module(...)          # 期间所有远端调用都带上这些能力的 id

设计取舍：
- **按模块归属**而不是按"当前在写哪个函数" —— 引擎的派单单位就是模块（`cap.modules`），
  硬要精确到函数会引入一堆不确定的推断，反而不可信。
- 一个模块可能承载多条能力 → `cap_id` 存**逗号分隔**（聚合时按能力拆分/均摊，见 cost_baseline）。
- 没有上下文时写空串，**绝不猜**（空串在报表里单独一列"未归因"，逼着把钩子补全）。
"""
from __future__ import annotations

from contextlib import contextmanager

_STATE: dict = {"module": "", "cap_ids": [], "phase": "", "bp": None}
_RESTORE: list = []


def set_blueprint(bp) -> None:
    """登记蓝图（用于 module → caps 的机械映射）。夜班/全量入口调一次。"""
    _STATE["bp"] = bp


def caps_for_module(module: str) -> list:
    """模块 → 它承载的能力 id（按蓝图 `cap.modules`，不猜）。"""
    bp = _STATE.get("bp")
    mod = str(module or "")
    if bp is None or not mod:
        return []
    out = []
    for c in (getattr(bp, "caps", None) or []):
        if mod in (getattr(c, "modules", None) or []):
            out.append(str(c.id))
    return out


@contextmanager
def unit(module: str = "", phase: str = "", cap_ids=None):
    """进入一个"作业单元"：期间远端调用都归到这些能力上（可嵌套）。"""
    prev = (_STATE["module"], list(_STATE["cap_ids"]), _STATE["phase"])
    if module:
        _STATE["module"] = str(module)
        _STATE["cap_ids"] = list(cap_ids) if cap_ids is not None else caps_for_module(module)
    if phase:
        _STATE["phase"] = str(phase)
    if cap_ids is not None and not module:
        _STATE["cap_ids"] = list(cap_ids)
    try:
        yield
    finally:
        _STATE["module"], _STATE["cap_ids"], _STATE["phase"] = prev


def snapshot() -> dict:
    """当前归因（远端记账时读它）。"""
    return {"cap_id": ",".join(_STATE["cap_ids"] or []),
            "module": _STATE["module"],
            "phase": _STATE["phase"]}


def reset() -> None:
    _STATE.update({"module": "", "cap_ids": [], "phase": ""})
