"""读取策略自检（开发文档 §8.5）。

判据不是"任务名字"，而是"要做什么决定，就需要什么强度的证据"：
  T0 接口摘要 / T1 文件全文 / T2 调用链 / T3 全仓结构。

这条策略要钉住三件事：
  1. 该读全文的任务（edit_existing / fix_bug）**必须**包含全文；
  2. 该读结构的任务（skeleton）**不能**把函数体也读进来（3 万行全文读不起）；
  3. 写前门禁必须用"本次上下文里给过全文"这个事实，而不是"我调过读文件函数"。

跑法: python checks/check_readplan.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import readplan  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make_ws() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="rp_"))
    (ws / "app").mkdir()
    (ws / "app/__init__.py").write_text("", encoding="utf-8")
    (ws / "app/store.py").write_text(
        'def save(x: int) -> int:\n    """存一个数。"""\n    return x\n', encoding="utf-8")
    (ws / "app/api.py").write_text(
        'from app.store import save\n\n\ndef handle(x: int) -> int:\n    return save(x)\n',
        encoding="utf-8")
    (ws / "app/big.py").write_text(
        "".join(f"\n\ndef f{i}(x: int) -> int:\n    return x\n" for i in range(600)),
        encoding="utf-8")
    return ws


def main() -> int:
    ws = make_ws()

    print("[1] 分类：任务 → 档位")
    p1 = readplan.plan("new_module", target="app/new.py", modules=["app/store.py"])
    check("new_module → T0", p1["tiers"] == ["T0"], str(p1["tiers"]))
    check("每个计划都有 why", bool(p1["why"]))

    p2 = readplan.plan("edit_existing", target="app/store.py")
    check("edit_existing → 含 T1", "T1" in p2["tiers"], str(p2["tiers"]))
    check("edit_existing 把目标列进待读", p2["files"] == ["app/store.py"], str(p2["files"]))

    p3 = readplan.plan("fix_bug", target="app/store.py")
    check("fix_bug → T1+T2（症状与病因都要看）", set(p3["tiers"]) == {"T1", "T2"}, str(p3["tiers"]))

    p4 = readplan.plan("change_contract", target="app/store.py")
    check("change_contract → T2", p4["tiers"] == ["T2"], str(p4["tiers"]))

    p5 = readplan.plan("skeleton")
    check("skeleton → T3", p5["tiers"] == ["T3"], str(p5["tiers"]))
    check("未知 kind 有兜底（不抛异常）", readplan.plan("whatever", target="a.py")["kind"] == "edit_existing")

    print("[2] collect：该给全文的给全文，该给结构的只给结构")
    g2 = readplan.collect(ws, p2, budget_chars=12000)
    check("全文真的进来了", "def save(x: int) -> int:" in g2["text"], g2["text"][:120])
    check("记下读过哪个文件", g2["files"] == ["app/store.py"], str(g2["files"]))
    check("小文件 full=True", g2["full"] is True)

    g4 = readplan.collect(ws, readplan.plan("edit_existing", target="app/big.py"), budget_chars=12000)
    check("大文件 full=False", g4["full"] is False, str(g4["chars"]))
    check("大文件明确告知禁止整篇重写", "禁止整篇重写" in g4["text"], "没告知 = 模型会硬写")

    g5 = readplan.collect(ws, p5, budget_chars=12000)
    check("T3 有文件清单", "app/store.py:" in g5["text"], g5["text"][:120])
    check("T3 只有签名不含函数体", "return x" not in g5["text"], "T3 读进了实现 = 白烧 token")

    print("[3] fix_bug 要带调用链")
    g3 = readplan.collect(ws, p3, budget_chars=12000)
    check("调用链（反向依赖）被带上", "app/api.py" in g3["text"], str(g3["files"]))

    print("[4] 写前门禁：看的是'这次给过全文没有'")
    ok, why = readplan.write_allowed(p2, "app/store.py", g2)
    check("读过全文 → 允许写", ok, why)
    ok2, why2 = readplan.write_allowed(p2, "app/big.py", g4)
    check("只给头尾 → 不许写", ok2 is False and "全文" in why2, why2)
    ok3, why3 = readplan.write_allowed(p2, "app/api.py", g2)
    check("本次没读过 → 不许写", ok3 is False and "没读过" in why3, why3)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（分类正确 / 该读全文读全文 / 结构不读实现 / 调用链带上 / 写前门禁看事实）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
