"""逐次对比"同一份契约、同一夜流程"的多次运行：钱花在哪个节点、哪个模块被反复重写、每次差在哪。

用法：
  python tools/analyze_runs.py                  # 默认看最近 12 小时内的全部运行
  python tools/analyze_runs.py --since 2026-11-04 20:00
  python tools/analyze_runs.py --task night-1793798048 --nodes
  python tools/analyze_runs.py --json out.json

口径：
  - 数据源是本机用量库 `prototype/data/proto.db` 的 remote_usage（每次远端调用一行，含 node_id，
    node_id 形如 `bodies:clean.py` / `author:store.py` / `patch:report.py` / `rescue:summarize` /
    `briefing:normalize_age` / `architect`）。
  - 失败类别只按机械信号归类，不做主观判断：
      parse_fail   输出短于 40 token（几乎必然是被判了格式错、回了个空/一句话）
      big_out      单次输出超过 6000 token（一个函数不该这么大 → 整文件级输出）
      retry_loop   同一 node 连续被叫 ≥4 次
      escalate     该运行里出现了比首档更高的 effort（说明首档没解决）
  - 不读模型自述，不猜质量；质量只在有 DELIVERY/TRACE 的工作区里取机械判据结果。
"""

import argparse
import ast
import builtins
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # prototype/
DB = ROOT / "data" / "proto.db"


def projects_root() -> Path:
    import os
    env = os.environ.get("SIXCODER_PROJECTS")
    if env:
        return Path(env)
    return ROOT.parent / "projects"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    return con


def runs(con, since: str, task: str) -> list[str]:
    if task:
        return [task]
    q = "SELECT task_id, min(created_at) t0, max(created_at) t1 FROM remote_usage GROUP BY task_id"
    rows = [dict(r) for r in con.execute(q)]
    rows = [r for r in rows if r["task_id"] and r["t0"] >= since]
    rows.sort(key=lambda r: r["t0"])
    return [r["task_id"] for r in rows]


def load(con, task_id: str) -> list[dict]:
    q = ("SELECT node_id, model, effort, prompt_tokens, completion_tokens, cache_hit_tokens,"
         " reasoning_tokens, ms, created_at, offpeak, cost_cny, cached_reply"
         " FROM remote_usage WHERE task_id=? ORDER BY id")
    return [dict(r) for r in con.execute(q, (task_id,))]


def kind_of(node: str) -> str:
    return (node or "?").split(":", 1)[0] or "?"


def module_of(node: str) -> str:
    return (node or "?").split(":", 1)[1] if ":" in (node or "") else ""


def summarize(rows: list[dict]) -> dict:
    calls = len(rows)
    pin = sum(r["prompt_tokens"] or 0 for r in rows)
    pout = sum(r["completion_tokens"] or 0 for r in rows)
    hit = sum(r["cache_hit_tokens"] or 0 for r in rows)
    reason = sum(r["reasoning_tokens"] or 0 for r in rows)
    cny = sum(r["cost_cny"] or 0 for r in rows)
    t0, t1 = rows[0]["created_at"], rows[-1]["created_at"]
    by_tier: dict[str, int] = {}
    for r in rows:
        by_tier[f"{r['model']}:{r['effort']}"] = by_tier.get(f"{r['model']}:{r['effort']}", 0) + 1
    by_node: dict[str, dict] = {}
    for r in rows:
        n = by_node.setdefault(r["node_id"] or "?", {"calls": 0, "out": 0, "cny": 0.0,
                                                     "pin": 0, "max_out": 0})
        n["calls"] += 1
        n["out"] += r["completion_tokens"] or 0
        n["pin"] += r["prompt_tokens"] or 0
        n["cny"] += r["cost_cny"] or 0
        n["max_out"] = max(n["max_out"], r["completion_tokens"] or 0)
    by_kind: dict[str, dict] = {}
    for node, v in by_node.items():
        k = kind_of(node)
        b = by_kind.setdefault(k, {"calls": 0, "out": 0, "cny": 0.0, "nodes": 0})
        b["calls"] += v["calls"]
        b["out"] += v["out"]
        b["cny"] += v["cny"]
        b["nodes"] += 1
    big = [r for r in rows if (r["completion_tokens"] or 0) >= 6000]
    short = [r for r in rows if (r["completion_tokens"] or 0) < 40 and not r["cached_reply"]]
    loops = [{"node": n, **v} for n, v in by_node.items() if v["calls"] >= 4]
    loops.sort(key=lambda x: -x["calls"])
    top = sorted(by_node.items(), key=lambda kv: -kv[1]["cny"])[:6]
    return {
        "task": rows[0].get("task_id", "") if "task_id" in rows[0] else "",
        "calls": calls, "pin": pin, "pout": pout, "hit": hit, "reason": reason, "cny": cny,
        "t0": t0, "t1": t1, "by_tier": by_tier, "by_node": by_node, "by_kind": by_kind,
        "big": [(r["node_id"], r["completion_tokens"]) for r in big],
        "short": len(short), "loops": loops, "top": top,
        "max_out": max((r["completion_tokens"] or 0) for r in rows) if rows else 0,
        "cached_reply": sum(1 for r in rows if r["cached_reply"]),
    }


