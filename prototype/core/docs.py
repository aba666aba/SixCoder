"""交付件自动生成：根目录 README + MIT LICENSE。

只在任务完成（全部节点 done/accepted）时写，且**内容全部可追溯**：
模块清单与公开接口来自 AST 实时扫描，不是模型编的。
"""
from __future__ import annotations

import ast
from pathlib import Path

SKIP = {".git", "__pycache__", ".pytest_cache", ".sixcoder", "workspaces", "node_modules",
        "dist", "build", "target"}

MIT_TEMPLATE = """MIT License

Copyright (c) {year} {holder}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""


def _modules(ws: Path) -> list[dict]:
    """扫描仓库的 Python 模块与公开符号（README 的目录与 API 段来自这里）。"""
    out: list[dict] = []
    for p in sorted(Path(ws).glob("*.py")):
        if p.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        symbols = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
                args = [a.arg + (f": {ast.unparse(a.annotation)}" if a.annotation else "")
                        for a in node.args.args if a.arg not in ("self", "cls")]
                ret = f" -> {ast.unparse(node.returns)}" if node.returns else ""
                doc = (ast.get_docstring(node) or "").split("\n")[0]
                symbols.append(f"- `{node.name}({', '.join(args)}){ret}`"
                               + (f" — {doc}" if doc else ""))
            elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
                doc = (ast.get_docstring(node) or "").split("\n")[0]
                methods = [m.name for m in node.body if isinstance(m, ast.FunctionDef)
                           and not m.name.startswith("_")]
                symbols.append(f"- `class {node.name}`" + (f" — {doc}" if doc else "")
                               + (f"（方法：{', '.join(methods[:6])}）" if methods else ""))
        out.append({"file": p.name, "module": p.stem, "symbols": symbols,
                    "lines": len(p.read_text(encoding='utf-8-sig', errors='replace').splitlines())})
    return out


def _tests(ws: Path) -> list[str]:
    return sorted(p.name for p in Path(ws).glob("test_*.py"))


def write_license(ws: Path, holder: str = "SixCoder 生成") -> Path:
    lic = Path(ws) / "LICENSE"
    if not lic.exists():
        import datetime
        lic.write_text(MIT_TEMPLATE.format(year=datetime.date.today().year, holder=holder),
                       encoding="utf-8")
    return lic


def write_readme(ws: Path, requirement: str, project: str = "") -> Path:
    ws = Path(ws)
    mods = _modules(ws)
    tests = _tests(ws)
    name = project or ws.name
    lines = [f"# {name}", ""]
    if requirement.strip():
        first = requirement.strip().splitlines()[0][:120]
        lines += [f"> {first}", ""]
    lines += ["## 快速开始", "", "```bash",
              "pip install -r requirements.txt   # 如有依赖", "python -m pytest -q             # 运行测试",
              "```", ""]
    if mods:
        lines += ["## 模块", "", "| 文件 | 行数 | 说明 |", "|---|---|---|"]
        for m in mods:
            lines.append(f"| `{m['file']}` | {m['lines']} | {m['symbols'][0][2:] if m['symbols'] else ''} |")
        lines.append("")
        lines += ["## 公开接口", ""]
        for m in mods:
            if not m["symbols"]:
                continue
            lines.append(f"### `{m['module']}`")
            lines += m["symbols"]
            lines.append("")
    if tests:
        lines += ["## 测试", "", "| 测试文件 |", "|---|"]
        lines += [f"| `{t}` |" for t in tests]
        lines.append("")
    lines += ["## 许可证", "", "本项目以 **MIT** 许可发布，见 [LICENSE](LICENSE)。", ""]
    readme = ws / "README.md"
    readme.write_text("\n".join(lines), encoding="utf-8")
    return readme


def generate(ws: Path, requirement: str, project: str = "") -> dict:
    """生成 README + LICENSE，返回元信息（供事件与报告记录）。"""
    lic = write_license(ws)
    rd = write_readme(ws, requirement, project)
    mods = _modules(ws)
    return {"readme": str(rd.name), "license": str(lic.name),
            "modules": len(mods), "symbols": sum(len(m["symbols"]) for m in mods),
            "tests": len(_tests(ws))}
