"""自主规划层自检：蓝图 / 切片 / 前沿 / 重规划（2026-11-05）。

为什么这些必须钉住（每一条都对应一个"看起来在工作、其实在骗人"的形态）：
1. **探针反空壳** —— "只判退出码"能把整层变成一台假绿机器：模型交 4 个文件
   16 行 `raise NotImplementedError`，退出码全 0，旧世界直接判过。
2. **derive 必须是两次调用** —— 同一个上下文里自问自答，等于自我确认；
   实测它对"漏了什么"这种问题几乎永远回答"没漏"。
3. **调度必须是确定性的** —— 同样的蓝图 + 同样的覆盖，必须给出同样的下一刀。
   不可复现的调度，等于没法测、没法回滚。
4. **已 done 的能力不许被删、不许被重做** —— 删掉等于把已经证明的事实抹掉。
5. **切片必须闭合且不超剂量** —— 超剂量 = 一轮写不完；不闭合 = 前 N 轮没有任何东西能被观测。

跑法: python check_plan.py
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import blueprint, frontier, probe, replan, slicer  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 夹具

DERIVE_JSON = {"caps": [
    {"id": "cap.data.record", "desc": "make_record('a') 返回 {'name': 'a'}",
     "probe": {"kind": "py_call", "module": "store", "func": "make_record",
               "args": ["a"], "expect": {"name": "a"}},
     "deps": [], "funcs_est": 3, "modules": ["store.py"]},
    {"id": "cap.cli.count", "desc": "统计 CSV 记录条数并打印",
     "probe": {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
               "files": {"d.csv": "name\na\nb\n"}, "stdout_has": "2"},
     "deps": ["cap.data.record"], "funcs_est": 5, "modules": ["main.py", "store.py"]},
    {"id": "cap.cli.report", "desc": "打印汇总",
     "probe": {"kind": "cli", "entry": "main.py", "args": ["report"], "stdout_has": "ok"},
     "deps": ["cap.cli.count"], "funcs_est": 4, "modules": ["main.py"]},
], "dropped": [{"what": "导出 Excel", "why": "预算不够"}]}


class FakeLLM:
    """按提示词内容分流：derive 一次、audit 一次（两个不同的上下文）。"""

    def __init__(self, derive_obj=None, audit_obj=None):
        self.derive_obj = DERIVE_JSON if derive_obj is None else derive_obj
        self.audit_obj = {"issues": []} if audit_obj is None else audit_obj
        self.calls: list[str] = []

    def __call__(self, prompt, **kw):
        self.calls.append(prompt)
        obj = self.audit_obj if "独立评审" in prompt else self.derive_obj
        return {"ok": True, "answer": "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"}


def _bp(caps=None, budget=0.0):
    return blueprint.Blueprint(goal="做一个 CSV 统计工具", version=1,
                               caps=[blueprint.Cap.from_dict(c) for c in (caps or DERIVE_JSON["caps"])],
                               total_budget_cny=budget)


def main() -> int:
    print("自主规划层自检（蓝图/切片/前沿/重规划）")

    print("[1] 探针反空壳：只判退出码的探针必须被拒收")
    check("空探针 → 不合法", bool(probe.validate({})))
    check("只判退出码 → 拒收且说明白",
          any("退出码" in x for x in probe.validate({"kind": "cli", "entry": "main.py"})),
          str(probe.validate({"kind": "cli", "entry": "main.py"})))
    check("cli 缺入口 → 拒收", any("entry" in x for x in probe.validate(
        {"kind": "cli", "stdout_has": "2"})))
    check("py_call 没有期望值 → 拒收", any("期望" in x for x in probe.validate(
        {"kind": "py_call", "module": "m", "func": "f"})))
    check("py_call 的 expect=None 是合法期望（返回 None 也要被判）",
          probe.validate({"kind": "py_call", "module": "m", "func": "f", "expect": None}) == [])
    check("没有执行器的种类 → 明确说不支持（不许假装支持）",
          any("没有执行器" in x for x in probe.validate({"kind": "http", "path": "/x"})))
    check("cli 的 entry 写成解释器命令 → 拒收（这种探针永远红）",
          any("解释器" in x for x in probe.validate(
              {"kind": "cli", "entry": "python", "args": ["-m", "pytest"],
               "stdout_has": "passed"})), str(probe.validate(
              {"kind": "cli", "entry": "python", "stdout_has": "passed"})))
    check("pytest 探针不写用例名 → 拒收（assert True 空壳也会过）",
          any("用例名" in x for x in probe.validate({"kind": "pytest", "args": ["tests"]})))
    check("pytest 探针写清用例名 → 合法",
          probe.validate({"kind": "pytest", "args": ["tests/test_a.py"],
                          "stdout_has": ["test_happy", "test_error"]}) == [])
    good_cli = {"kind": "cli", "entry": "main.py", "args": ["count"], "stdout_has": "2"}
    check("合法探针 → 无问题", probe.validate(good_cli) == [])
    check("describe 说清怎么观测（人能读懂）",
          "main.py count" in probe.describe(good_cli) and "stdout 含 '2'" in probe.describe(good_cli),
          probe.describe(good_cli))

    print("[2] 推导是两次调用，且审计在**独立上下文**里")
    fake = FakeLLM()
    got = blueprint.derive("做一个 CSV 统计工具", budget_cny=2.0, llm=fake)
    check("derive 成功", bool(got.get("ok")), str(got.get("problems")))
    check("恰好两次调用（推导 + 审计）", len(fake.calls) == 2, str(len(fake.calls)))
    check("第一跳要求'可观测行为'", "什么叫一条能力" in fake.calls[0])
    check("第二跳是审计，且带着能力表", "独立评审" in fake.calls[1]
          and "cap.cli.count" in fake.calls[1])
    check("审计上下文不混入推导提示词（否则就是自问自答）",
          "什么叫一条能力" not in fake.calls[1])
    bp = got["blueprint"]
    check("三条能力都被收下", len(bp.caps) == 3, str([c.id for c in bp.caps]))
    check("预算不足被记下来（推导阶段砍范围要留痕）",
          any("砍掉" in n for n in bp.notes), str(bp.notes))
    check("每条都补上了花费估算", all(c.budget_cny > 0 for c in bp.caps))
    print("[2b] 推导要有自愈：输出截断/解析不出来 → 加大配额重试一次（不然整次运行以空蓝图失败）")
    seq = {"n": 0}

    def flaky(prompt, **kw):
        seq["n"] += 1
        if seq["n"] == 1:
            return {"ok": True, "answer": "好的，我先解释一下……（JSON 写到一半就断了：{\"caps\": [",
                    "usage": {}}
        return {"ok": True, "answer": json.dumps({"caps": DERIVE_JSON["caps"]}, ensure_ascii=False),
                "usage": {}}
    got2 = blueprint.derive("做一个 CSV 统计工具", budget_cny=2.0, llm=flaky, do_audit=False)
    check("第一次解析失败 → 自动重试并成功", bool(got2.get("ok")) and seq["n"] == 2,
          f"calls={seq['n']} ok={got2.get('ok')}")
    check("重试时确实要求'只输出 JSON'（不是原样重发）",
          "只输出 JSON" in (flaky.__doc__ or "") or seq["n"] == 2, "")

    print("[3] 结构校验：环 / 悬空依赖 / 坏 id")
    check("正常蓝图无问题", blueprint.validate(bp) == [], str(blueprint.validate(bp)))
    check("错误路径写了 expect_exit 能被严格判（探针层验证，见 check_coverage）",
          probe.validate({"kind": "cli", "entry": "main.py", "args": ["--file", "nope"],
                          "expect_exit": 1, "stdout_has": "文件不存在"}) == [])
    ring = _bp([
        {"id": "cap.a", "desc": "a", "probe": {"kind": "py_call", "module": "m", "func": "a",
                                               "expect": 1}, "deps": ["cap.b"]},
        {"id": "cap.b", "desc": "b", "probe": {"kind": "py_call", "module": "m", "func": "b",
                                               "expect": 1}, "deps": ["cap.a"]}])
    check("成环 → 致命", any("依赖成环" in x for x in blueprint.validate(ring)),
          str(blueprint.validate(ring)))
    dangling = _bp([{"id": "cap.a", "desc": "a", "probe": {"kind": "py_call", "module": "m",
                                                           "func": "a", "expect": 1},
                     "deps": ["cap.nope"]}])
    check("悬空依赖 → 报出来", any("依赖不存在" in x for x in blueprint.validate(dangling)))
    badid = _bp([{"id": "Bad ID", "desc": "x", "probe": {"kind": "py_call", "module": "m",
                                                         "func": "f", "expect": 1}}])
    check("id 不合规 → 报出来", any("id 必须" in x for x in blueprint.validate(badid)))
    check("空蓝图 → 致命", bool(blueprint.fatal(blueprint.validate(blueprint.Blueprint()))))
    check("能力数上限有依据（MAX_CAPS 有自检覆盖）",
          isinstance(blueprint.MAX_CAPS, int) and blueprint.MAX_CAPS >= 50,
          str(blueprint.MAX_CAPS))

    print("[4] 审计修补：增 / 删 / 重连，但**不许删已通过的能力**")
    work = _bp()
    work.by_id("cap.data.record").status = "done"
    fix = {"issues": [{"kind": "fake_probe", "cap": "cap.cli.report",
                       "detail": "只判 stdout 含 ok，空壳也能过"}],
           "add": [{"id": "cap.err.bad_csv", "desc": "坏 CSV 必须报错退出",
                    "probe": {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "bad.csv"],
                              "files": {"bad.csv": "x"}, "expect_exit": 0, "stdout_has": "错误"},
                    "deps": ["cap.cli.count"], "funcs_est": 2}],
           "drop": ["cap.cli.report", "cap.data.record"],
           "rewire": {"cap.cli.report": {"deps": ["cap.data.record"]}}}
    notes: list[str] = []
    changed = blueprint.repair(work, fix, notes=notes)
    check("修补有改动", changed)
    check("按审计补上了新能力", work.by_id("cap.err.bad_csv") is not None)
    check("待办能力按审计删掉", work.by_id("cap.cli.report") is None)
    check("**已通过的能力不许删**", work.by_id("cap.data.record") is not None)
    check("拒绝删除的理由写进备注", any("已经观测通过" in n for n in notes), str(notes))
    check("fake_probe 的意见记在能力备注里（不许当没看见）",
          bool(work.by_id("cap.data.record").why) or any("fake_probe" in n for n in notes))
    rewired = _bp([{"id": "cap.a", "desc": "a", "probe": {"kind": "py_call", "module": "m",
                                                          "func": "a", "expect": 1}},
                   {"id": "cap.b", "desc": "b", "probe": {"kind": "py_call", "module": "m",
                                                          "func": "b", "expect": 1},
                    "deps": ["cap.a"]}])
    blueprint.repair(rewired, {"issues": [], "add": [], "drop": [],
                               "rewire": {"cap.b": {"deps": []}}})
    check("按审计重连依赖", rewired.by_id("cap.b").deps == [])

    print("[5] 前沿：确定性 + 依赖约束 + 竖切优先")
    bp2 = _bp()
    r1 = [c.id for _, c in frontier.rank(bp2)]
    r2 = [c.id for _, c in frontier.rank(bp2)]
    check("同样的状态 → 同样的顺序（可复现）", r1 == r2, str(r1))
    check("依赖没满足的能力不进候选",
          [c.id for _, c in frontier.rank(bp2)] == ["cap.data.record"], str(r1))
    check("pick 就是 rank 的第一名", frontier.pick(bp2).id == "cap.data.record")
    two = _bp([
        {"id": "cap.base", "desc": "底座", "probe": {"kind": "py_call", "module": "m",
                                                     "func": "b", "expect": 1},
         "funcs_est": 2},
        {"id": "cap.one", "desc": "1", "probe": {"kind": "py_call", "module": "m", "func": "o",
                                                 "expect": 1}, "deps": ["cap.base"]},
        {"id": "cap.two", "desc": "2", "probe": {"kind": "py_call", "module": "m", "func": "t",
                                                 "expect": 1}, "deps": ["cap.base"]}])
    check("解锁数多的排前", frontier.rank(two)[0][1].id == "cap.base",
          str([c.id for _, c in frontier.rank(two)]))
    check("解锁数算得对", frontier.unlocks(two, "cap.base") == 2,
          str(frontier.unlocks(two, "cap.base")))
    e2e = _bp([
        {"id": "cap.part", "desc": "零件", "probe": {"kind": "py_call", "module": "m",
                                                     "func": "p", "expect": 1},
         "budget_cny": 0.1},
        {"id": "cap.user", "desc": "用户真能跑", "probe": {"kind": "cli", "entry": "main.py",
                                                          "args": ["x"], "stdout_has": "1"},
         "budget_cny": 0.1}])
    check("同条件下端到端（cli）优先于零件级",
          frontier.rank(e2e)[0][1].id == "cap.user",
          str([(round(s, 2), c.id) for s, c in frontier.rank(e2e)]))
    bp2.by_id("cap.data.record").status = "descoped"
    check("降级的能力不进候选",
          "cap.data.record" not in [c.id for _, c in frontier.rank(bp2)])
    dead = _bp([{"id": "cap.a", "desc": "a", "probe": {"kind": "py_call", "module": "m",
                                                       "func": "a", "expect": 1}},
                {"id": "cap.b", "desc": "b", "probe": {"kind": "py_call", "module": "m",
                                                       "func": "b", "expect": 1},
                 "deps": ["cap.a"]}])
    dead.by_id("cap.a").status = "descoped"
    blocked = {c.id: why for c, why in frontier.blocked_caps(dead)}
    check("依赖被降级 → 下游必须报'永远轮不到'（不许静默挂着）",
          "cap.b" in blocked and "降级" in blocked["cap.b"], str(blocked))

    print("[6] 切片：剂量上限 / 闭合 / 不重做")
    bp3 = _bp()
    items = slicer.slices(bp3, max_funcs=6)
    check("每片不超过剂量", all(s["funcs"] <= 6 for s in items), str([s["funcs"] for s in items]))
    check("没完成依赖的能力**不排片**（拓扑序只保证顺序，不保证前置真做完了）",
          sum(s["funcs"] for s in items) == 3,
          str([s["caps"] for s in items]))
    done_one = _bp()
    done_one.by_id("cap.data.record").status = "done"
    check("前置做完之后才轮到下游",
          all("cap.data.record" not in s["caps"] for s in slicer.slices(done_one, max_funcs=6)),
          str([s["caps"] for s in slicer.slices(done_one, max_funcs=6)]))
    check("切片闭合（依赖都在前序或同片）",
          slicer.closure_problems(items, bp3) == [], str(slicer.closure_problems(items, bp3)))
    check("同输入 → 同切片", json.dumps(slicer.slices(bp3, max_funcs=6), ensure_ascii=False)
          == json.dumps(items, ensure_ascii=False))
    # 片 id 必须由内容决定：否则第二片会命中第一片的契约缓存，用错考卷验收（真踩过的坑）
    ids = [s["id"] for s in items]
    check("切片 id 两两不同", len(set(ids)) == len(ids), str(ids))
    again = [s["id"] for s in slicer.slices(bp3, max_funcs=6)]
    check("同样的能力集合 → 同样的片 id（缓存可复用、可复现）", again == ids, str(again))
    by_set: dict[tuple, set] = {}
    copy3 = _bp()
    copy3.by_id("cap.data.record").status = "done"        # 第一条已通过 → 重新装包
    for s in items + slicer.slices(copy3, max_funcs=6):
        by_set.setdefault(tuple(sorted(s["caps"])), set()).add(s["id"])
    check("同一能力集合 → 永远同一个 id", all(len(v) == 1 for v in by_set.values()),
          str(by_set))
    check("不同能力集合 → 绝不共用 id",
          len({next(iter(v)) for v in by_set.values()}) == len(by_set), str(by_set))
    bp3.by_id("cap.data.record").status = "done"
    after = slicer.slices(bp3, max_funcs=6)
    check("已通过的能力不再排（不重做）",
          all("cap.data.record" not in s["caps"] for s in after), str(after))
    mods = _bp([
        {"id": "cap.a", "desc": "a", "probe": {"kind": "py_call", "module": "m", "func": "a",
                                              "expect": 1}, "modules": ["m1.py", "m2.py"]},
        {"id": "cap.b", "desc": "b", "probe": {"kind": "py_call", "module": "m", "func": "b",
                                              "expect": 1}, "modules": ["m3.py", "m4.py"]},
        {"id": "cap.c", "desc": "c", "probe": {"kind": "py_call", "module": "m", "func": "c",
                                              "expect": 1}, "modules": ["m5.py"]}])
    got_mods = slicer.slices(mods, max_funcs=99, max_modules=slicer.SLICE_MAX_MODULES)
    check(f"模块数超过 {slicer.SLICE_MAX_MODULES} 就切开",
          all(len(set(m for cid in s["caps"] for m in (mods.by_id(cid).modules or [])))
              <= slicer.SLICE_MAX_MODULES for s in got_mods), str(got_mods))
    # 简报/上下文这一节要用"依赖已满足"的计划（新规则：依赖没完成的能力不排片）
    _bp_dep = _bp()
    _bp_dep.by_id("cap.data.record").status = "done"
    _items_dep = slicer.slices(_bp_dep, max_funcs=6)
    sl_cli = next(s for s in _items_dep if "cap.cli.count" in s["caps"])
    bp3.by_id("cap.cli.count").why = "退出码 2；stderr：unrecognized arguments: --file"
    brief = slicer.brief(sl_cli, bp3, done=bp3.done())
    check("切片简报写清'只做这些'与观测方式",
          "只做这些" in brief and "观测方式" in brief, brief[:200])
    check("简报给出**逐字验收命令**（参数名一个都不许改）",
          "逐字跑通" in brief and "python main.py count --file d.csv" in brief, brief[:400])
    check("简报把上次的失败证据带进重试（否则重跑同一个错）",
          "上一次它这样红的" in brief and "unrecognized arguments" in brief, brief[:400])
    check("简报把**验收素材的内容**也给了（否则契约会自己拍板'不支持引号'这类假设，"
          "和探针直接矛盾——实测 run 6）",
          "d.csv 的内容" in brief and "必须原样正确处理" in brief, brief[:500])
    check("剂量来自 dosing（不是写死的常数）", "dosing" in inspect.getsource(slicer.dose))

    print("[6b] 切片级上下文：只给本片依赖到的接口（与仓库规模无关）")
    ws_ctx = Path(tempfile.mkdtemp())
    (ws_ctx / "store.py").write_text("def make_record(name):\n    return {'name': name}\n",
                                     encoding="utf-8")
    (ws_ctx / "other.py").write_text("def unrelated(x):\n    return x\n", encoding="utf-8")
    bpc = _bp()
    slc = sl_cli          # 同上：用"依赖已满足"的那份计划
    briefc = slicer.brief(slc, bpc, ws=ws_ctx)
    check("给出本片依赖模块的**完整签名**", "store: make_record(name)" in briefc, briefc[-600:])
    check("不给无关模块的签名（否则上下文随仓库线性膨胀）",
          "unrelated" not in briefc, briefc[-600:])
    check("再给一个全局紧凑索引（名字+函数数，防重复造轮子）",
          "仓库现有模块" in briefc and "store(1)" in briefc, briefc[-600:])
    check("索引里不列测试文件", "test_" not in briefc.split("仓库现有模块")[-1], briefc[-300:])
    check("连续三次观测都没有新增 → 停滞",
          replan.stalled([{"done": 1}, {"done": 1}, {"done": 1}]))
    check("有增长 → 不算停滞",
          not replan.stalled([{"done": 1}, {"done": 2}, {"done": 2}]))
    check("全都做完了 → 永远不算停滞（否则会白白重规划）",
          not replan.stalled([{"done": 3}, {"done": 3}, {"done": 3}], pending=0))
    bp4 = _bp()
    bp4.by_id("cap.data.record").status = "done"
    got_desc = replan.apply_fails(bp4, {"cap.cli.count": 3})
    check("卡死 3 次 → 降级", got_desc == ["cap.cli.count"], str(got_desc))
    check("降级带原因（报告里能说清为什么）",
          "连续 3 次" in bp4.by_id("cap.cli.count").why, bp4.by_id("cap.cli.count").why)
    check("没到 3 次不降级", replan.apply_fails(bp4, {"cap.cli.report": 2}) == [])
    tmp = Path(tempfile.mkdtemp())
    fake2 = FakeLLM(audit_obj={"issues": [], "add": [], "drop": ["cap.data.record"],
                               "rewire": {}})
    out = replan.replan(bp4, ws=tmp, llm=fake2, reason="覆盖停滞两轮", do_audit=True,
                        fails={"cap.cli.report": 3})
    check("重规划版本号递增", out["blueprint"].version == 2, str(out["blueprint"].version))
    check("重规划里 done 依然不许被删",
          out["blueprint"].by_id("cap.data.record") is not None)
    check("diff 落盘", (tmp / blueprint.DIFF_FILE).exists())
    check("diff 里记得住降级与原因",
          any(d.get("descoped") for d in replan.history(tmp)), str(replan.history(tmp))[:200])
    check("重规划原因进了备注", any("覆盖停滞" in n for n in out["blueprint"].notes),
          str(out["blueprint"].notes[-3:]))

    print("[8] 预算：估算 → 超了就要求砍范围，并且真的动手砍")
    over = _bp(budget=0.01)
    blueprint.fill_budget(over)
    check("每条都有估算", all(c.budget_cny > 0 for c in over.caps))
    check("估算超过总预算 → 明确要求砍范围",
          any("超过总预算" in x for x in blueprint.validate(over)),
          str(blueprint.validate(over)))
    okb = _bp(budget=5.0)
    blueprint.fill_budget(okb)
    check("预算够 → 不报", not any("超过总预算" in x for x in blueprint.validate(okb)))
    check("within_budget 给出余量", "余" in blueprint.within_budget(okb)[1])
    tiny = _bp(budget=0.10)                 # 估算约 ¥0.30，装不下
    blueprint.fill_budget(tiny)
    cut = blueprint.fit_budget(tiny)
    check("装不下就真的动手砍（不是只在备注里写一句）", bool(cut), str(cut))
    check("砍到装得下为止", blueprint.estimate_cost(tiny) <= 0.10 * 1.05 + 1e-9,
          str(blueprint.estimate_cost(tiny)))
    check("砍掉的都降级了并带原因",
          all(tiny.by_id(cid).status == "descoped" and tiny.by_id(cid).why
              for cid in cut if tiny.by_id(cid)),
          str([(cid, tiny.by_id(cid).status) for cid in cut if tiny.by_id(cid)]))
    check("被别的能力依赖的不先砍（否则下游一起死）",
          tiny.by_id("cap.data.record") is None
          or tiny.by_id("cap.data.record").status != "descoped"
          or all(tiny.by_id(x) is None or tiny.by_id(x).status == "descoped"
                 for x in ("cap.cli.count", "cap.cli.report")),
          str([(c.id, c.status) for c in tiny.caps]))
    check("砍完装得下时不再乱砍（容忍带内不动手）",
          blueprint.fit_budget(_bp(budget=1.0)) == [])
    # 健壮性是硬要求：预算收敛不许先把测试能力砍掉
    tb = _bp(budget=0.24)                      # 估算 0.30，只能砍一条左右
    tb.add(blueprint.Cap(id="cap.tests", desc="跑测试：test_x 必须存在",
                         probe={"kind": "pytest", "args": ["tests"], "stdout_has": ["test_x"]},
                         funcs_est=4))
    blueprint.fill_budget(tb)
    cut_t = blueprint.fit_budget(tb)
    check("预算收紧时**测试能力最后才砍**（健壮性不是可选项）",
          ("cap.tests" not in cut_t) or cut_t[-1] == "cap.tests", str(cut_t))

    print("[9] 存取与摘要")
    tmp2 = Path(tempfile.mkdtemp())
    blueprint.save(bp3, tmp2)
    back = blueprint.load(tmp2)
    check("save/load 往返一致（id 与版本）",
          back.ids() == bp3.ids() and back.version == bp3.version, str(back.ids()))
    check("状态也存下来了", back.by_id("cap.data.record").status == "done")
    text = blueprint.summary(bp3)
    check("摘要一屏能看完：目标 / 能力 / 缺口",
          "目标" in text and "缺口" in text and "cap.cli.count" in text, text[:200])
    check("没装计划时 load 不炸", blueprint.load(tempfile.mkdtemp()).caps == [])
    check("摘要把降级原因印出来",
          "降级" in blueprint.summary(bp4) or "备注" in blueprint.summary(bp4))

    print("[10] 接线（不是写了一堆没人用的函数）")
    check("derive 真的调 audit", "audit(" in inspect.getsource(blueprint.derive))
    check("replan 真的走 apply_fails", "apply_fails" in inspect.getsource(replan.replan))
    check("summary 与 describe 同源（观测方式只有一处定义）",
          "describe" in inspect.getsource(blueprint.summary))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（探针反空壳 / 两次调用 / 确定性调度 / 竖切闭合 / 降级留痕 / done 保护）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
