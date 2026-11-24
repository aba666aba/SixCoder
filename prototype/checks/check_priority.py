"""写码优先级（核心优先）自检 —— **已接进排程**（2026-11-11 起）。

为什么单独一个脚本：
    这条线是"核心业务优先跑通"的落地，它的**两个边界**必须钉死：
      ① 排序口径对不对（核心在前、其余有确定顺序、保底能防饿死）；
      ② **不许越权** —— 接线之后仍然只许决定"先做哪个"，
         不许碰预算、不许砍能力（用户 2026-11-11 定稿："排进路线图，现在做"）。

跑法: python check_priority.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import blueprint as bp_mod, priority  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def _ws() -> Path:
    return Path(tempfile.mkdtemp(prefix="prio_"))


def _bp() -> bp_mod.Blueprint:
    """合成蓝图：两条核心能力 + 四条外围能力。

    依赖图（谁依赖谁）：
        cap.base ← cap.a, cap.b, cap.core2     （fanout 3）
        cap.core2 ← cap.d                        （fanout 1）
    模块落点：
        store_base.py ← cap.base          （data，核心，解锁 #0）
        algo_core.py  ← cap.core2, cap.d  （algorithm，核心，解锁 #3）
        algo_a.py     ← cap.a             （algorithm，非核心）
        algo_b.py     ← cap.b             （algorithm，非核心）
        misc.py       ← cap.misc          （generic，非核心）
        ui_page.py    ← cap.ui            （ui，非核心）
    """
    caps = [
        bp_mod.Cap(id="cap.base", desc="地基", deps=[], modules=["store_base.py"]),
        bp_mod.Cap(id="cap.a", desc="外围A", deps=["cap.base"], modules=["algo_a.py"]),
        bp_mod.Cap(id="cap.b", desc="外围B", deps=["cap.base"], modules=["algo_b.py"]),
        bp_mod.Cap(id="cap.core2", desc="核心2", deps=["cap.base"], modules=["algo_core.py"]),
        bp_mod.Cap(id="cap.d", desc="外围D", deps=["cap.core2"], modules=["algo_core.py"]),
        bp_mod.Cap(id="cap.ui", desc="界面", deps=[], modules=["ui_page.py"]),
        bp_mod.Cap(id="cap.misc", desc="杂项", deps=[], modules=["misc.py"]),
    ]
    return bp_mod.Blueprint(goal="测试", caps=caps)


def _contracts() -> dict:
    return {p: SimpleNamespace(raw="", funcs=[], module=Path(p).stem)
            for p in ("store_base.py", "algo_core.py", "algo_a.py",
                      "algo_b.py", "misc.py", "ui_page.py")}


def _paths(ranked: list[dict]) -> list[str]:
    return [r["path"] for r in ranked]


def _ledger(ws: Path, rows: list[dict]) -> None:
    p = ws / ".sixcoder" / "author_attempts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")


def main() -> int:
    ok = True
    print("写码优先级自检（核心优先 · 只算不接）")
    bp = _bp()
    contracts = _contracts()
    ws = _ws()

    # ── 1) 能力拓扑序 ────────────────────────────────────────────────────────
    print("[1] 能力拓扑序与被依赖度")
    order, pos, fan = priority.cap_topology(bp)
    ok &= check("拓扑序把依赖放在前面（cap.base 在最前）",
                order and order[0] == "cap.base", str(order[:3]))
    ok &= check("位置表可用", pos.get("cap.base") == 0 and pos.get("cap.d") == 4,
                str(pos))
    ok &= check("被依赖度算对（cap.base=3、cap.core2=1）",
                fan.get("cap.base") == 3 and fan.get("cap.core2") == 1, str(fan))
    ok &= check("没有蓝图也不炸", priority.cap_topology(None) == ([], {}, {}), "")

    # ── 2) 能力 → 模块映射 ───────────────────────────────────────────────────
    print("[2] 能力落到哪些模块")
    cmap = priority.cap_module_map(bp, ws)
    ok &= check("映射到模块（cap.base → store_base.py）",
                cmap.get("store_base.py") == ["cap.base"], str(cmap.get("store_base.py")))
    ok &= check("一个模块可承载多条能力（algo_core.py 两条）",
                sorted(cmap.get("algo_core.py") or []) == ["cap.core2", "cap.d"],
                str(cmap.get("algo_core.py")))

    # ── 3) 核心优先 ──────────────────────────────────────────────────────────
    print("[3] 核心优先")
    ranked = priority.rank(ws, contracts, bp=bp)
    p = _paths(ranked)
    ok &= check("承载被依赖能力的模块算核心",
                ranked[0]["core"] and ranked[1]["core"], str(p[:2]))
    ok &= check("核心排最前两位（store_base → algo_core）",
                p[:2] == ["store_base.py", "algo_core.py"], str(p[:2]))
    ok &= check("核心内按解锁位置排（#0 在 #3 前）",
                ranked[0]["unlock"] == 0 and ranked[1]["unlock"] == 3,
                str([r["unlock"] for r in ranked[:2]]))
    ok &= check("非核心全在核心之后",
                all(not r["core"] for r in ranked[2:]), str(p[2:]))

    # ── 4) 不是核心的怎么排 ──────────────────────────────────────────────────
    print("[4] 非核心的排序（端到端红 > 角色地基 > 成功率）")
    ok &= check("角色地基（algorithm）先于 generic",
                p.index("algo_a.py") < p.index("misc.py"), str(p))
    ok &= check("角色地基先于门面（ui）",
                p.index("misc.py") < p.index("ui_page.py"), str(p))
    ok &= check("门面 ui 排最后", p[-1] == "ui_page.py", str(p))

    # 端到端红优先：把 algo_b 标成端到端红 → 它应提到 algo_a 之前（同角色内）
    ranked2 = priority.rank(ws, contracts, bp=bp, cap_red={"algo_b.py"})
    p2 = _paths(ranked2)
    ok &= check("端到端红优先于同角色的普通模块",
                p2.index("algo_b.py") < p2.index("algo_a.py"), str(p2))

    # ── 5) 保底提权：被冷落的不许饿死，但也不许越过核心 ────────────────────────
    print("[5] 公平性保底（防饿死，但不越过核心）")
    ranked3 = priority.rank(ws, contracts, bp=bp, starved={"misc.py": priority.STARVE_LIMIT})
    p3 = _paths(ranked3)
    ok &= check("被冷落的非核心被提权到非核心首位",
                p3.index("misc.py") < p3.index("algo_a.py"), str(p3))
    ok &= check("提权**不越过核心**（两个核心仍在最前）",
                p3[:2] == ["store_base.py", "algo_core.py"], str(p3[:2]))
    ok &= check("冷落不够多就不提权",
                _paths(priority.rank(ws, contracts, bp=bp,
                                     starved={"misc.py": priority.STARVE_LIMIT - 1})
                       ).index("misc.py") > _paths(ranked3).index("misc.py"),
                "")

    # ── 6) 连续顶格仍写不出的靠后（#2 的记录**只用于排序，不用于砍**） ──────────
    print("[6] 台账数据只用于排序")
    _ledger(ws, [{"at": "2026-11-10 10:00:00", "path": "algo_a.py", "effort": "max",
                   "ok": False, "kind": "module"}] * 3)
    ranked4 = priority.rank(ws, contracts, bp=bp)
    p4 = _paths(ranked4)
    ok &= check("连续顶格失败的模块排到同角色模块之后",
                p4.index("algo_b.py") < p4.index("algo_a.py"), str(p4))
    ok &= check("但**没有被丢掉**（还在名单里）", "algo_a.py" in p4, str(p4))
    ok &= check("模块没被移出结果（只排后，不砍）",
                len(ranked4) == len(contracts), f"{len(ranked4)} vs {len(contracts)}")

    # ── 7) 确定性 ────────────────────────────────────────────────────────────
    print("[7] 确定性（同输入同输出）")
    a = _paths(priority.rank(ws, contracts, bp=bp))
    b = _paths(priority.rank(ws, contracts, bp=bp))
    ok &= check("两次排序完全一致", a == b, f"{a} vs {b}")

    # ── 8) 已完成的排除 ───────────────────────────────────────────────────────
    print("[8] 已完成的不进名单")
    ranked5 = priority.rank(ws, contracts, bp=bp, done_modules={"store_base.py"})
    ok &= check("done_modules 里的被排除", "store_base.py" not in _paths(ranked5),
                str(_paths(ranked5)))

    # ── 9) 报告口径 ──────────────────────────────────────────────────────────
    print("[9] 报告口径")
    lines = priority.report_lines(ranked)
    ok &= check("报告写明「只算不接」",
                any("只算不接" in x for x in lines), str(lines[:3]))
    ok &= check("报告写明不改派单/不改预算",
                any("不改派单" in x and "不改预算" in x for x in lines), "")
    ok &= check("每行都带可解释的理由",
                all(r.get("reason") for r in ranked), "")
    empty = priority.report_lines([])
    ok &= check("空输入不炸（如实说没有可排模块）",
                any("没有可排的模块" in x for x in empty), str(empty))

    # ── 10) **只算不接**：源码层断言 ──────────────────────────────────────────
    # 2026-11-11（用户定稿"排进路线图现在做"）：priority 已**接进排程**，
    # 钉子随之从"不许接"改成"接了但不许越权"—— 排序可以改，**不许碰预算/不许砍能力**。
    print("[10] 已接进排程（核心优先派单），但不许越权")
    src = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(
        encoding="utf-8", errors="replace")
    ok &= check("company.py 引用了 priority.rank（核心优先真的接上了）",
                "priority.rank" in src or "_prio.rank(" in src, "")
    ok &= check("company.py 引用了 starved_map（保底提权）",
                "starved_map" in src, "")
    ok &= check("company.py 引用了 module_deps（依赖已满足才开写）",
                "module_deps" in src, "")
    ok &= check("company.py 引用了 mark_served（记录本轮被服务到的模块）",
                "mark_served" in src, "")
    ok &= check("派单结果可解释（发 priority_pick 事件带 reason）",
                "priority_pick" in src and "reason" in src, "")
    ok &= check("业务流优先接上了（用上一轮 biz_flow 提权相关角色模块）",
                "biz_flow" in src and "_biz_mods" in src, "")
    ok &= check("收工理由必须写明业务流未通过",
                "业务流未通过" in src, "")
    prio_src = (Path(__file__).resolve().parents[1] / "core" / "priority.py").read_text(
        encoding="utf-8", errors="replace")
    ok &= check("priority.py 里没有写盘/派单副作用（只排序 + 记被服务时间）",
                "blueprint.save" not in prio_src and "record_attempt" not in prio_src
                and "needs_work" not in prio_src, "")
    # 注意：不能粗暴地 grep "budget" —— 文档字符串里就会命中。
    # 精确口径：不许**引用**预算/成本的数据源与写入口。
    # 精确口径：**代码里不许访问**预算/成本数据（文档字符串里提到事故不算）。
    _access = ('.cost_cny', '["cost_cny"]', "'cost_cny'")
    ok &= check("priority.py 不碰预算（代码里不访问成本/预算/花费账本）",
                not any(a in prio_src for a in _access)
                and "system_budget" not in prio_src
                and "budget_estimate" not in prio_src
                and "night_spent" not in prio_src
                and "project_ledger" not in prio_src, "")

    print("[11] 保底提权持久化：跨重启有效（否则重启一次就重新饿死一次）")
    with tempfile.TemporaryDirectory() as td:
        _wsdir = Path(td)
        ok &= check("刚被服务过的模块 → starved=0",
                    priority.starved_map(_wsdir, now=1000.0) == {}, "")
        priority.mark_served(_wsdir, ["app/a.py"], now=1000.0)
        ok &= check("落盘了（.sixcoder/priority_served.json）",
                    (_wsdir / priority.SERVED_FILE).exists(), "")
        ok &= check("服务过 → starved=0",
                    priority.starved_map(_wsdir, now=1000.0).get("app/a.py") == 0,
                    str(priority.starved_map(_wsdir, now=1000.0)))
        _later = priority.starved_map(_wsdir, now=1000.0 + 40 * 60)
        ok &= check("过了 40 分钟 → starved 涨到 3 轮以上（够触发保底提权）",
                    int(_later.get("app/a.py") or 0) >= priority.STARVE_LIMIT,
                    str(_later))
        ok &= check("模拟重启（新进程读同一个文件）读得到",
                    int(priority.starved_map(_wsdir, now=1000.0 + 40 * 60)
                        .get("app/a.py") or 0) >= priority.STARVE_LIMIT, "")

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（拓扑序 / 映射 / 核心优先 / 非核心排序 / 保底 / 只排序不砍 / 确定性 / 已接线且不越权）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
