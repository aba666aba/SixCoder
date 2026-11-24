"""假绿闸门自检：签名闸门与桩闸门必须抓住上一轮实测到的两个真实失败。

跑法: python check_fakgreen.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import gates  # noqa: E402

BAD_SIG = '''def merge_intervals(intervals, merge_all=False):
    """错误签名：多要一个必填参数之外的可选参数不影响，但下面那个才致命。"""
    return intervals
'''

BAD_SIG2 = '''def merge_intervals(intervals, strict):
    return intervals
'''

GOOD_SIG = '''def merge_intervals(intervals):
    """正确签名。"""
    if not intervals:
        return []
    return [[1, 6]]
'''

STUB = '''def roman_to_int(s):
    """桩：没实现。"""
    raise NotImplementedError


def int_to_roman(n):
    """只有 pass。"""
    pass
'''

BODY_OK = '''def roman_to_int(s):
    """有实现。"""
    total = 0
    for ch in reversed(s):
        total += 0
    return total
'''

CONTRACT = "实现 merge_intervals(intervals: list[list[int]]) -> list[list[int]]：合并重叠区间。"


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    tmp = Path(tempfile.mkdtemp())
    print("假绿闸门自检")

    sigs = gates.contract_signatures(CONTRACT)
    ok &= check("从契约抽出期望签名", "merge_intervals" in sigs and sigs["merge_intervals"]["required"] == 1,
                str(sigs))

    (tmp / "a.py").write_text(BAD_SIG2, encoding="utf-8")
    r = gates.gate_signature(tmp, ["a.py"], CONTRACT)
    ok &= check("多要必填参数被拦下（A1 的 TypeError）", not r.ok, r.detail)
    print(f"    {r.detail}")

    (tmp / "a.py").write_text(GOOD_SIG, encoding="utf-8")
    r = gates.gate_signature(tmp, ["a.py"], CONTRACT)
    ok &= check("正确签名放行", r.ok, r.detail)

    (tmp / "a.py").write_text(BAD_SIG, encoding="utf-8")
    r = gates.gate_signature(tmp, ["a.py"], CONTRACT)
    ok &= check("可选参数不影响判定", r.ok, r.detail)

    (tmp / "b.py").write_text(STUB, encoding="utf-8")
    r2 = gates.gate_stub(tmp, ["b.py"])
    ok &= check("NotImplementedError 桩被拦下（A2 的假 done）", not r2.ok, r2.detail)
    print(f"    {r2.detail}")

    (tmp / "b.py").write_text(BODY_OK, encoding="utf-8")
    r3 = gates.gate_stub(tmp, ["b.py"])
    ok &= check("有实现的函数放行", r3.ok, r3.detail)

    # 误报防护：抽象方法、空契约、无目标符号
    (tmp / "c.py").write_text(
        "import abc\n\n\nclass Base(abc.ABC):\n    @abc.abstractmethod\n    def run(self):\n        ...\n",
        encoding="utf-8")
    r4 = gates.gate_stub(tmp, ["c.py"])
    ok &= check("抽象方法不误报", r4.ok, r4.detail)

    # 误报防护：故意的空实现（回调占位）。真实代码里 asyncio 的 _sighandler_noop 就是 docstring+pass，
    # 旧逻辑判成桩 → 桩闸门在真实仓库上永久假红（假红一多就没人看闸门了）。
    (tmp / "d.py").write_text(
        'def _sighandler_noop(signum, frame):\n    """不做任何事（注册给 signal 的占位回调）。"""\n    pass\n',
        encoding="utf-8")
    r6 = gates.gate_stub(tmp, ["d.py"])
    ok &= check("名字带 noop 的空实现不误报", r6.ok, r6.detail)
    (tmp / "e.py").write_text(
        "def really_unimplemented(x):\n    pass\n", encoding="utf-8")
    r7 = gates.gate_stub(tmp, ["e.py"])
    ok &= check("普通空函数仍被拦（豁免只认名字里的 noop/nop）", not r7.ok, r7.detail)
    r5 = gates.gate_signature(tmp, ["c.py"], "随便写点需求，没有接口签名")
    ok &= check("无契约签名时跳过不误报", r5.ok and r5.evidence_ref == "no-contract", r5.detail)

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
