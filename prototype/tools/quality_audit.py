"""代码写作质量体检：把"写得怎么样"拆成可核对的具体维度，不靠感觉。

维度（全部机械，能核到具体行）：
  1. 结构：单函数行数 / 圈复杂度 / 嵌套深度（按当前档位阈值判）
  2. 边界：裸 except、吞异常、可变默认参数、运行时 assert、print 当日志（core/lint）
  3. 重复：同名定义、近似重复函数名
  4. 类型：注解覆盖率、mypy 结果
  5. 测试有效性：覆盖率 / 变异杀伤率（有测试才算）
  6. 证据强度：这些函数的判据条数（示例/边界/端到端）

用法:
    python tools/quality_audit.py                     # 默认几个代表工作区
    python tools/quality_audit.py --only night-v2f
    python tools/quality_audit.py --show-files        # 附最差函数清单
"""
from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # prototype/
sys.path.insert(0, str(ROOT))

from core import contract as cm, gates, iterate, lint, scale  # noqa: E402

PROJECTS = ROOT.parent / "projects"
CONTRACT = PROJECTS / "ab_contract.txt"
DEFAULT = ["night-v2f", "night-v2h", "night-bodies1", "ab_whole", "ab_system", "night6"]


def _files(ws: Path) -> list[Path]:
    return [p for p in sorted(ws.glob("*.py")) if not p.name.startswith("test_")]


def _annot(ws: Path) -> tuple[int, int]:
    """注解覆盖率：带注解的 (参数+返回) / 总数。"""
    total = typed = 0
    for p in _files(ws):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if n.returns is not None:
                    typed += 1
                total += 1
                for a in list(n.args.args) + list(n.args.kwonlyargs):
                    if a.arg in ("self", "cls"):
                        continue
                    total += 1
                    if a.annotation is not None:
                        typed += 1
    return typed, total


def _coverage(ws: Path) -> str:
    if not list(ws.glob("test_*.py")):
        return "无测试"
    try:
        import coverage  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return "无工具"
    subprocess.run([sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q", "--tb=no"],
                   cwd=str(ws), capture_output=True, text=True, timeout=180)
    rep = subprocess.run([sys.executable, "-m", "coverage", "report", "--format=total"],
                         cwd=str(ws), capture_output=True, text=True, timeout=60)
    out = (rep.stdout or "").strip().splitlines()
    try:
        return f"{float(out[-1]):.0f}%"
    except Exception:                                    # noqa: BLE001
        return "失败"


def _mutation(ws: Path) -> str:
    tests = [p.name for p in ws.glob("test_*.py")]
    rels = [p.name for p in _files(ws)]
    if not tests or not rels:
        return "无测试"
    killed = total = 0
    for rel in rels:
        g = gates.gate_mutation(ws, {rel: ("", (ws / rel).read_text(encoding="utf-8",
                                                                   errors="replace"))}, tests)
        ref = str(g.evidence_ref or "")
        if ref == "baseline-red":
            return "测试基线红"
        k, _, t = ref.replace("killed=", "").partition("/")
        if k.isdigit() and t.isdigit():
            killed += int(k)
            total += int(t)
    if not total:
        return "无变异体"
    return f"{killed}/{total}（{killed/total:.0%}）"


def audit(ws: Path, show_files: bool = False) -> dict:
    rels = [p.name for p in _files(ws)]
    lim = scale.quality_limits("S")
    rows = gates.measure_quality(ws, rels) if rels else []
    over = [r for r in rows if r["complexity"] > lim["complexity"]
            or r["depth"] > lim["depth"] or r["lines"] > lim["lines"]]
    worst = sorted(rows, key=lambda r: (-r["complexity"], -r["lines"]))[:5]
    lifts = lint.lint_files(ws, rels)
    by_rule: dict[str, int] = {}
    for f in lifts:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    try:
        dup = gates.gate_duplicates(ws, rels)
        dup_detail = "" if dup.ok else dup.detail
    except Exception:                                    # noqa: BLE001
        dup_detail = ""
    typed, total = _annot(ws)
    out = {
        "ws": ws.name, "files": len(rels),
        "functions": len(rows),
        "avg_lines": round(sum(r["lines"] for r in rows) / len(rows), 1) if rows else 0,
        "max_complexity": max((r["complexity"] for r in rows), default=0),
        "over_threshold": len(over),
        "lint_total": len(lifts),
        "lint_rules": by_rule,
        "dup": bool(dup_detail),
        "annot": f"{typed}/{total}" + (f"（{typed/total:.0%}）" if total else ""),
        "coverage": _coverage(ws),
        "mutation": _mutation(ws),
        "worst": [(r["func"], r["complexity"], r["depth"], r["lines"]) for r in worst],
    }
    if show_files and lifts:
        out["lint_samples"] = [f"{f['file']}:{f['line']} {f['rule']}" for f in lifts[:8]]
    # 判据条数（证据强度）
    try:
        contracts = cm.parse_requirement(CONTRACT.read_text(encoding="utf-8"))
        state = iterate.inspect(ws, contracts)
        out["verified"] = sum(s.get("verified", 0) for s in state.values())
        out["total"] = sum(s.get("funcs", 0) for s in state.values())
    except Exception:                                    # noqa: BLE001
        out["verified"] = out["total"] = 0
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--show-files", action="store_true")
    a = ap.parse_args(argv)
    names = a.only or DEFAULT
    print("## 代码写作质量体检\n")
    print("| 工作区 | 验收 | 函数 | 平均行数 | 最大复杂度 | 超阈值 | lint | 注解覆盖 | 覆盖率 | 变异杀伤 | 重复定义 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    detail = []
    for n in names:
        ws = PROJECTS / n
        if not ws.exists() or not _files(ws):
            continue
        r = audit(ws, a.show_files)
        detail.append(r)
        print(f"| {r['ws']} | {r['verified']}/{r['total']} | {r['functions']} | {r['avg_lines']} | "
              f"{r['max_complexity']} | {r['over_threshold']} | {r['lint_total']} | "
              f"{r['annot']} | {r['coverage']} | {r['mutation']} | "
              f"{'有' if r['dup'] else '无'} |")
    print("\n### lint 问题分布（按规则）\n")
    for r in detail:
        if r["lint_rules"]:
            print(f"- {r['ws']}：{r['lint_rules']}")
    print("\n### 最复杂的几个函数\n")
    for r in detail:
        print(f"- {r['ws']}：" + "；".join(f"{f}(复杂度{c}/嵌套{d}/{n}行)"
                                          for f, c, d, n in r["worst"][:3]))
    print("\n阈值口径（S 级）：复杂度 ≤%d、嵌套 ≤%d、单函数 ≤%d 行"
          % (scale.quality_limits("S")["complexity"], scale.quality_limits("S")["depth"],
             scale.quality_limits("S")["lines"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
