"""预算机制（2026-11-10 重设计 · 用户定稿口径）。

关于花费，系统现在只有「三件事 + 一条线」：
  1. 开局估算：蓝图构建后算「预计需要多少钱」（token 量 × 用户填的单价），展示给用户去充值；
  2. 记录：开工后由 `remote_usage` 账本 + `night_spent.json` 实时累计实际花费；
  3. 报告：项目结束在交付报告里报总消费；
  4. **唯一停机线**：总花费 ≥ 系统给的总预算 × 1.05 自动停机（`system_budget_stopped`）。

红线（不可逾越）：
  · 用户**不能**主动设置任何花费上限（`--max-cny` / `max_cny` / `SIXCODER_MAX_CNY` 全部移除）；
  · 系统预算写在产品仓 `.sixcoder/budget.json` 的 `system_budget_cny`，只有系统/运维能改；
  · 本模块只算 + 提供停机判定，不写任何账本、不接任何 UI 状态。

为什么基线用 token 而不是金额：同样的代码量，峰时价是谷时的两倍，
用金额做基线会把「什么时候跑」误当成「要干多少活」。
"""
from __future__ import annotations

import json
from pathlib import Path

# 富余系数：稳定段样本通常只有几条能力，给 20% 覆盖剩余能力的难度波动。
BUFFER = 0.20
# 峰时窗口（北京时间，工作日）用于生成"晚上启动"提示；实际判定以
# core.remote.is_offpeak() 与用户设置的 peak_window 为准，这里只写给人看。
PEAK_HINT = "工作日 09:00-12:00、14:00-18:00"

TOKENS_PER_MILLION = 1_000_000


def tokens_per_cap(stable_tokens: int, gained: int) -> float:
    """稳定段总 token ÷ 稳定段净增 done 数 = 每条能力的平均 token 消耗。

    样本不足（gained<=0 / tokens<=0）返回 0.0，由调用方决定不估算——**绝不编一个数**。
    """
    if gained <= 0 or stable_tokens <= 0:
        return 0.0
    return float(stable_tokens) / float(gained)


def estimate(*, tokens_per_cap: float, remaining: int, total_caps: int = 0,
             buffer: float = BUFFER, price_offpeak: float | None = None,
             price_peak: float | None = None) -> dict:
    """按定稿口径估算。价格单位：¥ / 百万 token（用户填写）。

    - `price_offpeak` / `price_peak` 为 None 时**不输出金额**，只输出 token 需求。
    - `total_caps` 给全量条数时，额外输出"从零做到完"的总 token 量（供参考）。
    """
    base = max(0.0, float(tokens_per_cap)) * max(0, int(remaining))
    need = base * (1.0 + float(buffer))
    out: dict = {
        "tokens_per_cap": float(tokens_per_cap),
        "remaining": int(remaining),
        "total_caps": int(total_caps or 0),
        "base_tokens": round(base),
        "buffer": float(buffer),
        "need_tokens": round(need),                       # 含富余，充值就按它算
        "project_total_tokens": round(max(0.0, float(tokens_per_cap))
                                      * max(0, int(total_caps)) * (1.0 + float(buffer))),
        "price_offpeak": price_offpeak,
        "price_peak": price_peak,
        "budget_offpeak": None,
        "budget_peak": None,
        "saving_cny": None,
    }
    if price_offpeak is not None and float(price_offpeak) > 0:
        out["budget_offpeak"] = round(need / TOKENS_PER_MILLION * float(price_offpeak), 2)
    if price_peak is not None and float(price_peak) > 0:
        out["budget_peak"] = round(need / TOKENS_PER_MILLION * float(price_peak), 2)
    if out["budget_offpeak"] is not None and out["budget_peak"] is not None:
        out["saving_cny"] = round(out["budget_peak"] - out["budget_offpeak"], 2)
    return out


