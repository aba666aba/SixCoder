"""骨架树自检（开发文档 §8.2）。

树的意义不是好看，是**能机械校验**：父 = 子之和、每层有预算、叶子必须落到真实文件。
这三条任何一条不成立，"规划"就退化成"大概写几个函数"。

跑法: python checks/check_skeleton_tree.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import blueprint_tree as bt  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="bt_"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def main() -> int:
    ws = make({
        "app/__init__.py": "",
        "app/a.py": "def fa():\n    return 1\n\n\ndef _priv():\n    return 2\n",
        "app/b.py": "def fb():\n    return 3\n",
        "core/__init__.py": "",
        "core/c.py": "def fc():\n    return 4\n",
    })

    print("[1] 从仓库实测出 L0/L1")
    tree = bt.build(ws)
    ids = [n["id"] for n in tree["nodes"]]
    check("L1 按目录分组", set(ids) == {"app", "core"}, str(ids))
    check("根节点有子节点清单", set(tree["root"]["children"]) == set(ids), str(tree["root"]["children"]))
    check("公开函数只数非下划线", tree["root"]["public"] == 3, str(tree["root"]["public"]))

    print("[2] 三校验：父=子之和 / 预算 / 叶子")
    chk = tree["checks"]
    check("新建的树 sum_ok", chk["sum_ok"], str(chk["problems"]))
    check("新建的树 leaf_ok", chk["leaf_ok"], str(chk["problems"]))
    check("没有目标时预算不判死", chk["budget_ok"], str(chk["problems"]))

    broken = {**tree, "root": {**tree["root"], "lines": tree["root"]["lines"] + 99}}
    chk2 = bt.validate(broken)
    check("父≠子之和必须报红", chk2["sum_ok"] is False, str(chk2["problems"])[:100])

    over = {**tree, "l1_cap": 1,
            "root": {**tree["root"], "target_lines": 100000}}
    chk3 = bt.validate(over)
    check("子系统超预算必须报红", chk3["budget_ok"] is False and chk3["over"], str(chk3))

    noleaf = {**tree, "nodes": [dict(tree["nodes"][0], files=[])] + tree["nodes"][1:]}
    chk4 = bt.validate({**noleaf, "root": {**tree["root"],
                                           "lines": sum(n["lines"] for n in noleaf["nodes"]),
                                           "funcs": sum(n["funcs"] for n in noleaf["nodes"]),
                                           "public": sum(n["public"] for n in noleaf["nodes"])}})
    check("没有文件的子系统不算叶子", chk4["leaf_ok"] is False, str(chk4["problems"])[:100])

    print("[3] 落盘 / 读回 / 缺口")
    p = bt.save(ws, tree)
    check("树落盘", p.is_file())
    again = bt.load(ws)
    check("读回一致", again["root"]["lines"] == tree["root"]["lines"], str(again.get("root")))
    g = bt.gaps(tree, 1000)
    check("缺口算对", g["target"] == 1000 and g["gap"] == 1000 - tree["root"]["lines"], str(g))
    check("缺口比例在 0..1", 0 <= g["pct"] <= 1, str(g["pct"]))

    print("[4] 摘要必须写明校验是否通过")
    lines = "\n".join(bt.summary_lines(tree))
    check("摘要含三校验结论", "校验" in lines, lines[:120])
    lines2 = "\n".join(bt.summary_lines({**tree, "checks": chk2}))
    check("校验没过时摘要要说出来", "未过" in lines2, lines2[:120])

    print("[5] 别把测试目录算进骨架")
    ws2 = make({"app/x.py": "def f():\n    return 1\n",
                "tests/test_x.py": "def test_f():\n    assert True\n"})
    t2 = bt.build(ws2)
    check("tests/ 不计入", [n["id"] for n in t2["nodes"]] == ["app"], str([n["id"] for n in t2["nodes"]]))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（L1 分组正确 / 三条校验都能红 / 落盘读回一致 / 摘要如实）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
