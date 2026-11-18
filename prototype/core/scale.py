"""代码规模分级（S/M/L/XL）：约束的固定成本必须被该规模下拦住的问题覆盖。

为什么需要分级：**同一套门禁在 1 万行项目上是过度工程化，在 100 万行项目上等于没有约束。**
分级的关键不是"加什么"，而是"知道什么时候不加什么"。

四级（依据是"约束的固定成本何时被规模收益覆盖"，不是行数本身好看）：
  S  < 10 万行    小型项目 / 单一团队      → 只要"不让基本错误进仓库"，架构靠人审
  M  10–50 万行   多模块生产系统        → 架构守护从可选变必选（ArchUnit / import-linter）
  L  50–100 万行  多团队 / 受控自主     → 用可观测性替代逐行审查；变更行变异测试
  XL > 100 万行   从零构建/长期演进     → 没有"完全自主"的实证；人类必须保留架构 owner

甲方模式的要求：**提需求时就要写明目标规模**。没写就按实测推断，并在交付报告里
明确写"我按 X 级跑的、依据是什么" —— 不许悄悄选一个。

证据锚点（来自公开实践，写在这里是为了让每一次分级决定都可追溯）：
  · 15,000 行时生产库在无自动化强制下接近崩溃（测试全绿、架构已碎）→ 小规模也有结构风险，
    只是风险可由人审覆盖；
  · 150,000 行实施架构强制执行测试后无可测量漂移，调试工作量约 -15%；
  · 100 万行 / 1,500 PR / 0 行人工编写可行，但架构骨架由人类设计、定位是内部 Beta；
  · 超过 100 万行从零生成并直接上线：**没有任何公开可验证的案例**。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

# ------------------------------------------------------------------ 级别定义
TIER_ORDER = ("S", "M", "L", "XL")


@dataclass(frozen=True)
class Tier:
    id: str
    label: str               # 人看的范围
    max_lines: int           # 上界（XL 用 0 表示无穷）
    judgement: str           # 这一级的核心判断（一句话）
    agents_md: str           # 常驻规则文件的规模与形态
    architecture: str        # 架构守护手段
    ci: tuple[str, ...]      # 门禁清单
    review: str              # 评审方式
    context: str             # 上下文工程
    entropy: str             # 熵管理
    upgrade: str             # 升级触发条件
    # 机器可用的开关（由 gates / 夜班消费）
    flags: dict = field(default_factory=dict)


TIERS: dict[str, Tier] = {
    "S": Tier(
        id="S", label="< 10 万行（小型项目 / 单一团队）", max_lines=100_000,
        judgement="不需要架构治理，需要的是不让基本错误进入仓库",
        agents_md="一份，50–100 行；只写命令/目录职责/硬性禁令/关键 don't-do",
        architecture="不启用机械化架构测试（边界由人审口头维护）",
        ci=("build", "typecheck", "lint", "unit_test", "no_test_skip", "secrets"),
        review="单 PR 单主题；人类逐 PR 审查；不设 diff 行数上限",
        context="根文件足够，不做工作集声明",
        entropy="人工定期清理（不自动删重复代码：测试覆盖不足时易误删）",
        upgrade="> 8–10 万行；模块 > 5–8 个；出现第二个长期贡献者；"
                "事故开始与跨模块边界穿透相关",
        flags={"architecture_gate": False, "coverage_floor": None, "diff_soft_cap": None,
               "mutation": False, "secrets": True, "dep_whitelist": False,
               "protected_zones": False, "health_metrics": False, "auto_merge_cleanup": False,
               "workingset_declaration": False},
    ),
    "M": Tier(
        id="M", label="10–50 万行（中型生产系统 / 多模块）", max_lines=500_000,
        judgement="架构守护从可选变为必选：用机械手段把漂移压住",
        agents_md="根 100–150 行（当目录用）+ 子目录 AGENTS.md + 领域指南 ≤300 行/篇",
        architecture="依赖方向检查（ArchUnit / dependency-cruiser / import-linter），<15s 完成",
        ci=("build", "typecheck", "lint", "unit_test", "no_test_skip", "secrets",
            "architecture", "coverage_80"),
        review="单主题 + 架构检查由机器拦；人工仍是主要信任来源；单 PR 软上限 500 行",
        context="工作集声明：动手前列出要读的文件/依赖接口/相关测试/明确的非目标；"
                "改完检查反向依赖，超出工作集要停下报告",
        entropy="轻量后台扫描（重复代码/过期文档），清理 PR 必须人工合并",
        upgrade="> 50 万行；模块 > 15–20 个；生成速度开始超过人工审查吞吐；"
                "出现跨团队公共 API 契约",
        flags={"architecture_gate": True, "coverage_floor": 0.80, "diff_soft_cap": 500,
               "mutation": False, "secrets": True, "dep_whitelist": False,
               "protected_zones": False, "health_metrics": False, "auto_merge_cleanup": False,
               "workingset_declaration": True},
    ),
    "L": Tier(
        id="L", label="50–100 万行（大型系统 / 多团队 / 受控自主）", max_lines=1_000_000,
        judgement="用可观测性和自动化替代逐行审查",
        agents_md="严格 ~100 行，只做渐进式披露入口；配 Doc-gardening 定期清理过时文档",
        architecture="强制分层 + 自定义 Lint；报错信息内嵌修复指令（边纠错边教）",
        ci=("build", "typecheck", "lint", "unit_test", "no_test_skip", "secrets",
            "architecture", "coverage_80", "mutation_changed_lines_70",
            "security_scan", "dep_whitelist", "protected_zones"),
        review="人类设边界 + 检查聚合指标（可不逐行读 diff，但必须能看见一切）",
        context="日志/指标/链路追踪直接暴露给 Agent，性能目标变成它自己能量测的验收条件",
        entropy="后台 Agent 自动扫描 + 自动提交清理 PR，多数 1 分钟内审完自动合并；"
                "生成速度必须与清理速度匹配",
        upgrade="> 100 万行；需要从零生成需长期自主演进的新系统；"
                "架构骨架本身需要由 Agent 提出",
        flags={"architecture_gate": True, "coverage_floor": 0.80, "diff_soft_cap": 500,
               "mutation": True, "mutation_min": 0.70, "secrets": True, "dep_whitelist": True,
               "protected_zones": True, "health_metrics": False, "auto_merge_cleanup": True,
               "workingset_declaration": True, "security_scan": True},
    ),
    "XL": Tier(
        id="XL", label="> 100 万行（超大规模 / 从零构建与长期演进）", max_lines=0,
        judgement="没有公开可验证的『从零生成直接上线』案例：人类必须保留架构 owner",
        agents_md="~100 行目录 + 按需加载；治理三文件与代码分离，Agent 不得修改治理文件",
        architecture="持续架构健康度指标（模块度/无环性/深度/冗余）+ 漂移检测，"
                     "门禁从『合并前』扩展到『每次修改后即时反馈』",
        ci=("build", "typecheck", "lint", "unit_test", "no_test_skip", "secrets",
            "architecture", "coverage_80", "mutation_changed_lines_70",
            "security_scan", "dep_whitelist", "protected_zones", "continuity_health"),
        review="同 L，但核心契约（分层方向/公共签名/数据 schema）不可变，人类担任架构 owner",
        context="只守少数系统级不变量 + 持续漂移检测；不试图约束 Agent 的每个行为",
        entropy="持续指标反馈 + 人类干预极端 case",
        upgrade="无（这一级已经超出当前可验证边界：诚实做法是缩小目标或增加人类介入）",
        flags={"architecture_gate": True, "coverage_floor": 0.80, "diff_soft_cap": 500,
               "mutation": True, "mutation_min": 0.70, "secrets": True, "dep_whitelist": True,
               "protected_zones": True, "health_metrics": True, "auto_merge_cleanup": True,
               "workingset_declaration": True, "security_scan": True},
    ),
}

# 每级"明确不做"的事 —— 分级的关键在于知道什么时候不加什么
SKIPPED: dict[str, tuple[str, ...]] = {
    "S": ("架构测试（ArchUnit/dependency-cruiser）", "覆盖率地板", "变更行变异测试",
          "依赖白名单", "受保护区自动拒绝", "持续架构健康度指标", "自动合并清理 PR",
          "工作集声明", "子目录 AGENTS.md / docs 知识库"),
    "M": ("变更行变异测试", "依赖白名单", "受保护区自动拒绝", "持续架构健康度指标",
          "自动合并清理 PR（清理 PR 必须人工合并）"),
    "L": ("持续架构健康度指标（仍用 PR 级门禁）",),
    "XL": (),
}


def profile(level: str) -> Tier:
    return TIERS[str(level).upper()]


# ------------------------------------------------------------------ 规模测量
_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache"}


def count_lines(ws: Path, tests: bool = True) -> dict:
    """量代码规模：生产代码行数、测试行数、**模块数**与文件数（确定性，无第三方）。

    「模块数」的口径是**真的模块**：一个非 `__init__.py` 的生产文件算一个模块。
    旧实现数的是 `rel.parent`（= 目录数），于是 `cal/core/numeric.py` 这种**包内模块
    一个都不算** —— 实测一个 90 文件 / 80 模块的仓库只被数出 `modules=10`
    （8 个子包目录 + 根 + `.`），而模块数是 S/M 档"该不该升档"的判据之一，
    数错会直接把规模判断带偏。
    """
    prod_lines = test_lines = files = 0
    modules: set[str] = set()
    # 2026-11-09 修：**在目录层面就跳过**，不要"遍历进去再事后过滤"。
    # 原来用 `Path.rglob("*.py")` + 事后判 `_SKIP_DIRS` —— 但 rglob 会**先进去**：
    # 毕设产物仓库的 .sixcoder/tmp 里堆了 36754 个探针沙箱目录（210MB），
    # rglob 全扫一遍要 19 秒，而 count_lines 在夜班里被反复调用（每轮 inspect 都调），
    # 实测夜班卡在这里 1.5 小时、CPU 零产出。
    # 改成 os.walk + 顶层剪枝：命中的目录**根本不进去**，遍历量从 15553 降到 ~105。
    for root, dirs, files_in in os.walk(ws):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files_in:
            if not fname.endswith(".py"):
                continue
            p = Path(root) / fname
            rel = p.relative_to(ws)
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            n = sum(1 for ln in text.splitlines() if ln.strip())
            name = fname
            is_test = name.startswith("test_") or name.endswith("_test.py") or "tests" in rel.parts
            if is_test:
                test_lines += n
                if not tests:
                    continue
            else:
                prod_lines += n
                if name != "__init__.py":          # 包的定义文件不是一个"模块"
                    modules.add(str(rel.with_suffix("")).replace("\\", "/"))
            files += 1
    return {"prod_lines": prod_lines, "test_lines": test_lines, "files": files,
            "modules": len(modules)}


def from_lines(total: int) -> str:
    """按行数定级（M/L 的边界取 10 万 / 50 万 / 100 万）。"""
    if total < 100_000:
        return "S"
    if total < 500_000:
        return "M"
    if total < 1_000_000:
        return "L"
    return "XL"


# ------------------------------------------------------------------ 需求里声明的规模
_DECL = re.compile(
    r"(?:代码规模|目标规模|规模|scale)\s*[:：]?\s*(?P<s>S|M|L|XL|s|m|l|xl)\s*(?:级|档|class)?",
    re.I)
_DECL_LOOSE = re.compile(r"^\s*(?:@scale)\s*[:：]?\s*(?P<s>S|M|L|XL)\s*$", re.I | re.M)

# 2026-11-09 修（用户真实需求暴露）：甲方常用**行数**声明规模，例如
#   「目标代码规模：**30000 行**（10000–50000 行级别）」
# 原来只认字母档位 → declared 读不到 → 回落到"按仓库实测推断"，
# 而仓库实测是**当前进度**（5710 行），不是**目标规模** → 依据错了、报告也会写错。
# 现在也认行数声明：按行数落到对应档位（与 from_lines 同一套边界）。
_DECL_LINES = re.compile(
    r"(?:代码规模|目标规模|目标代码规模|规模)\s*[:：]?\s*[*_\s]*(?P<n>\d{3,7})\s*[*_\s]*行"
    r"|(?P<n2>\d{3,7})\s*行\s*(?:规模|代码)", re.I)


def declared_from_requirement(requirement: str) -> str:
    """从需求原文里读"甲方声明的目标规模"（S/M/L/XL）；没写返回空串。

    认两种写法：
      · 字母档位：『代码规模：M』『@scale L』
      · 行数声明：『目标代码规模：30000 行』『30000 行级别』→ 按行数落档
    """
    text = requirement or ""
    m = _DECL_LOOSE.search(text) or _DECL.search(text)
    if m:
        s = m.group("s").upper()
        return s if s in TIERS else ""
    lm = _DECL_LINES.search(text)
    if lm:
        n = int(lm.group("n") or lm.group("n2"))
        if n > 0:
            return from_lines(n)
    return ""


def resolve(requirement: str, ws: Path | None = None) -> dict:
    """决定这一次按哪一级的约束跑。返回 {level, source, measured, warning, ...}。

    source:
      · "requirement" 甲方在需求里写明了（**唯一不需要警告的情况**）
      · "measured"    没写，按仓库实测规模推断（要在交付报告里写明）
    """
    declared = declared_from_requirement(requirement)
    measured = count_lines(ws) if ws is not None else {"prod_lines": 0, "test_lines": 0,
                                                       "files": 0, "modules": 0}
    total = measured["prod_lines"] + measured["test_lines"]
    inferred = from_lines(total)
    declared_lines = _declared_lines(requirement)
    if declared:
        return {"level": declared, "source": "requirement", "declared": declared,
                "declared_lines": declared_lines,
                "inferred": inferred, "measured": measured, "total_lines": total,
                "warning": ""}
    warn = ("需求里没写目标代码规模，按仓库实测（%d 行）推断为 %s 级。"
            "不同规模的约束差别很大，建议在需求里加一行『代码规模：S/M/L/XL』" % (total, inferred))
    return {"level": inferred, "source": "measured", "declared": "", "declared_lines": 0,
            "inferred": inferred, "measured": measured, "total_lines": total,
            "warning": warn}


def _declared_lines(requirement: str) -> int:
    """甲方声明的**目标行数**（0 = 没声明）。用于交付报告写明依据。"""
    m = _DECL_LINES.search(requirement or "")
    if not m:
        return 0
    return int(m.group("n") or m.group("n2") or 0)


# ------------------------------------------------------------------ 给门禁/报告用
def gate_plan(level: str) -> dict:
    """这一级要跑哪些门禁、明确不跑哪些（以及为什么）。"""
    t = profile(level)
    f = t.flags
    run: list[str] = []
    skip: list[tuple[str, str]] = []
    run += ["syntax", "signature", "anti_cheat", "stub", "duplicates", "undef", "import_ok",
            "structure"]
    if f.get("secrets"):
        run.append("security")
    run.append("deps")
    if f.get("architecture_gate"):
        run.append("architecture")
    else:
        skip.append(("architecture", "S 级：架构边界由人类审查维护，机械化测试的成本高于收益"))
    if f.get("coverage_floor"):
        run.append("coverage")
    else:
        skip.append(("coverage", "S 级：不设覆盖率地板（小程序覆盖不足时硬压会诱导写垃圾测试）"))
    if f.get("mutation"):
        run.append("mutation")
    else:
        skip.append(("mutation", f"{level} 级：变异测试成本高于它揭示的问题，"
                                 "L 级起只对变更行跑"))
    if f.get("dep_whitelist"):
        run.append("dep_whitelist")
    else:
        skip.append(("dep_whitelist", "白名单在 M 级以下只有维护成本，改为 PR 说明理由 + 人工确认"))
    if f.get("protected_zones"):
        run.append("protected_zones")
    else:
        skip.append(("protected_zones", "受保护区自动拒绝从 L 级起启用"))
    if f.get("health_metrics"):
        run.append("continuity_health")
    return {"level": t.id, "run": run, "skip": skip, "flags": dict(f)}


def quality_limits(level: str) -> dict:
    """这一级的可维护性阈值。**规模越小越松，规模越大越紧**。

    为什么不能一刀切 8/3/40：实测（night-v2 系列）`normalize_age()` 是一个"边界校验簇"，
    圈复杂度天然就是 9，被阈值 8 判红 → 整模块重写，那一次花费 ¥0.5031。
    在 S 级（小型项目、无人审查流水线）这种复杂度完全可接受；到 L 级（多团队）才该按 8 卡。
    """
    lv = str(level).upper()
    return {
        "S": {"complexity": 12, "depth": 4, "lines": 60},
        "M": {"complexity": 10, "depth": 4, "lines": 50},
        "L": {"complexity": 8, "depth": 3, "lines": 40},
        "XL": {"complexity": 8, "depth": 3, "lines": 40},
    }.get(lv, {"complexity": 12, "depth": 4, "lines": 60})


def write_per_tier(level: str) -> int:
    """同一模块、同一档允许重写的次数。**规模越大越少**：每次重写在大项目上都是审查负担。"""
    lv = str(level).upper()
    return {"S": 3, "M": 2, "L": 1, "XL": 1}.get(lv, 3)


def default_hours(level: str) -> float:
    """这一级的默认时间预算（甲方不给的时候用）。"""
    lv = str(level).upper()
    return {"S": 0.5, "M": 2.0, "L": 6.0, "XL": 12.0}.get(lv, 0.5)


def gate_mode(gate: str, level: str) -> str:
    """某个门禁在这一级该怎么用：block（不合格就阻断）/ advisory（只报告）/ skip（不做）。

    为什么不是"全开或全关"：同一个门禁在不同规模上的**正确用法**不一样。
    例：变异测试在 S 级做都不做；M 级跑了也只作参考（覆盖率仍有效）；
    到 L 级才用它阻断，因为那时覆盖率单独已经不足以证明测试有效。
    """
    lv = str(level).upper()
    if lv not in TIERS:
        lv = "S"
    f = TIERS[lv].flags
    if gate == "mutation":
        if not f.get("mutation"):
            return "skip" if lv == "S" else "advisory"
        return "block"
    if gate == "coverage":
        return "block" if f.get("coverage_floor") else "skip"
    if gate == "architecture":
        return "block" if f.get("architecture_gate") else "skip"
    if gate == "dep_whitelist":
        return "block" if f.get("dep_whitelist") else "advisory"
    if gate == "protected_zones":
        return "block" if f.get("protected_zones") else "advisory"
    if gate == "deps":
        # 依赖可复现性：S/M 级只报告（人工确认）；L 级起有白名单，硬约束
        return f.get("dep_whitelist") and "block" or "advisory"
    if gate == "workingset":
        # 工作集声明：M 级起是硬要求（改之前先说清范围，超出就停）
        return "block" if f.get("workingset_declaration") else "skip"
    if gate in ("secrets", "security"):
        return "block"
    return "block"


# 受保护区：这一级的"碰了就拒绝"清单（L 级起生效）
PROTECTED_PATTERNS: tuple[str, ...] = (
    "**/auth*", "**/*auth*.py", "**/*security*.py", "**/config/deploy*",
    "**/*.ci.yml", "**/.github/workflows/*", "**/AGENTS.md", "**/mission.md",
    "**/factory-rules.md", "**/docs/2[0-9]*-*.md",
)


def protected_hits(ws: Path, paths: list[str]) -> list[str]:
    """这些路径里哪些落在受保护区（只在 L 级以上阻断）。"""
    out: list[str] = []
    for rel in paths or []:
        low = str(rel).replace("\\", "/").lower()
        for pat in PROTECTED_PATTERNS:
            import fnmatch
            if fnmatch.fnmatch(low, pat.replace("**/", "")) or fnmatch.fnmatch(low, pat):
                out.append(str(rel))
                break
    return sorted(set(out))


def guard_write(ws: Path, rel: str, level: str | None = None) -> tuple[bool, str]:
    """远端工具写盘前的统一守卫（§13.4 第 4 步：受保护区对远端只读）。

    # 依据：开发文档 §13.4 第 4 步 + §11.2 L2——测试/契约/治理文件对远端工具必须只读，
    # 否则「删测试」是最省力的变绿方式。PROTECTED_PATTERNS（auth/security/deploy/AGENTS.md/
    # docs/2xxx 治理文档）命中即拒，杜绝远端绕过本地验收改判据。
    返回 (allowed, reason)。命中受保护区时：本级 block 则拒绝，advisory 只告警放行。
    level 缺省按工作区实测规模取（L 级以上才真正 block）。

    为什么是「写路径」独自守卫：company._author_round 已经在模块级拦过一次，
    但远端工具循环（remotetools）是另一条独立写路径，必须自己再查一遍，
    不能假设下游已检查（实测：tools.write_file / author 写盘处都没查 protected_hits）。
    """
    if level is None:
        try:
            level = resolve("", Path(ws))["level"]
        except Exception:                        # noqa: BLE001
            level = "S"
    mode = gate_mode("protected_zones", level)
    hits = protected_hits(ws, [rel])
    if hits:
        if mode == "block":
            return False, f"受保护区：{hits[0]} 不允许自动修改（{level} 级 block）"
        return True, f"受保护区告警（{hits[0]}，但本级 {level} 为 advisory，放行）"
    return True, ""


def is_protected(rel: str) -> bool:
    """仅按模式判断一个相对路径是否落在受保护区（不依赖工作区级别）。"""
    return bool(protected_hits(None, [rel]))


def upgrade_hint(measured: dict, level: str) -> dict:
    """升级触发检测：**该不该换档**由机械规则判，不靠人记。

    分级表每一级都写了"升级触发条件"，但之前只有定级、没有提示 ——
    于是"什么时候该从 S 升到 M"只能靠人想起来。这里把它变成每次开跑都会算的一句话。

    能机械判的（行数、模块目录数）；不能机械判的（"第二个长期贡献者"、
    "事故与跨模块边界穿透相关"）如实不猜，不写进结果。
    """
    lv = str(level).upper()
    total = int(measured.get("prod_lines") or 0) + int(measured.get("test_lines") or 0)
    mods = int(measured.get("modules") or 0)
    reasons: list[str] = []
    target = ""
    if lv == "S":
        target = "M"
        if total > 100_000:
            reasons.append(f"代码 {total} 行 > 10 万")
        if mods > 8:
            reasons.append(f"模块目录 {mods} 个 > 8")
    elif lv == "M":
        target = "L"
        if total > 500_000:
            reasons.append(f"代码 {total} 行 > 50 万")
        if mods > 20:
            reasons.append(f"模块目录 {mods} 个 > 20")
    elif lv == "L":
        target = "XL"
        if total > 1_000_000:
            reasons.append(f"代码 {total} 行 > 100 万")
    if not reasons:
        return {"should_upgrade": False, "from": lv, "to": "", "reasons": [],
                "note": f"{lv} 级的机械触发条件均未达到"}
    return {"should_upgrade": True, "from": lv, "to": target, "reasons": reasons,
            "note": (f"已达 {lv}→{target} 的升级触发条件（{'；'.join(reasons)}）："
                     f"下一夜建议按 {target} 级跑")}


def report_lines(level: str, resolved: dict | None = None) -> list[str]:
    """交付报告里的「规模与约束等级」小节（照实写：跑了什么、没跑什么、为什么）。"""
    t = profile(level)
    p = gate_plan(level)
    lines = ["", "## 规模与约束等级", "",
             f"- 按 **{t.id} 级**（{t.label}）执行",
             f"- 这一级的核心判断：{t.judgement}"]
    if resolved:
        src = "甲方在需求里写明" if resolved.get("source") == "requirement" \
            else "需求未写明，按仓库实测推断"
        lines.append(f"- 定级依据：{src}"
                     f"（实测 生产 {resolved['measured']['prod_lines']} 行 / "
                     f"测试 {resolved['measured']['test_lines']} 行 / "
                     f"{resolved['measured']['modules']} 个模块目录）")
        if resolved.get("warning"):
            lines.append(f"- **注意**：{resolved['warning']}")
    lines.append(f"- 常驻规则：{t.agents_md}")
    lines.append(f"- 架构守护：{t.architecture}")
    lines.append(f"- 评审方式：{t.review}")
    lines.append(f"- 上下文：{t.context}")
    lines.append(f"- 熵管理：{t.entropy}")
    lines.append(f"- 升级触发：{t.upgrade}")
    if resolved:
        try:
            uh = upgrade_hint(resolved.get("measured") or {}, t.id)
            if uh["should_upgrade"]:
                lines.append(f"- **⚠️ 该升级了**：{uh['note']}")
        except Exception:                                # noqa: BLE001
            pass
    lines.append("")
    lines.append("| 门禁 | 本级是否执行 | 说明 |")
    lines.append("|---|---|---|")
    for name in p["run"]:
        lines.append(f"| {name} | ✅ 执行 | — |")
    for name, why in p["skip"]:
        lines.append(f"| {name} | ⛔ 不执行 | {why} |")
    return lines
