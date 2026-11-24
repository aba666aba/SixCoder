"""共性缺陷守卫：机制/接口演进后"没人核查"这一整类问题的机检。

为什么要有这条（2026-11-12 用户纠正的元教训）：
    那天修了 4 个"机制写好了但从没触发"的缺陷 —— 业务流算在轮循环外、
    `_role_map` 返回值变了调用方没跟上、全自主线绕过接线守卫、
    残留引用了已删除的异常类。四个表象不同，但根因同源：
    **接口/机制变了，没人核查所有调用方与挂载点**。
    逐个修治标 —— 必须有一道**机检**把这一类一次性兜住。

本守卫覆盖三个维度（都是**可机判**的，不需要人肉读代码）：

  A. **残留引用**：except / raise 引用 `<模块>.<属性>`，但那个属性在模块里不存在。
     为什么单列：既有自检 `check_self_undef.py` 只查**未定义的名字**（Name），
     不查**属性**（module.Attr）→ "except model_policy.ModelTooSmall" 这类
     （类被删了、except 还在）会**静默漏网**。2026-11-12 实测就是靠这条抓到的。

  B. **死机制**：core 的公开函数，全仓找不到调用点 = "定义了从没被走过"。
     这是"机制写好了但从没触发"的第一形态（`_role_map` 那类接口漂移、
     `author_gone_restored` 那类配套函数漏接线的近亲）。
     口径：只报**确定的**（有明确的同名调用会被误判的排除规则），
     报出来的每一条都必须**人工判定**：死代码（可删）/ 本该接线却漏了（严重）。

  C. **两线挂载差异**：甲方线 company.py 与全自主线 auto.py 各自 import 了哪些
     core 模块，差集 = "只挂了一边"的机制候选。
     注意：auto.py 内层会调 company.night() → 甲方线的机制**通过内层复用**生效，
     所以差集**不等于**缺口 —— 本守卫只负责**列出候选**并标注需人工判定。

跑法: python checks/check_mechanism_wiring.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
CHECKS = ROOT / "checks"
TOOLS = ROOT / "tools"
FAILS: list[str] = []

# 扫描范围：引擎 + 自检 + 工具 + CLI + 服务端（这些是真实调用方）
SCAN_ROOTS = [ROOT / "cli.py", ROOT / "server.py", ROOT / "iterate_cli.py",
              CORE, CHECKS, TOOLS]
SKIP_DIRS = {"__pycache__", ".tmp", ".tmp_iso2", ".tmp_iso3", ".tmp_isolated",
             ".tmp_djlibs", ".tmp_wsr", ".removed_20261110", "node_modules",
             "dist", "dist-installer", ".git", "_logs", "projects", "shell",
             "fixtures", "tokenizer"}


def _py_files() -> list[Path]:
    out: list[Path] = []
    for root in SCAN_ROOTS:
        if not root.exists():
            continue
        if root.is_file() and root.suffix == ".py":
            out.append(root)
            continue
        for p in root.rglob("*.py"):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            out.append(p)
    return sorted(set(out))


def _trees() -> list[tuple[Path, ast.AST]]:
    out = []
    for p in _py_files():
        try:
            out.append((p, ast.parse(p.read_text(encoding="utf-8", errors="replace"))))
        except (SyntaxError, OSError):
            continue
    return out


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def core_top_names() -> dict[str, set[str]]:
    """每个 core 模块的顶层名字（类/函数/常量/import 别名）。"""
    out: dict[str, set[str]] = {}
    for p in sorted(CORE.glob("*.py")):
        if p.name == "__init__.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        names: set[str] = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for a in node.names:
                    names.add(a.asname or a.name.split(".")[0])
        out[p.stem] = names
    return out


def main() -> int:
    trees = _trees()
    top = core_top_names()

    print("[A] 残留引用：except/raise 引用了不存在的属性")
    bad: list[str] = []
    for p, tree in trees:
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                targets.append(node.type)
            elif isinstance(node, ast.Raise) and node.exc is not None:
                targets.append(node.exc)
            for t in targets:
                if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                        and t.value.id in top):
                    mod, name = t.value.id, t.attr
                    if name not in top[mod]:
                        rel = p.relative_to(ROOT)
                        bad.append(f"{rel}:{node.lineno} {mod}.{name}")
    check("没有 except/raise 引用不存在的属性", not bad,
          "残留：" + "；".join(bad[:6]))
    for b in bad[:10]:
        print(f"      残留引用：{b}")

    print()
    print("[B] 死机制：core 公开函数在全仓找不到调用点")
    called: set[str] = set()
    imported: set[str] = set()
    for p, tree in trees:
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    called.add(f.attr)
                    if isinstance(f.value, ast.Name):
                        imported.add(f.value.id)
            elif isinstance(node, ast.Attribute):
                called.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported.add(a.asname or a.name.split(".")[0])
    dead: list[str] = []
    for p in sorted(CORE.glob("*.py")):
        if p.name == "__init__.py":
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            if node.name in called:
                continue
            # 被 `模块.函数()` 调用：`模块名` 出现在 imported 里 → 算有调用
            if p.stem in imported:
                continue
            dead.append(f"{p.name}:{node.lineno} {node.name}")
    # 已知并已判定的死代码（人工判定结论写在这里，避免每次重复报）
    KNOWN_DEAD = {
        "ask.py:list_pending", "ask.py:plan_review",
        "model.py:check_transition", "oscillation.py:as_json",
    }
    real_dead = [d for d in dead
                 if not any(d.endswith(k.split(":")[-1]) and k.split(":")[0] in d
                            for k in KNOWN_DEAD)]
    check(f"没有未判定的死机制（已知死代码 {len(KNOWN_DEAD)} 个已登记）",
          not real_dead, "新增：" + "；".join(real_dead[:8]))
    for d in real_dead:
        print(f"      未判定死机制：{d}")
    if real_dead:
        print("      → 每一条都必须人工判定：死代码（可删）/ 本该接线却漏了（严重缺陷）")

    print()
    print("[C] 两线挂载差异（只报候选，需人工判定 —— 内层复用会覆盖一部分）")
    mounts: dict[str, set[str]] = {}
    for fname in ("company.py", "auto.py"):
        p = CORE / fname
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            mounts[fname] = set()
            continue
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and node.module:
                    mods.add(node.module.split(".")[0])
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[-1])
        mounts[fname] = mods
    comp = mounts.get("company.py", set())
    auto = mounts.get("auto.py", set())
    # auto.py 内层调 company.night() → 甲方线机制经内层复用对全自主线生效
    inner_reuse = "company" in auto
    only_comp = sorted(m for m in (comp - auto) if m in top)
    check("甲方线独挂模块已全部登记为候选（不是缺口判定，需人工确认）",
          True, f"候选 {len(only_comp)} 个")
    print(f"      auto.py 内层复用 company.night：{inner_reuse} → 甲方线机制经内层生效")
    print(f"      甲方线独挂候选（{len(only_comp)} 个，需人工判定是否真缺口）：")
    print("        " + "、".join(only_comp))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（无残留引用 / 无未判定死机制 / 两线挂载差异已列候选）")
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
