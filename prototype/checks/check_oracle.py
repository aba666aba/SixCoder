"""oracle 升级（§13.4 第 2 步）自检：计算校验 + 性质 + 复杂度 + e2e + 变异 当硬判据。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import oracle, company, iterate  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


MOD = '''
def abs_sum(a, b):
    return abs(a) + abs(b)

def very_complex(x):
    s = 0
    if x == 1: s += 1
    elif x == 2: s += 2
    elif x == 3: s += 3
    elif x == 4: s += 4
    elif x == 5: s += 5
    elif x == 6: s += 6
    elif x == 7: s += 7
    elif x == 8: s += 8
    elif x == 9: s += 9
    elif x == 10: s += 10
    elif x == 11: s += 11
    elif x == 12: s += 12
    else: s += 0
    return s
'''


def _patch():
    """模拟变异 / e2e 引擎给出『实测不达标』，验证 oracle 会把它当硬红。"""
    import core.oracle as O
    from core import arch, e2e
    from core.gates import GateResult

    orig_m = arch.gate_mutation_changed
    orig_e = e2e.write

    def fake_mutation(ws, changed=None, level="L"):
        return GateResult("mutation_changed", False, "engine_assertion", "killed=1/5",
                          "变异杀死率 20%（L 级要求 ≥70%）→ 这次改动没被测住", 1)

    def fake_e2e(ws, contracts=None, requirement="", fname="x", run=True):
        return {"ok": False, "reason": "业务流断言失败：checkout 后余额未更新", "covers": []}

    arch.gate_mutation_changed = fake_mutation
    e2e.write = fake_e2e
    return orig_m, orig_e


def main() -> int:
    print("oracle 升级自检（§13.4 第 2 步）")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "m.py").write_text(MOD, encoding="utf-8")

        # 1) 计算校验：期望值对 / 错
        c_ok = oracle.check(ws, {"module": "m",
                                "checks": [{"func": "abs_sum", "args": ["3", "-4"],
                                            "expect_return": 7}]})
        check("计算校验通过（期望 7 实测 7）", not c_ok["divergences"])
        c_bad = oracle.check(ws, {"module": "m",
                                  "checks": [{"func": "abs_sum", "args": ["3", "-4"],
                                              "expect_return": 99}]})
        check("计算校验抓出错误期望（99 vs 7）",
              any("不一致" in d["verdict"] for d in c_bad["divergences"]))

        # 2) 性质测试：成立 / 不成立
        res_prop_ok = oracle.evaluate(ws, {"module": "m",
            "properties": [{"func": "abs_sum", "samples": [[3, -4], [0, 0], [-1, 5]],
                            "expect": "r >= 0"}]}, "S")
        check("性质测试通过（r >= 0）", res_prop_ok["passed"]
              and any(ch["criterion"] == "property" and ch["ok"] for ch in res_prop_ok["checks"]))

        res_prop_bad = oracle.evaluate(ws, {"module": "m",
            "properties": [{"func": "abs_sum", "samples": [[3, -4]], "expect": "r > 100"}]}, "S")
        check("性质测试失败（r > 100 不成立 → 硬红）",
              res_prop_bad["passed"] is False
              and any("property" in f for f in res_prop_bad["failed"]))

        # 3) 复杂度预算：按级别默认门控（显式开关可覆盖）
        res_cx_m = oracle.evaluate(ws, {"module": "m", "path": "m.py"}, "M")  # M 默认运行
        cx_fail_m = any(ch["criterion"] == "complexity" and not ch["ok"]
                        for ch in res_cx_m["checks"])
        check("M 级默认运行并拦下 very_complex", res_cx_m["passed"] is False and cx_fail_m)

        res_cx_s = oracle.evaluate(ws, {"module": "m", "path": "m.py"}, "S")  # S 默认跳过
        check("S 级默认跳过复杂度（不误杀）",
              not any(ch["criterion"] == "complexity" for ch in res_cx_s["checks"]))

        res_cx_force = oracle.evaluate(ws, {"module": "m", "path": "m.py",
                                            "complexity_check": True}, "S")  # 显式强制
        check("显式 complexity_check=True 在 S 级也运行",
              any(ch["criterion"] == "complexity" for ch in res_cx_force["checks"]))

        # 4) 级别门控 + 变异/e2e 实测不达标 → 硬红（用假引擎）
        orig_m, orig_e = _patch()
        try:
            res_mut = oracle.evaluate(ws, {"module": "m", "mutation_check": True}, "L")
            check("L 级变异杀伤率不达标 → 硬红",
                  res_mut["passed"] is False and any("mutation" in f for f in res_mut["failed"]))
            res_e2e = oracle.evaluate(ws, {"module": "m", "e2e_check": True}, "M")
            check("M 级 e2e 业务流失败 → 硬红",
                  res_e2e["passed"] is False and any("e2e" in f for f in res_e2e["failed"]))
        finally:
            import core.oracle as O
            from core import arch, e2e
            arch.gate_mutation_changed = orig_m
            e2e.write = orig_e

        # 5) 接入实际流程的接缝（不是只有函数）
        import inspect as _insp
        sig_c = _insp.signature(company._author_round)
        check("company._author_round 接入 oracle_specs 接缝",
              "oracle_specs" in sig_c.parameters)
        sig_i = _insp.signature(iterate.inspect)
        check("iterate.inspect 接入 specs 接缝", "specs" in sig_i.parameters)

    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（计算校验 / 性质测试 / 复杂度预算 / 变异·e2e 硬判据 / 级别门控 / 流程接缝）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