def format_report(est: dict) -> str:
    """给用户看的估算文案：金额、峰谷对照、晚上启动提示。"""
    m = est["need_tokens"] / TOKENS_PER_MILLION
    lines = [
        "预算估算（只计算，不限制；价格由你填写）",
        f"  稳定段平均 {est['tokens_per_cap']/1000:.0f}k tok/条 × 剩余 {est['remaining']} 条"
        f" × 富余 {1+est['buffer']:.2f} = 需 {m:.1f}M tokens",
    ]
    if est.get("budget_offpeak") is not None:
        lines.append(f"  按谷时单价 ¥{est['price_offpeak']}/M → 建议充值 ¥{est['budget_offpeak']:.2f}")
    else:
        lines.append("  未填写谷时单价 → 只给 token 量，不编金额；请填入单价后自行充值")
    if est.get("budget_peak") is not None:
        lines.append(f"  按峰时单价 ¥{est['price_peak']}/M → ¥{est['budget_peak']:.2f}"
                     + (f"（谷时启动省 ¥{est['saving_cny']:.2f}）" if est.get("saving_cny") else ""))
    lines += [
        f"  建议 18:00 之后（谷时）启动夜班；峰时窗口 {PEAK_HINT} 避免新启动，",
        "  已运行的实例不必停机。周末/节假日全天谷时。",
        "  本估算仅供充值参考，不做任何停机限制；实际花费以项目结束报告为准。",
    ]
    return "\n".join(lines)


def from_workspace(ws, repo, *, since: str, price_offpeak: float | None = None,
                   price_peak: float | None = None, buffer: float = BUFFER) -> dict:
    """便捷取数（给审核时手动跑 / 将来接回用）：读账本稳定段 + 覆盖增量 + 蓝图剩余条数。

    `since` 格式 'YYYY-MM-DD HH:MM'，由用户指定"稳定之后"的起点；本模块不猜起点。
    只读，不写任何文件，不改变夜班状态。
    """
    from . import db

    rows = db.query(
        "SELECT COALESCE(SUM(prompt_tokens),0) pin, COALESCE(SUM(completion_tokens),0) pout "
        "FROM remote_usage WHERE created_at >= ?", (since,))
    tok = int(rows[0]["pin"] or 0) + int(rows[0]["pout"] or 0) if rows else 0

    bp_path = Path(ws) / ".sixcoder" / "blueprint.json"
    bp = json.loads(bp_path.read_text(encoding="utf-8-sig")) if bp_path.exists() else {"caps": []}
    caps = bp.get("caps") or []
    done = sum(1 for c in caps if c.get("status") == "done")
    remaining = len(caps) - done

    cov = Path(ws) / ".sixcoder" / "coverage.jsonl"
    gained = 0
    if cov.exists():
        import datetime as _dt
        cutoff = _dt.datetime.strptime(since, "%Y-%m-%d %H:%M").timestamp()
        obs = []
        for ln in cov.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(ln)
                if float(d.get("ts") or 0) >= cutoff:
                    obs.append(int(d.get("done") or 0))
            except (ValueError, TypeError):
                continue
        if len(obs) >= 2:
            gained = max(0, obs[-1] - obs[0])
    tpc = tokens_per_cap(tok, gained)
    est = estimate(tokens_per_cap=tpc, remaining=remaining, total_caps=len(caps),
                   buffer=buffer, price_offpeak=price_offpeak, price_peak=price_peak)
    est.update({"since": since, "stable_tokens": tok, "gained": gained,
                "done": done, "ok": tpc > 0})
    return est


# ---------------------------------------------------------------------------
# 系统总预算（唯一停机线，用户不可设上限）
# ---------------------------------------------------------------------------
# 基线 token/条：稳定段实测的历史均值（2026-11-10 夜班稳定段约 499k tok/条）。
# 属可调参数：改这一个常量即可，不写死进任何估算公式。
BASELINE_TOK_PER_CAP = 499_000
# 唯一停机阈值：总花费超过系统总预算的 5% 即自动收工。
OVER_CAP_RATIO = 1.05
# 系统预算配置文件（产品仓内，由系统/运维填写，用户改不了）
_BUDGET_CFG = ".sixcoder/budget.json"


def load_system_budget(ws) -> float:
    """读系统给的总预算（人民币）。文件不存在或字段缺失返回 0.0（= 不启停机线）。

    0 / 负数 = 不设上限（系统未给预算）。这是**唯一**的停机线，
    且用户无法通过任何 CLI / 界面参数覆盖它。
    """
    try:
        p = Path(ws) / _BUDGET_CFG
        if not p.exists():
            return 0.0
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        v = float(d.get("system_budget_cny") or 0.0)
        return v if v > 0 else 0.0
    except Exception:                              # noqa: BLE001
        return 0.0


