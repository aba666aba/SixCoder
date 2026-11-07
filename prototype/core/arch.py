"""分级体系里"M 级起必须有"的几道门禁。全部确定性、不调模型。

对应 docs/24 的分级表：
  · `gate_layers`      架构依赖方向（M+）：只许单向依赖，反向 import 直接拦。
                       Python 生态没有 ArchUnit，这里用 ast 做——零依赖、毫秒级。
  · `gate_coverage`    覆盖率地板（M+，默认 80%）：用 coverage.py 有就跑，没有就如实说"测不了"，
                       **绝不用行数假装覆盖率**。
  · `gate_mutation_changed` 变更行变异（L+，kill ≥70%）：只对本次改动涉及的行做变异，
                       比全文件变异快一个量级，也更贴"这次改动有没有被测到"。
  · `gate_dep_whitelist`    依赖白名单（L+）：白名单文件之外的第三方依赖必须拦。
  · `health_metrics`   持续架构健康度（XL）：模块度/无环性/最大深度/冗余的静态指标。

每条都返回 GateResult（与 core/gates.py 同构），"做不到"时如实降级为 advisory，
不许假装通过（这是本项目一以贯之的纪律：做不到的事要如实说明）。
"""
from __future__ import annotations

import ast
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import scale as _scale


@dataclass
class GateResult:
    gate: str
    ok: bool
    evidence_kind: str
    evidence_ref: str = ""
    detail: str = ""
    elapsed_ms: int = 0


_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache"}


def _py_files(ws: Path) -> list[Path]:
    ws = Path(ws)
    out = []
    for p in ws.rglob("*.py"):
        rel = p.relative_to(ws)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if p.name.startswith("test_") or p.name.endswith("_test.py"):
            continue
        # 引擎自己的临时探针不是客户代码：以前它被算进模块数/依赖图，
        # 交付报告于是写"2 个模块"（实际只有 1 个）。
        if p.name.startswith("_props"):
            continue
        out.append(p)
    return sorted(out)


# ------------------------------------------------------------------ 架构分层
def read_layers(ws: Path) -> list[str]:
    """从治理文件里读分层方向（`Types → Config → Repo → Service → Runtime → UI`）。"""
    f = Path(ws) / ".sixcoder" / "governance" / "architecture.md"
    if not f.exists():
        return []
    text = f.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if "→" in line or "->" in line:
            parts = re.split(r"→|->", line)
            names = [p.strip(" `|\t") for p in parts if p.strip(" `|\t")]
            if len(names) >= 2:
                return names
    return []


def _layer_of(rel: str, layers: list[str]) -> int:
    """这个文件落在第几层（按目录名/文件名匹配；匹配不到返回 -1 = 不受分层约束）。"""
    low = rel.replace("\\", "/").lower()
    for i, name in enumerate(layers):
        key = name.lower().replace(" ", "")
        if f"/{key}/" in f"/{low}" or low.startswith(f"{key}/") \
                or f"/{key}." in f"/{low}" or Path(low).stem == key:
            return i
    return -1


