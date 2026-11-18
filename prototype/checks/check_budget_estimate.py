"""预算机制自检（2026-11-10 用户定稿口径：三件事 + 一条线）。

钉住：
  [1] token 基线（稳定段总量 ÷ 净增 done；没数据不编数）；
  [2] 估算（富余加在 token 上；价格用户填，不填不编金额）；
  [3] 系统预算唯一停机线（总花费 ≥ 系统预算 ×1.05 → 停；没设预算永不停）；
  [4] 单价/预算从 .sixcoder/budget.json 读（引擎不编造价格）；
  [5] 两种夜班模式都已接线（甲方轮循环 + 全自主片循环）。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import budget_estimate as be  # noqa: E402

FAILS = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK   " if cond else "  FAIL ") + name + ("  " + extra if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] token 基线：稳定段总量 ÷ 净增 done")
    check("655k tok/条（1.97M ÷ 3）",
          abs(be.tokens_per_cap(1_965_020, 3) - 655_006.67) < 1)
    check("没有净增就不编数（返回 0）", be.tokens_per_cap(999_999, 0) == 0.0)
    check("没有 token 就不编数", be.tokens_per_cap(0, 3) == 0.0)

    print("[2] 估算：富余加在 token 上，价格由用户填")
    e = be.estimate(tokens_per_cap=655_006.67, remaining=59,
                    price_offpeak=0.66, price_peak=1.56)
    check("基础 token ≈38.6M", abs(e["base_tokens"] - 38_645_393) < 100, str(e["base_tokens"]))
    check("含 20% 富余 ≈46.4M", abs(e["need_tokens"] - 46_374_472) < 100, str(e["need_tokens"]))
    check("谷时预算 ≈¥30.6", abs(e["budget_offpeak"] - 30.61) < 0.2, str(e["budget_offpeak"]))
    check("峰时预算 ≈¥72.3", abs(e["budget_peak"] - 72.34) < 0.2, str(e["budget_peak"]))
    check("谷时启动省钱", e["saving_cny"] > 40, str(e["saving_cny"]))

    print("[3] 没填价格 → 只给 token，不编金额")
    e2 = be.estimate(tokens_per_cap=655_006.67, remaining=59)
    check("谷时金额为 None", e2["budget_offpeak"] is None)
    check("峰时金额为 None", e2["budget_peak"] is None)
    check("token 数照样给", e2["need_tokens"] > 0)
    check("文案提示填价", "不编金额" in be.format_report(e2))

    print("[4] 系统预算唯一停机线：×1.05 才停，没设预算永不停")
    ws = Path(tempfile.mkdtemp(prefix="sixcoder-budget2-"))
    check("没有 budget.json → 预算 0（不停机）", be.load_system_budget(ws) == 0.0)
    check("预算 0 时 system_budget_stopped 恒 False",
          be.system_budget_stopped(ws, "t") is False)
    (ws / ".sixcoder").mkdir(exist_ok=True)
    (ws / ".sixcoder" / "budget.json").write_text(
        json.dumps({"system_budget_cny": 10.0}), encoding="utf-8")
    check("读到系统预算 10.0", be.load_system_budget(ws) == 10.0)
    check("停机阈值 = 10 × 1.05 = 10.5",
          abs(be.budget_snapshot(ws)["stop_threshold_cny"] - 10.5) < 1e-9)
    check("负数/0 预算视为未设",
          (lambda: ((ws / ".sixcoder" / "budget.json").write_text(
              json.dumps({"system_budget_cny": -3}), encoding="utf-8"),
              be.load_system_budget(ws) == 0.0)[1])())
    (ws / ".sixcoder" / "budget.json").write_text(
        json.dumps({"system_budget_cny": 10.0}), encoding="utf-8")

    # 停机判定三态（把账本读数 mock 掉，不依赖真实库）
    import core.remote as _rm
    _orig = _rm.usage_summary
    try:
        # 显式 task_id → 按**这个任务**的账本
        _rm.usage_summary = lambda task_id="": {"cost_cny": 10.49}
        check("任务花费 10.49 < 阈值 10.5 → 不停", be.system_budget_stopped(ws, "t") is False)
        _rm.usage_summary = lambda task_id="": {"cost_cny": 10.50}
        check("任务花费 10.50 ≥ 阈值 10.5 → 停", be.system_budget_stopped(ws, "t") is True)
        _rm.usage_summary = lambda task_id="": {}
        check("取不到花费 → 不停（绝不误停）", be.system_budget_stopped(ws, "t") is False)

        # 空 task_id（全自主夜班）→ 必须按**本项目**计数器，不许拿全量历史账本。
        # 2026-11-10 18:05 实机事故：项目只花 ¥31.01，历史账本 ¥110.35 ≥ ¥105
        # → 夜班第一轮就"超额 5%"收工，一轮活都没干。
        # 老的自检用 `lambda task_id="": {"cost_cny": X}`（忽略 task_id）→ 两个口径
        # 在自检眼里是同一个数，永远测不出这个 bug。这里必须**分开造**。
        (ws / ".sixcoder" / "night_spent.json").write_text(
            json.dumps({"goal": "g", "total_cny": 3.0}), encoding="utf-8")
        _rm.usage_summary = lambda task_id="": {"cost_cny": 110.35}   # 全量历史账本
        check("本项目 ¥3 / 历史全量 ¥110.35 → 不停（本项目口径）",
              be.system_budget_stopped(ws, "") is False)
        check("project_spent_cny 读的是本项目计数器",
              abs(be.project_spent_cny(ws) - 3.0) < 1e-9, str(be.project_spent_cny(ws)))
        (ws / ".sixcoder" / "night_spent.json").write_text(
            json.dumps({"goal": "g", "total_cny": 10.6}), encoding="utf-8")
        check("本项目 ¥10.6 ≥ 阈值 10.5 → 停", be.system_budget_stopped(ws, "") is True)
        (ws / ".sixcoder" / "night_spent.json").write_text(
            json.dumps({"goal": "g", "total_cny": 99999.0}), encoding="utf-8")
        check("计数器比全量还大（坏数据）→ 按全量兜底，不因坏数字提前停",
              be.system_budget_stopped(ws, "") is False)
    finally:
        _rm.usage_summary = _orig

    print("[5] 单价也从 budget.json 读；estimate_from_blueprint 默认用上")
    (ws / ".sixcoder" / "budget.json").write_text(json.dumps({
        "system_budget_cny": 100.0,
        "price_offpeak_cny_per_mtok": 0.66,
        "price_peak_cny_per_mtok": 1.56}), encoding="utf-8")
    pr = be.load_prices(ws)
    check("读到谷时单价 0.66", pr.get("offpeak") == 0.66, str(pr))
    check("读到峰时单价 1.56", pr.get("peak") == 1.56, str(pr))
    # 假蓝图：2 条 absent → 基线 499k tok/条 × 2 × 1.2 = 1.1976M tok
    (ws / ".sixcoder" / "blueprint.json").write_text(json.dumps({
        "caps": [{"id": "a", "status": "absent"}, {"id": "b", "status": "absent"}]}),
        encoding="utf-8")
    est = be.estimate_from_blueprint(ws)
    check("开局估算可用（剩余 2 条）", est.get("ok") is True and est.get("remaining") == 2,
          str(est.get("reason") or est.get("remaining")))
    check("金额按用户单价算出（谷时 ≈¥0.79）",
          est.get("budget_offpeak") is not None and abs(est["budget_offpeak"] - 0.79) < 0.02,
          str(est.get("budget_offpeak")))
    snap = be.budget_snapshot(ws)
    check("预算快照齐全（预算/阈值/两个单价）",
          snap["system_budget_cny"] == 100.0 and snap["has_stop_line"] is True
          and snap["price_offpeak_cny_per_mtok"] == 0.66)

    print("[6] 两种夜班模式都已接线（估算 + 唯一停机线）")
    comp = (ROOT / "core" / "company.py").read_text(encoding="utf-8", errors="replace")
    auto = (ROOT / "core" / "auto.py").read_text(encoding="utf-8", errors="replace")
    check("甲方模式：蓝图构建后发 night_estimate 事件", "night_estimate" in comp)
    check("甲方模式：轮循环接系统预算停机", "system_budget_stopped(ws, task_id)" in comp)
    check("全自主：片循环接系统预算停机", 'system_budget_stopped(ws, "")' in auto)
    check("全自主：slice_start 事件带系统预算字段", "system_budget_cny" in auto)
    check("server：有 /v1/night/estimate 两步法端点",
          "/v1/night/estimate" in (ROOT / "server.py").read_text(encoding="utf-8",
                                                                 errors="replace"))

    print("[7] 计数口径：一个项目一个账（2026-11-10 18:05 事故的钉子）")
    be_src = (ROOT / "core" / "budget_estimate.py").read_text(encoding="utf-8", errors="replace")
    # ① 空 task_id（全自主夜班）的停机判定必须落在**本项目计数器**上
    check("停机判定引用 project_spent_cny（不是全量历史账本）",
          "project_spent_cny(ws)" in be_src)
    # ② 全量账本在预算模块里只许出现**一次**（坏数据兜底的那次比较），
    #    绝不许当成 spent 的取值 —— 那正是这次事故
    _n_global = be_src.count('_rm.usage_summary("")')
    check("全量账本在预算模块里只出现一次（坏数据兜底）", _n_global == 1, str(_n_global))
    # ③ 那个诱导误用的旧名字不许回来：它读的是全量，名字却像"当前花费"
    check("auto.py 的全局读数已改名 _global_spend_marker",
          "def _global_spend_marker" in auto)
    check("auto.py 里不许再有 def _usage_cny", "def _usage_cny" not in auto)

    print("\n全部通过（估算不限制、价格用户填、唯一停机线=系统预算×1.05、两模式已接线）"
          if not FAILS else f"\n有失败项：{', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
