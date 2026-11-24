"""demo 仓库初始化自检：没装 Git 不许把后端拖死（2026-11-20 真实故障）。

用户截图：别人电脑启动报 `FileNotFoundError [WinError 2]`，位置
`core/demo.py::ensure_demo` 调 `subprocess.run(["git", ...])`。Git 不是运行时
硬依赖，没装 Git 的机器直接后端 exit=1 → 界面只显示"后端未就绪"。

钉住：
  1. 没有 git.exe 时 ensure_demo 不调用 subprocess，仍生成 demo 文件；
  2. git 存在但执行抛 OSError 时也不冒泡；
  3. server 启动段对 ensure_demo 做了兜底（demo 失败不阻断后端）。

跑法: python checks/check_demo_bootstrap.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import demo  # noqa: E402

FAILS: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {str(detail)[:220]}" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 没装 Git：不调用 subprocess，demo 仍生成")
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-repo"
        with patch.object(demo, "DEMO", target), \
             patch.object(shutil, "which", return_value=None), \
             patch.object(demo.subprocess, "run") as run:
            out = demo.ensure_demo()
        ck("返回 demo 路径", out == target, out)
        ck("没有调用 git", run.call_count == 0, run.call_count)
        ck("calc.py 已生成", (target / "calc.py").is_file())
        ck("test_calc.py 已生成", (target / "test_calc.py").is_file())
        ck("没有 .git（没装 Git 就不假装有历史）", not (target / ".git").exists())

    print("[2] git 存在但执行失败：不冒泡")
    with tempfile.TemporaryDirectory() as td:
        target = Path(td) / "demo-repo"
        with patch.object(demo, "DEMO", target), \
             patch.object(shutil, "which", return_value="C:/fake/git.exe"), \
             patch.object(demo.subprocess, "run", side_effect=OSError("blocked")):
            out = demo.ensure_demo()
        ck("OSError 被吞掉，仍返回路径", out == target, out)
        ck("demo 文件仍在", (target / "calc.py").is_file())

    print("[3] server 启动段兜底")
    src = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    tail = src[src.rindex('if __name__ == "__main__":'):]
    ck("ensure_demo 在 try 里", "try:\n        ensure_demo()" in tail)
    ck("失败只打印、不 raise", "demo 初始化跳过" in tail)

    print("[4] 没 Git 时 Git 操作返回人话，不抛 500")
    from core import tools
    import os
    with patch.dict(os.environ, {"PATH": ""}):
        rc, out = tools.git(Path.cwd(), "status")
    ck("tools.git 返回 127", rc == 127, rc)
    ck("原因指向安装 Git", "安装 Git" in out or "找不到 git" in out, out)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
