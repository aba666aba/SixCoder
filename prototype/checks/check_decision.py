"""决策表自检：**定位准不准**（文件/函数/行）+ **遇到 bug 怎么决策**（动作选择）。

这是用户点名要"硬"的两件事，所以每条规则都要有正反用例：
  ① 有行号 → 选行级补丁；有函数名没行号 → 选函数级重写；只有文件 → 整文件；
  ② 偶发（限流/超时）→ 同档重试，不许升档；
  ③ 契约矛盾 → 不写代码，先回问契约；
  ④ 各动作预算用完后 → 降级/放弃，而不是无限重试同一档。

跑法: python check_decision.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import decision  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


SRC = '''"""demo。"""


def alpha(x: int) -> int:
    """alpha。"""
    return x + 1


def beta(rows: list[dict]) -> dict:
    """beta。"""
    out = {}
    for r in rows:
        out[r.get("id")] = r["age"]
    return out
'''


def main() -> int:
    print("故障定位 + 决策表 自检")
    ws = Path(tempfile.mkdtemp())
    (ws / "demo.py").write_text(SRC, encoding="utf-8")

    print("[1] 定位：traceback 能带出文件+行+函数")
    ev = ('Traceback (most recent call last):\n'
          '  File "C:/x/demo.py", line 11, in beta\n'
          '    out[r.get("id")] = r["age"]\n'
          "KeyError: 'age'")
    fs = decision.localize(ws, ev, rel="demo.py")
    check("解析出 1 条故障", len(fs) == 1, str([f.as_row() for f in fs]))
    check("文件与行号准确", fs[0].file == "demo.py" and fs[0].line == 11, fs[0].as_row())
    check("函数名准确（beta）", fs[0].function == "beta", fs[0].function)
    check("精度判定为 line", fs[0].precision == "line", fs[0].precision)

    print("[2] 定位：只有行号时反查函数名")
    fs2 = decision.localize(ws, 'File "demo.py", line 6, in <module>', rel="demo.py")
    check("行 6 反查得到 alpha", any(f.function == "alpha" for f in fs2),
          str([f.as_row() for f in fs2]))

    print("[3] 定位：闸门文本里的 file::func() 与 file:N")
    fs3 = decision.localize(ws, "demo.py::beta() 用了字段 'age'，但契约里没有", rel="demo.py")
    check("从 ::func() 抽出函数名", any(f.function == "beta" for f in fs3),
          str([f.as_row() for f in fs3]))
    fs3b = decision.localize(ws, "demo.py:11 对游标行直接 dict(row)", rel="demo.py")
    check("从 file:N 抽出文件名+行号", any(f.line == 11 for f in fs3b),
          str([f.as_row() for f in fs3b]))

    print("[4] 定位：判据只点函数名 → 用 AST 反查 def 行")
    fs4 = decision.localize(ws, "beta() 圈复杂度 12（上限 8）", rel="demo.py")
    check("认出函数 beta", any(f.function == "beta" for f in fs4), str([f.as_row() for f in fs4]))
    check("行号留空但精度=function",
          fs4 and fs4[0].precision == "function", fs4[0].precision if fs4 else "")

    print("[5] 定位：说不清 → 模块级")
    fs5 = decision.localize(ws, "业务流未跑通：test_e2e_flow.py 断言失败", rel="demo.py")
    check("精度=module", decision.precision_of(fs5) == "module",
          str([f.as_row() for f in fs5]))

    print("[6] 决策：按精度选动作")
    d1 = decision.decide(fs, rel="demo.py")
    check("有行号 → 行级补丁", d1.action == decision.ACT_PATCH_LINE, d1.as_row())
    check("目标里带 文件:行", any(":11" in t for t in d1.targets), str(d1.targets))
    d2 = decision.decide(fs4, rel="demo.py")
    check("有函数名没行号 → 只重写函数体", d2.action == decision.ACT_REWRITE_FUNC, d2.as_row())
    check("目标就是这个函数", d2.targets == ["beta"], str(d2.targets))
    d3 = decision.decide(fs5, rel="demo.py")
    check("说不清 → 整文件重写", d3.action == decision.ACT_REWRITE_MODULE, d3.as_row())

    print("[7] 决策：预算用完就降级，不许无限重试")
    d4 = decision.decide(fs, rel="demo.py", attempts_done={decision.ACT_PATCH_LINE: 1})
    check("行级补丁试过 1 次 → 降到函数级", d4.action == decision.ACT_REWRITE_FUNC, d4.as_row())
    d5 = decision.decide(fs4, rel="demo.py", attempts_done={decision.ACT_REWRITE_FUNC: 2})
    check("函数级试过 2 次 → 升到整文件", d5.action == decision.ACT_REWRITE_MODULE, d5.as_row())
    d6 = decision.decide(fs5, rel="demo.py",
                         attempts_done={decision.ACT_REWRITE_FUNC: 2,
                                        decision.ACT_REWRITE_MODULE: 1})
    check("全试过 → 放弃并写报告", d6.action == decision.ACT_GIVE_UP, d6.as_row())

    print("[8] 决策：偶发失败不升档")
    ept = decision.localize(ws, "被限流（HTTP 429）：稍后重试", rel="demo.py")
    d7 = decision.decide(ept, rel="demo.py")
    check("限流 → 同档重试", d7.action == decision.ACT_RETRY_SAME, d7.as_row())
    check("不会选行级/函数级", d7.action not in (decision.ACT_PATCH_LINE,
                                              decision.ACT_REWRITE_FUNC), d7.action)

    print("[9] 只有提示项 → 不改代码")
    adv = [decision.Finding(file="demo.py", kind="advisory", detail="保留 1 位小数",
                            severity="advisory")]
    check("只报告", decision.decide(adv).action == decision.ACT_REPORT_ONLY)

    print("[10] 决策表可渲染（进文档/交付报告）")
    tbl = decision.table_markdown()
    check("表格含行级/函数级/模块级三档",
          "行级补丁" in tbl and "只重写该函数体" in tbl and "整文件重写" in tbl)

    print("[11] 元信息不算故障条目（真实事故：回归提示把精度拖成 module）")
    meta = decision.localize(ws, "回归：clean.py 已验收数 2→1，已回滚", rel="demo.py")
    check("识别为 meta", meta and meta[0].kind == "meta", str([f.as_row() for f in meta]))
    check("精度不让 meta 拖成 module", decision.precision_of(meta) == "none",
          decision.precision_of(meta))
    mixed = decision.localize(ws, "回归：clean.py 已验收数 2→1；clean.py::beta() 用了字段 age",
                              rel="demo.py")
    check("同时有 meta 与真实故障时，精度取真实的",
          decision.precision_of(mixed) == "function", decision.precision_of(mixed))
    check("decide 只看真实故障（选函数级）",
          decision.decide(mixed, rel="demo.py").action == decision.ACT_REWRITE_FUNC,
          decision.decide(mixed, rel="demo.py").as_row())

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（行/函数/模块三级定位 + 决策表 + 预算降级 + 偶发不升档）")
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
