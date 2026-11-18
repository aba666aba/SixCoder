"""跑一次真实夜班，并把**全过程证据**落到 `_logs/runs/<name>/`（不覆盖、不清理）。

为什么单独写一个跑批工具：`ab_granular.py` 会在开跑前 `rmtree` 工作区，
事后只剩最后状态 —— 而定位问题需要的是"每一步做了什么、为什么、花了多少"。

产出（全部保留）：
  _logs/runs/<name>/events.jsonl   事件流（每条一行，实时 flush；崩了也有）
  _logs/runs/<name>/run.log        stdout/stderr
  _logs/runs/<name>/summary.json   用量/验收/结构体检/judge 汇总
  _logs/runs/<name>/artifacts/     收工时的 DELIVERY.md、TRACE.md、.sixcoder/*
  projects/<name>/                 工作区本身（不删）

用法：
  python tools/run_evidence.py --name night-v2a
  python tools/run_evidence.py --name night-v2b --granular bodies
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

from core import company, contract as cm, contractcheck, gates, iterate, remote  # noqa: E402

PROJECTS = ROOT.parent / "projects"
LOGS = ROOT.parent / "_logs" / "runs"
REQ = ROOT / "tools" / "fixtures" / "hq_requirement.txt"
CONTRACT = PROJECTS / "ab_contract.txt"


def _usage_snapshot() -> dict:
    return remote.usage_summary("")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--granular", default="whole", choices=["whole", "bodies"])
    ap.add_argument("--model", default="qwen2.5-coder:7b")
    ap.add_argument("--write-cap", type=int, default=3)
    args = ap.parse_args(argv)

    ws = PROJECTS / args.name
    if ws.exists() and any(ws.iterdir()):
        print(f"拒绝覆盖：{ws} 已存在且有内容。换一个 --name。")
        return 2
    ws.mkdir(parents=True, exist_ok=True)
    out = LOGS / args.name
    out.mkdir(parents=True, exist_ok=True)
    (out / "artifacts").mkdir(exist_ok=True)

    os.environ.pop("SIXCODER_GRANULAR", None)
    if args.granular != "whole":
        os.environ["SIXCODER_GRANULAR"] = args.granular
    os.environ["SIXCODER_WRITE_CAP"] = str(args.write_cap)

    req = REQ.read_text(encoding="utf-8")
    before = _usage_snapshot()
    since = time.strftime("%Y-%m-%d %H:%M:%S")
    ev_path = out / "events.jsonl"
    n_ev = 0

    def on(kind: str, payload) -> None:
        nonlocal n_ev
        n_ev += 1
        rec = {"i": n_ev, "t": time.strftime("%H:%M:%S"), "kind": kind,
               "payload": payload if isinstance(payload, str)
               else json.loads(json.dumps(payload, ensure_ascii=False, default=str))}
        with ev_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        short = json.dumps(rec["payload"], ensure_ascii=False, default=str)
        print(f"  [{n_ev:03d}] {kind}: {short[:150]}", flush=True)

    print(f"=== 跑 {args.name}（粒度={args.granular}, 写上限={args.write_cap}）===", flush=True)
    t0 = time.time()
    res = company.night(ws, req, args.model, use_remote=True,
                        contract_file=str(CONTRACT), emit=on)
    ms = int((time.time() - t0) * 1000)
    after = _usage_snapshot()

    # ---- 收工体检（与运行时同一套判据）----
    contracts = cm.parse_requirement(CONTRACT.read_text(encoding="utf-8"))
    state = iterate.inspect(ws, contracts)
    judge = {"verified": sum(s.get("verified", 0) for s in state.values()),
             "total": sum(s.get("funcs", 0) for s in state.values()),
             "hard": [f"{p}::{f}" for p, s in state.items() for f in (s.get("hard_failed") or [])],
             "untested": [f"{p}::{f}" for p, s in state.items() for f in (s.get("untested") or [])],
             "stubs": [f"{p}::{f}" for p, s in state.items() for f in (s.get("stubs") or [])]}
    mods = sorted(p for p in contracts)
    struct = gates.gate_structure(ws, mods)
    cc = contractcheck.check(contracts)

    # ---- 用量明细（按节点）----
    rows = []
    try:
        import sqlite3
        con = sqlite3.connect(str(ROOT / "data" / "proto.db"))
        con.row_factory = sqlite3.Row
        q = ("SELECT node_id, count(*) c, sum(prompt_tokens) pin, sum(completion_tokens) pout,"
             " sum(cache_hit_tokens) hit, round(sum(cost_cny),4) cny, max(completion_tokens) mx"
             " FROM remote_usage WHERE created_at >= ? GROUP BY node_id ORDER BY cny DESC")
        rows = [dict(r) for r in con.execute(q, (since,))]
        con.close()
    except Exception as exc:                                # noqa: BLE001
        rows = [{"error": str(exc)}]

    summary = {
        "name": args.name, "ws": str(ws), "granular": args.granular,
        "write_cap": args.write_cap, "ms": ms,
        "events": n_ev,
        "result": {"verified": res.verified, "total": res.total, "reason": res.reason[:300]},
        "judge": judge, "structure": {"ok": struct.ok, "detail": struct.detail},
        "contract_ok": cc.get("ok"),
        "usage": {"before": before, "after": after},
        "usage_delta": {k: (after.get(k, 0) - before.get(k, 0)) if isinstance(after.get(k), (int, float))
                        else None for k in ("calls", "prompt_tokens", "completion_tokens",
                                            "cache_hit_tokens", "cost_cny")},
        "by_node": rows,
    }
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2,
                                                 default=str), encoding="utf-8")

    # ---- 收工快照 ----
    art = out / "artifacts"
    for name in ("DELIVERY.md", "TRACE.md"):
        if (ws / name).exists():
            shutil.copy2(ws / name, art / name)
    six = ws / ".sixcoder"
    if six.exists():
        shutil.copytree(six, art / ".sixcoder", dirs_exist_ok=True)
    code_snapshot = {}
    for p in sorted(ws.glob("*.py")):
        code_snapshot[p.name] = p.read_text(encoding="utf-8", errors="replace")
    (art / "code_snapshot.json").write_text(json.dumps(code_snapshot, ensure_ascii=False,
                                                       indent=2), encoding="utf-8")

    print("\n=== 结果 ===")
    print(f"  验收 {res.verified}/{res.total}｜用时 {ms}ms｜事件 {n_ev} 条")
    print(f"  结构体检：{'通过' if struct.ok else '不过'} — {struct.detail[:200]}")
    print(f"  调用 {summary['usage_delta']['calls']}｜输出 {summary['usage_delta']['completion_tokens']}"
          f"｜¥{summary['usage_delta']['cost_cny']}")
    print(f"  证据：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
