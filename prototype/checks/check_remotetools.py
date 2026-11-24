"""远端工具循环（§13.4 第 3 步）自检：只读工具往返 + 记忆写入 + 受保护区拒绝。

用假 complete_fn 模拟远端发 <tool> 调用，不依赖真实远端 API。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remotetools  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


MOD = "def abs_sum(a, b):\n    return abs(a) + abs(b)\n"


def main() -> int:
    print("远端工具循环自检（§13.4 第 3 步）")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        (ws / "m.py").write_text(MOD, encoding="utf-8")

        # 假远端：第 0 轮发 read_file+grep+试图写测试；第 1 轮发 write_memory；第 2 轮给结论
        def fake_complete(instruction, messages):
            n_assist = sum(1 for m in messages if m.get("role") == "assistant")
            if n_assist == 0:
                return ('<tool name="read_file" path="m.py" start="1" end="3" />\n'
                        '<tool name="grep" pattern="def " path="." />\n'
                        '<tool name="write_file" path="tests/test_m.py" />')
            if n_assist == 1:
                return '<tool name="write_memory" type="fact" text="m.py 的 abs_sum 返回绝对值之和" />'
            return "我已查看代码：abs_sum 返回绝对值之和，逻辑正确。"

        loop = remotetools.ToolLoop(ws, level="L", max_rounds=6, complete_fn=fake_complete)
        res = loop.run("检查 m.py 的 abs_sum 实现")

        check("工具调用被解析（read_file/grep/write_file/write_memory）",
              len(loop.tool_calls) >= 4)
        check("read_file 出现在调用里", any("read_file" in c for c in loop.tool_calls))
        check("grep 出现在调用里", any("grep" in c for c in loop.tool_calls))
        check("write_file 出现在调用里", any("write_file" in c for c in loop.tool_calls))

        # 受保护区：试图写 tests/ 被拒
        check("写测试文件被受保护区拒绝", len(res["denied"]) >= 1
              and any("测试" in x for x in res["denied"]))

        # 记忆写入成功
        check("write_memory 写入成功", len(res["memory_written"]) == 1)
        check("记忆落盘到 knowledge.jsonl",
              (ws / ".sixcoder" / "knowledge.jsonl").exists())
        check("最终结论无工具调用、循环在 3 轮内结束",
              "abs_sum" in res["answer"] and res["rounds"] <= 3)

        # 解析器健壮性：坏标签不应崩
        safe = remotetools.parse_tool_calls("没有工具调用的普通回复 <tool 残缺")
        check("解析器对残缺标签安全返回空", safe == [])
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（只读工具往返 / 受保护区拒绝 / 记忆写入 / 循环收敛 / 解析健壮性）")
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
