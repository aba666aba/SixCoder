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

    P1 的前置：判据侧已经有 cli 包（退出码/输出/产物），
    这里补上"该生成什么文件"——否则引擎不知道 CLI 项目写哪里。

    设计取舍：CLI 工具通常是**平铺**的（一个入口 + 若干模块），
    不像 Web 那样分 models/services/api。所以只有一层。
    蓝图里如果声明了子目录（如 cmd/、lib/），按原样保留 —— 不强制扁平。
    """
    if layer != "cli":
        return []
    return [m for m in ctx.module_paths() if m.endswith(".py")]


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
    description="CLI 工具：单层平铺（入口 + 模块）",
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


def default_registry() -> PlanRegistry:
    global _default
    if _default is None:
        reg = PlanRegistry()
        for p in (FASTAPI_PLAN, DJANGO_PLAN, FRONTEND_PLAN, CLI_PLAN):
            reg.register(p)
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
        # 前端判定沿用旧逻辑：看落点模板后缀（这样以后加 Vue/Native 不用改这里）
        if str(getattr(adapter, "module_file_fmt", "") or "").endswith(
                (".jsx", ".tsx", ".vue")):
            name = "frontend"
        return default_registry().get(name)
    except Exception:                                # noqa: BLE001
        return None