"""按代码规模产出**治理文件**，并做"工作集声明 + 反向依赖检查"。

为什么要有这个文件：分级体系里 S 级就要求"一份 50–100 行的 AGENTS.md"，
而系统之前**一份都不产出** —— 等于每次都从零猜项目惯例。
没有常驻规则，规模约束就只是报告里的几行字，落不到下一夜。

三件事：
  1) `write_governance(ws, level, contracts, requirement)`：按档位渲染 AGENTS.md
     （S 50–100 行；M 加子目录指针 + docs 指南 ≤300 行；L/XL 只留 ~100 行目录并标注不可改）；
  2) `workingset(paths, contracts)`：工作集声明（要动的文件/接口/非目标），M 级起强制；
  3) `reverse_deps(ws, changed, contracts)`：改了这些文件，谁会被影响（反向依赖）。

都是确定性的，不调模型。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import scale as _scale

GOVERNANCE_FILE = "AGENTS.md"
GOVERNANCE_DIR = ".sixcoder/governance"


def _limits_line(level: str) -> str:
    lim = _scale.quality_limits(level)
    return (f"圈复杂度 ≤{lim['complexity']}、嵌套 ≤{lim['depth']} 层、"
            f"单函数 ≤{lim['lines']} 行")


def _dont_do_pairs() -> list[tuple[str, str]]:
    """关键 don't / do 配对：分级表里 S 级就要求的一种写法。

    为什么用配对而不是只写禁令：禁令只说"不许"，模型/人不知道替代写法，
    实测会出现"绕过去了，但换了个更糟的写法"。配对把"不要 A"和"改成 B"钉在一起。
    """
    return [
        ("用 `except Exception: pass` 吞掉错误",
         "校验失败就 `raise ValueError(...)`，让判据能看见"),
        ("删掉或跳过测试让流水线变绿", "修实现，或把测试改成契约允许的行为"),
        ("把密钥写进代码或提交", "读环境变量；缺了就报错并写进交付报告"),
        ("在每个函数里都写 `if not isinstance(...)`",
         "在最外层入口校验一次，内部只做正常流"),
        ("复制一份函数改两行",
         "抽出公共函数，用参数区分（同名重复定义会被结构体检拦下）"),
        ("改了函数却没同步调用方", "先查反向依赖（谁 import 我），再动手"),
        ("用 `print` 当日志", "用 logging 并给 level；测试不依赖 stdout"),
        ("用 `assert` 做运行时校验", "运行时用显式 `if ... raise`；assert 只留给测试"),
    ]


def _hard_rules() -> list[str]:
    """全级通用的硬性禁令（这几条是"基本错误不许进仓库"）。"""
    return [
        "不许把密钥/凭证写进代码或提交（`OPENAI_API_KEY=...`、私钥、数据库口令）",
        "不许删除或跳过测试来让流水线变绿（`pytest.skip` / 删 assert / 改期望值都算）",
        "不许引入未经说明的新依赖（标准库优先；第三方要在 PR 里写清用途与替代方案）",
        "不许把校验逻辑用 `except Exception: pass` 吞掉（宁可抛出，也不要静默）",
        "行为必须有机械判据：契约里的示例与边界要能跑起来，模型自述不算证据",
    ]


def render_agents_md(level: str, commands: list[str] | None = None,
                     dirs: list[tuple[str, str]] | None = None,
                     requirement: str = "") -> str:
    """渲染这一级的 AGENTS.md（S 50–100 行；M 加子目录指针；L/XL 只做目录）。"""
    t = _scale.profile(level)
    out: list[str] = [f"# AGENTS.md（{t.id} 级：{t.label}）", ""]
    if t.id in ("L", "XL"):
        out += ["> **本文件是渐进式披露入口，不是规则堆。** 细节在 `docs/` 里按需加载；",
                "> 治理文件（本文件、`mission.md`、`factory-rules.md`）Agent 不得修改。", ""]
    out += [f"本仓库按 **{t.id} 级**约束执行。这一级的核心判断：{t.judgement}。", ""]
    out += ["## 1. 逐字命令（照抄执行，不要自创）", ""]
    for c in (commands or ["python -m pytest -q", "python -m compileall -q ."]):
        out.append(f"```\n{c}\n```")
    out.append("")
    out += ["## 2. 目录职责（一句话一个）", ""]
    for d, why in (dirs or [(".", "全部产物；没有子目录时只有根")]):
        out.append(f"- `{d}` — {why}")
    out.append("")
    out += ["## 3. 硬性禁令（任何一级都不许破）", ""]
    out += [f"{i}. {r}" for i, r in enumerate(_hard_rules(), 1)]
    out.append("")
    out += ["### 关键 don't / do 配对（写错就照右边改）", "",
            "| 不要这样 | 改成这样 |", "|---|---|"]
    for bad, good in _dont_do_pairs():
        out.append(f"| {bad} | {good} |")
    out.append("")
    out += ["## 4. 这一级的质量阈值", "",
            f"- {_limits_line(t.id)}",
            f"- 评审方式：{t.review}",
            f"- 上下文纪律：{t.context}",
            f"- 熵管理：{t.entropy}"]
    out.append("")
    if t.id in ("S",):
        out += ["## 5. 明确不做（本级刻意不上，别为它们花时间）", ""]
        out += [f"- {x}" for x in _scale.SKIPPED[t.id]]
        out.append("")
    if t.id in ("M", "L", "XL"):
        out += ["## 5. 子目录规则（离任务最近者优先，但根级禁令全局生效）", "",
                "- 每个模块目录可放自己的 `AGENTS.md`；与本文件冲突时，**禁令以根文件为准**",
                "- 领域指南放 `docs/`，每篇 ≤300 行，按需加载，不要全量塞进上下文"]
        out.append("")
    out += ["## 6. 升级触发（达到任一条就该改档）", "", f"- {t.upgrade}", ""]
    if requirement.strip():
        out += ["## 7. 当前需求（来自甲方）", "", "```", requirement.strip()[:800], "```", ""]
    out.append(f"<!-- 生成于 {time.strftime('%Y-%m-%d %H:%M:%S')}，档位 {t.id} -->")
    return "\n".join(out)


def write_governance(ws: Path, level: str, contracts: dict | None = None,
                     requirement: str = "", force: bool = False) -> dict:
    """把治理文件落到工作区。**已存在且不是我们生成的就不覆盖**（人写的优先）。

    M 级起额外写子目录指针：每个模块目录一份极短的 AGENTS.md，指向根文件。
    """
    ws = Path(ws)
    root = ws / GOVERNANCE_FILE
    cmds = ["python -m pytest -q --tb=short"]
    dirs = [(str(Path(p).parent) if str(Path(p).parent) != "." else ".", "模块目录")
            for p in sorted(contracts or {})][:12]
    text = render_agents_md(level, commands=cmds, dirs=dirs or None, requirement=requirement)
    wrote: list[str] = []
    skipped: list[str] = []
    if root.exists() and not force:
        old = root.read_text(encoding="utf-8", errors="replace")
        if "SixCoder" not in old and "生成于" not in old:
            skipped.append(str(root))
    else:
        root.write_text(text, encoding="utf-8")
        wrote.append(str(root))
    lv = _scale.profile(level).id
    if lv in ("M", "L", "XL"):
        for p in sorted(contracts or {}):
            d = (ws / Path(p).parent)
            if not d.exists() or d == ws:
                continue
            f = d / GOVERNANCE_FILE
            if f.exists() and not force:
                continue
            f.write_text(
                f"# {d.name}/ 的局部规则（{lv} 级）\n\n"
                f"1. **根文件硬性禁令全局生效**：见仓库根的 `{GOVERNANCE_FILE}`，不得覆盖。\n"
                f"2. 本目录只放这一层的约定（数据形态、对外接口、不要碰的文件）。\n"
                f"3. 质量阈值同根文件：{_limits_line(lv)}。\n"
                f"4. 改动这里之前先看有没有别的模块 import 它（反向依赖）。\n",
                encoding="utf-8")
            wrote.append(str(f))
    if lv in ("M", "L", "XL"):
        gdir = ws / GOVERNANCE_DIR
        gdir.mkdir(parents=True, exist_ok=True)
        (gdir / "architecture.md").write_text(
            f"# 架构约束（{lv} 级）\n\n"
            "> 分层方向由人类（架构 owner）定义；Agent 只负责在约束内实现。\n\n"
            "## 依赖方向\n\n"
            "```\n"
            "Types → Config → Repo → Service → Runtime → UI\n"
            "```\n\n"
            "- 依赖只能**单向**；反向 import 一律视为违规（M 级起由架构测试拦截）。\n"
            "- 跨层调用必须经过下一层的公开接口，不许直接摸对方的内部函数。\n",
            encoding="utf-8")
        wrote.append(str(gdir / "architecture.md"))
    return {"ok": True, "level": lv, "wrote": wrote, "skipped": skipped,
            "lines": len(text.splitlines())}


# ------------------------------------------------------------------ 工作集声明
def workingset(changed: list[str], contracts: dict | None = None,
               requirement: str = "", non_goals: list[str] | None = None) -> dict:
    """工作集声明：动手前写清"要动什么、依赖谁的接口、相关测试、明确的非目标"。

    M 级起强制（`scale.gate_mode("workingset", level) == "block"`）：
    影响面超出声明就必须停下来报告，而不是"顺手修了"。
    """
    files = sorted({str(p) for p in changed or []})
    tests = sorted({f"test_{Path(p).stem}.py" for p in files})
    ifaces: list[str] = []
    for p in files:
        c = (contracts or {}).get(p)
        for f in (getattr(c, "funcs", None) or []):
            ifaces.append(f"{p}::{f.name}")
    return {"changed": files, "tests": tests, "interfaces": ifaces[:20],
            "non_goals": list(non_goals or []),
            "requirement": (requirement or "").strip()[:200]}


def render_workingset(ws_decl: dict) -> str:
    if not ws_decl:
        return ""
    lines = ["【工作集声明（先按这个范围做，超出就停下来报告）】",
             "- 要改的文件：" + ("、".join(f"`{x}`" for x in ws_decl.get("changed") or []) or "（无）"),
             "- 依赖的接口：" + ("、".join(f"`{x}`" for x in ws_decl.get("interfaces") or []) or "（无）"),
             "- 相关测试：" + ("、".join(f"`{x}`" for x in ws_decl.get("tests") or []) or "（无）")]
    if ws_decl.get("non_goals"):
        lines.append("- 明确的非目标：" + "、".join(ws_decl["non_goals"]))
    lines.append("- 超出这个范围 = 停下来写进报告，不要顺手改")
    return "\n".join(lines)


# ------------------------------------------------------------------ 反向依赖
def reverse_deps(ws: Path, changed: list[str], limit: int = 40) -> list[tuple[str, str]]:
    """谁 import 了这些文件（反向依赖）。改了别人依赖的东西，必须知道会波及谁。"""
    ws = Path(ws)
    mods = {Path(p).stem for p in changed or []}
    if not mods:
        return []
    out: list[tuple[str, str]] = []
    for p in ws.rglob("*.py"):
        if any(part in {".git", ".sixcoder", "__pycache__"} for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in mods:
            if f"import {m}" in text or f"from {m} import" in text:
                rel = str(p.relative_to(ws))
                if rel not in changed:
                    out.append((rel, m))
                    break
        if len(out) >= limit:
            break
    return sorted(out)


def _sha(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


GOVERNANCE_LOCK = ".sixcoder/governance/lock.json"


def _table_cols_text(contracts: dict | None) -> set[str]:
    """从契约原文里取表列（建表语句），用于 schema 指纹。"""
    import re as _re
    out: set[str] = set()
    joined = "\n".join(str(getattr(c, "raw", "") or "") for c in (contracts or {}).values())
    for m in _re.finditer(
            r"[A-Za-z_]\w{1,30}\s*\(([^()]*(?:INTEGER|TEXT|REAL|BLOB)[^()]*)\)", joined):
        for piece in m.group(1).split(","):
            name = piece.strip().split()[0] if piece.strip() else ""
            if _re.match(r"^[A-Za-z_]\w{0,40}$", name):
                out.add(name.lower())
    return out


def sig_digest(contracts: dict | None) -> str:
    """契约结构性指纹：函数名 + 参数类型 + 返回 + 表结构列。

    为什么要单独算：治理层要求"核心契约不可变（分层/公共签名/schema）"，
    而签名被悄悄改了**不会让任何单函数判据变红** —— 只有指纹能发现。
    只取结构性信息，不取自然语言描述，避免文档措辞变化就误报。
    """
    import hashlib
    rows: list[str] = []
    for path in sorted(contracts or {}):
        c = (contracts or {}).get(path)
        for f in (getattr(c, "funcs", None) or []):
            params = ",".join(f"{getattr(p, 'name', '')}:{getattr(p, 'type', '')}"
                              for p in (getattr(f, "params", None) or []))
            rows.append(f"{path}::{f.name}({params})->{getattr(f, 'returns', '')}")
    cols = sorted(_table_cols_text(contracts))
    blob = "\n".join(sorted(rows)) + "||" + ",".join(cols)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def lock_governance(ws: Path, contracts: dict | None = None) -> dict:
    """给治理文件记指纹，之后被改动就能发现（XL 级要求"治理文件不可变"）。

    为什么需要：现在只在**路径**上拦（`scale.protected_hits`），
    但拦不住"绕过门禁直接改"或者"上一轮自己改掉了"。
    记指纹后，每次开跑对比一次，被改过就如实报出来（谁改的无法追溯，但改过这件事能)。
    """
    ws = Path(ws)
    files = [ws / GOVERNANCE_FILE]
    files += sorted(p for p in ws.rglob(GOVERNANCE_FILE) if p.parent != ws)
    files += sorted((ws / GOVERNANCE_DIR).glob("*.md")) if (ws / GOVERNANCE_DIR).exists() else []
    snap = {str(p.relative_to(ws)).replace("\\", "/"): _sha(p) for p in files if p.exists()}
    if not snap:
        return {"ok": False, "reason": "没有治理文件可锁定"}
    import json
    f = ws / GOVERNANCE_LOCK
    f.parent.mkdir(parents=True, exist_ok=True)
    prev = {}
    if f.exists():
        try:
            prev = json.loads(f.read_text(encoding="utf-8")).get("files") or {}
        except Exception:                                # noqa: BLE001
            prev = {}
    payload: dict = {"files": snap, "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    if contracts is not None:
        payload["contract_sig"] = sig_digest(contracts)
    f.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "files": snap, "previous": prev,
            "contract_sig": payload.get("contract_sig", "")}


def verify_governance(ws: Path, level: str = "XL", contracts: dict | None = None) -> dict:
    """检查治理文件有没有被改过（XL 级才算；L 及以下只报告不追究）。"""
    import json
    ws = Path(ws)
    f = ws / GOVERNANCE_LOCK
    if not f.exists():
        return {"ok": True, "checked": False, "note": "还没有治理指纹（首次开跑会建立）"}
    try:
        raw = json.loads(f.read_text(encoding="utf-8"))
        want = raw.get("files") or {}
        locked_sig = raw.get("contract_sig") or ""
    except Exception:                                    # noqa: BLE001
        return {"ok": True, "checked": False, "note": "指纹文件读不了，跳过"}
    changed: list[str] = []
    for rel, sha in want.items():
        p = ws / rel
        if not p.exists():
            changed.append(f"{rel}（被删）")
        elif _sha(p) != sha:
            changed.append(f"{rel}（被改）")
    # 契约签名 / 表结构：被悄悄改了不会让任何单函数判据变红，只有指纹能发现
    if contracts is not None and locked_sig:
        now_sig = sig_digest(contracts)
        if now_sig != locked_sig:
            changed.append(f"契约签名/表结构被改（{locked_sig} → {now_sig}）")
    if not changed:
        return {"ok": True, "checked": True,
                "note": f"{len(want)} 个治理文件与指纹一致"
                        + ("，契约签名未变" if locked_sig else "")}
    msg = "治理文件被改动：" + "、".join(changed[:5])
    hard = str(level).upper() == "XL"
    return {"ok": not hard, "checked": True, "changed": changed,
            "note": msg + ("（XL 级：治理文件不可变，需人工确认后重新锁定）" if hard
                           else "（本级只报告，不阻断）")}


# ------------------------------------------------------------------ 领域指南（M+）
GUIDE_DIR = "docs/guides"
GUIDE_MAX_LINES = 300


def write_guides(ws: Path, level: str, contracts: dict | None = None) -> dict:
    """M 级起生成领域指南（每篇 ≤300 行），并校验**不许超长**。

    为什么要有：M 级的约束是"AGENTS.md 只做目录，细节下放到领域指南按需加载"。
    没有指南文件，这句规则就是空的（之前就是这样）。
    """
    lv = _scale.profile(level).id
    if lv == "S":
        return {"ok": True, "skipped": True, "reason": "S 级不建 docs 知识库"}
    ws = Path(ws)
    gdir = ws / GUIDE_DIR
    gdir.mkdir(parents=True, exist_ok=True)
    wrote: list[str] = []
    mods = sorted(contracts or {})
    per = 1
    chunks = [mods[i:i + per] for i in range(0, len(mods), per)] or [[]]
    for i, group in enumerate(chunks[:20]):
        name = (Path(group[0]).stem if group else "overview")
        f = gdir / f"{name}.md"
        if f.exists():
            continue
        body = [f"# {name}（领域指南 · {lv} 级）", "",
                "> 按需加载：只在改这个模块时读，不要全量塞进上下文。", ""]
        for path in group:
            c = (contracts or {}).get(path)
            body.append(f"## `{path}`")
            body.append("")
            body.append("| 函数 | 参数 | 返回 | 约束（来自契约） |")
            body.append("|---|---|---|---|")
            for fn in (getattr(c, "funcs", None) or []):
                params = ", ".join(f"{p.name}: {p.type}".replace(": object", "")
                                   for p in (fn.params or []))
                body.append(f"| `{fn.name}` | {params or '—'} | {fn.returns or '—'} | "
                            f"{(fn.doc or '')[:120].replace('|', '/')} |")
            body.append("")
        f.write_text("\n".join(body), encoding="utf-8")
        wrote.append(str(f))
    # 校验：不许超长（这就是 M 级规则里那句"≤300 行"的机械检查）
    over = []
    for f in gdir.glob("*.md"):
        n = len(f.read_text(encoding="utf-8").splitlines())
        if n > GUIDE_MAX_LINES:
            over.append(f"{f.name}（{n} 行）")
    return {"ok": not over, "wrote": wrote, "over_limit": over,
            "note": ("有指南超过 300 行：" + "、".join(over)) if over
                    else f"指南均 ≤{GUIDE_MAX_LINES} 行"}


def gate_workingset(ws: Path, changed: list[str], declared: list[str] | None = None,
                    level: str = "M") -> tuple[bool, str]:
    """工作集越界检查：实际改的文件是不是超出了声明范围。"""
    mode = _scale.gate_mode("workingset", level)
    if mode == "skip":
        return True, "S 级不做工作集声明（人工审查覆盖）"
    if not declared:
        return False, "M 级以上要求先声明工作集（要改哪些文件/依赖什么接口/测试/非目标）"
    extra = [p for p in changed if p not in declared]
    if extra:
        return False, ("超出工作集：" + "、".join(extra[:5])
                       + "（要么改声明并说明理由，要么把这部分留到下一轮）")
    return True, f"工作集内（{len(changed)} 个文件）"
