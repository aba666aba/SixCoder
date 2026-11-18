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
]

TOOL_NAMES = [t["function"]["name"] for t in TOOL_SCHEMAS]


def tool_schemas() -> list[dict]:
    """给远端的工具清单（深拷贝，避免调用方改坏全局）。"""
    return json.loads(json.dumps(TOOL_SCHEMAS))
