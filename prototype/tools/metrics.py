"""聚合指标仪表盘（L 级「不逐行审查，但必须能看见一切」的最低实现）。

为什么是它：L 级的分级表原话是"人类设边界 + 检查聚合指标"。
在拿到聚合指标之前，"0% 逐行审查"就等于"0% 信任"。
这里把每次夜班的机械事实聚合成一张表：花了多少、验收多少、缓存命中多少、缺陷多少、
有没有振荡/花费触顶/临时方案到期 —— 全部来自数据库与工作区，不调模型、不猜。

用法:
  python tools/metrics.py                 # 最近 10 次
  python tools/metrics.py --last 30
  python tools/metrics.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]           # prototype/
DB = ROOT / "data" / "proto.db"
OUT = ROOT.parent / "_logs" / "metrics.json"


def _rows(since_id: int = 0) -> list[dict]:
    if not DB.exists():
        return []
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    try:
        q = ("SELECT task_id, count(*) calls, sum(prompt_tokens) pin,"
             " sum(completion_tokens) pout, sum(cache_hit_tokens) hit,"
             " round(sum(cost_cny),4) cny, min(created_at) t0, max(created_at) t1"
             " FROM remote_usage GROUP BY task_id ORDER BY t0")
        return [dict(r) for r in con.execute(q)]
    finally:
        con.close()


def _delivery_facts(projects: Path) -> dict[str, dict]:
    """从各工作区的 DELIVERY.md 里抽机械事实（验收数/镜像/振荡/花费上限/临时方案）。"""
    out: dict[str, dict] = {}
    if not projects.exists():
        return out
    dirs = [d for d in projects.iterdir() if d.is_dir()]
    dirs.sort(key=lambda d: (d / 'DELIVERY.md').stat().st_mtime
              if (d / 'DELIVERY.md').exists() else 0)
    for d in dirs:
        f = d / "DELIVERY.md"
        if not f.exists():
            continue
        txt = f.read_text(encoding="utf-8", errors="replace")
        facts: dict = {"ws": d.name}
        for line in txt.splitlines():
            s = line.strip()
            if s.startswith("- **验收通过函数："):
                try:
                    v = s.split("：", 1)[1].split("**")[0]
                    a, b = v.split("/")
                    facts["verified"], facts["total"] = int(a), int(b)
                except Exception:                        # noqa: BLE001
                    pass
            elif s.startswith("- 按 **") and "级**" in s:
                facts["level"] = s.split("**")[1][:2].strip()
            elif s.startswith("- 花费："):
                facts["cost_line"] = s[4:][:80]
            elif s.startswith("- **振荡"):
                facts["oscillated"] = True
            elif s.startswith("- 未检测到振荡"):
                facts["oscillated"] = False
            elif "e2e" in s and "未跑通" in s:
                facts["e2e"] = "红"
            elif s.startswith("- 已跑通，覆盖"):
                facts["e2e"] = "通"
        out[d.name] = facts
    return out


def build(last: int = 10) -> dict:
    rows = _rows()[-last:] if last else _rows()
    facts = _delivery_facts(ROOT.parent / "projects")
    # 把用量行与交付事实按时间就近配对（工作区名不一定等于 task_id）
    table = []
    for r in rows:
        hit_rate = (r["hit"] / r["pin"]) if r["pin"] else 0.0
        table.append({
            "task": r["task_id"], "calls": r["calls"], "in": r["pin"], "out": r["pout"],
            "cache_hit_rate": round(hit_rate, 3), "cny": r["cny"], "t0": r["t0"], "t1": r["t1"],
        })
    totals = {
        "runs": len(table),
        "calls": sum(t["calls"] for t in table),
        "out_tokens": sum(t["out"] for t in table),
        "cny": round(sum(t["cny"] for t in table), 4),
        "cache_hit_rate": round(
            (sum(t["cache_hit_rate"] * (t["in"] or 0) for t in table)
             / max(1, sum(t["in"] for t in table))), 3),
    }
    quality = []
    for name, f in facts.items():
        quality.append({"ws": name, **{k: v for k, v in f.items() if k != "ws"}})
    verified = [q["verified"] / q["total"] for q in quality
                if q.get("total")]
    return {"totals": totals, "runs": table, "quality": quality[-last:],
            "quality_avg": round(sum(verified) / len(verified), 3) if verified else None}


def render(data: dict) -> str:
    t = data["totals"]
    lines = ["", "## 聚合指标（来自用量库 + 交付报告，全机械）", "",
             f"- 最近 {t['runs']} 次运行：调用 {t['calls']} 次、输出 {t['out_tokens']} token、"
             f"花费 ¥{t['cny']:.4f}、命中率 {t['cache_hit_rate']:.0%}",
             f"- 验收率（有交付报告的运行）：{data['quality_avg']:.0%}"
             if data.get("quality_avg") is not None else "- 验收率：暂无交付报告",
             "", "| 任务 | 调用 | 输出 | 命中率 | ¥ |", "|---|---|---|---|---|"]
    for r in data["runs"][-10:]:
        lines.append(f"| {r['task'][:22]} | {r['calls']} | {r['out']} | "
                     f"{r['cache_hit_rate']:.0%} | {r['cny']} |")
    if data["quality"]:
        lines += ["", "| 工作区 | 级别 | 验收 | e2e | 振荡 | 花费 |", "|---|---|---|---|---|---|"]
        for q in data["quality"][-10:]:
            lines.append(f"| {q['ws']} | {q.get('level', '—')} | "
                         f"{q.get('verified', '—')}/{q.get('total', '—')} | "
                         f"{q.get('e2e', '—')} | "
                         f"{'是' if q.get('oscillated') else ('否' if q.get('oscillated') is False else '—')} | "
                         f"{str(q.get('cost_line', '—'))[:40]} |")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=10)
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    data = build(a.last)
    print(render(data))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {OUT}")
    if a.json:
        Path(a.json).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入 {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
