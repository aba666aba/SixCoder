"""模块腐烂分：腐烂到一定程度就"按最新契约局部重刷"，而不是继续打补丁。

为什么要有（这是治理层最后一条）：
    夜班为了过关会不停打补丁。补丁本身不是错，错在**在已经烂掉的模块上继续打补丁** ——
    临时方案盖住临时方案，最后没人知道哪段是正解。
    分级表原话："模块腐烂到一定程度时走『按最新导轨局部重刷』，而不是继续打补丁。"

腐烂分（0–100）只由机械信号算，不看模型自述：

| 信号 | 权重 | 为什么它代表腐烂 |
|---|---|---|
| 临时方案标记（TODO/HACK/临时…） | 每个 12 分（最多 4 个计分） | 有意的将就，会复利 |
| 超阈值函数（复杂度/嵌套/行数） | 每个 10 分（最多 4 个） | 结构已难维护 |
| 同名重复定义 | 每个 20 分 | 说明是"复制+改两行"式补丁 |
| lint 问题（裸 except/吞异常/可变默认…） | 每个 4 分（最多 5 个） | 坏味道在扩散 |
| 依赖异常（被环依赖 / 依赖数超阈值） | 25 分 | 一改就牵连一片 |

阈值：
  · ≥ 60 → **rebuild**：这一轮直接整模块按当前契约重写（不许再走行级/函数级补丁）
  · 30–59 → **patch_careful**：可以补，但报告里必须标出腐烂分
  · < 30  → **patch**：正常打法
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from . import scale as _scale

MARKERS = re.compile(r"(TODO|FIXME|XXX|HACK|临时方案|临时绕过|先这样|权宜|兜底写法)", re.I)
_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".tmp"}

WEIGHTS = {"marker": 12, "over_limit": 10, "duplicate": 20, "lint": 4, "dep_anomaly": 25}
THRESHOLD_REBUILD = 60
THRESHOLD_CAREFUL = 30


def _module_files(ws: Path, rel: str) -> list[Path]:
    ws = Path(ws)
    return [ws / rel] if (ws / rel).exists() else []


def score_module(ws: Path, rel: str, level: str = "S") -> dict:
    """算一个模块的腐烂分。返回 {score, action, reasons, ...}。"""
    from . import lint as _lint, gates as _gates

    p = Path(ws) / rel
    if not p.exists():
        return {"module": rel, "score": 0, "action": "patch", "reasons": ["模块不存在"],
                "exists": False}
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"module": rel, "score": 0, "action": "patch", "reasons": ["读不到文件"],
                "exists": False}

    reasons: list[str] = []
    score = 0

    # 1) 临时方案标记
    markers = [i for i, line in enumerate(src.splitlines(), 1)
               if MARKERS.search(line) and not line.strip().startswith(("MARKERS", "r\"", "'"))]
    if markers:
        n = min(len(markers), 4)
        score += WEIGHTS["marker"] * n
        reasons.append(f"临时方案 {len(markers)} 处（L{markers[0]} 起）")

    # 2) 超阈值函数（用当前档位的阈值）
    lim = _scale.quality_limits(level)
    over: list[str] = []
    try:
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                m = _gates._func_metrics(node)
                if (m["complexity"] > lim["complexity"] or m["depth"] > lim["depth"]
                        or m["lines"] > lim["lines"]):
                    over.append(f"{node.name}({m['complexity']}/{m['depth']}/{m['lines']})")
    except SyntaxError:
        reasons.append("语法错误")
        score += 20
    if over:
        score += WEIGHTS["over_limit"] * min(len(over), 4)
        reasons.append(f"超阈值函数 {len(over)} 个（{over[0]} …）")

    # 3) 同名重复定义
    try:
        names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
        dupes = {n for n in names if names.count(n) > 1}
    except Exception:                                    # noqa: BLE001
        dupes = set()
    if dupes:
        score += WEIGHTS["duplicate"] * len(dupes)
        reasons.append(f"同名重复定义 {len(dupes)} 个（{', '.join(sorted(dupes)[:3])}）")

    # 4) lint
    findings = _lint.lint_source(src, rel)
    if findings:
        score += WEIGHTS["lint"] * min(len(findings), 5)
        rules = sorted({f["rule"] for f in findings})
        reasons.append(f"lint {len(findings)} 处（{', '.join(rules[:3])}）")

    # 5) 依赖异常：被自己仓库里几个模块 import（越多越动不得）+ 是否有环
    try:
        stem = Path(rel).stem
        importers = []
        for q in Path(ws).rglob("*.py"):
            if any(part in _SKIP_DIRS for part in q.relative_to(ws).parts) or q == p:
                continue
            t = q.read_text(encoding="utf-8", errors="replace")
            if f"import {stem}" in t or f"from {stem} import" in t:
                importers.append(str(q.relative_to(ws)))
        cycle = False
        for imp in importers[:8]:
            t = (Path(ws) / imp).read_text(encoding="utf-8", errors="replace")
            if f"import {Path(imp).stem}" in src or f"from {Path(imp).stem} import" in src:
                cycle = True
                break
        if cycle:
            score += WEIGHTS["dep_anomaly"]
            reasons.append("存在双向依赖（环）")
        elif len(importers) >= 3:
            score += WEIGHTS["dep_anomaly"] // 2
            reasons.append(f"被 {len(importers)} 个模块依赖（改动影响面大）")
    except Exception:                                    # noqa: BLE001
        pass

    score = min(100, score)
    action = ("rebuild" if score >= THRESHOLD_REBUILD
              else "patch_careful" if score >= THRESHOLD_CAREFUL else "patch")
    return {"module": rel, "score": score, "action": action, "reasons": reasons,
            "exists": True, "limits": lim,
            "markers": len(markers), "over_limit": len(over),
            "duplicates": sorted(dupes), "lint": len(findings)}


def scores(ws: Path, paths: list[str], level: str = "S") -> dict[str, dict]:
    return {rel: score_module(ws, rel, level) for rel in paths}


def should_rebuild(ws: Path, rel: str, level: str = "S") -> tuple[bool, dict]:
    """这一轮该不该直接整模块重刷（而不是打补丁）。"""
    info = score_module(ws, rel, level)
    return info["action"] == "rebuild", info


def report_lines(scores_map: dict[str, dict]) -> list[str]:
    """交付报告里的腐烂分小节。"""
    if not scores_map:
        return []
    lines = ["", "### 模块腐烂分（≥%d 直接整模块重刷，不再打补丁）" % THRESHOLD_REBUILD, "",
             "| 模块 | 分数 | 打法 | 主要依据 |", "|---|---|---|---|"]
    for rel, info in sorted(scores_map.items(), key=lambda kv: -kv[1]["score"]):
        act = {"rebuild": "**整模块重刷**", "patch_careful": "补丁（需留意）",
               "patch": "补丁"}[info["action"]]
        lines.append(f"| `{rel}` | {info['score']} | {act} | "
                     f"{'；'.join(info['reasons'][:2]) or '无异常信号'} |")
    hot = [r for r, i in scores_map.items() if i["action"] == "rebuild"]
    if hot:
        lines += ["", f"> 建议下一夜对 **{', '.join(hot)}** 按最新契约整模块重刷："
                      f"在这些模块上继续打补丁，只会让临时方案层层叠加。"]
    return lines
