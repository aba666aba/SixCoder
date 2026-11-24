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


# ---------------------------------------------------------------- 阶段③：常用文档
#
# 为什么单开一组（2026-11-23，对齐"三阶段流水线"的第三段）：
#   上面那两个只产出 **README + LICENSE** —— 那是对外交付用的门面。
#   但开发者接手一个仓库时真正读的是另外三份：模块在哪、架构长什么样、**交接给我要小心什么**。
#   这三份全部来自**确定性扫描**（AST / 依赖图 / 审计结果），不调用任何模型（零 token）。

def _deps_of(tree: ast.AST) -> list[str]:
    """一个模块 import 了哪些**仓内**模块（用于画依赖方向）。"""
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [a.name.split(".")[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:                        # 相对导入：记成"同级"
                out.append(".")
            elif node.module:
                out.append(node.module.split(".")[0])
    return out


def _local_deps(ws: Path, mods: list[dict]) -> dict[str, list[str]]:
    """仓内模块之间的依赖边：{模块名: [它 import 的仓内模块]}。"""
    names = {m["module"] for m in mods}
    edges: dict[str, list[str]] = {}
    for m in mods:
        p = Path(ws) / m["file"]
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except (SyntaxError, OSError):
            continue
        got = sorted({d for d in _deps_of(tree) if d in names and d != m["module"]})
        if got:
            edges[m["module"]] = got
    return edges


def write_modules_doc(ws: Path, project: str = "") -> Path:
    """MODULES.md：每个模块做什么、对外暴露什么、被谁依赖。"""
    ws = Path(ws)
    mods = _modules(ws)
    edges = _local_deps(ws, mods)
    rev: dict[str, list[str]] = {}
    for src, deps in edges.items():
        for d in deps:
            rev.setdefault(d, []).append(src)
    lines = [f"# 模块说明：{project or ws.name}", "",
             f"- 共 **{len(mods)}** 个模块、**{sum(len(m['symbols']) for m in mods)}** 个公开符号",
             "- 由 AST 扫描生成（不含人工描述，改代码后重跑即可）", ""]
    for m in mods:
        lines += [f"## `{m['file']}`（{m['lines']} 行）", ""]
        uses = edges.get(m["module"]) or []
        used = sorted(rev.get(m["module"]) or [])
        if uses:
            lines.append(f"- 依赖：{', '.join('`%s`' % u for u in uses)}")
        if used:
            lines.append(f"- 被依赖：{', '.join('`%s`' % u for u in used)}")
        if not uses and not used:
            lines.append("- 与其他仓内模块无依赖（孤立模块，确认是否还需要）")
        lines.append("")
        if m["symbols"]:
            lines += ["**公开接口**", ""] + m["symbols"] + [""]
        else:
            lines += ["_没有公开符号_", ""]
    p = ws / "MODULES.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def write_architecture_doc(ws: Path, project: str = "") -> Path:
    """ARCHITECTURE.md：依赖方向 + 环 + 架构违规（复用 archrules，不重写判据）。"""
    ws = Path(ws)
    mods = _modules(ws)
    edges = _local_deps(ws, mods)
    lines = [f"# 架构概览：{project or ws.name}", "",
             "- 依赖图与违规来自确定性扫描；**只数违规，不打耦合分**"
             "（分数可以靠拆碎模块刷出来）", ""]
    lines += ["## 模块依赖", ""]
    if edges:
        for src in sorted(edges):
            lines.append(f"- `{src}` → {', '.join('`%s`' % d for d in edges[src])}")
    else:
        lines.append("_所有模块互不依赖（平铺结构）_")
    lines.append("")
    # 环检测（F1）：自己算一遍当保险，同时把 archrules 的违规并进来
    lines += ["## 依赖环", ""]
    cycles = _find_cycles(edges)
    if cycles:
        for c in cycles[:10]:
            lines.append(f"- ❌ {' → '.join('`%s`' % x for x in c)}")
        lines.append("")
        lines.append("> 有环 = 这两个模块永远无法独立理解与测试，必须拆开。")
    else:
        lines.append("没有发现依赖环。")
    lines.append("")
    lines += ["## 架构违规", ""]
    try:
        from . import audit as _audit
        res = _audit.audit(ws)
        arch_findings = [f for f in res["findings"] if f.get("source") == "archrules"]
        if arch_findings:
            for f in arch_findings:
                lines.append(f"- [{f['severity']}] `{f.get('path') or ''}` {f.get('message')}")
        else:
            lines.append("架构规则未报违规。")
    except Exception as exc:                              # noqa: BLE001
        lines.append(f"- 架构违规：这项没跑成（{type(exc).__name__}），**不等于通过**")
    p = ws / "ARCHITECTURE.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """找依赖环（DFS 回边）。返回每组环的模块序列。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {k: WHITE for k in edges}
    for deps in edges.values():
        for d in deps:
            color.setdefault(d, WHITE)
    out: list[list[str]] = []
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in edges.get(u, []):
            if color.get(v, WHITE) == GRAY:
                i = stack.index(v)
                out.append(stack[i:] + [v])
            elif color.get(v, WHITE) == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    for node in list(color):
        if color[node] == WHITE:
            dfs(node)
    return out


def write_handover_doc(ws: Path, project: str = "") -> Path:
    """HANDOVER.md：**交接单**——接手前必须知道的四件事。

    这是第①段（夜班原型）和第②段（开发者微调）之间最容易出事的地方：
    交付报告说"做完了"，但没说自己没测什么。交接单专门回答"哪些地方不可信"。
    """
    ws = Path(ws)
    mods = _modules(ws)
    tests = _tests(ws)
    lines = [f"# 交接单：{project or ws.name}", "",
             "> 这份文档只回答一个问题：**接手之前，你必须先知道什么。**", ""]

    # 1) 测试覆盖的真实成色
    lines += ["## 1. 测试到底测了什么", ""]
    try:
        from . import testweight as _tw
        scan = _tw.scan(ws)
        lines.append(f"- 结论：**{scan['verdict']}**")
        if scan.get("problems"):
            for prob in scan["problems"][:8]:
                lines.append(f"  - {prob}")
    except Exception as exc:                              # noqa: BLE001
        lines.append(f"- 这项没跑成（{type(exc).__name__}），**不等于测试没问题**")
    if not tests:
        lines += ["", "_仓库里没有 test_*.py：任何\"已完成\"都没有测试证据支撑。_"]
    lines.append("")

    # 2) 审计：已知问题
    lines += ["## 2. 已知问题（接手后优先看）", ""]
    try:
        from . import audit as _audit
        res = _audit.audit(ws)
        lines.append(f"- {res['summary']}")
        top = [f for f in res["findings"] if f["severity"] in ("P0", "P1")][:10]
        if top:
            lines.append("")
            for f in top:
                loc = f.get("path") or ""
                if f.get("line"):
                    loc += f":{f['line']}"
                lines.append(f"- **[{f['severity']}]** `{loc}` {f.get('message')}")
                if f.get("how_to_fix"):
                    lines.append(f"  - 怎么改：{f['how_to_fix']}")
        if res.get("errors"):
            lines += ["", "没跑成的审计器（**不等于通过**）："]
            lines += [f"- `{e['auditor']}`：{e['error']}" for e in res["errors"]]
    except Exception as exc:                              # noqa: BLE001
        lines.append(f"- 审计没跑成（{type(exc).__name__}），**不等于没问题**")
    lines.append("")

    # 3) 未测的边界（从需求里抽不出来的那些，如实说明）
    lines += ["## 3. 没有证据的边界", "",
              "机械判据只覆盖**需求里写出来的部分**。以下这些通常没有被测，接手后要自己补：",
              "",
              "- 输入校验（空值 / None / 越界 / 类型错）",
              "- 并发与重复调用（幂等）",
              "- 异常路径的**错误信息**是否可读",
              "- 性能与大输入",
              ""]

    # 4) 从哪开始读
    lines += ["## 4. 建议的阅读顺序", ""]
    order = sorted(mods, key=lambda m: -len(m["symbols"]))
    for i, m in enumerate(order[:6], 1):
        n = len(m["symbols"])
        lines.append(f"{i}. `{m['file']}`（{n} 个公开符号，{m['lines']} 行）")
    lines += ["", "---", "",
              "> 后续开发从这里开始：先修 `## 2` 里的 P0/P1，再补 `## 3` 的边界测试。", ""]
    p = ws / "HANDOVER.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def write_stage3_docs(ws: Path, project: str = "") -> dict:
    """生成阶段③的三份开发者文档（MODULES / ARCHITECTURE / HANDOVER）。"""
    ws = Path(ws)
    wrote: dict[str, str] = {}
    for name, fn in (("MODULES.md", write_modules_doc),
                     ("ARCHITECTURE.md", write_architecture_doc),
                     ("HANDOVER.md", write_handover_doc)):
        try:
            wrote[name] = str(fn(ws, project).name)
        except Exception as exc:                          # noqa: BLE001
            wrote[name] = f"失败：{type(exc).__name__}: {exc}"
    return {"ok": all(not v.startswith("失败") for v in wrote.values()), "wrote": wrote}
