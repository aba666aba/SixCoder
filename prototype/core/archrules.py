"""架构约束（fitness functions）：把"耦合刚刚好"变成六条可执行的检查。

为什么不做"耦合度打分"：分数可以靠拆碎模块刷出来。这里只做**违规计数** ——
一条依赖要么合法，要么不合法，没有中间地带；数字（Ca/Ce/I/扇出/簇数）只用于报告。

六条（全部静态、零 token）：
  F1 无环              依赖图不许有环（有环 = 永远无法独立理解与测试）
  F2 层级方向          core 不许依赖 application/adapter
  F3 SDP 违规          声明的 stability 更低的模块不该被更稳定的模块依赖
                       （extension_point 的依赖倒置是唯一豁免，且必须显式声明）
  F4 跨子系统边        不同 subsystem 的模块之间不许有实现依赖
  F5 扇出超限          单模块依赖数 ≤ 7（知道的太多）
  F6 内聚（LCOM 近似） 模块内函数簇 > 1 = 一个文件装了两件事

规格文件 `.sixcoder/arch.json`（骨架期生成、实现期只读）。没有它时按路径**推断**，
推断结果会标 `source=inferred`，报告里必须写明"这是推断不是声明"。
"""
from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from . import arch as _arch

SPEC_FILE = ".sixcoder/arch.json"
MAX_FANOUT = 7
MIN_FUNCS_FOR_LCOM = 4
DUP_MIN_LINES = 5