# 用户填的谷/峰单价（¥ / 百万 token），同样写在 .sixcoder/budget.json。
# 不填 = 只给 token 需求、不编金额（价格是用户自己填的，引擎不替他拍板）。
_PRICE_OFFPEAK_KEY = "price_offpeak_cny_per_mtok"
_PRICE_PEAK_KEY = "price_peak_cny_per_mtok"


def load_prices(ws) -> dict:
    """读用户填的谷/峰单价（¥/M tok），来自 .sixcoder/budget.json。

    缺失 / 0 / 空 = None（不估金额，只估 token 量）。引擎绝不替用户编造价格。
    """
    out = {"offpeak": None, "peak": None}
    try:
        p = Path(ws) / _BUDGET_CFG
        if not p.exists():
            return out
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        for key, dst in ((_PRICE_OFFPEAK_KEY, "offpeak"), (_PRICE_PEAK_KEY, "peak")):
            v = d.get(key)
            if v not in (None, "", 0):
                try:
                    out[dst] = float(v)
                except (TypeError, ValueError):
                    out[dst] = None
    except Exception:                              # noqa: BLE001
        pass
    return out


def budget_snapshot(ws) -> dict:
    """给 UI 展示的预算配置快照：系统预算、用户单价、停机阈值（系统预算 × 1.05）。

    只读 .sixcoder/budget.json，不写任何东西。
    """
    cap = load_system_budget(ws)
    prices = load_prices(ws)
    return {
        "system_budget_cny": cap,
        "stop_threshold_cny": round(cap * OVER_CAP_RATIO, 2) if cap > 0 else 0.0,
        "price_offpeak_cny_per_mtok": prices.get("offpeak"),
        "price_peak_cny_per_mtok": prices.get("peak"),
        "has_stop_line": cap > 0,
    }


def project_spent_cny(ws) -> float:
    """**本项目**已花多少钱（一门一账，2026-11-10 用户定稿）。

    读取顺序：
      ① `project_ledger` 项目账本（新机制，`remote.complete` 每笔调用实时累加）；
      ② 账本里还没有这个项目（首次运行/未迁移）→ 退回 `night_spent.json` 计数器；
      ③ 两者都有 → **取较大值**并把它对账回账本（只升不降）。

    为什么必须"取较大值"：跑着旧代码的进程仍按 night_spent.json 记账，
    重启后新代码读账本 —— 两套账可能差一点点。少算 = 停机线失效 = 超支，
    所以宁可保守（取大）也绝不少算。

    为什么不能直接用 `usage_summary("")`：那是**全量历史账本**（所有项目 + 所有实验）。
    实测 2026-11-10 18:05：项目只花 ¥31.01，历史累计 ¥110.35 ≥ ¥105 →
    夜班第一轮就判"超额 5%"收工，一轮活都没干。
    """
    legacy = 0.0
    try:
        p = Path(ws) / ".sixcoder" / "night_spent.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        legacy = max(0.0, float(d.get("total_cny") or 0.0))
    except Exception:                            # noqa: BLE001
        legacy = 0.0
    try:
        from . import project_ledger as _pl
        cur = _pl.total(ws)
        if cur is None:                          # 账本还没有这个项目 → 用计数器
            return legacy
        # 取较大值：少算 = 停机线失效 = 超支。**这里只读不写** ——
        # 读函数带写副作用会把自检/临时目录的测试数据写进真账本（已踩过）。
        # 对账写回由 `tools/migrate_ledger.py` 与夜班启动时的显式 `bump_to` 负责。
        return max(cur, legacy)
    except Exception:                            # noqa: BLE001
        return legacy


