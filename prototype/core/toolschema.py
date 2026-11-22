"""工具的 JSON Schema（DeepSeek 原生 function calling 格式）。

官方文档：https://api-docs.deepseek.com/guides/tool_calls
  请求：{"tools":[{"type":"function","function":{"name","description","parameters"}}]}
  响应：choices[0].message.tool_calls[]，function.arguments 是 **JSON 字符串**
  回传：{"role":"tool","tool_call_id":<id>,"content":<结果>}

为什么要 schema：模型只有看到参数声明，才知道每个工具要什么、参数叫什么类型；
只靠提示词里的 `<tool name=... />` 语法是**模拟**，原生 schema 才是官方通路
（实测：远端是 deepseek-flash，官方支持 function calling，且思考模式同样支持）。
"""

from __future__ import annotations

import json


def _s(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object",
            "properties": properties,
            "required": list(required or [])}


_STR = {"type": "string"}
_INT = {"type": "integer"}

TOOL_SCHEMAS: list[dict] = [
    {"type": "function", "function": {
        "name": "read_file",
        "description": "读文件内容（可按行范围；不给范围读开头一段）。只读。",
        "parameters": _s({"path": {**_STR, "description": "相对仓库根的路径"},
                          "start": {**_INT, "description": "起始行（1 起）"},
                          "end": {**_INT, "description": "结束行（含）"}},
                         ["path"])}},
    {"type": "function", "function": {
        "name": "outline",
        "description": "列出这个文件里已有的函数/类/常量及其行号范围（先看再改，避免重写已有实现）。只读。",
        "parameters": _s({"path": {**_STR, "description": "相对仓库根的路径"}}, ["path"])}},
    {"type": "function", "function": {
        "name": "list_files",
        "description": "列出仓库（或某个子目录）下的文件。只读。",
        "parameters": _s({"sub": {**_STR, "description": "子目录，可空"},
                          "limit": {**_INT, "description": "最多列多少个"}})}},
    {"type": "function", "function": {
        "name": "grep",
        "description": "按正则搜代码/事实文件（.sixcoder 下有 web_routes.json / blueprint.json / slices/*.txt 契约）。只读。",
        "parameters": _s({"pattern": {**_STR, "description": "正则"},
                          "path": {**_STR, "description": "子目录或文件，默认仓库根"},
                          "limit": {**_INT, "description": "最多几条"}},
                         ["pattern"])}},
    {"type": "function", "function": {
        "name": "imports",
        "description": "看这个文件 import 了谁、本地定义了什么、**哪些名字疑似没定义**（最常见的错：用了没 import 的名字）。只读。",
        "parameters": _s({"path": {**_STR, "description": "相对仓库根的路径"}}, ["path"])}},
    {"type": "function", "function": {
        "name": "check_import",
        "description": "试 import 这个模块，返回能否导入和真实报错（等价本地的 import 冒烟）。只读。",
        "parameters": _s({"path": {**_STR, "description": "相对仓库根的 .py 路径"}}, ["path"])}},
    {"type": "function", "function": {
        "name": "contract_for",
        "description": "取这个模块在**本片契约**里的那一节：函数签名、必须满足的示例、假设（判据从哪来）。只读。",
        "parameters": _s({"module": {**_STR, "description": "模块路径"}}, ["module"])}},
    {"type": "function", "function": {
        "name": "route_status",
        "description": ("查一个端点为什么没接上：分因归类（已接线 / 缺函数要你写 / "
                        "接线被拒是引擎的问题）。端点报 404 时先查这个，别盲目重写代码。只读。"),
        "parameters": _s({"method": {**_STR, "description": "HTTP 方法，如 GET/POST"},
                          "path": {**_STR, "description": "端点路径，如 /api/v1/batches"}},
                         ["path"])}},
    {"type": "function", "function": {
        "name": "git_history",
        "description": ("看某个文件最近几个版本长什么样（git log + git show 的前 40 行）。"
                        "改坏文件前先看历史原文，别凭记忆重写。只读。"),
        "parameters": _s({"path": {**_STR, "description": "相对仓库根的文件路径（可空=全仓历史）"},
                          "limit": {**_INT, "description": "看几个版本，默认 3"}}, [])}},
    {"type": "function", "function": {
        "name": "run_python",
        "description": ("跑一段 Python 片段，返回真实结果（rc + 输出）。"
                        "用来验证一个函数对某输入返回什么、某个表达式算出来是几。只读。"),
        "parameters": _s({"code": {**_STR, "description": "Python 源码片段（≤8000 字符）"}},
                         ["code"])}},
    {"type": "function", "function": {
        "name": "requirement_for",
        "description": ("查需求原文里关于某主题的段落：全局验收指标、架构边界、规模目标"
                        "（这些在切片契约里看不到）。只读。"),
        "parameters": _s({"topic": {**_STR, "description": "主题关键词，如 blocking pair / 配额 / 算法包"}},
                         [])}},
    {"type": "function", "function": {
        "name": "run_tests",
        "description": "跑指定测试文件（pytest），返回结果摘要。只读（不写测试）。",
        "parameters": _s({"target": {**_STR, "description": "测试文件或目录"}}, ["target"])}},
    {"type": "function", "function": {
        "name": "static_analysis",
        "description": "对一个文件做语法/编译检查 + 行数复杂度粗报。只读。",
        "parameters": _s({"path": {**_STR, "description": "相对仓库根的路径"}}, ["path"])}},
    {"type": "function", "function": {
        "name": "write_memory",
        "description": "把确认过的工程事实/约束写进记忆库（下一轮会注入给你）。只能写 .sixcoder/knowledge.jsonl。",
        "parameters": _s({"type": {**_STR, "description": "fact / constraint / pitfall"},
                          "text": {**_STR, "description": "要记的内容"},
                          "src": {**_STR, "description": "来源标注"}},
                         ["text"])}},
    {"type": "function", "function": {
        "name": "decide_route",
        "description": (
            "补偿路径决策：在决定怎么实现/验收一个需求前，先判断该走哪条路——"
            "直接生成（判据可机械化+能起探针）、插件桥+产物验收（闭源宿主如 PR/Office）、"
            "降维判据+人在环（审美型质量）、授权部署（人预留了服务器并开放通道，"
            "白名单命令+强制人审）、结构性拒绝（进不去且判据不可判）。"
            "只读：不改代码，返回路径、理由、引擎能验什么、人要做什么、补不到哪。"),
        "parameters": _s({
            "target": {**_STR, "description": "目标形态描述，如 'PR 自动剪辑插件' / '订单管理 API'"},
            "closed_host": {"type": "boolean", "description": "目标是否寄生在闭源宿主里（PR/Office/AutoCAD 等），引擎进不去其进程"},
            "can_observe_runtime": {"type": "boolean", "description": "引擎能否起目标进程并跑探针观测结果"},
            "mechanical_verdict": {"type": "boolean", "description": "验收判据能否写成机器可判定（HTTP 状态码/DOM/数值/退出码）"},
            "subjective_quality": {"type": "boolean", "description": "交付质量是否主要依赖主观审美/体验判断（好不好看/顺不顺手）"},
            "deploy_authorized": {"type": "boolean", "description": "人是否已预留部署目标并开放受控通道（用于部署/上线类需求；未授权一律不走部署路径）"},
            "requirement": {**_STR, "description": "需求原文摘要（只用于回显，不参与判定）"},
        }, ["target"])}},
    {"type": "function", "function": {
        "name": "deploy_targets",
        "description": (
            "列出人工预先预留的部署服务器（受控部署通道）。只读：只给目标 id/host/user/"
            "认证方式（agent / 私钥引用 / 用户名密码）/凭据是否已配置(掩码)/命令白名单，"
            "**绝不返回私钥或密码本体**。只有人开了通道并登记过的目标才出现。"
            "先在 decide_route 判出需要部署，再用这个看有哪些目标可用。"),
        "parameters": _s({})}},
    {"type": "function", "function": {
        "name": "deploy_exec",
        "description": (
            "在预留服务器上执行一条命令（受控部署通道）。**严格约束，别指望能任意 ssh**："
            "① 只能连 deploy_targets 列出的目标；② 命令必须整条命中该目标的 allow_commands "
            "前缀且不含 shell 元字符，否则拒绝；③ 执行前先过**三方协商（≥2 同意才放行）**，"
            "审核者反对/弃权/三方没配好都拒绝；④ 还要**强制人工审核**，没人答/超时=拒绝；"
            "⑤ 全程写审计日志。认证支持 agent / 私钥路径 / 用户名密码三种，密码只按"
            "env:/file:/prompt 引用由本机读取，绝不落盘、不进日志。远端只负责提出"
            "'在哪台跑哪条命令+为什么'，放行权在三方+人。返回真实 rc 与输出，失败如实报告。"),
        "parameters": _s({
            "target": {**_STR, "description": "deploy_targets 里的目标 id"},
            "command": {**_STR, "description": "要执行的命令（必须命中该目标白名单前缀）"},
            "reason": {**_STR, "description": "为什么要跑这条命令（给人审看的）"},
        }, ["target", "command"])}},
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


def tool_schemas() -> list[dict]:
    """给远端的工具清单（深拷贝，避免调用方改坏全局）。"""
    return json.loads(json.dumps(TOOL_SCHEMAS))
