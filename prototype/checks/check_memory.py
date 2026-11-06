"""记忆层（§13.4 第 5 步）自检：.sixcoder/knowledge.jsonl 读写 + 稳定前缀注入。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import memory  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("记忆层自检（§13.4 第 5 步）")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        ms = memory.MemoryStore(ws)
        check("初始为空", ms.size() == 0)

        e1 = ms.append("仓库使用 Python 3.11，依赖见 requirements.txt", "fact", "probe")
        check("append 返回条目", e1 is not None and e1["type"] == "fact")
        check("size == 1", ms.size() == 1)

        bad = ms.append("", "fact")
        check("空文本被拒", bad is None)

        weird = ms.append("随便记点", "bogus_type")
        check("非法类型退回 fact", weird is not None and weird["type"] == "fact")

        ms.append("不要动 tests/ 下的文件", "constraint")
        items = ms.read()
        check("read 返回 3 条", len(items) == 3)
        check("条目含 ts/type/text/src", all({"ts", "type", "text"} <= set(it) for it in items))

        pre = ms.render_prefix()
        check("render_prefix 含标题", "项目记忆" in pre)
        check("render_prefix 含事实", "Python 3.11" in pre)
        check("render_prefix 含约束", "tests/" in pre)

        # 落盘路径正确
        check("落盘到 .sixcoder/knowledge.jsonl",
              (ws / ".sixcoder" / "knowledge.jsonl").exists())
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（读写 / 类型规整 / 稳定前缀注入 / 落盘路径）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
