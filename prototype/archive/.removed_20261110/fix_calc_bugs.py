"""修这次「计算器夜班」暴露的两个真 bug。

## bug 1：`NameError: name 'Path' is not defined` —— 夜班当场崩溃
    `core/contractgen.py` 里用了 `Path(ws)`（第 157/162 行，读写契约缓存），
    但文件顶部**从来没有 `from pathlib import Path`**。
    于是走"需求没有清晰契约 → 让远端把需求翻译成契约"这条路时必崩。
    计算器需求正是这条路（"生成一个计算器"解析不出模块契约）。
    以前没暴露是因为这条路径只在远端可用时才走到。

## bug 2：交付报告写着"花费 ¥6.2099（941 次远端调用）"，而这次一次调用都没有
    我在 `write_delivery` 里加的花费行，`scope` 里没有 task_id 时取到空串，
    `usage_summary("")` = **全库累计**，于是把历史总账写进了单次报告 —— 误导性极强。
    修法：拿不到 task_id 就明确写"本次未能统计"，绝不显示累计值冒充本次花费。
"""
from pathlib import Path

# ---------- bug 1 ----------
p = Path("core/contractgen.py")
s = p.read_text(encoding="utf-8")
if "from pathlib import Path" not in s:
    s = s.replace("import json\nimport re", "import json\nimport re\n\nfrom pathlib import Path", 1)
    p.write_text(s, encoding="utf-8")
    print("contractgen.py：补上 from pathlib import Path")
else:
    print("contractgen.py：已有 Path 导入")

# ---------- bug 2 ----------
p = Path("core/budget_cap.py")
s = p.read_text(encoding="utf-8")
old = '''def report_line(task_id: str, cap: float) -> str:
    """交付报告里的一行花费说明（有上限/没上限都写清）。"""
    spent = cost_spent(task_id)'''
new = '''def report_line(task_id: str, cap: float) -> str:
    """交付报告里的一行花费说明（有上限/没上限都写清）。

    **必须带 task_id**：`usage_summary("")` 返回的是**全库累计**，
    拿它冒充"本次花费"会把历史总账写进单次报告（实测：一次 0 调用的运行
    报告里写着"花费 ¥6.2099（941 次远端调用）"）。拿不到 task_id 就如实说统计不到。
    """
    if not str(task_id or "").strip():
        return "- 花费：本次未能统计（缺 task_id；不显示累计值以免误导）"
    spent = cost_spent(task_id)'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("budget_cap.py：无 task_id 时不再拿累计冒充本次")
else:
    print("budget_cap.py：锚点没找到")
