"""契约反推自检：从现有代码反推出的契约必须"只含代码里真写出的事实"。

这条钉的是开发文档 §15.2 第 4 条（关键缺口）：
    · 产生契约的入口此前只有 parse_requirement(需求文本) / contractgen.synthesize(需求)，
      没有"读现有代码产出契约"的路径 → 存量仓库进不了夜班；
    · 反推出的契约必须与 parse_requirement 的产物**同构**（同一个 Contract/FuncSig），
      闸门/骨架/判据才能直接吃；
    · **一个字都不许编**：没有注解就没有类型、没有 doctest 就没有示例、
      return {变量: v} 的键不收 —— 宁可契约薄，不许把猜测当事实。

跑法: python checks/check_contract_reverse.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import contract_reverse as cr  # noqa: E402
from core.contract import describe  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


GOOD_SRC = '''\
"""演示模块。"""


def add(a: float, b: float) -> float:
    """两数之和。

    >>> add(1, 2)
    3.0
    >>> add(-1, 1)
    0.0
    """
    return a + b


def div(a: float, b: float) -> float:
    """除法；b 为 0 抛异常。"""
    if b == 0:
        raise ValueError("b is zero")
    return a / b


def make_user(name: str, age: int = 18) -> dict:
    """造一个用户。"""
    return {"id": 1, "name": name}


class LRU:
    def get(self, key: str) -> str:
        return ""

    def put(self, key: str, val: str = "") -> None:
        return None


def _private(x):
    return x
'''


def main() -> int:
    print("契约反推自检（只提取真事实 / 同构 / 存量仓库入场券）")

    print("[1] 签名与类型：注解真实存在才收")
    c, issues = cr.parse_source("m.py", GOOD_SRC)
    add = c.by_name("add")
    check("解析出 5 个公开函数（类方法算在内）", len(c.funcs) == 5,
          str([f.name for f in c.funcs]))
    check("add 参数名与类型都对",
          [(p.name, p.type) for p in add.params] == [("a", "float"), ("b", "float")],
          str([(p.name, p.type) for p in add.params]))
    check("add 返回注解收下", add.returns == "float", add.returns)

    print("[2] doctest → 示例（期望值 = 输出行字面量）")
    check("add 收到 2 条示例", len(add.examples) == 2, str(add.examples))
    check("第一条示例参数与期望值都对",
          add.examples[0].args == ["1", "2"] and add.examples[0].expect == "3.0",
          str(add.examples[0]))

    print("[3] raise 语句 → 异常（不许从文案里编）")
    div = c.by_name("div")
    check("div 的异常是 ValueError", div.raises == ["ValueError"], str(div.raises))
    check("div 没有示例（docstring 里没有 doctest）", not div.examples)

    print("[4] return dict 字面量 → 返回键")
    mk = c.by_name("make_user")
    check("make_user 键 = id/name（书写序）", mk.keys == ["id", "name"], str(mk.keys))
    check("默认值收下：age=18",
          [(p.name, p.default) for p in mk.params][-1] == ("age", "18"),
          str([(p.name, p.default) for p in mk.params]))

    print("[5] 类方法：owner_class 与 self 剔除")
    get = c.by_name("get")
    check("get 属于 LRU", get.owner_class == "LRU", get.owner_class)
    check("self 不进参数表", [p.name for p in get.params] == ["key"],
          str([p.name for p in get.params]))

    print("[6] 私有函数默认不进契约面（可显式要）")
    check("默认不含 _private", c.by_name("_private") is None)
    c2, _ = cr.parse_source("m.py", GOOD_SRC, include_private=True)
    check("include_private=True 收下 _private", c2.by_name("_private") is not None)

    print("[7] 语法错误不 crash，如实进 issues")
    bad, probs = cr.parse_source("bad.py", "def f(:\n  pass\n")
    check("issues 里有语法错误", probs and "语法错误" in probs[0], str(probs))
    check("坏文件返回空契约", not bad.funcs)

    print("[8] from_code：目录口径（测试/配置/忽略目录不进契约面）")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "app.py").write_text(GOOD_SRC, encoding="utf-8")
        (root / "test_app.py").write_text("def test_x():\n    assert True\n",
                                          encoding="utf-8")
        (root / "conftest.py").write_text("", encoding="utf-8")
        (root / ".sixcoder").mkdir()
        (root / ".sixcoder" / "x.py").write_text("def y():\n    return 1\n",
                                                 encoding="utf-8")
        (root / "__pycache__").mkdir()
        (root / "__pycache__" / "z.py").write_text("def w():\n    return 2\n",
                                                   encoding="utf-8")
        cons, probs2 = cr.from_code(root)
        check("只收到 app.py", sorted(cons) == ["app.py"], str(sorted(cons)))
        check("没有 issue（都是正常跳过）", not probs2, str(probs2))

    print("[9] 同构：反推产物能直接走 describe（下游闸门/判据同一条路）")
    text = describe(c)
    check("describe 渲染出 add 签名与示例数", "add(a: float, b: float)" in text
          and "2 个示例" in text, text[:200])
    check("契约面可数（by_name/names/examples 同构方法可用）",
          "div" in c.names() and len(c.examples()) == 2, str(c.names()))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（签名真实 / doctest 成示例 / raise 成异常 / 字面量键 / 类归属 /"
          " 私有默认藏 / 坏文件不 crash / 目录口径 / 与需求契约同构）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
