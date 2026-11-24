"""自检：验证器白名单（远端点菜 / 本地执行 / 本地判定）。

为什么必须有（2026-11-15）：一旦允许远端"自己决定跑什么验证"，最快的堕落路径是
"自己出题自己判" —— 模型可以点一个永远为绿的检查，然后宣称修好了。
本自检把边界钉死：**只认白名单、只读、有上限、结果结构化回灌**。

跑法: python checks/check_validators.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE.parent
sys.path.insert(0, str(PROTO))

from core import validators as V                                  # noqa: E402
from core import wholerepo as W                                   # noqa: E402

FAILS: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  PASS ", name)
    else:
        print("  FAIL ", name, "|", str(detail)[:300])
        FAILS.append(name)


def make_ws(root: Path) -> Path:
    (root / "apps" / "demo").mkdir(parents=True, exist_ok=True)
    (root / "apps" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "demo" / "__init__.py").write_text("", encoding="utf-8")
    (root / "apps" / "demo" / "views.py").write_text(
        "def double(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
    return root


def main() -> int:
    print("[1] 菜单：只列白名单，未装工具如实标不可用")
    m = V.menu()
    ck("菜单含 py.mypy / py.ruff / py.migrate",
       all(x in m for x in ("py.mypy", "py.ruff", "py.migrate")), m[:200])
    ck("未安装的工具标「不可用」，不装样子",
       "不可用" in m and "bandit" in m, m[-300:])

    print()
    print("[2] 解析：`<verify>RUN: id</verify>` + WHY")
    p = V.parse_verify("<verify>\nRUN: py.mypy\nRUN: `py.migrate`\nWHY: 类型漂移\n</verify>")
    ck("解出两个 id", p["ids"] == ["py.mypy", "py.migrate"], p)
    ck("解出 WHY", p["why"] == "类型漂移", p)
    ck("没有 verify 段 → 空", V.parse_verify("=== FILE: a.py ===\nx=1\n")["ids"] == [], "")

    print()
    print("[3] 白名单 / 上限 / 不可用")
    with tempfile.TemporaryDirectory() as td:
        ws = make_ws(Path(td))
        r = V.run(ws, ["py.nope", "sec.bandit", "py.compile"])
        ck("白名单外的 id 被拒", [x["id"] for x in r["rejected"]] == ["py.nope"], r)
        ck("不可用的 id 报 unavailable（不是静默跳过）",
           [x["id"] for x in r["unavailable"]] == ["sec.bandit"], r)
        ck("白名单内的真跑了", [x["id"] for x in r["ran"]] == ["py.compile"], r)
        ck("**不设个数上限**（MAX_PER_ROUND is None）",
           V.MAX_PER_ROUND is None, V.MAX_PER_ROUND)
        r2 = V.run(ws, ["d1.syntax", "d4.crossref", "py.compile", "py.ruff",
                        "py.mypy", "py.tests"])
        ck("默认情况下 6 个全跑（不受个数限制）", len(r2["ran"]) == 6, r2)
        r2b = V.run(ws, ["d1.syntax", "d4.crossref", "py.compile"], total_timeout=1)
        ck("时间预算用尽 → 排队（不拒绝、不丢）",
           r2b["deferred"] and not r2b["rejected"], r2b)

        print()
        print("[4] 真判据：类型错要红、改完要绿（不是空跑）")
        bad = ws / "apps" / "demo" / "views.py"
        bad.write_text("def double(x: int) -> int:\n    return 'oops'\n", encoding="utf-8")
        r3 = V.run(ws, ["py.mypy"])
        ck("返回值类型不符 → py.mypy 判红",
           r3["ran"] and r3["ran"][0]["ok"] is False, r3)
        bad.write_text("def double(x: int) -> int:\n    return x * 2\n", encoding="utf-8")
        r4 = V.run(ws, ["py.mypy"])
        ck("改回正确 → py.mypy 判绿", r4["ran"] and r4["ran"][0]["ok"] is True, r4)

        print()
        print("[5] 只读：验证器不许改产物")
        before = {p: p.stat().st_mtime_ns for p in ws.rglob("*.py")}
        V.run(ws, ["d1.syntax", "d4.crossref", "py.compile", "py.ruff"])
        after = {p: p.stat().st_mtime_ns for p in ws.rglob("*.py")}
        ck("跑完所有验证器后源码 mtime 不变", before == after,
           [str(p) for p in before if before[p] != after.get(p)])

        print()
        print("[6] 结果落盘 + 回灌文本")
        V.save(ws, r4, round_no=3)
        back = V.load(ws)
        ck("落盘可读回", back.get("ran") and back.get("round") == 3, back)
        txt = V.render_results(back)
        ck("渲染成给远端看的文本（含通过/失败）", "py.mypy" in txt and "通过" in txt, txt)

        print()
        print("[6b] 队列：本轮跑不完的下一轮补跑，不丢请求")
        V._save_queue(ws, [])
        r6 = V.run_with_queue(ws, ["d1.syntax", "py.compile"], max_n=1)
        ck("超额的进了队列文件",
           V._load_queue(ws) == ["py.compile"], V._load_queue(ws))
        ck("本轮只跑 1 个", len(r6["ran"]) == 1, r6)
        r7 = V.run_with_queue(ws, [])
        ck("下一轮即使不点名，也会补跑欠的", any(x["id"] == "py.compile"
                                                for x in r7["ran"]), r7)
        ck("补完后队列清空", V._load_queue(ws) == [], V._load_queue(ws))

        print()
        print("[7] 提示词里带菜单与点名格式（远端才知道能点）")
        br = W.brief(ws, W.SimpleReport("报告", "short"), files={"a.py": "x=1\n"},
                     stats={"files": 1, "chars": 6})
        ck("brief 含 <verify> 段格式", "<verify>" in br and "RUN:" in br)
        ck("brief 含菜单 id", "py.mypy" in br and "py.migrate" in br)
        ck("brief 里 @MENU@ 已被替换（不留占位符）", "@MENU@" not in br)

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过（菜单口径 / 解析 / 白名单 / 上限 / 真判据 / 只读 / 回灌 / 提示词）")
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
