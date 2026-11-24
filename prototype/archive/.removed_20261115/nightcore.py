"""夜班执行内核（**施工锚点**：归一施工单见开发文档 §21，2026-11-11 用户定稿）。

现状（2026-11-11 开工前）：夜班有**两条**执行线，各写了一份写码循环：

  · `core/auto.py:night_auto`   —— 输入一句话目标（CLI `--goal` / `--goal-file`）
    有：切片规划层（`core/slicer.py`）、契约合成缓存（`core/contractgen.py`）
    缺：精细反馈（哪个模块欠哪个函数的工作单、架构师根因分析）

  · `core/company.py:night`     —— 输入完整需求（CLI `--requirement` / `--requirement-file`）
    有：契约体检（`core/iterate.py`）、红灯→工作单（`core/workorder.py`）、
        架构师、needs_work_state、needhuman（人批）
    缺：切片规划层（大项目上下文不可控）

**为什么必须归一（已经收过费）**：执行循环有两份 → "共享机制该挂在哪"每次都要重新判断
→ 迟早挂错。实测 2026-11-10：判据自洽修在 `openhouse.observe` 上，
而全自主线直接调 `coverage.run`、**绕过 openhouse** → 那个修复在真正跑的那条线上
**根本不生效**（修了个寂寞）。同类结构的坑还有接线保留旧段、include 补全、
`router` 声明补齐 —— 全都只在 `auto.py` 里，甲方线至今没这些保护。

**归一后的形态**（§21.3）：本文件是**唯一**写码循环。
两个入口变薄，只提供「输入形态 + 决策权」：

    cli --goal / --goal-file          → run(repo, goal=…, policy=…)
    cli --requirement / --requirement-file → run(repo, requirement=…, policy=…)
    server.py 目标框 / 需求框          → 同一个 run()
    tools/watchdog_night.py           → 不动（它就是"输入是需求文件"这一形态）

模式差异用**策略**表达，不再用"两条代码路径"表达：
`core/policy.py:Policy.arbiter` = remote（甲方：远端拍板）| human（开发者：关键节点要人批）；
只读模式不跑夜班。

**施工顺序（用户已定）**：先解覆盖停滞 → 再融合。本文件在 §21.5 第 1 步才填内容，
在此之前它只是锚点：不实现任何逻辑、也不被任何模块 import（跑了也是空跑）。

**铁律**：每一步改完必须真跑一次真实任务（§9.41 / §9.46 / §9.47 三个 bug 都是真跑才暴露，
自检全绿也没用）；不许"改断言让它绿"（§20.9 第 3 条）。
"""
from __future__ import annotations

from typing import Any

# 归一施工单：开发文档 §21（2026-11-11 用户定稿"彻底融合"）
# 施工步骤见 §21.5（第 1 步：抽切片规划层，两条线都能调）
PLAN_DOC = "开发文档.md §21"

# 目标签名（第 1 步之后才真正实现）：
#
#   def run(repo, *, goal="", requirement="", policy=None, plan="",
#           model="", max_rounds=40, emit=None, ...) -> NightResult
#
# 统一走这一个循环之后，下列资产必须**只有一处**：
#   · 写码循环          （现 auto.py / company.py 各一份）
#   · 覆盖观测入口      （现 auto 直调 coverage.run / company 走 openhouse.observe）
#   · 契约合成器        （core/contractgen.py，已共用，保持）
#   · 端到端能力观测    （core/openhouse.py，已共用，保持）
#   · 策略注入点        （core/policy.py，已共用，保持）


def run(repo: Any, **kwargs: Any) -> Any:
    """归一后的唯一夜班入口 —— **尚未实现**（施工单 §21.5 第 1 步）。

    现在调用会直接报错而不是静默跑空：这是故意的 ——
    空跑会让人以为"归一完成了"，而那正是 §9.45「契约全过却收工」同类的假象。
    """
    raise NotImplementedError(
        "core/nightcore.run 尚未实现：执行内核归一施工单见 %s（第 1 步：抽切片规划层）。"
        "在此之前夜班仍走 auto.night_auto / company.night 两条旧线。" % PLAN_DOC)
