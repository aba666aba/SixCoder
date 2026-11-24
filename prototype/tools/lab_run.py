"""实验台：在同一个目录里，用**不同系统类型**反复真跑夜班，跑完留下可核对的证据。

为什么要有它：66 项单元检查全绿，一个真缺陷都抓不到；而**一次真跑**就能抓出十几个
（假红/假绿/报告说假话/污染客户仓库）。所以"发现问题"的唯一手段是真跑 + 逐字读产物。

用法：
    python tools/lab_run.py --batch 1                # 第一批（5 种系统类型）
    python tools/lab_run.py --batch 2
    python tools/lab_run.py --slugs api-layer,cli-tool
    python tools/lab_run.py --batch 2 --dir D:/Download/cal --budget 20

每个类型做完这五件事（缺一件就等于没实验）：
  1. 归档上一次产物 → _logs/lab/_archive/…（不销毁证据）
  2. 清空工作区里的产物与 .sixcoder 状态（保证这轮是"从零"）
  3. 真跑夜班，事件流落盘
  4. 收证据：验收数/收工原因/花费、交付报告关键行、独立冒烟（import + pytest）、
     引擎残留物检查、docstring 残句检查
  5. 把本轮产物整份留在 lab/<slug>/products/（排查"判据逼模型写垃圾代码"只能靠逐行读代码）
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company, contract as cm, iterate, remote  # noqa: E402
from tools import lab_types  # noqa: E402

PY = sys.executable
ENGINE_RESIDUE = ("_props_run.py", ".props_run.py")
KEEP = {".gitignore"}
COPY_IGNORE = ("__pycache__", ".mypy_cache", ".tmp", ".git", ".pytest_cache", ".sixcoder")


def _force_rmtree(p: Path) -> None:
    """Windows 上 .git 里的对象文件是只读的，rmtree(ignore_errors=True) 会**静默失败**，
    留下半个 .git/objects —— 于是下一轮实验就在脏状态上跑（实测踩到：products 复制直接
    Permission denied，且 .git 残留会让隔离层误判"这是 git 仓库"）。"""
    import os
    import stat as _stat

    def _onerr(func, path, exc):                      # noqa: ANN001
        try:
            os.chmod(path, _stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    try:
        shutil.rmtree(p, onexc=_onerr)
    except TypeError:                                 # 老版本 Python 只有 onerror
        shutil.rmtree(p, onerror=_onerr)


def _reset_ws(ws: Path) -> list[str]:
    """清掉上一轮的产物与引擎状态（保留 .gitignore）。"""
    removed: list[str] = []
    if not ws.exists():
        ws.mkdir(parents=True)
        return removed
    for p in list(ws.iterdir()):
        if p.name in KEEP:
            continue
        try:
            if p.is_dir():
                _force_rmtree(p)
            else:
                p.unlink()
            removed.append(p.name)
        except OSError as exc:                            # noqa: BLE001
            removed.append(f"!{p.name}({type(exc).__name__})")
    left = [p.name for p in ws.iterdir() if p.name not in KEEP]
    if left:                                              # 清不干净就喊出来，别在脏地上做实验
        removed.append(f"!!清不掉：{left}")
    return removed


def _archive(ws: Path, out: Path, tag: str) -> None:
    """把上一轮产物整份归档（证据不能丢，尤其被改坏的产物最有价值）。"""
    if not ws.exists() or not any(ws.iterdir()):
        return
    dst = out / "_archive" / tag
    shutil.copytree(ws, dst, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))


def _smoke(ws: Path, paths: list[str]) -> dict:
    """独立冒烟：不共享引擎判据，自己 import、自己跑仓库里的测试。"""
    out: dict = {"imports": {}, "pytest": ""}
    for rel in paths:
        mod = Path(rel).stem
        code = ("import importlib, sys; sys.path.insert(0, '.'); "
                f"m = importlib.import_module('{mod}'); "
                "print('symbols', len([x for x in dir(m) if not x.startswith('_')]))")
        try:
            p = subprocess.run([PY, "-B", "-c", code], cwd=str(ws), capture_output=True,
                               text=True, timeout=60, encoding="utf-8", errors="replace")
            lines = [ln for ln in (p.stdout or p.stderr or "").strip().splitlines() if ln.strip()]
            out["imports"][rel] = lines[-1] if lines else ""
        except Exception as exc:                          # noqa: BLE001
            out["imports"][rel] = f"import 失败 {type(exc).__name__}"
    if sorted(p.name for p in ws.glob("test_*.py")):
        try:
            p = subprocess.run([PY, "-m", "pytest", "-q"], cwd=str(ws), capture_output=True,
                               text=True, timeout=240, encoding="utf-8", errors="replace")
            tail = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()][-1:]
            out["pytest"] = tail[0] if tail else (p.stderr or "")[-120:]
        except Exception as exc:                          # noqa: BLE001
            out["pytest"] = f"pytest 跑不动 {type(exc).__name__}"
    return out


def _residue(ws: Path) -> list[str]:
    bad = []
    for name in ENGINE_RESIDUE:
        if (ws / name).exists():
            bad.append(name)
    for p in ws.rglob("_props_run.py"):
        bad.append(str(p.relative_to(ws)))
    for p in ws.rglob("*.tmp"):
        bad.append(str(p.relative_to(ws)))
    return sorted(set(bad))


def _truncated_docs(ws: Path, contracts: dict) -> list[str]:
    """交付物的 docstring 是不是契约说明的**残句**（已咬过两次的缺陷类）。"""
    import ast
    out: list[str] = []
    for rel, c in contracts.items():
        p = ws / rel
        if not p.exists():
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            out.append(f"{rel}: 语法坏，解析不了")
            continue
        docs = {n.name: (ast.get_docstring(n) or "").strip() for n in tree.body
                if isinstance(n, ast.FunctionDef)}
        for f in c.funcs:
            got, want = docs.get(f.name, ""), (f.doc or "").strip()
            if got and want and len(want) > len(got) + 3 and want.startswith(got):
                out.append(f"{rel}::{f.name} 少 {len(want) - len(got)} 字")
    return out


def _report_lines(ws: Path, keys: tuple[str, ...]) -> list[str]:
    p = ws / "DELIVERY.md"
    if not p.exists():
        return ["（没有生成 DELIVERY.md —— 这本身就是缺陷）"]
    t = p.read_text(encoding="utf-8", errors="replace")
    out = []
    for k in keys:
        for ln in t.splitlines():
            if k in ln:
                out.append(ln.strip()[:180])
                break
    return out


def run_one(t: dict, ws: Path, out_root: Path) -> dict:
    slug = t["slug"]
    out = out_root / slug
    out.mkdir(parents=True, exist_ok=True)
    _archive(ws, out_root, f"{time.strftime('%m%d-%H%M')}-{slug}")
    removed = _reset_ws(ws)
    (out / "requirement.txt").write_text(t["req"], encoding="utf-8")

    evp = out / "events.jsonl"
    if evp.exists():
        evp.unlink()
    kinds: dict[str, int] = {}

    def emit(kind, payload):
        kinds[kind] = kinds.get(kind, 0) + 1
        with evp.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": kind, "payload": payload},
                                ensure_ascii=False, default=str) + "\n")

    before = remote.usage_summary("")
    t0 = time.time()
    res = company.night(ws, t["req"], "qwen2.5-coder:7b",
                        use_remote=not t.get("local", False),
                        emit=emit)
    spent_s = time.time() - t0
    after = remote.usage_summary("")
    cost = round(float(after["cost_cny"]) - float(before["cost_cny"]), 4)
    calls = int(after["calls"]) - int(before["calls"])

    contracts = cm.parse_requirement(t["req"])
    state = iterate.inspect(ws, contracts)
    (out / "state.json").write_text(json.dumps(
        {k: {kk: (sorted(vv) if isinstance(vv, set) else vv)
             for kk, vv in v.items() if kk in ("funcs", "verified", "verified_names", "stubs",
                                               "missing", "untested", "notes", "fails",
                                               "hard_failed", "engine_failed", "engine_errors")}
         for k, v in state.items()}, ensure_ascii=False, indent=1), encoding="utf-8")

    info = {
        "slug": slug, "name": t["name"], "scale": t.get("scale"),
        "removed_before_run": removed,
        "verified": res.verified, "total": res.total, "reason": res.reason,
        "seconds": round(spent_s, 1), "calls": calls, "cost_cny": cost,
        "event_kinds": kinds,
        "report": _report_lines(ws, ("验收通过函数", "收工原因", "花费", "未完成",
                                     "lint", "typecheck", "集成", "依赖审计",
                                     "架构健康度", "现有测试文件", "变异")),
        "per_module": {k: {"verified": v.get("verified"), "funcs": v.get("funcs"),
                           "hard_failed": v.get("hard_failed"),
                           "stubs": sorted(v.get("stubs") or []),
                           "notes": (v.get("notes") or [])[:2],
                           "fails": (v.get("fails") or [])[:2]}
                       for k, v in state.items()},
        "files": sorted(p.name for p in ws.rglob("*.py")),
        "residue": _residue(ws),
        "truncated_docs": _truncated_docs(ws, contracts),
        "smoke": _smoke(ws, list(contracts)),
    }
    (out / "result.json").write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                     encoding="utf-8")
    # 「故意自相矛盾的契约」这类实验，判分标准不是写没写出来，而是**有没有诚实拒绝**：
    # 一次都不许调远端、不许验收通过、理由必须点出矛盾。
    if t.get("expect") == "refuse":
        honest = (info["verified"] == 0 and info["calls"] == 0
                  and ("矛盾" in str(info["reason"]) or "拒绝" in str(info["reason"])))
        info["verdict"] = "PASS" if honest else "FAIL"
    # 产物必须留在本轮目录里：不然下一轮清空工作区后就只剩结论、没有可读的代码
    prod = out / "products"
    if prod.exists():
        _force_rmtree(prod)
    shutil.copytree(ws, prod, dirs_exist_ok=True, ignore=shutil.ignore_patterns(*COPY_IGNORE))
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="D:/Download/cal")
    ap.add_argument("--batch", default="1")
    ap.add_argument("--slugs", default="")
    ap.add_argument("--budget", type=float, default=20.0,
                    help="本实验自己的兜底花费线（实验脚本口径，与引擎停机线无关）")
    a = ap.parse_args()

    if a.slugs:
        want = [s.strip() for s in a.slugs.split(",") if s.strip()]
    else:
        want = [t["slug"] for t in lab_types.BATCHES.get(str(a.batch), lab_types.BATCH1)]
    todo = [lab_types.ALL[s] for s in want if s in lab_types.ALL]
    if not todo:
        print("没有匹配的类型")
        return 2

    ws = Path(a.dir)
    out_root = PROTO.parent / "_logs" / "lab"
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"实验目录：{ws}｜{len(todo)} 种系统类型｜实验兜底线 ¥{a.budget}\n")

    spent_all = 0.0
    results: list[dict] = []
    for t in todo:
        if spent_all >= a.budget:
            print(f"已达实验兜底线 ¥{a.budget}，停止")
            break
        print(f"=== [{t['slug']}] {t['name']}")
        try:
            info = run_one(t, ws, out_root)
        except Exception as exc:                          # noqa: BLE001
            print(f"  **引擎抛异常** {type(exc).__name__}: {exc}  ← 这是要修的 bug")
            import traceback
            traceback.print_exc()
            results.append({"slug": t["slug"], "crash": f"{type(exc).__name__}: {exc}"})
            continue
        spent_all += info["cost_cny"]
        results.append(info)
        print(f"  验收 {info['verified']}/{info['total']}｜{info['seconds']}s｜"
              f"{info['calls']} 次调用｜¥{info['cost_cny']}｜累计 ¥{round(spent_all, 4)}")
        print(f"  原因：{str(info['reason'])[:110]}")
        for rel, st in info["per_module"].items():
            clean = (st["verified"] == st["funcs"] and not st["hard_failed"]
                     and not st["notes"] and not st["stubs"])
            if clean:
                print(f"  ✓ {rel} {st['verified']}/{st['funcs']}")
            else:
                print(f"  ! {rel} {st['verified']}/{st['funcs']} 判红={st['hard_failed']} "
                      f"仍是桩={st['stubs']} 提示={st['notes']}")
                for f in st["fails"]:
                    print(f"      失败：{str(f)[:160]}")
        if info["residue"]:
            print(f"  **引擎残留物** {info['residue']}")
        if info["truncated_docs"]:
            print(f"  **docstring 残句** {info['truncated_docs']}")
        print(f"  冒烟：{info['smoke']['imports']}｜pytest: {info['smoke']['pytest'] or '无测试文件'}")
        if info.get("verdict"):
            print(f"  **本实验判分：{info['verdict']}**（期望：诚实拒绝、不调远端、不验收通过）")
        print()

    (out_root / f"summary-{time.strftime('%m%d-%H%M%S')}.json").write_text(
        json.dumps({"results": results, "spent_cny": round(spent_all, 4)},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"本轮合计花费 ¥{round(spent_all, 4)}｜证据目录 {out_root}")
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
