"""文档一致性自检（开发文档 §零 改动纪律）。

为什么要有：把"改完必须更新文档"写在文档里，只是一句劝告 —— 而劝告在赶工的时候最先被跳过。
这一层把它变成**机械事实**：

  1. 架构地图里列出的 `core/*.py` 必须真实存在（防止文档写了不存在的东西）；
  2. `core/` 下的每个 .py 必须在架构地图里出现（防止加了代码忘了写文档）；
  3. §8.7 实施状态表里标「已完成」的项，必须指向真实存在的 check 脚本；
  4. 文档必须包含 §零（改动纪律）与 §8.1–8.7（骨架/读取的权威口径）。

只做"存在性"和"指向有效性"的检查，不评判内容质量 —— 那部分只能靠人。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # 仓库根
DOC = ROOT / "开发文档.md"
CORE = Path(__file__).resolve().parents[1] / "core"
CHECKS = Path(__file__).resolve().parent

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    if not DOC.is_file():
        print("  FAIL  开发文档.md 不存在")
        return 1
    text = DOC.read_text(encoding="utf-8", errors="replace")

    print("[1] 权威章节齐全")
    for sec in ("## 零、改动纪律", "8.1", "8.2", "8.3", "8.4", "8.5", "8.6", "8.7"):
        check(f"包含 {sec}", sec in text)

    print("[2] 架构地图里的 core 文件必须真实存在")
    listed = set(re.findall(r"`core/([A-Za-z_][\w]*\.py)`", text))
    missing = sorted(n for n in listed if not (CORE / n).is_file())
    check(f"地图里 {len(listed)} 个文件都存在", not missing, "缺：" + ", ".join(missing[:6]))

    print("[3] core/ 下新增的文件必须登记（老账不追究，只减不增）")
    import json
    actual = {p.name for p in CORE.glob("*.py") if p.name != "__init__.py"}
    baseline = set()
    bp = CHECKS / "devdoc_baseline.json"
    if bp.is_file():
        try:
            baseline = set(json.loads(bp.read_text(encoding="utf-8")).get("undocumented") or [])
        except Exception:                            # noqa: BLE001
            baseline = set()
    fresh = sorted(actual - listed - baseline)
    check(f"core/ 新增的 {len(actual - baseline)} 个文件都登记了", not fresh,
          "没登记：" + ", ".join(fresh[:8]))
    stale = sorted(baseline - actual)
    check("基线里没有已删除的文件（基线要跟着删）", not stale, "过期：" + ", ".join(stale[:6]))

    print("[4] 标「已完成」的条目必须指向真实存在的 check")
    rows = [ln for ln in text.splitlines() if ln.startswith("|") and "已完成" in ln]
    bad: list[str] = []
    for ln in rows:
        for name in re.findall(r"check_[a-z0-9_]+\.py", ln):
            if not (CHECKS / name).is_file():
                bad.append(name)
    check(f"{len(rows)} 条「已完成」的引用都有效", not bad, "不存在：" + ", ".join(sorted(set(bad))))

    print("[5] 自检数量描述不许落后于实际")
    n = len(list(CHECKS.glob("check_*.py")))
    m = re.search(r"（(\d+)\s*个脚本）", text)
    stated = int(m.group(1)) if m else 0
    check(f"文档写的 {stated} 与实际 {n} 一致", stated == n,
          f"文档 {stated} vs 实际 {n}（加了 check 就要改这行）")

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（章节齐全 / 地图双向一致 / 完成项有 check / 数量不落后）")
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