def system_budget_stopped(ws, task_id: str) -> bool:
    """停机判定：**双闸并行**（2026-11-11 用户定稿）。

    · **闸门 A（预算闸）**：本项目总花费 ≥ 系统总预算 × 1.05
      —— 依据是引擎记账（估算性质），保留但不独占。
    · **闸门 B（余额闸）**：**官方余额耗尽**（`GET /user/balance` 返回 ≤ 0 或
      `is_available=false`）→ **立刻停**。
      余额是**账号的客观事实**，不该由"自算花费"这种估算决定能不能继续花钱。

    三账分离（§4.6 / 2026-11-11）：
      ① 项目需要的预算 = 引擎估算（预测）｜② 实际花费 = 运行时记账（事实）
      ③ 余额 = 官方接口（事实）—— 三者**不许互相替代**。

    系统预算 ≤ 0（未设）= 预算闸永不触发（**但余额闸照样生效**：
    余额是硬事实，与用户有没有设预算无关）。

    口径（2026-11-10 修正）：全自主夜班 `task_id` 传空 —— 以前空 task_id 会退化成
    "全量历史账本"，把别的项目/别的实验的花费也算进本项目额度（实测导致秒停）。
    现在空 task_id 一律取**本项目计数器**（`.sixcoder/night_spent.json`），
    与交付报告、UI 的"系统预算剩余"完全同源；显式 task_id 才按任务账本取。

    **查不到余额 ≠ 余额为零**（网络失败 / key 失效 / 端点非官方）→ 一律按"取不到"
    处理，**绝不误停**（与既有口径一致）。
    """
    # 闸门 B：官方余额（事实优先）
    try:
        from . import balance as _bal
        _hit, _why = _bal.balance_exhausted()
        if _hit:
            return True
    except Exception:                                    # noqa: BLE001
        pass                                             # 取不到 → 不误停
    cap = load_system_budget(ws)
    if cap <= 0:
        return False
    try:
        from . import remote as _rm
        if task_id:
            spent = float((_rm.usage_summary(task_id) or {}).get("cost_cny") or 0.0)
        else:
            global_spent = float((_rm.usage_summary("") or {}).get("cost_cny") or 0.0)
            spent = project_spent_cny(ws)
            # 计数器比全量还大 = 坏数据（项目花费不可能超过所有项目之和）。
            # 夹到全量等于又拿全量当本项目（正是本次事故），所以按"取不到"处理：
            # 绝不因为一个坏数字提前停机。
            if spent > global_spent:
                return False
    except Exception:                             # noqa: BLE001
        return False
    return spent >= cap * OVER_CAP_RATIO


def estimate_from_blueprint(ws, *, price_offpeak: float | None = None,
                            price_peak: float | None = None,
                            buffer: float = BUFFER,
                            baseline_tok_per_cap: float = BASELINE_TOK_PER_CAP) -> dict:
    """开局估算（蓝图已构建后调用）：数剩余能力数 × 基线 token/条 × 富余 → token 需求。

    - 价格由用户填（price_offpeak / price_peak，¥/M tok）；不显式传就读取
      .sixcoder/budget.json 里用户填的单价；都没有只给 token 量不编金额；
    - 基线 token/条用历史均值常量，可从配置调；不靠本 run 实际用量（开局时还没有）；
    - 只读蓝图，不改任何状态，不写账本。
    """
    # 没显式给单价时，默认用用户在 budget.json 里填的（价格是用户自己填的，引擎不替他拍板）
    if price_offpeak is None or price_peak is None:
        _p = load_prices(ws)
        if price_offpeak is None:
            price_offpeak = _p.get("offpeak")
        if price_peak is None:
            price_peak = _p.get("peak")
    bp_path = Path(ws) / ".sixcoder" / "blueprint.json"
    if not bp_path.exists():
        return {"ok": False, "reason": "blueprint.json 不存在，无法估算"}
    try:
        bp = json.loads(bp_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "reason": f"蓝图解析失败：{exc}"}
    caps = bp.get("caps") or []
    done = sum(1 for c in caps if c.get("status") == "done")
    remaining = len(caps) - done
    if remaining <= 0:
        return {"ok": False, "reason": "没有剩余能力，无需估算", "remaining": 0,
                "total_caps": len(caps), "done": done}
    est = estimate(tokens_per_cap=baseline_tok_per_cap, remaining=remaining,
                   total_caps=len(caps), buffer=buffer,
                   price_offpeak=price_offpeak, price_peak=price_peak)
    est.update({"ok": True, "source": "blueprint", "baseline_tok_per_cap": baseline_tok_per_cap,
                "done": done})
    return est


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="预算估算（只读、不接夜班）")
    ap.add_argument("--repo", required=True, help="产物仓库路径")
    ap.add_argument("--since", required=True, help="稳定段起点 YYYY-MM-DD HH:MM")
    ap.add_argument("--price-offpeak", type=float, default=None, help="谷时 ¥/M tok（用户填写）")
    ap.add_argument("--price-peak", type=float, default=None, help="峰时 ¥/M tok（用户填写）")
    a = ap.parse_args()
    e = from_workspace(a.repo, a.repo, since=a.since,
                       price_offpeak=a.price_offpeak, price_peak=a.price_peak)
    print(format_report(e))
