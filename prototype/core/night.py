"""夜班主循环（**唯一一条**）：判型 → 供料 → 整仓施工 → 全量验证 → 报告回灌。

为什么新开这个模块，而不是继续用 `auto.night_auto`（2026-11-15 用户定稿）：

    `night_auto` 约 1100 行，它的骨架是"切片 + 每片一份契约 + 片预算 + 片内自验"，
    那是为**小步增量**长出来的。而现在施工是"整仓一次给定、放肆改全部"——
    切片只会把整仓视野切碎（片越小，跨文件的共同因越看不见）。
    两条路并存的结果是**决策路径不唯一**（"谁决定改哪里"有两套答案），
    实测代价：`orders` 算了六层筛选，真正施工时又按另一套逻辑挑文件。

新路径只有四步，**全部复用已有能力，不新写业务逻辑**：

    ① 判型   `fullgen.decide_mode`：空项目(scratch) / 已有项目(repair)
    ② 供料   空 → `generate_contract_by_domain` 分域生成全量契约并**锁定**
             已有 → 直接用锁定的 `.sixcoder/full_contract.txt`
    ③ 施工   `company.night`：每轮 = 全量观测 → 报告 → 整仓会话 → 重新接线 → 全量重测
    ④ 收工   `company.night` 自带 DELIVERY.md + 三账

`night_auto` 保留但**不再被调用**（冻结；新路径跑稳后再删）。
阶段 1 的架构问答（`--clarify / --ask / --interview`）不在新路径里 ——
它属于"人参与设计"的另一个模式，`cli night` 碰到这些参数会回落到旧路径并明说。
"""
from __future__ import annotations

import time
from pathlib import Path


def _emit(emit, tag: str, data: dict) -> None:
    if not emit:
        return
    try:
        emit(tag, data)
    except Exception:                                    # noqa: BLE001
        pass


def run(repo, goal: str = "", *, model: str = "", max_rounds: int = 40,
        emit=None, fresh: bool = False, remote_profile: str = "",
        plan: str = "", scale_level: str = "", refresh_contract: bool = False):
    """跑一整夜。返回 `company.NightResult`（带 `.mode` 便于日志辨认）。"""
    from . import company as _co
    from . import fullgen as _fg
    from . import stack as _stk

    ws = Path(repo)
    t0 = time.time()

    # ① 判型（进程级栈也必须在这里定：不设就恒落回 fastapi）
    try:
        _stk.activate(ws)
    except Exception as exc:                             # noqa: BLE001
        _emit(emit, "notice", {"message": "栈激活跳过：%s: %s" % (type(exc).__name__, exc)})
    mode = "repair"
    try:
        mode = _fg.decide_mode(ws)
    except Exception as exc:                             # noqa: BLE001
        _emit(emit, "notice", {"message": "判型跳过（按已有项目处理）：%s" % type(exc).__name__})
    _emit(emit, "night_mode", {
        "mode": mode,
        "note": ("空项目：先生成全量契约，再整仓从零写" if mode == "scratch"
                 else "已有项目：直接整仓检测 + 整仓会话改（不重写已有实现）")})

    # ② 供料：契约是"判据的真相来源"，也是缓存命中的前提（一次锁定、前缀不变）
    lock = ws / _fg.CONTRACT_LOCK
    has_lock = False
    try:
        has_lock = lock.is_file() and bool(
            lock.read_text(encoding="utf-8-sig", errors="replace").strip())
    except OSError:
        has_lock = False
    if mode == "scratch" and (refresh_contract or fresh or not has_lock):
        _emit(emit, "night_contract_gen",
              {"note": "分域生成全量契约（单域 1-3 分钟，某域失败不连累其它域）"})
        try:
            g = _fg.generate_contract_by_domain(ws, None, emit=emit)
            _emit(emit, "night_contract_done", {
                "ok": g.get("ok"), "funcs": g.get("funcs"),
                "chars": g.get("chars"),
                "failed_domains": [d.get("name") for d in (g.get("domains") or [])
                                   if not d.get("ok")]})
        except Exception as exc:                         # noqa: BLE001
            _emit(emit, "notice", {"message": "契约生成失败：%s: %s"
                                               % (type(exc).__name__, exc)})
    try:
        text = _fg._lock_contract(ws, refresh=bool(refresh_contract), emit=emit)
        _emit(emit, "night_contract", {"chars": len(text or ""), "file": str(lock)})
    except Exception as exc:                             # noqa: BLE001
        _emit(emit, "notice", {"message": "契约锁定跳过：%s: %s" % (type(exc).__name__, exc)})

    # ③ 施工：只走公司制那一条（每轮整仓会话 + 全量验证）
    _emit(emit, "night_start", {"rounds": max_rounds, "mode": mode,
                                "elapsed": round(time.time() - t0, 1)})
    res = _co.night(ws, goal, model, max_rounds=max_rounds, emit=emit,
                    plan=plan, contract_file=str(lock),
                    remote_profile=remote_profile, scale_level=scale_level)

    # ④ 收工（DELIVERY.md / 三账由 company.night 写完）
    try:
        setattr(res, "mode", mode)
    except Exception:                                    # noqa: BLE001
        pass
    _emit(emit, "night_end", {
        "ok": bool(getattr(res, "ok", False)),
        "verified": getattr(res, "verified", -1),
        "total": getattr(res, "total", -1),
        "reason": str(getattr(res, "reason", ""))[:200],
        "delivery": str(getattr(res, "delivery_path", "") or ""),
        "need_human": str(getattr(res, "need_human", "") or "")[:200],
        "spent_s": round(time.time() - t0, 1)})
    return res
