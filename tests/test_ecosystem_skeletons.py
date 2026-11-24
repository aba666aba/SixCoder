"""生态包骨架完整性测试（2026-11-24 补全后）。

## 背景

核对时发现 5 个生态包只有 adapter 没有 templates/：
go_service / microservice / node_express / py_lib / spring_boot。
于是「生成这类项目」时仍然走旧逻辑，骨架是空的。

现在 8 个包全部补齐了骨架，本文件钉住：

1. 每个包都有 templates/，且非空
2. 骨架能真的跑起来（按本机有的工具链分档验证）
3. 骨架的识别 marker 存在（否则复制出来引擎认不出是什么项目）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import ecosystem, stack   # noqa: E402

ALL_PACKS = ("cli_tool", "electron", "frontend_spa", "go_service",
             "microservice", "node_express", "py_lib", "spring_boot")


def _env():
    return dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")


def test_every_pack_has_templates():
    """8 个包都必须有非空 templates/。"""
    for name in ALL_PACKS:
        d = ecosystem.pack_dir(name) / "templates"
        assert d.is_dir(), f"{name} 没有 templates/"
        files = [p for p in d.rglob("*") if p.is_file()]
        assert files, f"{name}/templates/ 是空的"


def test_marker_file_present_in_templates():
    """复制出来的项目必须能被 detect 认出来。

    这是骨架最容易漏的一点：templates/ 里没有 manifest 声明的 marker 文件，
    复制完 detect() 返回默认栈，整条链就跑偏了。
    """
    import json
    for name in ALL_PACKS:
        mf = ecosystem.pack_dir(name) / "manifest.json"
        meta = json.loads(mf.read_text(encoding="utf-8"))
        markers = meta.get("detect_markers") or []
        if not markers:
            continue
        ws = Path(tempfile.mkdtemp(prefix="mk-"))
        ecosystem.apply_templates(name, ws)
        assert stack.detect(ws) == name, (
            f"{name} 复制后 detect 返回 {stack.detect(ws)}，期望 {name}；"
            f"markers={markers}")


def test_py_lib_skeleton_imports():
    """py_lib 骨架必须能 import（空库阶段也不能炸）。"""
    ws = Path(tempfile.mkdtemp(prefix="pylib-"))
    r = ecosystem.apply_templates("py_lib", ws)
    assert r.get("ok") is True
    p = subprocess.run([sys.executable, "-c", "import lib; print(lib.__version__)"],
                       cwd=str(ws), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_env(), timeout=60)
    assert p.returncode == 0, p.stderr


def test_py_lib_resolves_module_symbols():
    """加了模块之后，库要能取到它的符号（懒加载生效）。"""
    ws = Path(tempfile.mkdtemp(prefix="pylib2-"))
    ecosystem.apply_templates("py_lib", ws)
    (ws / "lib" / "text.py").write_text("def shout(s):\n    return s.upper()\n",
                                        encoding="utf-8")
    p = subprocess.run([sys.executable, "-c", "import lib; print(lib.shout(\"hi\"))"],
                       cwd=str(ws), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=_env(), timeout=60)
    assert p.returncode == 0, p.stderr
    assert "HI" in p.stdout


def test_node_express_syntax():
    ws = Path(tempfile.mkdtemp(prefix="expr-"))
    ecosystem.apply_templates("node_express", ws)
    if not shutil.which("node"):
        pytest_skip = True  # noqa: F841
        return
    p = subprocess.run(["node", "--check", "server.js"], cwd=str(ws),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    assert p.returncode == 0, p.stderr


def test_microservice_syntax():
    """microservice 骨架的每个 .py 都要能编译。"""
    ws = Path(tempfile.mkdtemp(prefix="ms-"))
    ecosystem.apply_templates("microservice", ws)
    for f in sorted(ws.rglob("*.py")):
        p = subprocess.run([sys.executable, "-m", "py_compile", str(f)],
                           cwd=str(ws), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=_env(), timeout=60)
        assert p.returncode == 0, f"{f.name}: {p.stderr}"


def test_microservice_celery_marker():
    """celery_app.py 是 microservice 的识别 marker，必须有。"""
    ws = Path(tempfile.mkdtemp(prefix="ms2-"))
    ecosystem.apply_templates("microservice", ws)
    assert (ws / "celery_app.py").is_file()


def test_spring_boot_pom_is_valid_xml():
    import xml.etree.ElementTree as ET
    ws = Path(tempfile.mkdtemp(prefix="sb-"))
    ecosystem.apply_templates("spring_boot", ws)
    ET.parse(ws / "pom.xml")
    assert (ws / "src/main/java/com/sixcoder/Application.java").is_file()


def test_go_service_has_main_package():
    """go 骨架必须有 package main + func main（本机没装 go，做文本检查）。"""
    ws = Path(tempfile.mkdtemp(prefix="go-"))
    ecosystem.apply_templates("go_service", ws)
    src = (ws / "main.go").read_text(encoding="utf-8")
    assert "package main" in src
    assert "func main()" in src
    assert "net/http" in src
