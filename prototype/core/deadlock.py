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
# 协商态陈旧阈值（秒）：state=negotiate 且 wake_at 超过它 = 协商中途被中断
# （进程被杀/远端长时间无响应），钩子会一直判"已在协商中"→ 三方永久休眠。
# 10 分钟足够容纳两次远端审核者调用（实测单次 <1 分钟），不会误伤进行中的协商。
STALE_NEGOTIATE_SEC = 600
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
        if not isinstance(d, dict):
            return {}
        # 陈旧协商态自愈（2026-11-19）：协商中途进程被杀/远端挂起时，state 停在
        # negotiate，三个钩子开头都判"已在协商中"直接返回 → 三方永久休眠。
        # 唤醒时写 wake_at；这里读到超时未决的协商态就复位 idle，让钩子恢复。
        # 只对**明确带 wake_at 且超时**的状态动手：不带时间戳的历史 state 不猜，
        # 避免误伤合法协商或旧数据（宁可漏自愈，不可误复位进行中的协商）。
        if str(d.get("state") or "") == "negotiate" and d.get("wake_at"):
            try:
                if time.time() - float(d["wake_at"]) > STALE_NEGOTIATE_SEC:
                    _log(ws, {"event": "deadlock_stale_recovered",
                              "kind": d.get("kind"), "round": d.get("round"),
                              "wake_at": d.get("wake_at"),
                              "note": "协商态超时未决（>10分钟）→ 自动复位 idle，"
                                      "允许死局钩子重新唤醒三方"})
                    d["state"] = "idle"
                    save_state(ws, d)
            except (TypeError, ValueError):
                pass                                     # wake_at 脏数据：不复位
        return d
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
               "attempts": 0, "wake_at": time.time()})
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
               "attempts": 0, "wake_at": time.time(),
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
    try:
        decision = negotiate(ws, emit=emit)
    except BaseException as exc:                     # noqa: BLE001
        # **状态必须回到 idle**（2026-11-19 实测：negotiate 抛 IndexError 后
        # 状态停在 negotiate，下一轮 on_oscillation 直接判"已在协商流程中"
        # 返回 continue=False → 再也没人问三方，整夜机制失效）。
        _tb = "".join(__import__("traceback").format_exception(
            type(exc), exc, exc.__traceback__))
        _log(ws, {"event": "deadlock_error", "where": "negotiate",
                  "error": "%s: %s" % (type(exc).__name__, exc),
                  "traceback": _tb[-2000:]})
        st2 = load_state(ws)
        st2["state"] = "idle"
        save_state(ws, st2)
        raise
    if decision.get("granted"):
        return {"continue": True, "granted": True, "decision": decision}
    if decision.get("decision") == "fix_judge":
        # 改判据决议：夜班该**收工但不叫人** —— 看门狗 10 秒后自动拉起新进程，
        # 新判据在开跑时加载生效（蓝图不热重载，见 negotiate 里的说明）。
        st = load_state(ws)
        st["state"] = "idle"
        save_state(ws, st)
        _changed = int(decision.get("judge_fixed") or 0)
        if _changed == 0:
            # **处方为空时别只留一句"收工"**（2026-11-19 实测：两位审核者都投
            # fix_judge，但只给了人话没给结构化处方 → 引擎一处也改不了 → 收工，
            # 人的案头什么都没有）。把它们的意见落成一份可读 TODO，下次开工前
            # 照着改；同时如实记一条 error 级留痕，便于统计"处方缺失率"。
            try:
                _todo = _write_judge_todo(ws, decision)
            except BaseException:                        # noqa: BLE001
                _todo = ""
            _log(ws, {"event": "deadlock_judge_fix_empty",
                      "round": int(st.get("round") or 0),
                      "todo": _todo,
                      "reasons": {r: (decision.get("vote_reasons") or {}).get(r, {}).get("judge_fix", "")
                                  for r in ("reviewer1", "reviewer2")}})
        return {"continue": False, "granted": False, "judge_fixed": True,
                "changed": _changed,
                "notes": decision.get("judge_fix_notes") or [],
                "backup": decision.get("judge_fix_backup") or "",
                "decision": decision}
    st = load_state(ws)
    st["state"] = "idle"
    save_state(ws, st)
    return {"continue": False, "granted": False, "decision": decision}


