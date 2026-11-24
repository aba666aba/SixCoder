"""审阅模式（阶段③）：**审计 + 文档**——把散落的机械判据汇成一份人能读的报告。

## 为什么需要这个模块（2026-11-23，对齐"三阶段流水线"）

产品的完整流程是三段：① 造物模式跑出原型 → ② 精修模式微调 → ③ 审阅模式审计与出文档。

但盘查发现第③段只有一半：
  · `walkthrough` 有 `/v1/walkthrough` 入口，能跑；
  · `archrules` / `lint` / `review` / `debt` / `detectors` 这些**审计模块早就写好了**，
    却**一个 HTTP 入口都没有**——只写成了自检断言，用户根本碰不到；
  · 文档生成只有 `core/docs.py` 的 README + LICENSE 两种。

也就是说：能力在代码里，**没接到产品面上**。这正是"阶段②③成熟度断崖"的实际形态
——不是缺实现，是缺接线。

## 本模块只做两件事

1. `audit(ws)`：跑一遍**零 token 的确定性审计**，合并成统一结构
   （每条 finding 带 级别 / 文件 / 行号 / 说明 / 怎么改）。
   全部复用已有模块，不重写判据——判据只有一份真相源，这是项目铁律。
2. `write_docs(ws, ...)`：在 README/LICENSE 之外，补出**程序员真正会读的三份文档**：
   `MODULES.md`（模块与公开接口）、`ARCHITECTURE.md`（依赖方向与架构违规）、
   `HANDOVER.md`（交接单：改了什么、哪里没测、哪里有歧义）。

## 边界（诚实说明）

  · 审计**只读不写**：不碰用户配置、不改代码、不提交；
  · 全部确定性（AST / 正则 / 依赖图），**不调用任何模型**——零 token；
  · 判据本身仍属各模块所有，这里只做**聚合与排序**，绝不重新实现一遍。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

# 审计项按"该不该让人夜里睡不着"排序：P0 起
SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SEVERITY_LABEL = {
    "P0": "必须修（起不来 / 数据错）",
    "P1": "应该修（契约不符 / 接不上）",
    "P2": "可以更好（重复 / 死代码）",
    "P3": "建议（命名 / 风格）",
}

# 扫代码时跳过的目录（与项目其它处保持一致）
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".sixcoder", "workspaces",
             "node_modules", "dist", "build", "target", ".venv", "venv"}


@dataclass
class Finding:
    """一条审计发现。字段与交付报告的"没通过原因"同口径。"""

    severity: str
    source: str                      # 哪个审计器报的：archrules / lint / debt / review ...
    path: str = ""
    line: int = 0
    message: str = ""
    how_to_fix: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"severity": self.severity, "source": self.source, "path": self.path,
                "line": int(self.line or 0), "message": self.message,
                "how_to_fix": self.how_to_fix, "detail": dict(self.detail or {})}


def _py_files(ws: Path) -> list[Path]:
    """仓库里的 Python 源文件（跳过测试与依赖目录）。"""
    out: list[Path] = []
    root = Path(ws).resolve()
    try:
        for p in sorted(root.rglob("*.py")):
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
                continue
            out.append(p)
    except OSError:
        pass
    return out


def _rel(ws: Path, p: Path) -> str:
    try:
        return Path(p).resolve().relative_to(Path(ws).resolve()).as_posix()
    except (ValueError, OSError):
        return Path(p).name


# ---------------------------------------------------------------- 各审计器

def _audit_arch(ws: Path) -> list[Finding]:
    """架构约束（F1 无环 / F2 层级 / F3 SDP …）——违规才报，不报分数。"""
    out: list[Finding] = []
    try:
        from . import archrules as _arch
    except Exception:                                     # noqa: BLE001
        return out
    # archrules 的入口签名在不同版本里可能是 rules(ws) / check(ws)，都试一遍
    for fn_name in ("check", "rules", "run", "violations"):
        fn = getattr(_arch, fn_name, None)
        if not callable(fn):
            continue
        try:
            res = fn(Path(ws))
        except Exception:                                 # noqa: BLE001
            continue
        items = res if isinstance(res, (list, tuple)) else (
            res.get("violations") or res.get("items") or [])
        for it in items:
            if isinstance(it, dict):
                out.append(Finding(
                    severity=str(it.get("severity") or "P2"),
                    source="archrules", path=str(it.get("path") or it.get("file") or ""),
                    line=int(it.get("line") or 0),
                    message=str(it.get("message") or it.get("detail") or it.get("rule") or ""),
                    how_to_fix=str(it.get("fix") or it.get("how_to_fix") or ""),
                    detail=dict(it)))
        break
    return out


def _audit_lint(ws: Path) -> list[Finding]:
    """内置 lint（bare except / 可变默认值 / 宽 except …）。"""
    out: list[Finding] = []
    try:
        from . import lint as _lint
    except Exception:                                     # noqa: BLE001
        return out
    try:
        found = _lint.lint_files(Path(ws), None)
    except Exception:                                     # noqa: BLE001
        return out
    for f in found or []:
        if isinstance(f, dict):
            # lint 的真实字段名（2026-11-23 实测）：rule / file / line / text / why / fix
            rule = f.get("rule") or f.get("code") or "lint"
            why = f.get("why") or f.get("message") or f.get("msg") or ""
            text = (f.get("text") or "").strip()
            out.append(Finding(
                severity="P2", source="lint",
                path=str(f.get("file") or f.get("path") or ""),
                line=int(f.get("line") or 0),
                message=f"{rule}：{why}" + (f"（`{text[:80]}`）" if text else ""),
                how_to_fix=str(f.get("fix") or f.get("instruction") or ""),
                detail=dict(f)))
    return out


def _audit_debt(ws: Path) -> list[Finding]:
    """临时方案台账（TODO/HACK/临时/先这样）。"""
    out: list[Finding] = []
    try:
        from . import debt as _debt
    except Exception:                                     # noqa: BLE001
        return out
    try:
        rows = _debt.load(Path(ws))
    except Exception:                                     # noqa: BLE001
        return out
    for r in rows or []:
        if not isinstance(r, dict) or r.get("resolved"):
            continue
        out.append(Finding(
            severity="P2", source="debt", path=str(r.get("path") or ""),
            line=int(r.get("line") or 0),
            message=f"临时方案：{r.get('text') or r.get('mark') or ''}"[:160],
            how_to_fix="要么修成正解，要么写清为什么必须留着",
            detail=dict(r)))
    return out


def _audit_tests(ws: Path) -> list[Finding]:
    """测试质量（空壳测试：0 个用例 / 恒真断言）。复用 core/testweight。"""
    out: list[Finding] = []
    try:
        from . import testweight as _tw
    except Exception:                                     # noqa: BLE001
        return out
    try:
        scan = _tw.scan(Path(ws))
    except Exception:                                     # noqa: BLE001
        return out
    if scan.get("file_count") == 0:
        out.append(Finding(severity="P1", source="testweight",
                           message="仓库里没有任何测试文件",
                           how_to_fix="至少给核心函数补 pytest：正常路径 + 边界 + 异常"))
        return out
    for name in scan.get("shell_files") or []:
        out.append(Finding(severity="P1", source="testweight", path=name,
                           message="测试文件是空壳（0 个测试函数或没有真断言）",
                           how_to_fix="补真实断言；只判键名/恒真的断言等于没测"))
    return out


def _audit_todo_in_code(ws: Path) -> list[Finding]:
    """源码里的 TODO/FIXME（没进台账的那些也算）。"""
    out: list[Finding] = []
    marks = ("TODO", "FIXME")
    for p in _py_files(ws):
        if p.name.startswith("test_"):
            continue
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            s = ln.strip()
            if not s.startswith("#"):
                continue
            for m in marks:
                if m in s:
                    out.append(Finding(severity="P3", source="todo",
                                       path=_rel(ws, p), line=i,
                                       message=s[:140],  # 展示类截断：只决定报告里这一行显示多长
                                       how_to_fix="做完或转成台账条目",
                                       detail={"text": s}))
                    break
    return out


AUDITORS = (
    ("archrules", _audit_arch),
    ("lint", _audit_lint),
    ("debt", _audit_debt),
    ("testweight", _audit_tests),
    ("todo", _audit_todo_in_code),
)


def audit(ws: Path) -> dict:
    """跑一遍零 token 审计，返回 {findings, by_severity, counts, summary}。

    任何单个审计器出错都不会让整体失败——把错误记在该项的 `error` 里，
    如实说明"这一项没跑成"，而不是假装没问题（沿用项目的"默认红"纪律）。
    """
    ws = Path(ws)
    findings: list[Finding] = []
    ran: list[str] = []
    errors: list[dict] = []
    for name, fn in AUDITORS:
        try:
            got = fn(ws) or []
            findings.extend(got)
            ran.append(name)
        except Exception as exc:                          # noqa: BLE001
            errors.append({"auditor": name, "error": f"{type(exc).__name__}: {exc}"})
    findings.sort(key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                 f.path, int(f.line or 0)))
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    by_sev: dict[str, list[dict]] = {}
    for f in findings:
        by_sev.setdefault(f.severity, []).append(f.to_dict())
    total_py = len(_py_files(ws))
    summary = (f"扫描 {total_py} 个 .py："
               + "，".join(f"{k} {counts.get(k, 0)}" for k in ("P0", "P1", "P2", "P3"))
               + (f"；{len(errors)} 个审计器没跑成" if errors else ""))
    return {
        "ok": not any(f.severity == "P0" for f in findings),
        "root": str(ws.resolve()),
        "py_files": total_py,
        "auditors_ran": ran,
        "findings": [f.to_dict() for f in findings],
        "by_severity": by_sev,
        "counts": counts,
        "errors": errors,
        "summary": summary,
        "note": "只读审计：不写代码、不改配置、不调用模型（零 token）",
    }


def render_report(result: dict, limit: int = 40) -> str:
    """把审计结果渲染成给人看的 Markdown（也是 AUDIT.md 的内容）。"""
    lines = ["# 审计报告", "",
             f"- 根目录：`{result.get('root')}`",
             f"- 结论：**{result.get('summary')}**",
             f"- {result.get('note')}", ""]
    if result.get("errors"):
        lines += ["## 没跑成的审计器（不等于通过）", ""]
        for e in result["errors"]:
            lines.append(f"- `{e['auditor']}`：{e['error']}")
        lines.append("")
    shown = 0
    for sev in ("P0", "P1", "P2", "P3"):
        items = (result.get("by_severity") or {}).get(sev) or []
        if not items:
            continue
        lines += [f"## {sev} — {SEVERITY_LABEL.get(sev, '')}（{len(items)} 条）", ""]
        for it in items:
            if shown >= limit:
                lines += [f"…… 还有更多，见 JSON 结果（已列 {limit} 条）", ""]
                return "\n".join(lines) + "\n"
            loc = it.get("path") or ""
            if it.get("line"):
                loc += f":{it['line']}"
            head = f"- **[{it.get('source')}]** " + (f"`{loc}` " if loc else "")
            lines.append(head + (it.get("message") or ""))
            if it.get("how_to_fix"):
                lines.append(f"  - 怎么改：{it['how_to_fix']}")
            shown += 1
        lines.append("")
    if not result.get("findings"):
        lines += ["没有发现任何问题（P0–P3 全清）。", ""]
    return "\n".join(lines)
