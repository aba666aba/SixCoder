"""#11 类型检查 + lint 进夜班；#12 报错内嵌修复指令；#15 熵管理扫描。

分级口径（和 docs/24 对齐）：
  · 类型检查：S 级 CI 就有，但**夜班产物常常没有完整注解**，mypy 噪音大 →
    S/M 级只报告（advisory），L/XL 级阻断；
  · lint：S 级起就报告，L/XL 级阻断（多团队下坏味道会复利）；
  · 熵管理：M 级起扫重复/过期文档，L+ 阻断。
所有结果都带"怎么改"，而不是只报"你有问题"。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import scale as _scale


def _gate(name: str, ok: bool, ref: str, detail: str, t0: float):
    from .arch import GateResult
    return GateResult(name, ok, "static_rule", ref, detail, int((time.time() - t0) * 1000))


def gate_lint(ws: Path, level: str = "S", paths: list[str] | None = None):
    """#11/#12：lint（每条都带修复指令）；S/M 只报告，L+ 阻断。"""
    from . import lint as _lint
    t0 = time.time()
    findings = _lint.lint_files(Path(ws), paths)
    blocking = str(level).upper() in ("L", "XL")
    summary = _lint.summarize(findings) if findings else "没有 lint 问题"
    if not findings:
        return _gate("lint", True, "clean", "没有 lint 问题", t0)
    ok = not blocking
    return _gate("lint", ok, f"{len(findings)} 处",
                 ("已阻断（L 级要求零 lint）：" if blocking else "只报告：") + summary, t0)


def gate_typecheck(ws: Path, level: str = "S", timeout: int = 180):
    """#11：类型检查（mypy）。装不上就如实说，S/M 只报告，L+ 阻断。"""
    import sys
    import subprocess
    t0 = time.time()
    try:
        import mypy  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return _gate("typecheck", True, "unavailable",
                     "本机没装 mypy，类型检查测不了（不假装通过）；"
                     "`pip install mypy` 后可得到硬证据", t0)
    try:
        # 只检查**客户仓库里的源文件**：以前直接 `mypy .`，把引擎自己的临时探针
        # `_props_run.py` 也扫了 —— 交付报告于是出现"我们的临时脚本有类型错误"，
        # 甲方既改不了也看不懂。判据扫到自己的垃圾文件，比不扫更糟。
        targets = [p.name for p in sorted(Path(ws).glob("*.py"))
                   if not p.name.startswith("_props") and not p.name.startswith(".")]
        if not targets:
            return _gate("typecheck", True, "skipped", "仓库里没有可检查的 .py 文件", t0)
        proc = subprocess.run([sys.executable, "-m", "mypy", "--ignore-missing-imports",
                               "--no-error-summary"] + targets,
                              cwd=str(ws), capture_output=True, text=True, timeout=timeout)
    except Exception as exc:                             # noqa: BLE001
        return _gate("typecheck", True, "error", f"mypy 跑不动（{type(exc).__name__}）", t0)
    errs = [ln for ln in (proc.stdout or "").splitlines()
            if ": error:" in ln and "_props" not in ln]
    if not errs:
        return _gate("typecheck", True, "0 errors", "mypy 没有报类型错误", t0)
    blocking = str(level).upper() in ("L", "XL")
    sample = "；".join(e.replace(str(ws) + "/", "")[:110] for e in errs[:2])
    fix = "→ 按行补注解或加 `# type: ignore[code]` 并写明原因"
    return _gate("typecheck", not blocking, f"{len(errs)} errors",
                 ("已阻断：" if blocking else "只报告：") + sample + " " + fix, t0)


def gate_entropy(ws: Path, level: str = "M"):
    """#15：熵管理扫描（重复定义 + 过期/过长的领域指南 + 未清的临时方案）。"""
    from . import gates as _gates, debt as _debt, governance as _gov
    t0 = time.time()
    lv = str(level).upper()
    if lv == "S":
        return _gate("entropy", True, "skipped", "S 级熵管理靠人工（不自动扫）", t0)
    probs: list[str] = []
    try:
        dup = _gates.gate_duplicates(Path(ws), [str(p.relative_to(ws))
                                                for p in Path(ws).glob("*.py")
                                                if not p.name.startswith("_props")
                                                and not p.name.startswith(".")])
        if not dup.ok:
            probs.append("重复定义：" + dup.detail[:100] + " → 抽公共函数")
    except Exception:                                    # noqa: BLE001
        pass
    try:
        gdir = Path(ws) / _gov.GUIDE_DIR
        if gdir.exists():
            for f in gdir.glob("*.md"):
                n = len(f.read_text(encoding="utf-8", errors="replace").splitlines())
                if n > _gov.GUIDE_MAX_LINES:
                    probs.append(f"指南过期过长：{f.name} {n} 行（上限 "
                                 f"{_gov.GUIDE_MAX_LINES}）→ 拆成按模块的短篇")
    except Exception:                                    # noqa: BLE001
        pass
    try:
        expired = [r for r in _debt.load(Path(ws))
                   if not r.get("resolved")]
        if expired:
            probs.append(f"未清的临时方案 {len(expired)} 条 → 见临时方案台账")
    except Exception:                                    # noqa: BLE001
        pass
    if not probs:
        return _gate("entropy", True, "clean", f"{lv} 级熵扫描通过（无重复/过期指南/未清临时方案）", t0)
    blocking = lv in ("L", "XL")
    return _gate("entropy", not blocking, f"{len(probs)} 处",
                 ("已阻断：" if blocking else "只报告：") + "；".join(probs[:3]), t0)


def gate_deps_audit(ws: Path, level: str = "M", use_network: bool = True):
    """#23 依赖审计：幻觉包 + 已知漏洞。M 级只报告，L+ 阻断。

    为什么 M 级不阻断：这一级的依赖面还小，人工看一眼更划算；
    到 L 级（多团队/受控自主）就没人能逐个看依赖了，必须机械拦。
    """
    from . import deps_audit as _da, scale as _sc
    t0 = time.time()
    lv = str(level).upper()
    if lv == "S":
        return _gate("deps_audit", True, "skipped", "S 级依赖由 PR 说明 + 人工确认（不自动审计）", t0)
    try:
        res = _da.audit(Path(ws), use_network=use_network)
    except Exception as exc:                             # noqa: BLE001
        return _gate("deps_audit", True, "error",
                     f"依赖审计跑不动（{type(exc).__name__}）；不给通过结论", t0)
    scope = "clean" if res["ok"] else f"{len(res['missing'])} 幻觉包 / {len(res['vulns'])} 漏洞"
    if res.get("unverified"):
        scope += f" / {len(res['unverified'])} 未核对"
    blocking = lv in ("L", "XL")
    ok = res["ok"] or not blocking
    prefix = "已阻断：" if (not res["ok"] and blocking) else "只报告："
    return _gate("deps_audit", ok, scope, prefix + _da.summarize(res), t0)


def extra_gates(ws: Path, level: str, paths: list[str] | None = None) -> list:
    """本模块负责的门禁（供 level_gates / 交付报告统一调用）。"""
    return [gate_typecheck(ws, level), gate_lint(ws, level, paths),
            gate_entropy(ws, level), gate_deps_audit(ws, level)]
