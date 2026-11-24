"""判据接口（Judge Protocol）—— 阶段 1 核心（2026-11-24）。

## 解决什么（用户原话："固化只能做部分软件，不是通用的"）

现状：`core/gates.py` 里 30+ 个闸门**全部假设"这是一个 HTTP 服务"**：

    gate_db_rows        假设有 SQLite 表结构
    gate_api            假设有 REST 端点
    probe_app_candidates 假设有 app.main:app
    gate_security       假设有 HTTP 入参边界

判据就是引擎的**世界观**。判据锚死在 Web 服务上，引擎就只能造 Web 服务。
换"做一个命令行小工具"，引擎不会说"不支持"，而是**一路判红**
（找不到 app.main:app → 判据全红），看起来像"你写错了"。

更难的是：非 Web 项目**没有判据可跑**，于是只能靠模型自述 —— 
而"没有判据 = 默认通过"正是幻觉最大的温床
（同 `props.py:414` 的 `else: ok=True`）。

## 这一层做什么

把"判据"从"引擎内置函数"变成"可插拔包"：

    class Judge(Protocol):
        key: str                       # 唯一标识，如 "fastapi.db_rows"
        applies_to: frozenset[str]     # 只对哪些栈生效
        def check(ctx) -> GateResult   # 真正的检查

引擎只认 Judge，不认具体实现。选哪个栈 → 只跑那个栈的判据。

**关键效果**：CLI 项目不会被 HTTP 判据误判为红；
FastAPI 项目也不会被"文件产物存在"这种通用判据糊弄过去。

## 兼容策略（回归铁律）

阶段 1 **不改任何现有闸门的行为**。
做法：给现有闸门套一层 `LegacyJudge` 适配器，
`applies_to` 标成 `{"fastapi", "django"}`（它们本来就是为这两个写的），
旧调用路径原样保留。`tests/gate_snapshot.json` 逐字钉死这件事。

## 栈标识

```
python       纯 Python 库 / 脚本（无 Web 假设）
fastapi      ASGI 服务
django       Django 项目
cli          命令行工具（阶段 2 落地）
```

`applies_to` 用集合求交：判据对某栈生效 = 栈名在集合里，
或者集合里有 `"*"`（真正栈无关的判据，如"语法对不对"）。
"""
from __future__ import annotations

import json

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

from .gates import GateResult


# ── 栈标识 ───────────────────────────────────────────────

STACK_FASTAPI = "fastapi"
STACK_DJANGO = "django"
STACK_PYTHON = "python"
STACK_CLI = "cli"
ANY_STACK = "*"

KNOWN_STACKS = (STACK_FASTAPI, STACK_DJANGO, STACK_PYTHON, STACK_CLI)


@dataclass(frozen=True)
class JudgeContext:
    """判据能看到的全部上下文。

    **刻意窄**：只有文件系统根、相对路径、改动字典和契约文本。
    给判据更多权限 = 判据之间更容易耦合 = 更难插拔。
    """
    ws: Path
    paths: tuple[str, ...] = ()
    changed: dict[str, tuple[str, str]] = field(default_factory=dict)
    contract: str = ""
    node_kind: str = "impl"
    extras: dict[str, Any] = field(default_factory=dict)

    def rel_paths(self) -> tuple[str, ...]:
        return tuple(p.replace("\\", "/") for p in self.paths)


@runtime_checkable
class Judge(Protocol):
    """一条判据。"""
    key: str
    applies_to: frozenset[str]

    def check(self, ctx: JudgeContext) -> GateResult:
        ...


# ── 适配器：把现有闸门函数包装成 Judge ──────────────────

