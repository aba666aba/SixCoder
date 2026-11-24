"""补齐的分级规则自检：升级触发 / 改动规模 / 健康度漂移 / 治理锁定 / 领域指南。

这五项都是"低风险优先"那一批：**只报告、不阻断**（除了 XL 级的治理不可变）。
所以自检的重点是"该报的报了、不该报的别乱报"。

跑法: python check_tier4.py
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
from core import arch, governance, scale  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _mk(ws: Path, rel: str, text: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main() -> int:
    print("分级规则补齐自检")

    print("[1] 升级触发：该提示的提示，不该提示的闭嘴")
    small = {"prod_lines": 5000, "test_lines": 1000, "modules": 2}
    big = {"prod_lines": 120_000, "test_lines": 0, "modules": 3}
    many_mod = {"prod_lines": 5000, "test_lines": 0, "modules": 9}
    m_big = {"prod_lines": 600_000, "test_lines": 0, "modules": 3}
    xl_big = {"prod_lines": 1_200_000, "test_lines": 0, "modules": 3}
    check("S 级小仓库 → 不提示升级", not scale.upgrade_hint(small, "S")["should_upgrade"])
    u = scale.upgrade_hint(big, "S")
    check("S 级 >10 万行 → 提示升 M", u["should_upgrade"] and u["to"] == "M", str(u))
    u2 = scale.upgrade_hint(many_mod, "S")
    check("S 级模块 >8 个 → 提示升 M", u2["should_upgrade"], str(u2))
    check("M 级 >50 万行 → 提示升 L", scale.upgrade_hint(m_big, "M")["to"] == "L")
    check("L 级 >100 万行 → 提示升 XL", scale.upgrade_hint(xl_big, "L")["to"] == "XL")
    check("XL 级不再往上提示", not scale.upgrade_hint(xl_big, "XL")["should_upgrade"])
    check("提示里写清依据", "行" in u["note"] or "模块" in u["note"], u["note"])

    print("[2] 改动规模：算得出就报，算不出就说算不出（不假装）")
    with _sandbox_temp() as td:
        ws = Path(td)
        d = arch.diff_size(ws)
        check("不是它自己的 git 仓库 → 如实说算不出（不借用外层 diff）",
              (not d.get("ok")) and ("git" in str(d.get("reason"))
                                     or "仓库" in str(d.get("reason"))),
              str(d)[:120])
        g = arch.gate_diff_size(ws, "M")
        check("算不出时 → 未判定，不判通过也不判失败", g.evidence_ref == "unavailable",
              g.evidence_ref)
        gs = arch.gate_diff_size(ws, "S")
        check("S 级不设软上限", gs.evidence_ref == "skipped", gs.evidence_ref)
        check("M 级的软上限是 500", scale.profile("M").flags.get("diff_soft_cap") == 500)

    print("[3] 健康度漂移：有基线才算 delta，退化要点名")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "a.py", "def f():\n    return 1\n")
        _mk(ws, "e.py", "def g():\n    return 2\n")     # 基线要有 ≥2 个模块才判漂移
        first = arch.health_drift(ws)
        check("没有基线 → 不判漂移", not first["has_baseline"], str(first.get("note"))[:60])
        arch.save_health(ws, arch.health_metrics(ws))
        # 造一个"变差"：加环 + 加深依赖
        _mk(ws, "b.py", "import c\n")
        _mk(ws, "c.py", "import a\n")
        _mk(ws, "d.py", "import b\n")
        dr = arch.health_drift(ws)
        check("有基线 → 算得出 delta", dr.get("has_baseline"), str(dr.get("note"))[:60])
        check("能指出退化（环/深度/依赖边）", dr.get("regressed") and dr.get("worse"),
              str(dr.get("worse")))
        arch.save_health(ws, dr["now"])
        dr2 = arch.health_drift(ws)
        check("没变化时不乱报退化", not dr2.get("regressed"), str(dr2.get("note"))[:60])
        single = ws / "solo"
        single.mkdir()
        _mk(single, "x.py", "def h():\n    return 3\n")
        check("空/单模块仓库不判漂移（从 0 长起来不算退化）",
              not arch.health_drift(single).get("has_baseline"))

    print("[4] 治理文件指纹：被改就能发现")
    with _sandbox_temp() as td:
        ws = Path(td)
        governance.write_governance(ws, "M", {"pkg/a.py": None})
        lock = governance.lock_governance(ws)
        check("锁定成功并有指纹", lock["ok"] and len(lock["files"]) >= 1, str(lock)[:100])
        v = governance.verify_governance(ws, "XL")
        check("没改过 → 通过", v["ok"] and not v.get("changed"), str(v)[:100])
        (ws / "AGENTS.md").write_text("# 被偷偷改了\n", encoding="utf-8")
        v2 = governance.verify_governance(ws, "XL")
        check("XL 级被改 → 红", (not v2["ok"]) and v2["changed"], str(v2.get("note"))[:80])
        v3 = governance.verify_governance(ws, "M")
        check("M 级只报告不阻断", v3["ok"] and "只报告" in v3["note"], v3["note"][:80])
        (ws / "AGENTS.md").unlink()
        v4 = governance.verify_governance(ws, "XL")
        check("被删也算改动", any("被删" in x for x in (v4.get("changed") or [])), str(v4)[:100])

    print("[5] 领域指南：M+ 才生成，且 ≤300 行")
    with _sandbox_temp() as td:
        ws = Path(td)

        class _F:
            name = "clean_row"
            params: list = []
            returns = "dict"
            doc = "清洗整行"

        class _C:
            funcs = [_F()]

        s = governance.write_guides(ws, "S", {"clean.py": _C()})
        check("S 级不建 docs 知识库", s.get("skipped"), str(s)[:80])
        m = governance.write_guides(ws, "M", {"clean.py": _C()})
        check("M 级生成了指南", bool(m.get("wrote")), str(m)[:120])
        check("指南内容含契约约束", any("clean_row" in Path(x).read_text(encoding="utf-8")
                                        for x in (m.get("wrote") or [])))
        check("没有超 300 行", not m.get("over_limit"), str(m.get("over_limit")))
        # 造一个超长指南 → 必须报
        f = Path(m["wrote"][0])
        f.write_text("\n".join(["x"] * 400), encoding="utf-8")
        m2 = governance.write_guides(ws, "M", {"clean.py": _C()})
        check("指南超 300 行 → 报出来", bool(m2.get("over_limit")), str(m2.get("over_limit")))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（升级触发 / 改动规模 / 健康度漂移 / 治理锁定 / 领域指南）")
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
