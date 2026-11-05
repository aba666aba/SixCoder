"""「一轮写多少」是动态的：验收带宽自检（2026-11-05）。

旧实现把 `函数数 > 16` 写死在 contractgen.validate 里。16 不是物理上限，
它只是"一轮能验收多少"的经验值，而它随这些事实变化：

  · 这一档一次能吐多少 token（档案 max_output_tokens；0 = 自动时按档位估）
  · **思考强度会吃掉输出配额**（reasoning token 算在 max_tokens 里）→ effort 越高，
    可写代码量越小。这条最容易搞反，必须钉住。
  · 每个函数的代价（示例越多、异常越多越贵）
  · 依赖数（上下文越大越保守）
  · 近期一次通过率（低就缩小，高才敢放大 —— 先有证据再扩容）

跑法: python check_dosing.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contractgen, dosing  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("验收带宽（动态 dose）自检")

    print("[1] 配额来自档案；没配时按档位估")
    check("档案给了 max_output_tokens 就用它",
          dosing.capacity_tokens({"max_output_tokens": 20000, "effort": "none"}) == 20000)
    caps = {e: dosing.capacity_tokens({"max_output_tokens": 0, "effort": e})
            for e in ("none", "low", "high", "max")}
    check("没配时按档位估，且 none 最大", caps["none"] > caps["low"] > caps["high"] > caps["max"],
          str(caps))

    print("[2] 思考越深，能写的越少（reasoning 吃输出配额）")
    n_none, _ = dosing.suggest_max_funcs(capacity=caps["none"])
    n_max, _ = dosing.suggest_max_funcs(capacity=caps["max"])
    check("effort=max 的一轮函数数 < effort=none", n_max < n_none, f"{n_max} vs {n_none}")

    print("[3] 代价与依赖：越贵越保守")
    p1 = dosing.per_func_tokens(1, 0)
    p2 = dosing.per_func_tokens(4, 3)
    check("示例/异常越多，单函数越贵", p2 > p1, f"{p1} vs {p2}")
    a, _ = dosing.suggest_max_funcs(capacity=6000, per_func=150)
    b, why = dosing.suggest_max_funcs(capacity=6000, per_func=150, deps=5)
    check("依赖越多，一轮函数数越少", b < a, f"{b} vs {a}")
    check("依据里写明了依赖的影响", any("依赖" in x for x in why), str(why))

    print("[4] 一次通过率：低就缩，高才放，没有证据不乱猜")
    # 参数选在**没顶到 40 上限**的区间，否则放大看不出效果（这点本身也是信息：
    # 配额富余时，通过率只影响"要不要保守"，不影响"能不能更多"）
    lo, why_lo = dosing.suggest_max_funcs(capacity=3000, per_func=200, first_pass=0.3)
    mid, _ = dosing.suggest_max_funcs(capacity=3000, per_func=200, first_pass=0.7)
    hi, _ = dosing.suggest_max_funcs(capacity=3000, per_func=200, first_pass=0.95)
    check("通过率低 → 比中等更少", lo < mid, f"{lo} vs {mid}")
    check("通过率高 → 比中等更多", hi > mid, f"{hi} vs {mid}")
    check("通过率低时依据里写明", any("一次通过率" in x for x in why_lo), str(why_lo))
    capped_mid = dosing.suggest_max_funcs(capacity=10 ** 6, per_func=150, first_pass=0.7)[0]
    capped_hi = dosing.suggest_max_funcs(capacity=10 ** 6, per_func=150, first_pass=0.95)[0]
    check("配额富余时两者都被上限夹住（放大不会突破 40）",
          capped_mid == capped_hi == dosing.MAX_FUNCS, f"{capped_mid}/{capped_hi}")
    none_why = dosing.suggest_max_funcs(capacity=6000, per_func=150, first_pass=None)[1]
    check("没有实测数据时说明'按配额保守取'",
          any("还没有" in x for x in none_why), str(none_why))

    print("[5] 永远有上下界（动态 ≠ 无限）")
    tiny, _ = dosing.suggest_max_funcs(capacity=200, per_func=500)
    huge, _ = dosing.suggest_max_funcs(capacity=10 ** 7, per_func=80, first_pass=1.0)
    check("极小配额也至少 3 个（否则永远收不了一个模块）", tiny == dosing.MIN_FUNCS, str(tiny))
    check("极大配额也封顶 40", huge == dosing.MAX_FUNCS, str(huge))
    check("hard_cap 能进一步压低", dosing.suggest_max_funcs(capacity=10 ** 7, per_func=80,
                                                       hard_cap=10)[0] <= 10)

    print("[6] 接进契约合成（不是写了个没人用的函数）")
    sig = inspect.signature(contractgen.validate).parameters
    check("validate 接受动态 limit", "limit" in sig and sig["limit"].default is None)
    check("validate 不再写死 16", "n_funcs > 16" not in inspect.getsource(contractgen.validate))
    s_src = inspect.getsource(contractgen.synthesize)
    check("synthesize 用 dosing 算带宽", "suggest_max_funcs" in s_src)
    check("带宽写进提示词（远端知道这轮能做几个）", "验收带宽" in s_src)
    check("返回值带回 allowance（报告可追）", '"allowance"' in s_src)
    csrc = inspect.getsource(__import__("core.company", fromlist=["x"]))
    check("夜班按档位算带宽并把依据写进事件", '"_dosing"' in csrc or "import dosing" in csrc)
    check("每模块上限也跟着带宽走", "per_module_cap" in s_src)

    print("[7] 每个模块的上限分配")
    check("单模块 → 分到全部（但不超过 16）", dosing.per_module_cap(20, 1) == 16)
    check("多模块按比例分", dosing.per_module_cap(40, 4) == 10)
    check("模块极多时至少 4 个", dosing.per_module_cap(20, 20) == 4)

    print("[8] 契约锁存按需求指纹判（不然多轮长不起来）")
    import tempfile
    from pathlib import Path as _P
    _ws = _P(tempfile.mkdtemp())
    _txt = "1) a.py：\n   - f(x: int) -> int：加一\n示例（必须满足）：\nf(1) == 2\n"
    contractgen.save_locked(_ws, _txt, "需求甲")
    check("同一需求 → 复用锁存契约", bool(contractgen.load_locked(_ws, "需求甲").strip()))
    check("换了需求 → 不复用（第二轮才能合成新契约）",
          contractgen.load_locked(_ws, "需求乙") == "")
    check("不带需求的老调用仍复用（向后兼容）",
          bool(contractgen.load_locked(_ws).strip()))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（配额/思考/代价/依赖/通过率 → 动态带宽；有上下界；已接进合成与夜班）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
