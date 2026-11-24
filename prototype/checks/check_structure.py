"""交付质量闸门自检：结构体检 + 写次数上限 + e2e 不许被粉饰。

三条都来自 2026-11-04 的实测事故：
  ① 整文件模式报"8/8 已验收"，而交付的 store.py **少了一整列**（birth）
     —— 单函数判据只测空输入，看不见列缺失；只有结构体检和 e2e 能看见。
  ② bodies 模式交付过：整模块**没有 import sqlite3**、`_row_values` 用着上一轮的
     常量名 `_PEOPLE_COLUMNS`（文件里叫 `_FIELDS`）→ 只在调用时 NameError。
  ③ 同一模块最多被重写 **24 次**，占某次运行 59~86% 的花费。

跑法: python check_structure.py
"""
from __future__ import annotations


def _sandbox_temp():
    """临时目录放进工作区（沙箱只允许写工作区内的路径）。"""
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import gates  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


HEALTHY = '''"""正常的模块。"""

import sqlite3

_COLS = ("id", "name")


def connect() -> sqlite3.Connection:
    return sqlite3.connect(":memory:")


def fetch_all(conn) -> list[dict]:
    cur = conn.execute("SELECT * FROM people")
    return [{c: v for c, v in zip(_COLS, row)} for row in cur.fetchall()]
'''

LOST_IMPORT = '''"""少了 import 的模块。"""


def connect():
    return sqlite3.connect(":memory:")
'''

STALE_CONST = '''_FIELDS = ("id", "name")


def row_values(row):
    return tuple(row.get(k) for k in _PEOPLE_COLUMNS)
'''

DUPES = '''def helper():
    return 1


def helper():
    return 2
'''

STUB = '''def todo(rows):
    raise NotImplementedError
'''


def main() -> int:
    print("交付质量闸门自检")

    print("[1] 结构体检：真缺陷必须红")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "healthy.py").write_text(HEALTHY, encoding="utf-8")
        (ws / "lost_import.py").write_text(LOST_IMPORT, encoding="utf-8")
        (ws / "stale.py").write_text(STALE_CONST, encoding="utf-8")
        (ws / "dupes.py").write_text(DUPES, encoding="utf-8")
        (ws / "stub.py").write_text(STUB, encoding="utf-8")

        g = gates.gate_structure(ws, ["healthy.py"])
        check("健康的模块 → 通过（不误杀）", g.ok, g.detail)

        g = gates.gate_structure(ws, ["lost_import.py"])
        check("丢了 import sqlite3 → 红", not g.ok and "sqlite3" in g.detail, g.detail)

        g = gates.gate_structure(ws, ["stale.py"])
        check("用了上一轮才有的常量名 → 红（这条就是当时 e2e 才抓到的 NameError）",
              not g.ok and "_PEOPLE_COLUMNS" in g.detail, g.detail)

        g = gates.gate_structure(ws, ["dupes.py"])
        check("同名定义出现两次 → 红", not g.ok and "同名定义" in g.detail, g.detail)

        g = gates.gate_structure(ws, ["stub.py"], contract_funcs=["todo"])
        check("契约函数还是桩 → 红", not g.ok and "未实现" in g.detail, g.detail)
        g = gates.gate_structure(ws, ["stub.py"], contract_funcs=["other"])
        check("不是契约函数 → 不追究（只盯契约里的函数）", g.ok, g.detail)

        g = gates.gate_structure(ws, ["healthy.py", "lost_import.py"])
        check("多文件一起体检 → 汇总报出坏的那个", not g.ok and "lost_import" in g.detail,
              g.detail)

    print("[2] 未定义名字：不许误杀正常写法")
    with _sandbox_temp() as td:
        ws = Path(td)
        tricky = '''import json


class Box:
    def __init__(self, items):
        self.items = items

    def total(self):
        return sum(x * 2 for x in self.items)


def build(data, *args, **kw):
    local, sep, domain = "a@b", "@", "b"
    obj = {k: v for k, v in (data or {}).items()}
    fmt = f"{local}{sep}{domain}"
    try:
        n = int(fmt)
    except ValueError as exc:
        n = len(str(exc))
    with open(__file__) as fh:
        head = fh.readline()
    return json.dumps({"n": n, "obj": obj, "head": head, "box": Box([1]).total()})


def outer():
    def inner():
        return 1
    return inner()
'''
        (ws / "tricky.py").write_text(tricky, encoding="utf-8")
        g = gates.gate_undef(ws, ["tricky.py"])
        check("解包/推导式/except as/with as/内嵌函数/形参 都不误报", g.ok, g.detail)

        (ws / "bad.py").write_text("def f(x):\n    return MISSING_CONST + x\n", encoding="utf-8")
        g = gates.gate_undef(ws, ["bad.py"])
        check("真的未定义 → 红", not g.ok and "MISSING_CONST" in g.detail, g.detail)

        (ws / "star.py").write_text("from os.path import *\n\ndef f():\n    return join('a')\n",
                                    encoding="utf-8")
        g = gates.gate_undef(ws, ["star.py"])
        check("星号导入 → 静态判不了，放过（不误杀）", g.ok, g.detail)

    print("[3] 真实样本回放：当时的两份坏交付必须被判红")
    root = Path(__file__).resolve().parents[2] / "projects"
    for ws_name in ("ab_bodies", "ab_granular2", "ab_granular3"):
        ws = root / ws_name
        if not ws.exists():
            print(f"     （跳过回放：{ws_name} 不存在）")
            continue
        g = gates.gate_structure(ws, ["clean.py", "store.py", "report.py"])
        check(f"{ws_name} 的坏交付被判红", not g.ok, g.detail[:120])
    for ws_name in ("ab_whole", "ab_system"):
        ws = root / ws_name
        if not ws.exists():
            continue
        g = gates.gate_structure(ws, ["clean.py", "store.py", "report.py"])
        check(f"{ws_name} 的好交付不被误杀", g.ok, g.detail[:120])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（结构体检辨真伪 / 未定义名字不误杀 / 真实样本回放一致）")
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
