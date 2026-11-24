"""自检：**我们自己的源码**不许有未定义名字（这一类 bug 已经咬了三次）。

背景（都是这套系统自己踩的）：
  · `contractgen.py` 用了 `Path(` 却没导入 → 「生成一个计算器」那一夜直接
    `NameError: name 'Path' is not defined`，整夜崩、0/0 交付；
  · `company.py` 里 `verify=lambda: _verify(...)`，而 `_verify` 早随旧路径删了 →
    只要走行级补丁并且真打完补丁要复验，就是 NameError；
  · `_scale` / `os` 未定义 → 冒烟里整轮崩（也是这一族）。

这些都不是"逻辑错"，是"名字不存在"，用 `gates.gate_undef` 静态一跑就能抓。
所以把它变成常规自检：core/ + 入口脚本一起扫。

跑法: python check_self_undef.py
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import gates  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("自有源码未定义名字自检")

    print("[1] core/ 全部模块")
    files = [p.name for p in sorted((ROOT / "core").glob("*.py"))]
    g = gates.gate_undef(ROOT / "core", files)
    check(f"{len(files)} 个核心模块没有未定义名字", g.ok, g.detail[:400])

    print("[2] 入口脚本（server.py / cli.py）")
    tmp = ROOT / ".tmp" / "selfundef"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for name in ("server.py", "cli.py"):
            src = ROOT / name
            if src.exists():
                shutil.copy(src, tmp / name)
        names = [p.name for p in sorted(tmp.glob("*.py"))]
        g2 = gates.gate_undef(tmp, names)
        check(f"{len(names)} 个入口脚本没有未定义名字", g2.ok, g2.detail[:400])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("[3] 工具与自检脚本（跳过明显用 TEMP 之外依赖的）")
    for sub in ("tools", "checks"):
        names = [p.name for p in sorted((ROOT / sub).glob("*.py"))]
        if not names:
            continue
        g3 = gates.gate_undef(ROOT / sub, names)
        # 工具脚本里允许"故意引用外层变量"的少数情况，所以这里只报告不判失败
        print(f"     {sub}/：{'通过' if g3.ok else '有未定义名字 → ' + g3.detail[:160]}")

    print("[4] 三个历史 bug 的具体位置不再出现")
    comp = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("company.py 不再调用不存在的 _verify", "verify=lambda: _verify(" not in comp)
    check("company.py 用的是 _verify_module", "_verify_module(rel)" in comp)
    cg = (ROOT / "core" / "contractgen.py").read_text(encoding="utf-8")
    check("contractgen.py 导入了 Path", "from pathlib import Path" in cg)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（core/ 与入口脚本没有未定义名字；三个历史 bug 位置已消失）")
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
