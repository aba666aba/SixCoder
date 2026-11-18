"""夜班稳定性基准：把"一次跑得好不好"变成"每个函数到底靠不靠得住"。

为什么要它：同一套机制实测出现过 1/8、2/8、3/8、6/8、8/8 —— 只看一次结果等于赌博。
真正该问的不是"这次几分"，而是：
  1. 哪些函数**每次都过**（可以承诺）；
  2. 哪些**时好时坏**（机制没兜住，属真问题）；
  3. 哪些**从来没对过**（模型能力上限或契约缺判据）；
  4. 失败到底归因于什么（判据缺 / 生成不可用 / 行为错 / 缺 import / 需求歧义）。

两种用法：

    # 分析已有工作区（不跑模型，秒级）
    python tools/bench_night.py analyze ../projects/night2 ../projects/night3 ...

    # 连跑 N 次（每次都清空工作区），自动汇总
    python tools/bench_night.py run --runs 3 --hours 0.2 [--model qwen2.5:7b] [--no-remote]

输出：控制台表格 + `_logs/bench/bench-<时间>.md` 与同名 `.json`。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PROTO = HERE.parent.parent
REPO = PROTO.parent
FIXTURE = PROTO / "tools" / "fixtures" / "hq_requirement.txt"
BENCH_DIR = REPO / "_logs" / "bench"

sys.path.insert(0, str(PROTO))

# 失败归因：顺序即优先级（越靠前越"机制能修"）
CAUSE_RULES: list[tuple[str, str, str]] = [
    ("缺判据", r"没有机械判据|契约里没有可解析|no-sample|跳过", "契约没给出可判定事实（该补契约，不是模型问题）"),
    ("需求歧义", r"需甲方确认|字段 .* 但表列|没说清映射", "需求没说清（该问甲方）"),
    ("生成不可用", r"生成代码无法解析|没有编辑|执行者没有产出|ParserSyntaxError|符号级编辑失败",
     "模型交不出可用补丁（能靠分解/换提示词/远端救）"),
    ("缺 import/符号", r"NameError|ImportError|ModuleNotFound|has no attribute|未声明",
     "缺导入/符号（确定性 autofix 能补）"),
    ("行为错", r"应当抛|== |应返回|不一致|断言|KeyError|TypeError|IndexError|ValueError",
     "实现行为与契约不符（模型推理问题）"),
    ("仍是桩", r"NotImplementedError|仍是桩|是空桩", "没被实现（调度/工作单问题）"),
    ("其他", r".*", "未分类"),
]


def classify(text: str) -> tuple[str, str]:
    t = str(text or "")
    for tag, pat, why in CAUSE_RULES:
        if re.search(pat, t, re.I):
            return tag, why
    return "其他", "未分类"


def workspace_state(ws: Path) -> dict:
    from core import contract as cm, iterate
    text = FIXTURE.read_text(encoding="utf-8")
    contracts = cm.parse_requirement(text)
    state = iterate.inspect(ws, contracts)
    per_func: dict[str, dict] = {}
    causes: dict[str, int] = {}
    for path, st in state.items():
        fails = st.get("fails") or []
        for f in fails:
            tag, _ = classify(f)
            causes[tag] = causes.get(tag, 0) + 1
        c = contracts.get(path)
        if not c:
            continue
        good = set(st.get("verified_names") or [])
        for f in c.funcs:
            per_func[f"{path}::{f.name}"] = {
                "ok": f.name in good,
                "fails": [x for x in fails if f.name in str(x)][:1],
            }
    return {"state": state, "per_func": per_func, "causes": causes,
            "verified": sum(s["verified"] for s in state.values()),
            "total": sum(s["funcs"] for s in state.values())}


def independent_acceptance(ws: Path) -> tuple[int, int]:
    """独立验收（hq_verify，与引擎不共享代码）。"""
    script = PROTO / "tools" / "hq_verify.py"
    if not script.exists():
        return 0, 0
    proc = subprocess.run([sys.executable, str(script), str(ws)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"})
    m = re.search(r"独立验收：(\d+)/(\d+)", proc.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def analyse(workspaces: list[Path]) -> dict:
    runs = []
    for ws in workspaces:
        if not (ws / "DELIVERY.md").exists() and not any(ws.glob("*.py")):
            print(f"  跳过（不像夜班工作区）：{ws}")
            continue
        t0 = time.time()
        info = workspace_state(ws)
        ok, total = independent_acceptance(ws)
        runs.append({"name": ws.name, "verified": info["verified"], "total": info["total"],
                     "acceptance": f"{ok}/{total}", "acceptance_ok": ok,
                     "per_func": info["per_func"], "causes": info["causes"],
                     "seconds": round(time.time() - t0, 1)})
        print(f"  分析 {ws.name}: 引擎 {info['verified']}/{info['total']}｜独立验收 {ok}/{total}")
    return {"runs": runs, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}


def render(report: dict) -> str:
    runs = report["runs"]
    if not runs:
        return "（没有可分析的工作区）"
    funcs = sorted({f for r in runs for f in r["per_func"]})
    lines = ["# 夜班稳定性基准", "",
             f"- 生成时间：{report['generated_at']}",
             f"- 样本：{len(runs)} 次（{', '.join(r['name'] for r in runs)}）", "",
             "## 一、每次运行", "",
             "| 运行 | 引擎验收 | 独立验收 | 失败归因（条数） |",
             "|---|---|---|---|"]
    for r in runs:
        causes = "、".join(f"{k}×{v}" for k, v in sorted(r["causes"].items(), key=lambda x: -x[1])) or "—"
        lines.append(f"| {r['name']} | {r['verified']}/{r['total']} | {r['acceptance']} | {causes} |")

    lines += ["", "## 二、每个函数的可靠性（跨样本）", "",
              "| 函数 | 通过次数 | 可靠性 | 最近失败原因 |", "|---|---|---|---|"]
    summary = {"always": [], "flaky": [], "never": []}
    for f in funcs:
        oks = [bool(r["per_func"].get(f, {}).get("ok")) for r in runs]
        n = sum(oks)
        rate = n / len(runs)
        if n == len(runs):
            verdict, bucket = "✅ 每次都过（可承诺）", "always"
        elif n == 0:
            verdict, bucket = "❌ 从未通过", "never"
        else:
            verdict, bucket = f"⚠️ 时好时坏（{n}/{len(runs)}）", "flaky"
        summary[bucket].append(f)
        last_fail = ""
        for r in reversed(runs):
            fl = r["per_func"].get(f, {}).get("fails") or []
            if fl:
                last_fail = str(fl[0])[:90]
                break
        lines.append(f"| `{f}` | {n}/{len(runs)} | {verdict} | {last_fail or '—'} |")

    lines += ["", "## 三、结论怎么读", ""]
    lines.append(f"- **可以承诺**（每次都过）：{len(summary['always'])} 个 —— "
                 + ("、".join(f"`{x}`" for x in summary["always"]) or "无"))
    lines.append(f"- **时好时坏**（说明机制没兜住）：{len(summary['flaky'])} 个 —— "
                 + ("、".join(f"`{x}`" for x in summary["flaky"]) or "无"))
    lines.append(f"- **从未通过**（模型能力上限或缺判据）：{len(summary['never'])} 个 —— "
                 + ("、".join(f"`{x}`" for x in summary["never"]) or "无"))
    lines += ["", "> 提醒：样本数 < 5 时不要把比例当结论；`时好时坏` 才是真正要治的东西。"]

    all_causes: dict[str, int] = {}
    for r in runs:
        for k, v in r["causes"].items():
            all_causes[k] = all_causes.get(k, 0) + v
    if all_causes:
        lines += ["", "## 四、失败归因汇总（决定下一步修什么）", "",
                  "| 归因 | 条数 | 含义 |", "|---|---|---|"]
        why = {tag: why for tag, _, why in CAUSE_RULES}
        for k, v in sorted(all_causes.items(), key=lambda x: -x[1]):
            lines.append(f"| {k} | {v} | {why.get(k, '')} |")
    return "\n".join(lines) + "\n"


def cmd_analyze(args) -> int:
    ws = [Path(p) for p in args.paths] if args.paths else sorted(
        p for p in (REPO / "projects").iterdir() if p.is_dir() and p.name.startswith("night"))
    report = analyse(ws)
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    md = BENCH_DIR / f"bench-{stamp}.md"
    md.write_text(render(report), encoding="utf-8")
    (BENCH_DIR / f"bench-{stamp}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(render(report))
    print(f"报告：{md}")
    return 0


def cmd_run(args) -> int:
    from core import company
    if not FIXTURE.exists():
        print("缺少需求夹具：", FIXTURE)
        return 1
    req = FIXTURE.read_text(encoding="utf-8")
    ws_list = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for i in range(1, args.runs + 1):
        ws = REPO / "projects" / f"bench-{stamp}-r{i}"
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
        ws.mkdir(parents=True, exist_ok=True)
        print(f"\n=== 第 {i}/{args.runs} 次：{ws.name} ===", flush=True)
        t0 = time.time()
        res = company.night(ws, req, args.model,
                            use_remote=not args.no_remote, max_rounds=args.max_rounds,
                            emit=lambda k, p: print("  ", k, str(p)[:100], flush=True)
                            if k in ("round", "strategy", "architect") else None)
        print(f"  → 引擎 {res.verified}/{res.total}｜{round(time.time() - t0)}s｜{res.reason}")
        ws_list.append(ws)
    report = analyse(ws_list)
    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    md = BENCH_DIR / f"bench-{stamp}.md"
    md.write_text(render(report), encoding="utf-8")
    (BENCH_DIR / f"bench-{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                                   encoding="utf-8")
    print()
    print(render(report))
    print(f"报告：{md}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bench_night", description="夜班稳定性基准")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("analyze", help="分析已有工作区（不跑模型）")
    a.add_argument("paths", nargs="*")
    a.set_defaults(func=cmd_analyze)
    r = sub.add_parser("run", help="连跑 N 次并汇总")
    r.add_argument("--runs", type=int, default=3)
    r.add_argument("--hours", type=float, default=0.2)
    r.add_argument("--max-rounds", type=int, default=10)
    r.add_argument("--model", default="qwen2.5:7b")
    r.add_argument("--no-remote", action="store_true")
    r.set_defaults(func=cmd_run)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
