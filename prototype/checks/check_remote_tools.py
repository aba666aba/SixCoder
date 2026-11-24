"""远端能力面自检：工具要够全面、**函数体路径必须真的挂上工具**、"写不出来"要换路。

2026-11-09 用户指令：
  · "工具不能多写一点全面一点吗，让 api 充分利用"
  · "觉得写不出来了是不是需要调整"

对应三个真实缺口：
  1. 工具面只有 6 个（read_file/list_files/grep/run_tests/static_analysis/write_memory），
     缺"这个文件里已有什么结构""它依赖谁""能不能 import""判据要求是什么"——
     远端正因此用错字段名、漏 import、猜契约。
  2. `author_bodies`（函数体路径，绝大多数单子走这条）**零工具**：
     ToolLoop 早就实现，但写作路径只把说明追加进 prompt，从不解析/执行/回灌。
  3. 同一份失败证据连续出现 = 远端"写不出来"，原样重试是白烧钱 → 必须换路。

跑法: python checks/check_remote_tools.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import author, remotetools  # noqa: E402
from core import remote as _remote_mod  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class F:
    def __init__(self, n: str):
        self.name = n
        self.params = []
        self.returns = "int"
        self.doc = ""
        self.examples = []


def main() -> int:
    print("[1] 工具面：必须覆盖\"看结构/看依赖/试 import/查契约\"")
    guide = remotetools.GUIDE
    for t in ("read_file", "outline", "list_files", "grep", "imports",
              "check_import", "contract_for", "run_tests", "static_analysis",
              "write_memory"):
        # 2026-11-09：GUIDE 改成原生 function calling 说明后不再是 XML 形态，
        # 断言的**意图**是"GUIDE 里必须提到每个工具名"，所以查名字本身。
        check("GUIDE 里有 %s" % t, t in guide)
    for fn in ("_outline", "_imports", "_check_import", "_contract_for"):
        check("实现存在：%s" % fn, callable(getattr(remotetools, fn, None)))

    print("[2] 新工具的输出是真的（有内容、不是报错）")
    ws = Path(tempfile.mkdtemp())
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "m.py").write_text(
        "import os\n\n\n"
        "def alpha(x: int) -> int:\n"
        '    """alpha。"""\n'
        "    return x + 1\n\n\n"
        "def beta() -> int:\n"
        "    return undefined_name_here\n", encoding="utf-8")
    out = remotetools._outline(ws, "app/m.py")
    check("outline 列出函数与行号", "def alpha" in out and "def beta" in out, out[:120])
    imp = remotetools._imports(ws, "app/m.py")
    check("imports 点出未定义的名字", "undefined_name_here" in imp, imp[:200])
    ci = remotetools._check_import(ws, "app/m.py")
    check("check_import 给出导入结果", ("成功" in ci) or ("失败" in ci), ci[:120])
    cf = remotetools._contract_for(ws, "app/m.py")
    check("contract_for 有明确答复", bool(cf.strip()) and "没找到" not in cf or "找不到" not in cf,
          cf[:120])

    print("[3] 函数体路径必须真的挂上工具（不是只把说明塞进 prompt）")
    src = (ROOT / "core" / "author.py").read_text(encoding="utf-8")
    check("author_bodies 有 tool_loop 开关", "tool_loop: bool = True" in src)
    check("写作路径真的跑 ToolLoop", "_rtl.ToolLoop(" in src)
    check("工具结果会回灌给远端（传 messages）", "messages=messages" in src)
    check("工具循环留痕（author_tools 事件）", 'emit("author_tools"' in src)

    print("[4] \"写不出来\"要换路：同一错误重复 → 提示查证 + 只改点名函数")
    check("记录上一轮证据", "_prev_ev" in src)
    check("识别同一证据重复", "stuck_same_evidence" in src)
    check("下一轮提示先查证再改", "完全相同" in src and "outline" in src)

    print("[5] 端到端：远端在函数体路径上真的调了工具")
    ws2 = Path(tempfile.mkdtemp())
    (ws2 / "app").mkdir(parents=True)
    (ws2 / "app" / "m.py").write_text("def alpha(x: int) -> int:\n    return 0\n", encoding="utf-8")
    prompts: list[str] = []
    calls = {"n": 0}

    def _fake(prompt, **kw):
        calls["n"] += 1
        if kw.get("messages") and len(kw["messages"]) > 2:
            # 第二轮：工具结果已经回灌 → 交出函数体
            return {"ok": True, "answer": "```python\n# ===== alpha =====\nreturn 1\n```\n",
                    "usage": {"completion_tokens": 20}}
        prompts.append(prompt)
        return {"ok": True,
                "answer": '<tool name="outline" path="app/m.py" />',
                "usage": {"completion_tokens": 20}}

    _orig_complete, _orig_verify = _remote_mod.complete, author._verify
    _remote_mod.complete = _fake
    author._verify = lambda w, c, scope=None: {"ok": True, "feedback": ""}
    events: list[tuple[str, dict]] = []
    try:
        res = author.author_bodies(ws2, "app/m.py",
                                   type("C", (), {"funcs": [F("alpha")]})(), {},
                                   attempts=2, emit=lambda k, v: events.append((k, v)))
    finally:
        _remote_mod.complete, author._verify = _orig_complete, _orig_verify
    check("写作成功", bool(res.get("ok")), str(res.get("reason"))[:160])
    check("远端至少被调用 2 次（先工具、后交体）", calls["n"] >= 2, str(calls))
    check("有 author_tools 留痕", any(k == "author_tools" for k, _ in events),
          str([k for k, _ in events]))

    print("[4] 工具轮不得覆盖答案 + 跑满轮次要有收尾追问（2026-11-09 实锤）")
    src = Path(remotetools.__file__).read_text(encoding="utf-8")
    check("答案只在'这一轮没有再调工具'时更新",
          "_last_with_text" in src and "if not native and reply.strip()" in src)
    check("有收尾追问（不再挂工具，只要正文）",
          "allow_tools=False" in src and "不要再调用任何工具" in src)
    check("收尾追问会追问一次（多一轮）", "self.rounds += 1" in src)
    check("GUIDE 是原生 function calling 口径（不再是 XML 协议）",
          "<tool name=" not in remotetools.GUIDE)
    check("GUIDE 明确要求最终给出答案正文",
          "最终必须给出答案正文" in remotetools.GUIDE)
    # 行为级：连着几轮都在调工具、正文为空 → 收尾轮必须拿到答案
    _seq = [{"answer": "", "tool_calls": [
        {"id": "c1", "function": {"name": "list_files", "arguments": "{}"}}]},
        {"answer": "", "tool_calls": [
         {"id": "c2", "function": {"name": "list_files", "arguments": "{}"}}]},
        {"answer": "", "tool_calls": [
         {"id": "c3", "function": {"name": "list_files", "arguments": "{}"}}]}]

    def _fake_final(_instr, messages, _tools=None):
        return {"answer": "```python\ndef alpha():\n    return 1\n```\n",
                "tool_calls": [], "message": {}, "finish_reason": "stop"}

    _calls2 = {"n": 0}

    def _fake_seq(_instr, messages, _tools=None):
        _calls2["n"] += 1
        if _calls2["n"] <= len(_seq):
            d = _seq[_calls2["n"] - 1]
            return {"answer": d["answer"], "tool_calls": d["tool_calls"],
                    "message": {}, "finish_reason": "tool_calls"}
        return _fake_final(_instr, messages, _tools)

    _ws3 = Path(tempfile.mkdtemp())
    (_ws3 / "app").mkdir(parents=True)
    (_ws3 / "app" / "m.py").write_text("def alpha():\n    return 0\n", encoding="utf-8")
    _lp = remotetools.ToolLoop(_ws3, max_rounds=3, complete_fn=_fake_seq)
    _res3 = _lp.run("写 alpha")
    check("三轮都在调工具后仍拿到了答案（收尾轮）",
          "def alpha" in str(_res3.get("answer") or ""), str(_res3.get("answer"))[:120])
    check("收尾轮确实多调用了一次（4 次）", _calls2["n"] == 4, str(_calls2))

    print()
    # ── 全文已注入时，别再让它调工具重读同一份（2026-11-10 22:04 实测）──────
    # 15 次 author_start + 23 次重试 → 打出 166 次 tool_loop；每次工具轮都是一次
    # 带完整上下文的付费请求 → 调用量是必要量的 3~4 倍。
    # 注意：这不是"少给上下文"——该给的全文一个字都没少，只是不许重复付费再取一遍。
    print("[N] 上下文已给全时，工具说明要明确「别再重读这个文件」")
    from core import remotetools as _rtl2
    _g_off = _rtl2.tool_loop_guide(context_complete=False)
    _g_on = _rtl2.tool_loop_guide(context_complete=True)
    check("缺省（没给全）不加这句，避免误导",
          "不要为了再看这个文件而调用工具" not in _g_off)
    check("已给全时明确禁止重读", "不要为了再看这个文件而调用工具" in _g_on)
    check("仍允许看**别的**文件（不是禁用工具）", "别的文件" in _g_on)
    _author_src = (Path(__file__).resolve().parents[1] / "core" / "author.py").read_text(encoding="utf-8")
    check("author.py 真的把 read_ok 传下去了", "context_complete=bool(read_ok)" in _author_src)

    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（工具面全面 / 函数体路径真挂工具 / 写不出来会换路）")
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
