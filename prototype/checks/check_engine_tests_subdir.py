"""子目录模块的引擎测试自检（2026-11-07 真实夜班暴露的 bug）。

事故：夜班跑多模块 Django 项目时，日志出现
  notice: app/services/audit.py 没生成测试：引擎测试未通过：
  Hint: make sure your test modules/packages have valid Python names.
真因是 `props.render_pytest` 把测试文件写在仓库根、import 却用 `Path(path).stem`
（`from audit import ...`），而产物在 `app/services/audit.py` → ModuleNotFoundError。
引擎测试 100% 失败 → 测试文件被删 → 能力验收拿不到证据 → 夜班空转。

同时 `tp = "test_" + stem + ".py"` 会让 app/services/batches.py 与
app/api/batches.py 生成同名 test_batches.py 互相覆盖。

修复：带子目录的模块按绝对路径加载（不依赖包结构、不与同名模块串味），
测试文件名带目录前缀；根目录模块保持原 import 写法，行为不变。

跑法: python checks/check_engine_tests_subdir.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import gates, iterate, props  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def contract(fname: str, args: list[str], expect: str) -> NS:
    """只产生 call_eq 判据的可控契约（避开需求文本解析的中文噪音）。"""
    ex = NS(args=args, expect=expect)
    f = NS(name=fname, examples=[ex],
           params=[NS(name="a", type="int"), NS(name="b", type="int")],
           keys=[], raises=[], returns="int", is_method=lambda: False)
    return NS(raw="", funcs=[f])


def fresh() -> Path:
    return Path(tempfile.mkdtemp(prefix="sixcoder-subdir-check-"))


def main() -> int:
    print("子目录模块引擎测试自检")

    print("[1] 对照：旧写法（不带 target_rel）在子目录模块上必定失败")
    ws = fresh()
    (ws / "app" / "services").mkdir(parents=True)
    (ws / "app" / "services" / "audit.py").write_text(
        "def total(a, b):\n    return a + b\n", encoding="utf-8")
    checks = props.from_contract(contract("total", ["1", "2"], "3"))
    old = props.render_pytest("audit", checks)
    (ws / "test_audit.py").write_text(old, encoding="utf-8")
    r_old = gates.gate_tests(ws, ["test_audit.py"], timeout=90)
    check("旧写法确实复现 bug（测试判红）", not r_old.ok, r_old.detail[:160])
    check("失败原因就是找不到模块",
          "No module named" in r_old.detail or "valid Python names" in r_old.detail,
          r_old.detail[:160])

    print("[2] 修复：带 target_rel 时按绝对路径加载，测试通过")
    ws2 = fresh()
    (ws2 / "app" / "services").mkdir(parents=True)
    (ws2 / "app" / "services" / "audit.py").write_text(
        "def total(a, b):\n    return a + b\n", encoding="utf-8")
    new = props.render_pytest("audit", checks, target_rel="app/services/audit.py")
    check("渲染结果改用按路径加载", "spec_from_file_location" in new, new[:120])
    check("渲染结果不再有裸 from audit import", "from audit import" not in new, new[:120])
    (ws2 / "test_app_services_audit.py").write_text(new, encoding="utf-8")
    r_new = gates.gate_tests(ws2, ["test_app_services_audit.py"], timeout=90)
    check("修复后引擎测试真跑通", r_new.ok, r_new.detail[:200])

    print("[3] 端到端：add_engine_tests 对子目录模块能生成并留下测试")
    ws3 = fresh()
    (ws3 / "app" / "services").mkdir(parents=True)
    (ws3 / "app" / "services" / "audit.py").write_text(
        "def total(a, b):\n    return a + b\n", encoding="utf-8")
    res = iterate.add_engine_tests(ws3, {"app/services/audit.py": contract("total", ["1", "2"], "3")},
                                   "app/services/audit.py")
    check("add_engine_tests 返回 ok", bool(res.get("ok")), str(res)[:180])
    made = [p.name for p in ws3.glob("test_*.py")]
    check("测试文件带子目录前缀、没被删", "test_app_services_audit.py" in made, str(made))
    if made:
        check("留下的测试真能跑绿", gates.gate_tests(ws3, made, timeout=90).ok)

    print("[4] 同名不同目录：文件名不冲突、各自加载自己的实现")
    ws4 = fresh()
    (ws4 / "app" / "services").mkdir(parents=True)
    (ws4 / "app" / "api").mkdir(parents=True)
    (ws4 / "app" / "services" / "batches.py").write_text(
        "def which():\n    return 'services'\n", encoding="utf-8")
    (ws4 / "app" / "api" / "batches.py").write_text(
        "def which():\n    return 'api'\n", encoding="utf-8")
    svc = contract("which", [], "'services'")
    api = contract("which", [], "'api'")
    for rel, c in (("app/services/batches.py", svc), ("app/api/batches.py", api)):
        out = iterate.add_engine_tests(ws4, {rel: c}, rel)
        check(f"{rel} 生成成功", bool(out.get("ok")), str(out)[:160])
    names = sorted(p.name for p in ws4.glob("test_*.py"))
    check("两个测试文件都留下（不互相覆盖）", len(names) == 2, str(names))
    check("文件名按目录区分", "test_app_services_batches.py" in names
          and "test_app_api_batches.py" in names, str(names))
    if len(names) == 2:
        check("两份测试同时跑绿（没有串味）", gates.gate_tests(ws4, names, timeout=90).ok)

    print("[5] 回归：根目录模块行为不变")
    plain = props.render_pytest("index", checks)
    check("根目录仍用 from index import", "from index import" in plain, plain[:100])
    check("根目录不引入 importlib 加载", "spec_from_file_location" not in plain, plain[:100])

    print("[6] 渲染结果是合法 Python、关键字名不炸绑定行")
    import ast
    try:
        ast.parse(new)
        ok_syntax = True
    except SyntaxError as exc:
        ok_syntax = False
        print("   ", exc)
    check("渲染结果语法合法", ok_syntax)
    # 关键字不能当 Python 标识符：渲染器必须把它从 import/绑定列表里剔掉，
    # 否则 `class = getattr(m, 'class')` 直接语法错（新增 keyword 过滤就是防这个）。
    kw_checks = [{"kind": "call_eq", "expr": "m.class(1)", "expect": "1",
                  "hard": True, "desc": "关键字名"}]
    try:
        kw = props.render_pytest("audit", kw_checks, target_rel="app/services/audit.py")
        ok_kw = "class = getattr(" not in kw and "from audit import class" not in kw
    except Exception as exc:  # noqa: BLE001
        ok_kw = False
        print("   ", type(exc).__name__, exc)
    check("关键字名不会生成非法绑定行", ok_kw)
    check("props 里 keyword 已导入", "import keyword" in
          (ROOT / "core" / "props.py").read_text(encoding="utf-8"))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（子目录按路径加载 / 同名不覆盖 / 根目录行为不变）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
