#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""多远端协作：一个写作者 + 两个审核者（2026-11-18 用户口径）。

角色从哪来（配置层 = settings_store 的厂商 → key → 模型 三层）：
  · **写作者 writer**：老主路径不变 —— 「正在使用」厂商的当前模型
    （active_model()）。全部写码照旧走这条，风格一致由"只允许 writer 落盘"保证。
  · **审核者 reviewer1 / reviewer2**：优先取**其它 key 类**里的启用模型
    （不同 key → 不共享限流池，审核者不会因为写作者在跑而 429）；
    没有第二个 key 类时退到同厂商的其它模型档案；再没有就退到同一模型的
    **高档思考档**（high/max —— 至少是不同的推理路径），并标 degraded=True；
    连档案都没有（平铺单配置）→ 返回 reviewers=None，死局协商机制自动停用，
    一切照旧，绝不因为配置不够把夜班搞挂。

铁律边界：审核者只产出**裁决意见**（解锁/驳回/通过/否决），绝不落盘代码；
落盘的唯一入口仍是写作者。检测器是裁判，审核者是"裁判的辩护律师"，不是第二个裁判。
"""
from __future__ import annotations

from . import settings_store

ROLES = ("writer", "reviewer1", "reviewer2")


def _profile_of(mid: str) -> dict | None:
    """按档案 id 找模型档案（只在「正在使用」厂商里找，不跨厂商兜底）。"""
    cur = settings_store.current_provider()
    pid = str(cur.get("id") or "")
    for m in settings_store.models():
        if str(m.get("id") or "") == mid and str(m.get("provider") or "") == pid:
            return dict(m)
    return None


def roles() -> dict | None:
    """解析三角色。返回 {role: {profile dict, key_id, degraded?}} 或 None。

    **角色配置优先**（2026-11-18 晚，用户口径：三个角色可以是同一厂商的不同 key）：
      · key 类上显式指派的 role（writer / reviewer1 / reviewer2）先认领；
      · 该 key 类里挑**第一个启用的模型**（它自己的模型列表）；类里没模型 →
        借用写作者的模型但**用这把 key 发请求**（同厂商同模型、不同 key =
        独立限流池，这正是"一个厂商三把 key"的用法）；
      · 没指派角色的 key 类作为兜底候选（旧配置无感）。

    返回 None = 一个可用远端档案都没有（机制停用，行为照旧）。
    """
    writer = settings_store.active_model()
    if not writer or not str(writer.get("model") or "").strip():
        return None
    cur = settings_store.current_provider()
    pid = str(cur.get("id") or "")
    kcs = {str(k.get("id")): k for k in settings_store.key_classes(pid)}
    writer_key = str(writer.get("key_id") or "k1")

    def _model_in_key(kid: str) -> dict | None:
        """某 key 类自己的第一个启用模型（带该类的 key 继承）。"""
        kc = kcs.get(kid) or {}
        pool = [m for m in (kc.get("models") or []) if m.get("enabled", True)
                and str(m.get("model") or "").strip()]
        if not pool:
            return None
        m = dict(pool[0])
        m["provider"] = pid
        m["key_id"] = kid
        m["compat"] = pid if pid in ("deepseek", "glm") else "openai"
        if not str(m.get("api_key") or "").strip():
            m["api_key"] = str(kc.get("api_key") or "").strip() \
                or settings_store.provider_key(pid)
        if not m.get("base_url"):
            m["base_url"] = cur.get("base_url") or ""
        return m

    out: dict = {"writer": dict(writer)}
    degraded = False
    assigned = {str(k.get("role") or ""): k for k in (kcs.values()) if k.get("role")}
    for role in ("reviewer1", "reviewer2"):
        kc = assigned.get(role)
        if kc:
            m = _model_in_key(str(kc["id"]))
            if m:
                out[role] = m
                continue
            # 类里没模型：借用写作者模型 + 这把 key（独立限流池）
            q = dict(writer)
            q["key_id"] = str(kc["id"])
            q["api_key"] = str(kc.get("api_key") or "").strip() \
                or settings_store.provider_key(pid)
            q["id"] = "%s@%s" % (writer.get("id"), kc["id"])
            q["label"] = "%s（%s 的 key）" % (writer.get("label"), kc["id"])
            out[role] = q
            continue
        # 未指派角色 → 兜底：**其它 key 类**的模型 > 同厂商其它模型 > 同模型高档 effort。
        # degraded 按**结果**判：审核者与写作者同 key 类（同限流池）才打折。
        far_pool = [dict(m) for m in settings_store.models()
                    if str(m.get("provider") or "") == pid and m.get("enabled", True)
                    and str(m.get("model") or "").strip()
                    and str(m.get("key_id") or "k1") != writer_key]
        near_pool = [dict(m) for m in settings_store.models()
                     if str(m.get("provider") or "") == pid and m.get("enabled", True)
                     and str(m.get("model") or "").strip()
                     and str(m.get("key_id") or "k1") == writer_key
                     and str(m.get("id") or "") != str(writer.get("id") or "")]
        used = {str(out.get(r, {}).get("id") or "") for r in ("reviewer1",)}
        m = next((x for x in far_pool if str(x.get("id")) not in used), None) \
            or next((x for x in near_pool if str(x.get("id")) not in used), None)
        if m:
            out[role] = m
            if str(m.get("key_id") or "k1") == writer_key:
                degraded = True              # 同限流池，独立性打折
            continue
        effs = [e for e in (writer.get("efforts") or ["none"]) if e in ("high", "max")] or ["max"]
        q = dict(writer)
        q["effort"] = effs[0] if role == "reviewer1" else (effs[1] if len(effs) > 1 else effs[0])
        q["id"] = "%s@%s" % (writer.get("id"), q["effort"])
        q["label"] = "%s（%s 档审核）" % (writer.get("label"), q["effort"])
        out[role] = q
        degraded = True
    if degraded:
        out["degraded"] = True
    return out


def complete_as(role: str, **kw) -> dict:
    """以指定角色发调用（写作者走老路径，审核者带自己的档案直通）。"""
    rs = roles()
    if not rs:
        return {"ok": False, "error": "多远端角色不可用（配置撑不起三方）", "answer": ""}
    p = rs.get(role)
    if not p:
        return {"ok": False, "error": "未知角色：%s" % role, "answer": ""}
    if role == "writer":
        # 写作者 = 老主路径（阶梯 + 归因 + 预算全都不变）
        from . import remote
        return remote.complete(**kw)
    from . import remote
    # 审核者裁决是死局的唯一出口，**用该档案的最强思考档**（不为省钱缩思考）；
    # effort 兜底链：显式 effort > 档案 efforts 的最后一档 > none。
    p = dict(p)
    if not p.get("effort"):
        _effs = [e for e in (p.get("efforts") or ["none"]) if e]
        p["effort"] = _effs[-1] if _effs else "none"
    kw.setdefault("node_id", "review_%s" % role)
    return remote.complete(profile=p, **kw)