TS = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def workspace_of(stats: dict, proot: Path) -> Path | None:
    """按 TRACE.md 的生成时间反查工作区（运行结束时间最接近的）。"""
    if not proot.exists():
        return None
    end = stats["t1"]
    best, best_d = None, 10 ** 9
    for d in proot.iterdir():
        tr = d / "TRACE.md"
        if not tr.exists():
            continue
        txt = tr.read_text(encoding="utf-8", errors="replace")[:400]
        m = re.search(r"生成时间：([0-9: -]+)", txt)
        if not m:
            continue
        ts = m.group(1).strip()
        # 粗比：按字符串逐段差值（同为 YYYY-MM-DD HH:MM:SS，可直接字典序比较后取分钟差）
        try:
            from datetime import datetime
            a = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
            b = datetime.strptime(end, "%Y-%m-%d %H:%M:%S")
            dist = abs((a - b).total_seconds())
        except Exception:                                  # noqa: BLE001
            continue
        if dist < best_d:
            best, best_d = d, dist
    if best is not None and best_d <= 180:                 # 3 分钟内才算同一夜
        return best
    return None


def _bound_names(tree) -> set:
    """这个模块里所有被绑定的名字（近似全模块作用域），用来判"引用了但没定义"。"""
    out: set = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
            out |= {a.arg for a in n.args.args + n.args.kwonlyargs}
            if n.args.vararg:
                out.add(n.args.vararg.arg)
            if n.args.kwarg:
                out.add(n.args.kwarg.arg)
        elif isinstance(n, ast.Assign):
            out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(n.target, ast.Name):
                out.add(n.target.id)
        elif isinstance(n, ast.Import):
            out |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            out |= {a.asname or a.name for a in n.names}
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            out |= {t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)}
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None:
                    out |= {t.id for t in ast.walk(it.optional_vars)
                            if isinstance(t, ast.Name)}
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
        elif isinstance(n, (ast.comprehension,)):
            out |= {t.id for t in ast.walk(n.target) if isinstance(t, ast.Name)}
        elif isinstance(n, ast.Lambda):
            out |= {a.arg for a in n.args.args + n.args.kwonlyargs}
        elif isinstance(n, ast.NamedExpr) and isinstance(n.target, ast.Name):
            out.add(n.target.id)
    return out


def structure_check(ws: Path) -> dict:
    """交付物的机械体检：桩函数 / 重复定义 / 引用了但没定义的名字。"""
    res: dict = {"stubs": [], "dupes": [], "undef": []}
    for p in sorted(ws.glob("*.py")):
        if p.name.startswith("test_") or p.name.startswith("_"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            res["undef"].append(f"{p.name}：语法错误 {exc.lineno}")
            continue
        seen: dict[str, int] = {}
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen[n.name] = seen.get(n.name, 0) + 1
                if any(isinstance(x, ast.Raise) and "NotImplementedError" in ast.dump(x)
                       for x in ast.walk(n)):
                    res["stubs"].append(f"{p.name}::{n.name}")
        for name, cnt in seen.items():
            if cnt > 1:
                res["dupes"].append(f"{p.name}::{name}×{cnt}")
        bound = _bound_names(tree)
        bound |= set(vars(builtins))
        # 模块级定义的私有名集合，用来只报"我们自己丢的东西"
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in bound:
                res["undef"].append(f"{p.name}::{n.id}")
    res["undef"] = sorted(set(res["undef"]))
    return res


def trace_verdict(ws: Path) -> dict:
    """从 TRACE.md 取机械结论：契约函数数、已验收数、e2e 是否跑通、覆盖缺口首条。"""
    out: dict = {"ws": ws.name}
    tr = ws / "TRACE.md"
    if tr.exists():
        txt = tr.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"契约函数 (\d+) 个｜有判据 (\d+) 个｜已验收 (\d+) 个", txt)
        if m:
            out.update(contract=int(m.group(1)), judged=int(m.group(2)), verified=int(m.group(3)))
        m = re.search(r"未跑通\*\*：(.{0,160})", txt, re.S)
        if m:
            out["e2e"] = "FAIL " + " ".join(m.group(1).split())[:120]
        elif "e2e" in txt:
            out["e2e"] = "ok"
        m = re.search(r"### 2\.3[^\n]*\n+(.*?)(?=\n## |\Z)", txt, re.S)
        if m:
            body = " ".join(m.group(1).split())
            if "未跑通" not in body:
                out["e2e"] = "ok"
    return out


