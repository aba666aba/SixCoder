"""冻结函数回填自检（2026-11-07 真实夜班暴露的死循环）。

事故：`matching_core/da.py` 连续 6 轮失败指纹**完全相同**，`oscillation action=stop`。
现场是两条互斥指令撞在一起：
  · prompt 的【已通过验收、不许改动或删除的函数】写着 `blocking_pairs` 不许动；
  · 同一轮又因为"定位不到具体函数"要求**整文件重写**。
模型重写后通常修好了目标函数，但顺手改了 `blocking_pairs` → 已验收数下降 →
**整文件回滚** → 新修好的部分一起丢 → 下一轮从同一状态再来一次。
引擎自己也在 `.sixcoder/system_issues.jsonl` 里记下了这条互斥。

修法：回归保护先做**最小撤销** —— 只把变了的冻结函数按旧实现回填，保留本轮其它改动；
回填救不回来才整文件回滚。

跑法: python checks/check_frozen_restore.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import company, project  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


BEFORE = '''def keep(a):
    return a + 1


def other(a):
    return a
'''

AFTER = '''def keep(a):
    return a


def other(a):
    return a * 2
'''


def make(before: str, frozen: list[str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="fz_"))
    (ws / ".sixcoder").mkdir()
    (ws / ".sixcoder" / "frozen.json").write_text(
        '{"m.py": {"funcs": %s}}' % __import__("json").dumps(frozen), encoding="utf-8")
    (ws / "m.py").write_text(before, encoding="utf-8")
    return ws


def main() -> int:
    ws = make(BEFORE, ["keep"])
    before = (ws / "m.py").read_bytes()
    (ws / "m.py").write_text(AFTER, encoding="utf-8")
    res = company._restore_frozen(ws, "m.py", before)
    text = (ws / "m.py").read_text(encoding="utf-8")
    check("冻结函数被回填", "return a + 1" in text, res.get("why") or "")
    check("本轮其它改动被保留", "return a * 2" in text, "回填不该顺手回滚整文件")
    check("回填只报动过的函数", res.get("restored") == ["keep"], str(res.get("restored")))
    check("回填不影响语法", __import__("ast").parse(text) is not None)

    # 冻结函数没被改动 → 不该假装回填成功（让调用方继续走整文件回滚）
    ws2 = make(BEFORE, ["keep"])
    b2 = (ws2 / "m.py").read_bytes()
    (ws2 / "m.py").write_text(BEFORE.replace("return a\n", "return a * 3\n"), encoding="utf-8")
    r2 = company._restore_frozen(ws2, "m.py", b2)
    check("没改冻结函数时如实返回失败", r2.get("ok") is False, str(r2))

    # 没有冻结清单 → 同样如实返回失败
    ws3 = make(BEFORE, [])
    b3 = (ws3 / "m.py").read_bytes()
    (ws3 / "m.py").write_text(AFTER, encoding="utf-8")
    r3 = company._restore_frozen(ws3, "m.py", b3)
    check("无冻结清单时如实返回失败", r3.get("ok") is False, str(r3))

    # 冻结函数被**删掉**也要能回填（模型整文件重写时最容易干的事）
    ws4 = make(BEFORE, ["keep"])
    b4 = (ws4 / "m.py").read_bytes()
    (ws4 / "m.py").write_text("def other(a):\n    return a * 2\n", encoding="utf-8")
    r4 = company._restore_frozen(ws4, "m.py", b4)
    t4 = (ws4 / "m.py").read_text(encoding="utf-8")
    check("冻结函数被删也能回填", r4.get("ok") and "def keep" in t4, str(r4))

    check("project.load_frozen 口径一致", project.load_frozen(ws) == {"m.py": {"keep"}},
          str(project.load_frozen(ws)))

    # 同一类失败的另一半：整文件重写**删掉了**已有函数（不是改坏，是没了）。
    # 旧行为是打回让模型重写，模型下一轮还是给一份"按契约写的干净文件"，同样缺 → 死循环。
    from core import author  # noqa: E402
    old_src = "def keep(a):\n    return a + 1\n\n\ndef helper(a):\n    return a\n"
    new_src = "def keep(a):\n    return a + 2\n"
    gone = author._deleted_symbols(old_src, new_src)
    check("能检出被删的函数", gone == ["helper"], str(gone))
    merged = author._restore_symbols(old_src, new_src, gone)
    check("被删函数按旧实现接回", "def helper" in merged and "return a\n" in merged, repr(merged[:80]))
    check("接回时保留本轮新实现", "return a + 2" in merged, repr(merged[:80]))
    check("接不回来时返回空串（让调用方打回）",
          author._restore_symbols("", new_src, ["helper"]) == "", "")

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（只回填冻结函数 / 保留本轮改动 / 误删的函数按旧实现接回 / 救不回来时如实失败）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
