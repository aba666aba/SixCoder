"""行级补丁的多行锚点（§11.4m，2026-11-11）。

为什么要有这个自检：
    2026-11-11 实测，夜班日志里反复出现
        patch_rejected  reason=补丁未应用：第 40 行原文对不上：
          期望 "'direction': direction_text,\\n           "，实际 "'direction': direction_text,"
    根因：`core/patch.py:apply_patches` 的替换分支拿**单行**去比 `old`，
    而远端给的 `old` 常常是**跨几行**的一段代码 → 永远不等 → 100% 被拒 →
    引擎拿不到行级修复 → 退回**整文件重写** → 打坏该文件里已通过的能力
    （实测 `app/models.py` 承载 9 条 done 能力）→ 净倒退。

    这个自检把"多行锚点必须能用"和"对不上仍然必须拒"两条都钉死。

断言：
    1. 多行锚点能定位并整块替换（不再被"原文对不上"拒掉）
    2. 多行锚点定位到的是**整块**（不许退化成只用首行 → 改错位置）
    3. 内容对不上时**仍然必须拒绝**（安全规则不退让）
    4. 单行锚点语义**逐字不变**（老行为零变化）
    5. 单行 old 只是整行的片段时，只替换片段、保留前后缀
    6. 插入（old 为空）语义不变
    7. 越界必须拒
    8. 替换后的代码**语法正确**（缩进对齐没写坏）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import patch as _pt  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("行级补丁 · 多行锚点（§11.4m）")

    # ---- 1/2/8：多行锚点整块替换 ----
    code = ("def f():\n"
            "    d = {\n"
            "        'direction': direction_text,\n"
            "        'keywords': keywords,\n"
            "    }\n")
    r = _pt.apply_patches(code, [{
        "path": "x.py", "line": 3,
        "old": "'direction': direction_text,\n        'keywords': keywords",
        "new": "'direction': direction_text,\n        'keywords': sorted(keywords)"}])
    check("多行锚点不再被拒", r["ok"], str(r["rejected"]))
    check("多行锚点整块替换（不是只改首行）",
          "sorted(keywords)" in r["code"] and "'keywords': keywords," not in r["code"],
          r["code"])
    check("替换后语法正确", _compiles(r["code"]), r["code"])

    # ---- 3：内容对不上仍然必须拒 ----
    r2 = _pt.apply_patches(code, [{
        "path": "x.py", "line": 3,
        "old": "'direction': WRONG,\n        'keywords': keywords",
        "new": "x"}])
    check("内容对不上仍然拒绝", (not r2["ok"]) and len(r2["rejected"]) == 1, str(r2["rejected"]))
    check("拒绝时不动代码", r2["code"] == code, r2["code"])

    # ---- 2b：old 多行但文件里只有首行匹配 → 必须拒（不许退化成只用首行）----
    c8 = "def f():\n    only_one_line()\n"
    r8 = _pt.apply_patches(c8, [{
        "path": "x.py", "line": 2,
        "old": "only_one_line()\n    not_there", "new": "x"}])
    check("old 多行但文件不匹配必须拒（不退化成首行）",
          (not r8["ok"]) and r8["code"] == c8, str(r8["rejected"]))

    # ---- 4：单行锚点语义逐字不变 ----
    c3 = "def f():\n    a = 1\n    return a\n"
    r3 = _pt.apply_patches(c3, [{
        "path": "x.py", "line": 3, "old": "return a", "new": "return a + 1"}])
    check("单行锚点语义不变",
          r3["ok"] and r3["code"] == "def f():\n    a = 1\n    return a + 1\n",
          r3["code"])

    # ---- 5：单行 old 是整行片段 → 只换片段、保留前后缀 ----
    c5 = 'def f():\n    x = {\n        "direction": direction_text,\n    }\n'
    r5 = _pt.apply_patches(c5, [{
        "path": "x.py", "line": 3, "old": '"direction": direction_text',
        "new": '"direction": dir_text'}])
    check("单行片段替换（保留前后缀）",
          r5["ok"] and '"direction": dir_text' in r5["code"], r5["code"])

    # ---- 6：插入语义不变 ----
    r6 = _pt.apply_patches(c3, [{
        "path": "x.py", "line": 1, "old": "", "new": "import os"}])
    check("插入语义不变", r6["ok"] and r6["code"].startswith("import os"), r6["code"])

    # ---- 7：越界必须拒 ----
    r7 = _pt.apply_patches(c3, [{
        "path": "x.py", "line": 99, "old": "x", "new": "y"}])
    check("越界必须拒", (not r7["ok"]) and "不存在" in r7["rejected"][0], str(r7["rejected"]))

    # ---- 提示词必须告诉远端 old 可以多行 ----
    check("提示词说明了 old 可以跨多行",
          "old 可以跨多行" in _pt.PATCH_PROMPT, _pt.PATCH_PROMPT[:200])

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("全部通过（多行锚点可用 + 安全规则不退让）")
    return 0


def _compiles(code: str) -> bool:
    try:
        compile(code, "<t>", "exec")
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    sys.exit(main())