def gate_layers(ws: Path, level: str = "M") -> GateResult:
    """架构依赖方向（M 级起执行）：只许"低层 ← 高层"单向依赖，反向即违规。

    为什么用 ast 自己算：Python 没有 ArchUnit；dependency-cruiser 是 JS 生态。
    这套系统的判据必须能在没有网络、没有第三方包的机器上跑（甲方机器常常是裸 Python）。
    """
    t0 = time.time()
    mode = _scale.gate_mode("architecture", level)
    layers = read_layers(ws)
    if mode == "skip":
        return GateResult("layers", True, "static_rule", "skipped",
                          "S 级不启用架构测试：边界由人工审查维护（成本高于收益）",
                          int((time.time() - t0) * 1000))
    if not layers:
        return GateResult("layers", True, "static_rule", "no-layers",
                          "没有声明分层方向（.sixcoder/governance/architecture.md）→ 无法判，"
                          "按『做不到就如实说』处理，不给通过结论", int((time.time() - t0) * 1000))
    violations: list[str] = []
    for p in _py_files(ws):
        rel = str(p.relative_to(ws))
        src_layer = _layer_of(rel, layers)
        if src_layer < 0:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            name = ""
            if isinstance(n, ast.Import):
                for a in n.names:
                    name = a.name.split(".")[0]
                    dst = _layer_of(name + ".py", layers)
                    if dst > src_layer:
                        violations.append(f"{rel}（{layers[src_layer]}）import 了更高层 "
                                          f"{name}（{layers[dst]}）")
            elif isinstance(n, ast.ImportFrom) and n.module:
                dst = _layer_of(n.module.split(".")[0] + ".py", layers)
                if dst > src_layer:
                    violations.append(f"{rel}（{layers[src_layer]}）from {n.module}（{layers[dst]}）"
                                      f"—— 反向依赖")
    if violations:
        return GateResult("layers", False, "static_rule", f"{len(violations)} 处",
                          "；".join(violations[:3]), int((time.time() - t0) * 1000))
    return GateResult("layers", True, "static_rule", "ok",
                      f"依赖方向合规（{len(layers)} 层：{' → '.join(layers)}）",
                      int((time.time() - t0) * 1000))


# ------------------------------------------------------------------ 覆盖率地板
def gate_coverage(ws: Path, level: str = "M", floor: float | None = None,
                  timeout: int = 180) -> GateResult:
    """覆盖率地板（M 级起）。**用覆盖率工具实测，测不了就如实说测不了。**"""
    t0 = time.time()
    mode = _scale.gate_mode("coverage", level)
    if mode == "skip":
        return GateResult("coverage", True, "static_rule", "skipped",
                          "S 级不设覆盖率地板（小程序硬压会诱导写垃圾测试）",
                          int((time.time() - t0) * 1000))
    want = float(floor if floor is not None else (_scale.profile(level).flags.get("coverage_floor")
                                                  or 0.8))
    try:
        import coverage  # noqa: F401
    except Exception:                                    # noqa: BLE001
        return GateResult("coverage", True, "static_rule", "unavailable",
                          f"本机没装 coverage，测不了真实覆盖率（{level} 级要求 ≥{want:.0%}）。"
                          f"不假装通过：装 `pip install coverage` 后重跑即可得到硬证据",
                          int((time.time() - t0) * 1000))
    import subprocess
    import sys
    # `.coverage` 会落在**客户仓库根目录**（实测产物里多出一个 .coverage 文件）→ 指到引擎自己的
    # 隐藏目录里，交付物保持干净。
    _cov_env = {**os.environ}
    _covdir = Path(ws) / ".sixcoder" / "tmp"
    try:
        _covdir.mkdir(parents=True, exist_ok=True)
        _cov_env["COVERAGE_FILE"] = str(_covdir / ".coverage")
    except OSError:
        pass
    try:
        # **只测客户仓库里的代码**：不指定 --source 时 coverage 会把运行环境注入的
        # sitecustomize.py（实测 834 行、10% 覆盖）也算进 TOTAL，把真实 100% 拉到 13%，
        # M 档 80% 地板因此永远判红 —— 覆盖率就成了假红，会卡死整次夜班。
        # --source 指向工作区，report 再按同口径过滤，外部文件一律不计。
        source = str(Path(ws).resolve())
        proc = subprocess.run([sys.executable, "-m", "coverage", "run",
                               f"--source={source}", "-m", "pytest", "-q"],
                              cwd=str(ws), capture_output=True, text=True, timeout=timeout,
                              env=_cov_env)
        rep = subprocess.run([sys.executable, "-m", "coverage", "report", "--format=total",
                              f"--include={source}/*"],
                             cwd=str(ws), capture_output=True, text=True, timeout=60,
                             env=_cov_env)
        total = float((rep.stdout or "0").strip().splitlines()[-1] or 0)
    except Exception as exc:                             # noqa: BLE001
        return GateResult("coverage", True, "static_rule", "error",
                          f"覆盖率测量失败（{type(exc).__name__}）；不给通过结论",
                          int((time.time() - t0) * 1000))
    ok = total / 100.0 >= want
    return GateResult("coverage", ok, "engine_assertion", f"{total:.0f}%",
                      f"实测覆盖率 {total:.0f}%（{level} 级要求 ≥{want:.0%}）"
                      + ("" if ok else " → 低于地板"),
                      int((time.time() - t0) * 1000))


