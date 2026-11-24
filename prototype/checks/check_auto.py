"""全自主编排自检：一句话目标 → 蓝图 → 切片 → 生成 → 覆盖 → 重规划 → 报告（2026-11-05）。

这一层最容易出的错，全都是"报告和现实不符"：
1. 覆盖率造假 —— 把"降级"或"受阻"算成完成，报告写"全部通过"；
2. 不终止 —— 一片一直失败就一直重试，转到天亮（内循环有写次数上限，外循环没有）；
3. 一处困难拖死全局 —— 单片切片吃掉全部预算，剩下 90% 的能力一分钱没花；
4. 续跑重做 —— 已经观测通过的能力，第二轮又被重写一遍（白花钱，还把好好的代码改坏）；
5. 预算不封顶 —— 一片就花掉整项目预算。

所以这里用**假远端 + 假执行器**（不联网络、不花钱）把循环跑起来，断言上面五件事都不发生。
假执行器会真的往临时仓库里写代码，所以覆盖判定是真的走探针执行。

跑法: python check_auto.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import auto, blueprint, contractgen  # noqa: E402
from core import contract as contract_mod  # noqa: E402
from core import props  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 夹具：契约文本

CONTRACT_MODULES = [{"path": "main.py", "purpose": "本切片要做的函数（入口）",
                     "functions": [{"name": "f", "params": [{"name": "x", "type": "int"}],
                                    "returns": "int", "desc": "加一",
                                    "examples": ["f(1) == 2"]}]}]
SLICE_CONTRACT_TEXT = contractgen.render_text(CONTRACT_MODULES)


def fake_synthesize(requirement, **kw):
    return {"ok": True, "contracts": contractgen.contract_mod.parse_requirement(SLICE_CONTRACT_TEXT),
            "text": SLICE_CONTRACT_TEXT, "by": "fake", "problems": [],
            "allowance": 8, "allowance_why": ["测试"]}


# ---------------------------------------------------------------- 夹具：蓝图（假远端）

CAPS = [
    {"id": "cap.store.record", "desc": "make_record('a') 返回 {'name': 'a'}",
     "probe": {"kind": "py_call", "module": "store", "func": "make_record",
               "args": ["a"], "expect": {"name": "a"}},
     "deps": [], "funcs_est": 10, "modules": ["store.py"]},
    {"id": "cap.cli.count", "desc": "统计 CSV 记录条数",
     "probe": {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
               "files": {"d.csv": "name\na\nb\n"}, "stdout_has": "2"},
     "deps": ["cap.store.record"], "funcs_est": 30, "modules": ["main.py"]},
    {"id": "cap.cli.report", "desc": "打印汇总",
     "probe": {"kind": "cli", "entry": "main.py", "args": ["report"], "stdout_has": "ok"},
     "deps": ["cap.cli.count"], "funcs_est": 30, "modules": ["main.py"]},
]


class FakeLLM:
    def __init__(self, caps=None):
        self.caps = CAPS if caps is None else caps
        self.calls: list[str] = []

    def __call__(self, prompt, **kw):
        self.calls.append(prompt)
        obj = ({"issues": []} if "独立评审" in prompt else {"caps": self.caps})
        return {"ok": True, "answer": "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"}


STORE_OK = 'def make_record(name):\n    return {"name": name}\n'
# 新架构：远端只写 `cli_run(argv) -> (退出码, 文本)`，入口接线由**引擎**生成。
CORE_OK = '''\
def cli_run(argv):
    cmd = argv[0] if argv else ""
    if cmd == "count":
        path = argv[argv.index("--file") + 1] if "--file" in argv else ""
        try:
            with open(path, encoding="utf-8") as fh:
                rows = [ln for ln in fh.read().splitlines() if ln.strip()][1:]
        except OSError:
            return 1, "文件不存在: " + path
        return 0, str(len(rows))
    if cmd == "report":
        return 0, "ok: 汇总"
    return 2, "unknown"
'''
MAIN_COUNT = CORE_OK
MAIN_FULL = CORE_OK


def make_runner(log: list, write_code: bool = True, cost: float = 0.05):
    """假执行器：按简报里列出的能力，真的往仓库里写代码（这样覆盖是真判的）。"""
    def run(ws, brief, contract_path, **kw):
        import re
        # 只取"本轮要做的能力"这一节：简报后面还有"不要改坏"清单（已通过的能力），
        # 那是**保护名单**，不是本轮的活
        head = brief.split("# 不要改坏")[0].split("# 入口脚本的硬要求")[0]
        caps = re.findall(r"^- (cap\.[a-z0-9_.]+)：", head, re.M)
        log.append({"caps": caps, "contract": contract_path, "brief": brief})
        if write_code:
            ws = Path(ws)
            if "cap.store.record" in caps:
                (ws / "store.py").write_text(STORE_OK, encoding="utf-8")
            if "cap.cli.count" in caps or "cap.cli.report" in caps:
                # 引擎约定：CLI 逻辑统一放 `cli_core.py`（入口 main.py 由引擎生成并 import 它）。
                # 早期这里写的是 core.py —— 与实现脱节后，main.py 导入失败 → 能力反复红 →
                # 连续 3 次被降级 → "没有可做的切片"（2026-11-23 查明）。
                (ws / "cli_core.py").write_text(CORE_OK, encoding="utf-8")
        return {"ok": True, "reason": "假执行器", "verified": 1, "total": 1, "spent_s": 0.1,
                "cost_cny": cost, "delivery": ""}
    return run


class Emitter:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind, payload=None):
        self.events.append((kind, payload or {}))

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]


def main() -> int:
    contractgen.synthesize = fake_synthesize        # 全程不联网络

    print("全自主编排自检（覆盖驱动 / 预算封顶 / 降级留痕 / 可续跑）")

    print("[1] 端到端：一句话 + 假远端 + 假执行器 → 覆盖到 100%")
    ws = Path(tempfile.mkdtemp(prefix="auto-"))
    log: list = []
    em = Emitter()
    llm = FakeLLM()
    res = auto.night_auto(ws, "做一个 CSV 统计工具", llm=llm,
                          runner=make_runner(log), emit=em)
    check("按能力数全部通过才 ok", bool(res["ok"]), str(res["reason"]))
    check("覆盖 3/3", res["ratios"] == {"done": 3, "total": 3, "pct": 100.0}, str(res["ratios"]))
    n_sl = int(res["slices"])
    check("至少跑了一片，且每片都有真实产出", n_sl >= 1 and len(log) == n_sl, str((n_sl, len(log))))
    check("每片都带契约路径（不再有片级预算参数）",
          all(row.get("contract") for row in log), str(log[:1]))
    check("花费按切片累加", abs(res["spent_cny"] - 0.05 * n_sl) < 1e-6,
          f"{res['spent_cny']} vs {0.05 * n_sl}")
    check("蓝图落盘（续跑要用）", (ws / blueprint.BLUEPRINT_FILE).exists())
    check("覆盖历史落盘", (ws / ".sixcoder" / "coverage.jsonl").exists())
    report = Path(res["report"]).read_text(encoding="utf-8")
    check("报告写清覆盖比例", "3/3" in report, report[:160])
    check("报告里没有假的'全部通过'字样", "全部通过" not in report)
    check("事件里有切片开始/结束/收工",
          {"slice_start", "slice_done", "autonomy_done"} <= set(em.kinds()), str(em.kinds()))
    check("依赖顺序生效：report 能力不在第一片里（多片时）",
          n_sl == 1 or "cap.cli.report" not in log[0]["caps"],
          str([row["caps"] for row in log]))
    check("每片都有契约文件（可追溯）",
          all(Path(row["contract"]).exists() for row in log))
    sids = [res["blueprint"].by_id(c.id).slice_id for c in res["blueprint"].caps]
    check("能力记住了自己属于哪个切片（能力 → 片 → 契约 → 该片交付报告；"
          "观测顺带通过的也会标『·观测』）",
          all(sids) and len({s.split("·")[0] for s in sids}) >= 1,
          str([(c.id, c.slice_id) for c in res["blueprint"].caps]))
    check("每片用不同的契约文件（片 id 由内容决定，不会串用旧考卷）",
          len({row["contract"] for row in log}) == n_sl, str([row["contract"] for row in log]))
    cli_row = next((row for row in log if "cap.cli.count" in row["caps"]), None)
    check("含 CLI 能力的那片存在（否则没法验命令行绑定）", cli_row is not None,
          str([row["caps"] for row in log]))
    ctext = Path(cli_row["contract"]).read_text(encoding="utf-8") if cli_row else ""
    check("契约里被机械塞进了**纯函数**的验收（cli_run(argv) → 引擎接线，远端只写逻辑）",
          "cli_run(" in ctext and ")[1]) == True" in ctext, ctext[:400])
    check("而且配了 cli_run 签名（否则示例会被当成幻觉丢掉）",
          "- cli_run(argv: list[str]) -> tuple[int, str]：" in ctext, ctext[:400])
    check("夹具素材按探针编号改名写进脚本（多探针不互相覆盖）",
          "open('_p0_d.csv'" in ctext, ctext[:400])
    check("入口脚本是引擎生成的接线（cli_run → 打印 → 退出码）",
          "由引擎生成" in (ws / "main.py").read_text(encoding="utf-8"),
          str([p.name for p in ws.glob("*.py")]))
    check("CLI 片的契约里**没有入口模块块**（入口由引擎独占，远端写进去会消失）",
          "1) main.py" not in ctext and "cli_core.py" in ctext, ctext[:300])

    print("[2] 续跑：已通过的能力不重做（不许白花钱、更不许改坏）")
    log2: list = []
    res2 = auto.night_auto(ws, "", llm=FakeLLM(), runner=make_runner(log2))
    check("续跑没有重新开工", log2 == [], str(log2))
    check("续跑仍然是完成态", bool(res2["ok"]) and res2["ratios"]["pct"] == 100.0, str(res2["ratios"]))
    check("续跑原因说清是'待办清空'", "待办" in res2["reason"], res2["reason"])

    print("[3] 预算上限机制已移除（2026-11-10）：不再按钱停机/砍范围；有缺口仍要如实说")
    SMALL_CAPS = [
        {"id": "cap.store.record", "desc": "make_record('a') 返回 {'name': 'a'}",
         "probe": {"kind": "py_call", "module": "store", "func": "make_record",
                   "args": ["a"], "expect": {"name": "a"}}, "deps": [], "funcs_est": 3,
         "modules": ["store.py"]},
        {"id": "cap.cli.report", "desc": "打印汇总",
         "probe": {"kind": "cli", "entry": "main.py", "args": ["report"], "stdout_has": "ok"},
         "deps": [], "funcs_est": 3, "modules": ["main.py"]},
    ]
    ws3 = Path(tempfile.mkdtemp(prefix="auto-"))
    log3: list = []
    # 一片就花了 ¥0.20（超过总预算 ¥0.15）→ 下一轮必须停，并把缺口如实列出
    res3 = auto.night_auto(ws3, "做一个 CSV 统计工具", max_slices=1,
                           llm=FakeLLM(caps=SMALL_CAPS),
                           runner=make_runner(log3, write_code=False, cost=0.20))
    check("不因花钱而停（唯一停机线=系统预算×1.05，测试环境未设→跑到 max_slices）",
          "预算" not in res3["reason"], res3["reason"])
    check("只跑了 1 片（max_slices 生效）", res3["slices"] == 1, str(res3["slices"]))
    check("有缺口时 ok 必须是 False（不许报完成）", not res3["ok"], str(res3["ok"]))
    check("缺口列出来了", len(res3["gaps"]) >= 1, str(res3["gaps"]))
    # 旧的「预算装不下 → 砍范围（budget_cut）」机制已删除：静态钉死不许回来
    _src = (Path(__file__).resolve().parents[1] / "core" / "auto.py").read_text(
        encoding="utf-8")
    check("budget_cut 缩范围机制已移除", "budget_cut" not in _src)

    print("[4] 停滞 → 重规划 → 降级留痕（并且一定终止）")
    ws4 = Path(tempfile.mkdtemp(prefix="auto-"))
    log4: list = []
    res4 = auto.night_auto(ws4, "做一个 CSV 统计工具", llm=FakeLLM(),
                           runner=make_runner(log4, write_code=False))
    check("一句能力都没做成 → ok=False", not res4["ok"], str(res4["ok"]))
    check("至少重规划过一次", res4["replans"] >= 1, str(res4["replans"]))
    check("卡住的能力被降级", len(res4["descoped"]) >= 1, str(res4["descoped"]))
    check("降级带原因（不是静默消失）",
          all("连续" in (bp.why or "") or "降级" in (bp.why or "") for bp in res4["blueprint"].caps
              if bp.status == "descoped"), str([(c.id, c.why) for c in res4["blueprint"].caps
                                                if c.status == "descoped"]))
    rep4 = Path(res4["report"]).read_text(encoding="utf-8")
    check("报告里有降级章节", "降级" in rep4, rep4[:200])
    check("报告结论不撒谎（没有任何'完成'的口径）",
          not res4.get("ok") and ("全部能力已通过观测" not in res4["reason"]), res4["reason"])
    check("没有转到天亮（循环终止且次数有限）", len(log4) <= auto.MAX_ITERATIONS, str(len(log4)))
    check("重试时简报带上了上次的失败证据（否则重跑同一个错）",
          any("上一次它这样红的" in (row.get("brief") or "") for row in log4[1:]),
          str([(r.get("brief") or "")[:60] for r in log4[:3]]))

    print("[4b] 回归：后面的切片把已通过的能力改坏 → 必须被发现并写进报告")
    REGR = [
        {"id": "cap.store.record", "desc": "make_record('a') 返回 {'name': 'a'}",
         "probe": {"kind": "py_call", "module": "store", "func": "make_record",
                   "args": ["a"], "expect": {"name": "a"}},
         "deps": [], "funcs_est": 5, "modules": ["store.py"]},
        {"id": "cap.tests", "desc": "跑测试：test_a 必须存在",
         "probe": {"kind": "pytest", "args": ["tests/test_a.py"], "stdout_has": ["test_a"]},
         "deps": ["cap.store.record"], "funcs_est": 40, "modules": ["tests/test_a.py"]},
    ]
    ws4b = Path(tempfile.mkdtemp(prefix="auto-"))
    reg_log: list = []

    def reg_runner(ws, brief, contract_path, **kw):
        import re as _re
        head = brief.split("# 不要改坏")[0].split("# 入口脚本的硬要求")[0]

        caps = _re.findall(r"^- (cap\.[a-z0-9_.]+)：", head, _re.M)
        reg_log.append(caps)
        ws = Path(ws)
        if "cap.store.record" in caps:
            (ws / "store.py").write_text(STORE_OK, encoding="utf-8")
        if "cap.tests" in caps and "cap.store.record" not in caps:
            (ws / "tests").mkdir(exist_ok=True)
            (ws / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n",
                                                    encoding="utf-8")
            # 模拟"整文件重写把别人改坏"：这一片顺手把 store.py 覆盖成了错的
            (ws / "store.py").write_text("def make_record(name):\n    return None\n",
                                         encoding="utf-8")
        return {"ok": True, "reason": "假执行器", "verified": 1, "total": 1, "spent_s": 0.1,
                "cost_cny": 0.05, "delivery": ""}
    res4b = auto.night_auto(ws4b, "做一个带测试的小工具", max_slices=1,
                            llm=FakeLLM(caps=REGR), runner=reg_runner)
    check("先让第一条能力通过（为回归提供前提）",
          res4b["blueprint"].by_id("cap.store.record").status == "done",
          res4b["blueprint"].by_id("cap.store.record").status)
    res4b2 = auto.night_auto(ws4b, "做一个带测试的小工具", max_slices=1,
                             llm=FakeLLM(caps=REGR), runner=reg_runner)
    regs = res4b2.get("regressions") or []
    check("回归被记录（绿→红不能悄悄发生）", bool(regs), str(regs))
    check("回归指向具体能力与片号",
          any(r.get("cap") == "cap.store.record" and r.get("slice") for r in regs), str(regs))
    check("回归事实是持久的（修好之后也留档，不是随 why 一起被清空）",
          any("回归" in n and "cap.store.record" in n for n in (res4b2["blueprint"].notes or [])),
          str((res4b2["blueprint"].notes or [])[-3:]))
    rep4b = Path(res4b2["report"]).read_text(encoding="utf-8")
    check("报告里有回归章节（人能看到'哪些本来是通的'）", "回归" in rep4b, rep4b[:200])
    res4b3 = auto.night_auto(ws4b, "做一个带测试的小工具", max_slices=2,
                             llm=FakeLLM(caps=REGR), runner=reg_runner)
    check("回归之后还能把它修回来（不放弃）",
          res4b3["blueprint"].by_id("cap.store.record").status == "done",
          res4b3["blueprint"].by_id("cap.store.record").status)

    print("[5] 切片契约：按片缓存 + 只写模块名的假契约不许当缓存用")
    print("[5a] 入口验收命令必须机械写进契约示例（否则内循环绿、真验收红）")
    t1 = "1) main.py：说明\n   - add(a: int) -> int：加一\n\n示例（必须满足）：\n（无）\n"
    f1 = auto.ensure_entry_binding(t1, "main.py", "main(['--file', 'd.csv']) == 0")
    check("空示例槽被填上入口调用", "main(['--file', 'd.csv']) == 0" in f1, f1)
    check("'（无）'占位被替换（不是两行并存）", "（无）" not in f1, f1)
    check("契约里**同时**插入 main(argv) 签名（没这个符号，props 会把示例当幻觉丢掉）",
          "- main(argv: list[str]) -> int：" in f1, f1)
    parsed = contractgen.contract_mod.parse_requirement(f1)
    names = [getattr(f, "name", "") for v in parsed.values()
             for f in (getattr(v, "funcs", None) or [])]
    check("解析后 main 真的成了待办函数（结构体检才会派人写）",
          "main" in names and "add" in names, str(names))
    check("幂等：再绑一次不重复插",
          auto.ensure_entry_binding(f1, "main.py", "main(['--file', 'd.csv']) == 0") == f1)
    f1b = auto.ensure_entry_binding(t1, "main.py", "main(['--file', 'd.csv']) == 0",
                                    "退出码 2；stderr：unrecognized arguments: --file")
    check("失败证据被贴到 main 的规范字段里（作者 prompt 只有函数级规范）",
          "【上一轮实测失败】" in f1b and "unrecognized arguments: --file" in f1b, f1b)
    check("已经贴过就不重复贴",
          auto.ensure_entry_binding(f1b, "main.py", "main(['--file', 'd.csv']) == 0",
                                    "另一条证据") == f1b)
    check("入口脚本必须被要求写 __main__ 收尾（否则函数级绿、真命令行红）",
          'sys.exit(main(sys.argv[1:]))' in f1, f1)
    t2 = "1) a.py：\n   - f(x: int) -> int：加一\n示例（必须满足）：\nf(1) == 2\n"
    f2 = auto.ensure_entry_binding(t2, "a.py", "main([]) == 0")
    check("已有示例 → 追加，不覆盖原示例", "f(1) == 2" in f2 and "main([])" in f2, f2)
    check("入口不在契约里 → 把入口模块改名对齐（探针的 entry 就是硬约定）",
          "1) b.py：" in auto.ensure_entry_binding(t2, "b.py", "main([]) == 0"),
          auto.ensure_entry_binding(t2, "b.py", "main([]) == 0"))

    print("[5b] 夹具 + 入口调用 → 函数级判据真的执行（两套判据合流）")
    prep = auto.prep_lines({"files": {"d.csv": "team,amount\nA,10\n"}})
    check("探针素材被写成前置准备行", bool(prep) and "d.csv" in prep[0] and "write(" in prep[0],
          str(prep))
    bound = auto.ensure_entry_binding(t1, "main.py", "main(['--file', 'd.csv']) == 0",
                                      "", prep)
    check("准备行插在断言之前（否则夹具还没生成就去跑命令）",
          bound.index("open(") < bound.index("main(["), bound)
    wsx = Path(tempfile.mkdtemp(prefix="bind-"))
    tmpl = ("1) m.py：入口\n   - main(argv: list[str]) -> int：入口\n\n"
            "示例（必须满足）：\n（无）\n")
    bound = auto.ensure_entry_binding(tmpl, "m.py", "main(['--file', 'd.csv']) == 0",
                                      "", prep)
    good = ('import argparse\n\n\ndef main(argv):\n    p = argparse.ArgumentParser()\n'
            '    p.add_argument("--file", required=True)\n    p.parse_args(argv)\n'
            '    return 0\n')
    bad = ('import argparse\n\n\ndef main(argv):\n    p = argparse.ArgumentParser()\n'
           '    p.add_argument("src")\n    p.parse_args(argv)\n    return 0\n')
    got_checks = props.from_contract(list(contract_mod.parse_requirement(bound).values())[0])
    (wsx / "m.py").write_text(good, encoding="utf-8")
    r_good = props.run(wsx, "m.py", got_checks)
    (wsx / "m.py").write_text(bad, encoding="utf-8")
    r_bad = props.run(wsx, "m.py", got_checks)
    check("正确实现（支持 --file）→ 函数级判据通过", bool(r_good.get("ok")), str(r_good)[:200])
    check("位置参数实现 → 函数级判据变红（命令行形状被管住了）",
          not r_bad.get("ok"), str(r_bad)[:200])
    check("argparse 拒绝参数（SystemExit）→ 是**判据红**，不是'引擎自身跑不动'",
          not r_bad.get("engine_error"), str(r_bad)[:220])
    check("红的时候报的就是 argparse 的真实错（作者能照着改）",
          "unrecognized arguments" in json.dumps(r_bad, ensure_ascii=False)
          or "SystemExit" in json.dumps(r_bad, ensure_ascii=False), str(r_bad)[:200])

    print("[5c] 输出文本也要被管住（只判退出码时，格式错了照样全绿）")
    p_out = {"kind": "cli", "entry": "m.py", "args": ["--file", "d.csv"],
             "files": {"d.csv": "x\n"}, "stdout_has": "hello"}
    groups = auto.entry_block(p_out)
    flat = [x for g in groups for x in g]
    check("输出期望被机械翻译成脚本行（含 stdout 重定向 + 夹具改名）",
          any("redirect_stdout" in x for x in flat)
          and any("hello" in x and "_txt0" in x for x in flat)
          and any("_p0_d.csv" in x for x in flat), str(flat)[:300])
    ws2 = Path(tempfile.mkdtemp(prefix="bind2-"))
    bound2 = auto.ensure_entry_binding(
        "1) m.py：入口\n   - main(argv: list[str]) -> int：入口\n\n示例（必须满足）：\n（无）\n",
        "m.py", "main(['--file', 'd.csv']) == 0", "", groups=groups)
    checks2 = props.from_contract(list(contract_mod.parse_requirement(bound2).values())[0])
    ok_src = ('import argparse\n\n\ndef main(argv):\n    p = argparse.ArgumentParser()\n'
              '    p.add_argument("--file", required=True)\n    p.parse_args(argv)\n'
              '    print("hello")\n    return 0\n')
    bad_out = ok_src.replace('print("hello")', 'print("bye")')
    (ws2 / "m.py").write_text(ok_src, encoding="utf-8")
    r_ok = props.run(ws2, "m.py", checks2)
    (ws2 / "m.py").write_text(bad_out, encoding="utf-8")
    r_bado = props.run(ws2, "m.py", checks2)
    check("输出正确 → 判据通过", bool(r_ok.get("ok")), str(r_ok)[:220])
    check("退出码 0 但输出不对 → 判据红（这才是真验收）",
          not r_bado.get("ok"), str(r_bado)[:220])
    ws5 = Path(tempfile.mkdtemp(prefix="auto-"))
    sl = {"id": "s01", "caps": ["cap.store.record"], "funcs": 8, "modules": ["store.py"]}
    d = ws5 / auto.SLICE_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "s01.txt").write_text(SLICE_CONTRACT_TEXT, encoding="utf-8")
    got = auto.contract_for_slice(ws5, sl, "简报")
    check("同一片复用同一份考卷（可复现）", got.get("by") == "cached", str(got.get("by")))
    (d / "s02.txt").write_text("1) store.py：\n\n示例（必须满足）：\n（无）\n", encoding="utf-8")
    got2 = auto.contract_for_slice(ws5, {"id": "s02", "caps": [], "funcs": 8, "modules": []}, "简报")
    check("只有模块名、0 个函数的契约不算缓存（会被重新合成）",
          got2.get("by") != "cached", str(got2))
    # 口径（2026-11-12）：落盘文本 = 原考卷 + 引擎接线约束尾巴（auto._wire_constraints，
    # auto.py:2726-2728 —— 接线约束必须进契约，远端才知道引擎要求什么）。
    # 故这里断言"原考卷被完整保留"，而不是与原文本全等（那是口径过期，不是代码错）。
    _landed = (d / "s02.txt").read_text(encoding="utf-8").strip()
    check("重新合成后落盘（原考卷完整保留 + 接线约束尾巴）",
          SLICE_CONTRACT_TEXT.strip() in _landed, _landed[:200])

    print("[5d] 入口示例不许泄漏到别的模块（否则会去重写不相干的模块）")
    TEXT2 = ("1) entry.py：入口\n   - main(argv: list[str]) -> int：入口\n\n"
             "示例（必须满足）：\n"
             "open(\"d.csv\", 'w', encoding='utf-8').write(\"a,b\\n1,2\\n\")\n"
             "main(['--file', 'd.csv']) == 0\n\n"
             "2) helper.py：辅助\n   - parse_args(x: int) -> int：加一\n\n"
             "示例（必须满足）：\nparse_args(1) == 2\n")
    _mods = contract_mod.parse_requirement(TEXT2)
    _ent = props.from_contract(_mods["entry.py"])
    _hel = props.from_contract(_mods["helper.py"])
    check("入口模块拿到脚本判据（夹具 + main 调用）",
          any(x.get("kind") == "expr_seq" for x in _ent), str([x.get("kind") for x in _ent]))
    check("别的模块不会拿到入口脚本（跨模块泄漏 —— 实测让一轮重写了三个无关模块）",
          not any(x.get("kind") == "expr_seq" for x in _hel),
          str([x.get("kind") for x in _hel]))
    check("别的模块只拿到自己的示例",
          any(x.get("kind") == "call_eq" and "parse_args" in str(x.get("expr"))
              for x in _hel), str(_hel)[:200])
    _raw = ("1) core.py：计算\n   - add(a: int) -> int：加一\n\n示例（必须满足）：\nadd(1) == 2\n\n"
            "2) main.py：入口\n   - main(argv: list[str]) -> int：入口\n")
    _aligned = auto.align_entry_module(_raw, "csvstat.py")
    check("契约没按探针的入口文件名造模块 → 机械改名对齐（否则真命令行永远跑不通）",
          "2) csvstat.py：" in _aligned and "2) main.py：" not in _aligned, _aligned)
    check("名字已经对上就原样返回",
          auto.align_entry_module(_aligned, "csvstat.py") == _aligned)

    print("[6] 预算分配：封顶 / 地板 / 不超过余额")
    check("单切片封顶在 15%", abs(auto.slice_budget(10.0, 10.0, 2) - 1.5) < 1e-9,
          str(auto.slice_budget(10.0, 10.0, 2)))
    check("不超过剩余钱", auto.slice_budget(10.0, 0.2, 2) <= 0.2 + 1e-9,
          str(auto.slice_budget(10.0, 0.2, 2)))
    check("没钱时返回 0（不假装有预算）", auto.slice_budget(0.0, 0.0, 3) == 0.0)
    check("份额太小也保底（否则连一次契约合成都付不起）",
          auto.slice_budget(0.4, 0.4, 10) >= auto.SLICE_BUDGET_FLOOR,
          str(auto.slice_budget(0.4, 0.4, 10)))

    print("[7] 入口接线：缺 __main__ 就由引擎补 1 行（否则命令行永远跑不通）")
    ws_w = Path(tempfile.mkdtemp(prefix="wire-"))
    (ws_w / "e.py").write_text("def main(argv):\n    print('ok')\n    return 0\n",
                               encoding="utf-8")
    w1 = auto.ensure_entry_wiring(ws_w, "e.py")
    check("缺接线 → 补上并报告", w1.get("patched") and "__main__" in
          (ws_w / "e.py").read_text(encoding="utf-8"), str(w1))
    check("补完能直接当脚本跑",
          "ok" in __import__("subprocess").run(
              [__import__("sys").executable, str(ws_w / "e.py")],
              capture_output=True, text=True).stdout)
    check("幂等：第二次不再改", auto.ensure_entry_wiring(ws_w, "e.py").get("patched") is False)
    (ws_w / "n.py").write_text("VALUE = 1\n", encoding="utf-8")
    check("没有 main() 就不硬补（如实说做不到）",
          auto.ensure_entry_wiring(ws_w, "n.py").get("ok") is False,
          str(auto.ensure_entry_wiring(ws_w, "n.py")))
    check("入口不存在 → 如实说", auto.ensure_entry_wiring(ws_w, "nope.py").get("ok") is False)

    print("[8b] 「每次调用必须有意义」：无收益的切片必须被计数并改变策略")
    synth_calls = [0]
    _orig_synth = contractgen.synthesize

    def _counting_synth(*a, **kw):
        synth_calls[0] += 1
        return _orig_synth(*a, **kw)
    contractgen.synthesize = _counting_synth
    ws_m = Path(tempfile.mkdtemp(prefix="mean-"))
    mlog: list = []

    def stall_runner(ws, brief, contract_path, **kw):
        import re as _re
        head = brief.split("# 不要改坏")[0].split("# 入口脚本的硬要求")[0]
        mlog.append({"caps": _re.findall(r"^- (cap\.[a-z0-9_.]+)：", head, _re.M),
                     "contract": contract_path})
        return {"ok": True, "reason": "什么都不写", "verified": 0, "total": 1,
                "spent_s": 0.1, "cost_cny": 0.05, "delivery": ""}
    mres = auto.night_auto(ws_m, "做一个 CSV 统计工具", max_slices=3,
                           llm=FakeLLM(caps=SMALL_CAPS), runner=stall_runner)
    check("无收益的切片被计为无效尝试", mres["wasted"] >= 1, str(mres["wasted"]))
    check("有效调用率如实反映（无效 > 0 就不是 100%）",
          mres["meaningful_rate"] < 1.0, str(mres["meaningful_rate"]))
    # 口径变更（2026-11-07）：**无效之后不重合成考卷**。旧断言要求"不无效就重新合成"，
    # 结果是同一片三份考卷、已完成模块全部判退化、da.py 重写 7 次。
    # 现在无效只缩片 + 换打法（fail_note 带证据），考卷保持同一份 → 可复现、可比对。
    check("无效之后仍用同一份考卷（可复现）", synth_calls[0] <= max(1, mres["slices"]),
          f"合成 {synth_calls[0]} 次 / 切片 {mres['slices']} 次（>越界说明又换考卷了）")
    mrep = Path(mres["report"]).read_text(encoding="utf-8")
    check("报告里写明意义性（人能看到多少次调用是白花）", "意义性" in mrep, mrep[:200])

    print("[9] 轮数上限与'全降级不等于完成'")
    ws7 = Path(tempfile.mkdtemp(prefix="auto-"))
    res7 = auto.night_auto(ws7, "做一个 CSV 统计工具", llm=FakeLLM(),
                           runner=make_runner([], write_code=False), max_slices=1)
    check("max_slices 生效", res7["slices"] == 1, str(res7["slices"]))
    all_desc = blueprint.Blueprint(goal="x", caps=[
        blueprint.Cap(id="cap.a", desc="a", probe={"kind": "py_call", "module": "m", "func": "f",
                                                   "expect": 1}, status="descoped", why="测试"),
        blueprint.Cap(id="cap.b", desc="b", probe={"kind": "py_call", "module": "m", "func": "g",
                                                   "expect": 1}, status="descoped", why="测试")])
    blueprint.save(all_desc, ws7)
    res8 = auto.night_auto(ws7, "", llm=FakeLLM(), runner=make_runner([]))
    check("全部降级 → ok=False（不许当完成）", not res8["ok"], str(res8["ok"]))
    check("全部降级 → 明确说要人工介入", "人工介入" in res8["reason"], res8["reason"])

    # 考卷（切片契约）一旦生成，**不许因为"这次没收获"就重合成**：
    # 新考卷语义一变，之前通过的模块全被判退化 → 全量重写 → 又没收获 → 又换考卷。
    # 实测 2026-11-07：同一片被换了三份考卷（{"student"} → {"student_id"}），da.py 重写 7 次。
    src_auto = Path(auto.__file__).read_text(encoding="utf-8")
    check("没收获时不许换考卷", "force_refresh = True" not in src_auto,
          "auto.py 里又出现了 force_refresh = True：考卷会被重合成，已完成模块全部作废")
    check("没收获要发 slice_wasted 事件（可观测）", "slice_wasted" in src_auto)

    # 探针期望值必须跟着契约走（2026-11-07：探针要 s42、契约写 v1，能力永远 absent）
    class _Cap:
        def __init__(self, probe):
            self.probe = probe
    class _BP:
        def __init__(self, caps):
            self._c = caps
        def by_id(self, cid):
            return self._c.get(cid)
    _probe = {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/x", "expect_status": 201,
         "expect_json": {"dataset_version": "small_2x2-s42", "students": 2}}]}
    _bp2 = _BP({"cap.x": _Cap(_probe)})
    _sl = {"id": "s1", "caps": ["cap.x"]}
    _text = ('- seed_dataset 返回 {"dataset_version": "small_2x2-v1", "students": 2}。'
             '- dataset_version 固定为字符串 "small_2x2-v1"。')
    n = auto._sync_probe_expectations(_bp2, _sl, _text)
    ej = _probe["steps"][0]["expect_json"]
    check("探针字符串字段与契约对齐", n == 1 and ej["dataset_version"] == "small_2x2-v1",
          str(ej))
    check("数字字段不动（数字错了是真 bug）", ej["students"] == 2, str(ej))
    check("契约里没有的字段不动", ej.get("nope", "没加") == "没加", str(ej))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（覆盖驱动循环 / 预算封顶 / 降级留痕 / 可续跑 / 一定终止）")
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