# 连续无增长轮数阈值：达到就问三方（别等"策略走完+连续3轮"那条永远够不着的收工线）。
# 2026-11-19 实测：改的都是 absent 能力（弄红不了已通过的能力 → 不回滚）、
# 每轮换不同文件（振荡指纹一直变 → 不算振荡），on_rollback/on_oscillation 都够不着，
# 于是 done 连 8 轮卡 67 却一次协商都没触发。停滞必须自己成为第三形状。
STALL_WAKE_ROUNDS = 3
STALL_REPEAT_FILES = 2          # 根因台账里同一文件出现 >= 这个次数 = "反复改同一文件"


def _repeated_files(ws, lookback: int = 8) -> list[str]:
    """根因台账最近 N 轮里被反复改动的文件（次数 >= STALL_REPEAT_FILES）。

    这是"机器在原地打转"的直接证据：同一批文件被改了又改，端到端数却不动。
    只读，不写盘；台账缺失/损坏时返回 []（不阻断停滞触发本身）。
    """
    import collections
    try:
        from . import wholerepo as _wr
        hist = _wr.load_history(ws)[-lookback:] or []
    except Exception:                                    # noqa: BLE001
        return []
    cnt = collections.Counter()
    for rec in hist:
        if not isinstance(rec, dict):
            continue
        for f in (rec.get("files") or []):
            if f:
                cnt[str(f)] += 1
    return sorted(f for f, n in cnt.items() if n >= STALL_REPEAT_FILES)


def on_stall(ws, round_no: int, detail: str, caps: list[str] | None = None,
             stall_rounds: int = 0, emit=None) -> dict:
    """**停滞型死局唤醒**（2026-11-19）：端到端连续多轮不涨 + 根因反复改同一批文件。

    为什么需要第三个形状：
      · on_rollback 只在"把已通过能力弄红并回滚"时触发 —— 卡住的能力本就是
        absent，改它们弄红不了绿能力，这条永远不触发；
      · on_oscillation 靠"状态指纹回到来过的点"—— 每轮改不同文件、fingerprint
        一直在变，也不算振荡；
      · 结果：done 连 8 轮卡死，三方全程休眠，只在本地 rootcause 循环里烧钱。
    本函数由 company 的停滞判定（stall_rounds 跨策略累计）调用，走**同一套**
    negotiate()（协商包已含真实失败现象快照），返回结构与 on_oscillation 一致，
    company 可原样复用决议分派。

    返回 {"continue": bool, "granted": bool, ...}；最多协商 2 次（独立计数）。
    """
    st = load_state(ws)
    if str(st.get("state") or "") not in ("idle", ""):
        return {"continue": False, "reason": "已在协商/解锁流程中",
                "state": st.get("state")}
    if int(st.get("stall_attempts") or 0) >= MAX_NEGOTIATE_ATTEMPTS:
        return {"continue": False, "reason": "停滞协商已用过 2 次（放弃）"}
    repeat_files = _repeated_files(ws)
    st.update({"state": "negotiate", "kind": "stall",
               "caps": [str(c) for c in (caps or [])], "files": repeat_files,
               "round": int(round_no), "detail": str(detail or "")[:2000],
               "attempts": 0, "wake_at": time.time(),
               "stall_rounds": int(stall_rounds),
               "stall_attempts": int(st.get("stall_attempts") or 0) + 1})
    save_state(ws, st)
    _log(ws, {"event": "deadlock_wake", "kind": "stall",
              "round": round_no, "stall_rounds": stall_rounds,
              "caps": st["caps"], "repeat_files": repeat_files,
              "detail": str(detail or "")[:400]})
    if emit:
        try:
            emit("deadlock_wake", {"kind": "stall", "round": round_no,
                                   "stall_rounds": stall_rounds,
                                   "caps": st["caps"], "repeat_files": repeat_files,
                                   "note": "端到端连续不涨且反复改同一批文件 → "
                                           "死局协商（三方对着真实失败现象裁决）"})
        except Exception:                                # noqa: BLE001
            pass
    try:
        decision = negotiate(ws, emit=emit)
    except BaseException as exc:                         # noqa: BLE001
        # 状态必须回 idle（与 on_oscillation 同样的血泪：异常卡在 negotiate
        # 会让后续所有唤醒都判"已在流程中"→ 机制整夜失效）。
        _tb = "".join(__import__("traceback").format_exception(
            type(exc), exc, exc.__traceback__))
        _log(ws, {"event": "deadlock_error", "where": "negotiate(stall)",
                  "error": "%s: %s" % (type(exc).__name__, exc),
                  "traceback": _tb[-2000:]})
        st2 = load_state(ws)
        st2["state"] = "idle"
        save_state(ws, st2)
        raise
    if decision.get("granted"):
        return {"continue": True, "granted": True, "decision": decision}
    if decision.get("decision") == "fix_judge":
        st = load_state(ws)
        st["state"] = "idle"
        save_state(ws, st)
        _changed = int(decision.get("judge_fixed") or 0)
        if _changed == 0:
            try:
                _write_judge_todo(ws, decision)
            except BaseException:                        # noqa: BLE001
                pass
            _log(ws, {"event": "deadlock_judge_fix_empty", "kind": "stall",
                      "round": int(st.get("round") or 0)})
        return {"continue": False, "granted": False, "judge_fixed": True,
                "changed": _changed,
                "notes": decision.get("judge_fix_notes") or [],
                "backup": decision.get("judge_fix_backup") or "",
                "decision": decision}
    st = load_state(ws)
    st["state"] = "idle"
    save_state(ws, st)
    return {"continue": False, "granted": False, "decision": decision}


