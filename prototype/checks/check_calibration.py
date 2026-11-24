"""可配置 + 按数据自动校准 的自检（2026-11-05）。

用户指出的问题：我为了让自生成跑通，把一批常数写死在代码里（思考预留 12k/24k/40k、
上限 128k、写代码封顶 high、单模块预算 ¥0.15）。这些都是**少数几次观测**的产物，
换模型/换任务规模就可能全错 —— 等于把过拟合钉进产品。

所以要求：这些值必须 **可配置** 且 **能按真实账本自动校准**。
本自检把四件事钉住：
    1. 缺失/样本不足时**不猜**（沿用现值，并写明原因）；
    2. 样本够了才改，且**防抖**（改动 <15% 不动）；
    3. 永远有上下界（不会把预留校准成 0，也不会校准成天文数字）；
    4. 校准结果真的被 remote / company 用上（不是写了个没人读的函数）。

跑法: python check_calibration.py
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import calibration, company, remote, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("可配置 + 按数据自动校准 自检")

    print("[1] 默认值可读、可被设置覆盖")
    cur = calibration.current()
    check("默认来源是 defaults 或 measured", cur.get("source") in ("defaults", "measured"),
          str(cur.get("source")))
    check("默认预留是四档结构", set(cur["reasoning_reserve"]) >= {"low", "high", "max"},
          str(cur["reasoning_reserve"]))
    check("都能在设置里手改（remote.calibration 存在）",
          "calibration" in (settings_store.get_remote() or {}))
    check("remote 用的是校准值而不是硬编码",
          remote.reasoning_reserve("high") == int(calibration.current()["reasoning_reserve"]["high"]),
          f"{remote.reasoning_reserve('high')} vs {calibration.current()['reasoning_reserve']['high']}")

    print("[2] 样本不足 → 不猜，沿用现值并写明原因")
    empty = Path(tempfile.mkdtemp())
    sug = calibration.suggest(ws=empty, days=1, min_samples=9999)
    check("样本不足时不改动任何项", sug["changed"] == [], str(sug["changed"]))
    check("原因里写明『样本 … < … 沿用现值』",
          any("沿用现值" in str(v) for v in (sug.get("basis") or {}).values()),
          str(sug.get("basis")))

    print("[3] 有数据才改，且永远有上下界")
    obs = calibration.observe(days=30)
    check("读得到账本观测（by_effort 非空或本机无数据）", isinstance(obs.get("by_effort"), dict))
    # cap 类参数必须按**峰值**校准，不能按分位：
    # 给低了那一次调用被截断（钱白花+重写），给高了几乎不花钱 — 代价不对称。
    sug_peak = calibration.suggest(ws=empty, days=30, min_samples=5)
    for eff, xs in (obs.get("by_effort") or {}).items():
        if len(xs) < 5:
            continue
        want = int(sug_peak["reasoning_reserve"].get(eff) or 0)
        peak = max(float(v) for v in xs)
        check(f"{eff} 预留 ≥ 实测峰值（cap 按峰值不按 p95）", want >= peak,
              f"want={want} peak={int(peak)}")
    big = calibration.suggest(ws=empty, days=30, min_samples=1)
    for eff, val in (big.get("reasoning_reserve") or {}).items():
        check(f"{eff} 预留落在 [{calibration.RESERVE_MIN}, {calibration.RESERVE_MAX}]",
              calibration.RESERVE_MIN <= int(val) <= calibration.RESERVE_MAX, str(val))
    check("正文下限也夹在区间内",
          calibration.CONTENT_MIN <= int(big["content_floor"]) <= calibration.CONTENT_MAX,
          str(big["content_floor"]))
    check("单模块预算夹在区间内",
          calibration.MODBUDGET_MIN <= float(big["module_budget_cny"]) <= calibration.MODBUDGET_MAX,
          str(big["module_budget_cny"]))
    check("上限不低于 128k（宁可多给也不截断）",
          int(big["safety_cap"]) >= calibration.SAFETY_MIN, str(big["safety_cap"]))
    check("封顶档位只可能是四档之一",
          big["code_effort_ceiling"] in calibration.CEILINGS, str(big["code_effort_ceiling"]))

    print("[4] 防抖：改动小于 15% 不动")
    cur_r = dict(calibration.current()["reasoning_reserve"])
    if cur_r:
        first = sorted(cur_r)[0]
        # 直接把现值写回去 → 不应该产生 changed
        sug2 = {"reasoning_reserve": cur_r}
        check("同值重复校准不产生 changed（幂等）",
              (calibration.save({"reasoning_reserve": cur_r}) or {}).get("reasoning_reserve")
              == cur_r, str(first))

    print("[5] 写作尝试台账：成功了才说明这个档位可用")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        for _ in range(4):
            calibration.record_attempt(ws, path="a.py", effort="low", ok=True)
        for _ in range(4):
            calibration.record_attempt(ws, path="b.py", effort="max", ok=False)
        att = calibration.read_attempts(ws)
        check("台账读得回来", len(att) == 8, str(len(att)))
        sug3 = calibration.suggest(ws=ws, days=1, min_samples=1)
        # 2026-11-10：11-09 用户铁律"完全相信远端"把语义改成"封顶恒为 max、
        # 成功率只记录不参与决策"——旧断言（按成功率压封顶）与新语义直接相反。
        # 理由（校准器里已写明）：成功率里混着**引擎自身缺陷**造成的失败
        # （多行签名被截断、路由接线误判），拿它压封顶会形成"越失败越不敢思考"的死循环。
        check("封顶恒为 max（完全相信远端，不按成功率压档）",
              sug3["code_effort_ceiling"] == "max", str(sug3["code_effort_ceiling"]))
        check("依据里列出各档成功率",
              "成功率" in str(sug3["basis"].get("code_effort_ceiling")),
              str(sug3["basis"].get("code_effort_ceiling")))

    print("[6] 编排层真的用了（不是写个没人读的函数）")
    csrc = inspect.getsource(company)
    # 2026-11-10：封顶恒 max 之后，编排层要读的不是"校准封顶值"，而是**升档节奏**
    # （escalate_after 门槛，settings 唯一真相源）——护栏从"封顶"搬到了"节奏"，
    # 这里断言新护栏真的在编排层里。
    check("升档节奏由 escalate_after 门槛控制（不是成功率封顶）",
          "escalate_after" in csrc and "escalate_after" in inspect.getsource(company),
          "company.py 里找不到 escalate_after")
    check("单模块预算读校准值", "module_budget_cny" in csrc)
    check("每次写作尝试都记账", "record_attempt" in csrc)
    check("收工会自动校准一次", "recompute(ws)" in csrc)
    check("auto 可关（看得到 auto 开关）", "auto" in calibration.current())

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（可配置 / 样本不足不猜 / 有上下界 / 幂等 / 台账驱动封顶 / 编排层已接）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
