"""存量蓝图角色串味迁移（判据修正，零 token）。

2026-11-18：41+ 条能力用「同一用户名多角色注册 + 同名多次 login」，导致所有 token
都是末次注册的角色，teacher token 建题被 403 —— 假 bug 让夜班反复改命门文件、护栏
回滚、净增长恒 0。修复函数已在 probe.dedupe_role_users（执行层每次跑自动修补）与
blueprint.normalize_http（新蓝图源头修）。本脚本把**已生成的存量蓝图**也就地改写，
让 report / cap_contract_hint 读到的 steps 与判定层一致。

铁律：写盘前备份 → 写盘 → 复验（判据字段逐字不变、串味账号已清零）→ 失败可回滚。
幂等：已是独立账号的蓝图跑一次零改动。

跑法（用与夜班同一个解释器）：
    D:/Python/python.exe tools/migrate_role_probes.py <repo> [--dry]
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as B          # noqa: E402
from core import probe as P              # noqa: E402


def _judge_fingerprint(bp: "B.Blueprint") -> dict:
    """每条能力的所有判据字段 —— 迁移前后必须逐字相等（不许改判据）。"""
    fp = {}
    for c in bp.caps:
        steps = (c.probe or {}).get("steps") or []
        fp[c.id] = [
            {k: v for k, v in (s or {}).items() if k not in ("json", "data")}
            for s in steps
        ]
    return fp


def _role_smell_count(bp: "B.Blueprint") -> int:
    """统计仍存在串味（同一用户名 ≥2 角色）的能力数。"""
    import re
    n = 0
    for c in bp.caps:
        steps = (c.probe or {}).get("steps") or []
        by_user: dict[str, set[str]] = {}
        for s in steps:
            s = s or {}
            if "/register" not in str(s.get("path") or ""):
                continue
            body = s.get("json") if isinstance(s.get("json"), dict) else s.get("data")
            if not isinstance(body, dict):
                continue
            uk = next((k for k in body if re.search("user|name|email", str(k), re.I)), None)
            rk = next((k for k in body if "role" in str(k).lower()), None)
            if uk and rk and body.get(uk) and body.get(rk):
                by_user.setdefault(str(body[uk]), set()).add(str(body[rk]).lower())
        if any(len(roles) >= 2 for roles in by_user.values()):
            n += 1
    return n


def migrate(repo: Path, dry: bool = False) -> int:
    bp = B.load(repo)
    before_smell = _role_smell_count(bp)
    before_fp = _judge_fingerprint(bp)
    p = Path(repo) / B.BLUEPRINT_FILE
    changed_caps = 0
    for c in bp.caps:
        steps = (c.probe or {}).get("steps") or []
        if not steps:
            continue
        fixed, _notes = P.dedupe_role_users(steps)
        if fixed != steps:
            c.probe["steps"] = fixed
            changed_caps += 1
    after_smell = _role_smell_count(bp)
    after_fp = _judge_fingerprint(bp)

    print(f"repo={repo}")
    print(f"  串味能力数: {before_smell} → {after_smell}")
    print(f"  改写能力数: {changed_caps}")
    print(f"  判据字段逐字不变: {before_fp == after_fp}")
    if before_fp != after_fp:
        print("  [ABORT] 判据字段被改动 —— 拒绝写盘")
        return 2
    if after_smell != 0:
        print("  [ABORT] 仍有串味残留 —— 拒绝写盘")
        return 2
    if dry:
        print("  [dry] 未写盘")
        return 0
    if changed_caps == 0:
        print("  已是独立账号，无需迁移")
        return 0

    # 备份 → 写盘
    bak = p.with_name(p.name + ".bak-roleprobe-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(p, bak)
    print(f"  备份: {bak.name}")
    B.save(bp, repo)

    # 写盘后复验：从盘重读，串味必须归零、判据必须与迁移前一致
    bp2 = B.load(repo)
    reread_smell = _role_smell_count(bp2)
    reread_fp = _judge_fingerprint(bp2)
    ok = (reread_smell == 0 and reread_fp == before_fp)
    print(f"  复验（重读盘）: 串味={reread_smell} 判据一致={reread_fp == before_fp}")
    if not ok:
        shutil.copy2(bak, p)             # 回滚
        print("  [ROLLBACK] 复验失败，已恢复备份")
        return 3
    print("  [OK] 迁移完成并复验通过")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    repo = Path(sys.argv[1]).resolve()
    dry = "--dry" in sys.argv
    return migrate(repo, dry=dry)


if __name__ == "__main__":
    raise SystemExit(main())
