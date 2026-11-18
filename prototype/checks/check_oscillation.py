"""振荡检测自检：能不能真的认出"改回去又改回来"。

三类必须验：
  ① A→B→A：第 3 轮回到第 1 轮的状态 → 必须判振荡（这是"无增长计数"抓不到的情形）；
  ② A→B→C：状态一直往前走 → 不许误报；
  ③ 一直卡在同一状态（A→A→A）：第 2 次就必须报（它不是"有变化"，是原地踏步）。

跑法: python check_oscillation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import oscillation as osc  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _state(verified=0, red=(), stubs=(), untested=(), funcs=4):
    return {"m.py": {"verified": verified, "funcs": funcs,
                     "hard_failed": list(red), "stubs": list(stubs),
                     "untested": list(untested)}}


def main() -> int:
    print("振荡检测自检")

    print("[1] 指纹只取机械事实（判据结果）")
    a = osc.fingerprint(_state(red=["f"]))
    b = osc.fingerprint(_state(red=["f"]))
    c = osc.fingerprint(_state(red=["g"]))
    d = osc.fingerprint(_state(verified=1))
    check("同状态 → 同指纹", a == b, f"{a} vs {b}")
    check("不同红函数 → 不同指纹", a != c)
    check("已验收数变化 → 不同指纹", a != d)
    check("空状态也能算（不崩）", bool(osc.fingerprint({})))

    print("[2] A→B→A 必须被抓到")
    t = osc.Tracker()
    r1 = t.add(1, osc.fingerprint(_state(red=["f"])))
    r2 = t.add(2, osc.fingerprint(_state(red=["g"])))
    r3 = t.add(3, osc.fingerprint(_state(red=["f"])))
    check("第 1 轮新状态", not r1["oscillating"])
    check("第 2 轮新状态", not r2["oscillating"])
    check("第 3 轮回到第 1 轮 → 判振荡", r3["oscillating"], str(r3))
    check("指出回到哪一轮", r3.get("first_round") == 1, str(r3))
    check("说明写清'没有净效果'", "没有净效果" in r3.get("note", ""), r3.get("note"))

    print("[3] A→B→C 不许误报")
    t2 = osc.Tracker()
    seq = [osc.fingerprint(_state(red=[x])) for x in ("f", "g", "h")]
    got = [t2.add(i + 1, fp)["oscillating"] for i, fp in enumerate(seq)]
    check("一直往前 → 全程不报", not any(got), str(got))
    check("计到 3 个不同状态", t2.distinct() == 3, str(t2.distinct()))

    print("[4] 原地踏步（A→A→A）第 2 次就报")
    t3 = osc.Tracker()
    fp = osc.fingerprint(_state(red=["f"]))
    r1 = t3.add(1, fp)
    r2 = t3.add(2, fp)
    check("第 1 次不报", not r1["oscillating"])
    check("第 2 次就报（同一状态=没进展）", r2["oscillating"], str(r2))

    print("[5] 判定动作：有打法就换，没打法就收工")
    o = {"oscillating": True, "note": "回到第 1 轮的状态"}
    act1 = osc.decide_action(o, strategies_left=2)
    act2 = osc.decide_action(o, strategies_left=0)
    act3 = osc.decide_action({"oscillating": False}, strategies_left=2)
    check("还有打法 → 换策略", act1["action"] == "switch_strategy", str(act1))
    check("打法用完 → 收工", act2["action"] == "stop", str(act2))
    check("没振荡 → 继续", act3["action"] == "continue", str(act3))
    check("收工原因写进报告口径", "已列入交付报告" in act2["reason"], act2["reason"])

    print("[6] 序列化：能存能读（跨轮/重启用）")
    data = t.to_dict()
    t4 = osc.Tracker.from_dict(data)
    check("不同状态数一致", t4.distinct() == t.distinct(), f"{t4.distinct()} vs {t.distinct()}")
    check("振荡次数一致", t4.hits == t.hits)
    r = t4.add(4, osc.fingerprint(_state(red=["f"])))
    check("恢复后仍能识别旧状态", r["oscillating"] and r["first_round"] == 1, str(r))

    print("[7] 摘要给人看（报告/事件用）")
    s = osc.summarize(_state(verified=2, red=["f", "g"], stubs=["h"]))
    check("摘要里有验收数与红/桩清单", s["verified"] == 2 and s["red_count"] == 2
          and s["stub_count"] == 1, str(s))

    print("[8] 已接进夜班循环")
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    # 2026-11-10：指纹现在**带端到端能力状态**（cap_cov）——
    # 只看契约判据时，契约全过 → 指纹恒定 → 每轮都被判"振荡"收工（假振荡，白跑）。
    check("每轮记指纹", "_osc.fingerprint(state" in comp)
    check("指纹带端到端能力状态（契约全过时不误判振荡）",
          "_osc.fingerprint(state, cap_cov)" in comp)
    osrc = (Path(__file__).resolve().parents[1] / "core" / "oscillation.py").read_text(encoding="utf-8")
    check("fingerprint 支持 cap_cov", "cap_cov" in osrc and "cap_red=" in osrc)
    # 行为级：契约状态相同、端到端不同 → 指纹必须不同
    _st = {"app/a.py": {"verified": 3, "funcs": 3, "hard_failed": [], "stubs": [], "untested": []}}
    _c1 = {"caps": {"x": {"status": "done"}, "y": {"status": "absent"}}}
    _c2 = {"caps": {"x": {"status": "done"}, "y": {"status": "done"}}}
    check("端到端状态不同 → 指纹不同（不误判振荡）",
          osc.fingerprint(_st, _c1) != osc.fingerprint(_st, _c2))
    check("端到端状态相同 → 指纹相同（真振荡仍能识别）",
          osc.fingerprint(_st, _c1) == osc.fingerprint(_st, dict(_c1)))
    check("振荡后换打法或收工", "detect到振荡" in comp or "检测到振荡" in comp, )
    check("交付报告写振荡摘要", "oscillation" in comp and "未检测到振荡" in comp)

    # 处置入口：换打法**必须在本轮就干活**，且清空决策/写次数预算。
    # 旧实现 switch 后 continue → 五轮零 author_start 的空转（2026-11-07 实测）。
    dec = {"a.py": {"rewrite_module": 1}}
    wu = {"a.py": {"total": 3, "tiers": {1: 2}}}
    mode, v = osc.act({"oscillating": True}, {"action": "switch_strategy"},
                      1, 5, dec_used=dec, write_used=wu)
    check("换打法是 work 而不是跳过", mode == "work", f"{mode},{v}")
    check("策略下标前进一步", v == 2, str(v))
    check("决策预算被清空", dec == {}, str(dec))
    check("写次数预算被清空", wu == {}, str(wu))
    mode2, why2 = osc.act({"oscillating": True}, {"action": "stop"}, 4, 5, dec_used=dec)
    check("打法用完才 stop", mode2 == "stop" and isinstance(why2, str), f"{mode2},{why2}")
    mode3, v3 = osc.act({"oscillating": True}, {"action": "switch_strategy"}, 4, 5)
    check("已是最后一档不越界", mode3 == "work" and v3 == 4, f"{mode3},{v3}")

    print("[N] 地基保护：被依赖的主干不许自动降级")
    from core import blueprint as _bpo, replan as _rp
    _b = _bpo.Blueprint(goal="g")
    _b.add(_bpo.Cap(id="cap.base", desc="x", funcs_est=1, probe={}))
    _b.add(_bpo.Cap(id="cap.down1", desc="x", funcs_est=1, probe={}, deps=["cap.base"]))
    _b.add(_bpo.Cap(id="cap.down2", desc="x", funcs_est=1, probe={}, deps=["cap.base"]))
    _b.add(_bpo.Cap(id="cap.orphan", desc="x", funcs_est=1, probe={}))
    _got = _rp.apply_fails(_b, {"cap.base": 3, "cap.orphan": 3})
    check("地基没被降级", _b.by_id("cap.base").status != "descoped",
          _b.by_id("cap.base").status)
    check("孤儿仍可降级（保住原行为）",
          _b.by_id("cap.orphan").status == "descoped",
          _b.by_id("cap.orphan").status)
    check("地基原因写清楚了", "地基" in str(_b.by_id("cap.base").why),
          str(_b.by_id("cap.base").why)[:80])
    check("返回列表只含孤儿", _got == ["cap.orphan"], str(_got))
    check("notes 留痕", any("地基保护" in n for n in _b.notes), str(_b.notes))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（A→B→A 抓得住 / 单向推进不误报 / 原地踏步即报 / "
          "换打法必干活 / 可持久化）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
