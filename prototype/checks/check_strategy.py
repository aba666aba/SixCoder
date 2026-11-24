"""停滞阶梯（换方向）自检（2026-11-09）。

钉住的事：
  1. 连续停滞次数要驱动**策略切换**，不能每次重规划都做同一件事；
  2. focus 用**结构判据**收缩范围：孤儿能力（无 deps、无人依赖、非 done）降级，
     done / 有依赖 / 被依赖的能力一律保住（不认业务词、不硬编码名单）；
  3. escalate 要有明确的「需人工换方向」信号，而不是继续烧钱空转；
  4. **接入主线**：replan.replan(stall_streak=2) 后蓝图真的被收缩，并 emit 事件。

跑法: python checks/check_strategy.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

try:
    from core import strategy  # noqa: E402
except ImportError:
    strategy = None

from core import blueprint as bpmod, replan  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


_P = {"kind": "http", "steps": [{"method": "GET", "path": "/x", "expect_status": 200}]}


def _bp() -> bpmod.Blueprint:
    b = bpmod.Blueprint(goal="g")
    b.add(bpmod.Cap(id="cap.done", desc="x", funcs_est=1, probe=_P))
    b.by_id("cap.done").status = "done"
    b.add(bpmod.Cap(id="cap.base", desc="x", funcs_est=1, probe=_P))
    b.add(bpmod.Cap(id="cap.down1", desc="x", funcs_est=1, probe=_P, deps=["cap.base"]))
    b.add(bpmod.Cap(id="cap.down2", desc="x", funcs_est=1, probe=_P, deps=["cap.base"]))
    b.add(bpmod.Cap(id="cap.orphan1", desc="x", funcs_est=1, probe=_P))
    b.add(bpmod.Cap(id="cap.orphan2", desc="x", funcs_est=1, probe=_P))
    return b


def main() -> int:
    print("停滞阶梯（换方向）自检")
    check("core/strategy.py 已实现", strategy is not None, "模块不存在")
    if strategy is None:
        print("\n有失败项：" + ", ".join(FAILS))
        return 1

    print("[1] 停滞次数驱动策略切换")
    check("0 次 → flat", strategy.choose(0) == "flat", strategy.choose(0))
    check("1 次 → flat", strategy.choose(1) == "flat", strategy.choose(1))
    check("2 次 → focus（收缩范围）", strategy.choose(2) == "focus", strategy.choose(2))
    check("3 次 → escalate（请人换方向）", strategy.choose(3) == "escalate",
          strategy.choose(3))
    check("负数不炸", strategy.choose(-1) == "flat")

    print("[2] flat 不改蓝图")
    b = _bp()
    r = strategy.apply(b, "flat")
    check("changed=0", int(r.get("changed") or 0) == 0, str(r))
    check("孤儿仍在", b.by_id("cap.orphan1").status != "descoped")

    print("[3] focus 用「孤儿 ∩ 失败证据」收缩，保护地基与 done")
    b = _bp()
    f2 = {"cap.orphan1": 2, "cap.orphan2": 2}
    r = strategy.apply(b, "focus", fails=f2)
    check("降级了 2 条孤儿", int(r.get("changed") or 0) == 2, str(r))
    check("孤儿1 已降级", b.by_id("cap.orphan1").status == "descoped")
    check("孤儿2 已降级", b.by_id("cap.orphan2").status == "descoped")
    check("done 保住", b.by_id("cap.done").status == "done")
    check("被依赖的地基保住", b.by_id("cap.base").status != "descoped")
    check("有 deps 的下游保住", b.by_id("cap.down1").status != "descoped")
    check("降级带原因", "停滞" in str(b.by_id("cap.orphan1").why),
          str(b.by_id("cap.orphan1").why))
    check("幂等（再跑一次不重复降级）",
          int(strategy.apply(b, "focus", fails=f2).get("changed") or 0) == 0)

    print("[4] escalate 给明确信号，不继续空转")
    b = _bp()
    r = strategy.apply(b, "escalate", fails=f2)
    check("返回 halt=True", bool(r.get("halt")), str(r))
    check("done 一个都没掉", b.by_id("cap.done").status == "done")
    check("notes 写明需人工换方向",
          any("人工" in n for n in b.notes), str(b.notes)[:120])

    print("[5] 接入主线：replan.replan 真的应用策略")
    b = _bp()
    events: list[tuple] = []
    with tempfile.TemporaryDirectory() as d:
        rep = replan.replan(b, ws=Path(d), reason="覆盖停滞",
                            stall_streak=2, do_audit=False, fails=f2,
                            emit=lambda k, v: events.append((k, v)))
    b2 = rep["blueprint"]
    check("replan 成功", bool(rep.get("ok")), str(rep.get("issues")))
    check("蓝图里孤儿已降级", b2.by_id("cap.orphan1").status == "descoped",
          b2.by_id("cap.orphan1").status)
    check("地基仍在", b2.by_id("cap.base").status != "descoped")
    check("emit 了 strategy_shift",
          any(k == "strategy_shift" for k, _ in events), str(events)[:120])
    check("未停滞时不做收缩",
          _bp().by_id("cap.orphan1").status != "descoped")

    print("[6] 证据不足时保住核心算法（真实蓝图的结构孤儿含 cap.algo.*）")
    b = _bp()
    b.add(bpmod.Cap(id="cap.algo.solve_student_da", desc="核心算法（结构孤儿）",
                    funcs_est=1, probe=_P))
    r = strategy.apply(b, "focus")            # 无失败证据
    check("无证据一条都不砍", int(r.get("changed") or 0) == 0, str(r))
    check("核心算法保住", b.by_id("cap.algo.solve_student_da").status != "descoped")
    r = strategy.apply(b, "focus", fails={"cap.orphan1": 2})
    check("只砍有证据的孤儿", b.by_id("cap.orphan1").status == "descoped")
    check("同一轮里核心算法仍保住",
          b.by_id("cap.algo.solve_student_da").status != "descoped")
    b3 = _bp()
    b3.add(bpmod.Cap(id="cap.algo.solve", desc="无失败证据的孤儿",
                     funcs_est=1, probe=_P))
    strategy.apply(b3, "escalate", fails=f2)
    check("escalate 把未收缩孤儿列给人",
          any("决策信息" in n for n in b3.notes), str(b3.notes)[:140])
    check("无证据的孤儿没被误砍", b3.by_id("cap.algo.solve").status != "descoped")

    print("[7] halt 时向远端征集「框架外方向」（提出≠拍板）")
    import json as _json
    on_halt = getattr(strategy, "on_halt", None)
    check("on_halt 已实现", callable(on_halt), "属性不存在")
    if callable(on_halt):
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / ".sixcoder").mkdir()
            b4 = _bp()
            seen_instr: list[str] = []

            def fake(instruction, messages):
                seen_instr.append(instruction)
                return _json.dumps({"proposals": [
                    {"title": "收缩验收范围到主干 30 条", "why": "76 条平铺吃光预算",
                     "evidence": "blueprint.json: 84 caps / 10 done"},
                    {"title": "把切片粒度从 6 函数降到 3", "why": "远端一次写太多",
                     "evidence": "unverified"},
                    {"title": "编个方向", "why": "没有证据"},           # 应被丢弃
                ]}, ensure_ascii=False)

            ev: list[tuple] = []
            r = on_halt(ws, b4, emit=lambda k, v: ev.append((k, v)),
                        complete_fn=fake,
                        facts={"done": 10, "total": 84, "orphans": 33, "streak": 3})
            ps = r.get("proposals") or []
            check("收下 2 条有证据的提案", len(ps) == 2, str(len(ps)))
            check("无证据的被丢弃", all(p.get("title") != "编个方向" for p in ps))
            check("一律标 proposed（不自动执行）",
                  all(p.get("status") == "proposed" for p in ps))
            check("auto_apply 恒为 False",
                  all(p.get("auto_apply") is False for p in ps))
            check("instruction 带停滞事实", "84" in seen_instr[0] and "33" in seen_instr[0],
                  seen_instr[0][:120])
            check("instruction 授权质疑框架",
                  "框架" in seen_instr[0] or "验收范围" in seen_instr[0])
            check("emit 留痕", any(k == "strategy_proposal" for k, _ in ev), str(ev))
            pfile = ws / ".sixcoder" / "strategy_proposals.jsonl"
            check("落盘 append-only jsonl",
                  pfile.exists() and len(pfile.read_text(encoding="utf-8").splitlines()) == 2)
            check("不写蓝图（提案不自动改状态）",
                  not (ws / "blueprint.json").exists()
                  and b4.by_id("cap.orphan1").status != "descoped")

            def bad_json(instruction, messages):
                return "我觉得可以换技术栈（不是 JSON）"

            r2 = on_halt(ws, b4, emit=None, complete_fn=bad_json, facts={})
            check("非 JSON 回答安全返回空", not (r2.get("proposals") or []), str(r2))

            def boom(instruction, messages):
                raise RuntimeError("remote down")

            ev3: list[tuple] = []
            r3 = on_halt(ws, b4, emit=lambda k, v: ev3.append((k, v)),
                         complete_fn=boom, facts={})
            check("远端异常降级不抛", not (r3.get("proposals") or []), str(r3))
            check("异常有留痕", len(ev3) > 0, str(ev3))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（策略阶梯 / 证据收缩 / 地基保护 / 人工升级 / 接入 replan / 框架外提案）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
