"""代码规模分级自检：约束随规模变，且"知道什么时候不加什么"。

分级不是审美问题，是成本问题：**同一套门禁在小项目上过度工程化、在大项目上等于没有约束。**
这一档钉住四件事：
  ① 定级：需求里写明的优先；没写就按实测推断，且**必须给出警告**；
  ② 开关：S 级不跑架构测试/覆盖率/变异；M 级加架构+覆盖率；L 级再加变更行变异+受保护区；
  ③ 诚实：每一级都要能列出"本级明确不做的事"和理由；
  ④ 交付报告里写清楚"按哪级跑的、依据是什么、跑了什么、没跑什么"。

跑法: python check_scale.py
"""
from __future__ import annotations


def _sandbox_temp():
    """临时目录放进工作区（沙箱只允许写工作区内的路径）。"""
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import scale  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("代码规模分级自检")

    print("[1] 定级：声明优先，没声明就推断且必须警告")
    req_s = "写个清洗模块。\n代码规模：S\n"
    req_m = "1) store.py：建表。\n代码规模：M 级（10–50 万行）\n"
    req_x = "@scale XL\n要一个超大规模系统\n"
    r1 = scale.resolve(req_s, None)
    r2 = scale.resolve(req_m, None)
    r3 = scale.resolve(req_x, None)
    r4 = scale.resolve("没有任何规模说明的需求", None)
    check("需求里写 S → 定 S", r1["level"] == "S" and r1["source"] == "requirement", str(r1["level"]))
    check("需求里写 M 级（带括号说明）→ 定 M", r2["level"] == "M", str(r2["level"]))
    check("@scale XL 这种写法也认", r3["level"] == "XL", str(r3["level"]))
    check("没写 → 不报错、能定级", r4["level"] in scale.TIERS, str(r4["level"]))
    check("没写 → 必须给警告（不许悄悄选一个）", bool(r4["warning"]), str(r4.get("warning"))[:80])
    check("写明了 → 不该有警告", not r1["warning"])

    print("[2] 实测推断：按行数落在正确的级")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "big.py").write_text("\n".join(["x = 1"] * 5000) + "\n", encoding="utf-8")
        (ws / "test_big.py").write_text("\n".join(["assert 1"] * 1000) + "\n", encoding="utf-8")
        c = scale.count_lines(ws)
        check("生产/测试分别计数", c["prod_lines"] == 5000 and c["test_lines"] == 1000, str(c))
        check("小仓库 → S 级", scale.from_lines(c["prod_lines"] + c["test_lines"]) == "S")
        check("平铺文件 → 1 个模块（去掉 test_ 与 __init__）",
              c["modules"] == 1, str(c))
    # 包结构：一个非 __init__ 文件 = 一个模块。旧实现数的是目录数，
    # 于是 `pkg/sub/mod.py` 这类包内模块**一个都不算**（实测 90 文件/80 模块被数成 10）。
    with _sandbox_temp() as td:
        ws = Path(td)
        for pkg in ("a", "b"):
            for sub in ("x", "y"):
                d = ws / pkg / sub
                d.mkdir(parents=True)
                (d / "__init__.py").write_text("", encoding="utf-8")
                (d / "m1.py").write_text("V = 1\n", encoding="utf-8")
                (d / "m2.py").write_text("V = 2\n", encoding="utf-8")
        cp = scale.count_lines(ws)
        check("包结构：8 个模块（不是 4 个目录、也不含 __init__）", cp["modules"] == 8, str(cp))
    check("10 万行 → M", scale.from_lines(100_000) == "M")
    check("50 万行 → L", scale.from_lines(500_000) == "L")
    check("100 万行 → XL", scale.from_lines(1_000_000) == "XL")

    print("[3] 约束开关：规模越小越少，且每级都有'明确不做'")
    for lv in scale.TIER_ORDER:
        p = scale.gate_plan(lv)
        print(f"     {lv}: 执行 {len(p['run'])} 项 / 不做 {len(p['skip'])} 项")
    s, m, l, x = (scale.gate_plan(lv) for lv in scale.TIER_ORDER)
    check("S 级不跑架构测试（成本高于收益）", "architecture" not in s["run"])
    check("S 级不跑变异测试", "mutation" not in s["run"])
    check("S 级不设覆盖率地板", "coverage" not in s["run"])
    check("M 级必须跑架构测试", "architecture" in m["run"])
    check("M 级设覆盖率地板 0.80", m["flags"].get("coverage_floor") == 0.80)
    check("M 级仍不跑变异测试（成本 > 收益）", "mutation" not in m["run"])
    check("L 级跑变更行变异测试", "mutation" in l["run"] and l["flags"].get("mutation_min") == 0.70)
    check("L 级开受保护区自动拒绝", "protected_zones" in l["run"])
    check("L 级开依赖白名单", "dep_whitelist" in l["run"])
    check("XL 级加持续健康度指标", "continuity_health" in x["run"])
    check("S 级的'不做清单'必须写理由",
          all(isinstance(n, str) and n for n in scale.SKIPPED["S"]) and len(scale.SKIPPED["S"]) >= 5)
    check("S/M/L 都至少列了 1 条'不做'（XL 没有更高的级可省，允许为空)",
          all(scale.SKIPPED[lv] for lv in ("S", "M", "L")))

    print("[4] 交付报告：写清按哪级跑、依据、跑什么/不跑什么")
    lines = scale.report_lines("M", {"source": "requirement", "measured":
                                     {"prod_lines": 1234, "test_lines": 567, "modules": 3},
                                     "warning": ""})
    text = "\n".join(lines)
    check("报告里有级别与小节标题", "M 级" in text and "规模与约束等级" in text)
    check("报告里写明定级依据", "依据" in text and "1234" in text)
    check("报告里列出执行的门禁", "✅ 执行" in text)
    check("报告里列出不做的门禁及理由", "⛔ 不执行" in text and "变异测试" in text)
    lines2 = scale.report_lines("S", {"source": "measured", "measured":
                                      {"prod_lines": 500, "test_lines": 100, "modules": 1},
                                      "warning": "需求里没写目标代码规模"})
    text2 = "\n".join(lines2)
    check("按实测定级时报告里出现警告", "需求里没写目标代码规模" in text2)
    check("S 级报告里明确写了不跑架构测试", "architecture" in text2 or "架构" in text2)

    # [11] 2026-11-09 修：甲方用**行数**声明规模（如「目标代码规模：30000 行」）
    #      原来只认字母档位 → 回落到"按仓库实测推断"，依据写错（实测是当前进度，
    #      不是目标规模）。现在认行数声明并按行数落档。
    check("认行数声明：目标代码规模 30000 行", scale.declared_from_requirement(
        "目标代码规模：30000 行") == "S")
    check("认行数声明：带 Markdown 加粗也认得", scale.declared_from_requirement(
        "目标代码规模：**30000 行**（10000–50000 行级别）") == "S")
    check("行数声明能取到具体行数（用于报告写依据）",
          scale._declared_lines("目标代码规模：**30000 行**（10000–50000 行级别）") == 30000)
    check("行数声明：50 万行 → L 级", scale.declared_from_requirement(
        "目标代码规模：500000 行") == "L")
    check("行数声明优先级：不误抓括号里的范围上界",
          scale._declared_lines("目标代码规模：**30000 行**（10000–50000 行级别）") == 30000)
    r = scale.resolve("目标代码规模：**30000 行**（10000–50000 行级别）", None)
    check("resolve 的依据是 requirement 不是 measured", r["source"] == "requirement")
    check("resolve 在声明了规模时不报警告", r["warning"] == "")
    check("没声明规模的仍走实测并保留警告",
          scale.resolve("随便什么需求", None)["source"] == "measured")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（定级/实测推断/分级开关/报告如实列出不做的事）")
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