@dataclass
class LegacyJudge:
    """把 `gates.py` 里的老闸门函数包成 Judge，行为**逐字不变**。

    `applies_to` 默认 `{"fastapi", "django"}` —— 因为它们本来就是
    为这两个栈写的（见模块开头）。标错会让 CLI 项目被误判。
    """
    key: str
    fn: Callable[..., GateResult]
    applies_to: frozenset[str] = frozenset({STACK_FASTAPI, STACK_DJANGO})
    # 老函数签名五花八门：有的吃 (ws, paths)，有的吃 (ws, paths, contract)。
    # 这里按位置传，调用方负责给对参数 —— 这是过渡期的妥协，
    # 阶段 3 会把每个闸门都改成统一签名。
    arg_names: tuple[str, ...] = ("ws", "paths")
    description: str = ""

    def check(self, ctx: JudgeContext) -> GateResult:
        args = []
        for name in self.arg_names:
            if name == "ws":
                args.append(ctx.ws)
            elif name == "paths":
                args.append(list(ctx.rel_paths()))
            elif name == "contract":
                args.append(ctx.contract)
            elif name == "changed":
                args.append(ctx.changed)
            elif name == "node_kind":
                args.append(ctx.node_kind)
            elif name in ctx.extras:
                args.append(ctx.extras[name])
            else:
                raise KeyError(
                    f"判据 {self.key} 需要参数 {name!r}，"
                    f"但上下文里没有（extras={sorted(ctx.extras)}）"
                )
        return self.fn(*args)


@dataclass
class CtxJudge:
    """把"直接吃 JudgeContext"的函数包成 Judge。

    和 LegacyJudge 的区别：LegacyJudge 包装的是**老闸门的散参数签名**
    （ws, paths, contract...），CtxJudge 包装的是**新写的、统一签名**的判据。

    新判据应该用这个 —— 它拿到的是完整上下文，不用再和参数名对齐。
    这是阶段 2 之后新增判据的标准写法。
    """
    key: str
    fn: Callable[[JudgeContext], GateResult]
    applies_to: frozenset[str]
    description: str = ""

    def check(self, ctx: JudgeContext) -> GateResult:
        return self.fn(ctx)


# ── 注册表 ───────────────────────────────────────────────

class JudgeRegistry:
    """按栈挑选要跑哪些判据。

    设计要点：`for_stack` 返回的列表**顺序稳定**（按 key 排序），
    避免每轮顺序漂移导致提示词缓存失效 —— 这是 SixCoder
    "稳定前缀"口径的延续。
    """

    def __init__(self) -> None:
        self._judges: dict[str, Judge] = {}

    def register(self, judge: Judge) -> None:
        if not judge.key:
            raise ValueError("判据必须有 key")
        if judge.key in self._judges:
            raise ValueError(f"判据 key 重复：{judge.key}")
        if not judge.applies_to:
            raise ValueError(f"判据 {judge.key} 的 applies_to 不能为空（用 {ANY_STACK!r} 表全部）")
        self._judges[judge.key] = judge

    def get(self, key: str) -> Optional[Judge]:
        return self._judges.get(key)

    def all(self) -> list[Judge]:
        return [self._judges[k] for k in sorted(self._judges)]

    def for_stack(self, stack: str) -> list[Judge]:
        """这个栈该跑哪些判据。栈名不认识不猜，返回空表（调用方据此判断"没有判据"）。"""
        out = [j for k, j in sorted(self._judges.items())
               if ANY_STACK in j.applies_to or stack in j.applies_to]
        return out

    def stacks_for(self, key: str) -> frozenset[str]:
        j = self._judges.get(key)
        return j.applies_to if j else frozenset()


# ── 内置注册表：把现有闸门装进来（阶段 1 只做包装，不改行为）──

