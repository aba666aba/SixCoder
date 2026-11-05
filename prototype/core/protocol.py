"""本地 ↔ 远端的工作协议（甲方模式的核心接口约定）。

为什么要"协议"而不是随便给个提示词：
    远端很强，你不告诉它边界，它会把**机械的活也自己做了** —— 自己写测试、自己
    拼交付报告、自己在正文里画表格逐条核对。那是白烧 token，而且做得比本地差
    （本地是确定性的：同一个输入永远同一个结论）。

所以协议只有两句话：
    **想清楚 + 写代码 = 远端的活；验证/测试/报告/格式化/落盘 = 本地的活。**
    远端只准回机器能解析的 JSON，不准回解释、表格、步骤说明。

这个模块只负责"把协议渲染成提示词"，不执行任何东西。
"""
from __future__ import annotations

# 本地具备的机械能力（这些活远端别做）。改 gates/props 时同步这里 —— checks/check_protocol.py 会校验。
LOCAL_CAPABILITIES = (
    ("语法与编译", "编译检查、import 可导入性检查（gate_import_ok）"),
    ("签名与结构", "契约签名比对（缺参/多必填参）、桩检测、重复定义检测"),
    ("行为判据", "契约示例（call_eq）、异常断言（raises）、返回键（keys）、往返性质（roundtrip）"),
    ("边界矩阵", "按注解自动生成空/None/缺字段/超长/类型错输入，崩溃型异常算不合格"),
    ("数据库", "真 sqlite 端到端（写入→读回）、游标行形态、executemany 绑定形态"),
    ("测试", "由契约渲染 pytest 并执行（测试代码不由模型写）"),
    ("静态质量", "圈复杂度/嵌套深度/单行数、安全规则、字段名一致性"),
    ("变异", "在改动行上做变异，测试必须能杀死至少一个"),
    ("落盘与回滚", "符号级编辑、整文件写入、按单快照回滚、自动补 import/补参数"),
    ("报告", "DELIVERY.md / TRACE.md / 交付报告全部由引擎生成"),
)

REMOTE_MUST = (
    "决定模块划分、函数接口（参数名/类型/返回类型）、异常语义、数据结构",
    "写实现代码（整文件或指定符号），保证满足契约里每一条示例",
    "在信息不足时**明确说出缺什么**，不要猜（甲方模式：说出来就让本地去查证据）",
)

REMOTE_MUST_NOT = (
    "不要写 pytest 测试（本地按契约渲染，你写的测试更弱）",
    "不要写验收报告/交付说明/追溯矩阵（本地生成）",
    "不要解释你的代码，不要输出步骤、表格、思考过程",
    "不要跑命令、不要假设能执行代码（你只能回文本）",
    "不要改契约规定的签名/异常类型",
)


def capabilities_block() -> str:
    """渲染成提示词里的一段"分工声明"。"""
    lines = ["【本地工具（已具备，你不要重复做）】"]
    for name, detail in LOCAL_CAPABILITIES:
        lines.append(f"- {name}：{detail}")
    lines.append("")
    lines.append("【你必须做的】")
    lines += [f"- {x}" for x in REMOTE_MUST]
    lines.append("")
    lines.append("【你不许做的】")
    lines += [f"- {x}" for x in REMOTE_MUST_NOT]
    return "\n".join(lines)


def output_format(kind: str) -> str:
    """每种工作的严格输出格式（机器解析，不接受解释性文本）。"""
    if kind == "files":
        return ('只输出 JSON：{"files":[{"path":"模块.py","content":"<完整文件内容>"}]}\n'
                "content 必须是完整文件（含 import），不是片段、不是 diff、不要围栏外文字。")
    if kind == "edits":
        return ('只输出 JSON：{"edits":[{"op":"replace","symbol":"函数名",'
                '"code":"def 函数名(...):\\n    ..."}]}\n'
                "code 是这一个函数的完整定义（含 def 行、类型注解、docstring）。")
    if kind == "plan":
        return ('只输出 JSON：{"plan":["第一条","第二条"],"orders":[{"kind":"implement",'
                '"target":"模块.py","symbol":"函数名","detail":"做什么"}]}')
    if kind == "files":
        pass
    if kind == "delegate":
        return ('如果你觉得某些**机械性内容**让本地模型写更划算（样板/校验/日志/适配层），'
                '用 delegate 委托，自己只写一句话规格：\n'
                '{"delegate":[{"kind":"validation|boilerplate|logging|adapter",'
                '"target":"函数名","spec":"一句话说清要补什么"}]}\n'
                '每个模块最多 3 项；本地模型写完后会过判据，不过会打回给你自己写。')
    if kind == "question":
        return ('如果你判断信息不足以下结论，**不要猜**，只输出 JSON：'
                '{"question":"要问人的一句话","options":["可选方案A","可选方案B"],'
                '"why":"为什么必须问"}')
    return "只输出 JSON。"


def render(kind: str) -> str:
    """完整协议块：分工 + 输出格式。接进任何给远端的提示词。"""
    return capabilities_block() + "\n\n【输出格式（严格）】\n" + output_format(kind)
