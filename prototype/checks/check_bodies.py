"""骨架/函数体/局部重写的**确定性**自检（不调模型）。

为什么必须单开：粒度过细的那条路实测更差（6/8、¥0.35），日志里反复出现
"回归：clean.py 已验收数 2→1，已回滚"。那说明**只重写一个函数时把别的函数弄坏了**。
这种 bug 必须用确定性测试钉死，不能靠在夜班里烧钱试。

三条不变量：
    A. 局部重写（只给部分函数体）→ 其余函数的源码必须**逐字不变**；
    B. 局部重写（走 _merge_skeleton）→ 其余函数同样逐字不变；
    C. 骨架渲染 + 填体后：模块能编译、缩进正确、空行/注释不丢内容。

跑法: python check_bodies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import author  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class F:
    """最小 FuncSig 替身。"""

    def __init__(self, name: str, params=(), ret="int", doc=""):
        self.name = name
        self.params = [type("P", (), {"name": n, "type": t})() for n, t in params]
        self.returns = ret
        self.doc = doc


SRC = '''"""模块文档。"""

import math


def alpha(x: int) -> int:
    """alpha 的描述里带引号 "2024-01-02"，必须能活下来。"""
    if x < 0:
        raise ValueError("负")
    return x + 1


def beta(rows: list[dict]) -> dict:
    """beta。"""
    out = {}
    for r in rows:
        if r.get("age"):
            out[r.get("id")] = r["age"]
    return out


def _helper(v: int) -> int:
    """辅助函数不该被动到。"""
    return v * 2
'''


def blocks_of(src: str) -> dict[str, str]:
    """把源码按顶格 def 切成 {函数名: 该函数源码}。"""
    import re
    lines = src.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("def ")]
    out = {}
    for k, s0 in enumerate(idx):
        e0 = idx[k + 1] if k + 1 < len(idx) else len(lines)
        m = re.match(r"def\s+(\w+)", lines[s0])
        out[m.group(1)] = "\n".join(lines[s0:e0]).rstrip()
    return out


def main() -> int:
    print("骨架 / 函数体 / 局部重写 自检")
    funcs = [F("alpha", [("x", "int")], "int", "alpha 的描述"),
             F("beta", [("rows", "list[dict]")], "dict", "beta 的描述"),
             F("_helper", [("v", "int")], "int", "辅助")]

    print("[1] 骨架能编译、签名正确")
    sk = author.render_skeleton(funcs)
    try:
        compile(sk, "<sk>", "exec")
        ok = True
    except SyntaxError:
        ok = False
    check("骨架可编译", ok)
    check("签名带注解", "def alpha(x: int) -> int:" in sk, sk[:80])
    check("占位体是 NotImplementedError", "raise NotImplementedError" in sk)
    check("描述里的双引号被消毒（不出现四引号结尾）", '""""' not in sk)

    print("[2] 全新填充：只填 alpha，其它留桩")
    code, missing = author.apply_bodies(sk, {"alpha": "return x + 1"})
    check("alpha 被填", "return x + 1" in code)
    check("beta 仍是桩", "raise NotImplementedError" in blocks_of(code)["beta"])
    check("未填清单正确", set(missing) == {"beta", "_helper"}, str(missing))
    compile(code, "<c>", "exec")

    print("[3] 局部重写（不变量 A）：其余函数逐字不变")
    before = blocks_of(SRC)
    # 模拟"只重写 beta"：在已有实现上替换 beta 的函数体
    src_no_beta = SRC.replace(before["beta"], "def beta(rows: list[dict]) -> dict:\n"
                                            '    """beta。"""\n    raise NotImplementedError')
    code2, _ = author.apply_bodies(src_no_beta, {"beta": "return {'count': len(rows)}"},
                                   stub_missing=False)
    after = blocks_of(code2)
    check("alpha 逐字未变", after.get("alpha") == before["alpha"], str(after.get("alpha"))[:80])
    check("_helper 逐字未变", after.get("_helper") == before["_helper"])
    check("beta 换成了新体", "return {'count': len(rows)}" in after.get("beta", ""))
    compile(code2, "<c2>", "exec")

    print("[4] 局部重写（走 _merge_skeleton，不变量 B）")
    merged = author._merge_skeleton(SRC, funcs, ["beta"])
    code3, _ = author.apply_bodies(merged, {"beta": "return {k: v for k, v in (rows[0] or {}).items()}"},
                                   stub_missing=False)
    after3 = blocks_of(code3)
    check("alpha 逐字未变", after3.get("alpha") == before["alpha"], str(after3.get("alpha"))[:80])
    check("_helper 逐字未变", after3.get("_helper") == before["_helper"])
    check("模块头（docstring/import）保留",
          code3.startswith('"""模块文档。"""') and "import math" in code3, code3[:60])
    try:
        compile(code3, "<c3>", "exec")
        ok3 = True
    except SyntaxError as exc:
        ok3 = False
        print("     编译错误：", exc)
    check("合并后可编译", ok3)

    print("[5] 缩进与空行处理（不变量 C）")
    body = "if x:\n    return 1\n\n# 注释行\nreturn 0"
    lines = author._indent_body(body)
    check("每行都缩进 4 格", all(x == "" or x.startswith("    ") for x in lines), str(lines))
    check("空行保持为空", "" in lines)
    check("注释行也缩进", any(x.strip().startswith("#") for x in lines))
    check("已经缩进过的体不会被双重缩进",
          author._indent_body("    return 1")[0] == "    return 1",
          str(author._indent_body("    return 1")))

    print("[6] 坏函数体能被点名")
    blame = author._blame_body(funcs, {"alpha": "return (1", "beta": "return {}"})
    check("只有坏的那个被点名", len(blame) == 1 and blame[0].startswith("alpha"), str(blame))
    check("好函数体不被点名", author._blame_body(funcs, {"beta": "return {}"}) == [])

    print("[7] 回归：模型少给函数体时，其它函数**不许被擦成桩**（真实事故）")
    # 事故现场：clean.py 的 clean_row 被擦成 NotImplementedError，已验收数 2→1。
    # 触发条件：本轮要"重写全部"（only=[]）且模型少返回了几个函数体。
    targets = ["alpha", "beta"]                      # 本轮目标：两个函数
    # 走真正的入口 rebuild_partial：目标被替换，模型没给的**还原成原实现**
    only_alpha, missing7 = author.rebuild_partial(SRC, funcs, targets,
                                                  {"alpha": "return x + 2"})
    after7 = blocks_of(only_alpha)
    check("缺失清单里列出了模型没给的 beta", "beta" in missing7, str(missing7))
    check("模型只给 alpha 时，beta 保持原实现（不是桩）",
          "raise NotImplementedError" not in after7.get("beta", ""),
          after7.get("beta", "")[:120])
    check("alpha 被更新", "return x + 2" in after7.get("alpha", ""))
    check("_helper 不动", after7.get("_helper") == before["_helper"])
    try:
        compile(only_alpha, "<c7>", "exec")
        ok7 = True
    except SyntaxError as exc:
        ok7 = False
        print("     编译错误：", exc)
    check("结果可编译", ok7)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（骨架可编译 / 局部重写不动别人 / 缩进正确 / 坏体点名）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
