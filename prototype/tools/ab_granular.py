"""同一需求、同一锁存契约，对比两种"写代码粒度"的成本与质量。

    python tools/ab_granular.py            # 两种都跑
    python tools/ab_granular.py --only whole|bodies

输出：调用次数 / 输入输出 token / 花费 / 用时 / 验收数 / 缺陷清单。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import company, contract as cm, contractcheck, iterate, remote  # noqa: E402

PROJECTS = ROOT.parent / "projects"
TASK = ROOT / "tools" / "fixtures" / "hq_requirement.txt"
CONTRACT = PROJECTS / "ab_contract.txt"


def usage() -> dict:
    return remote.usage_summary("")


def delta(a: dict, b: dict) -> dict:
    return {"calls": b["calls"] - a["calls"],
            "prompt": b["prompt_tokens"] - a["prompt_tokens"],
            "completion": b["completion_tokens"] - a["completion_tokens"],
            "cost": round(b["cost_cny"] - a["cost_cny"], 4)}


def judge(ws: Path) -> dict:
    contracts = cm.parse_requirement(CONTRACT.read_text(encoding="utf-8"))
    state = iterate.inspect(ws, contracts)
    hard = []
    for path, st in state.items():
        for f in (st.get("hard_failed") or []):
            hard.append(f"{path}::{f}")
        for msg in (st.get("fails") or [])[:2]:
            hard.append(str(msg)[:140])
    cc = contractcheck.check(contracts)
    return {"verified": sum(s.get("verified", 0) for s in state.values()),
            "total": sum(s.get("funcs", 0) for s in state.values()),
            "defects": hard[:6], "defect_count": len(hard),
            "contract_ok": cc["ok"]}


def run(arm: str, hours: float) -> dict:
    ws = PROJECTS / f"ab_{arm}"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    os.environ.pop("SIXCODER_GRANULAR", None)
    if arm == "bodies":
        os.environ["SIXCODER_GRANULAR"] = "bodies"
    req = TASK.read_text(encoding="utf-8")
    before = usage()
    t0 = time.time()
    events = []

    def on(kind, payload):
        if kind in ("decision", "escalate", "order_done", "order_failed", "author_start"):
            events.append((kind, json.dumps(payload, ensure_ascii=False)[:150]))

    res = company.night(ws, req, "qwen2.5-coder:7b", use_remote=True,
                        contract_file=str(CONTRACT), emit=on)
    ms = int((time.time() - t0) * 1000)
    after = usage()
    os.environ.pop("SIXCODER_GRANULAR", None)
    return {"arm": arm, "verified": res.verified, "total": res.total,
            "reason": res.reason[:80], "ms": ms, "usage": delta(before, after),
            "judge": judge(ws), "events": events[-14:], "ws": str(ws)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0.2)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    arms = [a.only] if a.only else ["whole", "bodies"]
    out = []
    for arm in arms:
        print(f"\n=== 跑 {arm} ===", flush=True)
        r = run(arm, a.hours)
        out.append(r)
        u = r["usage"]
        print(f"  验收 {r['verified']}/{r['total']}｜用时 {r['ms']}ms")
        print(f"  调用 {u['calls']}｜输入 {u['prompt']}｜输出 {u['completion']}｜¥{u['cost']}")
        print(f"  判据复核 {r['judge']['verified']}/{r['judge']['total']}｜缺陷 {r['judge']['defect_count']}")
        for kind, payload in r["events"]:
            if kind == "decision":
                print("   decision:", payload[:140])

    print("\n=== 对照 ===")
    print("| 粒度 | 调用 | 输入 | 输出 | 花费 | 用时 | 验收 | 缺陷 |")
    print("|---|---|---|---|---|---|---|---|")
    for r in out:
        u = r["usage"]
        print(f"| {r['arm']} | {u['calls']} | {u['prompt']} | {u['completion']} | "
              f"¥{u['cost']} | {r['ms']}ms | {r['verified']}/{r['total']} | "
              f"{r['judge']['defect_count']} |")
    (PROJECTS / "ab_granular_report.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("\n明细：projects/ab_granular_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
