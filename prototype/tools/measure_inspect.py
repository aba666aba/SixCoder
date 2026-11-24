"""把每个历史工地的"体检得分"重新算一遍（离线、可重复）。

为什么需要它：改判据的时候最容易自欺——"我加了边界矩阵"，但如果不拿真实产物重跑一遍，
就不知道分数变了没有、变的是哪几个函数。这个脚本只读，不改任何工地文件。

用法：
    python tools/measure_inspect.py projects/night2 projects/night3 ...
    python tools/measure_inspect.py --all
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import contract as contract_mod          # noqa: E402
from core import db, iterate                      # noqa: E402


def requirement_of(project: Path) -> str:
    """工地对应的需求原文：优先取任务库，其次取 DELIVERY.md 里的需求块。"""
    name = project.name
    try:
        con = sqlite3.connect(db.DB_PATH)
        row = con.execute(
            "select requirement from tasks where workspace like ? order by rowid desc limit 1",
            (f"%{name}%",)).fetchone()
        con.close()
        if row and row[0]:
            return str(row[0])
    except Exception:                             # noqa: BLE001
        pass
    deliv = project / "DELIVERY.md"
    if deliv.exists():
        text = deliv.read_text(encoding="utf-8-sig", errors="replace")
        marker = "## 需求"
        if marker in text:
            return text.split(marker, 1)[1].split("\n## ", 1)[0]
    return ""


def measure(project: Path) -> dict:
    req = requirement_of(project)
    if not req.strip():
        return {"project": str(project), "error": "拿不到需求原文"}
    contracts = contract_mod.parse_requirement(req)
    if not contracts:
        return {"project": str(project), "error": "需求解析不出契约"}
    state = iterate.inspect(project, contracts)
    rows = []
    for path, st in sorted(state.items()):
        rows.append({"path": path, "funcs": st.get("funcs"),
                     "verified": st.get("verified_names") or [],
                     "hard_failed": st.get("hard_failed") or [],
                     "untested": st.get("untested") or []})
    n_ok = sum(len(r["verified"]) for r in rows)
    n_bad = sum(len(r["hard_failed"]) for r in rows)
    n_todo = sum(len(r["untested"]) for r in rows)
    return {"project": str(project), "ok": n_ok, "hard_failed": n_bad,
            "untested": n_todo, "modules": rows,
            "e2e": iterate.last_e2e(project)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("projects", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    targets = [Path(p) for p in a.projects]
    if a.all or not targets:
        targets = sorted(p for p in (ROOT.parent / "projects").iterdir()
                         if p.is_dir() and (p / "DELIVERY.md").exists())
    bad = 0
    for p in targets:
        r = measure(p.resolve() if p.is_absolute() else (ROOT.parent / p).resolve())
        if r.get("error"):
            print(f"{p.name}: 跳过（{r['error']}）")
            continue
        total = r["ok"] + r["hard_failed"] + r["untested"]
        print(f"{p.name}: {r['ok']}/{total} 验收通过，机械判据判红 {r['hard_failed']}，"
              f"无判据 {r['untested']}")
        if a.verbose:
            for m in r["modules"]:
                print(f"  {m['path']}: 绿 {len(m['verified'])} 红 {len(m['hard_failed'])} "
                      f"未验 {len(m['untested'])}")
                if m["hard_failed"]:
                    print(f"    红: {', '.join(m['hard_failed'])}")
                if m["untested"]:
                    print(f"    未验: {', '.join(m['untested'])}")
        bad += r["hard_failed"]
    return 0 if not bad else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
