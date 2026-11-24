"""office_cli 生态包测试（2026-11-24）。

## 这个包解决什么

用户问"能不能把 DSH 的 Office 插件移植到 SixCoder"。

## 为什么不是"移植插件"

  · DSH 的 Office 能力依赖 @deepseek-ai/libreoffice-kit-win32-x64
    = 189.7 MB 的完整 LibreOffice 二进制
  · 而 SixCoder 的定位是"生成并验证代码"，不是"自己读写文档"

## 所以做成生态包：让 SixCoder 能生成并验证"处理 Office 的程序"

判据关键：**必须读回产物**。
  · 文件存在但只有一个空 sheet            -> 假绿
  · 单元格是该有的值、不是 None           -> 真绿
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


def _env():
    return dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def test_pack_registered():
    packs = [r["name"] for r in (ecosystem.list_packs() or [])]
    assert "office_cli" in packs, packs


def test_adapter_loads():
    ad, why = ecosystem.load_adapter("office_cli")
    assert ad is not None, why
    assert ad.entry_path == "office_main.py"
    assert ad.asgi_app() is False


def test_detect_after_apply():
    ws = Path(tempfile.mkdtemp(prefix="offdet-"))
    ecosystem.apply_templates("office_cli", ws)
    assert stack.detect(ws) == "office_cli"


def test_skeleton_help_runs():
    ws = Path(tempfile.mkdtemp(prefix="offhelp-"))
    ecosystem.apply_templates("office_cli", ws)
    p = subprocess.run([sys.executable, "office_main.py", "--help"], cwd=str(ws),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=_env(), timeout=60)
    assert p.returncode == 0, p.stderr
    assert "usage" in (p.stdout or "").lower()


def test_real_xlsx_command_end_to_end():
    """真跑：造 xlsx -> 命令处理 -> **读回产物断言内容**。"""
    ws = Path(tempfile.mkdtemp(prefix="offe2e-"))
    ecosystem.apply_templates("office_cli", ws)

    (ws / "commands" / "xlsx2csv.py").write_text(
        "import csv\nimport sys\nfrom pathlib import Path\nimport openpyxl\n\n"
        "def run(args):\n"
        "    if not args.items:\n        return 2\n"
        "    src = Path(args.items[0])\n"
        "    wb = openpyxl.load_workbook(src)\n"
        "    sh = wb.active\n"
        "    out = src.with_suffix('.csv')\n"
        "    with out.open('w', newline='', encoding='utf-8') as f:\n"
        "        w = csv.writer(f)\n"
        "        for row in sh.iter_rows(values_only=True):\n"
        "            w.writerow(['' if c is None else c for c in row])\n"
        "    print('完成:', out.name)\n"
        "    return 0\n", encoding="utf-8")

    mk = ("import openpyxl\n"
          "wb = openpyxl.Workbook()\n"
          "sh = wb.active\n"
          "sh.append(['name', 'age'])\n"
          "sh.append(['甲', 20])\n"
          "wb.save('in.xlsx')\n")
    p0 = subprocess.run([sys.executable, "-c", mk], cwd=str(ws),
                        capture_output=True, text=True, env=_env(), timeout=60)
    assert p0.returncode == 0, p0.stderr

    p = subprocess.run([sys.executable, "office_main.py", "xlsx2csv", "in.xlsx"],
                       cwd=str(ws), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_env(), timeout=120)
    assert p.returncode == 0, p.stderr

    # **读回产物**：只看"文件存在"是没验
    csvf = ws / "in.csv"
    assert csvf.is_file(), "没有生成 csv"
    txt = csvf.read_text(encoding="utf-8")
    assert "name,age" in txt, txt
    assert "甲,20" in txt, txt


def test_empty_skeleton_has_no_commands():
    """空骨架必须是可用的（--help 能跑），不能因为没命令就崩。"""
    ws = Path(tempfile.mkdtemp(prefix="offempty-"))
    ecosystem.apply_templates("office_cli", ws)
    p = subprocess.run([sys.executable, "office_main.py", "nope"], cwd=str(ws),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", env=_env(), timeout=60)
    assert p.returncode == 2, f"未知命令应退出 2，实际 {p.returncode}"


def test_layerplan_generated_for_pack():
    """有 templates 的包会自动得到生成计划。"""
    plan = layerplan.default_registry().get("office_cli")
    assert plan is not None, "office_cli 没有生成计划"
    ctx = layerplan.LayerContext(ws=None, blueprint=None)
    assert plan.targets(ctx, "modules") == [] or isinstance(plan.targets(ctx, "modules"), list)
