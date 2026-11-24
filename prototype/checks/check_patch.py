"""行级修复自检：一行有问题就只改那一行，且**原文对不上就不许改**。

用户批评过：整文件错就重写整文件、函数错就重写整个函数——粒度太粗、白烧输出。
这里钉住最细一档的行为：
  · 补丁按行号 + 原文逐字匹配应用；原文对不上 → 拒绝（宁可不改，不许乱改）；
  · 行号越界 → 拒绝；
  · 多行补丁从后往前应用，行号不错位；
  · 证据里能认出"行"时才算行级可修；认不出就交给函数级/整文件。

跑法: python check_patch.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import patch  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


CODE = """def divide(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return a / b
"""


def main() -> int:
    print("行级修复自检")

    print("[1] 补丁解析")
    raw = ('```json\n{"patches":[{"path":"calc.py","line":3,'
           '"old":"        return 0.0","new":"        raise ValueError(\'div by zero\')"}]}\n```')
    got = patch.parse_patches(raw)
    check("能从围栏里解析出补丁", len(got) == 1 and got[0]["line"] == 3, str(got))
    check("字段齐全", got[0]["path"] == "calc.py" and "ValueError" in got[0]["new"])
    check("非法行号被丢弃",
          patch.parse_patches('{"patches":[{"path":"a.py","line":"x","old":"a","new":"b"}]}') == [])
    check("空补丁被丢弃",
          patch.parse_patches('{"patches":[{"path":"a.py","line":1,"old":"","new":""}]}') == [])

    print("[2] 应用：原文匹配才改")
    r = patch.apply_patches(CODE, [{"path": "calc.py", "line": 3,
                                    "old": "        return 0.0",
                                    "new": "        raise ValueError('div by zero')"}])
    check("原文对得上 → 应用成功", r["ok"] and "raise ValueError" in r["code"], str(r)[:160])
    check("只改了那一行（函数其余部分不动）",
          r["code"].count("return a / b") == 1 and "if b == 0:" in r["code"])

    print("[3] 安全性：原文对不上/越界 → 拒绝")
    r2 = patch.apply_patches(CODE, [{"path": "calc.py", "line": 3,
                                     "old": "        return 1.0",
                                     "new": "        return 2.0"}])
    check("原文对不上 → 拒绝且不改文件", (not r2["ok"]) and r2["code"] == CODE, str(r2)[:160])
    r3 = patch.apply_patches(CODE, [{"path": "calc.py", "line": 99, "old": "x", "new": "y"}])
    check("行号越界 → 拒绝", not r3["ok"] and "不存在" in r3["rejected"][0], str(r3)[:120])

    print("[4] 多补丁：从后往前应用，行号不错位")
    code2 = "a = 1\nb = 2\nc = 3\n"
    r4 = patch.apply_patches(code2, [
        {"path": "x.py", "line": 1, "old": "a = 1", "new": "a = 10"},
        {"path": "x.py", "line": 3, "old": "c = 3", "new": "c = 30"},
    ])
    check("两行都改对", r4["ok"] and "a = 10" in r4["code"] and "c = 30" in r4["code"],
          r4["code"])

    print("[5] 缩进保持")
    r5 = patch.apply_patches(CODE, [{"path": "calc.py", "line": 4,
                                     "old": "    return a / b", "new": "return a / b if b else 0.0"}])
    check("new 缩进被自动对齐到原行", r5["ok"] and "\n    return a / b if b else 0.0" in r5["code"],
          r5["code"])

    print("[6] 什么时候算'行级可修'")
    check("语法错带行号 → 行级", patch.is_line_localized("第 12 行语法错误：invalid syntax"))
    check("traceback 行号 → 行级", patch.is_line_localized('File "calc.py", line 7, in div'))
    check("NameError → 行级", patch.is_line_localized("调用抛了 NameError: name 'datetime' is not defined"))
    check("行为判据（示例不符）→ 不是行级",
          not patch.is_line_localized('summarize([{"age": 10}]) 得到 {"count": 0}，应为 {"count": 1}'))
    check("能抽出出错行号", patch.line_hint("第 12 行语法错误") == 12,
          str(patch.line_hint("第 12 行语法错误")))

    # ---- 以下针对"行级档自己把路堵死"的三个缺陷（2026-11-04 复盘得到）----
    print("[7] 不许再自我设限：行级也能'新增'（缺 import / 缺常量）")
    body = "def f():\n    return _COLS\n"
    r7 = patch.apply_patches(body, [{"path": "m.py", "line": 1, "old": "",
                                     "new": '_COLS = ("id", "name")\nimport sqlite3\n'}])
    check("old 为空 → 在第 N 行前插入（原第 1 行没被覆盖）",
          r7["ok"] and r7["code"].startswith("_COLS") and "def f():" in r7["code"], r7["code"])
    check("插入也计入 applied 且说明是插入", any("插入" in x for x in r7["applied"]),
          str(r7["applied"]))
    r7b = patch.apply_patches(body, [{"path": "m.py", "line": 2, "old": "",
                                      "new": "    extra = 1\n"}])
    check("可以插到文件中间（行号不错位）",
          r7b["ok"] and r7b["code"].splitlines()[1] == "    extra = 1", r7b["code"])
    r7c = patch.apply_patches(body, [{"path": "m.py", "line": 99, "old": "", "new": "x = 1\n"}])
    check("插入位置越界 → 拒绝", not r7c["ok"], str(r7c)[:120])
    r7d = patch.apply_patches(body, [{"path": "m.py", "line": 1, "old": "def f():",
                                      "new": "_A = 1\n_B = 2"}])
    check("new 多行 → 展开成多行（替换档也支持）",
          r7d["ok"] and "_A = 1" in r7d["code"] and "_B = 2" in r7d["code"], r7d["code"])

    print("[8] 输入不再被字符上限砍掉")
    small = "def f():\n    return 1\n"
    check("小文件 → 输入就是全文（行号 + 原文，一字不少）",
          patch.view_for_patch(small) == patch._numbered(small))
    big = "".join(f"def f{i}(a):\n    return a + {i}\n" for i in range(2000))
    v = patch.view_for_patch(big, hint=3000)
    check("大文件放得下 → 给全文，不再按字符砍（旧上限 12,000 字符）",
          len(v) > 12000 and "未显示" not in v, f"视图 {len(v)} 字符")
    v2 = patch.view_for_patch(big, hint=3000, budget_tokens=2000)   # 强制走压缩分支
    check("真超预算 → 结构骨架 + 出错窗口，并明确告知'其余行未显示'",
          "未显示" in v2 and "def f1499(a):" in v2 and "3000 |" in v2, v2[-160:])
    check("压缩视图比全文小", len(v2) < len(v), f"{len(v2)} vs {len(v)}")
    # 真实形状的大文件：函数少、每个函数长 → 骨架很小，压缩才有意义
    real = "".join("def f%d():\n" % k + "".join(f"    x{k}_{j} = {j}\n" for j in range(100))
                   for k in range(20))
    v3 = patch.view_for_patch(real, hint=1200, budget_tokens=2000)
    check("真实大文件 → 压缩到全文的几分之一（骨架小、窗口小）",
          len(v3) * 4 < len(patch._numbered(real)), f"{len(v3)} vs {len(patch._numbered(real))}")
    check("真实大文件窗口里看到出错行附近原文", "x11_99 = 99" in v3 or "x12_0 = 0" in v3, v3[-200:])
    untouched = patch.apply_patches(big, [{"path": "m.py", "line": 3001, "old": "",
                                           "new": "# marker\n"}])
    check("大文件里按真实行号插入一次成功（证明行号没有被截断弄错位）",
          untouched["ok"] and untouched["code"].splitlines()[3000] == "# marker",
          str(untouched["applied"]))

    print("[9] 证据截断：关键行优先，不许砍掉尾巴")
    tail = "\n".join([f"普通日志 {i}" for i in range(300)]) + \
        "\nFAILED test_x.py::test_a - AssertionError: boom\nE   assert 1 == 2"
    got = patch.narrow_evidence(tail, 300)
    check("尾部关键行被保住（旧的从头截断会全丢）",
          "assert 1 == 2" in got and "FAILED" in got, got[-120:])
    check("证据仍然窄（不超预算）", len(got) <= 300, f"{len(got)} 字符")
    check("本来就不长 → 原样返回", patch.narrow_evidence("短证据") == "短证据")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（原文匹配 / 越界拒绝 / 多补丁不错位 / 缩进对齐 / 行级判定 / "
          "插入与多行 / 输入不被砍 / 证据头尾兼顾）")
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
