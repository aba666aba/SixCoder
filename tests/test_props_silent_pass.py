"""判据执行器的静默通过缺陷回归测试（2026-11-24 修复）。

## 缺陷

core/props.py 的 RUNNER 模板里有一个 else 分支：

    else:
        ok, detail = True, ""        # 未知 kind = 通过

后果：新加一种判据类型时，如果忘了在 RUNNER 里补分支，
那条判据**永远绿** —— 不是"失败"，是"看起来通过但根本没检查"。

这比一条失败的判据危险得多：它把"没验证"伪装成"验证通过"，
直接打穿"报告不说谎"这个核心承诺。

## 这组测试钉什么

1. 已知 kind 正常工作（没改坏原有行为）
2. 未知 kind 判红，且原因里点出"未知判据类型"
3. 未知 kind 不能被混在已知 kind 里蒙混过关
4. 静态上不许再出现 "else: ok = True" 的写法
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import props   # noqa: E402


def _ws(module_src: str) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="props-silent-"))
    (ws / "m.py").write_text(module_src, encoding="utf-8")
    return ws


MOD = "def add(a, b):\n    return a + b\n"


def test_known_kind_still_passes():
    """已知 kind 必须照常工作 —— 修缺陷不能把正常路径改坏。"""
    ws = _ws(MOD)
    checks = [{"kind": "call_eq", "expr": "m.add(1, 2)", "expect": "3", "desc": "add(1,2)==3"}]
    r = props.run(ws, "m.py", checks, timeout=30)
    assert r.get("ok") is True, r
    assert r.get("passed") == 1, r


def test_known_kind_failure_still_detected():
    ws = _ws(MOD)
    checks = [{"kind": "call_eq", "expr": "m.add(1, 2)", "expect": "999", "desc": "故意错"}]
    r = props.run(ws, "m.py", checks, timeout=30)
    assert r.get("ok") is False, r


def test_unknown_kind_fails_not_passes():
    """核心回归：未知 kind 必须是红，不能是绿。"""
    ws = _ws(MOD)
    checks = [{"kind": "totally_made_up_kind", "desc": "未知种类", "expect": "x"}]
    r = props.run(ws, "m.py", checks, timeout=30)
    assert r.get("ok") is False, f"未知 kind 竟然判通过：{r}"
    fails = r.get("fails") or []
    assert fails, "未知 kind 没有产生任何失败记录"
    joined = " ".join(str(f.get("detail") or "") for f in fails)
    assert ("未知判据类型" in joined) or ("totally_made_up_kind" in joined), joined


def test_unknown_kind_does_not_hide_among_known():
    """一条对的 + 一条未知：整体必须红，未知那条必须被点名。"""
    ws = _ws(MOD)
    checks = [
        {"kind": "call_eq", "expr": "m.add(1, 2)", "expect": "3", "desc": "对的"},
        {"kind": "weird_unknown", "desc": "未知的", "expect": "y"},
    ]
    r = props.run(ws, "m.py", checks, timeout=30)
    assert r.get("ok") is False, f"混了未知 kind 还判通过：{r}"
    joined = " ".join(str(f.get("detail") or "") for f in (r.get("fails") or []))
    assert ("weird_unknown" in joined) or ("未知判据类型" in joined), joined


def test_runner_template_unknown_kind_is_not_silent():
    """静态钉住：**顶层 kind 分派**的 else 分支不许静默通过。

    注意区分两种 else：
      · 顶层 `elif kind == ...` 链的 else —— 这是"未知 kind"，必须判红；
      · 内层业务分支的 else（如 artifact_exists 里"文件存在且要点齐全"）
        —— 那是合法的通过路径，不该拦。

    所以这里只在**与 kind 分派同缩进**的 else 上做检查。
    """
    runner = props.RUNNER
    lines = runner.splitlines()

    # 找到所有 "elif kind ==" / "if kind ==" 的缩进，作为分派层
    dispatch_indents = set()
    for line in lines:
        s = line.strip()
        if s.startswith(("elif kind", "if kind")):
            dispatch_indents.add(len(line) - len(line.lstrip()))

    assert dispatch_indents, "没找到 kind 分派，模板结构变了，请更新这个测试"

    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip())
        if line.strip() != "else:" or indent not in dispatch_indents:
            continue
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        assert not nxt.startswith("ok"), (
            f"RUNNER 第 {i + 1} 行的顶层 kind else 分支疑似静默通过：{nxt}"
        )
        assert "False" in nxt or "未知" in nxt, (
            f"RUNNER 第 {i + 1} 行的未知 kind 分支没有判红：{nxt}"
        )
