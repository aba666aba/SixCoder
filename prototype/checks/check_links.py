r"""文档链接自检：仓库里的相对 Markdown 链接必须都指向真实存在的文件。

为什么放进 CI：文档是这套系统的"证据链"的一部分（TRACE/DELIVERY 都引用文档）。
一旦目录被整理（本项目就整理过两次），断链会让"30 秒答出来"变成半小时。
断链成本极低就能防住，所以让它挡住 CI。

跑法: python check_links.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FAILS: list[str] = []
LINK_RE = re.compile(r"\]\((\.{0,2}/?[\w\u4e00-\u9fa5\-./]+\.md)(#[^)]*)?\)")


def md_files() -> list[Path]:
    pats = ["*.md", "docs/*.md", "docs/plan/*.md", "docs/legacy/*.md",
            "projects/*.md", "prototype/*.md"]
    out: list[Path] = []
    for pat in pats:
        out += sorted(ROOT.glob(pat))
    return out


def main() -> int:
    files = md_files()
    print(f"扫描 {len(files)} 个 Markdown 文件")
    checked = 0
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in LINK_RE.finditer(text):
            target = (f.parent / m.group(1)).resolve()
            checked += 1
            if not target.exists():
                rel = f.relative_to(ROOT).as_posix()
                FAILS.append(f"{rel} → {m.group(1)}")
    print(f"检查 {checked} 条相对链接")

    if FAILS:
        print(f"\n断链 {len(FAILS)} 条：")
        for x in FAILS[:20]:
            print("  ", x)
        print("\n有失败项")
        return 1
    print("全部通过（没有断链）")
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
