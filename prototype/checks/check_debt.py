"""临时方案台账自检：记得到、到期会红、修好会 resolved、分级不同。

跑法: python check_debt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import debt  # noqa: E402

FAILS: list[str] = []


def _sandbox_temp():
    """临时目录放进工作区（沙箱只允许写工作区内的路径）。"""
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _mk(ws: Path, rel: str, text: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main() -> int:
    print("临时方案台账自检")

    print("[1] 扫得到各种标记")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "a.py", "def f():\n    # TODO: 先这样，明天再改\n    return 1\n")
        _mk(ws, "b.py", "x = 1  # HACK 临时绕过校验\n")
        _mk(ws, "c.py", "def g():\n    return 2  # 正常注释\n")
        found = debt.scan(ws)
        marks = {(r["file"], r["marker"]) for r in found}
        check("TODO 被扫到", ("a.py", "TODO") in marks, str(marks))
        check("HACK/临时 被扫到", ("b.py", "HACK") in marks, str(marks))
        check("正常注释不被误报", all(r["file"] != "c.py" for r in found), str(marks))

    print("[2] 台账：第一次记 first_seen，之后只更新行号")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "a.py", "\n\n# TODO: 临时方案\nx = 1\n")
        r1 = debt.sync(ws, "M", 0)
        check("第一次扫到 1 条新条目", len(r1["new"]) == 1, str(r1["new"]))
        _mk(ws, "a.py", "\n# TODO: 临时方案\nx = 1\n")     # 行号变了
        r2 = debt.sync(ws, "M", 1)
        check("第二次不重复记账", not r2["new"], str(r2["new"]))
        open_items = debt.load(ws)
        check("行号跟着更新", any(i["line"] == 2 for i in open_items), str(open_items))
        check("首次轮次保留（可追溯）", any(i["first_seen_round"] == 0 for i in open_items))

    print("[3] 修好了 → 标记 resolved（不删历史）")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "a.py", "# TODO: 临时\nx = 1\n")
        debt.sync(ws, "M", 0)
        _mk(ws, "a.py", "# 已按最新写法重写\nx = 1\n")
        r = debt.sync(ws, "M", 1)
        check("被清掉的条目进了 resolved", len(r["resolved"]) == 1, str(r["resolved"]))
        rows_raw = debt.load(ws, fold=False)      # 审计流：只追加，历史一条不丢
        folded = debt.load(ws)                    # 折叠后的最新状态
        check("历史还在（不是删除）", len(rows_raw) >= 2, str(len(rows_raw)))
        check("折叠后没有未清条目", not [x for x in folded if not x.get("resolved")],
              str(folded))

    print("[4] 到期判红：分级不同")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "a.py", "# TODO: 临时\nx = 1\n")
        debt.sync(ws, "L", 0)                                  # L 级 lifetime=3
        g0 = debt.gate(ws, "L", 0)
        check("未到期 → 通过", g0["ok"], str(g0))
        g3 = debt.gate(ws, "L", 3)
        check("L 级到期 → 判红", not g3["ok"], str(g3))
        check("红的时候给出文件行号", "a.py" in g3["detail"], g3["detail"])
        g3m = debt.gate(ws, "M", 3)
        check("M 级到期 → 只报告（不红）", g3m["ok"] and g3m["expired"] == 1, str(g3m))
        g3s = debt.gate(ws, "S", 3)
        check("S 级 → 跳过硬清理", g3s["ok"] and g3s["mode"] == "skip", str(g3s))
        # 每级寿命不同
        check("L 级寿命是 3 轮", debt.LIFETIME["L"] == 3, str(debt.LIFETIME))

    print("[5] 报告小节写清台账与到期")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "a.py", "# HACK 临时\nx = 1\n")
        debt.sync(ws, "M", 0)
        lines = debt.report_lines(ws, "M", 6)
        txt = "\n".join(lines)
        check("有台账小节标题", "临时方案台账" in txt, txt[:80])
        check("写清台账文件路径", debt.LEDGER in txt)
        check("列出未清条目", "a.py" in txt and "HACK" in txt)

    print("[6] 已接进夜班")
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("开跑同步台账", "_debt0.sync(ws" in comp)
    check("交付报告含台账小节", "_debt.report_lines" in comp)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（扫得到 / 不重复记账 / 修好即 resolved / 到期按级处理）")
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
