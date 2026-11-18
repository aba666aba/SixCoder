"""账本迁移（一次性、幂等）：`remote_usage` 补 project_id + 把当前账单补进项目账本。

2026-11-10 用户定稿口径：
  · **一个项目一本账，不算所有项目的总和**；
  · 迁移前已经花掉的钱**人工补一次**（来源 `.sixcoder/night_spent.json`）；
  · 之后每笔远端调用自动累加（`remote.complete` → `project_ledger.add`）。

跑法（引擎目录下）：
    python tools\\migrate_ledger.py --repo F:\\WorkSpace\\biyeshejishuangxuan
    python tools\\migrate_ledger.py --repo ... --dry-run     # 只看不做

幂等：重复跑不会把账改大改小（`seed` 只升不降，`ALTER` 先查 pragma）。
安全：`remote_usage` 正在被夜班写入，加列若遇锁会重试几次；失败就跳过——
      账本本身不依赖那一列（明细归因是"以后更好"，不是"停机线依赖它"）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import db                                   # noqa: E402
from core import project_ledger as PL                 # noqa: E402


def add_project_column(dry: bool = False, retries: int = 5) -> str:
    """给 remote_usage 补 `project_id` 列（幂等）。返回中文状态串。"""
    try:
        cols = [str(r["name"]) for r in db.query("PRAGMA table_info(remote_usage)")]
    except Exception as exc:                          # noqa: BLE001
        return f"读不到 remote_usage 结构（{type(exc).__name__}）"
    if "project_id" in cols:
        return "已存在 project_id 列（跳过）"
    if dry:
        return "将新增 project_id 列（dry-run 未执行）"
    for i in range(retries):
        try:
            db.execute("ALTER TABLE remote_usage ADD COLUMN project_id TEXT DEFAULT ''")
            return "已新增 project_id 列"
        except Exception as exc:                      # noqa: BLE001
            msg = f"{type(exc).__name__}: {exc}"
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                time.sleep(1.5 * (i + 1))
                continue
            return f"加列失败（{msg[:80]}）—— 账本仍可用，明细归因以后再做"
    return "加列遇锁重试后仍失败 —— 账本仍可用，明细归因以后再做"


def main() -> int:
    ap = argparse.ArgumentParser(description="账本迁移（一次性、幂等）")
    ap.add_argument("--repo", required=True, help="产物仓库路径")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    a = ap.parse_args()
    ws = Path(a.repo).resolve()
    pid = PL.project_id_of(ws)
    print(f"仓库：{ws}")
    print(f"项目指纹：{pid or '(无法计算)'}")
    if not pid:
        print("仓库路径为空，退出")
        return 2

    before = PL.total(ws)
    print(f"迁移前账本：{'（无记录）' if before is None else f'¥{before:.4f}'}")

    legacy = 0.0
    p = ws / ".sixcoder" / "night_spent.json"
    if p.is_file():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            legacy = float(d.get("total_cny") or 0.0)
            print(f"night_spent.json 记录：¥{legacy:.4f}（goal={d.get('goal')}）")
        except Exception as exc:                      # noqa: BLE001
            print(f"night_spent.json 解析失败：{type(exc).__name__}")
    else:
        print("night_spent.json 不存在（视为本项目还没花过钱）")

    col = add_project_column(dry=a.dry_run)
    print(f"明细表加列：{col}")

    if a.dry_run:
        print("dry-run：账本未改动")
        return 0

    after = PL.seed(ws, legacy, "人工补账：迁移前本项目累计（来源 night_spent.json）")
    # 对账：让账本至少等于计数器，避免少算
    after = PL.bump_to(ws, legacy)
    print(f"迁移后账本：¥{after:.4f}")

    # 自检：明细汇总 vs 账本（明细里能归因的部分 + 账本人工补账应当自洽）
    try:
        rows = db.query(
            "SELECT COALESCE(SUM(cost_cny),0) AS s, COUNT(*) AS n FROM remote_usage"
            " WHERE project_id=?", (pid,))
        s = float((rows[0] or {}).get("s") or 0)
        n = int((rows[0] or {}).get("n") or 0)
        print(f"明细中已归因到本项目的：¥{s:.4f}（{n} 条）")
        print("口径：账本 = 人工补账 + 后续自动累加；明细是证据，可随时重算校正")
    except Exception as exc:                          # noqa: BLE001
        print(f"明细汇总跳过（{type(exc).__name__}）")
    for ln in PL.report_lines(ws):
        print(ln)
    return 0


if __name__ == "__main__":
    sys.exit(main())
