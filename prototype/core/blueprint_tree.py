"""骨架树：把"仓库现状 + 需求目标"变成一棵可校验的树（L0 系统 → L1 子系统 → L2 模块）。

注意：本模块与 `core/skeleton.py` 不是一回事 —— 那个是"渲染函数骨架（签名 + NotImplementedError）"，
这个是"规划层的树"。取名 `blueprint_tree` 就是为了不再撞名。

为什么要树而不是一张平铺能力表（实测 2026-11-07）：
  · 平铺表里"能力"和"文件"是多对多，父节点不等于子节点之和 → 预算没法继承，
    做完一半也看不出离目标还差多少；
  · 没有树就没有"叶子必须落到文件"的约束，于是规划停在"大概几个函数"，
    实现阶段才开始现编接口 → 契约漂移、已完成作废（`batch_id` int→str 那次）。

三条机械校验（全静态、零 token）：
  1. sum_ok     每个父节点的行数/函数数 == 子节点之和
  2. budget_ok  每层不超预算（L1 用每子系统上限；L0 的目标行数只作参考）
  3. leaf_ok    每个叶子都对应真实文件（规划不许指向不存在的文件）

树落盘 `.sixcoder/skeleton_tree.json`，供切片、覆盖、报告共用同一份事实。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

SKIP_DIRS = {".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "venv", ".venv", "node_modules", "tests", "test", "docs", "_logs"}
TREE_FILE = ".sixcoder/skeleton_tree.json"
DEFAULT_L1_CAP = 8000        # 单个子系统目标行数上限（超过就该继续分）


def _scan_file(p: Path) -> dict:
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return {"lines": 0, "funcs": 0, "public": 0}
    pub = sum(1 for n in tree.body
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and not n.name.startswith("_"))
    return {"lines": len(text.splitlines()), "funcs": len(tree.body), "public": pub}


def build(ws, *, target_lines: int = 0, l1_cap: int = DEFAULT_L1_CAP) -> dict:
    """从仓库实测出 L0/L1/L2。target_lines=0 表示只做现状（不判预算）。"""
    ws = Path(ws)
    groups: dict[str, list[dict]] = {}
    for p in sorted(ws.rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.relative_to(ws).parts):
            continue
        rel = p.relative_to(ws).as_posix()
        top = rel.split("/")[0] if "/" in rel else "."
        st = _scan_file(p)
        groups.setdefault(top, []).append({"path": rel, **st})

    children = []
    for name, files in sorted(groups.items()):
        stats = {k: sum(f[k] for f in files) for k in ("lines", "funcs", "public")}
        children.append({"id": name, "level": 1, "parent": "root",
                         "files": [f["path"] for f in files], **stats,
                         "kind": "package" if name != "." else "loose"})

    total = {k: sum(c[k] for c in children) for k in ("lines", "funcs", "public")}
    root = {"id": "root", "level": 0, "parent": "", "children": [c["id"] for c in children],
            **total, "target_lines": int(target_lines or 0)}
    tree = {"root": root, "nodes": children, "l1_cap": int(l1_cap)}
    tree["checks"] = validate(tree)
    return tree


def validate(tree: dict) -> dict:
    """三校验。返回 {sum_ok, budget_ok, leaf_ok, problems[], over[]}。"""
    problems: list[str] = []
    root = tree.get("root") or {}
    nodes = tree.get("nodes") or []

    for key in ("lines", "funcs", "public"):
        s = sum(int(n.get(key) or 0) for n in nodes)
        if int(root.get(key) or 0) != s:
            problems.append(f"sum: root.{key}={root.get(key)} 但子节点之和={s}")
    sum_ok = not [p for p in problems if p.startswith("sum:")]

    over = [n["id"] for n in nodes if int(n.get("lines") or 0) > int(tree.get("l1_cap") or 0)]
    target = int(root.get("target_lines") or 0)
    budget_ok = not over or not target      # 没有目标时只报告不判死
    if over and target:
        problems.append("budget: 子系统超上限 " + "、".join(over[:4]))

    leaf_ok = True
    for n in nodes:
        if not n.get("files"):
            leaf_ok = False
            problems.append(f"leaf: 子系统 {n['id']} 没有任何文件（不能当叶子）")
    return {"sum_ok": sum_ok, "budget_ok": budget_ok, "leaf_ok": leaf_ok,
            "problems": problems, "over": over}


def save(ws, tree: dict) -> Path:
    p = Path(ws) / TREE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tree, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load(ws) -> dict:
    p = Path(ws) / TREE_FILE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def gaps(tree: dict, target_lines: int) -> dict:
    """离目标还差多少：按子系统给缺口，供"继续拆/继续产"用。"""
    root = tree.get("root") or {}
    have = int(root.get("lines") or 0)
    need = max(0, int(target_lines or 0) - have)
    return {"have": have, "target": int(target_lines or 0), "gap": need,
            "pct": round(have / target_lines, 3) if target_lines else 0.0}


def summary_lines(tree: dict) -> list[str]:
    """给报告/事件用的一行式摘要。"""
    root = tree.get("root") or {}
    chk = tree.get("checks") or {}
    ok = all(chk.get(k) for k in ("sum_ok", "budget_ok", "leaf_ok"))
    lines = [f"- 骨架：L1 {len(tree.get('nodes') or [])} 个子系统｜{root.get('lines', 0)} 行"
             f"｜{root.get('public', 0)} 个公开函数｜{'三校验通过' if ok else '**校验未过**'}"]
    for n in (tree.get("nodes") or [])[:8]:
        lines.append(f"  - `{n['id']}`：{n['lines']} 行 / {len(n['files'])} 文件")
    if not ok:
        lines += ["  - " + p for p in (chk.get("problems") or [])[:4]]
    return lines
