# -*- coding: utf-8 -*-
"""成本基线报表：¥/能力、废单率、缓存命中。

口径（2026-11-15 用户定调："同样质量下最少的钱"）：
- **¥/能力** = 该能力相关调用花费之和 ÷ 该能力"已验证"次数（这里用调用次数近似，
  真正的 verified 数在 coverage 账本里；报表把两边都打出来，不混用）。
- **废单率** = 归到「分诊判定为 local_wiring / env」的能力上的花费 ÷ 总花费。
  这类钱本该由本地零成本解决 —— 它是**纯浪费**，不是质量成本。
- **未归因** = `cap_id` 为空的花费。**单列，不摊进平均值** ——
  摊进去会让报表好看，但会让优化失去方向（2026-11-15 之前的全部历史都是这种）。

用法：
    python tools/cost_baseline.py --repo <产物仓> [--top 12]
"""
from __future__ import annotations

# 引擎不含项目身份（2026-11-19）：路径来自 --repo / SIXCODER_REPO / _logs/engine.json
import sys as _sys
_ENGINE = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import repo_root as _repo_root  # noqa: E402


import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))


def project_id_of(repo: Path) -> str:
    try:
        from core import project_ledger as pl

        return pl.project_id_of(str(repo))
    except Exception:                                    # noqa: BLE001
        return ""


def latest_triage(repo: Path) -> dict:
    """每个模块的最新分诊结论（后写覆盖先写）。"""
    out: dict[str, dict] = {}
    p = Path(repo) / ".sixcoder" / "triage.jsonl"
    if not p.is_file():
        return out
    try:
        with p.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                mod = str(row.get("module") or "")
                if mod:
                    out[mod] = row
    except OSError:
        pass
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=str(_repo_root() or ""))
    ap.add_argument("--top", type=int, default=12)
    args = ap.parse_args()
    repo = Path(args.repo)
    pid = project_id_of(repo)

    db = ENGINE / "data" / "proto.db"
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    where, params = ("WHERE project_id=?", (pid,)) if pid else ("", ())
    rows = con.execute(
        "SELECT cap_id, purpose, model, prompt_tokens, cache_hit_tokens,"
        " completion_tokens, cost_cny FROM remote_usage " + where, params).fetchall()
    con.close()

    if not rows:
        print("没有可归因的调用记录（project_id=%r）" % pid)
        return 0

    total = sum(float(r["cost_cny"] or 0) for r in rows)
    pt = sum(int(r["prompt_tokens"] or 0) for r in rows)
    hit = sum(int(r["cache_hit_tokens"] or 0) for r in rows)

    by_cap: dict[str, dict] = defaultdict(lambda: {"cny": 0.0, "n": 0, "pt": 0, "hit": 0})
    by_purpose: dict[str, dict] = defaultdict(lambda: {"cny": 0.0, "n": 0})
    unattributed = {"cny": 0.0, "n": 0}
    for r in rows:
        cny = float(r["cost_cny"] or 0)
        by_purpose[str(r["purpose"] or "-")]["cny"] += cny
        by_purpose[str(r["purpose"] or "-")]["n"] += 1
        caps = [c for c in str(r["cap_id"] or "").split(",") if c]
        if not caps:
            unattributed["cny"] += cny
            unattributed["n"] += 1
            continue
        # 一笔记到多个能力上 → 按能力数均摊（明细不重复计总额）
        share = cny / len(caps)
        for c in caps:
            by_cap[c]["cny"] += share
            by_cap[c]["n"] += 1
            by_cap[c]["pt"] += int(r["prompt_tokens"] or 0)
            by_cap[c]["hit"] += int(r["cache_hit_tokens"] or 0)

    tri = latest_triage(repo)
    # 模块 → 能力（用蓝图的机械映射，避免猜）
    mod_caps: dict[str, list] = {}
    try:
        from core import blueprint as bpmod
        from core import attribution as at

        b = bpmod.load(repo)
        at.set_blueprint(b)
        for cap in (getattr(b, "caps", None) or []):
            for m in (getattr(cap, "modules", None) or []):
                mod_caps.setdefault(m, []).append(str(cap.id))
    except Exception:                                    # noqa: BLE001
        pass

    waste_caps: dict[str, str] = {}
    for mod, row in tri.items():
        cls = str(row.get("class") or "")
        if cls in ("local_wiring", "env"):
            for c in (row.get("cap_id") or "").split(","):
                if c:
                    waste_caps[c] = cls
            for c in mod_caps.get(mod, []):
                waste_caps.setdefault(c, cls)

    waste = sum(v["cny"] for k, v in by_cap.items() if k in waste_caps)

    print("=" * 68)
    print("成本基线   项目 %s" % (pid or "（未识别）"))
    print("=" * 68)
    print("总花费            ¥%.4f   调用 %d 次" % (total, len(rows)))
    print("缓存命中率        %.1f%%   (prompt %s / 命中 %s)"
          % ((100.0 * hit / pt) if pt else 0.0, f"{pt:,}", f"{hit:,}"))
    print("未归因花费        ¥%.4f  （%.1f%%，%d 次）"
          % (unattributed["cny"], 100.0 * unattributed["cny"] / total if total else 0,
             unattributed["n"]))
    print("废单估算          ¥%.4f  （%.1f%%：分诊为 local_wiring/env 的能力）"
          % (waste, 100.0 * waste / total if total else 0))
    if by_cap:
        print("已归因能力        %d 条，平均 ¥%.4f / 条" % (len(by_cap), total / max(1, len(by_cap))))

    print()
    print("按用途（谁在花钱）")
    for k, v in sorted(by_purpose.items(), key=lambda kv: -kv[1]["cny"])[:8]:
        print("  %-22s ¥%9.4f  %4d 次" % (k[:22], v["cny"], v["n"]))

    if by_cap:
        print()
        print("最贵的能力（前 %d）" % args.top)
        for cap, v in sorted(by_cap.items(), key=lambda kv: -kv[1]["cny"])[:args.top]:
            rate = (100.0 * v["hit"] / v["pt"]) if v["pt"] else 0.0
            tag = "  ← 废单（%s）" % waste_caps[cap] if cap in waste_caps else ""
            print("  %-34s ¥%8.4f  %3d 次  命中 %4.1f%%%s"
                  % (cap[:34], v["cny"], v["n"], rate, tag))

    print()
    print("怎么读这张表")
    print("  · 未归因高 → 归因钩子没覆盖那条路径（不是「没有成本」，是「没记账」）")
    print("  · 废单高   → 本地动作没跟上：重接线 / 补 import / 起服务，都不该花钱")
    print("  · 命中率低 → 前缀不稳（契约重合成、上下文乱序），不是模型问题")
    return 0


# 未配置项目路径时：这些自检依赖真实产物，明确 SKIP（不是 FAIL）
if __name__ == "__main__" and _repo_root() is None:
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    print("SKIP：未配置项目路径（--repo / SIXCODER_REPO / _logs/engine.json），本自检需要真实产物仓")
    raise SystemExit(0)

if __name__ == "__main__":
    sys.exit(main())