LAYER_RANK = {"core": 0, "application": 1, "adapter": 2}
SKIP_DIRS = {".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "venv", ".venv", "node_modules", "tests", "test"}


def _infer_layer(rel: str) -> str:
    low = rel.lower()
    if re.search(r"(^|/)(matching_core|domain|core|algorithm|engine)/", low) or "/core/" in low:
        return "core"
    if re.search(r"(^|/)(api|web|http|routes|cli|db|repo|adapter|infra)/", low):
        return "adapter"
    return "application"


def infer_spec(ws) -> dict:
    """没有 arch.json 时按路径推断（够用，但必须在报告里标注是推断）。"""
    ws = Path(ws)
    mods: dict[str, dict] = {}
    for name, p in _arch._module_index(ws).items():
        rel = p.relative_to(ws).as_posix()
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        mods[rel] = {"layer": _infer_layer(rel),
                     "subsystem": rel.split("/")[0] if "/" in rel else "root",
                     "stability": None}
    return {"level": "S", "source": "inferred", "layers": list(LAYER_RANK),
            "modules": mods, "extension_points": []}


def load_spec(ws) -> dict:
    import json
    ws = Path(ws)
    p = ws / SPEC_FILE
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("modules"):
                d.setdefault("source", "declared")
                d.setdefault("extension_points", [])
                return d
        except Exception:                                # noqa: BLE001
            pass
    spec = infer_spec(ws)
    spec["source"] = "inferred"
    return spec


def graph(ws) -> dict[str, set[str]]:
    """模块级依赖图（模块名 = 相对路径，与 spec 对齐）。"""
    ws = Path(ws)
    idx = _arch._module_index(ws)
    rev = {name: p.relative_to(ws).as_posix() for name, p in idx.items()}
    out: dict[str, set[str]] = {}
    for name, p in idx.items():
        rel = rev[name]
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            out[rel] = set()
            continue
        deps: set[str] = set()
        for node in ast.walk(tree):
            for t in _arch._import_targets(node, name, idx):
                tgt = rev.get(t)
                if tgt and tgt != rel:
                    deps.add(tgt)
        out[rel] = deps
    return out


def _cycles(g: dict[str, set[str]]) -> list[list[str]]:
    """返回强连通分量里 >1 的组（Tarjan，迭代版）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on: set[str] = set()
    stack: list[str] = []
    out: list[list[str]] = []
    counter = [0]

    for root in list(g):
        if root in index:
            continue
        work = [(root, iter(sorted(g.get(root, ()))))]
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in g:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, iter(sorted(g.get(nxt, ())))))
                    advanced = True
                    break
                if nxt in on:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


def _module_stats(ws, rel: str) -> dict:
    """单模块：函数、模块级名字、函数间簇（LCOM 近似）。"""
    p = Path(ws) / rel
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return {"funcs": [], "clusters": 0}
    funcs = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    names = {n.name for n in funcs}
    globals_ = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    globals_.add(t.id)
    loads: dict[str, set[str]] = {}
    calls: dict[str, set[str]] = {}
    for f in funcs:
        ld, cl = set(), set()
        for n in ast.walk(f):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                ld.add(n.id)
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                cl.add(n.func.id)
        loads[f.name] = ld & globals_
        calls[f.name] = cl & names
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    fl = list(names)
    for i, a in enumerate(fl):
        for b in fl[i + 1:]:
            if b in calls.get(a, ()) or a in calls.get(b, ()):
                union(a, b)
            elif loads.get(a, set()) & loads.get(b, set()):
                union(a, b)
    return {"funcs": fl, "clusters": len({find(n) for n in names})}


def _dup_ratio(ws) -> dict:
    """重复实现率（按函数体归一化哈希，≥5 行才算）：防止"为了解耦而复制粘贴"。"""
    ws = Path(ws)
    seen: dict[str, int] = {}
    total = 0
    dup = 0
    for p in ws.rglob("*.py"):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = [ln.strip() for ln in ast.unparse(node).splitlines()
                    if ln.strip() and not ln.strip().startswith(("def ", '"""', "#"))]
            if len(body) < DUP_MIN_LINES:
                continue
            total += len(body)
            h = hashlib.sha1("\n".join(body).encode("utf-8")).hexdigest()
            seen[h] = seen.get(h, 0) + 1
            if seen[h] > 1:
                dup += len(body)
    return {"lines": total, "dup_lines": dup,
            "ratio": round(dup / total, 4) if total else 0.0}


def check(ws, spec: dict | None = None, *, level: str = "M",
          max_fanout: int = MAX_FANOUT) -> dict:
    """跑 F1–F6。返回 {ok, violations, metrics, spec_source}。"""
    ws = Path(ws)
    spec = spec or load_spec(ws)
    mods = spec.get("modules") or {}
    g = graph(ws)
    violations: list[dict] = []

    def info(rel: str) -> dict:
        return mods.get(rel) or {}

    # F1 环
    for comp in _cycles(g):
        violations.append({"rule": "F1", "modules": comp[:4],
                           "detail": f"依赖成环：{' → '.join(comp[:4])}"})

    # 反向依赖豁免：extension point 的倒置
    invert = set()
    for ep in spec.get("extension_points") or []:
        if ep.get("invert_dependency"):
            invert.add(str(ep.get("module") or ""))

    ca: dict[str, int] = {m: 0 for m in g}
    for a, deps in g.items():
        for b in deps:
            ca[b] = ca.get(b, 0) + 1

    for a, deps in sorted(g.items()):
        la = info(a).get("layer") or _infer_layer(a)
        for b in sorted(deps):
            lb = info(b).get("layer") or _infer_layer(b)
            # F2 层级方向
            if LAYER_RANK.get(la, 1) < LAYER_RANK.get(lb, 1):
                violations.append({"rule": "F2", "from": a, "to": b,
                                   "detail": f"{la} 依赖了更易变的 {lb}（方向反了）"})
            # F3 SDP（只对"声明了 stability"的模块生效，推断值不参与判定）
            sa = info(a).get("stability")
            sb = info(b).get("stability")
            if isinstance(sa, (int, float)) and isinstance(sb, (int, float)):
                if sa < sb and not (a in invert or b in invert):
                    violations.append({"rule": "F3", "from": a, "to": b,
                                       "detail": f"更稳定的模块（{sa}）依赖了更易变的（{sb}）"})
            # F4 跨子系统
            suba = info(a).get("subsystem") or (a.split("/")[0] if "/" in a else "root")
            subb = info(b).get("subsystem") or (b.split("/")[0] if "/" in b else "root")
            # 跨子系统只允许**朝更稳定的层单向**（application → core 是正常的）；
            # 同层跨子系统（业务 ↔ 业务）才是真问题：两个边界平行演进，互相依赖必然互相拖累。
            allow = {str(x) for x in (info(a).get("allowed_cross") or [])}
            # 只判**同层**跨子系统：往更稳定层（app → core）是正常的，往更易变层由 F2 管。
            if (suba != subb and b not in allow and a not in invert
                    and LAYER_RANK.get(la, 1) == LAYER_RANK.get(lb, 1)):
                violations.append({"rule": "F4", "from": a, "to": b,
                                   "detail": f"同层跨子系统：{suba} → {subb}（要跨必须显式声明 allowed_cross）"})
        # F5 扇出
        if len(deps) > max_fanout:
            violations.append({"rule": "F5", "module": a,
                               "detail": f"扇出 {len(deps)} > {max_fanout}"})

    # F6 内聚
    lcom: dict[str, int] = {}
    for rel in g:
        st = _module_stats(ws, rel)
        n = len(st["funcs"])
        if n >= MIN_FUNCS_FOR_LCOM:
            lcom[rel] = st["clusters"]
            if st["clusters"] > 1:
                violations.append({"rule": "F6", "module": rel,
                                   "detail": f"{n} 个函数分成 {st['clusters']} 簇（一个文件装了两件事）"})

    total_edges = sum(len(v) for v in g.values())
    metrics = {
        "modules": len(g), "edges": total_edges,
        "instability": {m: round(len(d) / max(1, len(d) + ca.get(m, 0)), 3)
                        for m, d in sorted(g.items())},
        "fanout": {m: len(d) for m, d in sorted(g.items()) if len(d) > 3},
        "lcom": lcom, "dup": _dup_ratio(ws),
    }
    return {"ok": not violations, "violations": violations[:30],
            "counts": {r: sum(1 for v in violations if v["rule"] == r)
                       for r in ("F1", "F2", "F3", "F4", "F5", "F6")},
            "metrics": metrics, "spec_source": spec.get("source") or "inferred",
            "level": level}


def report_lines(res: dict) -> list[str]:
    """交付报告里的架构小节（只报事实与违规，不给"总分"）。"""
    if not res:
        return []
    c = res.get("counts") or {}
    m = res.get("metrics") or {}
    src = "声明（arch.json）" if res.get("spec_source") == "declared" else "**按路径推断**（没有 arch.json）"
    lines = ["", "### 架构约束（fitness functions）", "",
             f"- 规格来源：{src}",
             f"- 模块 {m.get('modules', 0)} 个｜依赖边 {m.get('edges', 0)} 条"
             f"｜重复实现率 {m.get('dup', {}).get('ratio', 0):.1%}",
             f"- 违规：环 {c.get('F1', 0)}｜方向 {c.get('F2', 0)}｜SDP {c.get('F3', 0)}"
             f"｜跨子系统 {c.get('F4', 0)}｜扇出 {c.get('F5', 0)}｜内聚 {c.get('F6', 0)}"]
    if res.get("violations"):
        lines += ["", "| 规则 | 位置 | 说明 |", "|---|---|---|"]
        for v in res["violations"][:12]:
            where = (v.get("module") or f"{v.get('from')} → {v.get('to')}"
                     if (v.get("module") or v.get("from")) else " → ".join(v.get("modules") or []))
            lines.append(f"| {v['rule']} | `{where}` | {v.get('detail', '')[:80]} |")
    return lines