def fmt_row(s: dict) -> str:
    tiers = ",".join(f"{k}×{v}" for k, v in sorted(s["by_tier"].items(), key=lambda kv: -kv[1]))
    hitrate = (s["hit"] / s["pin"]) if s["pin"] else 0.0
    return (f"| {s['task'][-10:]:10s} | {s['t1'][11:]} | {s['calls']:3d} | {s['pin']:6d} |"
            f" {hitrate:5.0%} | {s['pout']:6d} | {s['reason']:5d} | {s['cny']:7.3f} |"
            f" {s['max_out']:5d} | {tiers[:34]:34s} |")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-11-04 20:00")
    ap.add_argument("--task", default="")
    ap.add_argument("--nodes", action="store_true", help="打印每次运行的节点明细")
    ap.add_argument("--json", default="")
    args = ap.parse_args(argv)

    con = connect()
    tasks = runs(con, args.since, args.task)
    if not tasks:
        print("没有可分析的运行")
        return 1
    proot = projects_root()
    stats = []
    for t in tasks:
        rows = load(con, t)
        if not rows:
            continue
        s = summarize(rows)
        s["task"] = t
        ws = workspace_of(s, proot)
        s["ws"] = str(ws) if ws else ""
        s["verdict"] = trace_verdict(ws) if ws else {}
        s["struct"] = structure_check(ws) if ws else {}
        stats.append(s)

    print(f"\n## 一、每次运行总览（{len(stats)} 次）\n")
    print("| 运行 | 结束 | 调用 | prompt | 命中率 | 输出 | 推理 | ¥ | 最大输出 | 档位分布 |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for s in stats:
        print(fmt_row(s))

    print("\n## 二、钱花在哪里（每次前 6 个节点，按 ¥）\n")
    for s in stats:
        ws = Path(s["ws"]).name if s["ws"] else "-"
        print(f"### {s['task']}  工作区={ws}  ¥{s['cny']:.3f}  {s['calls']} 次")
        v = s["verdict"]
        if v:
            print(f"  判据结果：契约 {v.get('contract','?')} 个函数｜已验收 {v.get('verified','?')}｜"
                  f"e2e={v.get('e2e','?')}")
        for node, d in s["top"]:
            share = d["cny"] / s["cny"] if s["cny"] else 0
            print(f"  - {node:24s} 次{d['calls']:3d}  出{d['out']:7d}  ¥{d['cny']:6.4f}"
                  f"({share:4.0%})  单次最大{d['max_out']}")
        if s["loops"]:
            loop_txt = ", ".join("%s×%d" % (x["node"], x["calls"]) for x in s["loops"][:5])
            print("  重试环：" + loop_txt)
        if s["big"]:
            print(f"  超大输出：{', '.join(f'{n}={t}' for n, t in s['big'][:6])}")
        st = s.get("struct") or {}
        if st:
            if st.get("stubs"):
                print(f"  ⚠ 交付里还是桩：{', '.join(st['stubs'][:6])}")
            if st.get("dupes"):
                print(f"  ⚠ 重复定义：{', '.join(st['dupes'][:6])}")
            if st.get("undef"):
                print(f"  ⚠ 引用但没定义：{', '.join(st['undef'][:6])}")
        if args.nodes:
            print("  节点全量：")
            for node, d in sorted(s["by_node"].items(), key=lambda kv: -kv[1]["cny"]):
                print(f"    {node:26s} 次{d['calls']:3d} 入{d['pin']:7d} 出{d['out']:7d} ¥{d['cny']:7.4f}")

    print("\n## 三、按路径汇总（author=整文件 / bodies=骨架+函数体 / patch=行补丁 / "
          "rescue=本地救援 / briefing=边界取证 / architect=规划）\n")
    print("| 路径 | 运行数 | 调用合计 | 输出合计 | ¥合计 | 平均每次运行 ¥ |")
    print("|---|---|---|---|---|---|")
    agg: dict[str, dict] = {}
    for s in stats:
        for k, b in s["by_kind"].items():
            a = agg.setdefault(k, {"runs": 0, "calls": 0, "out": 0, "cny": 0.0})
            a["runs"] += 1
            a["calls"] += b["calls"]
            a["out"] += b["out"]
            a["cny"] += b["cny"]
    for k, a in sorted(agg.items(), key=lambda kv: -kv[1]["cny"]):
        print(f"| {k} | {a['runs']} | {a['calls']} | {a['out']} | {a['cny']:.3f} |"
              f" {a['cny']/max(1,a['runs']):.3f} |")

    if args.json:
        Path(args.json).write_text(json.dumps(
            [{k: v for k, v in s.items() if k not in ("by_node",)} for s in stats],
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
