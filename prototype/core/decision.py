"""故障定位与决策：**准确定出"哪个文件、哪个函数、哪一行"，再决定怎么修。**

用户的关切（这是整套系统最该硬的地方）：
    ① 准确确认函数和行数；
    ② 遇到 bug 时，系统到底按什么规则决定下一步做什么。

这个模块只做两件事（都不调模型，纯确定性）：
    localize()  —— 把"各种证据"统一成结构化故障清单 [{file, function, line, kind, detail}]
    decide()    —— 按一张**写死的决策表**给出下一步动作

证据 → 定位的对应关系（precision 只升不降）：
    · 编译/traceback            → 文件 + 行 + 函数（栈帧名）
    · pytest 失败               → 测试文件 + 被调用文件 + 行 + 函数
    · SQL 行形态 / 导入不可用   → 文件 + 行 + 函数
    · 签名 / 示例 / 异常 / 键 / 边界 / 可维护性 → 文件 + 函数（行用 AST 反查 def 行）
    · 端到端 / 重复定义 / 字段映射 → 文件 + 模块（说不出是哪个函数）

决策表（decision table）——**这就是"遇到 bug 怎么决策"的答案**：

| 定位精度 | 动作 | 重试上限 | 失败后 |
|---|---|---|---|
| 行（有 line） | 行级补丁 patch | 1 次 | → 函数级 |
| 函数（有 function） | 只重发该函数体 | 2 次 | → 升档 |
| 模块（只有 file） | 整文件重写 | 1 次 | → 升档 |
| 契约矛盾 | **不写代码**，回问契约 1 次 | 1 次 | → 停手，写交付报告 |
| 偶发（限流/超时/网络） | 同档重试，**不升档** | 同档预算内 | 不影响档位 |
| 依赖/环境缺失 | 不动代码，写依赖清单 | 0 | 只报告 |

升档（flash:none → low → high → max，pro 默认关）：同一动作类连续失败达到
`escalate_after` 次就升一档；**成功即回最低档**；整档用完仍失败 → 标"需人工"，写进报告。
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

# ---- 动作常量（决策表的输出） ----
ACT_PATCH_LINE = "patch_line"          # 行级补丁
ACT_REWRITE_FUNC = "rewrite_function"  # 只重写某几个函数体
ACT_REWRITE_MODULE = "rewrite_module"  # 整文件重写
ACT_ASK_CONTRACT = "ask_contract"      # 契约自相矛盾：先回问契约
ACT_RETRY_SAME = "retry_same_tier"     # 偶发失败：同档重试
ACT_REPORT_ONLY = "report_only"        # 环境/依赖：只报告
ACT_GIVE_UP = "give_up"                # 该模块暂缓（写进交付报告）

LINE_ACTIONS = (ACT_PATCH_LINE,)

META_HINTS = ("回归：", "已回滚", "两版都未通过", "预算耗尽", "策略阶梯",
              "已验收数", "收工原因", "轮次")

TRANSIENT_HINTS = ("限流", "429", "超时", "timed out", "Timeout", "网络", "请求失败",
                   "HTTP 502", "HTTP 503", "connection", "ConnectionError")


@dataclass
class Finding:
    """一条结构化故障。precision 只有三档：line > function > module。"""
    file: str
    kind: str
    detail: str
    function: str = ""
    line: int = 0
    severity: str = "hard"                      # hard | advisory
    source: str = ""                            # compile|pytest|gate|props|e2e|remote

    @property
    def precision(self) -> str:
        if self.line > 0:
            return "line"
        if self.function:
            return "function"
        return "module"

    def as_row(self) -> str:
        where = self.file + (f"::{self.function}" if self.function else "")
        if self.line:
            where += f":{self.line}"
        return f"[{self.precision}] {where} — {self.kind}：{self.detail[:100]}"


@dataclass
class Decision:
    action: str
    targets: list[str] = field(default_factory=list)     # 函数名或文件名
    file: str = ""
    reason: str = ""
    findings: list[Finding] = field(default_factory=list)

    def as_row(self) -> str:
        t = "、".join(self.targets[:5]) or "-"
        return f"{self.action} → {self.file or '-'} 目标 {t}（{self.reason}）"


# ---------------------------------------------------------------- 工具
def _func_line_map(src: str) -> dict[str, int]:
    """函数名 → def 所在行号（1-based）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    out = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = node.lineno
    return out


