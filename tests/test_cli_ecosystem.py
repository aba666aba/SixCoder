"""CLI 骨架与生态包合并测试（合并方案 C，2026-11-24）。

## 背景

P1 我写了一套自己的 CLI 骨架（cli_template.py + write_cli_entry），入口叫 main.py。
后来核对发现生态包 cli_tool 早已存在，但两边对不上：
  · 生态包 detect_markers = ["cli.py"]，我生成 main.py -> 永远不被识别成 CLI 项目
  · 生态包有 cli 探针（真跑 + 断言 stdout/退出码），我没有
  · 我有骨架模板，生态包没有（template_dirs = ()）

## 合并后的形态

  · 骨架：cli_tool/templates/cli.py（我写的模板，入口名改成 cli.py）
  · 复制：ecosystem.apply_templates("cli_tool", ws)（既有引擎能力）
  · 识别：stack.detect -> cli_tool（既有 marker）
  · 验收：生态包的 cli 探针（既有）
  · 生成计划：layerplan.CLI_PLAN 只产出子命令模块，不碰入口

我的 cli_template.py / write_cli_entry 已删除。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import ecosystem, layerplan, stack   # noqa: E402


def test_pack_has_cli_skeleton_template():
    """骨架必须真的在生态包里（合并方案的核心）。"""
    tpl = ROOT / "prototype" / "stacks" / "cli_tool" / "templates"
    assert (tpl / "cli.py").is_file(), "缺 cli_tool/templates/cli.py"
    src = (tpl / "cli.py").read_text(encoding="utf-8")
    # 模板必须**原样就能编译**（不能依赖引擎替换占位符 ——
    # apply_templates 只做原样复制，实测带 {commands!r} 会复制成语法错误）
    compile(src, "<tpl>", "exec")
    assert "argparse" in src
    assert "_discover_commands" in src, "应当运行时自己扫 commands/"


def test_apply_templates_creates_runnable_cli():
    """复制出来的骨架必须能跑（--help 退出码 0）。"""
    ws = Path(tempfile.mkdtemp(prefix="cli-eco-"))
    r = ecosystem.apply_templates("cli_tool", ws)
    assert r.get("ok") is True, r
    assert (ws / "cli.py").is_file()

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run([sys.executable, "cli.py", "--help"], cwd=str(ws),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30, env=env)
    assert p.returncode == 0, p.stderr
    assert "usage" in (p.stdout or "").lower()


def test_no_args_exits_2():
    ws = Path(tempfile.mkdtemp(prefix="cli-eco2-"))
    ecosystem.apply_templates("cli_tool", ws)
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    p = subprocess.run([sys.executable, "cli.py"], cwd=str(ws),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30, env=env)
    assert p.returncode == 2


def test_detect_recognizes_template_result():
    """关键：复制骨架后，栈识别必须认出 cli_tool（marker 是 cli.py）。"""
    ws = Path(tempfile.mkdtemp(prefix="cli-eco3-"))
    ecosystem.apply_templates("cli_tool", ws)
    assert stack.detect(ws) == "cli_tool", f"识别成 {stack.detect(ws)}"


def test_layerplan_cli_excludes_entry():
    """生成计划不能把入口 cli.py 交给远端写（入口由骨架负责）。"""
    class _C:
        def __init__(self, m):
            self.modules = m
            self.status = "done"
            self.probe = {}

    class _B:
        def __init__(self, c):
            self.caps = c

    plan = layerplan.default_registry().get("cli_tool")
    assert plan is not None
    ctx = layerplan.LayerContext(ws=None, blueprint=_B([_C(["cli.py", "commands/add.py"])]))
    got = plan.targets(ctx, "modules")
    assert got == ["commands/add.py"], f"入口不该出现在落点里，实际 {got}"


def test_old_duplicate_files_removed():
    """合并后不该还留着重复实现。"""
    assert not (ROOT / "prototype" / "core" / "cli_template.py").exists(), \
        "cli_template.py 应已删除（改用生态包模板）"
    from core import auto
    assert not hasattr(auto, "write_cli_entry"), "write_cli_entry 应已删除"