"""跨模块引用修补自检（2026-11-07 真实夜班暴露的 bug）。

事故：`matching_core/da.py` 里写了 `run_batch(...)`，而 `run_batch` 就定义在
`app/services/batches.py` 里 —— 结构体检只报"本模块没有任何定义/导入"，
引擎判**整文件回滚**，模型下一轮再写一遍同样缺 import 的代码，来回烧钱。

口径（三件事必须分开，不许混）：
  1. 符号在别的模块存在 → 引擎补 `from <mod> import <name>`，0 token、确定性；
  2. 项目里根本没有这个符号 → 记**接口待办**（need_interfaces.jsonl），不假装补上；
  3. 多个同名提供方 → 不猜，记待办交给重规划/人工。

跑法: python checks/check_linkfix.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import gates, linkfix  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="lf_"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def main() -> int:
    ws = make({
        "app/__init__.py": "",
        "app/services/__init__.py": "",
        "app/services/batches.py": "def run_batch(batch_id):\n    return {}\n",
        "matching_core/da.py": "def da_match():\n    return run_batch(1)\n",
    })
    rel = "matching_core/da.py"
    g = gates.gate_structure(ws, [rel])
    check("结构体检先报未定义名字", (not g.ok) and "run_batch" in g.detail, g.detail)

    got = linkfix.findings(g.detail)
    check("能从 detail 取出缺失名字", got and got[0]["name"] == "run_batch", str(got))
    check("能找到提供方", linkfix.providers(ws, "run_batch") == ["app/services/batches.py"],
          str(linkfix.providers(ws, "run_batch")))

    r = linkfix.patch(ws, rel, g.detail)
    g2 = gates.gate_structure(ws, [rel])
    check("补 import 后结构体检转绿", g2.ok, g2.detail)
    check("补的是正确的模块", r["added"] == [{"name": "run_batch",
                                              "module": "app/services/batches.py"}], str(r))
    check("真的写进了文件",
          "from app.services.batches import run_batch" in (ws / rel).read_text(encoding="utf-8"))

    # 项目里没有这个符号 → 如实报 unknown，绝不伪造 import
    ws2 = make({"m.py": "def f():\n    return nothere(1)\n"})
    g3 = gates.gate_structure(ws2, ["m.py"])
    r3 = linkfix.patch(ws2, "m.py", g3.detail)
    check("不存在的符号如实报 unknown", [x["name"] for x in r3["unknown"]] == ["nothere"], str(r3))
    check("不存在的符号不写文件", "import nothere" not in (ws2 / "m.py").read_text(encoding="utf-8"))
    need = linkfix.record_need(ws2, "m.py", r3["unknown"])
    check("接口待办落盘", need.is_file() and "nothere" in need.read_text(encoding="utf-8"))

    # 多个同名提供方 → 不猜
    ws3 = make({
        "a/x.py": "def dup():\n    return 1\n",
        "b/y.py": "def dup():\n    return 2\n",
        "c/z.py": "def use():\n    return dup()\n",
    })
    g4 = gates.gate_structure(ws3, ["c/z.py"])
    r4 = linkfix.patch(ws3, "c/z.py", g4.detail)
    check("同名多提供方不猜", [x["name"] for x in r4["ambiguous"]] == ["dup"], str(r4))
    check("多提供方时不写文件", "import dup" not in (ws3 / "c/z.py").read_text(encoding="utf-8"))

    # 同子系统优先：候选里只有一个跟调用方同目录前缀时才选它
    ws4 = make({
        "app/a.py": "def helper():\n    return 1\n",
        "app/sub/b.py": "def helper():\n    return 2\n",
        "app/sub/c.py": "def use():\n    return helper()\n",
    })
    g5 = gates.gate_structure(ws4, ["app/sub/c.py"])
    r5 = linkfix.patch(ws4, "app/sub/c.py", g5.detail)
    check("同子系统优先", [a["module"] for a in r5["added"]] == ["app/sub/b.py"], str(r5))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    # 标准库缺失 import：确定性补（作者连漏 4 次 import sqlite3 的实测）
    ws2 = Path(tempfile.mkdtemp())
    f2 = ws2 / "m.py"
    f2.write_text("import threading\n"
                  "\n"
                  "\n"
                  "def conn() -> sqlite3.Connection:\n"
                  "    return sqlite3.connect(':memory:', check_same_thread=False)\n",
                  encoding="utf-8")
    r2 = linkfix.patch(ws2, "m.py",
                       "m.py:4 用了 sqlite3，但本模块没有任何定义/导入")
    ok2 = (r2["wrote"] and r2["added"] and r2["added"][0]["module"] == "stdlib"
           and "import sqlite3" in f2.read_text(encoding="utf-8"))
    check("标准库缺失 import 确定性补齐", ok2, str(r2)[:160])

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（存在就补 import / 不存在记待办 / 多提供方不猜 / 同子系统优先 / 标准库直补）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
