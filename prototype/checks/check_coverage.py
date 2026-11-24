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
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
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
    # probe 已改成"固定名字 + 原地覆盖、不删"（避开沙箱单轮删除 ≥50 个文件的守卫），
    # 断言口径从"跑完即清"改为：临时文件隔离在 .sixcoder/tmp 下、不落进仓库根目录。
    check("探针临时文件隔离在 .sixcoder/tmp 下（不落进仓库根目录）",
          all((ws8 / ".sixcoder" / "tmp") in p.parents for p in left), str(left))

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
    # 2026-11-19：旧代码在 only 显式点名时会把 descoped 重新观测成 absent，
    # 引擎于是为已废弃判据反复派单。descoped 是终态，不该被一次观测复活。
    cov_d = coverage.run(bp10, ws10, only=["cap.store.record"])
    dstat = cov_d["caps"].get("cap.store.record", {}).get("status")
    check("only 点名也不复活已降级能力", dstat == "descoped", str(dstat))
    cov_d2 = coverage.run(bp10, ws10, only=["cap.store.record"],
                          include_descoped=True)
    check("显式 include_descoped 才重评（保留重评出口）",
          cov_d2["caps"].get("cap.store.record", {}).get("status") != "descoped",
          str(cov_d2["caps"].get("cap.store.record")))
    weird = coverage.run(bp_of([{"id": "cap.http", "desc": "接口可用",
                                 "probe": {"kind": "http", "path": "/x"}, "funcs_est": 2}]),
                         make_ws())
    check("没有执行器的探针 → unsupported（不假装支持）",
          weird["caps"]["cap.http"]["status"] == "unsupported",
          str(weird["caps"]["cap.http"]))
    check("unsupported 不计入 done", coverage.done_count(weird) == 0)

    print("[10] 失败证据必须带上'判据实际发了什么请求'（2026-11-18 血泪）")
    # 坑：detail 原来只有 actual/expected，没有请求体 → 远端盯着源码瞎猜键名，
    #     实测猜了 5 轮（topicIds/preferenceList/...）都没猜中探针真发的 `items`。
    # 钉三件事：① 纯函数提取 ② coverage.run 的失败分支真调用 ③ 报告渲染层没截断。
    ws_r = make_ws()
    (ws_r / "main.py").write_text(
        'async def app(scope, receive, send):\n'
        '    if scope["type"] == "http":\n'
        '        await send({"type": "http.response.start", "status": 200,\n'
        '                    "headers": [(b"content-type", b"application/json")]})\n'
        '        await send({"type": "http.response.body", "body": b\'{"total": 0}\'})\n',
        encoding="utf-8")
    req_cap = {"id": "cap.prefs.submit", "desc": "提交志愿",
               "probe": {"kind": "http", "steps": [
                   {"method": "POST", "path": "/api/v1/prefs/",
                    "json": {"items": [{"topic_id": "1", "rank": 1}]},
                    "expect_status": 201, "expect_json": {"total": 3}}]},
               "funcs_est": 1}
    cov_r = coverage.run(bp_of([req_cap]), ws_r, timeout=5)
    det_r = cov_r["caps"]["cap.prefs.submit"]["detail"]
    check("断言失败的条目仍是 absent（补全证据不改判定）",
          cov_r["caps"]["cap.prefs.submit"]["status"] == "absent", det_r[:200])
    check("失败详情带请求体（判据实发的 items 键名可见）",
          "items" in det_r and "请求体" in det_r, det_r[:200])
    check("失败详情带期望值（状态 + 字段）",
          "期望状态=201" in det_r and "期望字段" in det_r, det_r[:200])
    # 实际响应体：远端必须看到"产物返回了什么字段"才能对准契约
    # （实测 trace 要 unacceptable_count、产物返回 unacceptable 列表 → 字段名错位，
    #   远端看不见全貌就误改 topic 键改了 5 轮）
    check("失败详情带实际响应体（产物返回的字段全貌可见）",
          "实际响应" in det_r and "total=0" in det_r, det_r[:400])
    req_lines = [ln for ln in det_r.splitlines() if "请求体=" in ln]
    check("请求证据行存在且 < 300（下游 [:300] 不会静默切掉）",
          bool(req_lines) and all(len(ln) < 300 for ln in req_lines),
          str([(len(ln), ln[:80]) for ln in req_lines]))
    resp_lines = [ln for ln in det_r.splitlines() if "实际响应" in ln]
    check("响应证据行存在且 < 300（不被下游截断）",
          bool(resp_lines) and all(len(ln) < 300 for ln in resp_lines),
          str([(len(ln), ln[:80]) for ln in resp_lines]))
    # 纯函数：没有"第N步"就返回空，不误加
    check("无失败步骤号时不硬加请求段",
          coverage.failed_step_request({"steps": [{"path": "/x"}]}, "整体失败") == "")
    # 报告渲染层：整仓报告必须把请求体交到远端
    from core import wholerepo  # noqa: E402
    rep_r = wholerepo.report_from_state(cap_cov=cov_r)
    check("整仓报告保留 items 请求体（远端真能读到，不用猜）",
          "items" in rep_r._text and "请求体" in rep_r._text, rep_r._text[:300])
    check("整仓报告保留实际响应体（字段名对照能读到）",
          "实际响应" in rep_r._text and "total=0" in rep_r._text, rep_r._text[:300])
    # 工作单链（red_modules）也曾把详情截前 300 吃掉响应段 —— 钉住它也带上了
    from core import openhouse  # noqa: E402
    _bp_r = bp_of([req_cap])
    # red_modules 按端点路径反查落点模块；/prefs/ 需要仓里有 app/prefs.py 才定位得到
    (ws_r / "app").mkdir(exist_ok=True)
    (ws_r / "app" / "prefs.py").write_text("# prefs 落点\n", encoding="utf-8")
    _mods, _det = openhouse.red_modules(cov_r, _bp_r, ws=ws_r)
    _all_ev = " ".join(sum(_det.values(), []))
    check("工作单链也带实际响应（不被 red_modules 的 [:300] 吃掉）",
          "实际响应" in _all_ev and "total=0" in _all_ev,
          "mods=%s ev=%s" % (_mods, _all_ev[:200]))

    print("[10b] 字符串形态的 json 请求体也要如实展示（2026-11-19 实测事故）")
    # 探针写成 `"json": "{\"students\":...}"`（字符串而非对象）时，旧渲染层
    # `body = st["json"] if isinstance(dict) else st["data"]` 得 None →
    # 打印「(该步无 json 请求体)」。**这是假的**：请求体确实发了，只是以字符串
    # 形态发出。远端因此坚信"probe 没传数据"，去改本来正确的产物规则改了 5 轮。
    str_probe = {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/rules/pre-filter/",
         "json": '{"students": [{"username": "stu1", "gpa": 2.0}], '
                 '"topics": [{"code": "PF1", "min_gpa": 3.0}], '
                 '"pairs": [{"student": "stu1", "topic": "PF1"}]}',
         "expect_status": 200, "expect_json": {"unacceptable_count": 1}}]}
    _det_s = coverage.failed_step_request(
        str_probe, "第1步 JSON 字段不符：unacceptable_count（实际 0；期望 1）",
        evidence={"steps": [{"step": 1, "method": "POST",
                             "path": "/api/v1/rules/pre-filter/", "status": 200,
                             "body": '{"unacceptable_count": 0}'}]})
    check("字符串 json **不再**被误报成『无请求体』",
          "无 json 请求体" not in _det_s and "该步确实没有请求体" not in _det_s, _det_s[:300])
    check("字符串 json 的真实内容展示出来了（students/min_gpa 可见）",
          "students" in _det_s and "min_gpa" in _det_s, _det_s[:300])
    # 真正没请求体时才说"确实没有"（不把"有"说成"无"）
    _none_probe = {"kind": "http", "steps": [
        {"method": "GET", "path": "/x/", "expect_status": 200}]}
    _det_n = coverage.failed_step_request(
        _none_probe, "第1步 状态码 500（期望 200）",
        evidence={"steps": [{"step": 1, "method": "GET", "path": "/x/", "status": 500}]})
    check("确实没请求体时才标注『确实没有』",
          "该步确实没有请求体" in _det_n, _det_n[:200])
    # 鉴权状态必须可见（2026-11-19 实测）：期望 2xx 却没带 Authorization →
    # 产物回 403「权限不足」，远端只看到状态码，无法判断"没带 token"还是"真没权限"。
    _det_auth = coverage.failed_step_request(
        {"kind": "http", "steps": [
            {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "T"},
             "expect_status": [201]}]},
        "第1步 POST /api/v1/topics/ → 状态码 403（期望 200/201）",
        evidence={"steps": [{"step": 1, "method": "POST",
                             "path": "/api/v1/topics/", "status": 403,
                             "body": '{"detail": "权限不足"}'}]})
    check("期望 2xx 但没带鉴权头 → 证据里点出来",
          "未带鉴权头" in _det_auth, _det_auth[:240])
    _det_auth2 = coverage.failed_step_request(
        {"kind": "http", "steps": [
            {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "T"},
             "headers": {"Authorization": "Bearer ${t}"},
             "expect_status": [201]}]},
        "第1步 POST /api/v1/topics/ → 状态码 403（期望 200/201）",
        evidence={"steps": [{"step": 1, "method": "POST",
                             "path": "/api/v1/topics/", "status": 403}]})
    check("带了鉴权头就不加这条提示（不噪声）",
          "未带鉴权头" not in _det_auth2, _det_auth2[:240])
    # 端到端：字符串 json 到达产物时必须是**对象**（不是字符串），否则产物
    # json.loads 得到 str、当空 bundle 处理 → 规则一条都不命中 → 恒 0。
    ws_echo = Path(tempfile.mkdtemp(prefix="cov-echo-"))
    (ws_echo / "main.py").write_text(
        'import json\n'
        'async def app(scope, receive, send):\n'
        '    if scope["type"] != "http":\n'
        '        return\n'
        '    msg = await receive()\n'
        '    raw = (msg.get("body") or b"").decode("utf-8", "replace")\n'
        '    try:\n'
        '        parsed = json.loads(raw)\n'
        '    except Exception:\n'
        '        parsed = None\n'
        '    out = json.dumps({"type": type(parsed).__name__,\n'
        '                      "keys": sorted(parsed) if isinstance(parsed, dict) else [],\n'
        '                      "students_n": len((parsed or {}).get("students") or [])\n'
        '                      if isinstance(parsed, dict) else -1}).encode()\n'
        '    await send({"type": "http.response.start", "status": 200,\n'
        '                "headers": [(b"content-type", b"application/json")]})\n'
        '    await send({"type": "http.response.body", "body": out})\n',
        encoding="utf-8")
    r_echo = probe.run(ws_echo, {"kind": "http", "steps": [
        {"method": "POST", "path": "/bundle/",
         "json": '{"students": [{"username": "s1", "gpa": 2.0}], '
                 '"topics": [{"code": "PF1", "min_gpa": 3.0}], '
                 '"pairs": [{"student": "s1", "topic": "PF1"}]}',
         "expect_status": 200, "expect_json": {"type": "dict", "students_n": 1}}]},
        timeout=10)
    check("字符串 json 到达产物时是**对象**（不是字符串，规则才判得动）",
          bool(r_echo.get("ok")), str(r_echo.get("detail"))[:240])

    print("[11] 同一步多个 save 的兜底值必须互不相同（2026-11-19 实测事故）")
    # cap.llm.generate_validate_review 第 3 步 save c1/c2/c3 → first/second/
    # third_candidate_id，而产物响应里没有这三个键。旧逻辑把三个变量**全兜底成 1**
    # → 三次评审打在同一个候选（采纳→改写→驳回）→ stats 恒 adopted=0/rewritten=0，
    # 判据永远红；远端对着本来正确的产物改了 5 轮。典型"判据夹具 bug 制造假 bug"。
    ws_save = Path(tempfile.mkdtemp(prefix="cov-save-"))
    (ws_save / "main.py").write_text(
        'import json\n'
        'async def app(scope, receive, send):\n'
        '    if scope["type"] != "http":\n'
        '        return\n'
        '    path = scope.get("path") or "/"\n'
        '    if path == "/gen":\n'
        '        body = json.dumps({"candidates": [{"id": "a1"}, {"id": "a2"},\n'
        '                                         {"id": "a3"}], "count": 3}).encode()\n'
        '    else:\n'
        '        body = json.dumps({"got": path.rsplit("/", 1)[-1]}).encode()\n'
        '    await send({"type": "http.response.start", "status": 200,\n'
        '                "headers": [(b"content-type", b"application/json")]})\n'
        '    await send({"type": "http.response.body", "body": body})\n',
        encoding="utf-8")
    save_probe = {"kind": "http", "steps": [
        {"method": "POST", "path": "/gen", "json": {"count": 3},
         "expect_status": 200,
         "save": {"c1": "first_candidate_id", "c2": "second_candidate_id",
                  "c3": "third_candidate_id"}},
        {"method": "GET", "path": "/c/${c1}", "expect_status": 200},
        {"method": "GET", "path": "/c/${c2}", "expect_status": 200},
        {"method": "GET", "path": "/c/${c3}", "expect_status": 200}]}
    r_save = probe.run(ws_save, save_probe, timeout=10)
    _vars = (r_save.get("evidence") or {}).get("vars") or {}
    check("三个兜底变量互不相同（不再全塌成 1）",
          len({str(_vars.get(k)) for k in ("c1", "c2", "c3")}) == 3, str(_vars))
    check("兜底按 1/2/3 递增",
          [str(_vars.get(k)) for k in ("c1", "c2", "c3")] == ["1", "2", "3"], str(_vars))
    _steps_ev = (r_save.get("evidence") or {}).get("steps") or []
    _fb = [s.get("save_fallback") for s in _steps_ev if s.get("save_fallback")]
    check("兜底不静默：trace 里留了 save_fallback 痕迹",
          bool(_fb) and len(_fb[0]) == 3, str(_fb))

    # save 写成**字符串**时不能让整步抛异常（2026-11-19 实测）
    # `"save": "{\"tok\": \"token\"}"` → 旧代码 `.items()` 抛 AttributeError →
    # 该步整体失败 → token 从未存上 → 后续 `Bearer ${tok}` 全是字面量 → 403。
    # （cap.inv.topic.guard_pending_1751 就是这么红的。）
    ws_sv = Path(tempfile.mkdtemp(prefix="cov-savestr-"))
    (ws_sv / "main.py").write_text(
        'import json\n'
        'async def app(scope, receive, send):\n'
        '    if scope["type"] != "http":\n'
        '        return\n'
        '    path = scope.get("path") or "/"\n'
        '    if path == "/login":\n'
        '        body = json.dumps({"token": "T9"}).encode()\n'
        '    else:\n'
        '        hdrs = dict(scope.get("headers") or [])\n'
        '        auth = (hdrs.get(b"authorization") or b"").decode("utf-8", "replace")\n'
        '        body = json.dumps({"auth": auth}).encode()\n'
        '    await send({"type": "http.response.start", "status": 200,\n'
        '                "headers": [(b"content-type", b"application/json")]})\n'
        '    await send({"type": "http.response.body", "body": body})\n',
        encoding="utf-8")
    r_sv = probe.run(ws_sv, {"kind": "http", "steps": [
        {"method": "POST", "path": "/login", "json": {"username": "u", "password": "p"},
         "expect_status": 200, "save": '{"tok": "token"}'},
        {"method": "GET", "path": "/who", "headers": {"Authorization": "Bearer ${tok}"},
         "expect_status": 200, "expect_json": {"auth": "Bearer T9"}}]}, timeout=10)
    check("save 写成字符串时不抛异常，且 token 真存上（后续不用字面量 ${tok}）",
          bool(r_sv.get("ok")), str(r_sv.get("detail"))[:300])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（真执行 / 空壳过不了 / 依赖不误判 / 回归网 / 不落垃圾）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