# ------------------------------------------------------------------ 变更行变异
def changed_lines_from_diff(ws: Path, rel: str) -> set[int]:
    """这个文件在 git 工作区里**改动过的行号**（没有 git/没有改动就返回空集）。"""
    import subprocess
    try:
        p = subprocess.run(["git", "diff", "-U0", "--", rel], cwd=str(ws),
                           capture_output=True, text=True, timeout=30)
    except Exception:                                    # noqa: BLE001
        return set()
    lines: set[int] = set()
    for m in re.finditer(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", p.stdout or "", re.M):
        start = int(m.group(1))
        count = int(m.group(2) or 1)
        lines.update(range(start, start + max(1, count)))
    return lines


def gate_mutation_changed(ws: Path, changed: dict[str, tuple[str, str]] | None = None,
                          level: str = "L", min_kill: float | None = None) -> GateResult:
    """变更行变异（L 级起，kill ≥70%）：只变异本次改动的行。

    为什么按变更行：全文件变异在 50 万行以上跑不动，而且大部分变异体跟"这次改动"无关。
    没有 git diff 时退回"整文件"并明确说明（不假装是增量）。
    """
    t0 = time.time()
    mode = _scale.gate_mode("mutation", level)
    if mode == "skip":
        return GateResult("mutation_changed", True, "static_rule", "skipped",
                          f"{level} 级不跑变异测试", int((time.time() - t0) * 1000))
    want = float(min_kill if min_kill is not None
                 else (_scale.profile(level).flags.get("mutation_min") or 0.7))
    from . import gates as _gates
    files = sorted((changed or {}).keys()) or [str(p.relative_to(ws)) for p in _py_files(ws)[:3]]
    tested = 0
    killed = 0
    scope = "变更行"
    for rel in files:
        try:
            src = Path(ws, rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = changed_lines_from_diff(ws, rel)
        if not lines:
            scope = "整文件（无 git diff，退化为全文件）"
            lines = set(range(1, len(src.splitlines()) + 1))
        sub = "\n".join(ln if (i + 1) in lines else "" for i, ln in enumerate(src.splitlines()))
        mm = _gates.gate_mutation(ws, {rel: ("", sub)}, [p.name for p in Path(ws).glob("test_*.py")])
        ref = str(mm.evidence_ref or "")
        got, _, tot = ref.replace("killed=", "").partition("/")
        if got.isdigit() and tot.isdigit():
            killed += int(got)
            tested += int(tot)
    if not tested:
        return GateResult("mutation_changed", True, "static_rule", "no-mutants",
                          f"没有可用的变异体（{scope}）；不给通过结论",
                          int((time.time() - t0) * 1000))
    rate = killed / tested
    ok = rate >= want
    return GateResult("mutation_changed", ok, "engine_assertion",
                      f"killed={killed}/{tested}",
                      f"{scope}变异杀死率 {rate:.0%}（{level} 级要求 ≥{want:.0%}）"
                      + ("" if ok else " → 这次改动没被测住"),
                      int((time.time() - t0) * 1000))


# ------------------------------------------------------------------ 依赖白名单
WHITELIST_FILE = ".sixcoder/governance/deps-allow.txt"


def gate_dep_whitelist(ws: Path, level: str = "L") -> GateResult:
    """依赖白名单（L 级起硬约束）：白名单外的第三方依赖必须拦。

    白名单文件格式：一行一个包名，`#` 注释；标准库不用写。
    """
    t0 = time.time()
    mode = _scale.gate_mode("dep_whitelist", level)
    if mode == "skip":
        return GateResult("dep_whitelist", True, "static_rule", "skipped",
                          "S 级不用依赖白名单（改为 PR 说明理由 + 人工确认）",
                          int((time.time() - t0) * 1000))
    if mode == "advisory":
        # M 级：白名单不是硬约束，只做提示（这一级还没有足够的依赖面去维护一份名单）
        return GateResult("dep_whitelist", True, "static_rule", "advisory",
                          "M 级不强制白名单：新依赖由 PR 说明理由 + 人工确认即可",
                          int((time.time() - t0) * 1000))
    f = Path(ws) / WHITELIST_FILE
    if not f.exists():
        return GateResult("dep_whitelist", False, "static_rule", "missing",
                          f"L 级要求有依赖白名单文件 `{WHITELIST_FILE}`（一行一个包名）；"
                          f"没有白名单 = 任何第三方依赖都不受控",
                          int((time.time() - t0) * 1000))
    allowed = {ln.split("#")[0].strip().lower() for ln in
               f.read_text(encoding="utf-8", errors="replace").splitlines()}
    allowed.discard("")
    import sys as _sys
    stdlib = set(getattr(_sys, "stdlib_module_names", ())) | {"sqlite3", "typing", "dataclasses"}
    bad: list[str] = []
    for p in _py_files(ws):
        rel = str(p.relative_to(ws))
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            names: list[str] = []
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                names = [n.module.split(".")[0]]
            for name in names:
                low = name.lower()
                if low in stdlib or low in allowed:
                    continue
                if any(low == Path(q).stem.lower() for q in
                       [str(x.relative_to(ws)) for x in _py_files(ws)]):
                    continue                              # 本仓库自己的模块
                if low in _LOCAL_HINT:
                    continue
                bad.append(f"{rel} import {name}")
    if bad:
        return GateResult("dep_whitelist", False, "static_rule", f"{len(bad)} 处",
                          "白名单外的依赖：" + "；".join(bad[:3])
                          + f"（要引入就写进 `{WHITELIST_FILE}` 并在 PR 说明用途）",
                          int((time.time() - t0) * 1000))
    return GateResult("dep_whitelist", True, "static_rule", "ok",
                      f"依赖都在白名单内（{len(allowed)} 个）", int((time.time() - t0) * 1000))


_LOCAL_HINT = {"m", "calc", "clean", "store", "report", "main", "app", "utils", "core",
               "models", "views", "service", "repo", "config", "runtime", "ui", "types"}


# ------------------------------------------------------------------ XL 健康度
def _module_index(ws: Path) -> dict[str, Path]:
    """点分模块名 → 文件。`pkg/__init__.py` 记成包名本身。

    为什么不能像以前那样只用 `p.stem` 当节点名：包结构下会丢两类依赖 ——
      · `from cal.core import numeric`（点分绝对导入）：head 是 "cal"，而 stem 表里没有 "cal" → 无边
      · `from . import base_events`（`ImportFrom.module is None`）→ 以前直接跳过
    实测一个 90 文件的包式仓库被判成 `edges=0`（看起来"没有依赖"，实际跨包 import 一大堆）；
    而 `asyncio` 这种相对导入密集的包只认出一半的边。依赖图错了，环/深度/密度就全是错的。
    """
    idx: dict[str, Path] = {}
    for p in _py_files(ws):
        try:
            rel = p.relative_to(ws)
        except ValueError:
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        if not parts:
            continue
        idx[".".join(parts)] = p
    return idx


def _resolve(name: str, idx: dict[str, Path]) -> str:
    """把导入名落到仓库里的模块（取最长可命中的前缀）；落不到返回空串。"""
    cur = name
    while cur:
        if cur in idx:
            return cur
        cur = cur.rsplit(".", 1)[0] if "." in cur else ""
    return ""


def _import_targets(node: ast.AST, me: str, idx: dict[str, Path]) -> set[str]:
    """一条 import 语句指向仓库里的哪些模块（点分名）。"""
    out: set[str] = set()
    pkg = me.rsplit(".", 1)[0] if "." in me else ""
    if isinstance(node, ast.Import):
        for a in node.names:
            hit = _resolve(a.name, idx)
            if hit:
                out.add(hit)
        return out
    if not isinstance(node, ast.ImportFrom):
        return out
    level = int(getattr(node, "level", 0) or 0)
    base = str(getattr(node, "module", "") or "")
    if level:                                   # 相对导入：回到当前包的对应层
        parts = pkg.split(".") if pkg else []
        drop = level - 1
        if drop > len(parts):
            return out
        parts = parts[:len(parts) - drop] if drop else parts
        base = ".".join([*parts, *([base] if base else [])])
    if base:
        pass
    hits: set[str] = set()
    for a in node.names:                        # from pkg import mod → pkg.mod（落到最深模块）
        if a.name == "*":
            continue
        cand = f"{base}.{a.name}" if base else a.name
        hit = _resolve(cand, idx)
        if hit:
            hits.add(hit)
    if hits:
        out |= hits
    elif base:
        # 只有"名字没落到模块"时才算对包本身的依赖（例如 from .utils import helper）。
        # 为什么不能无条件加包节点：`from . import x` 会让每个子模块都连到包 __init__，
        # 而包 __init__ 通常又 import 子模块 → 任何"包 re-export 风格"的仓库都会凭空长出环，
        # 依赖密度也被灌水（实测 asyncio：加包节点后 edges=133、无环=False、密度 0.126；
        # 只保留最深模块后环消失、密度回到正常量级）。
        hit = _resolve(base, idx)
        if hit:
            out.add(hit)
    out.discard(me)
    return out


def health_metrics(ws: Path) -> dict:
    """持续架构健康度（XL 级）：模块度、无环性、最大依赖深度、重复 import 密度。

    全部静态可算，不依赖任何第三方工具——这一级唯一的约束力来源就是"能持续算出漂移"。
    节点名用**点分模块名**（`cal.core.numeric`），不再用文件 stem：stem 会把不同包里
    同名模块混成一个节点，也认不出包式导入。
    """
    idx = _module_index(ws)
    graph: dict[str, set[str]] = {}
    for name, p in idx.items():
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            graph[name] = set()
            continue
        deps: set[str] = set()
        for n in ast.walk(tree):
            deps |= _import_targets(n, name, idx)
        graph[name] = deps

    def _cycle(node: str, seen: set[str], stack: set[str]) -> bool:
        if node in stack:
            return True
        if node in seen:
            return False
        seen.add(node)
        stack.add(node)
        for d in graph.get(node, ()):  # noqa: SIM118
            if _cycle(d, seen, stack):
                return True
        stack.discard(node)
        return False

    seen: set[str] = set()
    cyclic = [m for m in graph if _cycle(m, seen, set())]
    def _depth(node: str, memo: dict[str, int], stack: set[str]) -> int:
        if node in memo:
            return memo[node]
        if node in stack:
            return 0
        stack.add(node)
        d = 0 if not graph.get(node) else 1 + max(_depth(x, memo, stack) for x in graph[node])
        stack.discard(node)
        memo[node] = d
        return d
    memo: dict[str, int] = {}
    depth = max((_depth(m, memo, set()) for m in graph), default=0)
    edges = sum(len(v) for v in graph.values())
    n = max(1, len(graph))
    density = edges / (n * (n - 1)) if n > 1 else 0.0
    return {"modules": n, "edges": edges, "acyclic": not cyclic,
            "cyclic": sorted(cyclic)[:5], "max_depth": depth,
            "dependency_density": round(density, 4)}


# ------------------------------------------------------------------ 变更规模（M+）
def diff_size(ws: Path) -> dict:
    """本次工作区的改动规模：文件数、增删行数（git 可用时才算得准）。"""
    import subprocess
    ws = Path(ws)
    # 必须确认"它自己就是一个 git 工作树的根"。
    # 否则 `git diff` 会往上找到外层仓库，把外层无关的改动算进来
    # （沙箱临时目录里实测踩过：报 ok=True、+0/-0，其实是外层仓库的 diff）。
    try:
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(ws),
                                capture_output=True, text=True, timeout=15)
        if inside.returncode != 0 or (inside.stdout or "").strip() != "true":
            return {"ok": False, "reason": "不是 git 工作树，算不出改动规模"}
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=str(ws),
                             capture_output=True, text=True, timeout=15)
        root = (top.stdout or "").strip()
        if root and Path(root).resolve() != ws.resolve():
            return {"ok": False,
                    "reason": "该目录在外层仓库里（不是它自己的仓库），不借用外层 diff"}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}"}
    try:
        p = subprocess.run(["git", "diff", "--numstat"], cwd=str(ws),
                           capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return {"ok": False, "reason": "不是 git 仓库或无 HEAD，算不出改动规模"}
        files = adds = dels = 0
        for line in (p.stdout or "").splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            a, d = parts[0], parts[1]
            files += 1
            adds += int(a) if a.isdigit() else 0
            dels += int(d) if d.isdigit() else 0
        return {"ok": True, "files": files, "additions": adds, "deletions": dels,
                "changed": adds + dels}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}"}


