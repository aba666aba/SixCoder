"""原生 function calling 协议自检（DeepSeek 官方 OpenAI 兼容格式）。

官方文档：https://api-docs.deepseek.com/guides/tool_calls
  请求：{"tools":[{"type":"function","function":{"name","description","parameters"}}]}
  响应：choices[0].message.tool_calls[]，function.arguments 是 **JSON 字符串**
  回传：{"role":"tool","tool_call_id":<id>,"content":<结果>}

跑法: python checks/check_function_calling.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import remote, remotetools  # noqa: E402
from core import toolschema  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 工具 schema：形状必须是 OpenAI/DeepSeek 官方格式")
    schemas = toolschema.tool_schemas()
    check("有 14 个工具", len(schemas) == 14, str(len(schemas)))
    bad = []
    for t in schemas:
        f = t.get("function") or {}
        if t.get("type") != "function" or not f.get("name") or not f.get("description"):
            bad.append(str(t)[:80])
        p = f.get("parameters") or {}
        if p.get("type") != "object" or not isinstance(p.get("properties"), dict):
            bad.append(f.get("name", "?") + ": parameters 形状不对")
    check("每个工具的 type/name/description/parameters 都合规", not bad, str(bad[:3]))
    names = [t["function"]["name"] for t in schemas]
    for n in ("read_file", "outline", "grep", "imports", "check_import",
              "contract_for", "run_tests", "static_analysis", "write_memory",
              "list_files"):
        check("工具齐全：%s" % n, n in names)
    check("read_file 的 path 是必填", "path" in schemas[0]["function"]["parameters"]["required"])

    print("[2] 请求体：带 tools 时必须写进 payload")
    step = {"model": "deepseek-flash", "effort": "none"}
    r = {"base_url": "https://api.deepseek.com", "api_key": "x", "provider": "deepseek"}
    payload, _base, _key = remote._payload_for(step, r, [{"role": "user", "content": "hi"}],
                                               json_mode=False, tools=schemas)
    check("payload 里有 tools", "tools" in payload, str(list(payload))[:160])
    check("tools 用的就是那份 schema", payload.get("tools") == schemas)
    check("默认不强制 tool_choice（auto 由服务端决定）", "tool_choice" not in payload)
    payload2, _b, _k = remote._payload_for(step, r, [{"role": "user", "content": "hi"}],
                                           json_mode=False, tools=schemas, tool_choice="none")
    check("显式 tool_choice 会带上", payload2.get("tool_choice") == "none")

    print("[3] 消息归一化：助手那轮的 tool_calls 必须原样保留")
    # **调真代码**，不要照抄一份（2026-11-10 修）：以前这段是照抄生产逻辑的，
    # 于是"归一化漏字段"这类错自检永远看不见 —— 漏了 reasoning_content 也没人发现。
    msgs = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "outline", "arguments": "{\"path\":\"a.py\"}"}}],
         "reasoning_content": "我先看看这个文件"},
        {"role": "tool", "tool_call_id": "call_1", "content": "结果"},
    ]
    kept = remote.normalize_messages(msgs) or []
    check("空的助手轮（带 tool_calls）没被丢掉",
          any(k.get("tool_calls") for k in kept), str(len(kept)))
    check("tool 结果带着 tool_call_id",
          any(k.get("tool_call_id") == "call_1" for k in kept))
    check("**reasoning_content 原样保留**（带 tools 时官方要求回传，否则 400）",
          any(k.get("reasoning_content") == "我先看看这个文件" for k in kept),
          str([k.get("reasoning_content") for k in kept]))
    check("没有 reasoning_content 的消息不会被塞空串",
          all(k.get("reasoning_content") != "" for k in kept), "")
    check("空消息列表归一化成 None（不发给远端）",
          remote.normalize_messages([]) is None
          and remote.normalize_messages(None) is None, "")

    print("[3b] GLM 也必须带 tools（协议同形，不能只保 DeepSeek）")
    _gstep = {"model": "glm-5.3", "effort": "max"}
    _gr = {"base_url": "https://open.bigmodel.cn/api/paas/v4", "api_key": "x",
           "provider": "glm", "compat": "glm"}
    gp, _gb, _gk = remote._payload_for(_gstep, _gr, [{"role": "user", "content": "hi"}],
                                       json_mode=False, tools=schemas)
    check("GLM 的 payload 里也有 tools", "tools" in gp, str(list(gp))[:160])
    check("GLM 的 tools 就是那份 schema", gp.get("tools") == schemas)
    check("GLM 思考开着（GLM-5.3 不支持 disabled）",
          (gp.get("thinking") or {}).get("type") == "enabled", str(gp.get("thinking")))

    print("[4] 端到端：模型用原生 tool_calls 时，工具会被执行并回灌")
    ws = Path(tempfile.mkdtemp())
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "m.py").write_text("def alpha(x: int) -> int:\n    return 0\n", encoding="utf-8")
    seen: list[dict] = []

    def _complete(_instr, messages, tools=None):
        seen.append({"messages": list(messages), "tools": tools})
        if len(seen) == 1:
            return {"answer": "",
                    "tool_calls": [{"id": "call_x", "type": "function",
                                    "function": {"name": "outline",
                                                 "arguments": json.dumps({"path": "app/m.py"})}}],
                    # 思考模式下的思维链（DeepSeek 官方：带 tools 时必须回传）
                    "message": {"role": "assistant", "content": "",
                                "reasoning_content": "我先看看这个文件有哪些函数"}}
        return {"answer": "看到了：def alpha", "tool_calls": [], "message": {}}

    loop = remotetools.ToolLoop(ws, max_rounds=3, complete_fn=_complete)
    res = loop.run("看看这个文件里有什么")
    check("远端收到 tools schema", bool(seen and seen[0].get("tools")), "tools 没传下去")
    check("工具真的执行了（tool_calls 有记录）", len(res.get("tool_calls") or []) == 1,
          str(res.get("tool_calls")))
    check("结果回灌成 role=tool 的消息",
          any(m.get("role") == "tool" and m.get("tool_call_id") == "call_x"
              for m in seen[-1]["messages"]),
          str([m.get("role") for m in seen[-1]["messages"]]))
    # DeepSeek 官方：带 tools 时上一轮的 reasoning_content 必须原样回传，否则 400。
    check("**第二轮带回了上一轮的 reasoning_content**（官方硬要求）",
          any(m.get("role") == "assistant" and m.get("reasoning_content")
              == "我先看看这个文件有哪些函数" for m in seen[-1]["messages"]),
          str([{k: v for k, v in m.items() if k != "content"}
               for m in seen[-1]["messages"] if m.get("role") == "assistant"]))
    check("最终答案是模型的正文", res.get("answer") == "看到了：def alpha",
          str(res.get("answer"))[:80])

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（原生 function calling 协议合规 / 工具能真被调用）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
