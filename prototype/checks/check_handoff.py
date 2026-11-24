"""接力包解析自检：完整 JSON、被截断的 JSON、纯文本兜底都必须能救出编辑。

跑法: python check_handoff.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import handoff  # noqa: E402


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    print("接力包解析自检")

    full = json.dumps({"edits": [
        {"op": "replace", "symbol": "load", "code": "def load(p):\n    return 1"},
        {"op": "add", "symbol": "save", "code": "def save(p, r):\n    return 2", "after": "load"},
    ]}, ensure_ascii=False)
    edits = handoff._parse_answer(full)
    ok &= check("完整 JSON 解析出 2 条", len(edits) == 2, str(edits))
    ok &= check("add/replace 归一化正确",
                [e["op"] for e in edits] == ["replace", "add"], str([e["op"] for e in edits]))

    # 模拟 max_tokens 截断：第二个对象不完整
    truncated = full[: full.rindex("{", 0, full.rindex('"code": "def save'))]
    edits2 = handoff._parse_answer(truncated)
    ok &= check("截断 JSON 仍救回第一条", len(edits2) == 1 and edits2[0]["symbol"] == "load",
                str(edits2))

    # 带围栏
    fenced = "```json\n" + full + "\n```"
    ok &= check("能剥掉 Markdown 围栏", len(handoff._parse_answer(fenced)) == 2)

    # 完全没有 JSON → 不瞎猜
    ok &= check("无效文本返回空", handoff._parse_answer("我觉得应该这样改……") == [])

    # 缺 code 的对象必须被丢弃
    bad = json.dumps({"edits": [{"op": "replace", "symbol": "x"},
                                {"op": "replace", "symbol": "y", "code": "def y():\n    return 1"}]})
    ok &= check("缺少 code 的编辑被丢弃", [e["symbol"] for e in handoff._parse_answer(bad)] == ["y"])

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