def build_default_registry() -> JudgeRegistry:
    """把 gates.py 里现有的闸门包装成 Judge 注册进去。

    **applies_to 的归类依据**（重要，别拍脑袋改）：

    | 判据 | applies_to | 为什么 |
    |---|---|---|
    | syntax / duplicates / import_ok / stub | * | 纯 Python 性质，与栈无关 |
    | anti_cheat | * | 防作弊对任何形态都成立 |
    | tests / assertions | * | 有测试就验，没有就跳过 |
    | db_rows / field_map / dict_keys | fastapi,django | SQL 表结构假设 |
    | api / security | fastapi,django | HTTP 入参边界假设 |
    | completeness / signature | * | 契约符号，与栈无关 |
    """
    from . import gates as G

    reg = JudgeRegistry()
    any_stack = frozenset({ANY_STACK})
    web = frozenset({STACK_FASTAPI, STACK_DJANGO})

    reg.register(LegacyJudge("syntax", G.gate_syntax, any_stack, ("ws", "paths"),
                             "语法能不能编译"))
    reg.register(LegacyJudge("anti_cheat", G.gate_anti_cheat, any_stack,
                             ("ws", "node_kind", "changed"), "7 项反逃避检查"))
    reg.register(LegacyJudge("stub", G.gate_stub, any_stack, ("ws", "paths"),
                             "不许把未实现当完成"))
    reg.register(LegacyJudge("import_ok", G.gate_import_ok, any_stack, ("ws", "paths"),
                             "模块能不能 import"))
    reg.register(LegacyJudge("duplicates", G.gate_duplicates, any_stack, ("ws", "paths"),
                             "同文件不得重名"))
    reg.register(LegacyJudge("cross_dupes", G.gate_cross_dupes, any_stack, ("ws", "paths"),
                             "跨文件同签名重复"))
    reg.register(LegacyJudge("completeness", G.gate_completeness, any_stack,
                             ("ws", "paths", "contract"), "契约点名的符号必须全在"))
    reg.register(LegacyJudge("signature", G.gate_signature, any_stack,
                             ("ws", "paths", "contract"), "签名与契约一致"))
    reg.register(LegacyJudge("assertions", G.gate_assertions, any_stack, ("ws", "paths"),
                             "断言不许是空壳"))
    reg.register(LegacyJudge("tests", G.gate_tests, any_stack, ("ws", "paths"),
                             "测试能跑"))

    # —— Web 专用 ——
    reg.register(LegacyJudge("db_rows", G.gate_db_rows, web, ("ws", "paths"),
                             "SQL 返回行的形态"))
    reg.register(LegacyJudge("api", G.gate_api, web, ("ws", "paths"),
                             "API 用法静态检查"))
    reg.register(LegacyJudge("security", G.gate_security, web, ("ws", "paths"),
                             "HTTP 边界安全"))
    reg.register(LegacyJudge("field_map", G.gate_field_map_result, web, ("ws", "paths", "contract"),
                             "模块字段与表列映射"))

    return reg


_default: Optional[JudgeRegistry] = None


def default_registry() -> JudgeRegistry:
    global _default
    if _default is None:
        _default = build_default_registry()
    return _default

# ── 与 stack.py 的桥接（阶段 3 第一块）──────────────────

def project_stack(ws) -> str:
    """判断这个项目属于哪个栈。

    **优先看 .sixcoder/stack.json（人写的），再看实物探测，最后才回默认。**

    为什么要这一层：stack.py 的 from_workspace 只认 fastapi/django，
    一个 CLI 项目会被判成 fastapi，于是跑一堆 HTTP 判据、全红。
    这里先认 cli，认不出再交给 stack.py。
    """
    from pathlib import Path as _P
    ws = _P(ws)

    # 1) 显式配置最权威
    cfg = ws / ".sixcoder" / "stack.json"
    if cfg.is_file():
        try:
            name = str((json.loads(cfg.read_text(encoding="utf-8-sig")) or {}).get("stack") or "").lower()
            if name:
                return name
        except Exception:                                # noqa: BLE001
            pass

    # 2) CLI 项目的实物特征
    if (ws / ".sixcoder" / "cli.json").is_file():
        return STACK_CLI

    # 3) 交给 stack.py（它的 detect 认 fastapi/django）
    try:
        from . import stack as _stack
        return str(_stack.from_workspace(ws).name or STACK_FASTAPI)
    except Exception:                                    # noqa: BLE001
        return STACK_FASTAPI


_with_cli: Optional[JudgeRegistry] = None


def full_registry() -> JudgeRegistry:
    """包含所有判据包（内置 + CLI + 多语言）的注册表。

    **必须缓存**：每次调用都新建 + register_xxx 会重复注册同一个 key
    （实测 2026-11-24：连调两次 judges_for_project 就 ValueError "判据 key 重复"）。
    而且判据表是只读的，缓存没有副作用。
    """
    global _with_cli
    if _with_cli is None:
        reg = default_registry()
        from . import judges_cli as _cli
        _cli.register_cli(reg)
        from . import judges_lang as _lang
        _lang.register_lang(reg)
        _with_cli = reg
    return _with_cli


def judges_for_project(ws) -> tuple[str, list]:
    """返回 (栈名, 这个项目该跑的判据列表)。

    **没有判据 != 通过**。空列表会被调用方当成"没有可验收的东西"，
    而不是"全部通过"—— 这是 props.py:414 修完之后的口径。
    """
    name = project_stack(ws)
    return name, full_registry().for_stack(name)