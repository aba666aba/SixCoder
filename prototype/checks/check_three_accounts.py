"""三账分离自检（2026-11-11 用户定稿）。

**三个量，分开算，不许互相替代**：

| 量 | 是什么 | 谁产出 | 性质 | 用途 |
|---|---|---|---|---|
| ① 项目需要的预算 | 这个项目做完大概要花多少 | 引擎估算（token × 用户填的单价）| **预测** | 充值决策 |
| ② 实际花费 | 已经花了多少 | 运行时逐笔记账 | **事实** | 交付报告 |
| ③ 余额 | 账号还剩多少 | DeepSeek 官方 `GET /user/balance` | **事实** | 硬约束 |

**为什么必须分开**（实测 2026-11-11，同一天出现三个数）：
账本 ¥39.72 / `night_spent` ¥33.40 / 文档 ¥31.01 ——
引擎拿"①估算"和"②记账"在算"还能花多少"，而 ③ 才是事实。

**对账恒等式**：`充值总额 = ② 实际花费 + ③ 余额 + 赠金消耗`
→ 差额 = **账外消费**（别的会话/别的 key/别的项目/手工调用）或账本漏记。

**停机双闸并行**：
  · 闸门 A（预算闸）：② 累计 ≥ 系统预算 × 1.05
  · 闸门 B（余额闸）：③ ≤ 0 或 `is_available=false` → **立刻停**（事实优先）

本自检钉住这些口径。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import balance  # noqa: E402
from core import budget_estimate as be  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    import inspect

    print("[1] 余额闸：官方余额耗尽才停，且「查不到」不误停")
    hit, why = balance.balance_exhausted({"ok": True, "available": True,
                                          "total": 10.0, "currency": "CNY"})
    check("余额充足 → 不停", hit is False, why)
    hit2, why2 = balance.balance_exhausted({"ok": True, "available": True,
                                            "total": 0.0, "currency": "CNY"})
    check("余额为 0 → 停", hit2 is True and "耗尽" in why2, why2)
    hit3, why3 = balance.balance_exhausted({"ok": True, "available": False,
                                            "total": 5.0, "currency": "CNY"})
    check("is_available=false → 停", hit3 is True, why3)
    hit4, _ = balance.balance_exhausted({"ok": False, "detail": "查不到"})
    check("查不到 → 不误停（网络失败/key 失效不能当余额为零）",
          hit4 is False, "查不到余额却停机 = 误停")

    print("[2] 停机判定里必须有余额闸（双闸并行）")
    src = inspect.getsource(be.system_budget_stopped)
    check("system_budget_stopped 里接了余额闸",
          "balance_exhausted" in src,
          "只有预算闸 → 余额耗尽引擎还以为能跑")
    check("余额闸在预算闸**之前**（事实优先）",
          src.find("balance_exhausted") < src.find("load_system_budget"),
          "顺序反了 = 先按估算停了，事实没机会说话")

    print("[3] 对账：缺哪个就报缺哪个，绝不编数")
    rec = balance.reconcile(None)
    check("没登记充值总额 → outside_spend 为 None（不编数）",
          rec.get("outside_spend_cny") is None, str(rec))
    check("note 里点名缺哪一项",
          "充值总额" in (rec.get("note") or ""), str(rec.get("note")))

    print("[4] 三账在交付报告里各占一行（不许合并）")
    import core.company as comp
    csrc = inspect.getsource(comp)
    check("报告里有「实际花费」行", "实际花费" in csrc, "")
    check("报告里有「官方余额」行", "官方余额" in csrc, "")
    check("报告里有「系统总预算」行", "系统总预算" in csrc, "")

    print("[5] 充值总额的读取键名固定")
    check("键名是 recharged_cny", balance.RECHARGED_KEY == "recharged_cny", "")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
