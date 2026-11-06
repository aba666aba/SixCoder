"""迁移实验度量 v2：把"返工杠杆"和"降级归因"算出来（用户 2026-11-06 的三条要求）。

三个口径：
  1. **attempts 分布**：首次通过率 / 平均尝试次数 / 返工集中在哪些能力
     · 新工作区用蓝图里原生的 `attempts`；
     · 旧工作区没有该字段 → 从 `.sixcoder/coverage.jsonl` 的历史重建（下界估计）。
  2. **降级归因**：把每条未通过的能力分到五类，并**现在就跑一次探针**看真实原因：
     可自动修复（生成失败 / 细节未达标 / 判据口径）· 需规格补充 · 接口冲突 · 预算截断
  3. **判据数归一**：能力条数不可比 → 用"原子判据数"归一（探针断言条数 + 契约示例条数）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import blueprint, coverage, probe as probe_mod, replan, scale  # noqa: E402

AUTO_FIX = ("生成失败", "细节未达标", "判据口径")


def _atomic(probe: dict) -> int:
    """一条能力的探针里有多少条原子断言（用来归一化覆盖率）。"""
    if not isinstance(probe, dict):
        return 0
    n = 0
    has = probe.get("stdout_has")
    n += len(has) if isinstance(has, list) else (1 if has else 0)
    if probe.get("stdout_eq") is not None:
        n += 1
    if probe.get("stderr_has"):
        n += 1
    if "expect" in probe:
        n += 1
    if probe.get("expect_raises"):
        n += 1
    if probe.get("expect_exit") is not None:
        n += 1
    if str(probe.get("kind") or "") == "pytest":
        n += 1
    return max(1, n)


def _contract_examples(ws: Path) -> int:
    n = 0
    d = ws / ".sixcoder" / "slices"
    if not d.exists():
        return 0
    for p in d.glob("*.txt"):
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        n += sum(1 for ln in txt.splitlines() if "==" in ln and not ln.strip().startswith("#"))
    return n


def _attempts(ws: Path, bp) -> dict[str, int]:
    """原生 attempts；旧工作区（没有该字段）从覆盖历史重建一个下界。"""
    out = {c.id: int(getattr(c, "attempts", 0) or 0) for c in bp.caps}
    if any(v > 0 for v in out.values()):
        return out
    hs = coverage.history(ws, limit=0)
    if not hs:
        return out
    red: dict[str, int] = {c.id: 0 for c in bp.caps}
    for h in hs:
        for cid, st in (h.get("statuses") or {}).items():
            if cid in red and st != "done":
                red[cid] += 1
    obs = max(1, len(hs) - 1)
    for cid, n in red.items():
        out[cid] = max(out[cid], min(3, round(3.0 * n / obs)))
    return out


def _is_stub(ws: Path, cap) -> str:
    """这条能力的目标模块是不是还是空桩（`raise NotImplementedError`）。

    实测（mid4）：16 条 CLI 能力全红，但根因是 `cli_core.py` 整份留在空桩 —— 分类器
    如果只看探针输出（"stdout 里没有…" + wrapper 的 traceback）会误判成 16 个接口冲突，
    于是"可自动修复占比"被算成 0，把结论带向"要换设计"。**必须先看代码本身。**
    """
    p = cap.probe if isinstance(cap.probe, dict) else {}
    kind = str(p.get("kind") or "")
    cands: list[Path] = []
    if kind == "cli":
        cands = [ws / "cli_core.py", ws / str(p.get("entry") or "")]
    elif kind == "py_call":
        mod = str(p.get("module") or "").replace(".", "/")
        cands = [ws / f"{mod}.py"]
    for f in cands:
        try:
            if f.is_file() and "NotImplementedError" in f.read_text(encoding="utf-8",
                                                                   errors="replace"):
                return f.name
        except OSError:
            continue
    return ""


def classify_cap(ws: Path, cap, why: str, run_probe: bool = True) -> tuple[str, str]:
    """把一条未通过的能力归到五类（返回 (类别, 证据片段)）。"""
    w = str(why or "")
    if "预算不足" in w:
        return "预算截断", w[:80]
    stub = _is_stub(ws, cap)
    if stub:
        return "生成失败", f"{stub} 仍是空桩（raise NotImplementedError）"
    detail = ""
    if run_probe:
        try:
            r = probe_mod.run(ws, cap.probe)
            detail = str(r.get("detail") or "")
        except Exception as exc:                        # noqa: BLE001
            detail = f"探针跑不了：{type(exc).__name__}"
    text = detail or w
    # **接口冲突**优先判：wrapper 与 cli_run 的返回形状不一致时，stderr 会是解包/调用错误
    if any(k in text for k in ("line 32, in main", "cannot unpack", "too many values",
                               "not enough values", "TypeError: ", "AttributeError: ")):
        return "接口冲突", text[:90]
    if "导入失败" in text or "ModuleNotFoundError" in text:
        return "生成失败", text[:90]
    if "函数不存在" in text:
        return "接口冲突", text[:90]
    if "stdout 里没有" in text or "返回值不符" in text or "里没有用例" in text:
        if str((cap.probe or {}).get("kind") or "") == "pytest":
            return "需规格补充", text[:90]
        return "细节未达标", text[:90]
    if "退出码" in text:
        return "判据口径", text[:90]
    if "stderr" in text:
        return "细节未达标", text[:90]
    return "需规格补充", text[:90]


def metrics(ws, cost_cny: float = 0.0, minutes: float = 0.0, run_probe: bool = True) -> dict:
    ws = Path(ws)
    bp = blueprint.load(ws)
    lines = scale.count_lines(ws)
    prod = int(lines.get("prod_lines") or 0)
    mods = lines.get("modules") or 0
    try:
        nmods = len(mods)
    except TypeError:
        nmods = int(mods or 0)

    attempts = _attempts(ws, bp)
    done = [c for c in bp.caps if c.status == "done"]
    bad = [c for c in bp.caps if c.status != "done"]
    first_pass = [c for c in done if attempts.get(c.id, 0) <= 1]
    avg_att = round(sum(attempts.values()) / max(1, len(bp.caps)), 2)
    rework_top = sorted(((attempts.get(c.id, 0), c.id) for c in bp.caps
                         if attempts.get(c.id, 0) >= 2), reverse=True)[:8]

    j_total = sum(_atomic(c.probe) for c in bp.caps) + _contract_examples(ws)
    j_done = sum(_atomic(c.probe) for c in done) + _contract_examples(ws) * (
        len(done) / max(1, len(bp.caps)))
    classes: dict[str, list[str]] = {}
    for c in bad:
        kind, ev = classify_cap(ws, c, c.why or "", run_probe=run_probe)
        classes.setdefault(kind, []).append(f"{c.id}｜{ev[:60]}")
    auto = sum(len(v) for k, v in classes.items() if k in AUTO_FIX)
    return {
        "caps_total": len(bp.caps), "caps_done": len(done),
        "caps_descoped": len([c for c in bp.caps if c.status == "descoped"]),
        "coverage_pct": round(100.0 * len(done) / max(1, len(bp.caps)), 1),
        "coverage_by_judgement_pct": round(100.0 * j_done / max(1, j_total), 1),
        "judgements_total": j_total,
        "first_pass_rate": round(len(first_pass) / max(1, len(done)), 3),
        "avg_attempts": avg_att,
        "rework_top": rework_top,
        "descope_classes": {k: len(v) for k, v in classes.items()},
        "descope_detail": classes,
        "auto_fixable_pct": round(100.0 * auto / max(1, len(bad)), 1),
        "regressions": len([n for n in (bp.notes or []) if n.startswith("回归：")]),
        "replans": len(replan.history(ws)),
        "prod_lines": prod, "modules": nmods,
        "test_lines": int(lines.get("test_lines") or 0),
        "cny": round(float(cost_cny or 0.0), 4),
        "cny_per_100": round(100.0 * float(cost_cny or 0.0) / prod, 3) if prod else 0.0,
        "minutes": round(float(minutes or 0.0), 1),
    }


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--cost", type=float, default=0.0)
    ap.add_argument("--minutes", type=float, default=0.0)
    ap.add_argument("--no-probe", action="store_true", help="不跑探针，只用已有说明归因")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    m = metrics(a.run, a.cost, a.minutes, run_probe=not a.no_probe)
    if a.json:
        print(json.dumps(m, ensure_ascii=False))
        return 0
    print(f"实验目录：{a.run}")
    print(f"  能力 {m['caps_total']}（通过 {m['caps_done']} / 降级 {m['caps_descoped']}）")
    print(f"  覆盖率：按能力 {m['coverage_pct']}%｜**按判据 {m['coverage_by_judgement_pct']}%**"
          f"（共 {m['judgements_total']} 条原子判据）")
    print(f"  一次通过率 {m['first_pass_rate']:.0%}｜平均尝试 {m['avg_attempts']} 次"
          f"｜返工 Top：{m['rework_top'] or '无'}")
    print(f"  未通过归因：{m['descope_classes']}｜**可自动修复占比 {m['auto_fixable_pct']}%**")
    for k, v in (m["descope_detail"] or {}).items():
        print(f"    [{k}] " + "；".join(v[:3]))
    print(f"  回归 {m['regressions']} 次｜重规划 {m['replans']} 次")
    print(f"  产物 {m['prod_lines']} 行 / {m['modules']} 模块（测试 {m['test_lines']} 行）")
    print(f"  花费 ¥{m['cny']:.2f}｜每百行 ¥{m['cny_per_100']:.3f}｜耗时 {m['minutes']} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
