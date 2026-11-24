"""生成骨架计划（LayerPlan）—— P0：把"生成什么结构"也变成可插拔。

## 解决什么

判据层已经可插拔了（core/judges.py：19 条判据按栈分发）。
但**生成骨架还是硬编码的**：

    # fullgen.py 原来
    LAYERS = (
        ("models",   ("app/models.py", "app/schemas.py")),
        ("services", None),
        ("api",      None),
    )

外加 _layer_targets 里一串 if is_django / if is_frontend ——
那些分支其实**已经说明了"层由栈决定"**，只是写散在函数里。

后果：加一个 CLI 判据包也没用，因为引擎不知道 CLI 项目该生成什么文件。
这就是"判据能通用了，生成还不能通用"的那道坎。

## 这一层做什么

把"某个栈要分几层、每层写哪些文件"变成一个接口：

    class LayerPlan(Protocol):
        stack: str                          # 对哪个栈生效
        layers: tuple[tuple[str, ...], ...] # 层名序列（依赖顺序）
        def targets(ctx, layer) -> list[str]  # 这一层的落点文件

新增一种形态 = 写一个 LayerPlan，不用改 fullgen。

## 兼容策略（回归铁律）

P0 **不改任何现有生成行为**。做法：
  · 把现在 _layer_targets 里的 if 分支逐字搬进三个 LayerPlan
  · fullgen 改成从注册表取方案，取不到时**回退到原来的硬编码**
  · checks/check_fullgen.py 与 pytest 钉住"层名与落点一个字节没变"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class LayerContext:
    """生成计划能看到的上下文。与 JudgeContext 同一取舍：刻意窄。"""
    ws: Path
    blueprint: Any = None       # 蓝图对象（可能是 None：还没拆解）
    emit: Any = None

    def module_paths(self) -> list[str]:
        """蓝图里声明的模块落点（去掉 descoped 的能力）。

        这是三个 LayerPlan 共用的取数逻辑 —— 放在上下文里，
        避免每个方案各写一份（写岔了会出现"某个栈少生成文件"的假象）。
        """
        bp = self.blueprint
        if bp is None:
            return []
        out = set()
        for c in (getattr(bp, "caps", None) or []):
            if getattr(c, "status", "") == "descoped":
                continue
            for m in (getattr(c, "modules", None) or []):
                out.add(str(m))
        return sorted(out)


@runtime_checkable
class LayerPlan(Protocol):
    """一种软件形态的生成计划。"""
    stack: str
    layers: tuple

    def targets(self, ctx: LayerContext, layer: str) -> list[str]:
        ...


@dataclass
class SimplePlan:
    """通用的 LayerPlan 实现：层名固定，落点由一个函数算。

    大多数栈都能用它 —— 只有"落点怎么算"不同，不需要各写一个类。
    """
    stack: str
    layers: tuple
    resolver: Any                     # (ctx, layer) -> list[str]
    description: str = ""

    def targets(self, ctx: LayerContext, layer: str) -> list[str]:
        return list(self.resolver(ctx, layer) or [])


# ── 三个内置方案：逐字搬自 fullgen._layer_targets 的既有分支 ──────────

def _fastapi_targets(ctx: LayerContext, layer: str) -> list[str]:
    """FastAPI：models 用固定文件，services/api 扫目录。

    与旧实现逐字一致（含"文件不存在就返回空"的语义）。
    """
    ws = ctx.ws
    if layer == "models":
        return [p for p in ("app/models.py", "app/schemas.py") if (ws / p).is_file()]
    sub = "services" if layer == "services" else "api"
    d = ws / "app" / sub
    if not d.is_dir():
        return []
    return [str(p.relative_to(ws)).replace("\\", "/") for p in sorted(d.glob("*.py"))]


def _django_targets(ctx: LayerContext, layer: str) -> list[str]:
    """Django：数据层已建好不重写；业务层一次性写 apps/ 下所有 .py。"""
    if layer == "models":
        return []                  # 重生成会把已有模型和迁移冲掉
    if layer != "services":
        return []                  # Django 没有 services/api 两批之分
    return [m for m in ctx.module_paths()
            if m.startswith(("apps/", "matching_core/")) and m.endswith(".py")]


def _frontend_targets(ctx: LayerContext, layer: str) -> list[str]:
    """前端 SPA / Electron：按蓝图里声明的组件文件写，不碰 Python 分层。"""
    if layer != "ui":
        return []
    return [m for m in ctx.module_paths()
            if m.endswith((".jsx", ".tsx", ".vue", ".js", ".ts"))]


def _cli_targets(ctx: LayerContext, layer: str) -> list[str]:
    """CLI 工具：单层，按蓝图声明的 .py 落点写。

    ## 与生态包 cli_tool 的关系（2026-11-24 合并）

    这个计划不自己定义入口约定，而是**对齐生态包 cli_tool**：
      · 入口固定 cli.py（生态包的 detect_markers 只认这个文件）
      · 子命令模块放 commands/（生态包 adapter 的 module_file_fmt）
      · 骨架来自 cli_tool/templates/（engine 用 ecosystem.apply_templates 复制）
      · 验收走生态包的 cli 探针（真跑 cli.py + 断言 stdout/退出码）

    为什么"对齐"而不是各写一套：早先这里用的是 main.py，
    与生态包的 cli.py 冲突 —— 生成的 CLI 项目永远不被识别成 CLI 项目。
    合并后只有一个入口名、一套判据。
    """
    if layer != "cli":
        return []
    entry = "cli.py"
    out = []
    for m in ctx.module_paths():
        if not m.endswith(".py"):
            continue
        if m == entry:
            continue          # 入口由骨架生成，不让远端写
        out.append(m)
    return out


FASTAPI_PLAN = SimplePlan(
    stack="fastapi",
    layers=(("models",), ("services",), ("api",)),
    resolver=_fastapi_targets,
    description="models -> services -> api 三层（FastAPI 默认）",
)

DJANGO_PLAN = SimplePlan(
    stack="django",
    layers=(("models",), ("services",), ()),
    resolver=_django_targets,
    description="Django：models 跳过（已建好），业务层一次写完",
)

FRONTEND_PLAN = SimplePlan(
    stack="frontend",
    layers=(("ui",),),
    resolver=_frontend_targets,
    description="前端 SPA/Electron：只写组件层",
)

CLI_PLAN = SimplePlan(
    stack="cli",
    layers=(("cli",),),
    resolver=_cli_targets,
    description="CLI 工具：单层，入口 cli.py（生态包 cli_tool 的骨架 + 探针）",
)


# ── 注册表 ─────────────────────────────────────────────

class PlanRegistry:
    """按栈取生成计划。取不到返回 None（调用方回退到旧硬编码，保证行为不变）。"""

    def __init__(self) -> None:
        self._plans: dict[str, LayerPlan] = {}

    def register(self, plan: LayerPlan) -> None:
        if not getattr(plan, "stack", ""):
            raise ValueError("生成计划必须有 stack")
        if plan.stack in self._plans:
            raise ValueError(f"生成计划 stack 重复：{plan.stack}")
        self._plans[plan.stack] = plan

    def get(self, stack: str) -> Optional[LayerPlan]:
        return self._plans.get(str(stack or "").lower())

    def all(self) -> list[LayerPlan]:
        return [self._plans[k] for k in sorted(self._plans)]


_default: Optional[PlanRegistry] = None


def ecosystem_plan(name: str, description: str = "") -> Optional[SimplePlan]:
    """给"有骨架模板的生态包"生成一个通用计划（2026-11-24 打通）。

    ## 为什么用通用计划而不是每个包写一个

    生态包的骨架已经在 templates/ 里声明好了（cli_tool / electron /
    frontend_spa 都有）。生成侧只需要知道两件事：
      1. 有几层 —— 这类项目都是**单层**（骨架给齐结构，业务写模块）
      2. 落点文件 —— 蓝图声明的 .js/.py 模块

    所以不必为每个包手写 targets 函数，一个通用实现就够。
    这也避免了"加一个生态包要同时改 layerplan"的负担。

    ## 只给有 templates 的包

    没有骨架模板的包（go_service / microservice / node_express /
    py_lib / spring_boot）返回 None —— 让它们回退到旧逻辑，
    避免"没有骨架却声称能生成"。
    """
    def _targets(ctx, layer):
        # 层名语义（2026-11-24 二次修正，被 check_ecosystem 抓出来的回归）：
        #
        # · **组件类栈**（electron / frontend_spa）：**不区分层名**。
        #   旧实现 fullgen._layer_targets 的前端分支就是这样 ——
        #   无论问 services 还是 models，都返回蓝图里的 .jsx/.tsx/.vue。
        #   我在 P0 把它改成"只认 modules"，直接让 check_ecosystem 的
        #   "electron 落点走组件分支"失败。
        #   为什么旧行为是对的：前端根本没有 models/services/api 这套分层，
        #   用层名去筛只会全部筛空。
        #
        # · 其他生态包：只认本计划声明的 modules 层。
        try:
            from . import ecosystem as _eco2
            _ad, _ = _eco2.load_adapter(name)
        except Exception:                            # noqa: BLE001
            _ad = None
        _is_component = str(getattr(_ad, "module_file_fmt", "") or "").endswith(
            (".jsx", ".tsx", ".vue"))
        if not _is_component and layer != "modules":
            return []
        # 单层：把蓝图声明的业务模块都算进来，骨架文件由生态包负责
        try:
            from . import ecosystem as _eco
            tpl_dir = _eco.pack_dir(name) / "templates"
        except Exception:                            # noqa: BLE001
            tpl_dir = None
        # 骨架文件清单（相对包 templates/ 的路径）。
        #
        # **只用来排除"和骨架完全同路径"的文件**（如 src/main.jsx、src/App.jsx）。
        # 不能只比文件名 —— 实测（check_ecosystem）：业务页面 src/pages/Home.jsx
        # 与骨架模板 templates/src/pages/Home.jsx 同名，按文件名排除会把它
        # 一起排掉，导致"该写的前端页面一个都不写"。
        skeleton = set()
        if tpl_dir is not None and tpl_dir.is_dir():
            for f in tpl_dir.rglob("*"):
                if f.is_file():
                    skeleton.add(f.relative_to(tpl_dir).as_posix())
        # 骨架文件分两类处理（2026-11-24：被三处测试/自检来回夹出来的规则）：
        #
        #   A. **入口/装配文件**（main.jsx / App.jsx / main.py / cli.py /
        #      vite.config.js / package.json / index.html …）——**永远不让远端写**。
        #      它们是引擎的装配产物，远端改了会和骨架约定冲突
        #      （实测：远端若改 cli.py 的子命令分发，退出码约定就失守）。
        #
        #   B. **起步占位页**（如 src/pages/Home.jsx）——蓝图显式声明了就放行。
        #      这类文件只是"给你一个能跑的空页面"，业务要做首页时当然要覆盖它。
        #
        # 区分办法：A 类按文件名（叶子名）判断，与目录无关；
        # B 类按完整相对路径判断。
        _ENTRY_NAMES = {
            "main.py", "cli.py", "app.py", "__main__.py",
            "main.jsx", "main.tsx", "main.js", "main.ts",
            "App.jsx", "App.tsx", "App.vue",
            "vite.config.js", "vite.config.ts", "vite.config.mjs",
            "index.html", "package.json", "package-lock.json",
            "main.cjs", "preload.cjs", "electron-builder.json",
        }
        entry_files = {rel for rel in skeleton
                       if rel.rsplit("/", 1)[-1] in _ENTRY_NAMES}
        declared = set(ctx.module_paths())

        out = []
        for m in ctx.module_paths():
            rel = str(m or "").strip()
            if not rel:
                continue
            if rel in entry_files:
                continue                             # A 类：入口永远由骨架提供
            if rel in skeleton and rel not in declared:
                continue                             # B 类：没被声明就不动它
            if _is_component and not rel.endswith(
                    (".jsx", ".tsx", ".vue", ".js", ".ts")):
                continue                             # 组件类栈只写组件文件
            out.append(rel)
        return out

    return SimplePlan(
        stack=name,
        layers=(("modules",),),
        resolver=_targets,
        description=description or f"{name}：单层（骨架来自生态包 templates/）",
    )


def default_registry() -> PlanRegistry:
    global _default
    if _default is None:
        reg = PlanRegistry()
        # 只注册"没有生态包对应物"的内置计划。
        #
        # 2026-11-24 打通时发现名字重复：我早先造的 cli / frontend 计划，
        # 与生态包 cli_tool / frontend_spa 是同一个东西的两个名字。
        # 统一以**生态包名**为准（那是真适配器、真探针所在），
        # 我造的 CLI_PLAN / FRONTEND_PLAN 不再注册 —— 但代码保留，
        # 因为 judges_cli 等地方仍用 "cli" 这个短名，删了会牵连更多。
        for p in (FASTAPI_PLAN, DJANGO_PLAN):
            reg.register(p)

        # 有骨架模板的生态包 -> 自动补一个通用计划（2026-11-24 打通）
        # 失败不影响内置计划；同名已注册的跳过（内置优先）。
        try:
            from . import ecosystem as _eco
            for row in (_eco.list_packs() or []):
                if str(row.get("kind") or "") != "stack":
                    continue
                nm = str(row.get("name") or "").strip()
                if not nm or reg.get(nm) is not None:
                    continue
                tpl = _eco.pack_dir(nm) / "templates"
                if not tpl.is_dir():
                    continue
                plan = ecosystem_plan(nm, str(row.get("description") or "")[:80])
                if plan is not None:
                    reg.register(plan)
        except Exception:                            # noqa: BLE001
            pass

        _default = reg
    return _default


def plan_for(ws: Path, adapter=None) -> Optional[LayerPlan]:
    """按仓库的栈取生成计划。取不到返回 None。

    adapter 由调用方传入（fullgen 已经有 _st.from_workspace 的结果，
    这里不再重复探测，避免两处判断不一致）。
    """
    try:
        if adapter is None:
            from . import stack as _st
            adapter = _st.from_workspace(ws)
        name = str(getattr(adapter, "name", "") or "")
        # 适配器名就是生态包名（stack 启动时注册的就是它们），直接用。
        # 早先这里有一句"前端判定后改成裸 frontend"，已删除：
        # 那是自造的名字，跟生态包 frontend_spa 对不上（2026-11-24 打通时统一）。
        return default_registry().get(name)
    except Exception:                                # noqa: BLE001
        return None