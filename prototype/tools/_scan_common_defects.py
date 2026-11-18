"""一次性排查：找"机制/接口演进后没人核查"这一类缺陷的**全貌**。

背景（2026-11-12）：修了 4 个缺陷 —— biz_flow 挂在收工段（对轮内无效）、
`str(v[0])` 元组字符串化（_role_map 返回值从 str 变 list[tuple] 后调用方没跟上）、
auto.py 直调 apply_web_wiring（共享机制挂甲方线、全自主线绕过）、
ModelTooSmall 未定义（硬拒口径撤销后残留引用没清）。
四个表象不同，但根因同源：**接口/机制变了，没人核查所有调用方与挂载点**。

本脚本做**全量扫描**，找出这一类里还剩多少：
  A. 属性级残留引用：except/raise 引用 `<模块>.<属性>`，但那个属性不存在
     （自检 check_self_undef 只查"名字"，不查"属性" → 这类漏网）
  B. 死机制：core 模块的公开函数，全仓找不到调用点（定义了从没被走过）
  C. 跨层挂载差异：甲方线(company.night) 与全自主线(auto.night_auto) 各自用到哪些
     core 模块 → 差集 = "只挂了一边"的机制

只读扫描，不改任何东西。
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
ALL_PY = sorted(ROOT.rglob("*.py"))
# 排除临时/归档/沙箱目录
SKIP = {".tmp", ".tmp_iso2", ".tmp_iso3", ".tmp_isolated", ".tmp_djlibs", ".tmp_wsr",
        "__pycache__", ".removed_20261110", "node_modules", "dist", "dist-installer",
        ".git", "_logs", "projects", "shell"}


def _skip(p: Path) -> bool:
    return any(part in SKIP for part in p.parts)


def core_files() -> list[Path]:
    return [p for p in CORE.glob("*.py") if p.name != "__init__.py"]


# ───────────────────────────────────────── A. 属性级残留引用
def scan_attr_refs():
    """扫 except/raise 后面的 `<模块>.<属性>`，核对该属性在目标模块里真实存在。"""
    # 收集每个 core 模块的顶层名字（类/函数/赋值/import）
    top_names: dict[str, set[str]] = {}
    for p in core_files():
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
        top_names[p.stem] = names

    hits = []
    for p in ALL_PY:
        if _skip(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            # except X.Y as e:  →  X 必须是已 import 的模块名，Y 必须在 X 里存在
            if isinstance(node, ast.ExceptHandler) and node.type is not None:
                for attr in _module_attrs(node.type):
                    mod, name = attr
                    if mod in top_names and name not in top_names[mod]:
                        hits.append((str(p.relative_to(ROOT)), node.lineno,
                                     f"except {mod}.{name} → {mod} 里没有 {name}"))
            # raise X.Y
            if isinstance(node, ast.Raise) and node.exc is not None:
                for attr in _module_attrs(node.exc):
                    mod, name = attr
                    if mod in top_names and name not in top_names[mod]:
                        hits.append((str(p.relative_to(ROOT)), node.lineno,
                                     f"raise {mod}.{name} → {mod} 里没有 {name}"))
    return hits


def _module_attrs(node: ast.AST) -> list[tuple[str, str]]:
    out = []
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        out.append((node.value.id, node.attr))
    return out


# ───────────────────────────────────────── B. 死机制
def scan_dead_functions():
    """core 的公开函数，全仓（含 checks/tools）找不到调用点 = 定义了从没被走过。

    误报修正（第一版踩到的坑）：
      · `import X` 之后用 `X.func()` 调用 → AST 里是 Attribute(func=X.func)，
        名字是 func → 靠 attr_used 兜住；但**别名**（from core import iterate as it）
        仍算得到（Name=it），要靠"被 import 的模块的公开函数"反查。
      · 同名函数会互相掩盖（cli.out 的 as_json 参数名不算调用）。
    """
    # 收集全仓调用过的名字
    called: set[str] = set()
    attr_used: set[str] = set()
    imported_modules: set[str] = set()
    for p in ALL_PY:
        if _skip(p):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (SyntaxError, OSError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    attr_used.add(f.attr)
                    called.add(f.attr)
                    if isinstance(f.value, ast.Name):
                        imported_modules.add(f.value.id)
            elif isinstance(node, ast.Attribute):
                attr_used.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    imported_modules.add(a.asname or a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    imported_modules.add(a.asname or a.name.split(".")[0])

    dead = []
    for p in core_files():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name.startswith("_"):
                    continue          # 私有：不算
                if node.name in called or node.name in attr_used:
                    continue
                # 通过 `模块.函数()` 调用的：模块名出现在 imported_modules 且
                # 该模块就是本文件 → 算"有调用"
                if p.stem in imported_modules:
                    continue
                dead.append(f"{p.name}:{node.lineno} {node.name}")
    return dead


# ───────────────────────────────────────── C. 两线挂载差异
def scan_line_mounts():
    """甲方线 company.night 与全自主线 auto.night_auto 各自用到哪些 core 模块。"""
    out = {}
    for fname in ("company.py", "auto.py"):
        p = CORE / fname
        src = p.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        mods: set[str] = set()
        for node in ast.walk(tree):
            # from . import X as Y  /  from .X import ...
            if isinstance(node, ast.ImportFrom):
                if node.level and node.module:
                    mods.add(node.module.split(".")[0])
                for a in node.names:
                    mods.add(a.name)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[-1])
        out[fname] = mods
    return out


def main() -> None:
    print("=" * 78)
    print("A. 属性级残留引用（except/raise 引用了不存在的属性）")
    print("=" * 78)
    hits = scan_attr_refs()
    if not hits:
        print("  （无）")
    for f, ln, desc in hits:
        print(f"  {f}:{ln}  {desc}")

    print()
    print("=" * 78)
    print("B. 死机制（core 公开函数，全仓找不到调用点）")
    print("=" * 78)
    dead = scan_dead_functions()
    print(f"  共 {len(dead)} 个")
    for d in dead:
        print("   ", d)

    print()
    print("=" * 78)
    print("C. 两线挂载差异")
    print("=" * 78)
    mounts = scan_line_mounts()
    comp = mounts.get("company.py", set())
    auto = mounts.get("auto.py", set())
    only_comp = sorted(comp - auto)
    only_auto = sorted(auto - comp)
    print(f"  只在 company.py（甲方线）挂载：{len(only_comp)}")
    for m in only_comp:
        print("   ", m)
    print(f"  只在 auto.py（全自主线）挂载：{len(only_auto)}")
    for m in only_auto:
        print("   ", m)

    print()
    print("=" * 78)
    print("D. 候选：与已修缺陷同形的地方（'定义了但可能没走到'的写法）")
    print("=" * 78)
    # 找形如 `X[0]` 后立刻 str() / 当字符串用的 —— _role_map 那类元组字符串化
    pat = re.compile(r"str\(\s*\w+\[0\]\s*\)")
    for p in ALL_PY:
        if _skip(p):
            continue
        try:
            src = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(src.splitlines(), 1):
            s = ln.strip()
            if s.startswith("#"):
                continue
            if pat.search(ln):
                print(f"  {p.relative_to(ROOT)}:{i}  {s[:110]}")


if __name__ == "__main__":
    main()
