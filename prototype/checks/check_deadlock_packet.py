"""死局协商包自检（2026-11-19）：钉住"协商必须有权限看到真实失败现象"。

血泪背景：审核者原本只看得到判据定义 + 回滚叙事，看不到探针实际跑了什么，
于是一直改错对象（改判据文本而非夹具/产物），端到端连续 6 轮卡住。
本 check 断言整条调用路径：coverage 落快照 → _packet 按被弄红能力读回并注入。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import coverage, deadlock  # noqa: E402

FAILS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global FAILS
    print("  %-4s %s%s" % ("OK" if cond else "FAIL", name,
                           ("  <- " + extra) if (extra and not cond) else ""))
    if not cond:
        FAILS += 1


def main() -> int:
    print("死局协商包自检（真实失败现象注入）")
    ws = Path(tempfile.mkdtemp(prefix="dlpkt-"))
    (ws / ".sixcoder").mkdir(exist_ok=True)

    # ① coverage 快照往返：只存非 done，带完整 detail
    cov = {"total": 3, "done": ["cap.ok"],
           "caps": {
               "cap.ok": {"status": "done", "detail": "通过"},
               "cap.bad": {"status": "absent",
                           "detail": "第7步 JSON 字段不符：total 实际3 期望2\n"
                                     "【失败步骤的请求】\n第7步 GET /api/x/ 实际响应={total:3}"},
               "cap.bad2": {"status": "absent", "detail": "第2步 404 路由未命中"},
           }}
    coverage.save_failures(ws, cov)
    allf = coverage.load_failures(ws)
    check("快照只存非 done 能力", set(allf.keys()) == {"cap.bad", "cap.bad2"},
          str(list(allf.keys())))
    check("快照带完整失败现象(请求/期望)", "实际3 期望2" in allf["cap.bad"]["detail"]
          and "失败步骤的请求" in allf["cap.bad"]["detail"])
    only = coverage.load_failures(ws, ["cap.bad"])
    check("按 cap 过滤读取", set(only.keys()) == {"cap.bad"}, str(list(only.keys())))

    # ② _packet 注入：给定 st.caps 含 cap.bad，包体必须带真实失败现象
    st = {"caps": ["cap.bad"], "repeat_caps": ["cap.bad"],
          "detail": "振荡：回到第1轮", "state": "negotiate"}
    # 造假蓝图（只给 cap.bad 的判据定义，数字段即可）
    bp = {"caps": [{"id": "cap.bad", "desc": "测试能力",
                    "probe": {"kind": "http", "steps": [
                        {"method": "GET", "path": "/api/x/", "expect_json": {"total": 2}}]}}]}
    (ws / ".sixcoder" / "blueprint.json").write_text(
        json.dumps(bp, ensure_ascii=False), encoding="utf-8")
    packet = deadlock._packet(ws, st)
    parsed = json.loads(packet)
    check("协商包含'真实失败现象'段",
          "真实失败现象" in parsed and len(parsed["真实失败现象"]) >= 1,
          str(list(parsed.keys())))
    injected = json.dumps(parsed.get("真实失败现象"), ensure_ascii=False)
    check("注入的是 actual/expected 而非只有判据文本",
          "实际3 期望2" in injected and "cap.bad" in injected, injected[:200])
    check("判据定义仍在(并排发)",
          any(isinstance(x, dict) and x.get("id") == "cap.bad"
              for x in parsed.get("判据定义", [])), str(parsed.get("判据定义"))[:200])
    check("诊断归因引导在位",
          "夹具" in json.dumps(parsed, ensure_ascii=False)
          and "追移动靶" in json.dumps(parsed, ensure_ascii=False))

    # ③ 没有快照时不炸、包体仍可用（降级留痕）
    ws2 = Path(tempfile.mkdtemp(prefix="dlpkt-empty-"))
    st2 = {"caps": ["cap.x"], "state": "negotiate"}
    p2 = json.loads(deadlock._packet(ws2, st2))
    check("无快照时协商包仍生成(不崩)", isinstance(p2, dict) and "问题" in p2)

    # ④ 停滞型死局（2026-11-19）：反复改同文件识别 + on_stall 唤醒 + attempts 守卫。
    # mock _ask_reviewer，让协商在本地完成（三方都投 keep → 不解锁），不花钱。
    ws3 = Path(tempfile.mkdtemp(prefix="dlstall-"))
    (ws3 / ".sixcoder").mkdir(exist_ok=True)
    from core import wholerepo
    rc = [
        {"round": 1, "files": ["apps/a/urls.py", "apps/b/views.py"]},
        {"round": 2, "files": ["apps/a/urls.py", "apps/c/models.py"]},
        {"round": 3, "files": ["apps/a/urls.py"]},
    ]
    wholerepo.save_history(ws3, rc)
    rep = deadlock._repeated_files(ws3)
    check("识别反复改的文件(>=2次)", "apps/a/urls.py" in rep
          and "apps/b/views.py" not in rep, str(rep))

    _orig_ask = deadlock._ask_reviewer
    deadlock._ask_reviewer = lambda role, packet: {
        "ok": True, "answer": '{"verdict":"keep","reason":"test"}'}
    try:
        r1 = deadlock.on_stall(ws3, 7, "连续3轮无增长", caps=["cap.x"],
                               stall_rounds=3)
        check("on_stall 触发协商(keep→不继续)", r1.get("continue") is False
              and (r1.get("decision") or {}).get("decision") == "keep", str(r1))
        st3 = deadlock.load_state(ws3)
        check("唤醒态记 kind=stall + 反复文件",
              st3.get("kind") == "stall" and "apps/a/urls.py" in (st3.get("files") or []),
              str({k: st3.get(k) for k in ("kind", "files")}))
        check("状态协商后回 idle", st3.get("state") == "idle", str(st3.get("state")))
        # 协商包仍带真实失败现象（on_stall 复用 negotiate → _packet）
        check("停滞协商记 attempt", int(st3.get("stall_attempts") or 0) == 1,
              str(st3.get("stall_attempts")))
        r2 = deadlock.on_stall(ws3, 8, "连续4轮无增长", caps=["cap.x"], stall_rounds=4)
        check("第二次仍可协商", int(deadlock.load_state(ws3).get("stall_attempts") or 0) == 2,
              str(r2.get("reason")))
        r3 = deadlock.on_stall(ws3, 9, "连续5轮无增长", caps=["cap.x"], stall_rounds=5)
        check("第三次被守卫挡住(防无限烧钱)", r3.get("continue") is False
              and "2 次" in str(r3.get("reason")), str(r3))
    finally:
        deadlock._ask_reviewer = _orig_ask

    # ⑤ company 接线：on_stall 调用必须在 _night_inner 里，判据用 stall_rounds
    import ast
    csrc = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    tree = ast.parse(csrc)
    wired = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_night_inner":
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "on_stall"):
                    wired = True
    check("company._night_inner 接了 on_stall", wired)

    # ⑥ 陈旧协商态自愈（2026-11-19）：协商中途被中断 → state 卡 negotiate →
    #    三个钩子全判"已在协商中"→三方休眠。load_state 读到超时未决态要复位 idle。
    import time
    ws4 = Path(tempfile.mkdtemp(prefix="dlstale-"))
    (ws4 / ".sixcoder").mkdir(exist_ok=True)

    # 进行中的协商（刚 wake_at）：不复位
    fresh = {"state": "negotiate", "kind": "oscillation", "round": 6,
             "wake_at": time.time(), "attempts": 0}
    deadlock.save_state(ws4, fresh)
    check("进行中的协商不被误复位",
          deadlock.load_state(ws4).get("state") == "negotiate")

    # 陈旧协商态（wake_at 超过阈值）：复位 idle + 留痕
    stale = dict(fresh)
    stale["wake_at"] = time.time() - deadlock.STALE_NEGOTIATE_SEC - 60
    deadlock.save_state(ws4, stale)
    got = deadlock.load_state(ws4)
    check("陈旧协商态被复位 idle", got.get("state") == "idle", str(got.get("state")))
    logs = (ws4 / ".sixcoder" / "unlock_log.jsonl")
    check("自愈留痕 deadlock_stale_recovered",
          logs.is_file() and "deadlock_stale_recovered" in logs.read_text(encoding="utf-8"))

    # 不带 wake_at 的历史 state：不猜、不复位（避免误伤旧数据）
    legacy = {"state": "negotiate", "kind": "rollback", "round": 3}
    deadlock.save_state(ws4, legacy)
    check("无时间戳的历史 state 不误复位",
          deadlock.load_state(ws4).get("state") == "negotiate")

    # 三个钩子唤醒时都写 wake_at（钉调用路径，不只是函数存在）
    src = (ROOT / "core" / "deadlock.py").read_text(encoding="utf-8")
    check("三个钩子唤醒都写 wake_at",
          src.count('"wake_at": time.time()') >= 3,
          "出现 %d 次" % src.count('"wake_at": time.time()'))

    print("\n结果：%s" % ("全部通过" if FAILS == 0 else "%d 条失败" % FAILS))
    return 1 if FAILS else 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
