"""给**已有的历史工作区**重打一遍分：用当前这套判据回头看旧产物，做前后对比。

为什么这么做：机制改了以后，光看"新跑的那次多少分"没有说服力 ——
旧产物是同一套需求写出来的，用新判据重算，才知道"新机制到底让哪一档变严了/变准了"。

对每个工作区算（全部机械、不调模型）：
  · 引擎验收（iterate.inspect）：verified/total
  · e2e：跑一次 test_e2e_flow.py（有就跑）
  · 结构体检：语法/重名/桩/未定义名字
  · lint：坏味道条数（带规则名）
  · 腐烂分：该工作区最高分模块
  · 依赖审计：幻觉包 / 已知漏洞（离线，用缓存）
  · 覆盖率：有测试且装了 coverage 才算
  · 治理文件：AGENTS.md 行数（有没有按分级生成）

用法:
    python tools/grade_workspaces.py                  # 扫 projects/ 全部
    python tools/grade_workspaces.py --only night-v2f ab_whole
    python tools/grade_workspaces.py --no-coverage    # 跳过覆盖率（快）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # prototype/
sys.path.insert(0, str(ROOT))

from core import arch, contract as cm, deps_audit, gates, iterate, lint, rot  # noqa: E402

PROJECTS = ROOT.parent / "projects"
CONTRACT = PROJECTS / "ab_contract.txt"
REQ = ROOT / "tools" / "fixtures" / "hq_requirement.txt"
OUT = ROOT.parent / "_logs" / "grade_workspaces.json"


def _contract_for(ws: Path) -> dict:
    """优先用锁定的 ab 契约；没有就按需求文件解析（旧工作区大多是这个来源）。"""
    try:
        if CONTRACT.exists():
            return cm.parse_requirement(CONTRACT.read_text(encoding="utf-8"))
        if REQ.exists():
            return cm.parse_requirement(REQ.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        pass
    return {}


def _e2e_ok(ws: Path) -> str:
    t = ws / "test_e2e_flow.py"
    if not t.exists():
        return "无"
    try:
        p = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=no", t.name],
                           cwd=str(ws), capture_output=True, text=True, timeout=120)
        out = (p.stdout or "") + (p.stderr or "")
        if p.returncode == 0:
            return "通"
        return "红"
    except Exception:                                    # noqa: BLE001
        return "?"


def _coverage(ws: Path) -> str:
    tests = list(ws.glob("test_*.py"))
    if not tests:
        return "无测试"
    try:
        import coverage  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return "无工具"
    try:
        subprocess.run([sys.executable, "-m", "coverage", "run", "-m", "pytest", "-q", "--tb=no"],
                       cwd=str(ws), capture_output=True, text=True, timeout=180)
        rep = subprocess.run([sys.executable, "-m", "coverage", "report", "--format=total"],
                             cwd=str(ws), capture_output=True, text=True, timeout=60)
        line = (rep.stdout or "").strip().splitlines()[-1] if (rep.stdout or "").strip() else "0"
        return f"{float(line):.0f}%"
    except Exception:                                    # noqa: BLE001
        return "失败"


def grade(ws: Path, with_coverage: bool = True) -> dict:
    contracts = _contract_for(ws)
    rels = [str(p.name) for p in sorted(ws.glob("*.py"))
            if not p.name.startswith("test_")]
    out: dict = {"ws": ws.name, "modules": len(rels)}
    try:
        state = iterate.inspect(ws, contracts) if contracts else {}
        out["verified"] = sum(s.get("verified", 0) for s in state.values())
        out["total"] = sum(s.get("funcs", 0) for s in state.values())
        out["hard_failed"] = sum(len(s.get("hard_failed") or []) for s in state.values())
        out["stubs"] = sum(len(s.get("stubs") or []) for s in state.values())
    except Exception as exc:                             # noqa: BLE001
        out.update({"verified": 0, "total": 0, "inspect_error": type(exc).__name__})
    try:
        sg = gates.gate_structure(ws, rels, contract_funcs=[
            f.name for c in contracts.values() for f in (getattr(c, "funcs", []) or [])])
        out["structure"] = "过" if sg.ok else "不过"
        out["structure_detail"] = "" if sg.ok else sg.detail[:80]
    except Exception:                                    # noqa: BLE001
        out["structure"] = "?"
    try:
        findings = lint.lint_files(ws, rels)
        out["lint"] = len(findings)
        out["lint_rules"] = sorted({f["rule"] for f in findings})[:4]
    except Exception:                                    # noqa: BLE001
        out["lint"] = -1
    try:
        scores = rot.scores(ws, rels, "S")
        worst = max(scores.values(), key=lambda x: x["score"]) if scores else {"score": 0}
        out["rot_max"] = worst["score"]
        out["rot_hot"] = [r for r, v in scores.items() if v["action"] == "rebuild"]
    except Exception:                                    # noqa: BLE001
        out["rot_max"] = -1
    try:
        da = deps_audit.audit(ws, use_network=False)      # 交付/回头看用缓存，不联网
        out["deps_missing"] = len(da["missing"])
        out["deps_vulns"] = len(da["vulns"])
        out["deps_unverified"] = len(da["unverified"])
    except Exception:                                    # noqa: BLE001
        out["deps_missing"] = -1
    out["e2e"] = _e2e_ok(ws)
    out["coverage"] = _coverage(ws) if with_coverage else "跳过"
    agents = ws / "AGENTS.md"
    out["agents_md"] = (len(agents.read_text(encoding="utf-8", errors="replace").splitlines())
                        if agents.exists() else 0)
    lock = ws / ".sixcoder" / "governance" / "lock.json"
    out["governance_lock"] = lock.exists()
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=[])
    ap.add_argument("--no-coverage", action="store_true")
    a = ap.parse_args(argv)

    cands = [PROJECTS / n for n in a.only] if a.only else \
        [d for d in sorted(PROJECTS.iterdir()) if d.is_dir()]
    rows = []
    for ws in cands:
        if not ws.exists() or not list(ws.glob("*.py")):
            continue
        r = grade(ws, with_coverage=not a.no_coverage)
        rows.append(r)
        print(f"  扫完 {ws.name}: {r.get('verified', 0)}/{r.get('total', 0)} "
              f"e2e={r.get('e2e')} lint={r.get('lint')} rot={r.get('rot_max')}")
    if not rows:
        print("没有可评分的工作区")
        return 1

    print("\n## 用当前判据给历史产物重打分\n")
    head = ("| 工作区 | 验收 | e2e | 结构 | lint | 腐烂分 | 幻觉包 | 漏洞 | 覆盖率 | AGENTS.md |")
    print(head)
    print("|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        v = r.get("verified", 0)
        t = r.get("total", 0)
        print(f"| {r['ws'][:18]} | {v}/{t} | {r.get('e2e', '?')} | {r.get('structure', '?')} | "
              f"{r.get('lint', '?')} | {r.get('rot_max', '?')} | "
              f"{r.get('deps_missing', '?')} | {r.get('deps_vulns', '?')} | "
              f"{r.get('coverage', '?')} | {r.get('agents_md', 0)} 行 |")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
