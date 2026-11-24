"""空壳测试自检：**"测试都过了"什么时候是在说谎**（2026-11-23 真 bug 的回归）。

## 这条自检保护的是哪个真 bug

`projects/night3` 交付报告写「验收通过函数：8/8」，需求要求「每个模块都要有 pytest
测试，覆盖正常与异常路径」。实际产物里 `test_clean.py` 全文只有一行 docstring
（0 个测试函数），跑 `pytest` 仍然显示 `1 passed`。用户看到"测试通过"，
而 `summarize([])` 除零、`clean_row(age=None)` 属性错误它一条都没测。

旧电路同时有三处漏洞，本文件逐条钉住：
  ① `gates.gate_assertions` 只看**已有测试函数**，文件里 0 个测试 → checked=0 → 判红；
     但 `checked > 0` 来自"别的文件里有个真测试"时，空壳文件被静默放过；
  ② 恒真断言 `assert True` 技术上"有断言"，旧逻辑抓不到；
  ③ `pytest` 返回码 0 / 5 被一律当成"测试通过"，没人问"测了几个"。

跑法: python check_testweight.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import gates, testweight  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _sandbox_temp():
    """临时目录放进工作区（沙箱只允许写工作区内的路径）。"""
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=str(root))


def _mk(ws: Path, rel: str, text: str) -> None:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def main() -> int:
    print("空壳测试自检")

    print("[1] night3 原样复刻：docstring 壳文件被判成空壳")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_clean.py", '"""测试（由夜班自动创建）。"""\n')
        r = testweight.scan(ws)
        check("0 个测试函数 → tests=0", r["tests"] == 0, str(r["tests"]))
        check("整份文件进 shell_files", "test_clean.py" in r["shell_files"], str(r["shell_files"]))
        check("verdict 说清是空壳", "空壳" in r["verdict"], r["verdict"])
        check("problems 点名 0 个用例", any("一个测试函数都没有" in p for p in r["problems"]),
              str(r["problems"]))

    print("[2] 恒真断言不是断言")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_t.py", "def test_a():\n    assert True\n\n"
                             "def test_b():\n    assert 1\n\n"
                             "def test_c():\n    x = 1\n")
        r = testweight.scan(ws)
        check("3 个测试函数都数得到", r["tests"] == 3, str(r["tests"]))
        check("没有一个算真断言", r["verified"] == 0, str(r["verified"]))
        check("verdict 点名恒真", "空壳" in r["verdict"], r["verdict"])
        check("problems 指出 assert True", any("恒真断言" in p for p in r["problems"]),
              str(r["problems"]))

    print("[3] 函数体只有 pass/docstring 也算壳")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_p.py", 'def test_pass():\n    """说明。"""\n    pass\n')
        r = testweight.scan(ws)
        check("tests=1 但 verified=0", r["tests"] == 1 and r["verified"] == 0, str(r))
        check("problems 指出函数体是空的", any("函数体是空的" in p for p in r["problems"]),
              str(r["problems"]))

    print("[4] 真断言与 pytest.raises 都算（不误杀）")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_ok.py",
            "import pytest\n\n"
            "def test_eq():\n    assert 1 + 1 == 2\n\n"
            "def test_raises():\n    with pytest.raises(ValueError):\n"
            "        int('abc')\n")
        r = testweight.scan(ws)
        check("2 个测试都算有真断言", r["verified"] == 2, str(r["verified"]))
        check("不是空壳", r["shell_files"] == [], str(r["shell_files"]))
        check("verdict 如实计数", "2/2" in r["verdict"], r["verdict"])

    print("[5] 混合：有真测试也照样点名空壳文件（旧的静默放过）")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_good.py", "def test_x():\n    assert 2 > 1\n")
        _mk(ws, "test_shell.py", '"""啥也没有。"""\n')
        r = testweight.scan(ws)
        check("good 计入", r["verified"] == 1, str(r["verified"]))
        check("shell 仍被点名", r["shell_files"] == ["test_shell.py"], str(r["shell_files"]))
        check("problems 不空", bool(r["problems"]), str(r["problems"]))

    print("[6] 没有测试文件如实说，不假装通过")
    with _sandbox_temp() as td:
        r = testweight.scan(Path(td))
        check("file_count=0", r["file_count"] == 0, str(r["file_count"]))
        check("verdict=没有测试", r["verdict"] == "没有测试", r["verdict"])

    print("[7] 接进 gates.gate_assertions：空壳测试仓闸门必须红")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_clean.py", '"""测试（由夜班自动创建）。"""\n')
        g = gates.gate_assertions(ws, ["test_clean.py"])
        check("空壳仓 gate_assertions 判红", not g.ok, g.detail)
        check("detail 说得出为什么", "一个测试函数都没有" in g.detail or "空壳" in g.detail, g.detail)

    print("[8] 接进 gates.gate_assertions：真测试仓必须绿（不误杀）")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_ok.py", "def test_eq():\n    assert 1 + 1 == 2\n")
        g = gates.gate_assertions(ws, ["test_ok.py"])
        check("真测试仓判绿", g.ok, g.detail)

    print("[9] 坏测试文件不炸（parse error 如实报，不抛异常）")
    with _sandbox_temp() as td:
        ws = Path(td)
        _mk(ws, "test_broken.py", "def test_x(:\n")
        r = testweight.scan(ws)
        check("解析失败如实记录", any("无法解析" in p for p in r["problems"]), str(r["problems"]))
        check("不被当成通过", bool(r["shell_files"]) or r["verified"] == 0, str(r))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（壳文件/恒真断言/空函数体识别 + 真断言不误杀 + 闸门接线）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
