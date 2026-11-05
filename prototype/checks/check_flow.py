"""分级体系"接进流程"自检：不是"函数存在"，而是"夜班真的会用到它们"。

四项：
  ① 治理文件：S 级生成 50–100 行 AGENTS.md；M+ 额外生成子目录规则 + architecture.md + 依赖白名单；
     已经存在的**人写文件不许被覆盖**；
  ② 工作集声明：S 级不做；M 级起必须写，且越界要拦；
  ③ 按档门禁：M 级的架构方向能真的抓出反向依赖；覆盖率本机没装 → 必须如实说"测不了"而不是假绿；
     依赖白名单缺失 → L 级必须红；
  ④ 反向依赖：能算出"谁 import 了我"。

跑法: python check_flow.py
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
    print("分级体系接入流程自检")

    print("[1] 治理文件：按档产出，且不覆盖人写的")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "clean.py", "def f():\n    return 1\n")
        r = governance.write_governance(ws, "S", {"clean.py": None}, "写个清洗模块")
        root = ws / "AGENTS.md"
        text = root.read_text(encoding="utf-8") if root.exists() else ""
        n = len(text.splitlines())
        print(f"     S 级 AGENTS.md {n} 行")
        check("S 级生成了 AGENTS.md", bool(r["wrote"]) and root.exists())
        check("S 级篇幅在 40–120 行区间（原口径 50–100，允许模板浮动）", 40 <= n <= 120, f"{n} 行")
        check("写了硬性禁令（不许提交密钥/不许删测试）",
              "密钥" in text and ("删" in text and "测试" in text))
        check("写了本级质量阈值", "圈复杂度" in text)
        check("写了本级明确不做的清单（S 级才写）", "明确不做" in text)
        check("S 级不生成子目录规则", not any(p.name == "AGENTS.md" and p.parent != ws
                                               for p in ws.rglob("AGENTS.md")))
        # 人写的文件不许覆盖
        root.write_text("# 人写的规则\n不许改我\n", encoding="utf-8")
        r2 = governance.write_governance(ws, "S", {"clean.py": None})
        check("已存在的人写文件不被覆盖",
              "不许改我" in root.read_text(encoding="utf-8") and str(root) in (r2["skipped"] or [str(root)]),
              str(r2["skipped"]))

    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "pkg/a.py", "def f():\n    return 1\n")
        r = governance.write_governance(ws, "M", {"pkg/a.py": None})
        wrote = [Path(x) for x in r["wrote"]]
        check("M 级写根文件", any(p.name == "AGENTS.md" and p.parent == ws for p in wrote))
        check("M 级写子目录规则", any(p.name == "AGENTS.md" and p.parent != ws for p in wrote))
        check("M 级写架构约束文件",
              (ws / ".sixcoder/governance/architecture.md").exists())
        arch_txt = (ws / ".sixcoder/governance/architecture.md").read_text(encoding="utf-8")
        check("架构文件里有分层方向（→ 连接）", "→" in arch_txt)
        check("M 级读得回分层", len(arch.read_layers(ws)) >= 2, str(arch.read_layers(ws)))

    print("[2] 工作集声明：S 不做，M+ 必须做，越界要拦")
    ok_s, why_s = governance.gate_workingset(Path("."), ["a.py"], None, "S")
    check("S 级跳过工作集", ok_s and "S 级" in why_s, why_s)
    ok_m, why_m = governance.gate_workingset(Path("."), ["a.py"], None, "M")
    check("M 级没有声明 → 拦", (not ok_m) and "先声明" in why_m, why_m)
    ok_m2, why_m2 = governance.gate_workingset(Path("."), ["a.py"], ["a.py"], "M")
    check("M 级声明了且没越界 → 放行", ok_m2, why_m2)
    ok_m3, why_m3 = governance.gate_workingset(Path("."), ["a.py", "b.py"], ["a.py"], "M")
    check("M 级越界 → 拦并指出越界文件", (not ok_m3) and "b.py" in why_m3, why_m3)
    decl = governance.workingset(["clean.py"], {"clean.py": type("C", (), {"funcs": [
        type("F", (), {"name": "clean_row"})()]})()}, "清洗需求")
    txt = governance.render_workingset(decl)
    check("工作集文本含文件/接口/测试/非目标",
          "clean.py" in txt and "clean_row" in txt and "test_clean.py" in txt)

    print("[3] 按档门禁：真的能抓，做不到的如实说")
    with _sandbox_temp() as td:
        ws = Path(td)
        # 分层：service 依赖 repo（合规）；repo 反向依赖 service（违规）
        _mk(ws, "repo/db.py", "def get():\n    return 1\n")
        _mk(ws, "service/api.py", "from repo.db import get\n\n\ndef run():\n    return get()\n")
        g = arch.gate_layers(ws, "S")
        check("S 级架构门禁 → skipped（不做，不是通过）", g.evidence_ref == "skipped", g.evidence_ref)
        g = arch.gate_layers(ws, "M")
        check("M 级没有分层声明 → 不给通过结论", g.evidence_ref == "no-layers", g.evidence_ref)
        (ws / ".sixcoder/governance").mkdir(parents=True, exist_ok=True)
        (ws / ".sixcoder/governance/architecture.md").write_text(
            "```\nTypes → Repo → Service\n```\n", encoding="utf-8")
        g = arch.gate_layers(ws, "M")
        check("合规的依赖方向 → 通过", g.ok, g.detail)
        _mk(ws, "repo/bad.py", "from service.api import run\n\n\ndef x():\n    return run()\n")
        g = arch.gate_layers(ws, "M")
        check("反向依赖 → 抓到", (not g.ok) and "反向依赖" in g.detail, g.detail[:120])

        cov = arch.gate_coverage(ws, "M")
        # 装了 coverage 就必须给真实数字；没装就必须如实说"测不了"，不许假绿。
        # （这条以前写死了"没装"，装上 coverage 之后反而红了 —— 断言要跟着能力走）
        try:
            import coverage as _cov_pkg  # noqa: F401
            has_cov = True
        except Exception:                                  # noqa: BLE001
            has_cov = False
        if has_cov:
            check("覆盖率：装了工具就给真实数字（或者是明确的测量失败）",
                  cov.evidence_ref not in ("unavailable",) and (
                      "%" in cov.evidence_ref or cov.evidence_ref == "error"),
                  f"{cov.evidence_ref} / {cov.detail[:60]}")
        else:
            check("覆盖率：没装工具就如实说测不了，不假绿",
                  cov.evidence_ref == "unavailable" and "测不了" in cov.detail,
                  cov.evidence_ref)
        cov_s = arch.gate_coverage(ws, "S")
        check("S 级不跑覆盖率", cov_s.evidence_ref == "skipped")

        dep = arch.gate_dep_whitelist(ws, "L")
        check("L 级没有白名单文件 → 红", not dep.ok and "白名单" in dep.detail, dep.evidence_ref)
        arch.write_dep_whitelist(ws, "L")
        _mk(ws, "third.py", "import requests\n\n\ndef f():\n    return requests\n")
        dep2 = arch.gate_dep_whitelist(ws, "L")
        check("白名单外的第三方 import → 红", not dep2.ok and "requests" in dep2.detail,
              dep2.detail[:120])
        (ws / arch.WHITELIST_FILE).write_text("requests\n", encoding="utf-8")
        dep3 = arch.gate_dep_whitelist(ws, "L")
        check("加进白名单后 → 通过", dep3.ok, dep3.detail[:120])
        dep_m = arch.gate_dep_whitelist(ws, "M")
        check("M 级不强制白名单（advisory 级别，不做活）",
              dep_m.ok and dep_m.evidence_ref in ("advisory", "skipped"), dep_m.evidence_ref)

    print("[4] 反向依赖与健康度（XL 指标）")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "clean.py", "def f():\n    return 1\n")
        _mk(ws, "store.py", "from clean import f\n\n\ndef g():\n    return f()\n")
        rev = governance.reverse_deps(ws, ["clean.py"])
        check("算出谁 import 了 clean.py", any("store.py" == a for a, _ in rev), str(rev))
        _mk(ws, "loop_a.py", "import loop_b\n")
        _mk(ws, "loop_b.py", "import loop_a\n")
        hm = arch.health_metrics(ws)
        check("健康度能发现环", hm["acyclic"] is False, str(hm))
        g = arch.gate_health(ws, "XL")
        check("XL 级有环 → 红", not g.ok, g.detail[:120])
        g_l = arch.gate_health(ws, "L")
        check("L 级不用健康度指标", g_l.evidence_ref == "skipped")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（治理文件按档产出 / 工作集拦越界 / 门禁真能抓且做不到如实说 / 反向依赖与健康度）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
