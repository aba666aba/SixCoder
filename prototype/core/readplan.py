"""读取策略（开发文档 §8.5）：这一单该读哪几档、读哪些文件、读到什么程度。

为什么要有这一层：上下文不是"越多越好"，也不是"按任务名字拍脑袋"。判据只有一条 ——
**要做什么决定，就需要什么强度的证据**：

  · 只是调用别人 → 接口摘要够（T0）；
  · 要改这个文件 → 必须看全文（T1），没看全文就不许写（硬门禁）；
  · 跨模块/改契约 → 要看调用链与反向依赖（T2），否则改完必炸别处；
  · 搭骨架/全局重构 → 要看全仓**结构**（T3），但不读全文（3 万行全文 ~40 万 token）。

这一层只做**确定性**的事：分类、选文件、裁剪、报告读了什么。不做语义判断。
"""
from __future__ import annotations

import ast
from pathlib import Path

TIERS = ("T0", "T1", "T2", "T3")

KINDS = {
    "new_module": ("T0",),          # 新文件：只需接口摘要 + 结构
    "edit_existing": ("T0", "T1"),  # 改已有文件：全文是硬要求
    "fix_bug": ("T1", "T2"),        # 修 bug：报错文件 + 调用链
    "change_contract": ("T2",),     # 改契约：两边都读 + 反向依赖
    "skeleton": ("T3",),            # 搭骨架：全仓结构
}

WHY = {
    "new_module": "只调用已有接口，不需要读别人的实现（接口摘要够）",
    "edit_existing": "要改这个文件，必须看全文，否则改坏已验收的部分是必然",
    "fix_bug": "报错文件是症状不是病因：还要看调用链每一跳与对应测试",
    "change_contract": "改契约要两边都读（文档给语义、源码给边界），并查反向依赖",
    "skeleton": "只有结构需要全读；实现全文读不起，也没必要（3 万行 ≈ 40 万 token）",
}

SKIP_DIRS = {".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "venv", ".venv", "node_modules"}


def plan(kind: str, *, target: str = "", modules: list[str] | None = None,
         evidence: str = "") -> dict:
    """产出这一单的读取计划（纯函数，不碰磁盘）。"""
    k = str(kind or "").strip()
    if k not in KINDS:
        k = "edit_existing" if target else "new_module"
    tiers = list(KINDS[k])
    files: list[str] = []
    if target and "T1" in tiers:
        files.append(target)
    mods = [str(m) for m in (modules or []) if m]
    return {"kind": k, "tiers": tiers, "target": target, "files": files,
            "modules": mods, "evidence": str(evidence or "")[:400],
            "why": WHY[k]}


def _clip(text: str, head: int = 7000, tail: int = 4000) -> tuple[str, bool]:
    if len(text) <= head + tail:
        return text, False
    return text[:head] + "\n# ……中间省略……\n" + text[-tail:], True


def _outline(ws: Path) -> list[str]:
    """全仓结构：每个文件一行（模块级 def/class 名字），不含函数体。"""
    out: list[str] = []
    for p in sorted(Path(ws).rglob("*.py")):
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        rel = p.relative_to(ws).as_posix()
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            out.append(f"{rel}（解析失败）")
            continue
        names: list[str] = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(node.name + "()")
            elif isinstance(node, ast.ClassDef):
                names.append(node.name)
        out.append(f"{rel}: " + (", ".join(names[:14]) or "（无顶层定义）"))
    return out


def collect(ws, p: dict, *, budget_chars: int = 12000) -> dict:
    """按计划把内容取出来。返回 {text, files, chars, full, truncated, note}。

    full=True 只表示"目标文件全文确实给全了"——写前门禁就看这个字段，
    而不是看"我有没有调用过读文件"。
    """
    ws = Path(ws)
    kind = p.get("kind") or ""
    blocks: list[str] = []
    files_read: list[str] = []
    full = True
    truncated = False
    note = ""

    # T3：全仓结构（不是全文）
    if "T3" in (p.get("tiers") or []):
        lines = _outline(ws)
        blocks.append("# 仓库结构（AST outline：只有文件与顶层定义，不含函数体）\n"
                      + "\n".join(lines[:600]))
        note = f"结构 {len(lines)} 个文件"

    # T1：目标文件全文（超预算只给头尾，并明确告知）
    if "T1" in (p.get("tiers") or []) and p.get("target"):
        tp = ws / p["target"]
        if tp.is_file():
            txt = tp.read_text(encoding="utf-8", errors="replace")
            clipped, cut = _clip(txt, 7000, max(1000, budget_chars - 7000))
            files_read.append(p["target"])
            full = not cut
            truncated = truncated or cut
            blocks.append("# 当前文件内容" + ("（**只有头尾**：全文 %d 字符，超过注入上限）" % len(txt)
                                          if cut else "（在它基础上增量修改，别整篇重写）")
                          + "\n```python\n" + clipped.rstrip() + "\n```\n")
            if cut:
                blocks.append("**禁止整篇重写**：你没看过中间部分，只能用 patches 改你看得见的片段。\n")

    # T2：调用链（同目录/依赖方/被依赖方）
    if "T2" in (p.get("tiers") or []):
        tgt = p.get("target") or ""
        extra = [f for f in (p.get("modules") or []) if f and f != tgt]
        if tgt:
            try:
                from . import governance as _gov
                for a, _m in _gov.reverse_deps(ws, [tgt])[:6]:
                    if a not in extra:
                        extra.append(a)
            except Exception:                            # noqa: BLE001
                pass
        for rel in extra[:6]:
            fp = ws / rel
            if not fp.is_file():
                continue
            txt = fp.read_text(encoding="utf-8", errors="replace")
            clipped, cut = _clip(txt, 2500, 1500)
            if cut:
                truncated = True
            files_read.append(rel)
            blocks.append(f"# 相关文件 {rel}{'（只给头尾）' if cut else ''}\n"
                          f"```python\n{clipped.rstrip()}\n```\n")

    # T0：接口摘要（只给签名，不给实现）
    if "T0" in (p.get("tiers") or []) and p.get("modules"):
        try:
            from . import slicer as _sl
            sig = _sl.interface_digest(ws, list(p["modules"]), limit=20)
            if sig:
                blocks.append("# 可调用的接口（只有签名）\n" + "\n".join(sig))
        except Exception:                                # noqa: BLE001
            pass

    text = "\n\n".join(b for b in blocks if b)
    return {"text": text, "files": files_read, "chars": len(text),
            "full": bool(full and (not p.get("target") or p.get("target") in files_read)),
            "truncated": truncated, "note": note}


def write_allowed(p: dict, target: str, got: dict) -> tuple[bool, str]:
    """写前门禁：要写 target，就必须在本次上下文里给过它的全文。"""
    if not target:
        return False, "没有目标文件"
    if (Path(target)).name == "":
        return False, "目标路径不合法"
    if target not in (got.get("files") or []):
        return False, f"没读过 {target}（不在本次上下文里），不许写"
    if not got.get("full", False):
        return False, f"{target} 只给了头尾，没看过全文，不许整篇重写"
    return True, "已读全文"
