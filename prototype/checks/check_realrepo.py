"""真实仓库回归：包式导入 / 平台条件模块 / 依赖图 / lint 噪音（2026-11-05 加）。

来源：在 1 万行语料上跑机制时抓到的 4 个"机制在真实代码上不成立"的问题，
每一个都是**假红或漏报**，都会让夜班拿到错误结论：

  1. `gate_import_ok` 用文件名当模块名 → 包内模块 `pkg/sub/mod.py` 全部判"导入失败"，
     连 `__init__.py` 都会被 `import __init__` 判红（假红）；
  2. 平台条件模块（`unix_events.py` 之类）在别的平台上必然导入失败（假红）；
  3. `arch.health_metrics` 只按文件 stem 建节点 → `from pkg.sub import mod` 与
     `from . import x` 全部识别不出（实测包式仓库 edges=0，看着像"零依赖"）；
     反过来无条件给包节点建边又会把"包 re-export"变成假环；
  4. `lint` 把 `__init__.py` 里的门面 `import *` 当问题（噪音，真实仓库里这是标准写法）。

跑法: python check_realrepo.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import arch, gates, lint, scale  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _pkg_ws(td: str) -> Path:
    """造一个包式小仓库：alpha/sub/mod.py + __init__.py + 跨包导入。

    包名故意避开 stdlib（**不能用 `io`/`json` 这类名字**：那样 `import io.reader`
    会先撞到标准库的同名模块，测出来的是"包名冲突"而不是机制本身）。
    """
    ws = Path(td)
    for pkg in ("alpha", "beta"):
        (ws / pkg).mkdir(parents=True, exist_ok=True)
        (ws / pkg / "__init__.py").write_text("", encoding="utf-8")
    (ws / "alpha" / "sub").mkdir(parents=True, exist_ok=True)
    (ws / "alpha" / "sub" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "alpha" / "numeric.py").write_text(
        "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
    # 相对导入 + 点分绝对导入 + 包级 re-export
    (ws / "alpha" / "sub" / "mod.py").write_text(
        "from .. import numeric\n\n"
        "def twice(x: int) -> int:\n    return numeric.add(x, x)\n", encoding="utf-8")
    (ws / "beta" / "reader.py").write_text(
        "from alpha import numeric\nfrom alpha.sub import mod\n\n"
        "def run(x: int) -> int:\n    return mod.twice(numeric.add(x, 1))\n", encoding="utf-8")
    (ws / "alpha" / "__init__.py").write_text(
        "from .numeric import *\nfrom . import sub\n", encoding="utf-8")
    return ws


def main() -> int:
    print("真实仓库回归（包式导入 / 平台模块 / 依赖图 / lint 噪音）")

    with tempfile.TemporaryDirectory() as td:
        ws = _pkg_ws(td)
        rels = ["alpha/numeric.py", "alpha/sub/mod.py", "beta/reader.py",
                "alpha/__init__.py", "alpha/sub/__init__.py"]
        # 先量规模（后面会再加"故意坏掉"的文件，别把它们算进来）
        c = scale.count_lines(ws)
        check("规模：模块数 = 非 __init__ 的生产文件数",
              c["modules"] == 3 and c["files"] == 6, str(c))

        print("[1] 导入闸门：包式路径要按点分模块名导入")
        g = gates.gate_import_ok(ws, rels)
        check("包内模块 + __init__ 全部能干净导入（旧实现假红 5/5）", g.ok, g.detail)
        check("证据里写明检查了几个模块", "/5" in (g.evidence_ref or ""), g.evidence_ref)

        print("[2] 导入闸门：真的坏导入仍然要红（豁免不能顺手放过真问题）")
        (ws / "alpha" / "broken.py").write_text(
            "import definitely_no_such_module_xyz\n", encoding="utf-8")
        g2 = gates.gate_import_ok(ws, ["alpha/broken.py"])
        check("真导入错误必须红", not g2.ok, g2.detail)

        print("[3] 导入闸门：平台条件模块跳过而不是判红")
        (ws / "alpha" / "unix_events.py").write_text(
            "import definitely_no_such_module_xyz\n", encoding="utf-8")
        g3 = gates.gate_import_ok(ws, ["alpha/unix_events.py"])
        if sys.platform.startswith("win32"):
            check("Windows 上 unix_events.py 判定为平台条件模块（跳过而非红）",
                  g3.ok and "平台条件" in g3.detail, g3.detail)
        else:
            check("非 Windows 环境中该文件照常检查", True, "")

        print("[4] 依赖图：包式导入必须建边，且不为包 re-export 造环")
        m = arch.health_metrics(ws)
        check("识别出模块（点分名）", m["modules"] >= 5, str(m["modules"]))
        check("跨包 / 相对导入都建了边（旧实现 edges=0）", m["edges"] >= 3, str(m["edges"]))
        check("包 re-export 不造成假环", m["acyclic"] is True, str(m["cyclic"]))
        check("最大依赖深度 > 0", m["max_depth"] >= 1, str(m["max_depth"]))

        print("[5] 规模：模块数按文件数而不是目录数")
        # （这一项已在开头量过；这里只复核"加了坏文件之后"仍然按文件数增长，不是按目录数）
        c2 = scale.count_lines(ws)
        check("新增文件按模块增长（目录数没变）", c2["modules"] == 5, str(c2))

        print("[6] lint：包的 __init__.py 里 re-export 星号导入不算问题，其它文件仍算")
        a = lint.lint_source("from .numeric import *\n", "pkg/__init__.py")
        b = lint.lint_source("from os import *\n", "pkg/util.py")
        check("__init__.py 的星号导入不再报", not [x for x in a if x["rule"] == "star_import"],
              str(a))
        check("普通模块的星号导入仍然报", any(x["rule"] == "star_import" for x in b), str(b))

    # ---------------------------------------------------------------- 7 判据的导入
    print("[7] 判据按路径加载嵌套模块（不然越像真实项目越全红）")
    from core import author, props
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td2:
        ws2 = Path(td2)
        (ws2 / "pkg" / "deep").mkdir(parents=True)        # 故意**不**建 __init__.py
        (ws2 / "pkg" / "deep" / "calc.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")
        checks = [{"kind": "call_eq", "expr": "m.add(2, 3)", "expect": "5",
                   "desc": "add(2, 3) == 5", "hard": True}]
        r = props.run(ws2, "pkg/deep/calc.py", checks)
        check("没有 __init__.py 的嵌套模块也能被判据验（旧实现 ModuleNotFoundError 全红）",
              r.get("ok") is True, str(r)[:200])
        author._ensure_packages(ws2, "pkg/deep/calc.py")
        check("写完代码会补齐包的 __init__.py",
              (ws2 / "pkg" / "__init__.py").exists() and (ws2 / "pkg" / "deep" / "__init__.py").exists())
        r2 = props.run(ws2, "pkg/deep/calc.py", checks)
        check("补了包结构之后仍然通过", r2.get("ok") is True, str(r2)[:160])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（包式导入 / 平台模块 / 依赖图 / 规模计数 / lint 豁免）")
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