# ---------------------------------------------------------------- 协商
def _write_judge_todo(ws, decision: dict) -> str:
    """处方为空时，把两位审核者的**人话诊断**落成一份可读 TODO。

    为什么需要（2026-11-19 实夜）：03:41 第 9 轮三方投票 fix_judge 2:1 通过，
    但两名审核者只给了 `judge_fix` 文本、没给 `judge_patch` 结构化处方 →
    `judgefix` 一处也改不了（changed=0）→ 夜班按"0 改动必叫人"收工。
    人的案头上只剩一句"判据有病"，**具体要改什么全在 unlock_log 的 JSON 里**，
    没人会去翻。这份 md 就是把它摊平：

        F:\\WorkSpace\\<项目>\\.sixcoder\\judge_fix_todo.md

    内容只陈述审核者说了什么，**不自动改任何东西**（判断权在远端/人）。
    """
    reasons = (decision.get("vote_reasons") or {})
    lines = ["# 判据待修清单（三方投票认定判据有病，但未给出可执行处方）",
             "",
             "> 由 `core/deadlock.py` 在收工时自动生成（2026-11-19）。",
             "> 投票结果：" + json.dumps(decision.get("tally") or {}, ensure_ascii=False),
             "> 票型：" + json.dumps(decision.get("votes") or {}, ensure_ascii=False),
             ""]
    for role in ("reviewer1", "reviewer2"):
        r = reasons.get(role) or {}
        lines += ["## %s" % role,
                  "",
                  "- 理由：%s" % (str(r.get("reason") or "（空）")),
                  "- 建议改法：%s" % (str(r.get("judge_fix") or "（空）")),
                  "- 处方条数：%s" % (len(r.get("judge_patch") or [])),
                  "- 调用错误：%s" % (str(r.get("error") or "（无）")),
                  ""]
    lines += ["## 怎么处理",
              "",
              "1. 照上面的建议修改 `.sixcoder/blueprint.json` 里对应能力的 `probe.steps`；",
              "2. 或重跑夜班——若审核者这次给出 `judge_patch`，引擎会自动执行并备份；",
              "3. 改完可跑 `python cli.py full --repo <项目>` 单独验证判据（不花钱、不改代码）。",
              ""]
    p = _dir(ws) / "judge_fix_todo.md"
    _dir(ws).mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


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

    # **真实失败现象**（2026-11-19 用户口径：协商机制必须有权限看到真实失败现象）。
    # 血泪：原先协商包只给"判据定义 + 回滚叙事"，两名审核者在信息不全的框架里
    # 做了正确推理却一直改错对象（改判据文本，而非夹具/产物），端到端连续 6 轮
    # 卡 67。失败现象（actual/expected + 失败步骤的真实请求/响应）由 coverage
    # 在每轮观测后落成快照，这里按被弄红能力读回来，和判据定义并排发。
    failures_detail: list[dict] = []
    try:
        from . import coverage as _covmod
        _fails = _covmod.load_failures(ws, st.get("caps") or [])
        for _cid, _row in (_fails or {}).items():
            failures_detail.append({"cap": _cid,
                                    "实际现象": str((_row or {}).get("detail") or "")[:3000]})
    except Exception as exc:                             # noqa: BLE001
        failures_detail = [{"error": "失败现象读取失败: %s" % type(exc).__name__}]

    # **项目方诊断**（2026-11-19）：项目根 `.sixcoder/advice.md` 有人工已定位的结论时
    # 一并发给审核者。动机：有些失败是"产物里一行键优先级写错"这类**人类一眼能定位、
    # 机器推不出来**的问题（pre_filter 的 id/code 就是），审核者只能对着
    # `0 vs 1` 改 5 轮。引擎不含项目知识，只负责"有就带上"。
    _advice = ""
    try:
        from . import wholerepo as _wr
        _advice = _wr.load_advice(ws)
    except Exception:                                    # noqa: BLE001
        _advice = ""

    # **缺失工具**（2026-11-19 用户口径：自主工具库）。审核者若判断产物本身没错、
    # 而是环境缺连接器/工具链/辅助脚本，可开 install_tool 处方让引擎现装工具。
    # 这里只是把"本机缺什么 + 已装什么"摆到审核者面前；装什么、怎么装由它们定。
    missing_tools: dict = {"未就绪的工具链": [], "已装工具": []}
    try:
        from . import recon as _recon
        for _tc in (_recon.toolchains() or []):
            if not _tc.get("ok"):
                missing_tools["未就绪的工具链"].append({
                    "id": _tc.get("id"),
                    "提示": str(_tc.get("fix_hint") or "本机不可用")[:160]})
    except Exception as exc:                             # noqa: BLE001
        missing_tools["未就绪的工具链"] = [{"error": type(exc).__name__}]
    try:
        from . import browser as _browser
        _bok, _bwhy = _browser.available()
        if not _bok:
            missing_tools.setdefault("未就绪的工具链", []).append(
                {"id": "browser", "提示": str(_bwhy)[:160]})
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import tool_lib as _tl
        missing_tools["已装工具"] = [
            {"name": r.get("name"), "provides": r.get("provides")}
            for r in (_tl.list_tools() or [])]
    except Exception:                                    # noqa: BLE001
        pass
    # 已隔离安装的环境包 + 各生态最快国内镜像源（让审核者知道"能不能装、从哪装"）
    try:
        from . import envinstall as _eimod
        missing_tools["已装环境包"] = [
            {"manager": r.get("manager"), "packages": r.get("packages"),
             "index": r.get("index", "")}
            for r in (_eimod.list_envs() or [])]
        _mirror_env: dict = {}
        for _eco in ("pip", "hf", "go", "npm"):
            try:
                _mirror_env.update(_eimod.env_vars(_eco, force=False) or {})
            except Exception:                            # noqa: BLE001
                continue
        missing_tools["国内镜像最快源"] = _mirror_env
    except Exception:                                    # noqa: BLE001
        pass
    # 已装生态包（补全的项目类型能力）+ 引擎当前支持的栈
    try:
        from . import ecosystem as _ecomod2
        from . import stack as _stackmod
        missing_tools["已装生态包"] = [
            {"name": r.get("name"), "kind": r.get("kind"),
             "provides": r.get("provides")}
            for r in (_ecomod2.list_packs() or [])]
        missing_tools["当前支持的栈"] = list(_stackmod.names())
    except Exception:                                    # noqa: BLE001
        pass

    # **待补全的生态包**（2026-11-24）：澄清阶段用户点名要的栈引擎没有，
    # 会登记在 .sixcoder/pending_stacks.json。这里读出来交给审核者，
    # 它们可以开 install_ecosystem 处方让远端现写一个 StackAdapter。
    #
    # 为什么放在这里：原来是「只有死局才协商」，而缺栈不表现为卡住
    # （引擎按默认栈生成、判据全绿）-> 这个机制从没被触发过。
    pending_stacks: list = []
    try:
        _pf = Path(ws) / ".sixcoder" / "pending_stacks.json"
        if _pf.is_file():
            pending_stacks = json.loads(_pf.read_text(encoding="utf-8")) or []
            if pending_stacks:
                missing_tools["待补全的栈（用户点名要、引擎没有）"] = [
                    {"stack": str(x.get("stack") or ""),
                     "why": str(x.get("reason") or "")[:200]}
                    for x in pending_stacks if isinstance(x, dict)]
                missing_tools["补全办法"] = (
                    "对每一个待补全的栈开 install_ecosystem 处方：写一个 StackAdapter 子类，"
                    "声明 entry_path / module_file_fmt / detect 特征；"
                    "适配器禁止 import os/subprocess/socket/shutil/importlib，"
                    "禁止 open/exec/eval/write_text（写盘由引擎做）。"
                    "**需要 3/3 全票**才会真正安装。")
    except Exception:                                # noqa: BLE001
        pending_stacks = []

    return json.dumps({
        "死局描述": "写作者为修复缺口提交的改动，把以下已通过能力弄红，回归护栏整批回滚。"
                   "同一批能力已被回滚弄红多次，换打法无效 —— 机器已无法自行收敛。",
        "被弄红能力": st.get("caps") or [],
        "反复出现": st.get("repeat_caps") or [],
        "判据定义": caps_detail,
        "真实失败现象": failures_detail,   # 探针实际跑出来的，先看这个再下结论
        "项目方诊断": _advice,             # 人工已定位的结论（有就必看，别重新猜）
        "缺失工具与已装工具": missing_tools,  # 可开 install_tool 处方现装工具
        "护栏细节": st.get("detail") or "",
        "本轮被回滚的文件": st.get("files") or [],
        "回滚历史": st.get("history") or [],
        "问题": "是否解锁，允许写作者对这些文件做一次**有监督**的修改？"
               "解锁不等于通过：改完仍需三方复审，复审不过照样回滚。"
               "若你认为症结在判据本身（判据把两个关注点耦合/期望值过时），也请说明。",
        "诊断方法（2026-11-19 用户口径，务必按此归因）":
            "**先对照「真实失败现象」里每条的 实际 vs 期望，再下结论**——"
            "不要只凭「判据定义」的文本推断。实测教训：连续 6 轮审核者只盯着判据文本"
            "（互斥/截断）改，端到端一直卡住；真正的靶子是 `total 实际3 期望2`、"
            "`unacceptable_count 实际0 期望1` 这类**计数值对不上**。按三类归因："
            "① 夹具/数据准备错（前置步骤没造出判据断言要的数据，如计数差 ±1）→ "
            "用 add_step/set_field 补夹具步骤，或 replace_probe 重写整条探针；"
            "② 判据文本错（期望值过时/自相矛盾/JSON 截断）→ 改断言；"
            "③ 产物代码错（404 路由未挂、该 409 却 200、业务层假绿）→ 投 unlock "
            "让写作者改代码，别硬改判据去迁就坏实现。同一批能力若上轮改过判据仍红，"
            "优先怀疑①或③，不要再改一遍判据期望值（追移动靶）。"
            "④ **环境缺工具**（对照「缺失工具与已装工具」段：产物逻辑没错，只是"
            "本机缺某连接器/适配脚本/辅助工具，探针根本跑不起来）→ 投 fix_judge 并"
            "开 install_tool 处方，让引擎把工具现装进本地工具库（装完引擎会复验，"
            "rc≠0 自动回滚）。注意：工具链（go/cargo/playwright 等系统软件）引擎"
            "不自动装包，只能装「代码+自检脚本」形态的工具。",
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
                '  {"op":"install_tool","cap":"cap.x","tool":{"name":"纯英文小写名",'
                '"description":"干什么","provides":["它补上哪个缺工具信号/能力"],'
                '"files":{"entry.py":"<工具源码>"},'
                '"verify":["python","-c","import entry; assert entry.ok()"]}}',
                '  {"op":"install_env","cap":"cap.x","manager":"pip",'
                '"packages":["requests==2.31.0"]}',
                '  {"op":"install_ecosystem","cap":"cap.x","pack":{"name":"纯英文小写名",'
                '"kind":"stack","description":"补什么项目类型","provides":["stack:xxx"],'
                '"needs":["node"],"adapter_class":"XxxAdapter",'
                '"files":{"adapter.py":"<StackAdapter 子类源码>"},'
                '"verify":["node","--version"]}}',
            ])
            + chr(10) + "规则：只改判据（探针/描述/安装工具），不碰产物业务代码；"
            "install_tool 的工具会装进引擎本地工具库并跑 verify 复验（rc==0 才算装好，"
            "失败自动回滚），工具名必须纯英文小写；"
            "install_env 是**环境包/软件安装**：manager 可为 pip/npm（隔离目录、不污染"
            "系统 Python）或 winget/choco/scoop/brew（系统软件，如装 ollama/go 工具链；"
            "**装到系统、不可隔离、不自动卸载**，失败只记审计日志）；"
            "**它需要 3/3 全票**——提案人与两名审核者三方给的 manager+包清单必须完全"
            "一致才会执行，任一方没提或提的不同一律不装（普通判据修改只需 2/3）；"
            "包名只允许 `名字` 或 `名字==精确版本`，禁止 -r/-e/git+/URL/路径；"
            "install_ecosystem 是**补全缺失的项目类型能力**（前端 SPA/桌面/中间件…）："
            "远端写一个 StackAdapter 子类 + manifest，引擎 AST 静态校验后装进生态库并复验；"
            "**同样需要 3/3 全票**；适配器源码**禁止 import os/subprocess/socket/shutil/"
            "importlib，禁止 open/exec/eval/write_text**（写盘由引擎做，adapter 只返回内容）；"
            "同一冲突两名审核者给出一致操作才执行（不一致一律跳过）。",
        "投 fix_judge 的硬要求（2026-11-19 实测教训）":
            "**必须同时给 judge_patch**。只写 judge_fix 人话、不给结构化处方，"
            "引擎一处也改不了，只能收工等人——等于你放弃了这次修复机会。"
            "（上一夜两位审核者都投了 fix_judge 却没给处方，结果整夜只涨了 7 条就停了。）"
            "确实不知道该怎么改，才投 keep。",
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


