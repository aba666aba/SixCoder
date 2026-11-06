"""check_clarify.py — 澄清循环自检（合成夹具，按 docs/25 纪律）。

不依赖真实远端：用假 complete + 假 human 证明
  ① 多轮澄清能跑通并收敛；② 同一份文档迭代累积（不两份交替）；
  ③ 人类选择落档、双闸门（满意+足够）最终满足；④ 不会死循环。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import clarify  # noqa: E402


passed = 0


def check(name: str, cond) -> None:
    global passed
    assert cond, f"FAIL: {name}"
    passed += 1
    print(f"  ✓ {name}")


def fake_complete(system: str, user: str, effort: str) -> str:
    """第1轮给 2 个待定项；后续轮（含已选内容）深化并加 1 个新项；足够判定轮空 choices。

    注意：用『已有选择』而非 key 字符串判断后续轮次——_user_prompt 渲染时用的是
    问题文本而非 key，prompt 里不含 "fmt"/"store" 字面量，靠 key 匹配会永不命中。
    """
    if "第 1 版" in user:
        return (
            "叙述：先做 CSV 导入与按月汇总。\n```choices\n"
            '[{"key":"fmt","question":"CSV 字段分隔符？","options":["逗号","分号","制表符"]},'
            '{"key":"store","question":"数据存哪？","options":["SQLite","纯文件"]}]\n```'
        )
    if "已有选择" in user:
        return (
            "叙述：用 SQLite 存，逗号分隔。\n```choices\n"
            '[{"key":"report","question":"报表导出格式？","options":["xlsx","pdf","html"]}]\n```'
        )
    return "叙述：细节已足够。\n```choices\n[]\n```"


def fake_human(doc: clarify.DesignDoc):
    """每轮选第一个选项；第 2 轮后表示满意。"""
    sel = {c.key: c.options[0] for c in doc.open_choices()}
    satisfied = doc.rounds >= 2
    return sel, satisfied


# 1) 完整循环
doc = clarify.clarify_loop(
    "做一个 CSV 记账工具", fake_complete, fake_human, effort="max", max_rounds=8
)

check("循环至少跑了一轮", doc.rounds >= 1)
check("待定项被人类选择落档", any(c.picked for c in doc.choices))
check(
    "同一份文档累积（fmt/store/report 都在，非两份交替）",
    {c.key for c in doc.choices} == {"fmt", "store", "report"},
)
check("叙述非空（越改越细）", bool(doc.narrative))
check("双闸门最终满足（满意 + 足够）", doc.satisfied and doc.sufficient)
check("循环终止（未超 max_rounds 死循环）", doc.rounds <= 8)

# 2) is_sufficient：无待定项即足够
d2 = clarify.DesignDoc(requirement="x")
check("无待定项时 is_sufficient=True", clarify.is_sufficient(d2, fake_complete))

# 3) apply_choices 同 key 不重复加
d3 = clarify.start("x", fake_complete)
before = len(d3.choices)
d3 = clarify.apply_choices(d3, {"fmt": "逗号"}, fake_complete)
check("apply_choices 同 key 不重复（只追加新项）", len(d3.choices) == before + 1)

# 4) max_rounds 上限保护：human 永不满意也应停
def never_satisfied(_doc):
    return {c.key: c.options[0] for c in _doc.open_choices()}, False


doc_cap = clarify.clarify_loop(
    "需求", fake_complete, never_satisfied, effort="max", max_rounds=3
)
check("max_rounds 上限能停（不死循环）", doc_cap.rounds <= 3)

print(f"\ncheck_clarify: {passed} 项全部通过")
assert passed == 9, f"预期 9 项，实际 {passed}"
