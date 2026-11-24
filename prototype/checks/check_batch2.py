"""第二批分级规则自检：typecheck/lint/熵管理/聚合指标/即时反馈/单主题/计划门禁。

跑法: python check_batch2.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import lint, quality, scale  # noqa: E402

FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


BAD = '''import os
import json


def f(x=[]):
    try:
        return x
    except:
        pass


def g():
    assert 1 == 1
    print("debug")
    # TODO 先这样
'''


def main() -> int:
    print("第二批分级规则自检")

    print("[1] lint：抓得到，且每条都带修复指令（L 级要求报错内嵌修复）")
    fs = lint.lint_source(BAD, "bad.py")
    rules = {f["rule"] for f in fs}
    check("裸 except 被抓", "bare_except" in rules, str(rules))
    check("吞异常被抓", "swallow" in rules, str(rules))
    check("可变默认参数被抓", "mutable_default" in rules, str(rules))
    check("未使用导入被抓", "unused_import" in rules, str(rules))
    check("运行时 assert 被抓", "assert_runtime" in rules, str(rules))
    check("print 当日志被抓", "print_debug" in rules, str(rules))
    check("临时标记被抓（与债务台账联动）", "todo_marker" in rules, str(rules))
    check("每条都有 fix", all(f.get("fix") for f in fs), str(fs[:1]))
    check("摘要里包含修复写法", "改成" in lint.summarize(fs), lint.summarize(fs))
    clean = "def h(a: int) -> int:\n    return a + 1\n"
    check("干净代码不误报", not lint.lint_source(clean, "ok.py"), str(lint.lint_source(clean)))

    print("[2] lint 门禁按档：S/M 只报告，L+ 阻断")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "bad.py").write_text(BAD, encoding="utf-8")
        g_s = quality.gate_lint(ws, "S")
        g_m = quality.gate_lint(ws, "M")
        g_l = quality.gate_lint(ws, "L")
        check("S 级只报告（ok=True 但要写明有几处）", g_s.ok and "处" in g_s.evidence_ref,
              g_s.evidence_ref)
        check("M 级只报告", g_m.ok, g_m.evidence_ref)
        check("L 级阻断", not g_l.ok, g_l.evidence_ref)
        check("阻断时说清怎么改", "改成" in g_l.detail or "→" in g_l.detail, g_l.detail[:80])
        (ws / "bad.py").unlink()
        (ws / "ok.py").write_text("def h(a: int) -> int:\n    return a + 1\n", encoding="utf-8")
        check("干净工作区 → 通过", quality.gate_lint(ws, "L").ok)

    print("[3] 类型检查：装不上就如实说，装上了按档处理")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "x.py").write_text("def f(a: int) -> int:\n    return a\n", encoding="utf-8")
        g = quality.gate_typecheck(ws, "M", timeout=60)
        try:
            import mypy  # noqa: F401
            has_mypy = True
        except Exception:                                # noqa: BLE001
            has_mypy = False
        if has_mypy:
            check("装了 mypy → 有实测证据（0 errors 或列出错误）",
                  g.evidence_ref in ("0 errors",) or "errors" in g.evidence_ref, str(g.evidence_ref))
        else:
            check("没装 mypy → 如实说测不了，不假绿",
                  g.evidence_ref == "unavailable" and "测不了" in g.detail, g.detail[:80])

    print("[4] 熵管理：S 跳过；M 报告；L 阻断")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "dup.py").write_text("def f():\n    return 1\n\n\ndef f():\n    return 2\n",
                                   encoding="utf-8")
        check("S 级跳过", quality.gate_entropy(ws, "S").evidence_ref == "skipped")
        g_m = quality.gate_entropy(ws, "M")
        check("M 级有重复定义 → 报告", (not g_m.detail.startswith("M 级")) and "重复定义" in g_m.detail,
              g_m.detail[:80])
        check("M 级不阻断", g_m.ok, str(g_m.evidence_ref))
        g_l = quality.gate_entropy(ws, "L")
        check("L 级阻断", not g_l.ok, g_l.detail[:80])
        (ws / "dup.py").unlink()
        (ws / "ok.py").write_text("def g():\n    return 1\n", encoding="utf-8")
        check("干净 → 通过", quality.gate_entropy(ws, "L").ok)

    print("[5] 聚合指标：能从用量库与交付报告算出总账")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "metrics", Path(__file__).resolve().parents[1] / "tools" / "metrics.py")
    m = importlib.util.module_from_spec(spec)            # type: ignore
    spec.loader.exec_module(m)                           # type: ignore
    data = m.build(6)
    check("有 totals 且字段齐", all(k in data["totals"] for k in
                                  ("runs", "calls", "out_tokens", "cny", "cache_hit_rate")),
          str(data["totals"]))
    txt = m.render(data)
    check("渲染出总账与明细表", "聚合指标" in txt and "|" in txt)
    check("命中率在 0..1 之间", 0 <= data["totals"]["cache_hit_rate"] <= 1,
          str(data["totals"]["cache_hit_rate"]))

    print("[6] 调用链：三项都接进夜班")
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("M+ 一轮只改一个模块", 'if _lv16 in ("M", "L", "XL"):' in comp and "max_modules = 1" in comp)
    check("XL 即时反馈", "instant_health" in comp)
    check("造物模式计划门禁", "SIXCODER_CLIENT_PLAN_GATE" in comp and "plan_gate" in comp)
    arch = (Path(__file__).resolve().parents[1] / "core" / "arch.py").read_text(encoding="utf-8")
    check("level_gates 带上第二批门禁", "quality as _q" in arch and "extra_gates" in arch)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（lint 带修复指令 / 类型检查如实 / 熵管理分档 / 聚合指标 / 三项接进夜班）")
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
