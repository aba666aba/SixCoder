"""钉住「全量生成 + 错误报告」流程（2026-11-14，开发文档 §13）。

这条流程替代切片增量。它成立的前提是四件事，本文件逐条钉死：

1. **错误报告能去重、编号跨轮稳定** —— 否则"上轮修了没有"无从判断
   （11-11 实测 `stuck_same_evidence` 连续 4 次报同一条，引擎以为是新问题）；
2. **检测器真的是工具判定**（AST/import/探针），不是问模型"你写对了吗"；
3. **收敛判据写死在代码里** —— 不降就换策略、P0 卡住就叫人，不许无限循环烧钱；
4. **`full` 默认只读** —— 只看报告不许改产物。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import converge, detectors, errorreport, fullgen  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name +
          (("  — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 错误报告：去重 / 编号稳定 / 状态 / 渲染")
    r = errorreport.Report(Path("."))
    e1 = r.add(errorreport.Error("P1", "contract", "app/a.py:1", "缺参数", symbol="f"))
    e2 = r.add(errorreport.Error("P1", "contract", "app/a.py:1", "缺参数（第二次）",
                                 symbol="f"))
    check("同 key 只留一条", len(r.items) == 1, str(len(r.items)))
    check("同 key 返回同一条对象", e1 is e2)
    check("去重后证据被更新", "第二次" in (e2.what or ""), e2.what)
    i1 = errorreport.Error("P1", "contract", "app/a.py:1", "x", symbol="f").make_id()
    i2 = errorreport.Error("P1", "contract", "app/a.py:1", "y", symbol="f").make_id()
    check("编号只由去重键决定（跨轮稳定）", i1 == i2, f"{i1} vs {i2}")
    i3 = errorreport.Error("P2", "contract", "app/a.py:1", "x", symbol="f").make_id()
    check("级别变了编号就变", i1 != i3)
    r.add(errorreport.Error("P0", "syntax", "app/b.py:2", "语法错", symbol="g"))
    txt = r.render()
    check("渲染按级别排序（P0 在 P1 前）", txt.find("## P0") < txt.find("## P1"), txt[:80])
    check("渲染带编号与位置", i1 in txt and "app/a.py:1" in txt)
    check("fatal 只算 P0+P1", len(r.fatal()) == 2, str(r.stats()))
    # 状态机
    r._hist_index = {errorreport.Error("P1", "crossref", "app/c.py:3", "旧错",
                                       symbol="h").key():
                     errorreport.Error("P1", "crossref", "app/c.py:3", "旧错", symbol="h")}
    stale = r.mark_missing(set(r._hist_index))
    check("上轮有、本轮没复现 → stale", bool(stale) and stale[0].status == "stale",
          str([x.status for x in stale]))

    print("[2] 检测器是工具判定（AST），且能抓真问题")
    src = ("def f(a):\n    return a\n"
           "def f(a, b):\n    return a\n")          # 顶层重名
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "app").mkdir()
        (w / "app" / "dup.py").write_text(src, encoding="utf-8")
        (w / "app" / "stub.py").write_text("def g():\n    pass\n", encoding="utf-8")
        errs = detectors.d1_syntax(w)
        kinds = " ".join(e.what for e in errs)
        check("D1 抓到顶层重名", "重名" in kinds, kinds[:120])
        check("D1 抓到空桩", "空桩" in kinds, kinds[:120])
        # D4：调用不存在的函数
        (w / "app" / "biz.py").write_text("def real():\n    return 1\n", encoding="utf-8")
        (w / "app" / "caller.py").write_text(
            "def go():\n    return 'x'\n", encoding="utf-8")
        errs4 = detectors.d4_crossref(w)
        check("D4 跑得通（空产物不误报）",
              all(e.kind == "crossref" for e in errs4), str(len(errs4)))

    print("[3] 收敛判据：六种情形都判得对")
    cases = [
        ("首轮", [], 12, 23, "continue"),
        ("在降", [{"fatal": 12}], 8, 25, "continue"),
        ("不降", [{"fatal": 8}, {"fatal": 8}], 8, 25, "switch_strategy"),
        ("P0 卡住", [{"fatal": 5, "p0": 2}, {"fatal": 5, "p0": 2}], 5, 25, "stop_human"),
        ("done 卡住", [{"fatal": 5, "done": 23}, {"fatal": 4, "done": 23}], 4, 23,
         "stop_human"),
        ("全绿", [{"fatal": 1}], 0, 84, "done"),
    ]
    for name, hist, fatal, done, want in cases:
        got = converge.judge(hist, fatal=fatal, done=done, total=84).action
        check(f"{name} → {want}", got == want, f"实际 {got}")

    print("[4] fullgen 安全边界")
    check("fullgen 有 run/detect/repair", all(
        hasattr(fullgen, x) for x in ("run", "detect", "repair")))
    check("三层分批顺序固定（models→services→api）",
          [x[0] for x in fullgen.LAYERS] == ["models", "services", "api"],
          str([x[0] for x in fullgen.LAYERS]))
    src_run = inspect.getsource(fullgen.run)
    check("检测模式是纯只读（不接线、不修）",
          "if not detect_only:" in src_run, "detect_only 还接线 → 看报告会改坏产物")
    check("run 走收敛判据（不是死循环）", "judge(" in src_run)
    check("报告落盘到 errors_report.md", "errors_report.md" in src_run)

    print("[5] CLI 入口存在且默认只读")
    import cli
    src_cli = inspect.getsource(cli)
    check("有 cmd_full", hasattr(cli, "cmd_full"))
    check("full 子命令已注册", 'add_parser("full"' in src_cli)
    check("默认 detect_only（要 --repair 才改代码）",
          "not bool(getattr(args, \"repair\"" in src_cli,
          "默认就会改代码 → 危险")

    print("[6] 契约锁定（缓存命中率的关键 + 消掉命名漂移）")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / ".sixcoder" / "slices").mkdir(parents=True)
        (w / ".sixcoder" / "slices" / "s1.txt").write_text(
            "1) app/x.py：POST /api/v1/x 的入口：\n   - f(body: dict) -> dict：做点事\n",
            encoding="utf-8")
        a = fullgen._lock_contract(w)
        check("首次从现状锁定契约", "POST /api/v1/x" in a, a[:60])
        check("锁定文件已生成", (w / ".sixcoder" / "full_contract.txt").is_file())
        # 改切片契约（模拟"又重合成了一次"）
        (w / ".sixcoder" / "slices" / "s1.txt").write_text("完全不同的内容\n",
                                                          encoding="utf-8")
        b = fullgen._lock_contract(w)
        check("第二次读锁定文件（切片变了也不影响）", b == a, "前缀必须稳定，否则缓存全丢")
        c = fullgen._lock_contract(w, refresh=True)
        check("refresh=True 才会重新取", c != a, c[:40])
    check("run 用锁定契约（不是每次读最新切片）",
          "_lock_contract(" in inspect.getsource(fullgen.run))
    check("CLI 有 --refresh-contract（并提示会丢缓存）",
          "refresh-contract" in inspect.getsource(cli))
    check("full_round 事件带 cache_hit", "cache_hit" in inspect.getsource(fullgen.run))

    print("[7] 两种场景自动判定（空项目从零写 / 已有仓库分析优化）")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        check("空目录 → scratch", fullgen.decide_mode(w) == "scratch",
              fullgen.decide_mode(w))
        (w / "app").mkdir()
        for i in range(4):
            (w / "app" / f"m{i}.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        check("4 个模块 → repair", fullgen.decide_mode(w) == "repair",
              fullgen.decide_mode(w))
        check("阈值写在代码里（SCRATCH_MIN_MODULES）",
              fullgen.SCRATCH_MIN_MODULES == 3, str(fullgen.SCRATCH_MIN_MODULES))

    class _C:
        def __init__(self, i):
            self.id = i
    class _BP:
        caps = [_C("cap.topic.a"), _C("cap.topic.b"), _C("cap.batch.c"),
                _C("cap.people.d")]
    doms = fullgen._domains(_BP())
    check("按能力 id 第二段分域", set(doms) == {"topic", "batch", "people"},
          str(list(doms)))
    check("topic 域有 2 条", len(doms.get("topic") or []) == 2, str(doms))
    check("有分域契约生成入口",
          hasattr(fullgen, "generate_contract_by_domain"))
    src = inspect.getsource(fullgen.run)
    check("run 自动判定 mode（不靠人记参数）", "decide_mode(ws)" in src)
    check("scratch 时缺契约才生成（有锁就用）", "not _has" in src)
    check("CLI 有 --mode", "--mode" in inspect.getsource(cli))
    check("返回值带 mode（日志能看出走的哪条路）", '"mode": _mode' in src)

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
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
