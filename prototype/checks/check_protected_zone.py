"""受保护区（§13.4 第 4 步）自检：测试/契约/治理文件对远端工具只读。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import scale, remotetools  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("受保护区自检（§13.4 第 4 步）")
    with tempfile.TemporaryDirectory() as d:
        ws = Path(d)
        # 1) 受保护区模式命中
        hits = scale.protected_hits(ws, ["AGENTS.md", "src/auth_login.py",
                                         "docs/2026-strategy.md", "src/app.py"])
        check("命中 AGENTS.md", "AGENTS.md" in hits)
        check("命中 auth 文件", any("auth" in h for h in hits))
        check("命中治理文档 docs/2xxx", any(h.endswith("docs/2026-strategy.md") for h in hits))
        check("普通源码不在受保护区", "src/app.py" not in hits)

        # 2) guard_write：L 级 block，普通源码放行
        ok_l, _ = scale.guard_write(ws, "AGENTS.md", "L")
        check("L 级拒绝写 AGENTS.md", ok_l is False)
        ok_src, _ = scale.guard_write(ws, "src/app.py", "L")
        check("L 级允许写普通源码", ok_src is True)
        # S 级 advisory（author 路径可告警放行，但有理由）
        ok_s, why_s = scale.guard_write(ws, "AGENTS.md", "S")
        check("S 级受保护区为 advisory（带告警理由）", ok_s is True and "告警" in why_s)

        # 3) 远端工具循环：测试/契约/治理文件永远只读，不受级别影响
        loop = remotetools.ToolLoop(ws, level="S")
        t_ok, t_reason = loop._tool_allow_write("AGENTS.md")
        check("工具循环在 S 级也拒绝 AGENTS.md", t_ok is False and "受保护区" in t_reason)
        test_ok, test_reason = loop._tool_allow_write("tests/test_m.py")
        check("工具循环拒绝写测试文件", test_ok is False and "测试" in test_reason)
        auth_ok, _ = loop._tool_allow_write("src/auth_login.py")
        check("工具循环拒绝写 auth 文件", auth_ok is False)
        app_ok, _ = loop._tool_allow_write("src/app.py")
        check("工具循环允许写普通源码", app_ok is True)
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（受保护区命中 / L 级 block / 测试-契约-治理对远端工具永远只读）")
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
