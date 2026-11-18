#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""死局协商与解锁登记（2026-11-18 用户口径落地）。

死局的形状（实测 2026-11-18 matching 域）：9 条绿能力与待实现能力挤在同一个
文件里，写作者一改就被回归护栏整批回滚，换打法无用 —— 机器自己破不了局。

机制（三方 = 写作者 + 审核者1 + 审核者2，**多数决**）：
  1. 唤醒：同一批能力**第二次**被回滚弄红时（首次回滚只记账，不打扰审核者）；
  2. 协商：失败判据的探针证据（请求/响应/期望）+ 回滚历史发给两名审核者，
     写作者默认投"解锁"（它是修复方案的提出者），**至少一名审核者同意**
     才构成 2/3 多数 → 解锁；两名都反对 → 保持锁定，一切照旧；
  3. 解锁不是绕过裁判：解锁轮护栏照跑，范围内能力红了**不回滚但留证**；
  4. 复审：写作者独改（风格一致）后，机械证据 + 三方复审，≥2 票过 →
     死局解决、审核者休眠；不过 → 整批回滚（用本轮备份），重协商或放弃；
  5. 放弃：同一唤醒最多协商 2 次，用完恢复原逻辑（护栏立即回滚、按振荡收工）。

铁律边界（不可破）：
  · 解锁必须留痕（unlock_log.jsonl 全事件落盘）；
  · 解锁范围内能力红**不是通过**，是"待裁决"——复审不过照样回滚；
  · 审核者只出意见，代码只能由写作者落盘；
  · 审核者调用失败 ≠ 同意（没有独立同意就没有解锁）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

MAX_NEGOTIATE_ATTEMPTS = 2          # 同一死局最多协商次数（用完放弃）
_FILE_NAME = "deadlock.json"
_LOG_NAME = "unlock_log.jsonl"


# ---------------------------------------------------------------- 落盘
def _dir(ws) -> Path:
    return Path(ws) / ".sixcoder"


def _path(ws) -> Path:
    return _dir(ws) / _FILE_NAME


