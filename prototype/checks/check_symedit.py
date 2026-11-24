"""symedit 自检：确定性渲染必须做到"只改该改的，其它字节不动"。

跑法： python check_symedit.py     （全部通过 exit=0）
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import symedit

SRC = '''"""模块文档。"""
from __future__ import annotations

CONST = 3          # 行内注释必须保留


# 上面是空行与注释，渲染器不许动
def add(a: int, b: int) -> int:
    """加法。"""
    return a + b


def sub( a,b ):
    # 没 docstring、没注解、空格很怪
    return a-b


class Box:
    """容器。"""

    def __init__(self, v):
        self.v = v

    def get(self):
        return self.v
'''

NEW_SUB = '''def sub(a: int, b: int) -> int:
    """减法。"""
    return a - b'''

NEW_GET = '''    def get(self) -> int:
        """取值。"""
        return self.v + 0'''

NEW_MUL = '''def mul(a: int, b: int) -> int:
    """乘法。"""
    return a * b'''


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("symedit 自检")

    ok &= check("libcst 可用", symedit.available())
    syms = symedit.outline(SRC)
    quals = [s.qualname for s in syms]
    ok &= check("outline 覆盖顶层函数/类/方法",
                quals == ["add", "sub", "Box", "Box.__init__", "Box.get"], str(quals))

    # 1) 替换顶层函数：只有 sub 的三行变化
    rep = symedit.apply_edits(SRC, [{"op": "replace", "symbol": "sub", "code": NEW_SUB}])
    ok &= check("替换顶层函数成功", rep.ok, rep.reason)
    if rep.ok:
        before = SRC.replace(symedit.source_of(SRC, "sub"), "@@SUB@@")
        after = rep.new_source.replace(symedit.source_of(rep.new_source, "sub"), "@@SUB@@")
        ok &= check("除 sub 外全文逐字节相同", before == after)
        ok &= check("注释与空行保留",
                    "CONST = 3          # 行内注释必须保留" in rep.new_source
                    and "# 上面是空行与注释" in rep.new_source)
        ok &= check("其它函数未被改动",
                    "def add(a: int, b: int) -> int:" in rep.new_source
                    and "return a + b" in rep.new_source)
        ok &= check("sub 已被替换为新实现",
                    'def sub(a: int, b: int) -> int:' in rep.new_source
                    and '"""减法。"""' in rep.new_source)

    # 2) 替换类方法
    rep2 = symedit.apply_edits(SRC, [{"op": "replace", "symbol": "Box.get", "code": NEW_GET}])
    ok &= check("替换类方法成功", rep2.ok, rep2.reason)
    if rep2.ok:
        ok &= check("方法缩进正确", "\n    def get(self) -> int:" in rep2.new_source)
        ok &= check("其它方法不受影响", "def __init__(self, v):" in rep2.new_source)

    # 3) 插入新函数
    rep3 = symedit.apply_edits(SRC, [{"op": "add", "symbol": "mul", "code": NEW_MUL,
                                      "after": "add"}])
    ok &= check("插入新函数成功", rep3.ok, rep3.reason)
    if rep3.ok:
        i_add = rep3.new_source.index("def add")
        i_mul = rep3.new_source.index("def mul")
        i_sub = rep3.new_source.index("def sub")
        ok &= check("插入位置在 add 之后、sub 之前", i_add < i_mul < i_sub)
        ok &= check("插入后语法合法", _parses(rep3.new_source))

    # 4) 空操作替换必须逐字节还原
    rep4 = symedit.apply_edits(SRC, [{"op": "replace", "symbol": "sub",
                                      "code": symedit.source_of(SRC, "sub")}])
    ok &= check("原样回写 = 字节不变（保真）", rep4.ok and rep4.new_source == SRC)

    # 5) 失败路径必须明确拒绝，不能写坏文件
    bad1 = symedit.apply_edits(SRC, [{"op": "replace", "symbol": "nope", "code": NEW_SUB}])
    ok &= check("定位失败被拒绝", not bad1.ok and "定位失败" in bad1.reason, bad1.reason)
    bad2 = symedit.apply_edits(SRC, [{"op": "replace", "symbol": "sub",
                                      "code": "def sub(a: int -> int:\n    return a"}])
    ok &= check("语法坏的生成被拒绝", not bad2.ok, bad2.reason)
    bad3 = symedit.apply_edits("def broken(:\n", [{"op": "replace", "symbol": "x",
                                                   "code": NEW_SUB}])
    ok &= check("原文件坏时拒绝", not bad3.ok)
    bad4 = symedit.apply_edits(SRC, [])
    ok &= check("空编辑列表被拒绝", not bad4.ok)

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


def _parses(src: str) -> bool:
    import ast
    try:
        ast.parse(src)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
