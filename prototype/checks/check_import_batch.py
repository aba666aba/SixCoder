"""导入闸门批量化自检：一次子进程 import 全部，但结果必须与逐个**完全一致**。

这条钉的是开发文档 §18.7 第 4 件（批量验证）：
    · 旧实现每文件起一个 Python 进程（33 文件 6–10s），1000 文件必超时；
    · 批量 = 单进程 + 清单文件 + importlib，一次启动、一个 sys.path；
    · **性能优化不许换正确性**：批量子进程崩 → 回退逐个；平台条件模块照旧跳过；
    · 结果口径（findings 文本、跳过说明）与旧实现逐字一致。

跑法: python checks/check_import_batch.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import gates  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


GOOD = "def add(a, b):\n    return a + b\n"
BAD = "def f(x: MissingType):\n    return x\n"       # 导入即 NameError
OTHER = "def mul(a, b):\n    return a * b\n"


def _mkws() -> Path:
    td = tempfile.mkdtemp(prefix="imp_batch_")
    root = Path(td)
    (root / "good_mod.py").write_text(GOOD, encoding="utf-8")
    (root / "bad_import.py").write_text(BAD, encoding="utf-8")
    (root / "another.py").write_text(OTHER, encoding="utf-8")
    (root / "unix_events.py").write_text(GOOD, encoding="utf-8")  # Windows 下应跳过
    return root


def main() -> int:
    print("导入闸门批量化自检（一次 import 全部 / 结果与逐个一致 / 崩溃回退 / 平台跳过）")

    root = _mkws()
    paths = ["good_mod.py", "bad_import.py", "another.py", "unix_events.py"]

    print("[1] 批量：一个坏导入仍红，平台条件模块仍跳过")
    r = gates.gate_import_ok(Path(root), paths)
    check("整体判红（有坏导入）", r.ok is False, r.detail)
    check("findings 点名 bad_import", "bad_import.py" in r.detail, r.detail)
    check("findings 带真实错误类型", "NameError" in r.detail, r.detail)
    check("good_mod 不误伤", "good_mod.py" not in r.detail)
    # 平台跳过说明在旧实现里只在"全绿"时进 detail（有 findings 时只报 findings），
    # 这里用独立场景单独验证它仍然生效。
    r_skip = gates.gate_import_ok(Path(root), ["good_mod.py", "unix_events.py"])
    check("平台条件模块被跳过并写清（全绿口径）",
          "平台条件模块跳过" in r_skip.detail and "unix_events" in r_skip.detail,
          r_skip.detail)

    print("[2] 批量只起一个子进程（不是 3 个文件各起一个）")
    real_run = subprocess.run
    calls = {"n": 0}

    def counting(*a, **k):
        calls["n"] += 1
        return real_run(*a, **k)

    subprocess.run = counting
    try:
        gates.gate_import_ok(Path(root), ["good_mod.py", "another.py"])
    finally:
        subprocess.run = real_run
    check("3 个文件只起 1 个子进程", calls["n"] == 1, f"实际 {calls['n']} 次")

    print("[3] 全绿口径不变：detail 与旧实现一致")
    r_ok = gates.gate_import_ok(Path(root), ["good_mod.py", "another.py"])
    check("两个正常模块 → 全绿", r_ok.ok is True and "2 个模块都能干净导入" in r_ok.detail,
          r_ok.detail)

    print("[4] 批量子进程崩溃 → 回退逐个，结果仍对（不许把测不了伪装成通过）")
    orig_batch = gates._batch_import
    gates._batch_import = lambda *a, **k: {}        # 模拟批量崩了
    try:
        r_fb = gates.gate_import_ok(Path(root), ["good_mod.py", "bad_import.py"])
    finally:
        gates._batch_import = orig_batch
    check("回退后坏导入仍红", r_fb.ok is False and "bad_import.py" in r_fb.detail, r_fb.detail)
    check("回退后正常模块不误伤", "good_mod.py" not in r_fb.detail)

    print("[5] 包结构：批量路径下包内模块仍能正确 import")
    pkg = Path(tempfile.mkdtemp(prefix="imp_pkg_"))
    (pkg / "cal").mkdir()
    (pkg / "cal" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cal" / "core").mkdir()
    (pkg / "cal" / "core" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cal" / "core" / "numeric.py").write_text("def sq(x):\n    return x * x\n",
                                                     encoding="utf-8")
    r_pkg = gates.gate_import_ok(pkg, ["cal/core/numeric.py"])
    check("包内模块 `import cal.core.numeric` 全绿", r_pkg.ok is True, r_pkg.detail)

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（坏导入红 / 单子进程 / 全绿口径一致 / 崩溃回退 / 包结构正确）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
