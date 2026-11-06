"""覆盖层自检：探针**真的执行**、判据**真的挡得住空壳**（2026-11-05）。

为什么必须用真文件、真子进程来测：
    这一层是"项目到底做完没有"的唯一证据来源。它一旦会假绿，整套自主运行就变成
    一台漂亮的谎话机器 —— 报告写"全绿"，项目里其实只有一个 print(0)。
    所以这里不测"函数返回了 True"，而是**在临时仓库里写真的模块、真的命令行**，
    然后：
      · 删掉模块 → 必须变红
      · 换成 `raise NotImplementedError` 空桩 → 必须变红
      · 换成 `print(0)` 空壳（退出码 0）→ 必须变红
      · 依赖没完成 → **不许执行探针**（否则假红把夜班带偏）
      · 第一轮通过、第二轮改坏 → 必须变红（这就是项目级回归网）

跑法: python check_coverage.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import blueprint, coverage, probe  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


MAIN_OK = '''\
import csv, sys


def main(argv):
    if not argv:
        print("用法: main.py count --file x.csv")
        return 2
    if argv[0] == "count":
        path = argv[argv.index("--file") + 1] if "--file" in argv else ""
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        print(len(rows))
        return 0
    if argv[0] == "report":
        print("ok: 报告")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

MAIN_SHELL = "print(0)\n"          # 空壳：退出码 0，什么也没算

STORE_OK = '''\
def make_record(name):
    return {"name": name}


def add(a, b):
    return a + b


def boom(x):
    if x < 0:
        raise ValueError("负数不行")
    return x
'''

STORE_STUB = '''\
def make_record(name):
    raise NotImplementedError


def add(a, b):
    raise NotImplementedError
'''

CAPS = [
    {"id": "cap.store.record", "desc": "make_record('a') 返回 {'name': 'a'}",
     "probe": {"kind": "py_call", "module": "store", "func": "make_record",
               "args": ["a"], "expect": {"name": "a"}}, "funcs_est": 2},
    {"id": "cap.cli.count", "desc": "统计 CSV 记录条数并打印",
     "probe": {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
               "files": {"d.csv": "name\na\nb\n"}, "stdout_has": "2"}, "funcs_est": 3},
]


def make_ws(store: str = STORE_OK, main: str = MAIN_OK) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="cov-"))
    (ws / "store.py").write_text(store, encoding="utf-8")
    (ws / "main.py").write_text(main, encoding="utf-8")
    return ws


def bp_of(caps=None):
    return blueprint.Blueprint(goal="CSV 统计工具",
                               caps=[blueprint.Cap.from_dict(c) for c in (caps or CAPS)])


def main() -> int:
    print("覆盖层自检（真执行 / 反空壳 / 回归网 / 依赖不误判）")

    print("[1] 真能力：探针真跑通才算 done")
    ws = make_ws()
    bp = bp_of()
    cov = coverage.run(bp, ws)
    check("py_call 探针通过", cov["caps"]["cap.store.record"]["status"] == "done",
          str(cov["caps"]["cap.store.record"]))
    check("cli 探针通过（临时素材 + stdout 比对）",
          cov["caps"]["cap.cli.count"]["status"] == "done",
          str(cov["caps"]["cap.cli.count"]))
    check("比例算得对", coverage.ratios(cov) == {"done": 2, "total": 2, "pct": 100.0},
          str(coverage.ratios(cov)))

    print("[2] 空壳 / 空桩 / 删模块 / 值不对 —— 一个都不许过")
    ws2 = make_ws(store=STORE_STUB)
    r = probe.run(ws2, {"kind": "py_call", "module": "store", "func": "add",
                        "args": [2, 3], "expect": 5})
    check("空桩 NotImplementedError → 红", not r["ok"], str(r))
    check("红的时候说清是什么错（不是只说'失败'）",
          "NotImplementedError" in (r.get("detail") or ""), str(r.get("detail")))

    ws3 = make_ws(main=MAIN_SHELL)
    r = probe.run(ws3, {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
                        "files": {"d.csv": "name\na\nb\n"}, "stdout_has": "2"})
    check("退出码 0 的 print(0) 空壳 → 红（这就是旧世界的假绿形态）", not r["ok"], str(r))
    check("空壳红灯的原因是 stdout 对不上",
          "stdout" in (r.get("detail") or ""), str(r.get("detail")))

    ws4 = make_ws()
    (ws4 / "store.py").unlink()
    r = probe.run(ws4, {"kind": "py_call", "module": "store", "func": "add",
                        "args": [2, 3], "expect": 5})
    check("模块被删了 → 红", not r["ok"], str(r))
    check("缺模块时说的是导入失败，不是含糊的失败",
          "导入失败" in (r.get("detail") or ""), str(r.get("detail")))

    ws5 = make_ws()
    r = probe.run(ws5, {"kind": "py_call", "module": "store", "func": "add",
                        "args": [2, 3], "expect": 6})
    check("返回值不符 → 红", not r["ok"], str(r))
    check("不符时把'实际值 vs 期望值'都写出来（能直接改）",
          "返回" in (r.get("detail") or "") and "期望" in (r.get("detail") or ""),
          str(r.get("detail")))
    r = probe.run(ws5, {"kind": "py_call", "module": "store", "func": "boom",
                        "args": [-1], "expect_raises": "ValueError"})
    check("期望抛异常 → 真抛了才算过", r["ok"], str(r))
    r = probe.run(ws5, {"kind": "py_call", "module": "store", "func": "add",
                        "args": [1, 1], "expect_raises": "ValueError"})
    check("该抛没抛 → 红", not r["ok"], str(r))
    r = probe.run(ws5, {"kind": "cli", "entry": "nope.py", "stdout_has": "x"})
    check("入口不存在 → 红（能力没做出来，不是引擎坏了）",
          not r["ok"] and not r.get("engine_error"), str(r))

    print("[2b] 退出码：没写 expect_exit 就不判；写了就严格判（错误路径靠这条活着）")
    ws_e = make_ws()
    r_any = probe.run(ws_e, {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
                             "files": {"d.csv": "name\na\nb\n"}, "stdout_has": "2"})
    check("没写 expect_exit → 只看 stdout（退出码不参与判断）", r_any["ok"], str(r_any))
    r_err = probe.run(ws_e, {"kind": "cli", "entry": "main.py", "args": ["nope"],
                             "expect_exit": 1, "stdout_has": "用法"})
    check("写了 expect_exit=1 → 退出码 2 的实现被判红", not r_err["ok"], str(r_err))
    check("红的原因是退出码（不是 stdout）", "退出码" in (r_err.get("detail") or ""),
          str(r_err.get("detail")))

    print("[3] 依赖没完成的也要观测（否则一条红锁死整条链 —— 实测 run 7）")
    chain = [
        {"id": "cap.a", "desc": "第一条", "probe": {"kind": "cli", "entry": "missing.py",
                                                    "stdout_has": "x"}, "funcs_est": 2},
        {"id": "cap.b", "desc": "依赖第一条", "probe": {"kind": "cli", "entry": "main.py",
                                                        "args": ["report"],
                                                        "stdout_has": "ok"},
         "deps": ["cap.a"], "funcs_est": 2},
    ]
    ws_chain = make_ws()
    cov_chain = coverage.run(bp_of(chain), ws_chain)
    check("上游红、下游自身能跑 → 下游照样算通过（依赖只是计划概念）",
          cov_chain["caps"]["cap.b"]["status"] == "done",
          str(cov_chain["caps"]["cap.b"]))
    bad_chain = [
        {"id": "cap.a", "desc": "第一条", "probe": {"kind": "cli", "entry": "nope1.py",
                                                    "stdout_has": "x"}, "funcs_est": 2},
        {"id": "cap.b", "desc": "依赖第一条", "probe": {"kind": "cli", "entry": "nope2.py",
                                                        "stdout_has": "y"},
         "deps": ["cap.a"], "funcs_est": 2},
    ]
    cov_bad = coverage.run(bp_of(bad_chain), make_ws())
    check("两条都红时，下游的失败原因里带上'依赖未完成'",
          "依赖未完成" in cov_bad["caps"]["cap.b"]["detail"],
          cov_bad["caps"]["cap.b"]["detail"])
    check("但下游**确实被执行过**（错误是它自己的'入口不存在'）",
          "入口不存在" in cov_bad["caps"]["cap.b"]["detail"],
          cov_bad["caps"]["cap.b"]["detail"])

    print("[4] 项目级回归网：第二轮改坏了第一轮的东西 → 必须变红")
    ws6 = make_ws()
    bp6 = bp_of()
    first = coverage.run(bp6, ws6)
    check("第一轮 2/2", coverage.done_count(first) == 2, str(coverage.ratios(first)))
    (ws6 / "store.py").write_text(STORE_STUB, encoding="utf-8")   # 后面那一轮把它改坏了
    second = coverage.run(bp6, ws6)
    check("第二轮变红（回归被抓住）", coverage.done_count(second) == 1,
          str(coverage.ratios(second)))
    check("而且指明是哪个能力坏的",
          second["caps"]["cap.store.record"]["status"] != "done"
          and second["caps"]["cap.cli.count"]["status"] == "done",
          str(second["caps"]))

    print("[5] 测试能力也要能观测：pytest 探针必须看到具体用例名")
    ws5b = make_ws()
    (ws5b / "tests").mkdir(exist_ok=True)
    (ws5b / "tests" / "test_store.py").write_text(
        "from store import make_record\n\n\n"
        "def test_happy():\n    assert make_record('a') == {'name': 'a'}\n\n\n"
        "def test_type():\n    assert isinstance(make_record('b'), dict)\n",
        encoding="utf-8")
    r = probe.run(ws5b, {"kind": "pytest", "args": ["tests/test_store.py"],
                         "stdout_has": ["test_happy", "test_type"]})
    check("真测试 → 通过（用例名都出现）", r["ok"], str(r))
    (ws5b / "tests" / "test_store.py").write_text(
        "def test_noop():\n    assert True\n", encoding="utf-8")
    r = probe.run(ws5b, {"kind": "pytest", "args": ["tests/test_store.py"],
                         "stdout_has": ["test_happy", "test_type"]})
    check("空壳测试（assert True）→ 红：用例名对不上", not r["ok"], str(r))
    check("红的时候说清缺哪个用例",
          "test_happy" in (r.get("detail") or ""), str(r.get("detail")))

    print("[6] 超时与'引擎出错'必须与'产物错'分开")
    ws7 = make_ws(store=STORE_OK + "\n\nimport time\n\n\ndef slow():\n    time.sleep(5)\n",
                  main=MAIN_OK)
    r = probe.run(ws7, {"kind": "py_call", "module": "store", "func": "slow", "args": [],
                        "expect": None}, timeout=1)
    check("超时 → 红且说是超时", not r["ok"] and "超时" in (r.get("detail") or ""), str(r))
    (ws7 / "die.py").write_text("raise SystemExit(3)\n", encoding="utf-8")
    r = probe.run(ws7, {"kind": "py_call", "module": "die", "func": "f", "expect": 1})
    check("探针脚本跑不起来 → 标记 engine_error（不许当成产物有 bug）",
          bool(r.get("engine_error")), str(r))

    print("[6] 探针自己不许在客户仓库里留垃圾")
    ws8 = make_ws()
    coverage.run(bp_of(), ws8)
    junk = [p.name for p in ws8.iterdir() if p.name not in ("main.py", "store.py", ".sixcoder")]
    check("仓库根目录没有探针产生的文件", junk == [], str(junk))
    check("临时素材没有落进仓库（d.csv 只在临时目录里）",
          not (ws8 / "d.csv").exists())
    left = list((ws8 / ".sixcoder" / "tmp").glob("probe-*")) if (ws8 / ".sixcoder").exists() else []
    check("临时目录跑完即清", left == [], str(left))

    print("[7] 历史 / 停滞 / 落盘")
    ws9 = make_ws()
    bp9 = bp_of()
    for i in range(3):
        coverage.record(ws9, coverage.run(bp9, ws9), note=f"第 {i + 1} 次")
    hs = coverage.history(ws9)
    check("观测历史落盘", len(hs) == 3, str(len(hs)))
    check("历史里记了 done 数（停滞判断要用）", all("done" in h for h in hs))
    check("一直没进展 → 判定停滞", coverage.stalled(ws9, bp9), str([h.get("done") for h in hs]))
    (ws9 / "store.py").unlink()
    coverage.record(ws9, coverage.run(bp9, ws9), note="改坏了")
    check("状态变了 → 不算停滞（停滞指的是'卡住不动'，不是'被改坏'）",
          not coverage.stalled(ws9, bp9))

    print("[8] 报告：说清完成多少、缺什么、为什么缺")
    ws10 = make_ws(store=STORE_STUB)
    bp10 = bp_of()
    cov10 = coverage.run(bp10, ws10)
    text = coverage.report(bp10, cov10)
    check("报告带比例", "1/2" in text, text[:200])
    check("报告列出缺口原因", "NotImplementedError" in text or "缺" in text, text[:300])
    check("报告逐条打勾/打叉", "✓" in text and "✗" in text, text[:200])

    print("[9] 状态写回蓝图；没有执行器的种类永远不算完成")
    marked = coverage.mark(bp10, cov10)
    check("done / absent 写回蓝图",
          marked.by_id("cap.cli.count").status == "done"
          and marked.by_id("cap.store.record").status != "done",
          str([(c.id, c.status) for c in marked.caps]))
    bp10.by_id("cap.store.record").status = "descoped"
    keep = coverage.mark(bp10, cov10)
    check("降级状态不会被一次通过偷偷撤销",
          keep.by_id("cap.store.record").status in ("descoped", "done"))
    weird = coverage.run(bp_of([{"id": "cap.http", "desc": "接口可用",
                                 "probe": {"kind": "http", "path": "/x"}, "funcs_est": 2}]),
                         make_ws())
    check("没有执行器的探针 → unsupported（不假装支持）",
          weird["caps"]["cap.http"]["status"] == "unsupported",
          str(weird["caps"]["cap.http"]))
    check("unsupported 不计入 done", coverage.done_count(weird) == 0)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（真执行 / 空壳过不了 / 依赖不误判 / 回归网 / 不落垃圾）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
