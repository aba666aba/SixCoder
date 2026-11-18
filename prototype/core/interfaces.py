"""接口清单（`interfaces.json`）：谁提供什么签名，以及"代码与清单是否一致"。

为什么需要（开发文档 §8.2 / §8.6）：接口如果只是每片开工时现编一段文字，就会出现
  · 同一函数今天 `batch_id: int`、明天 `batch_id: str`，已完成的代码全部作废；
  · 跨模块调用写了个根本不存在的函数，直到运行时才 NameError。
接口清单把"签名"变成**可枚举、可比对**的事实：能生成、能锁、能算 diff。

三个确定性能力（不调模型）：
  scan(ws)          从 AST 抽出每个模块的公开函数签名 + 抛出的异常
  consistency(ws)   ① 清单与代码是否一致（漂移）② 跨模块 import 是否都能解析到真实符号
  diff(ws)          与上次锁定的清单比：新增 / 删除 / **签名变更**（变更要显式付出代价）
"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

IFACE_FILE = ".sixcoder/interfaces.json"
SKIP_DIRS = {".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache",
             "venv", ".venv", "node_modules", "tests", "test"}


def _annotation(node) -> str:
    try:
        return ast.unparse(node) if node is not None else ""
    except Exception:                                    # noqa: BLE001
        return ""


def _raises(fn) -> list[str]:
    out: list[str] = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call):
            name = getattr(n.exc.func, "id", "") or getattr(n.exc.func, "attr", "")
            if name:
                out.append(name)
    return sorted(set(out))


def _sig(fn) -> dict:
    args = []
    for a in fn.args.args:
        args.append({"name": a.arg, "type": _annotation(a.annotation)})
    defaults = len(fn.args.defaults or [])
    for i, a in enumerate(args):
        a["default"] = i >= len(args) - defaults
    return {"name": fn.name, "params": args,
            "returns": _annotation(fn.returns), "raises": _raises(fn),
            "doc": (ast.get_docstring(fn) or "").strip().splitlines()[0][:80]
            if ast.get_docstring(fn) else ""}


def scan(ws) -> dict:
    """{相对路径: {funcs: [...], classes: [...], hash: "..."}}（只列公开符号）。"""
    ws = Path(ws)
    out: dict[str, dict] = {}
    for p in sorted(ws.rglob("*.py")):
        try:
            rel = p.relative_to(ws).as_posix()
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ws).parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        funcs = [_sig(n) for n in tree.body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and not n.name.startswith("_")]
        classes = [{"name": n.name, "methods": [_sig(m)["name"] for m in n.body
                                                if isinstance(m, ast.FunctionDef)]}
                   for n in tree.body if isinstance(n, ast.ClassDef)]
        if not funcs and not classes:
            continue
        blob = json.dumps({"funcs": funcs, "classes": classes}, sort_keys=True,
                          ensure_ascii=False)
        out[rel] = {"funcs": funcs, "classes": classes,
                    "hash": hashlib.sha1(blob.encode("utf-8")).hexdigest()[:16]}
    return out


def save(ws, data: dict | None = None) -> Path:
    ws = Path(ws)
    data = data if data is not None else scan(ws)
    p = ws / IFACE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True),
                 encoding="utf-8")
    return p


def load(ws) -> dict:
    try:
        return json.loads((Path(ws) / IFACE_FILE).read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def diff(ws, old: dict | None = None, new: dict | None = None) -> dict:
    """与锁定版本比：新增模块 / 删除模块 / 签名变更。"""
    old = old if old is not None else load(ws)
    new = new if new is not None else scan(ws)
    if not old:
        return {"first_time": True, "added": sorted(new), "removed": [],
                "changed": [], "unchanged": 0}
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = []
    same = 0
    for rel in sorted(set(new) & set(old)):
        if new[rel].get("hash") != old[rel].get("hash"):
            o = {f["name"]: f for f in (old[rel].get("funcs") or [])}
            n = {f["name"]: f for f in (new[rel].get("funcs") or [])}
            detail = {"module": rel,
                      "added": sorted(set(n) - set(o)),
                      "removed": sorted(set(o) - set(n)),
                      "resig": [k for k in sorted(set(n) & set(o))
                                if json.dumps(n[k], sort_keys=True)
                                != json.dumps(o[k], sort_keys=True)]}
            changed.append(detail)
        else:
            same += 1
    return {"first_time": False, "added": added, "removed": removed,
            "changed": changed, "unchanged": same}


def _module_of(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def unresolved_imports(ws) -> list[dict]:
    """跨模块 import 指向仓库内模块、但那个模块里**没有这个符号** → 记一条。

    这类错误静态可抓、运行必炸（NameError），却经常被当成"能力做不出来"。
    """
    ws = Path(ws)
    files = {}
    for p in ws.rglob("*.py"):
        try:
            rel = p.relative_to(ws).as_posix()
        except ValueError:
            continue
        if any(part in SKIP_DIRS for part in p.relative_to(ws).parts):
            continue
        files[_module_of(rel)] = rel
    names: dict[str, set[str]] = {}
    for mod, rel in files.items():
        try:
            tree = ast.parse((ws / rel).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        got = {n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        for n in tree.body:
            if isinstance(n, ast.Assign):
                got |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        names[mod] = got
    out: list[dict] = []
    for mod, rel in files.items():
        try:
            tree = ast.parse((ws / rel).read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            tgt = node.module
            if tgt not in names:
                continue
            for a in node.names:
                if a.name == "*":
                    continue
                if a.name in names[tgt]:
                    continue
                if f"{tgt}.{a.name}" in files:       # 导入的是子模块，不算错
                    continue
                out.append({"file": rel, "module": tgt, "symbol": a.name,
                            "line": getattr(node, "lineno", 0)})
    return out


def consistency(ws, iface: dict | None = None) -> dict:
    """清单 vs 代码 + 跨模块 import 可解析性。"""
    d = diff(ws, old=(iface if iface is not None else load(ws)))
    un = unresolved_imports(ws)
    drift = bool(d.get("changed") or d.get("removed"))
    return {"ok": (not drift and not un), "drift": d, "unresolved": un,
            "first_time": bool(d.get("first_time"))}


def apply_drift(ws, *, phase: str = "start") -> dict:
    """变更通道，分两个阶段（参数 phase）：

    · phase="start"（片开始）：漂移 → 受影响模块回待办（反正契约要求重写）；
    · phase="end"（片结束）：**只重新锁定，绝不清零**。本片刚把函数全部验收通过
      （实测 2026-11-07 verified=10/10），这时拿"和开工快照不一样"去清零，
      等于把本片刚挣的进度当场作废 —— 那是一次真 bug（interface_drift reset=4）。

    只有"签名变更(resig)"和"删函数(removed)"才算改接口；单纯新增函数不算。
    返回 {first_time, hard[], unresolved[], ok, relocked}，调用方据此发事件。
    """
    ws = Path(ws)
    cons = consistency(ws)
    if cons.get("first_time"):
        save(ws)
        return {"first_time": True, "hard": [], "unresolved": [], "ok": True,
                "relocked": True}
    drift = cons.get("drift") or {}
    hard = [c for c in (drift.get("changed") or [])
            if c.get("resig") or c.get("removed")]
    if phase == "start" and (hard or not cons.get("ok")):
        from . import project as _proj
        for c in hard:
            _proj.save_progress(ws, c["module"], {"verified": [], "ok": False})
    save(ws)
    return {"first_time": False, "hard": (hard if phase == "start" else []),
            "unresolved": cons.get("unresolved") or [], "ok": cons.get("ok"),
            "relocked": True}
