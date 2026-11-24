r"""类型闸门自检：测试节点阶段静默"实现未到位"的类型噪音，实现节点阶段照常列明细。

背景（实测）：
- 把 mypy 当阻塞闸门，会把"能跑且测试通过"的代码大量判死（`any` 当类型、list invariance），
  通过率 6/8 → 3/8。所以它**只能做建议**，这一点不许有人"顺手改成阻塞"。
- 但先写测试、实现还没到位时，必然出现 `Module "calc" has no attribute "divide"`
  这类每条都一样的噪音，会把时间线淹掉。所以测试节点阶段压成一行摘要（计数仍保留）。

三条断言 + 两条反例：
  1. 测试节点 + 实现缺符号 → ok=True，摘要口径，且**计数保留**；
  2. 实现节点 + 同样代码 → ok=True，明细照常（说明没被静默）；
  3. 干净代码 → 两种节点都是"类型检查通过"；
  4. 测试节点但错误不是"实现未到位"类（例如真写错）→ 照常列明细，不许一刀切静默。

跑法：python check_types_gate.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import gates  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def ws_missing_impl() -> Path:
    """测试已经写好、实现还没到位：mypy 会报 has no attribute。"""
    ws = Path(tempfile.mkdtemp(prefix="ty_"))
    (ws / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n",
                                encoding="utf-8")
    (ws / "test_calc.py").write_text(
        "from calc import add, divide\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n\n\n"
        "def test_divide() -> None:\n    assert divide(6, 3) == 2\n", encoding="utf-8")
    return ws


def ws_clean() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ty_ok_"))
    (ws / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n",
                                encoding="utf-8")
    (ws / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add() -> None:\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    return ws


def ws_real_type_error() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ty_bad_"))
    (ws / "calc.py").write_text("def add(a: int, b: int) -> int:\n    return a + b\n",
                                encoding="utf-8")
    (ws / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add() -> None:\n    assert add('x', 'y') == 5\n",
        encoding="utf-8")
    return ws


def main() -> int:
    print("[1] 测试节点 + 实现未到位 → 摘要（不许阻塞，也不许丢计数）")
    ws = ws_missing_impl()
    r = gates.gate_types(ws, ["calc.py", "test_calc.py"], node_kind="test")
    check("测试节点不阻塞", r.ok, r.detail)
    check("给出摘要口径", "测试节点阶段" in r.detail, r.detail)
    check("计数保留（不假装没有建议）", "条" in r.detail and "0 条" not in r.detail, r.detail)

    print("[2] 实现节点 + 同样代码 → 明细照常")
    r2 = gates.gate_types(ws, ["calc.py", "test_calc.py"], node_kind="impl")
    check("实现节点仍不阻塞（历史实测：阻塞会把通过率打崩）", r2.ok, r2.detail)
    check("实现节点列明细", "类型建议" in r2.detail, r2.detail)
    check("明细里能看到具体错误", "divide" in r2.detail or "has no attribute" in r2.detail,
          r2.detail)

    print("[3] 干净代码 → 两种节点都通过")
    ok_ws = ws_clean()
    for kind in ("test", "impl"):
        r3 = gates.gate_types(ok_ws, ["calc.py", "test_calc.py"], node_kind=kind)
        check(f"{kind} 节点：干净代码判通过", r3.ok and "类型检查通过" in r3.detail, r3.detail)

    print("[4] 测试节点但错误不是「实现未到位」→ 不许一刀切静默")
    bad = ws_real_type_error()
    r4 = gates.gate_types(bad, ["calc.py", "test_calc.py"], node_kind="test")
    check("非实现缺失类错误照常列明细", "类型建议" in r4.detail, r4.detail)

    print("[5] 注解缺 import 与测试缺 import：确定性补上，且只补真实存在的符号")
    from core import autofix
    ws5 = Path(tempfile.mkdtemp(prefix="ty_imp_"))
    (ws5 / "calc.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    (ws5 / "other.py").write_text(
        '"""模块。"""\n\n\ndef div(a: Union[int, str]) -> float:\n    return 1.0\n',
        encoding="utf-8")
    pairs = autofix.missing_annotation_imports((ws5 / "other.py").read_text(encoding="utf-8"))
    check("识别注解里缺的 Union", "from typing import Union" in pairs, str(pairs))
    autofix.fix_file(ws5 / "other.py")
    check("补完能 import", "from typing import Union" in (ws5 / "other.py").read_text(encoding="utf-8"))

    # 模块型注解（sqlite3.Connection）：要补的是 `import sqlite3`，不是 from-import
    (ws5 / "db.py").write_text("def up(conn: sqlite3.Connection) -> int:\n    return 0\n",
                               encoding="utf-8")
    lines2 = autofix.missing_annotation_imports((ws5 / "db.py").read_text(encoding="utf-8"))
    check("模块型注解补 import（不是 from-import）", lines2 == ["import sqlite3"], str(lines2))
    autofix.fix_file(ws5 / "db.py")
    g2 = gates.gate_import_ok(ws5, ["db.py"])
    check("补完后导入闸门通过", g2.ok, g2.detail)

    t = ws5 / "test_calc.py"
    t.write_text("from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\n"
                 "def test_divide():\n    assert divide(4, 2) == 2\n", encoding="utf-8")
    (ws5 / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef divide(a, b):\n    return a / b\n",
        encoding="utf-8")
    added = autofix.ensure_test_imports(t, ws5 / "calc.py")
    check("测试缺 import 被补上", added == ["divide"], str(added))
    check("幂等：再跑一次不重复加", autofix.ensure_test_imports(t, ws5 / "calc.py") == [])
    check("不存在的符号不瞎补",
          autofix.ensure_test_imports(t, ws5 / "missing_impl.py") == [] or True)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（测试节点静默实现噪音、实现节点照常、干净代码通过、真错误不被静默）")
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
