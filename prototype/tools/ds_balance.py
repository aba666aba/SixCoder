"""查 DeepSeek 官方余额（独立命令行）。

为什么单独做（2026-11-11 用户指示）：
    "剩余余额 DS 有查询指令可以直接查询官方的，不用这样计算"
    —— 余额是**账号的客观事实**，官方接口直接给；
    引擎自算的"已花费"只是**估算/账本**，会因迁移、补账、口径切换而打架
    （实测同一天出现三个数：账本 ¥38.68 / night_spent ¥33.40 / 文档 ¥31.01）。

用法：
    D:\\Python\\python.exe tools\\ds_balance.py          # 一行摘要
    D:\\Python\\python.exe tools\\ds_balance.py --json   # 完整字段
    D:\\Python\\python.exe tools\\ds_balance.py --reconcile F:\\WorkSpace\\biyeshejishuangxuan

官方接口：GET https://api.deepseek.com/user/balance
    Authorization: Bearer <key>
    → {"is_available": bool,
       "balance_infos": [{"currency","total_balance","granted_balance","topped_up_balance"}]}
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import balance  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="查 DeepSeek 官方余额")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    ap.add_argument("--reconcile", default="", help="同时与该仓库的引擎账本对账")
    args = ap.parse_args()

    bal = balance.query()
    if args.json:
        out: dict = {"balance": bal}
        if args.reconcile:
            out["reconcile"] = _reconcile(args.reconcile, bal)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if bal.get("ok") else 1

    print(balance.balance_line())
    if not bal.get("ok"):
        print("（查不到就如实说查不到；引擎不会拿估算值冒充余额）")
        return 1
    if args.reconcile:
        rec = _reconcile(args.reconcile, bal)
        for k, v in rec.items():
            print("  %s: %s" % (k, v))
    return 0


def _reconcile(repo: str, bal: dict) -> dict:
    """官方余额 vs 引擎账本：只报差额，**不改账**（账是历史事实）。"""
    out: dict = {}
    try:
        from core import budget_estimate as _be
        spent = round(_be.project_spent_cny(Path(repo)), 4)
        out["引擎自算已花"] = "¥%.4f" % spent
    except Exception as exc:                             # noqa: BLE001
        out["引擎自算已花"] = "取不到（%s）" % type(exc).__name__
        spent = None
    tot = bal.get("total")
    if tot is not None and spent is not None:
        # 充值总额未知 → 只能报"若充值 X 则已花 Y"，不编数
        out["官方余额"] = "¥%.2f" % tot
        out["提示"] = ("官方余额是权威；引擎自算的已花 %s 与它对不上时，"
                       "以官方为准（充值总额未登记，无法反推差额）" % out["引擎自算已花"])
    return out


if __name__ == "__main__":
    raise SystemExit(main())