def load_state(ws) -> dict:
    try:
        d = json.loads(_path(ws).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_state(ws, st: dict) -> None:
    _dir(ws).mkdir(parents=True, exist_ok=True)
    _path(ws).write_text(json.dumps(st, ensure_ascii=False, indent=1),
                         encoding="utf-8")


def _log(ws, event: dict) -> None:
    """全事件落盘（铁律：解锁必须留痕）。自检/离线失败只打印，不炸主流程。"""
    try:
        _dir(ws).mkdir(parents=True, exist_ok=True)
        event = dict(event)
        event.setdefault("at", time.strftime("%Y-%m-%d %H:%M:%S"))
        with (_dir(ws) / _LOG_NAME).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError as exc:                               # noqa: PERF203
        print("[deadlock] 日志写盘失败: %s" % exc)


def reset(ws) -> None:
    """会话开始时清状态（夜班每批新起 → 审核者休眠）。"""
    save_state(ws, {"state": "idle", "history": []})


# ---------------------------------------------------------------- 唤醒判定
def on_rollback(ws, regressed: list[str], files: list[str], round_no: int,
                detail: str, emit=None) -> dict:
    """回归护栏整批回滚后调用。返回 {"woke": bool, "decision": ...}。

    唤醒条件：本次弄红的能力里，有任一条**此前已被回滚弄红过**（同一批能力
    反复撞墙 = 死局的形状）。首次回滚只记账 —— 单次可能是普通手滑，不值得
    花审核者的钱（与 uiwatch"连续 3 轮才算故障"同一设计哲学：误报比漏报贵）。
    """
    st = load_state(ws)
    # 空工作区 load_state() 返回 {}（没有 state 键）——必须先把 None 归成 ""，
    # 否则 `None not in ("idle","")` 判真 → 首次回滚就误判"已在协商中"（实测）。
    if str(st.get("state") or "") not in ("idle", ""):
        # 已在协商/解锁流程中：这次回滚（解锁轮复审不过）在 on_round_result 里处理
        return {"woke": False, "state": st.get("state")}
    history = list(st.get("history") or [])
    reg = [str(c) for c in (regressed or []) if c]
    repeat = sorted(set(reg) & {c for h in history for c in (h.get("caps") or [])})
    history.append({"round": round_no, "caps": reg,
                    "files": [str(f) for f in (files or [])],
                    "at": time.strftime("%Y-%m-%d %H:%M:%S")})
    st["history"] = history[-12:]                        # 只留最近 12 次防膨胀
    if not repeat:
        save_state(ws, st)
        _log(ws, {"event": "rollback_recorded", "caps": reg, "round": round_no})
        return {"woke": False, "repeat": []}
    # 死局确认 → 唤醒审核者
    st.update({"state": "negotiate", "kind": "rollback", "caps": reg,
               "repeat_caps": repeat, "files": [str(f) for f in (files or [])],
               "round": round_no, "detail": str(detail or "")[:2000],
               "attempts": 0})
    save_state(ws, st)
    _log(ws, {"event": "deadlock_wake", "caps": reg, "repeat": repeat,
              "round": round_no})
    if emit:
        try:
            emit("deadlock_wake", {"caps": repeat, "round": round_no,
                                   "note": "同一批能力再次被回滚 → 唤醒审核者协商"})
        except Exception:
            pass
    decision = negotiate(ws, emit=emit)
    return {"woke": True, "repeat": repeat, "decision": decision}


def on_oscillation(ws, round_no: int, detail: str, caps: list[str] | None = None,
                   emit=None) -> dict:
    """**振荡收工前的最后一问**（2026-11-18 真实夜班暴露）。

    为什么需要：死局有两种形状——
      ① 改动被护栏回滚（on_rollback 覆盖）；
      ② 玩法用完、每轮都"写入 1 个文件但状态回到原样"（振荡收工）。
    实测 21:43-22:23 那一夜全是形状②：8 轮各写 1 个文件、没有一次回滚，
    死局钩子从未被问到 → 审核者全程休眠 → 又按老逻辑收工。这是同一场死局，
    判定层不该因为"没走回滚"就当它不存在。

    返回 {"continue": bool, "granted": bool, "decision": ...}：
      · 授予解锁 → continue=True（夜班继续跑，下一轮带解锁范围干活）；
      · 拒绝/审核者不可用 → continue=False（照旧收工叫人，不白烧）；
      · 已协商过 2 次 → 不再问（防止无限重试烧钱）。
    """
    st = load_state(ws)
    if str(st.get("state") or "") not in ("idle", ""):     # None 必须归成 ""
        return {"continue": False, "reason": "已在协商/解锁流程中",
                "state": st.get("state")}
    if int(st.get("osc_attempts") or 0) >= 2:
        return {"continue": False, "reason": "振荡协商已用过 2 次（放弃）"}
    st.update({"state": "negotiate", "kind": "oscillation",
               "caps": [str(c) for c in (caps or [])], "files": [],
               "round": int(round_no), "detail": str(detail or "")[:2000],
               "attempts": 0,
               "osc_attempts": int(st.get("osc_attempts") or 0) + 1})
    save_state(ws, st)
    _log(ws, {"event": "deadlock_wake", "kind": "oscillation",
              "round": round_no, "caps": st["caps"],
              "detail": str(detail or "")[:400]})
    if emit:
        try:
            emit("deadlock_wake", {"kind": "oscillation", "round": round_no,
                                   "caps": st["caps"],
                                   "note": "打法用完仍无净效果 → 死局协商（三方裁决是否解锁重试）"})
        except Exception:
            pass
    decision = negotiate(ws, emit=emit)
    if decision.get("granted"):
        return {"continue": True, "granted": True, "decision": decision}
    if decision.get("decision") == "fix_judge":
        # 改判据决议：夜班该**收工但不叫人** —— 看门狗 10 秒后自动拉起新进程，
        # 新判据在开跑时加载生效（蓝图不热重载，见 negotiate 里的说明）。
        st = load_state(ws)
        st["state"] = "idle"
        save_state(ws, st)
        return {"continue": False, "granted": False, "judge_fixed": True,
                "changed": decision.get("judge_fixed", 0),
                "notes": decision.get("judge_fix_notes") or [],
                "backup": decision.get("judge_fix_backup") or "",
                "decision": decision}
    st = load_state(ws)
    st["state"] = "idle"
    save_state(ws, st)
    return {"continue": False, "granted": False, "decision": decision}


# ---------------------------------------------------------------- 协商
def _packet(ws, st: dict) -> str:
    """协商提示词：给审核者**裁决所需的最小事实集**，不给代码全文。"""
    from . import blueprint as _bpmod
    caps_detail = []
    try:
        bp = _bpmod.load(Path(ws))
        for c in (getattr(bp, "caps", None) or []):
            cid = str(getattr(c, "id", "") or "")
            if cid in (st.get("caps") or []):
                pr = getattr(c, "probe", None) or {}
                raw_steps = pr.get("steps") if isinstance(pr, dict) else None
                # **带编号**给审核者（2026-11-19）：处方用 "第几步" 定位，
                # 不给编号等于让它们猜序号 → 补丁必错位。
                numbered = []
                for i, stp in enumerate(raw_steps or [], 1):
                    if not isinstance(stp, dict):
                        continue
                    item = {"n": i}
                    for k in ("method", "path", "json", "expect_status",
                              "expect_json", "body_has", "body_not_has", "save"):
                        if k in stp:
                            v = stp[k]
                            item[k] = (json.dumps(v, ensure_ascii=False)[:220]
                                       if isinstance(v, (dict, list)) else str(v)[:120])
                    numbered.append(item)
                caps_detail.append({"id": cid,
                                    "desc": str(getattr(c, "desc", "") or "")[:200],
                                    "steps": numbered})
    except Exception as exc:                             # noqa: BLE001
        caps_detail = [{"error": "蓝图读取失败: %s" % type(exc).__name__}]
    return json.dumps({
        "死局描述": "写作者为修复缺口提交的改动，把以下已通过能力弄红，回归护栏整批回滚。"
                   "同一批能力已被回滚弄红多次，换打法无效 —— 机器已无法自行收敛。",
        "被弄红能力": st.get("caps") or [],
        "反复出现": st.get("repeat_caps") or [],
        "判据定义": caps_detail,
        "护栏细节": st.get("detail") or "",
        "本轮被回滚的文件": st.get("files") or [],
        "回滚历史": st.get("history") or [],
        "问题": "是否解锁，允许写作者对这些文件做一次**有监督**的修改？"
               "解锁不等于通过：改完仍需三方复审，复审不过照样回滚。"
               "若你认为症结在判据本身（判据把两个关注点耦合/期望值过时），也请说明。",
        "输出格式": '{"verdict":"unlock|keep|fix_judge","reason":"<一句话>",'
                     '"judge_fix":"<人话说明>","judge_patch":[<操作>, ...]}',
        "引擎立场（2026-11-19 用户定稿）": "引擎**不做任何项目假设**：它不知道"
            "谁是教师/学生，也不认识任何具体业务。开放性问题（判据该怎么写、"
            "期望值是否合理、步骤要不要增删）由你们三方判断并给处方，引擎只做"
            "**安全边界**（只碰判据、不碰产物代码）。宁可你们多改，也不要本地"
            "写死规则——那是过拟合，换一个项目就失效。",
        "judge_patch 处方格式（2026-11-19：远端直接开处方，引擎校验后机械执行）":
            "每条操作用 step 精确定位（步骤编号见上面 steps 的 n）。可用操作："
            + chr(10).join([
                '  {"op":"delete_step","cap":"cap.x","step":4}',
                '  {"op":"set_field","cap":"cap.x","step":5,"field":"body_has","value":["真标题"]}',
                '  {"op":"set_field","cap":"cap.x","step":7,"field":"expect_status","value":[409]}',
                '  {"op":"set_json_key","cap":"cap.x","step":6,"field":"expect_json","key":"status","value":"done"}',
                '  {"op":"del_json_key","cap":"cap.x","step":6,"field":"expect_json","key":"teacher_username"}',
                '  {"op":"add_step","cap":"cap.x","step":3,"value":{"method":"POST","path":"/api/x/","expect_status":[201]}}',
                '  {"op":"set_cap","cap":"cap.x","field":"desc","value":"修正后的能力描述"}',
                '  {"op":"replace_probe","cap":"cap.x","value":{"kind":"http","steps":[]}}',
            ])
            + chr(10) + "规则：只改判据（探针/描述），不碰产物代码；"
            "同一冲突两名审核者给出一致操作才执行（不一致一律跳过）。拿不准就投 keep。",
        "第三票 fix_judge（2026-11-18 用户口径）": "如果你判断症结在判据本身（题目自相"
            "矛盾——同一件事一条要成功一条要失败、同一端点两份 status 词表、夹具里没有"
            "断言要的数据），投 fix_judge 并在 judge_fix 写清怎么改。写作者与两名审判官"
            "各一票，任一选项拿到 ≥2 票即成决议：unlock=放行修复、fix_judge=改判据后重跑、"
            "keep=收工等人。",
    }, ensure_ascii=False)


def _parse_verdict(text: str) -> dict:
    """从答复里抠出裁决 JSON（模型爱加围栏/说明，兜住）。抠不出 = 弃权→反对。"""
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


def _ask_reviewer(role: str, packet: str) -> dict:
    try:
        from . import multiremote
        # 参数必须与 remote.complete 的真实签名一致（2026-11-18 实测坑：
        # 多传它不认识的参数会直接 TypeError，被吞成 reviewer unavailable → 永远
        # 没有独立同意 → 死局永远解不开。接口漂移一律按"恒失败"处理最贵）。
        return multiremote.complete_as(
            role, prompt=packet, task_id="deadlock", node_id="deadlock_%s" % role,
            json_mode=True)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def negotiate(ws, emit=None) -> dict:
    """三方协商。写作者默认投解锁（提案人）；≥1 名审核者同意 = 2/3 多数。"""
    st = load_state(ws)
    st["attempts"] = int(st.get("attempts") or 0) + 1
    packet = _packet(ws, st)
    votes = {"writer": "unlock", "writer_note": "提案人默认投解锁"}
    reasons = {}
    for role in ("reviewer1", "reviewer2"):
        r = _ask_reviewer(role, packet)
        v = _parse_verdict(str(r.get("answer") or "")) if r.get("ok") else {}
        verdict = str(v.get("verdict") or "").strip().lower()
        # 审核者调用失败或给不出可解析的裁决 = **没有独立同意** → 记 unavailable
        if verdict not in ("unlock", "keep", "fix_judge"):
            verdict = "unavailable"
        votes[role] = verdict
        _patch = v.get("judge_patch")
        reasons[role] = {"reason": str(v.get("reason") or "")[:300],
                         "judge_fix": str(v.get("judge_fix") or "")[:300],
                         "judge_patch": _patch if isinstance(_patch, list) else [],
                         "error": "" if r.get("ok") else str(r.get("error") or "")[:200]}
    st["votes"] = votes
    st["vote_reasons"] = reasons

    # **三选一多数决**（2026-11-18 用户口径：审判官和写作者共同投票决定改判据）：
    #   任一选项拿到 ≥2 票 = 决议。fix_judge 优先判定（它是"判据有病"的更强证据：
    #   两名独立审核者都指向判据时，放行写作者去改代码等于让他背锅）。
    tally = {k: sum(1 for v in votes.values() if v == k)
             for k in ("unlock", "keep", "fix_judge")}
    decision = ""
    for opt in ("fix_judge", "unlock"):        # fix_judge 优先
        if tally.get(opt, 0) >= 2:
            decision = opt
            break
    if not decision:
        decision = "keep"

    out = {"granted": False, "votes": votes, "tally": tally, "decision": decision}
    if decision == "unlock":
        st["state"] = "writer_fix"
        st["unlock"] = {"caps": list(st.get("caps") or []),
                        "files": list(st.get("files") or []),
                        "round": int(st.get("round") or 0)}
        _log(ws, {"event": "deadlock_unlock", "caps": st.get("caps"),
                  "votes": votes, "round": st.get("round")})
        out["granted"] = True
    elif decision == "fix_judge":
        # **改判据决议**：只做确定性修复（拆同角色账号 / 统一 status 词表），
        # 不花钱、不猜；改完蓝图落盘（自动备份）。夜班**正常收工但不叫人**，
        # 看门狗 10 秒后自动拉起新进程 —— 蓝图只在开跑时加载一次，
        # 重启才能让新判据生效（这是"能当场生效"与"改动可信"的取舍）。
        st["state"] = "judge_fix"
        _log(ws, {"event": "deadlock_judge_fix_vote", "votes": votes,
                  "tally": tally, "round": st.get("round"),
                  "judge_fix_notes": [reasons.get(r, {}).get("judge_fix", "")
                                      for r in ("reviewer1", "reviewer2")]})
        try:
            from . import judgefix as _jf
        except Exception as exc:                        # noqa: BLE001
            _jf = None
            _res = {"changed": 0, "error": "%s: %s" % (type(exc).__name__, exc)}
        total, notes, backups, skipped = 0, [], [], []
        # ① 本地确定性规则打底（免费、幂等）：把"机械可识别"的冲突一次清掉
        if _jf is not None:
            try:
                _res = _jf.apply(ws)
                total += int(_res.get("changed") or 0)
                notes.extend(_res.get("notes") or [])
                if _res.get("backup"):
                    backups.append(str(_res["backup"]))
            except Exception as exc:                    # noqa: BLE001
                notes.append("本地规则失败：%s: %s" % (type(exc).__name__, exc))
        # ② **远端处方**（2026-11-19 用户口径）：审核者直接开处方，引擎只校验+执行。
        #    两名独立审核者**一致的**操作才执行（多远端一致 = 保险），冲突跳过。
        _patches = [(reasons.get(r) or {}).get("judge_patch") or []
                    for r in ("reviewer1", "reviewer2")]
        _ops, _mnotes = [], []
        if _jf is not None and any(_patches):
            _ops, _mnotes = _jf.merge_patches(_patches, ws=ws)
            if _ops:
                _pr = _jf.apply_patch(ws, _ops)
                total += int(_pr.get("changed") or 0)
                notes.extend(_pr.get("notes") or [])
                if _pr.get("backup"):
                    backups.append(str(_pr["backup"]))
                skipped.extend(_pr.get("skipped") or [])
            notes.extend(_mnotes)
        out["judge_fixed"] = total
        out["judge_fix_notes"] = notes[:24]
        out["judge_fix_skipped"] = skipped[:12]
        out["judge_fix_backup"] = ",".join(backups)
        out["judge_patch_ops"] = len(_ops)
        st["state"] = "idle"
        _log(ws, {"event": "deadlock_judge_fix_applied",
                  "changed": out["judge_fixed"],
                  "patch_ops": out.get("judge_patch_ops", 0),
                  "skipped": out.get("judge_fix_skipped") or [],
                  "notes": out["judge_fix_notes"],
                  "backup": out["judge_fix_backup"]})
    else:
        st["state"] = "idle"
        st["unlock"] = None
        _log(ws, {"event": "deadlock_keep_locked", "votes": votes})
    save_state(ws, st)
    if emit:
        try:
            emit("deadlock_vote", {"votes": votes, "tally": tally,
                                   "decision": decision,
                                   "granted": out["granted"],
                                   "judge_fixed": out.get("judge_fixed", 0),
                                   "caps": st.get("caps") or []})
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- 解锁查询
def unlock_active(ws, round_no: int | None = None) -> dict | None:
    """当前有解锁范围 → 返回 {"caps": [...], "files": [...], "round": N}；否则 None。

    wholerepo 的回归护栏用它：范围内的能力红 → 不回滚但留证（待复审）；
    **范围外照旧立即回滚** —— 解锁不放行无关改动。
    `round_no`（可选）：解锁轮次过期保护——解锁后 3 轮内没走完复审就作废，
    防"解锁了但那轮远端没写任何东西"把状态机永远挂在 writer_fix 上。
    """
    st = load_state(ws)
    if st.get("state") != "writer_fix":
        return None
    u = st.get("unlock")
    if not isinstance(u, dict):
        return None
    u_round = int(u.get("round") or 0)
    if round_no is not None and u_round and int(round_no) > u_round + 3:
        st.update({"state": "idle", "unlock": None, "expired": True})
        save_state(ws, st)
        _log(ws, {"event": "deadlock_expired", "round": round_no,
                  "unlock_round": u_round})
        return None
    return {"caps": [str(c) for c in (u.get("caps") or [])],
            "files": [str(f) for f in (u.get("files") or [])],
            "round": u_round}


# ---------------------------------------------------------------- 复审
def on_round_result(ws, round_no: int, regressed: list[str], green: list[str],
                    backup: str, rootcause: str, emit=None) -> dict:
    """解锁轮落盘后调用（写作者独改完毕）。三方复审，多数决。"""
    st = load_state(ws)
    if st.get("state") != "writer_fix":
        return {"acted": False}
    scoped = [str(c) for c in (st.get("unlock") or {}).get("caps") or []]
    still_red = [c for c in scoped if c in set(regressed or [])]
    now_green = [c for c in scoped if c in set(green or [])]
    # 写作者一票：机械证据是绿的才投 pass（它的"主张"必须先过机器这一关）
    writer_pass = not still_red
    votes = {"writer": "pass" if writer_pass else "fail"}
    reasons = {"writer": {"reason": ("解锁范围内能力全绿" if writer_pass
                                     else "仍红：%s" % ", ".join(still_red[:6]))}}
    agree = 1 if writer_pass else 0
    packet = json.dumps({
        "复审对象": "写作者在解锁范围内的修改。机械探针已重跑，结果如下。",
        "解锁范围能力": scoped,
        "机械结果": {"仍红": still_red, "通过": now_green},
        "写作者说明": str(rootcause or "")[:1500],
        "问题": "这次修改是否可以接受？（unlock 期间弄红的不算通过，但如果修改"
               "在结构上已解决死局、剩余红是判据本身要修订，可投 pass 并说明要改哪条判据）",
        "输出格式": '{"verdict":"pass|fail","reason":"<一句话>","judge_fix":"<若判据要改，改什么>"}',
    }, ensure_ascii=False)
    for role in ("reviewer1", "reviewer2"):
        r = _ask_reviewer(role, packet)
        v = _parse_verdict(str(r.get("answer") or "")) if r.get("ok") else {}
        verdict = str(v.get("verdict") or "").strip().lower()
        if verdict not in ("pass", "fail"):
            # 审核者缺席：**机械证据兜底**——范围内全绿算过，有红算不过。
            verdict = "pass" if writer_pass else "fail"
            reasons[role] = {"reason": "审核者缺席，按机械证据判",
                             "error": "" if r.get("ok") else str(r.get("error") or "")[:200]}
        else:
            reasons[role] = {"reason": str(v.get("reason") or "")[:300],
                             "judge_fix": str(v.get("judge_fix") or "")[:300]}
        votes[role] = verdict
        if verdict == "pass":
            agree += 1
    resolved = agree >= 2                              # 3 票里 ≥2 = 多数
    out = {"acted": True, "resolved": resolved, "votes": votes,
           "still_red": still_red, "restore_backup": None}
    if resolved:
        st.update({"state": "idle", "unlock": None, "resolved_round": round_no,
                   "resolved_caps": scoped})
        _log(ws, {"event": "deadlock_resolved", "caps": scoped,
                  "votes": votes, "round": round_no})
    else:
        out["restore_backup"] = backup or None
        st["attempts"] = int(st.get("attempts") or 0)
        if int(st.get("attempts") or 0) >= MAX_NEGOTIATE_ATTEMPTS:
            st.update({"state": "idle", "unlock": None, "giveup": True})
            _log(ws, {"event": "deadlock_giveup", "caps": scoped,
                      "votes": votes, "round": round_no})
        else:
            # 复审不过 → **立即重协商**（attempts 在 negotiate 里 +1；
            # 不立即重协商会卡死在 negotiate 态——下一次回滚进来只会早退）。
            st["state"] = "negotiate"
            save_state(ws, st)
            _log(ws, {"event": "deadlock_review_failed", "caps": scoped,
                      "votes": votes, "round": round_no})
            return {**out, "renegotiate": negotiate(ws, emit=emit)}
    st["last_votes"] = votes
    st["last_reasons"] = reasons
    save_state(ws, st)
    if emit:
        try:
            emit("deadlock_review", {"votes": votes, "resolved": resolved,
                                     "caps": scoped})
        except Exception:
            pass
    return out