def decide_env_install(writer_verdict: str, patches: list,
                       op_name: str = "install_env") -> tuple[list, list[str]]:
    """install_env 的 **3/3 全票**判定（纯函数、零副作用，便于自检）。

    2026-11-19 用户口径：「pip 这种环境安装也放权给协商，但需要协商到 3/3
    才可以一致同意」。普通判据修改 ≥2 票即可；装包更危险，三票必须一致：
      · 提案人（writer）票不是 keep（默认 unlock 即同意）；
      · 两名审核者的 install_env（manager + 排序后包清单）**完全相同**。
    任一方没提、或提的不一样 → 不装。

    返回 (agree, notes)：agree = [(manager, [packages...]), ...]（按名排序）。
    patches 是两名审核者的 judge_patch 列表 [patch1, patch2]。
    """
    def _env_ops(patch):
        return [o for o in (patch or [])
                if isinstance(o, dict) and str(o.get("op")) == op_name]

    def _norm(op):
        if op_name == "install_ecosystem":
            return str((op.get("pack") or {}).get("name") or "")
        return (str(op.get("manager") or "").strip().lower(),
                tuple(sorted(str(x).strip().lower()
                             for x in (op.get("packages") or []))))

    sets = [{_norm(o) for o in _env_ops(p)} for p in (patches or [])]
    notes: list[str] = []
    writer_ok = str(writer_verdict or "").strip().lower() != "keep"
    label = "生态包安装" if op_name == "install_ecosystem" else "环境安装"
    # 必须两名审核者**都**主张装同一个东西，且非空
    if len(sets) < 2 or not all(sets):
        if any(sets):
            notes.append("%s未达 3/3：需两名审核者都主张装同一个（当前只有一方提）" % label)
        return [], notes
    agree = sets[0] & sets[1]
    if not agree:
        notes.append("%s未达 3/3 一致（reviewer1=%s；reviewer2=%s）"
                     % (label, sorted(map(str, sets[0])), sorted(map(str, sets[1]))))
        return [], notes
    if not writer_ok:
        notes.append("%s未达 3/3：提案人（writer）投 keep，不同意装" % label)
        return [], notes
    if op_name == "install_ecosystem":
        return sorted(agree), notes
    return [(m, list(pk)) for (m, pk) in sorted(agree)], notes


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

    out = {"granted": False, "votes": votes, "tally": tally, "decision": decision,
           "vote_reasons": reasons}      # 处方为空时靠它落一份人可读 TODO
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
        # ③ **install_env 的 3/3 全票门槛**（2026-11-19 用户口径：
        #    "pip 这种环境安装也放权给协商，但需要协商到 3/3 才可以一致同意"）。
        #    普通判据修改 ≥2 票即可；装包更危险，三票必须一致：
        #      · 提案人（writer）票不是 keep（提案人同意，默认 unlock 即同意）；
        #      · reviewer1 与 reviewer2 的 install_env（manager + 排序包清单）**完全相同**。
        #    只有三者都满足的包集合才装；有一方没提或提的不一样 → 不装并留痕。
        #    install_env 不走下方 merge_patches（那里"取第一份"语义不适合装包）。
        try:
            from . import envinstall as _ei
        except Exception:                                # noqa: BLE001
            _ei = None
        _env_agree, _env_notes = decide_env_install(votes.get("writer"), _patches)
        notes.extend(_env_notes)
        if (_env_notes and not _env_agree) or (_patches and not _env_agree):
            _log(ws, {"event": "install_env_rejected_no_consensus",
                      "writer": votes.get("writer"),
                      "notes": _env_notes, "round": st.get("round")})
        for (_mgr, _elems) in _env_agree:
            if _ei is None:
                notes.append("环境安装模块不可用，跳过 %s %s"
                             % (_mgr, ",".join(_elems)))
                continue
            _er = _ei.install(_mgr, _elems)
            if _er.get("ok"):
                total += 1
                notes.append("3/3 一致 → 隔离安装 %s %s（%s）"
                             % (_mgr, ",".join(_elems),
                                str(_er.get("verify") or "")[:80]))
                _log(ws, {"event": "install_env_applied", "manager": _mgr,
                          "packages": _elems, "dir": _er.get("dir"),
                          "round": st.get("round")})
            else:
                notes.append("环境安装失败 %s %s：%s"
                             % (_mgr, ",".join(_elems),
                                "; ".join(_er.get("errors") or [])[:200]))
                _log(ws, {"event": "install_env_failed", "manager": _mgr,
                          "packages": _elems,
                          "errors": _er.get("errors") or [],
                          "round": st.get("round")})
        # **生态包安装**（前端 SPA / 桌面 / 中间件…）：同样要 3/3 全票。
        # 它比装包更危险（适配器代码会进主进程），门槛只多不少。
        _eco_agree, _eco_notes = decide_env_install(
            votes.get("writer"), _patches, "install_ecosystem")
        notes.extend(_eco_notes)
        if _eco_notes and not _eco_agree:
            _log(ws, {"event": "install_ecosystem_rejected_no_consensus",
                      "writer": votes.get("writer"), "notes": _eco_notes,
                      "round": st.get("round")})
        if _eco_agree:
            _eco_ops = {}
            for _p in _patches:
                for _o in (_p or []):
                    if isinstance(_o, dict) and str(_o.get("op")) == "install_ecosystem":
                        _nm = str((_o.get("pack") or {}).get("name") or "")
                        _eco_ops.setdefault(_nm, _o)
            try:
                from . import ecosystem as _ecomod
            except Exception:                            # noqa: BLE001
                _ecomod = None
            for _nm in _eco_agree:
                _op = _eco_ops.get(_nm)
                if _op is None:
                    continue
                if _ecomod is None:
                    notes.append("生态包模块不可用，跳过 %s" % _nm)
                    continue
                _er = _ecomod.install(_op.get("pack") or {})
                if _er.get("ok"):
                    total += 1
                    notes.append("3/3 一致 → 装生态包 %s（%s）"
                                 % (_nm, "; ".join(_er.get("notes") or [])[:120]))
                    _log(ws, {"event": "install_ecosystem_applied",
                              "name": _nm, "round": st.get("round")})
                else:
                    notes.append("生态包装失败 %s：%s"
                                 % (_nm, "; ".join(_er.get("errors") or [])[:200]))
                    _log(ws, {"event": "install_ecosystem_failed", "name": _nm,
                              "errors": _er.get("errors") or [],
                              "round": st.get("round")})
        # 从常规补丁里剥掉 install_env / install_ecosystem（已单独处理，别再走 merge）
        _patches = [[o for o in p
                     if not (isinstance(o, dict)
                             and str(o.get("op")) in ("install_env", "install_ecosystem"))]
                    for p in _patches]
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