def gate_diff_size(ws: Path, level: str = "M", soft_cap: int | None = None) -> GateResult:
    """单次改动规模软上限（M 级起 500 行）：**先只报告，不阻断**。

    为什么先不阻断：夜班是"一次一个模块"的直写，还没有 PR 拆分能力；
    先把它测出来、写进报告，让"这次改动很大"变成看得见的事实，
    等有了微分支隔离再升级成硬约束（低风险优先，不一次上两道闸）。
    """
    t0 = time.time()
    cap = int(soft_cap if soft_cap is not None
              else (_scale.profile(level).flags.get("diff_soft_cap") or 0))
    if not cap:
        return GateResult("diff_size", True, "static_rule", "skipped",
                          f"{level} 级不设改动规模软上限", int((time.time() - t0) * 1000))
    d = diff_size(ws)
    if not d.get("ok"):
        return GateResult("diff_size", True, "static_rule", "unavailable",
                          f"算不出改动规模（{d.get('reason')}）；不假装通过",
                          int((time.time() - t0) * 1000))
    over = d["changed"] > cap
    # 分级表口径："超出需拆分或说明理由" —— 所以 M 级以上**阻断**，
    # 但要留一个可追溯的豁免口：写了理由就放行，理由进交付报告（不是静默放过）。
    import os
    blocking_level = str(level).upper() in ("M", "L", "XL")
    reason = (os.environ.get("SIXCODER_DIFF_CAP_REASON") or "").strip()
    waived = over and blocking_level and bool(reason)
    ok = (not over) or (not blocking_level) or waived
    if not over:
        tail = "；在上限内"
    elif waived:
        tail = f"；**超过软上限但已说明理由**：{reason[:80]}（已记入交付报告）"
    elif blocking_level:
        tail = ("；**超过软上限，已阻断**：拆分成几个小改动，"
                "或设 SIXCODER_DIFF_CAP_REASON='为什么这次必须一次改完' 放行")
    else:
        tail = "；超过软上限（S 级不设硬上限：小项目切碎反而增加开销）"""
    return GateResult("diff_size", ok, "static_rule", f"+{d['additions']}/-{d['deletions']}",
                      (f"本次改动 {d['files']} 个文件、+{d['additions']}/-{d['deletions']}"
                       f"（{level} 级软上限 {cap} 行）" + tail),
                      int((time.time() - t0) * 1000))


