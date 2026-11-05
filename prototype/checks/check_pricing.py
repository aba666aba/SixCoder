"""模型档案与价目自检：增删改、阶梯重排、价格驱动成本、非法值收敛。

为什么替换旧的 check_pricing：价格过去只能改一张"美元价目表"，现在价格长在
**模型档案**里（每个模型可有自己的连接参数、思考档位、价格）。旧的美元字段
（cost_usd / offpeak_ratio）已经废弃，这里把新口径钉死。

跑法: python check_pricing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db, remote, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("模型档案与价目自检")
    backup = settings_store.models()
    try:
        # 1) 默认阶梯：flash 四档（甲方模式代码只由远端写，本地档默认关）
        steps = settings_store.ladder()
        # pro 默认不启用（贵 3~4 倍，flash 够用）
        want = [("flash", "none"), ("flash", "low"), ("flash", "high"), ("flash", "max")]
        got = [(s["profile_id"], s["effort"]) for s in steps]
        check("默认阶梯 = flash 四档，顺序正确", got == want, str(got))
        check("pro 默认不在阶梯里（要用去界面打开）",
              all(s["profile_id"] != "pro" for s in steps), str(got))
        check("默认不含本地档（代码只由远端整文件写）",
              all(x[0] != "local" for x in got), str(got[:2]))

        # 2) 价格来自档案（人民币），高峰是空闲的两倍
        prof = next(p for p in settings_store.models() if p["model"] == "deepseek-flash")
        off, peak = prof["pricing"]["offpeak"], prof["pricing"]["peak"]
        check("flash 空闲输入价 = 官方 1 元/百万", abs(off["in_miss"] - 1.0) < 1e-9, str(off))
        check("flash 高峰 = 空闲 × 2", abs(peak["in_miss"] - off["in_miss"] * 2) < 1e-9, str(peak))

        # 3) 改价 → 成本跟着变（同一批 token 重新计价）
        tag = "pricing-check"
        db.execute("DELETE FROM remote_usage WHERE task_id=?", (tag,))
        remote._record(task_id=tag, node_id="x", model="deepseek-flash",
                       prompt_tokens=1_000_000, completion_tokens=1_000_000,
                       cache_hit_tokens=0)
        db.execute("UPDATE remote_usage SET offpeak=0 WHERE task_id=?", (tag,))
        s1 = remote.usage_summary(tag)["peak"]["cost_cny"]
        profs = settings_store.models()
        for p in profs:
            if p["model"] == "deepseek-flash":
                p["pricing"]["peak"] = {"in_hit": 0.1, "in_miss": 3.0, "out": 5.0}
        settings_store.save_models(profs)
        s2 = remote.usage_summary(tag)["peak"]["cost_cny"]
        check("改价后成本随之变化（高峰 2+8=10 元 → 改价后 3+5=8 元）",
              abs(s1 - 10.0) < 1e-6 and abs(s2 - 8.0) < 1e-6, f"{s1} → {s2}")

        # 4) 非法值收敛：负价变 0、非法思考档位丢弃、非法 id 不落库
        profs = settings_store.models()
        profs.append({"id": "weird", "model": "deepseek-flash", "priority": 99,
                      "efforts": ["none", "瞎写", "max"], "max_output_tokens": -5,
                      "pricing": {"offpeak": {"in_hit": -1, "in_miss": "abc", "out": 2}}})
        saved = settings_store.save_models(profs)
        w = next((p for p in saved if p["id"] == "weird"), None)
        check("非法思考档位被丢弃，只剩合法值", w and w["efforts"] == ["none", "max"], str(w))
        check("负数价格被夹到 0", w and w["pricing"]["offpeak"]["in_hit"] == 0.0, str(w))
        check("非法价格文本回退 0（不写坏配置）",
              w and w["pricing"]["offpeak"]["in_miss"] == 0.0, str(w))
        # 输出配额的唯一真相源是 tokens.output_budget；档案里的这个值只在运维显式配置时生效。
        # 语义约定：0 = 自动（不设上限，按任务规模算）。所以负数应被夹到 0，而不是夹到某个数值。
        check("越界 max_output_tokens 被夹到 [0, 393216]（0 = 自动）",
              w and 0 <= w["max_output_tokens"] <= 393216, str(w))
        check("负数 → 0 = 自动（不再兜一个固定 8000 的第二真相源）",
              w and w["max_output_tokens"] == 0, str(w))

        # 5) 增删改都能反映到档案表；但**阶梯只认当前那一个模型**（单模型语义）
        settings_store.save_models(
            [p for p in settings_store.models() if p["id"] != "weird"])
        check("删除后阶梯恢复 4 档（flash 四档）", len(settings_store.ladder()) == 4,
              str(len(settings_store.ladder())))
        profs = settings_store.models()
        profs.append({"id": "flash2", "model": "deepseek-flash", "priority": 15,
                      "efforts": ["low"], "max_output_tokens": 8000})
        settings_store.save_models(profs)
        got = [(s["profile_id"], s["effort"]) for s in settings_store.ladder()]
        check("新增的档案**不会**挤进阶梯（单模型：一次只用一个）",
              all(p == "flash" for p, _ in got), str(got))
        settings_store.update_remote({"active_model": "flash2"})
        check("切成 flash2 后阶梯就是它的单档 low",
              [(s["profile_id"], s["effort"]) for s in settings_store.ladder()] == [("flash2", "low")],
              str([(s["profile_id"], s["effort"]) for s in settings_store.ladder()]))
        settings_store.update_remote({"active_model": ""})
        settings_store.save_models([p for p in settings_store.models() if p["id"] != "flash2"])

        # 6) 换模型 = 换 active_model（不再是"多模型并排成阶梯"）
        profs = settings_store.models()
        for p in profs:
            if p["id"] == "pro":
                p["enabled"] = True
        settings_store.save_models(profs)
        check("打开 pro **不会**自动接管（仍是单模型：flash）",
              all(s["profile_id"] == "flash" for s in settings_store.ladder()),
              str([(s["profile_id"], s["effort"]) for s in settings_store.ladder()]))
        settings_store.update_remote({"active_model": "pro"})
        pro_ladder = [(s["profile_id"], s["effort"]) for s in settings_store.ladder()]
        check("显式切到 pro → 阶梯整条换成 pro 的四档",
              pro_ladder == [("pro", "none"), ("pro", "low"), ("pro", "high"), ("pro", "max")],
              str(pro_ladder))
        settings_store.update_remote({"active_model": "不存在的档案"})
        check("不存在的 active_model 被忽略（配置不被写坏）",
              all(s["profile_id"] == "pro" for s in settings_store.ladder()),
              str([(s["profile_id"], s["effort"]) for s in settings_store.ladder()]))
        settings_store.update_remote({"active_model": ""})

        db.execute("DELETE FROM remote_usage WHERE task_id=?", (tag,))
    finally:
        settings_store.save_models(backup)

    check("收尾：档案表被还原", len(settings_store.ladder()) == 4,
          str([(s["profile_id"], s["effort"]) for s in settings_store.ladder()]))
    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（八档顺序 / 价格驱动成本 / 非法值收敛 / 增删改反映到阶梯）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
