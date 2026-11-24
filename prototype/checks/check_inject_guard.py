"""子进程注入守护（2026-11-23）。

背景：内置 Python 是嵌入式 `._pth` 隔离模式，`sys.path` 固定、不读 PYTHONPATH、
连脚本目录都不自动加入。任何"起子进程去 import 用户项目"的裸写，在安装版里
**必然失败**，还会被误判成"产物有问题"。这一轮为此修了 7 处 → 说明它是**模式**。

这道自检负责"防第 8 次"：扫描 core/ 里所有真正会被执行的 argv 列表，
发现下面两类裸写就判红：

  ① `["-c", "import xxx…"]`（不带 sys.path 注入）
  ② `["-m", "pytest", …]`（不带 `-o pythonpath=`）

判定基于 **AST 字面量**（不是文本 grep）：`[...]` 里把常量项拼起来看特征，
所以写成变量的 argv（如 `pyenv.script_argv(...)`）不会被误伤。

确实有意保留裸写时，在该行写 `# inject-ok: <理由>` 并说明为什么不需要注入。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []

# 会**执行** argv 的调用点（第一个位置参数是命令列表）
EXEC_FNS = {"run", "Popen", "call", "check_output", "_run_child"}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" :: {detail[:220]}" if detail and not ok else ""))


def _fn_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _const_text(node: ast.AST) -> str:
    """取字符串常量（含 f-string 的常量片段）用于特征判断。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str))
    return ""


def scan_file(path: Path) -> list[tuple[int, str]]:
    """返回 [(行号, 原因)]。解析失败不静默：抛出去由调用方记为失败。"""
    src = path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src, filename=str(path))
    lines = src.splitlines()
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _fn_name(node.func) not in EXEC_FNS:
            continue
        if not node.args:
            continue
        arg0 = node.args[0]
        if not isinstance(arg0, (ast.List, ast.Tuple)):
            continue
        texts = [_const_text(e) for e in arg0.elts]
        joined = "|".join(texts)
        if "-c" in texts:
            i = texts.index("-c")
            nxt = (texts[i + 1] if i + 1 < len(texts) else "").lstrip()
            if nxt.startswith("import ") and "sys.path" not in joined:
                hits.append((node.lineno, "裸 `-c import`（缺 sys.path 注入）"))
        if "-m" in texts and "pytest" in texts and "pythonpath" not in joined:
            hits.append((node.lineno, "裸 `-m pytest`（缺 -o pythonpath=）"))
    # inject-ok 白名单（同行的注释）
    out = []
    for ln, why in hits:
        ctx = lines[ln - 1] if 0 < ln <= len(lines) else ""
        if "inject-ok" in ctx:
            continue
        out.append((ln, why))
    return out


def main() -> int:
    print("子进程注入守护（._pth 隔离下的 sys.path 注入）")
    print("[1] 扫描 core/*.py 里真正会执行的 argv")
    bad: list[str] = []
    scanned = 0
    for f in sorted(CORE.glob("*.py")):
        if f.name == "__init__.py":
            continue
        scanned += 1
        try:
            for ln, why in scan_file(f):
                bad.append(f"{f.name}:{ln} {why}")
        except SyntaxError as exc:
            bad.append(f"{f.name} 语法错误：{exc}")
    check(f"扫描 {scanned} 个模块", not bad, "；".join(bad[:6]))

    print("[2] pyenv 助手可用（三种形态）")
    from core import pyenv
    ia = pyenv.import_argv("/tmp/x", "m")
    sa = pyenv.script_argv("/tmp/x/a.py", ["--k", "1"])
    pa = pyenv.pytest_argv("/tmp/x", ["tests"])
    check("import_argv 带 sys.path", any("sys.path" in str(x) for x in ia), str(ia))
    check("script_argv 用 runpy", any("runpy.run_path" in str(x) for x in sa), str(sa)[:160])
    check("pytest_argv 带 pythonpath", any("pythonpath=" in str(x) for x in pa), str(pa))
    check("pytest_argv 路径用正斜杠",
          all("\\" not in x.split("pythonpath=")[-1] for x in pa if "pythonpath=" in x), str(pa))

    print("[3] 真跑：三种形态都能在临时项目里工作")
    import subprocess
    import tempfile
    ws = Path(tempfile.mkdtemp(prefix="inj-"))
    (ws / "m.py").write_text("V = 42\n", encoding="utf-8")
    (ws / "cli_core.py").write_text("def run():\n    return 7\n", encoding="utf-8")
    (ws / "main.py").write_text("from cli_core import run\nprint(run())\n", encoding="utf-8")
    (ws / "test_m.py").write_text("from m import V\n\ndef test_v():\n    assert V == 42\n",
                                  encoding="utf-8")

    def _run(argv):
        return subprocess.run(argv, cwd=str(ws), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")

    r1 = _run(pyenv.import_argv(ws, "m"))
    r2 = _run(pyenv.script_argv(ws / "main.py"))
    r3 = _run(pyenv.pytest_argv(ws, ["test_m.py"]))
    check("import_argv 真跑通过", r1.returncode == 0, r1.stderr[-160:])
    check("script_argv 真跑通过且输出正确", r2.returncode == 0 and "7" in (r2.stdout or ""),
          (r2.stdout or r2.stderr)[-160:])
    check("pytest_argv 真跑收集到用例", r3.returncode == 0, (r3.stdout or r3.stderr)[-160:])

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
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
