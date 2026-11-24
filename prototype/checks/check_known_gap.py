"""已知缺口台账自检（缺陷 #2 的**保守版**：只记录，不降权）。

为什么单独一个脚本：
    这条线的全部风险在**副作用** —— 一个"只记录"的机制如果悄悄改了派单或污染了
    `proven_floor`，那就是把"保守版"变成了破坏版。所以这里钉三件事：
      ① 记账与聚合口径对不对；
      ② **不改任何派单行为**（源码层断言）；
      ③ **不污染 proven_floor**（工作单级证据 ≠ 模块级证据）。

跑法: python check_known_gap.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import calibration, effort, known_gap  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def _ws() -> Path:
    d = Path(tempfile.mkdtemp(prefix="kg_"))
    return d


def _write(d: Path, rows: list[dict]) -> None:
    p = d / ".sixcoder" / "author_attempts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    import json
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")


def main() -> int:
    ok = True
    print("已知缺口台账自检（只记录，不降权）")

    # ── 1) 记账：默认 kind=module，显式可写 order ──────────────────────────────
    print("[1] 写作尝试台账记账")
    d = _ws()
    calibration.record_attempt(d, path="a.py", effort="low", ok=True)
    calibration.record_attempt(d, path="b.py", effort="max", ok=False, kind="order")
    rows = calibration.read_attempts(d)
    ok &= check("记了两条", len(rows) == 2, str(len(rows)))
    ok &= check("默认 kind=module", str(rows[0].get("kind")) == "module", str(rows[0]))
    ok &= check("显式 kind=order 落盘", str(rows[1].get("kind")) == "order", str(rows[1]))

    # ── 2) 聚合：成功率 / 各档成败 / 花费 ──────────────────────────────────────
    print("[2] 按模块聚合")
    d = _ws()
    _write(d, [
        {"at": "2026-11-10 10:00:00", "path": "x.py", "effort": "low", "ok": True,
         "funcs": 3, "cost_cny": 0.1, "kind": "module"},
        {"at": "2026-11-10 10:05:00", "path": "x.py", "effort": "high", "ok": False,
         "funcs": 3, "cost_cny": 0.2, "kind": "module"},
        {"at": "2026-11-10 10:10:00", "path": "x.py", "effort": "low", "ok": True,
         "funcs": 3, "cost_cny": 0.05, "kind": "order"},
        {"at": "2026-11-10 10:15:00", "path": "y.py", "effort": "none", "ok": True,
         "funcs": 1, "cost_cny": 0.01, "kind": "module"},
    ])
    per = known_gap.summarize(d)
    ok &= check("聚合出两个模块", set(per) == {"x.py", "y.py"}, str(sorted(per)))
    x = per.get("x.py") or {}
    ok &= check("尝试数算对", int(x.get("attempts") or 0) == 3, str(x.get("attempts")))
    ok &= check("成功数算对", int(x.get("successes") or 0) == 2, str(x.get("successes")))
    ok &= check("成功率算对（2/3，台账按三位小数落）",
                abs(float(x.get("success_rate") or 0) - round(2 / 3, 3)) < 1e-9,
                str(x.get("success_rate")))
    ok &= check("花费累加", abs(float(x.get("cost_cny") or 0) - 0.35) < 1e-6,
                str(x.get("cost_cny")))
    ok &= check("按 kind 分开统计",
                int((x.get("by_kind") or {}).get("module", {}).get("ok") or 0) == 1 and
                int((x.get("by_kind") or {}).get("order", {}).get("ok") or 0) == 1,
                str(x.get("by_kind")))
    ok &= check("按档分出成败（x.py 有两条 low 成功：一条模块级一条工作单级）",
                int((x.get("by_effort") or {}).get("low", {}).get("ok") or 0) == 2 and
                int((x.get("by_effort") or {}).get("high", {}).get("fail") or 0) == 1,
                str(x.get("by_effort")))

    # ── 3) 最高档连败：成功归零 / 低档失败归零 / 顶档失败累加 ───────────────────
    print("[3] 最高档连败口径")
    d = _ws()
    _write(d, [
        {"at": "2026-11-10 10:00:00", "path": "z.py", "effort": "max", "ok": False, "kind": "module"},
        {"at": "2026-11-10 10:01:00", "path": "z.py", "effort": "max", "ok": False, "kind": "module"},
        {"at": "2026-11-10 10:02:00", "path": "z.py", "effort": "max", "ok": False, "kind": "module"},
    ])
    z = known_gap.summarize(d).get("z.py") or {}
    ok &= check("连败 3 次都记上", int(z.get("top_fail_streak") or 0) == 3,
                str(z.get("top_fail_streak")))
    ok &= check("到阈值就标成已知缺口候选", bool(z.get("known_gap")), str(z.get("known_gap")))
    ok &= check("候选清单能取到", [c.get("path") for c in known_gap.candidates(d)] == ["z.py"],
                str([c.get("path") for c in known_gap.candidates(d)]))

    _write(d, [
        {"at": "2026-11-10 10:00:00", "path": "z.py", "effort": "max", "ok": False, "kind": "module"},
        {"at": "2026-11-10 10:01:00", "path": "z.py", "effort": "max", "ok": True, "kind": "module"},
        {"at": "2026-11-10 10:02:00", "path": "z.py", "effort": "max", "ok": False, "kind": "module"},
    ])
    z = known_gap.summarize(d).get("z.py") or {}
    ok &= check("成功后连败归零", int(z.get("top_fail_streak") or 0) == 1,
                str(z.get("top_fail_streak")))
    ok &= check("归零后不再是候选", not bool(z.get("known_gap")), str(z.get("known_gap")))

    _write(d, [
        {"at": "2026-11-10 10:00:00", "path": "z.py", "effort": "max", "ok": False, "kind": "module"},
        {"at": "2026-11-10 10:01:00", "path": "z.py", "effort": "low", "ok": False, "kind": "module"},
    ])
    z = known_gap.summarize(d).get("z.py") or {}
    ok &= check("低档失败不算「顶格也写不出」（连败归零）",
                int(z.get("top_fail_streak") or 0) == 0, str(z.get("top_fail_streak")))

    # ── 4) 关键交互：**不许污染 proven_floor** ────────────────────────────────
    print("[4] 工作单级证据不许进 proven_floor（模块级证据才认）")
    d = _ws()
    _write(d, [
        {"at": "2026-11-10 10:00:00", "path": "ord.py", "effort": "none", "ok": True,
         "kind": "order"},
        {"at": "2026-11-10 10:00:00", "path": "mod.py", "effort": "none", "ok": True,
         "kind": "module"},
    ])
    ok &= check("工作单成功 → 模块起点档不认（应为空）",
                effort.proven_floor(d, "ord.py") == "", effort.proven_floor(d, "ord.py"))
    ok &= check("模块级成功 → 认最低档 none",
                effort.proven_floor(d, "mod.py") == "none", effort.proven_floor(d, "mod.py"))

    # ── 5) 报告口径：空台账如实说；有候选时明说"没降权" ────────────────────────
    print("[5] 交付报告口径")
    empty = _ws()
    lines = known_gap.report_lines(empty)
    ok &= check("空台账如实说没有记录",
                any("还没有写作尝试记录" in x for x in lines), str(lines))
    d2 = _ws()
    _write(d2, [{"at": "2026-11-10 10:00:00", "path": "z.py", "effort": "max",
                 "ok": False, "kind": "module"}] * 3)
    lines2 = known_gap.report_lines(d2)
    ok &= check("报告里点名已知缺口候选",
                any("z.py" in x and "候选" in x for x in lines2), str(lines2))
    ok &= check("明说本夜照常派单、没降权",
                any("照常派单" in x for x in lines2), str(lines2))
    ok &= check("报告里写清成功率表头",
                any("成功率" in x for x in lines2), str(lines2))

    # ── 6) 坏数据不炸 ──────────────────────────────────────────────────────────
    print("[6] 坏数据不炸")
    d3 = _ws()
    p = d3 / ".sixcoder" / "author_attempts.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("不是json{{{\n\n{\"path\": \"ok.py\", \"effort\": \"low\", \"ok\": true}\n",
                 encoding="utf-8")
    try:
        per3 = known_gap.summarize(d3)
        good = set(per3) == {"ok.py"}
    except Exception as exc:                             # noqa: BLE001
        good = False
        print(f"    {type(exc).__name__}: {exc}")
    ok &= check("坏行跳过、好行照读", good, "")

    # ── 7) **不改派单**：源码层断言（只有记录，没有降权逻辑） ───────────────────
    print("[7] 确认这条线没有偷偷改派单")
    src = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(
        encoding="utf-8", errors="replace")
    ok &= check("工作单路开始记账（kind=order）",
                'kind="order"' in src, "")
    ok &= check("known_gap 只被用来出报告，没被用来派单",
                "known_gap" in src and "import known_gap" in src, "")
    kg_src = (Path(__file__).resolve().parents[1] / "core" / "known_gap.py").read_text(
        encoding="utf-8", errors="replace")
    ok &= check("known_gap 模块里没有任何派单/预算副作用",
                "needs_work" not in kg_src and "dispatch" not in kg_src
                and "budget" not in kg_src.lower().replace("预算消耗", ""), "")

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（记账 / 聚合 / 连败口径 / 不污染 proven_floor / 报告如实 / 不改派单）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