def _find_function_for_line(src: str, line: int) -> str:
    """给一个行号，反查它落在哪个函数里。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    hit = ""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= line <= end:
                hit = node.name
    return hit


def is_transient(text: str) -> bool:
    return any(k in str(text or "") for k in TRANSIENT_HINTS)


# ---------------------------------------------------------------- 定位
_TRACE = re.compile(r'File "(?P<f>[^"]+)", line (?P<l>\d+)(?:, in (?P<fn>\w+))?')
_GATE_FUNC = re.compile(r"(?P<f>[\w./\\-]+\.py)::(?P<fn>\w+)\(\)")
_GATE_LINE = re.compile(r"(?P<f>[\w./\\-]+\.py):(?P<l>\d+)")


def localize(ws: Path, evidence: str, rel: str = "") -> list[Finding]:
    """把一段证据文本解析成结构化故障清单（能定到行就一定带上行号）。"""
    ws = Path(ws)
    text = str(evidence or "")
    out: list[Finding] = []
    seen: set[tuple] = set()

    def add(f: Finding) -> None:
        key = (f.file, f.function, f.line, f.kind)
        if key in seen:
            return
        seen.add(key)
        out.append(f)

    # 1) traceback / 编译错误（最精确：有行号，还能从栈帧拿到函数名）
    for m in _TRACE.finditer(text):
        path = m.group("f").replace("\\", "/").split("/")[-1]
        line = int(m.group("l"))
        fn = m.group("fn") or ""
        if not path.endswith(".py"):
            continue
        src = ""
        p = ws / path
        if p.exists():
            try:
                src = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                src = ""
        if not fn and src:
            fn = _find_function_for_line(src, line)
        add(Finding(file=path, kind="traceback", detail=text.strip().splitlines()[-1][:160]
                    if text.strip() else "", function=fn, line=line, source="compile"))

    # 2) 闸门/判据文本：file::func() 与 file:N
    for m in _GATE_FUNC.finditer(text):
        add(Finding(file=m.group("f"), kind="gate", detail=text[:160],
                    function=m.group("fn"), source="gate"))
    for m in _GATE_LINE.finditer(text):
        path = m.group("f")
        line = int(m.group("l"))
        fn = ""
        p = ws / path
        if p.exists():
            try:
                fn = _find_function_for_line(p.read_text(encoding="utf-8-sig",
                                                         errors="replace"), line)
            except OSError:
                fn = ""
        add(Finding(file=path, kind="static", detail=text[:160], function=fn, line=line,
                    source="gate"))

    # 3) 判据里只点了函数名（`xxx() ` 形式）→ 行号用 AST 反查 def 行
    src = ""
    if rel:
        p = ws / rel
        if p.exists():
            try:
                src = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                src = ""
    if src:
        fmap = _func_line_map(src)
        for name in fmap:
            if re.search(rf"(?<![\w.]){re.escape(name)}\(\)", text):
                add(Finding(file=rel, kind="function", detail=text[:160], function=name,
                            line=0, source="props"))

    # 4) 元信息（回归/回滚/预算/两版都未通过…）不是"故障条目"，
    #    它有文件名但没有可修的位置，拿来判精度只会把整批拖成"模块级"。
    is_meta = any(h in text for h in META_HINTS) and not any(f.line or f.function for f in out)
    if is_meta:
        add(Finding(file=rel or "?", kind="meta", detail=text[:160], source="meta"))

    # 5) 什么都没解析出来 → 记成"模块级"（说不清是哪个函数）
    if not out and text.strip():
        add(Finding(file=rel or "?", kind="module", detail=text[:160], source="unknown"))
    return out


def precision_of(findings: list[Finding]) -> str:
    """整批故障的定位精度 = 最差的那一条（因为要都能修完）。

    元信息（回归/回滚/预算）不参与判定 —— 它只说明"上一版被回滚了"，
    并不告诉我们错误在哪一行/哪个函数；把它算进去会让每次失败都退化成"重写整文件"。
    """
    real = [f for f in findings if f.kind != "meta"]
    if not real:
        return "none"
    order = {"line": 0, "function": 1, "module": 2}
    return max((f.precision for f in real), key=lambda p: order.get(p, 3))


def decide(findings: list[Finding], rel: str = "", attempts_done: dict | None = None
           ) -> Decision:
    """按决策表给出下一步动作。

    attempts_done: {动作: 已试次数}；用来判断"该降级/该升档/该放弃"。
    """
    attempts_done = attempts_done or {}
    hard = [f for f in findings if f.severity == "hard" and f.kind != "meta"]
    if not hard:
        return Decision(ACT_REPORT_ONLY, file=rel, reason="只有提示项，不改代码", findings=findings)

    # 偶发失败（限流/超时）→ 同档重试，不影响档位
    if any(is_transient(f.detail) for f in hard) and len(hard) == 1:
        return Decision(ACT_RETRY_SAME, file=rel, reason="偶发失败（限流/超时），同档重试",
                        findings=findings)

    prec = precision_of(hard)
    funcs = sorted({f.function for f in hard if f.function})
    lines = [f for f in hard if f.line > 0]

    # 行级：所有硬故障都能定到行，且行级补丁还没试过
    if prec == "line" and all(f.line > 0 for f in hard) and \
            attempts_done.get(ACT_PATCH_LINE, 0) < 1:
        return Decision(ACT_PATCH_LINE, targets=[f"{f.file}:{f.line}" for f in lines],
                        file=rel or (lines[0].file if lines else ""),
                        reason="证据定位到行，先做最小改动", findings=findings)

    # 函数级：能定到函数，且函数级重写没超预算
    if funcs and attempts_done.get(ACT_REWRITE_FUNC, 0) < 2:
        return Decision(ACT_REWRITE_FUNC, targets=funcs, file=rel,
                        reason=f"定位到 {len(funcs)} 个函数，只重写这几个函数体",
                        findings=findings)

    # 模块级：说不清是哪个函数，或函数级已用完预算
    if attempts_done.get(ACT_REWRITE_MODULE, 0) < 1:
        return Decision(ACT_REWRITE_MODULE, file=rel,
                        reason="跨函数/说不清位置，整文件重写一次", findings=findings)

    return Decision(ACT_GIVE_UP, file=rel,
                    reason="行级/函数级/整文件都试过仍失败，暂缓并写进交付报告",
                    findings=findings)


def record(attempts_done: dict, action: str) -> dict:
    attempts_done[action] = attempts_done.get(action, 0) + 1
    return attempts_done


def table_markdown() -> str:
    """把决策表渲染成文档（docs/ 用，也用于交付报告）。"""
    return "\n".join([
        "| 定位精度 | 动作 | 重试上限 | 失败后 |",
        "|---|---|---|---|",
        "| 行（有明确行号） | 行级补丁 | 1 次 | 降级到函数级 |",
        "| 函数（有函数名，行号未知） | 只重写该函数体 | 2 次 | 升一档 |",
        "| 模块（只知道文件） | 整文件重写 | 1 次 | 升一档 |",
        "| 契约自相矛盾 | 不写代码，回问契约 | 1 次 | 停手 + 写报告 |",
        "| 偶发（限流/超时） | 同档重试 | 档内预算 | 不影响档位 |",
        "| 依赖/环境缺失 | 只报告 | 0 | 列进依赖清单 |",
    ])
