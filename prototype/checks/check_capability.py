"""模型能力表 / 工具通路降级自检（2026-11-10）。

为什么单独一个脚本：
    这条线是"模型能力参差不齐时的通用兜底"。它的**两个风险**必须钉死：
      ① 降级**必须真的生效** —— 换通路后请求里不能再带 tools（否则照样 400）；
      ② 降级**必须被记住** —— 否则每个项目、每一轮都要重新踩一遍。

跑法: python check_capability.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import capability, remotetools  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)
    return cond


def main() -> int:
    ok = True
    print("模型能力表 / 工具通路自检")

    # 本地记忆隔离到临时目录（不许污染真机器上的记忆文件）
    _memo_real = capability.MEMO_FILE
    capability.MEMO_FILE = Path(tempfile.mkdtemp(prefix="cap_")) / "capability.json"

    # ── 1) 内置已知表 ────────────────────────────────────────────────────────
    print("[1] 内置已知表")
    ok &= check("deepseek-flash → native（真机验证过）",
                capability.known_mode("deepseek-flash", "deepseek") == "native")
    ok &= check("glm-5.3 → native（官方有 function calling 文档）",
                capability.known_mode("glm-5.3", "glm") == "native")
    ok &= check("glm-4.5v → none（官方路由标记不支持 function call）",
                capability.known_mode("glm-4.5v", "glm") == "none")
    ok &= check("没登记的模型 → 未知（空）",
                capability.known_mode("some-unknown-model", "") == "")
    ok &= check("未知模型默认先试 native（乐观）",
                capability.tool_mode("some-unknown-model", "") == "native")

    # ── 2) 档案显式声明优先 ─────────────────────────────────────────────────
    print("[2] 档案显式声明优先于内置表")
    ok &= check("档案写 false → none（哪怕它是已知支持模型）",
                capability.tool_mode("deepseek-flash", "deepseek",
                                     profile={"supports_tools": False}) == "none")
    ok &= check("档案写 true → native（哪怕它是视觉模型）",
                capability.tool_mode("glm-4.5v", "glm",
                                     profile={"supports_tools": True}) == "native")
    ok &= check("档案没写 → 回到内置表",
                capability.tool_mode("glm-4.5v", "glm", profile={}) == "none")

    # ── 3) 本地记忆优先于内置表，且能记住降级 ────────────────────────────────
    print("[3] 真踩过的降级被记住")
    ok &= check("还没踩过 → 没有记忆", capability.learned_mode("glm-5.3") == "")
    capability.remember("glm-5.3", "xml", why="tools not supported")
    ok &= check("记住后 learned_mode 返回 xml",
                capability.learned_mode("glm-5.3") == "xml")
    ok &= check("记忆优先于内置表（内置说 native，记忆说 xml）",
                capability.tool_mode("glm-5.3", "glm") == "xml")
    ok &= check("记忆文件真的落盘了",
                capability.MEMO_FILE.exists(), str(capability.MEMO_FILE))
    capability.remember("glm-x", "native", why="不该记升级")
    ok &= check("**只记降级、不记升级**",
                capability.learned_mode("glm-x") == "")

    # ── 4) 错误识别 ─────────────────────────────────────────────────────────
    print("[4] 识别「这个模型不支持 tools」")
    for text in ("tools not supported",
                 '{"code":"invalid_parameter","message":"tools not supported"}',
                 "Tool use is not supported",
                 "does not support tool calls"):
        ok &= check(f"认得出：{text[:40]}",
                    capability.is_tools_unsupported(text))
    for text in ("HTTP 500", "限流，请稍后重试", "timeout", "", "401 Unauthorized"):
        ok &= check(f"不误认：{text[:24] or '(空)'}",
                    not capability.is_tools_unsupported(text))

    # ── 5) 三档提示词 ───────────────────────────────────────────────────────
    print("[5] 每条通路一份提示词")
    g_native = remotetools.guide_for("native")
    g_xml = remotetools.guide_for("xml")
    g_none = remotetools.guide_for("none")
    ok &= check("native 用 GUIDE", g_native == remotetools.GUIDE)
    ok &= check("xml 用 GUIDE_XML", g_xml == remotetools.GUIDE_XML)
    ok &= check("none 用 GUIDE_NONE", g_none == remotetools.GUIDE_NONE)
    ok &= check("三份互不相同",
                len({g_native, g_xml, g_none}) == 3)
    ok &= check("xml 版教 XML 调用格式", "<tool name=" in g_xml)
    ok &= check("**xml 版不再说「不要输出 XML」**（原来那句把兜底堵死了）",
                "不要输出 XML" not in g_xml)
    ok &= check("none 版明说没有工具", "没有" in g_none)
    ok &= check("none 版禁止编造文件内容",
                ("不要" in g_none and ("编造" in g_none or "假装" in g_none)), "")

    # ── 6) 按通路决定请求里带不带 tools ─────────────────────────────────────
    print("[6] 按通路决定带不带 tools")
    ws = Path(tempfile.mkdtemp(prefix="capws_"))
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "m.py").write_text("def alpha(x: int) -> int:\n    return 0\n",
                                     encoding="utf-8")
    for mode, want_tools in (("native", True), ("xml", False), ("none", False)):
        seen: list = []

        def _fc(_instr, messages, tools=None, _seen=seen):
            _seen.append({"tools": tools,
                          "system": messages[0].get("content") if messages else ""})
            return {"answer": "好了", "tool_calls": [], "message": {}}

        lp = remotetools.ToolLoop(ws, max_rounds=1, complete_fn=_fc, tool_mode=mode)
        lp.run("看看")
        got = bool(seen and seen[0].get("tools"))
        ok &= check(f"{mode} 通路：请求带 tools = {want_tools}", got == want_tools)
        want_guide = {"native": remotetools.GUIDE, "xml": remotetools.GUIDE_XML,
                      "none": remotetools.GUIDE_NONE}[mode]
        ok &= check(f"{mode} 通路：系统提示词用对应那份",
                    bool(seen) and seen[0].get("system") == want_guide)

    # ── 7) 端点说"不支持 tools" → 自动降级并重试 ───────────────────────────
    print("[7] 自动降级（native → xml）并重试")
    calls: list = []
    events: list = []

    def _fc_bad(_instr, messages, tools=None):
        calls.append({"tools": tools})
        if len(calls) == 1:
            return {"answer": "", "tool_calls": [], "message": {},
                    "error": '{"code":"invalid_parameter","message":"tools not supported"}'}
        return {"answer": "降级后答上来了", "tool_calls": [], "message": {}}

    lp = remotetools.ToolLoop(ws, max_rounds=2, complete_fn=_fc_bad,
                              tool_mode="native", model="fake-model-x",
                              emit=lambda k, p: events.append((k, p)))
    res = lp.run("看看")
    ok &= check("确实请求了两次（第一次失败 → 同轮降级重试）",
                len(calls) == 2, str(len(calls)))
    ok &= check("第一次带 tools", bool(calls[0].get("tools")))
    ok &= check("**降级后不再带 tools**", not calls[-1].get("tools"),
                str(bool(calls[-1].get("tools"))))
    ok &= check("降级事件发出来了",
                any(k == "tool_mode_downgrade" for k, _ in events), str(events[:2]))
    ok &= check("降级后通路变成 xml", lp.tool_mode == "xml", lp.tool_mode)
    ok &= check("降级被记住了（下次直接走 xml）",
                capability.learned_mode("fake-model-x") == "xml",
                str(capability.learned_mode("fake-model-x")))
    ok &= check("降级后仍拿到答案", "降级后答上来了" in (res.get("answer") or ""),
                str(res.get("answer"))[:80])

    # ── 8) 已经 none 的模型不该再发 tools，也不该再降 ───────────────────────
    print("[8] none 通路的模型")
    calls2: list = []

    def _fc_none(_instr, messages, tools=None):
        calls2.append({"tools": tools})
        return {"answer": "我没有工具，只能按提示词写", "tool_calls": [], "message": {}}

    lp2 = remotetools.ToolLoop(ws, max_rounds=1, complete_fn=_fc_none, tool_mode="none")
    lp2.run("看看")
    ok &= check("none 通路不发 tools", not calls2[0].get("tools"))
    ok &= check("none 通路拿得到答案", bool(lp2.run("再看看").get("answer")))

    capability.MEMO_FILE = _memo_real
    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（已知表 / 档案声明 / 记忆优先 / 错误识别 / 三档提示词 / 按通路带不带 tools / 自动降级）")
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
