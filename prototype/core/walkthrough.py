"""端到端链路走查：把本轮新增能力串成一条可读的用户旅程。

和两个同名易混的模块分工（别混）：
  · `core/e2e.py`    —— **业务流判据**：清洗→入库→汇总的跨模块不变式（代码产物侧）；
  · `core/verify.py` —— **能力验收单**：每项默认红、机判、带 human_review；
  · 本模块            —— **链路连通走查**：按用户使用顺序过一遍，确认各模块能被
    调用、配置能只读读出、视图彼此一致。它回答"整条链通不通"，不回答"能力对不对"。

安全约定（沿用红线）：
  · 只做**只读 / 内存态**操作：不写用户 settings、不连真实服务器、不落任何凭据；
  · 半自动步骤用 `__walk__` 命名空间，跑完立刻 `clear_all()`；
  · 任何一步异常只记该步失败，不吞掉其它步骤的结果。
"""
from __future__ import annotations

from typing import Any


def _step(name: str, ok: bool, detail: str, *, skipped: bool = False) -> dict[str, Any]:
    status = "skip" if skipped else ("pass" if ok else "fail")
    return {"name": name, "ok": bool(ok) and not skipped, "status": status,
            "detail": detail}


def walkthrough(*, include_verify: bool = False) -> dict[str, Any]:
    """按用户旅程走一遍。`include_verify=True` 时额外真跑一张验收单（较慢）。"""
    steps: list[dict[str, Any]] = []

    # 1) 样式库可读 + 风格约定可生成
    try:
        from core import uistyles as u
        cfg = u.public_config()
        presets = cfg.get("presets") or []
        types = cfg.get("types") or {}
        slots = cfg.get("color_slots") or []
        n_types = sum(len(v) for v in types.values())
        ok1 = len(presets) == 10 and n_types == 40 and len(slots) >= 6
        steps.append(_step("样式库可读", ok1,
                           f"预设 {len(presets)} / 类型 {n_types} / 插槽 {len(slots)}"))
        sel = str((cfg.get("selected") or {}).get("selected") or "")
        txt = u.contract_text(sel) if sel else ""
        steps.append(_step("风格约定可生成", True,
                           ("已按选定风格生成约定" if txt
                            else "用户未选风格（跳过，不注入）"),
                           skipped=not bool(txt)))
    except Exception as exc:                             # noqa: BLE001
        steps.append(_step("样式库可读", False, f"{type(exc).__name__}: {exc}"))

    # 2) 半自动人填闭环（内存态，只碰 __walk__ 命名空间，绝不清用户已有值）
    try:
        from core import human_once as h
        h.clear("__walk__", "otp")
        h.put("__walk__", "otp", "000000", ttl=30, uses=1)
        got = h.get("__walk__", "otp")
        consumed = not h.has("__walk__", "otp")
        h.clear("__walk__", "otp")
        ok2 = got == "000000" and consumed
        steps.append(_step("半自动人填闭环", ok2,
                           "put→get→消费，值不回显" if ok2 else "闭环失败"))
    except Exception as exc:                             # noqa: BLE001
        steps.append(_step("半自动人填闭环", False, f"{type(exc).__name__}: {exc}"))

    # 3) 插件适配清单完整
    try:
        from core import host_plugins as hp
        ads = hp.list_adapters()
        bad = [a for a in ads if not a.get("docs") or not a.get("can_verify")]
        ok3 = len(ads) >= 5 and not bad
        steps.append(_step("插件适配清单", ok3,
                           f"{len(ads)} 个适配器，均有公开文档与可验判据" if ok3
                           else f"有 {len(bad)} 个缺文档/判据"))
    except Exception as exc:                             # noqa: BLE001
        steps.append(_step("插件适配清单", False, f"{type(exc).__name__}: {exc}"))

    # 4) 判据开放清单（文档全 https）
    try:
        from core import judgment_sources as js
        srcs = js.list_sources()
        ok4 = len(srcs) >= 5 and all(
            str(s.get("docs", "")).startswith("https://") for s in srcs)
        steps.append(_step("判据开放清单", ok4,
                           f"{len(srcs)} 个平台，文档全 https" if ok4
                           else "有平台缺 https 文档"))
    except Exception as exc:                             # noqa: BLE001
        steps.append(_step("判据开放清单", False, f"{type(exc).__name__}: {exc}"))

    # 5) 部署目标视图（只读，不真连）
    try:
        from core import deploy as d
        dc = d.public_config()
        ts = dc.get("targets") or []
        steps.append(_step("部署目标视图", True,
                           f"通道{'已启用' if dc.get('enabled') else '未启用'}；"
                           f"{len(ts)} 个预留目标"))
    except Exception as exc:                             # noqa: BLE001
        steps.append(_step("部署目标视图", False, f"{type(exc).__name__}: {exc}"))

    # 6) 多机编排视图（计划与目标引用一致）
    try:
        from core import orchestration as o
        oc = o.public_config()
        plans = oc.get("plans") or []
        missing = [x for p in plans for x in p.get("host_details", [])
                   if not x.get("configured")]
        steps.append(_step("多机编排视图", True,
                           f"{len(plans)} 个计划"
                           + (f"；{len(missing)} 台目标未登记" if missing else "")))
    except Exception as exc:                             # noqa: BLE001
        steps.append(_step("多机编排视图", False, f"{type(exc).__name__}: {exc}"))

    # 7) 闭环验收单（可选，较慢：会真跑浏览器）
    if include_verify:
        try:
            from core import verify as v
            r = v.run_all()
            steps.append(_step("闭环验收单", bool(r.get("ok")),
                               f"{r.get('passed')}/{r.get('total')} 项通过"))
        except Exception as exc:                         # noqa: BLE001
            steps.append(_step("闭环验收单", False, f"{type(exc).__name__}: {exc}"))

    failed = [s for s in steps if s["status"] == "fail"]
    return {
        "ok": not failed,
        "passed": sum(1 for s in steps if s["status"] == "pass"),
        "skipped": sum(1 for s in steps if s["status"] == "skip"),
        "total": len(steps),
        "steps": steps,
        "note": "链路走查：只读/内存态，不改用户配置、不连真实服务器；"
                "能力成色看 verify 验收单",
    }


def describe() -> dict[str, Any]:
    return {
        "walks": ["style", "human_once", "host_plugin", "judgment",
                  "deploy_view", "orchestration_view", "verify(optional)"],
        "mutates_user_config": False,
        "connects_real_host": False,
        "note": "端到端链路走查；与 verify 的验收单互补，不替代",
    }
