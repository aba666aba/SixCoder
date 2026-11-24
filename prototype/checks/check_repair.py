r"""A/B/C 三处修复的自检：缺符号要核实、坏测试文件要修测试文件、思考要按阶梯升。

三个真实事故（都在本轮"链路自检"里暴露）：

A. 自由变量被当成"缺失符号"
   测试里写 `divide(a, b)` → `NameError: name 'a' is not defined` → 旧逻辑提示模型
   "必须提供这些符号：a、b" → 模型往测试文件里加 `from calc import a` → 级联 ImportError。

B. 坏的是测试文件，却一直在改实现
   测试文件里有 `from test_calc import test_calc_divide_1`（自己 import 自己）。
   实现节点怎么改都不会绿，远端救火也白跑。

C. 思考模式一刀切
   实测（docs/模型门槛 §4.0）：符号级编辑关思考不掉质量、completion 少 6 倍；
   但开放式诊断（架构师找根因）关思考有风险。所以改成阶梯：
   **第一次 off；off 没解决（已问过架构师且仍无增长）才升 on；整夜最多升 1 次。**

跑法：python check_repair.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import agent, props  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


class Cap:
    def __init__(self):
        self.events = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def kinds(self):
        return [k for k, _ in self.events]


def ws_with(impl: str, test: str) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="rep_"))
    (ws / "calc.py").write_text(impl, encoding="utf-8")
    (ws / "test_calc.py").write_text(test, encoding="utf-8")
    return ws


def main() -> int:
    print("[A] 缺符号必须核实，不能把自由变量当符号")
    free = "NameError: name 'a' is not defined"
    check("自由变量 a 不算缺符号", agent._missing_symbols(free) == [], str(agent._missing_symbols(free)))
    check("自由变量 b（契约里也没 b( ）不算",
          agent._missing_symbols("NameError: name 'b' is not defined", contract="divide(a, b) -> float") == [])
    real = "ImportError: cannot import name 'divide' from 'calc'"
    check("真缺函数 divide 要识别", agent._missing_symbols(real, contract="divide(a, b) -> float") != [])
    check("没有契约时也不瞎认", agent._missing_symbols(real) == [] or
          "divide" in " ".join(agent._missing_symbols(real)))
    ws = ws_with("def add(a, b):\n    return a + b\n", "from calc import add\n")
    check("给了工作区就以符号表为准（add 存在）",
          agent._missing_symbols("ImportError: cannot import name 'add' from 'calc'",
                                 ws, "calc.py", "add(a, b)") != [])
    check("给了工作区就拒绝不存在的符号",
          agent._missing_symbols("ImportError: cannot import name 'nope' from 'calc'",
                                 ws, "calc.py", "nope(a)") == [])

    print("[B] 坏测试文件：外科手术修 import，**绝不允许删测试**（删了就是假绿）")
    broken_test = ("import pytest\nfrom calc import divide\n\n"
                   "from test_calc import test_calc_divide_1\n"
                   "from calc import a\n\n"
                   "def test_divide_ok():\n    assert divide(6, 3) == 2.0\n")
    ok_test = "import pytest\nfrom calc import add\n\ndef test_add():\n    assert add(2,3)==5\n"
    impl = "def add(a: int, b: int) -> int:\n    return a + b\n"
    ws_b = ws_with(impl, broken_test)
    detail_bad = ("test_calc.py:13: in <module>\n    from test_calc import test_calc_divide_1\n"
                  "E   ImportError: cannot import name 'test_calc_divide_1' from partially "
                  "initialized module 'test_calc'")
    hit = agent._test_file_self_broken(detail_bad, ["test_calc.py"])
    check("识别出自我导入的坏测试文件", bool(hit), hit)
    detail_ok = "assert add(2, 3) == 5\nE   AssertionError: assert 6 == 5"
    check("普通断言失败不误判", agent._test_file_self_broken(detail_ok, ["test_calc.py"]) == "")
    cap = Cap()
    node = {"nid": "n-b", "kind": "impl", "path": "calc.py",
            "contract": "add(a: int, b: int) -> int：返回两数之和"}
    rep = agent._repair_test_files(ws_b, node, "t-b", cap)
    check("坏测试文件被修（不是被删）",
          bool(rep["repaired"] or rep["rewritten"]) and not rep.get("removed"), str(rep))
    tpath = ws_b / "test_calc.py"
    check("测试文件仍然存在（绝不删测试）", tpath.exists())
    new_src = tpath.read_text(encoding="utf-8") if tpath.exists() else ""
    check("不再自我导入", "from test_calc import" not in new_src, new_src[:160])
    check("不再从实现导入参数名 a", "import a" not in new_src, new_src[:160])
    check("修完仍至少有一个测试", "def test_" in new_src, new_src[:160])
    check("修完语法合法", _parses(new_src), new_src[:160])

    ws_ok = ws_with(impl, ok_test)
    rep2 = agent._repair_test_files(ws_ok, node, "t-ok", Cap())
    check("语法正常的测试文件不许动",
          not rep2["repaired"] and not rep2["rewritten"]
          and (ws_ok / "test_calc.py").read_text(encoding="utf-8") == ok_test, str(rep2))

    print("[B2] 重名定义必须判红（7 个 divide 那次的教训）")
    from core import gates
    ws_d = Path(tempfile.mkdtemp(prefix="dup_"))
    (ws_d / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n\n\ndef add(a, b):\n    return a - b\n", encoding="utf-8")
    (ws_d / "ok.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    d = gates.gate_duplicates(ws_d, ["calc.py"])
    check("同名定义判红", not d.ok, d.detail[:120])
    check("正常文件不误杀", gates.gate_duplicates(ws_d, ["ok.py"]).ok)

    print("[C] 思考模式按阶梯升（off → on，最多一次）")
    import inspect
    src = inspect.getsource(agent._remote_escalation_enabled)
    check("远端救火默认开、可关", "SIXCODER_REMOTE_ESCALATE" in src)
    comp_src = Path("core/company.py").read_text(encoding="utf-8")
    check("架构师首次用 off", 'thinking = "off"' in comp_src)
    check("卡住才升 on", 'thinking = "on"' in comp_src and "stall_rounds" in comp_src)
    check("整夜最多升一次", "MAX_THINK_ON = 1" in comp_src and "think_on_used < MAX_THINK_ON" in comp_src)
    it_src = Path("core/iterate.py").read_text(encoding="utf-8")
    check("ask_architect 支持 thinking 参数", "thinking: str = \"off\"" in it_src)
    check("ask_architect 把 thinking 传给远端", "thinking=thinking" in it_src)
    rm_src = Path("core/remote.py").read_text(encoding="utf-8")
    check("remote.complete 接受 thinking 覆盖", "thinking: str | None = None" in rm_src)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（A 不瞎补符号 / B 会修测试文件 / C 思考按阶梯升且封顶）")
    return 0


def _parses(text: str) -> bool:
    import ast
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