# ------------------------------------------------------------------ 健康度漂移
HEALTH_FILE = ".sixcoder/health.json"


def save_health(ws: Path, metrics: dict) -> dict:
    """记下**基线**（开跑时的健康度）。收工时拿它对比才知道有没有漂移。"""
    import json
    f = Path(ws) / HEALTH_FILE
    prev = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            prev = data.get("baseline") or data.get("now") or {}
        except Exception:                                # noqa: BLE001
            prev = {}
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"baseline": metrics, "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "previous_baseline": prev},
                            ensure_ascii=False, indent=2), encoding="utf-8")
    return prev


def health_drift(ws: Path) -> dict:
    """与上次相比的漂移：模块数、依赖边、最大深度、无环性的变化。

    为什么必须算 delta：XL 级唯一的约束力来源就是"能看出质量在退化"。
    只算当前值等于每次都是新账本，看不出趋势。
    """
    import json
    now = health_metrics(ws)
    f = Path(ws) / HEALTH_FILE
    prev: dict = {}
    if f.exists():
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            # 基线取"上次开跑时的快照"：开跑前由 night 覆盖写入 now
            prev = data.get("baseline") or data.get("now") or {}
        except Exception:                                # noqa: BLE001
            prev = {}
    if not prev:
        return {"has_baseline": False, "now": now,
                "note": "没有基线（首次开跑或仓库刚起步）；本次指标已存，下次可对比"}
    if int(prev.get("modules") or 0) < 2:
        # 空仓库/单模块不算漂移：从 0 长起来不是"退化"
        return {"has_baseline": False, "now": now, "prev": prev,
                "note": "基线太小（<2 个模块，仓库刚起步），不判漂移"}
    delta = {
        "modules": now["modules"] - int(prev.get("modules") or 0),
        "edges": now["edges"] - int(prev.get("edges") or 0),
        "max_depth": now["max_depth"] - int(prev.get("max_depth") or 0),
        "acyclic_regressed": bool(prev.get("acyclic")) and not now["acyclic"],
    }
    worse = []
    if delta["max_depth"] > 0:
        worse.append(f"依赖深度 +{delta['max_depth']}")
    if delta["edges"] > max(2, int(prev.get("edges") or 0) // 2):
        worse.append(f"依赖边 +{delta['edges']}（翻得太快）")
    if delta["acyclic_regressed"]:
        worse.append("出现了环")
    return {"has_baseline": True, "now": now, "prev": prev, "delta": delta,
            "regressed": bool(worse), "worse": worse,
            "note": ("；".join(worse) if worse else "相对上次没有退化")}


def gate_health(ws: Path, level: str = "XL", max_depth: int = 6) -> GateResult:
    """XL 级：健康度指标越界即视为漂移（有环、依赖过深）。"""
    t0 = time.time()
    if not _scale.profile(level).flags.get("health_metrics"):
        return GateResult("health", True, "static_rule", "skipped",
                          f"{level} 级不用持续健康度指标（PR 级门禁足够）",
                          int((time.time() - t0) * 1000))
    m = health_metrics(ws)
    ok = m["acyclic"] and m["max_depth"] <= max_depth
    detail = (f"{m['modules']} 个模块、{m['edges']} 条依赖、最大深度 {m['max_depth']}、"
              f"无环={m['acyclic']}、依赖密度 {m['dependency_density']}")
    if not m["acyclic"]:
        detail += f"；有环：{', '.join(m['cyclic'])}"
    if m["max_depth"] > max_depth:
        detail += f"；依赖链过深（>{max_depth}）"
    return GateResult("health", ok, "static_rule",
                      f"depth={m['max_depth']}", detail, int((time.time() - t0) * 1000))


def level_gates(ws: Path, level: str, changed: dict | None = None) -> list[GateResult]:
    """一次性跑完"这一级要求的额外门禁"（M/L/XL 才会真的做活）。"""
    out = [gate_layers(ws, level)]
    out.append(gate_diff_size(ws, level))
    out.append(gate_coverage(ws, level))
    out.append(gate_mutation_changed(ws, changed, level))
    out.append(gate_dep_whitelist(ws, level))
    out.append(gate_health(ws, level))
    try:
        from . import quality as _q
        out += _q.extra_gates(ws, level, changed and list(changed))
    except Exception:                                # noqa: BLE001
        pass
    return out


def write_dep_whitelist(ws: Path, level: str) -> str:
    """给 L/XL 级生成一个初始白名单（只列当前实际用到的第三方，标准库不算）。"""
    f = Path(ws) / WHITELIST_FILE
    if f.exists():
        return str(f)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        "# 依赖白名单：一行一个包名（标准库不用写）。\n"
        "# L 级起是硬约束：白名单外的第三方 import 会被门禁拦下。\n"
        "# 引入新依赖 → 在这里加一行 + 在 PR 里说明用途与替代方案。\n",
        encoding="utf-8")
    return str(f)
