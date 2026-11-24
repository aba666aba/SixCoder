"""Agent 主循环：探测 → 计划 → 逐节点「生成补丁 → 闸门 → 快照」→ 审计。

关键设计（与 docs 对齐）：
- 状态只由 gate_runs 推进；模型文字只进事件流，不计入通过（docs/13 §4-S5）
- 测试先行：test 节点必须红，impl 节点必须绿 + 变异测试能杀死（docs/05 §1.2）
- 失败走升级阶梯：重采样 → 窄反馈（docs/06 §4）
"""
from __future__ import annotations

import ast
import difflib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from . import apikb, budget, context, db, docs as docsgen, gates, llm_cache, ollama, oracle, planner, probe, recon, skeleton, style, symedit, tools
from . import props   # 判据（also used to re-render a broken test file）

ROOT = Path(__file__).resolve().parent.parent
WORKSPACES = ROOT / "workspaces"
IGNORE = shutil.ignore_patterns("node_modules", "__pycache__", ".pytest_cache", ".venv",
                                "venv", "dist", "build", "target", ".idea", ".vscode")

CONVENTIONS = """Python 项目规约（内置）：
- 函数必须带类型标注；模块顶部 import 按标准库/三方/本地分组
- 参数校验失败抛 ValueError，信息用中文
- 不使用 print 输出调试信息；异常不得用裸 except/pass 吞掉
- 测试用 pytest，函数名 test_*, 每个测试至少一个断言"""


class Emitter:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.listeners: list = []

    def bind(self, fn) -> None:
        self.listeners.append(fn)

    def __call__(self, kind: str, payload: dict | None = None) -> None:
        self.emit(kind, payload)

    def emit(self, kind: str, payload: dict | None = None) -> None:
        payload = payload or {}
        db.add_event(self.task_id, kind, payload)
        for fn in list(self.listeners):
            try:
                fn({"kind": kind, "payload": payload, "ts": db.now()})
            except Exception:
                pass


def _snapshot(ws: Path, message: str, paths: list[str] | None = None) -> str:
    """提交本节点改动的快照。**要传 paths**（本节点实际写过的文件）。

    不传就退化成 `add -A`；当 ws 落在仓库根时，那等于"把用户所有未提交的改动一起
    打包提交"——实测因此产出过 39 个"消息写着 impl(calc.py)、内容却是别的文件"的混提交。
    """
    if paths is None:
        tools.git(ws, "add", "-A")
    else:
        wanted = [p for p in dict.fromkeys(paths) if str(p).strip()]
        if not wanted:
            return ""                    # 明确"本次没有改动"：绝不退化成 add -A
        tools.git(ws, "add", "-A", "--", *[str(p) for p in wanted])
    tools.git(ws, "-c", "user.name=SixCoder", "-c", "user.email=coder@local",
              "commit", "-q", "-m", message)
    rc, head = tools.git(ws, "rev-parse", "--short", "HEAD")
    return head if rc == 0 else ""


# ---------------------------------------------------------------- 计划

PLAN_PROMPT = """你是代码任务规划器。把需求拆成「可独立验证」的节点，只输出 JSON。

硬性规则：
1. **必须先写测试再改实现**（测试先行）。
2. kind 只有两个取值：test 或 impl。不要写 "test|impl" 这种占位符。
3. mode 只有两个取值：patch（改已有文件）或 whole（整文件新建）。
4. 测试节点的 path 必须是测试文件（文件名 test_<模块>.py），不要指向被测源码。
5. 每个节点只涉及 1 个文件。
6. **按模块成对输出**：每个模块先出 test 节点，再出 impl 节点。
7. 新模块一律用 mode=whole 整文件新建；项目内的模块都放在仓库根目录（不要建子包）。
8. 节点总数不超过 {max_nodes} 个；优先保证「测试+实现」成对完整，不要为了多写模块而牺牲成对性。
9. **需求里点名的每个模块都必须出现在计划里**（例如需求提到 models/storage/service/cli 四个模块，
   就应给这四个模块各出一对节点）。不要只做第一个模块就收工。

输出格式（照抄结构，替换内容）：
{{"nodes":[
  {{"kind":"test","mode":"whole","path":"test_models.py","contract":"为 models 补测试：正常构造、金额<=0 抛 ValueError、日期格式非法抛 ValueError"}},
  {{"kind":"impl","mode":"whole","path":"models.py","contract":"实现 Transaction 数据类与校验"}}
],"reasoning":"一句话说明拆分理由"}}

仓库文件：
{files}

现有测试文件：{tests}

需求：{requirement}
目标文件（用户指定，可为空）：{target}
仓库形态：{state}（{policy}）
"""


def _plan(node_ctx: dict, model: str, emit: Emitter) -> list[dict]:
    msg = PLAN_PROMPT.format(**node_ctx)
    res = ollama.chat([{"role": "user", "content": msg}], model, json_mode=True,
                      temperature=0.2, num_predict=1200)
    emit("llm_call", {"stage": "plan", "model": model, "ms": res["ms"],
                      "tokens": res["eval_count"]})
    raw = res["text"].strip()
    try:
        data = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {"nodes": []}
    nodes = data.get("nodes") or []
    emit("plan", {"nodes": nodes, "reasoning": data.get("reasoning", "")})
    return nodes[:8]


# ---------------------------------------------------------------- 生成

TEST_PROMPT = """你是 pytest 测试生成器。只输出 JSON，不要解释、不要 Markdown 围栏。

输出格式（只允许 append_code）：
{{"ops":[{{"op":"append_code","code":"\\n\\ndef test_xxx():\\n    assert ..."}}]}}

铁律：
1. 追加的函数名必须以 test_ 开头。
2. **禁止写被测函数的实现**（不要写 def divide(...)——那是另一个节点的工作）。
3. 被测函数此刻尚未实现，所以这些测试现在**应当失败**，这是预期的。
4. 至少 1 个正常路径 + 1 个异常路径；每个测试至少 1 个断言。
5. 如果用 pytest.raises，测试文件顶部必须有 import pytest（引擎也会自动补）。

【测试文件】{path}
【被测需求】{contract}
【测试文件当前内容】
{content}
{feedback}"""

SIGNATURE_PROMPT = """你是接口设计器。**只输出 JSON**，不要写实现代码。

为下列任务设计该模块的公开接口（函数/类签名）：
{{"functions":[
  {{"name":"load","params":[{{"name":"path","type":"str"}}],"returns":"list[dict]","doc":"读取 JSON 文件","is_class":false}},
  {{"name":"Transaction","params":[{{"name":"amount","type":"float"}}],"returns":"None","doc":"交易","is_class":true}}
]}}

规则：
1. 只输出该任务真正需要的接口，2-5 个即可；名字用 snake_case（类用 PascalCase）。
2. 参数类型用 Python 标注（str/int/float/bool/list[dict]/None 等）；不要写 object。
3. 需要抛异常的函数，在 doc 里写清"何时抛 ValueError"。
4 只输出 JSON，不要解释。

【任务】{contract}
【目标文件】{path}
【已有模块 API】
{api}
"""

NEW_FILE_PROMPT = """你是代码生成器。为下列任务**新建一个完整的 Python 文件**，只输出一个 python 代码块。

要求：
1. 文件开头写模块 docstring；公开函数/类都要类型标注与 docstring。
2. 只 import 标准库与【已有模块 API】里列出的符号（不要 import 尚不存在的符号，不要自己发明模块名）。
3. 参数非法一律抛 ValueError，错误信息用中文、说明原因。
4. 不要写测试代码（测试是另一个节点的事）。不要写 `if __name__ == "__main__"`。
5. 不要使用 print 输出调试信息。
6. 代码要能被直接 import 执行（无副作用）。

【任务】{contract}
【目标文件】{path}
【已有模块 API（可直接 from X import Y）】
{api}
【规约】
{conventions}
{feedback}"""

PATCH_PROMPT = """你是代码补丁生成器。只输出 JSON，不要解释、不要 Markdown 围栏。

输出格式（严格，一个 JSON 对象，ops 里放 1-3 条）：
{{"ops":[
  {{"op":"append_code","code":"def divide(a, b):\\n    ..."}},
  {{"op":"replace_function","name":"sub","code":"def sub(a, b):\\n    ..."}},
  {{"op":"patch","search":"<原文片段>","replace":"<替换内容>"}}
]}}

四种 op 的适用场景（优先用前两种，它们不需要精确复制原文）：
- append_code：在文件末尾追加新函数/新代码。**新增函数优先用它**。
- replace_function：整体替换一个已存在的函数，要用 name 指定函数名。
- insert_after：在指定函数之后插入代码，用 name 指定函数名。
- patch：只改函数内部的少量行，需要 search 逐字符匹配原文（中文必须原样复制）。

规则：
1. 只改与任务相关的代码，不要顺手重构、不要改格式。
2. code 里写完整可运行的 Python（含缩进与类型标注）。
3. 不要重复输出同一段代码。

【任务】{contract}
【目标文件】{path}（共 {nlines} 行）
【已验证的 API 签名（事实来源；命名与参数照此写，不要凭记忆编 API）】
{apifacts}
【纯 ASCII 锚点（如需 patch，可参考这些行）】
{anchors}
【当前文件内容】
{content}
【规约】
{conventions}
{feedback}"""

SYMEDIT_PROMPT = """你是符号级补丁器。**只输出 JSON**，不要解释、不要 Markdown 围栏。

输出格式（严格）：
{{"edits":[
  {{"op":"replace","symbol":"<符号限定名>","code":"def foo(a: int) -> int:\\n    \\"\\"\\"文档。\\"\\"\\"\\n    return a"}},
  {{"op":"add","symbol":"new_fn","after":"已有函数名","code":"def new_fn() -> int:\\n    return 1"}}
]}}

铁律：
1. 你只输出**要改的那个符号的完整定义**；文件其余部分（import、注释、其它函数）由渲染器原样保留，你**不要**输出整文件。
2. symbol 必须取自下面【可编辑符号】清单；类方法写成 Cls.method。
3. code 必须包含 def 行本身，函数体缩进 4 空格，带类型注解和一行 docstring。
4. 新增符号用 op=add，并用 after 指定插入到哪个已有符号之后。
5. 不确定时宁少改：只改一条符号，也不要顺带重构。

【任务】{contract}
【目标文件】{path}
【已验证的 API 签名（事实来源；命名与参数照此写，不要凭记忆编 API）】
{apifacts}
【可编辑符号】（限定名 + 签名）
{symbols}
【待改符号的现状】
{targets}
【必须通过的测试（本地用它判定你是否完成；不要修改测试，也不要新增同名函数绕过）】
{tests}
【规约】
{conventions}
{feedback}"""

WHOLE_PROMPT = """你是测试代码生成器。为下列任务写一个 pytest 测试文件，只输出一个 python 代码块。
要求：
1. 覆盖正常路径 + 至少一个异常路径（如参数非法/除零）。
2. 每个测试函数至少一个断言，断言必须能区分正确与错误实现。
3. 不用 skip 标记，不用 try/except 吞掉异常。
4. **必须写全 import**：用到 pytest.raises 就先 `import pytest`；被测函数必须 from 模块 import。
5. 只 import 标准库与被测模块。

【任务】{contract}
【目标文件】{path}
【被测模块现有内容】
{content}
【规约】
{conventions}
{feedback}"""


def _strip_fence(text: str) -> str:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    return m.group(1) if m else text


def _anchors(content: str, k: int = 4) -> str:
    """给模型一批纯 ASCII 的可用锚点，绕开 3B 复制中文会出错的问题。"""
    picks: list[str] = []
    for line in content.splitlines():
        if not line.strip() or any(ord(c) > 127 for c in line):
            continue
        if line.lstrip().startswith(("def ", "class ", "return ", "}")):
            if line not in picks:
                picks.append(line)
    tail = [l for l in content.splitlines() if l.strip() and all(ord(c) < 128 for c in l)][-2:]
    for t in tail:
        if t not in picks:
            picks.append(t)
    return "\n".join(f"  {p}" for p in picks[-k:]) or "  （无纯 ASCII 锚点，请自行精确复制）"


def _autofix_test_imports(ws: Path, rel: str, text: str, module_rel: str,
                          extra: list[str] | None = None) -> tuple[str, list[str]]:
    """确定性后处理：补齐测试文件缺失的 import（3B 最高频的确定性缺陷）。

    为什么放在引擎而不是提示词里：这是一个**可判定的缺陷**，
    用 30 行代码修比比让模型"记得 import"可靠得多（docs/05 §5 确定性检查）。
    """
    import ast as _ast
    mod = Path(module_rel).stem if module_rel else ""
    exported: list[str] = []
    src = ws / module_rel if module_rel else None
    if src and src.exists():
        try:
            tree = _ast.parse(src.read_text(encoding="utf-8"))
            exported = [n.name for n in tree.body
                        if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))]
        except SyntaxError:
            exported = []

    body = "\n".join(l for l in text.splitlines() if not re.match(r"\s*(from|import)\s", l))
    used = set(re.findall(r"\b([A-Za-z_]\w*)\b", body))
    # extra：被测函数此刻可能还不存在（测试先行），所以不能只靠"当前导出的符号"
    candidates = list(dict.fromkeys(exported + list(extra or [])))
    missing = [e for e in candidates
               if e in used and not re.search(rf"^\s*from\s+{mod}\s+import[^\n]*\b{e}\b", text, re.M)]

    adds: list[str] = []
    if "pytest." in text and not re.search(r"^\s*import pytest", text, re.M):
        adds.append("import pytest")
    if missing and mod:
        adds.append(f"from {mod} import {', '.join(sorted(set(missing)))}")
    if not adds:
        return text, []

    lines = text.splitlines()
    idx = 0
    for i, line in enumerate(lines):
        if re.match(r"\s*(from|import)\s", line):
            idx = i + 1
    return "\n".join(lines[:idx] + adds + lines[idx:]) + "\n", adds


TEST_SPEC_PROMPT = """你是测试规约生成器。**只输出 JSON，不要写测试代码**。

把需求转成「可机器渲染」的检查清单：
{{"module":"models","checks":[
  {{"func":"validate_amount","args":["100"],"expect_return":null,"expect_exception":null}},
  {{"func":"validate_amount","args":["0"],"expect_return":null,"expect_exception":"ValueError"}}
]}}

规则：
1. module 是被测模块名（不含 .py），只能有一个。
2. func 是该模块里的一个函数名或类名；args 是字符串数组，按参数顺序（字符串要自带引号，如 "\\"abc\\""）。
3. 一个 check 只描述一次调用，三种形态：
   - 正常取值断言：expect_return 给期望返回值（Python 字面量），expect_exception 为 null
   - 异常断言：expect_exception 给异常类型（如 "ValueError"），expect_return 为 null
   - 只验证不抛异常：两者都为 null
4. 每个 check 的 args 必须是合法字面量，不要放变量或表达式。
5. 至少 2 个 check：一个正常路径 + 一个异常路径（若该函数没有异常路径，用"只验证不抛异常"）。
6. 最多 6 个 check，覆盖需求里提到的关键行为。
7. **func 只能从下面「允许的符号」里选**，选别的名字会导致测试 import 失败：
   {allowed}

【需求】{contract}
【被测文件】{path}
【被测模块现有代码】
{content}
"""

TEST_TEMPLATE_HEAD = '''import pytest
from {module} import {names}
'''

TEST_TEMPLATE_CALL = '''

def test_{module}_{func}_{i}():
    {body}
'''

_IDENT = re.compile(r"^[A-Za-z_]\w{0,40}$")


def _safe_literal(value: str, limit: int = 80) -> str:
    """限制字面量长度并禁止换行/分号注入。"""
    return str(value or "").strip().replace("\n", " ").replace(";", " ")[:limit]


def _contract_symbols(ws: Path, rel: str, contract: str) -> set[str]:
    """本模块"合同里承诺存在的符号"：契约里点名的 + 文件里已有的。

    实测教训：测试规约会让模型自由编一个函数名（如 validate_amount），而实现节点只按契约
    产出 Transaction，于是测试永远 import 失败（A 组 4 个 impl 节点全部死在这里）。
    所以测试规约的 func 必须限定在这批符号内。
    """
    names: set[str] = set()
    try:
        p = tools.safe_path(ws, rel)
        if p.exists():
            names |= {s.name for s in symedit.outline(p.read_text(encoding="utf-8",
                                                                 errors="replace"))}
    except Exception:                                  # noqa: BLE001
        pass
    for tok in re.findall(r"[A-Za-z_]\w{2,}", contract or ""):
        names.add(tok)
    return names


def _unconstrained_checks(raw: list) -> list[dict]:
    """符号约束的降级路径：不做名字校验，只保留语法上可渲染的检查。"""
    out: list[dict] = []
    for c in raw[:6]:
        if not isinstance(c, dict):
            continue
        func = str(c.get("func") or "").strip()
        if not _IDENT.match(func):
            continue
        args = [str(a) for a in (c.get("args") or []) if str(a).strip()]
        exc = c.get("expect_exception")
        exc_s = None if exc in (None, "", "null") else str(exc).strip()
        if not args and (exc_s in (None, "TypeError", "Exception")):
            continue
        ret = c.get("expect_return")
        out.append({"func": func, "args": args,
                    "expect_return": None if ret in (None, "", "null") else str(ret).strip(),
                    "expect_exception": exc_s})
    return out


def _generate_test_spec(ctx: dict, model: str, temperature: float, feedback: str):
    """让模型只填「检查清单」的槽位，测试代码由引擎模板渲染（docs/05 确定性后处理）。"""
    allowed = ctx.get("allowed") or set()
    allowed_txt = "、".join(sorted(allowed)[:12]) or "（未限定，用契约里出现的名字）"
    prompt = TEST_SPEC_PROMPT.format(contract=ctx["contract"], path=ctx["path"],
                                     content=ctx["content"], allowed=allowed_txt)
    if feedback:
        prompt += f"\n【上次失败原因，必须修正】\n{feedback}\n"
    cached = llm_cache.get(model, temperature, prompt)
    if cached:
        res = {"text": cached, "ms": 0, "eval_count": 0, "cached": True}
    else:
        res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                          temperature=temperature, num_predict=600)
        llm_cache.put(model, temperature, prompt, res.get("text", ""))
    data = None
    cands = [res["text"]]
    m = re.search(r"\{.*\}", res["text"] or "", re.DOTALL)
    if m:
        cands.append(m.group(0))
    for cand in cands:
        if not cand:
            continue
        try:
            data = json.loads(cand)
            break
        except Exception:
            data = None
    if not isinstance(data, dict):
        return None, res

    module = str(data.get("module") or "").strip()
    if module.endswith(".py"):
        module = module[:-3]
    if not _IDENT.match(module):
        return None, res

    checks = []
    dropped = 0
    raw_checks = data.get("checks") or []
    if not isinstance(raw_checks, list):
        return None, res
    for c in raw_checks[:6]:
        if not isinstance(c, dict):
            continue
        func = str(c.get("func") or "").strip()
        args = [str(a) for a in (c.get("args") or []) if str(a).strip()]
        ret = c.get("expect_return")
        exc = c.get("expect_exception")
        if not _IDENT.match(func):
            continue
        allowed = ctx.get("allowed") or set()
        if allowed and func not in allowed:
            # 先尝试纠名（load_data → load），纠不动才丢弃：实测直接丢弃会让测试节点反复失败
            near = difflib.get_close_matches(func, sorted(allowed), n=1, cutoff=0.6)
            if near:
                func = near[0]
            else:
                dropped += 1
                continue
        if not args:
            # 零参数函数也允许：允许 args 为空数组
            if c.get("args") not in ([], None):
                continue
        ret_s = None if ret in (None, "", "null") else str(ret).strip()
        exc_s = None if exc in (None, "", "null") else str(exc).strip()
        # 过滤空心检查：
        #  a) 空参调用（`Transaction()`）几乎必然 TypeError，验证的是 Python 语法不是我们的代码
        #  b) 空参 + 期待 TypeError/Exception，同上是废检查
        if not args and (exc_s in (None, "TypeError", "Exception")):
            continue
        checks.append({"func": func, "args": args, "expect_return": ret_s,
                       "expect_exception": exc_s})
    # 参数不是合法字面量的检查直接丢掉：否则渲染阶段会抛 ValueError 让整个节点作废
    if checks:
        valid = []
        for c in checks:
            try:
                for a in c["args"]:
                    oracle.normalize_literal(a)
                valid.append(c)
            except Exception:                 # noqa: BLE001
                continue
        checks = valid
    if not checks and dropped:
        # 放宽：全部被符号约束滤空时，退回"不约束"的那批检查（宁可让 impl 节点去满足它，
        # 也不要让测试节点直接判死——实测这样会把 4/8 打成 1/8）
        checks = _unconstrained_checks(data.get("checks") or [])
        if checks:
            data["_unconstrained"] = True
    if not checks:
        return None, res
    # 冲突消解：需求说"返回 X"的用例，模型却断言"必须抛异常" → 以需求为准，丢掉该检查。
    # （实测：safe_div 除零需求是返回 0.0，模型测试却要求抛 ZeroDivisionError，
    #   于是实现节点被自己的测试反复判死，而产物其实是对的）
    req = (ctx.get("requirement") or "").lower()
    if req:
        kept = []
        for c in checks:
            if c.get("expect_exception") and "返回" in req and "抛" not in req:
                continue
            kept.append(c)
        if kept:
            checks = kept
    # 完整性校验：至少要有一个"正常路径"检查
    if all(c["expect_exception"] for c in checks):
        return None, res
    data["module"], data["checks"] = module, checks
    return data, res


def _render_check(check: dict, i: int, module: str) -> str:
    func = check["func"]
    args = ", ".join(oracle.normalize_literal(a) for a in check["args"])
    call = f"{func}({args})"
    if check.get("expect_exception"):
        # 异常类型是**类**不是字符串：pytest 9 不接受 pytest.raises('ValueError')
        # （实测 service 节点连续 3 次死在这里）。裸标识符必须原样当类用。
        raw = str(check["expect_exception"]).strip().strip("'\"")
        exc = raw if re.match(r"^[A-Za-z_][\w.]*$", raw) else "Exception"
        body = f"with pytest.raises({exc}):\n        {call}"
    elif check.get("expect_return") is not None:
        exp = oracle.normalize_literal(check["expect_return"])
        body = f"assert {call} == {exp}"
    else:
        # "只验证不抛异常"：用 try/except + pytest.fail，避免变成没有断言的裸调用
        body = (f"try:\n        {call}\n"
                f"    except Exception as _e:\n"
                f"        pytest.fail(f\"正常输入不应抛异常：{{_e}}\")")
    return TEST_TEMPLATE_CALL.format(module=module, func=func, i=i, body=body)


def _render_test_spec(spec: dict) -> str:
    checks = spec.get("checks") or []
    if not checks:
        raise tools.ToolError("测试规约没有任何 checks")
    module = spec["module"]
    names = sorted({c["func"] for c in checks})
    code = TEST_TEMPLATE_HEAD.format(module=module, names=", ".join(names))
    for i, c in enumerate(checks, 1):
        for a in c["args"]:
            try:
                oracle.normalize_literal(a)
            except Exception as exc:
                raise tools.ToolError(str(exc))
        code += _render_check(c, i, module)
    return code


def _apply_test_spec(ws: Path, rel: str, base: str, spec: dict) -> tuple[str, str]:
    """把渲染出的测试追加到测试文件（保留既有测试），并确定性补齐 import。"""
    code = _render_test_spec(spec)
    funcs = sorted({c["func"] for c in spec["checks"]})
    if not base.strip():
        new = code
    else:
        body = code
        if f"from {spec['module']} import" in base:
            body = "\n".join(code.splitlines()[2:]).lstrip("\n")
        if "import pytest" in base:
            body = body.replace("import pytest\n", "", 1)
        new = base.rstrip("\n") + "\n\n\n" + body.strip("\n") + "\n"
    fixed, adds = _autofix_test_imports(ws, rel, new, spec["module"] + ".py", extra=funcs)
    return fixed, code


def _generate_signatures(ctx: dict, model: str, temperature: float):
    """只生成签名 JSON（100-200 token），骨架由引擎渲染。"""
    prompt = SIGNATURE_PROMPT.format(contract=ctx["contract"], path=ctx["path"],
                                     api=ctx.get("api") or "（暂无）")
    cached = llm_cache.get(model, 0.0, prompt)
    if cached:
        try:
            data = json.loads(cached)
        except Exception:
            data = None
        if data:
            return skeleton.parse_signatures(data), {"cached": True, "ms": 0, "eval_count": 0}
    res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                      temperature=temperature, num_predict=400)
    data = None
    m = re.search(r"\{.*\}", res["text"] or "", re.DOTALL)
    for cand in (res["text"], m.group(0) if m else None):
        if not cand:
            continue
        try:
            data = json.loads(cand)
            break
        except Exception:
            data = None
    if not isinstance(data, dict):
        return [], res
    llm_cache.put(model, 0.0, prompt, res["text"])
    return skeleton.parse_signatures(data), res


_STYLE_CACHE: dict[str, str] = {}


def _style_text(ws: Path) -> str:
    """每个仓库只算一次风格规约（扫 40 个文件，确定性）。"""
    key = str(Path(ws).resolve())
    if key not in _STYLE_CACHE:
        try:
            _STYLE_CACHE[key] = style.render(style.profile(ws))
        except Exception:
            _STYLE_CACHE[key] = CONVENTIONS
    return _STYLE_CACHE[key]


def _ref_enabled() -> bool:
    """M1 参照实现差分开关（SIXCODER_REF=1 开启；会额外调用一次模型写参照）。"""
    import os
    return os.environ.get("SIXCODER_REF", "0") == "1"


def _bestofn_enabled() -> bool:
    """M2 可验证 Best-of-N 开关（SIXCODER_BESTOFN=1：采样 k 个候选，按质量分选优）。"""
    import os
    return os.environ.get("SIXCODER_BESTOFN", "0") == "1"


def _remote_escalation_enabled() -> bool:
    """本地连续失败后是否上报远端出方案（默认开；SIXCODER_REMOTE_ESCALATE=0 可关）。

    默认开：本地 7B 卡住时，一次远端调用约 $0.0005，比"整条任务卡住等人来救"便宜得多；
    而且远端只出方案，落盘与判定仍走本地确定性渲染 + 全部闸门（docs/13 判定权分离）。
    """
    import os
    return os.environ.get("SIXCODER_REMOTE_ESCALATE", "1") != "0"


def _quality(results: list) -> int:
    """候选质量分：越小越好（只在"已全绿"候选之间比较）。"""
    penalty = 0
    for gate, _ok, detail in results:
        d = str(detail or "")
        m = re.search(r"建议 (\d+) 条", d)
        if m:
            penalty += int(m.group(1))
        m = re.search(r"偏离 (\d+) 处", d)
        if m:
            penalty += int(m.group(1))
        penalty += d.count("缺 docstring")
        penalty += d.count("缺类型标注")
    return penalty


def _api_facts(text: str, limit: int = 8) -> str:
    """从 API 知识库回填真实签名；KB 缺失时返回"无"（不猜、不假装知道）。"""
    try:
        kb = apikb.ApiKB(gates.API_KB_DB)
        if not kb.available():
            return "（无）"
        return kb.block_for(text, limit=limit) or "（无）"
    except Exception:                                 # noqa: BLE001
        return "（无）"


def _tests_text(ws: Path, test_files: list[str], rel: str, limit: int = 2600) -> str:
    """把与本节点配对的测试正文取出来喂给实现节点。

    实测教训：只给契约，模型会写出与测试对不上的接口（`ImportError: cannot import name ...`），
    4 个实现节点全部死在这里。测试才是本地判定标准，必须让它看见。
    """
    blocks, used = [], 0
    for t in _paired_tests(test_files, rel):
        try:
            src = (ws / t).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if used + len(src) > limit:
            src = src[:max(0, limit - used)]
        blocks.append(f"### {t}\n```python\n{src}\n```")
        used += len(src)
        if used >= limit:
            break
    return "\n\n".join(blocks) or "（暂无配对测试）"


def _module_local_contract(contract: str, rel: str) -> str:
    """取"本模块那一段"契约，避免把别的模块的函数名带进骨架。

    实测事故：storage.py 的骨架里出现了 models 的 `create_transaction`，
    于是实现节点去填一个不属于自己的桩，整条链一起失败。
    """
    text = contract or ""
    stem = Path(rel).stem
    m = re.search(rf"{re.escape(stem)}(?:\.py)?\s*[：:]\s*(.*?)(?=\n\s*\d+\)|\Z)", text, re.DOTALL)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return text


def _task_requirement(node: dict) -> str:
    try:
        rows = db.query("SELECT requirement FROM tasks WHERE id=?", (node.get("task_id"),))
        return (rows[0].get("requirement") or "") if rows else ""
    except Exception:                                 # noqa: BLE001
        return ""


def _symedit_enabled() -> bool:
    """H2 实验开关：SIXCODER_SYMEDIT=0 时回到"整文件/patch 文本"老路径。"""
    import os
    return symedit.available() and os.environ.get("SIXCODER_SYMEDIT", "1") != "0"


MISSING_RE = re.compile(
    r"cannot import name '([A-Za-z_]\w*)' from '([\w.]+)'"
    r"|NameError: name '([A-Za-z_]\w*)' is not defined"
    r"|ModuleNotFoundError: No module named '([\w.]+)'")


def _impl_rel_of(node: dict) -> str:
    """从节点路径推出"实现模块"的相对路径（测试节点时去掉 test_ 前缀）。"""
    rel = str(node.get("path") or "")
    name = Path(rel).name
    return rel[5:] if name.startswith("test_") else rel


TEST_BROKEN_PAT = (
    "partially initialized module",          # 自己 import 自己（实测踩过）
    "cannot import name",                    # 从测试模块导入不存在的名字
    "SyntaxError",                           # 测试文件语法坏
    "IndentationError",
)


def _test_file_self_broken(detail: str, test_files: list[str]) -> str:
    """判断失败是不是"测试文件自身坏了"（导入/语法），返回证据片段或空串。

    判据：报错栈里出现测试文件名 + 上述典型错误 → 这不是实现的问题。
    为什么要单独判：坏测试文件会让实现节点无论怎么改都红，远端救火也会白跑
    （实测：`from test_calc import test_calc_divide_1` 自己导入自己 → ImportError 级联）。
    """
    text = str(detail or "")
    names = [Path(t).name for t in (test_files or []) if t]
    if not names:
        return ""
    hit_pat = next((p for p in TEST_BROKEN_PAT if p in text), "")
    if not hit_pat:
        return ""
    # 报错位置必须落在这个测试文件里才算（避免把实现里的 ImportError 误判成测试文件坏）
    for name in names:
        for line in text.splitlines():
            if name in line and (": in <module>" in line or "import" in line.lower()):
                return f"{name}: {hit_pat}（{line.strip()[:120]}）"
    if any(name in text for name in names):
        return f"{hit_pat}"
    return ""


def _test_file_static_broken(p: Path, impl_rel: str, impl_symbols: set[str]) -> str:
    """静态判断测试文件是否"自身坏了"，返回原因（空串=没问题）。

    为什么不能只看语法：真实事故里文件**语法完全合法**，坏在导入——
    `from test_calc import test_calc_divide_1`（自己 import 自己）、
    `from calc import a`（a 是实现里的参数，不是符号）。这类只有 import 时才会炸。
    """
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    try:
        import ast as _ast
        _ast.parse(text)
    except SyntaxError as exc:
        return f"语法错误：{exc.msg}（第 {exc.lineno} 行）"
    stem = p.stem
    impl_stem = Path(impl_rel).stem if impl_rel else ""
    # ① 自己 import 自己
    if re.search(rf"^\s*from\s+{re.escape(stem)}\s+import\b", text, re.M):
        return f"自我导入：from {stem} import …"
    # ② 从实现模块导入不存在的符号（自由变量被当成符号，实测踩过）
    for m in re.finditer(rf"^\s*from\s+{re.escape(impl_stem)}\s+import\s+([\w\s,]+)$",
                         text, re.M):
        names = [x.strip().split(" as ")[0].strip() for x in m.group(1).split(",")]
        missing = [n for n in names if n and n != "*" and n not in impl_symbols]
        if missing:
            return f"从 {impl_stem} 导入了不存在的符号：{', '.join(missing)}"
    return ""


def _repair_test_files(ws: Path, node: dict, task_id: str, emit) -> dict:
    """修补坏掉的测试文件：**外科手术式**删掉坏 import，绝不允许删测试。

    血泪教训（本函数第一版）：坏测试文件在被删掉之后，实现节点"因为没有配对测试"
    直接通过了 —— 这叫**假绿**，比失败更糟（docs/16 的主题）。
    所以现在的规则是：
      1. 只删"坏的那几行 import"（自我导入、导入了实现里不存在的符号）；
      2. 删完必须还能 import，且至少还剩 1 个测试；
      3. 实在救不回来，就用契约判据重渲染；再不行就写一个"符号存在且可调用"的最小测试；
      4. **任何情况下都不删测试文件**。
    返回 {repaired: [...], rewritten: [...], unchanged: [...], reasons: [...]}。
    """
    from . import handoff, props as props_mod
    impl_rel = _impl_rel_of(node)
    targets = handoff._paired_test_files(ws, impl_rel) if impl_rel else []
    repaired, rewritten, unchanged, reasons = [], [], [], []
    try:
        impl_symbols = props_mod.module_symbols(ws, impl_rel)
    except Exception:                                    # noqa: BLE001
        impl_symbols = set()
    impl_stem = Path(impl_rel).stem if impl_rel else ""
    need = node.get("contract") or ""
    requirement = _task_requirement(node)
    try:
        checks = props_mod.from_requirement((need or "") + "\n" + requirement, impl_symbols)
    except Exception:                                    # noqa: BLE001
        checks = []
    engine_code = (props_mod.render_pytest(impl_stem, checks, target_rel=impl_rel)
                   if checks else "")

    for rel in targets:
        p = ws / rel
        if not p.exists():
            continue
        why = _test_file_static_broken(p, impl_rel, impl_symbols)
        if not why:
            unchanged.append(rel)
            continue
        reasons.append(f"{rel}: {why}")
        # ① 外科手术：删掉坏 import 行
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        cleaned_lines = []
        for line in text.splitlines():
            stripped = line.strip()
            bad = False
            if re.match(rf"from\s+{re.escape(p.stem)}\s+import\b", stripped):
                bad = True                       # 自我导入
            m = re.match(rf"from\s+{re.escape(impl_stem)}\s+import\s+([\w\s,]+)$", stripped)
            if m:
                names = [x.strip().split(" as ")[0].strip() for x in m.group(1).split(",")]
                miss = [n for n in names if n and n != "*" and n not in impl_symbols]
                if miss:
                    # 只删坏的那几个名字，好的留着
                    keep = [n for n in names if n not in miss]
                    if keep:
                        indent = line[: len(line) - len(line.lstrip())]
                        cleaned_lines.append(f"{indent}from {impl_stem} import {', '.join(keep)}")
                    bad = True
            if not bad:
                cleaned_lines.append(line)
        cleaned = "\n".join(cleaned_lines).strip() + "\n"
        has_test = bool(re.search(r"^\s*def\s+test_", cleaned, re.M))
        parses = _parses_src(cleaned)
        if parses and has_test:
            p.write_text(cleaned, encoding="utf-8")
            repaired.append(rel)
            emit("test_file_repaired", {"node": node["nid"], "path": rel, "reason": why,
                                        "how": "删掉坏 import", "tests": _count_tests(cleaned)})
            continue
        # ② 救不回来 → 引擎判据重渲染
        if engine_code:
            p.write_text(engine_code, encoding="utf-8")
            rewritten.append(rel)
            emit("test_file_repaired", {"node": node["nid"], "path": rel, "reason": why,
                                        "how": "引擎判据重渲染", "checks": len(checks)})
            continue
        # ③ 最后一档：写一个最小可用测试（保证"有测试"这件事不被抹掉）
        minimal = _minimal_test(impl_stem, impl_symbols, need)
        p.write_text(minimal, encoding="utf-8")
        rewritten.append(rel)
        emit("test_file_repaired", {"node": node["nid"], "path": rel, "reason": why,
                                    "how": "最小测试（符号存在且可调用）"})
    return {"repaired": repaired, "rewritten": rewritten, "unchanged": unchanged,
            "reasons": reasons}


def _count_tests(text: str) -> int:
    return len(re.findall(r"^\s*def\s+test_", text or "", re.M))


def _parses_src(text: str) -> bool:
    try:
        import ast as _ast
        _ast.parse(text)
        return True
    except SyntaxError:
        return False


def _minimal_test(impl_stem: str, symbols: set[str], contract: str) -> str:
    """最小可用测试：不猜行为，只钉住"符号存在且可调用"，防止测试被悄悄抹掉。"""
    fns = []
    for m in re.finditer(r"([A-Za-z_]\w*)\s*\(", contract or ""):
        name = m.group(1)
        if name in symbols and name not in fns:
            fns.append(name)
    fns = fns[:3] or sorted(symbols)[:3]
    lines = ["import inspect", "", f"import {impl_stem} as _mod", "", ""]
    for i, name in enumerate(fns):
        lines += [f"def test_symbol_{i}_{name}_exists():",
                  f"    assert callable(getattr(_mod, {name!r}, None)), {name!r}", ""]
    if not fns:
        lines += ["def test_module_imports():", f"    assert _mod is not None", ""]
    return "\n".join(lines)


def _missing_symbols(text: str, ws: Path | None = None, impl_rel: str = "",
                     contract: str = "") -> list[str]:
    """从失败输出里抽出"真的缺的符号"，变成下一个待办（satisfy-import）。

    实测事故（链路自检反复失败的真实根因之一）：测试里写了 `divide(a, b)`（自由变量），
    pytest 报 `NameError: name 'a' is not defined`，旧逻辑把它当成"缺符号 a"，
    于是提示模型"必须提供这些符号：a、b"，模型就往测试文件里加
    `from calc import a` / `from test_calc import test_divide` —— 自己 import 自己，
    级联成 ImportError，远端再聪明也救不了（因为坏的是测试文件，不是实现）。

    两道闸：
    1. 名字必须以**函数调用**或**类构造**的形式出现在契约/测试里（`a(`/`b(` 不是符号）；
    2. 给了工作区就再核一遍：该名字必须真的在目标模块的符号表里。
    """
    out: list[str] = []
    known: set[str] = set()
    if ws is not None and impl_rel:
        try:
            known = props.module_symbols(ws, impl_rel)
        except Exception:                                # noqa: BLE001
            known = set()
    for m in MISSING_RE.finditer(text or ""):
        name = m.group(1) or m.group(3)
        mod = m.group(2) or m.group(4) or ""
        if not name:
            continue
        # 闸 1：只有在契约里被当函数/类用过，或者本来就是模块名，才算"符号"
        looks_symbolic = bool(re.search(rf"\b{re.escape(name)}\s*\(", contract or ""))
        if not looks_symbolic and name not in known:
            continue
        # 闸 2：工作区里能核对时，名字必须真的存在于实现模块
        if known and name not in known:
            continue
        tag = f"{mod}.{name}" if mod else name
        if tag not in out:
            out.append(tag)
    return out[:6]


def _symedit_ctx(source: str, contract: str, limit: int = 2000) -> tuple[str, str] | None:
    """构建符号清单 + 待改符号现状。返回 None 表示该文件不适合符号级编辑。"""
    syms = symedit.outline(source)
    if not syms:
        return None
    outline = "\n".join(f"- {s.signature()}  [行 {s.lineno}-{s.endline}]"
                        + (f"  # {s.doc}" if s.doc else "") for s in syms[:40])
    # 目标优先级：骨架桩 > 契约点名 > 前两个函数
    # （实测：模型会去加新符号，却不填骨架里那些 raise NotImplementedError 的桩）
    stub_names = set()
    try:
        for n in ast.walk(ast.parse(source)):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and gates._is_stub(n):
                stub_names.add(n.name)
    except SyntaxError:
        pass
    stubs = [s for s in syms if s.kind == "function" and s.name in stub_names]
    named = [s for s in syms
             if re.search(rf"\b{re.escape(s.name)}\b", contract) and s.kind == "function"]
    targets = (stubs or named or [s for s in syms if s.kind == "function"])[:2]
    blocks, used = [], 0
    for s in targets:
        body = symedit.source_of(source, s.qualname)
        if not body:
            continue
        if used + len(body) > limit:
            body = body[:max(0, limit - used)]
        blocks.append(f"### {s.qualname}\n{body}")
        used += len(body)
    head = ""
    if stub_names:
        head = ("【必须填实的骨架桩】以下函数现在只 raise NotImplementedError / 只有 pass，"
                "请用 replace 给出真实实现：" + "、".join(sorted(stub_names)[:6]) + "\n\n")
    return outline, head + ("\n\n".join(blocks) if blocks else "（没有匹配到现有符号，可能是新增）")


def _generate(ctx: dict, model: str, temperature: float, feedback: str) -> dict:
    """补丁生成。symbol 走符号级编辑（libcst 渲染）；patch 走 JSON ops；whole 走整文件生成。"""
    fb = f"\n【上次失败原因，必须修正】\n{feedback}\n" if feedback else ""
    if ctx["mode"] == "symbol":
        prompt = SYMEDIT_PROMPT.format(
            contract=ctx["contract"], path=ctx["path"], symbols=ctx["symbols"],
            targets=ctx["targets"], conventions=ctx.get("conventions") or CONVENTIONS,
            apifacts=ctx.get("apifacts") or "（无）",
            tests=ctx.get("tests") or "（暂无配对测试）", feedback=fb)
        cached = llm_cache.get(model, temperature, prompt)
        if cached:
            return {"text": cached, "ms": 0, "eval_count": 0, "cached": True}
        res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                          temperature=temperature, num_predict=700)
        llm_cache.put(model, temperature, prompt, res.get("text", ""))
        return res
    if ctx["mode"] == "patch":
        prompt = PATCH_PROMPT.format(
            contract=ctx["contract"], path=ctx["path"], nlines=len(ctx["content"].splitlines()),
            content=ctx["content"], conventions=ctx.get("conventions") or CONVENTIONS,
            anchors=ctx.get("anchors", ""), apifacts=ctx.get("apifacts") or "（无）",
            feedback=fb)
        cached = llm_cache.get(model, temperature, prompt)
        if cached:
            return {"text": cached, "ms": 0, "eval_count": 0, "cached": True}
        res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                          temperature=temperature, num_predict=600)
        llm_cache.put(model, temperature, prompt, res.get("text", ""))
        return res
    # 新建文件（impl/whole）：整文件生成。不要设 stop=["```"]（会把开头的围栏当结束符）
    prompt = NEW_FILE_PROMPT.format(
        contract=ctx["contract"], path=ctx["path"],
        api=ctx.get("api") or "（仓库里还没有其他模块）",
        conventions=CONVENTIONS, feedback=fb)
    return ollama.chat([{"role": "user", "content": prompt}], model,
                       temperature=temperature, num_predict=1400)


# ---------------------------------------------------------------- 节点执行

def _strip_ln_block(text: str) -> str:
    """去掉模型可能抄进去的行号前缀，如 `   4| def f():` → `def f():`。"""
    out = []
    for line in text.split("\n"):
        m = re.match(r"^[ \t]*\d+[ \t]*\|[ ]?(.*)$", line)
        out.append(m.group(1) if m else line)
    return "\n".join(out)


def _find_function(text: str, name: str) -> tuple[int, int]:
    """返回函数在文件中的 0-based 行区间 [start, end)（含装饰器）。"""
    import ast as _ast
    try:
        tree = _ast.parse(text)
    except SyntaxError as exc:
        raise tools.ToolError(f"文件解析失败：{exc.msg}") from exc
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name == name:
            start = node.decorator_list[0].lineno - 1 if node.decorator_list else node.lineno - 1
            return start, node.end_lineno
    raise tools.ToolError(f"未找到函数 `{name}`。现有函数：{[n.name for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))]}")


def _apply_ops(ws: Path, rel: str, base: str, ops: list[dict]) -> str:
    """符号级操作：append_code / replace_function / insert_after / patch。

    为什么不只用文本窗口：3B 复制含中文的原文经常出错、也常选到不唯一的锚点；
    按函数名操作是确定性的，不需要"精确复制"。
    """
    text = base
    for op in ops:
        kind = str(op.get("op") or op.get("kind") or "").lower()
        code = str(op.get("code") or "").rstrip("\n")
        if kind in ("append", "append_code", "append_function"):
            if not code.strip():
                raise tools.ToolError("append_code 缺少 code 字段")
            text = text.rstrip("\n") + "\n\n\n" + code + "\n"
        elif kind in ("replace_function", "replace_func", "replace"):
            name = op.get("name")
            if not name:
                if op.get("search"):
                    text = tools.apply_patch_text(text, op["search"], op.get("replace", ""), rel)
                    continue
                raise tools.ToolError("replace_function 需要 name 字段")
            start, end = _find_function(text, name)
            lines = text.splitlines()
            text = "\n".join(lines[:start] + code.splitlines() + lines[end:]) + "\n"
        elif kind in ("insert_after", "append_after_function"):
            name = op.get("name")
            if not name:
                raise tools.ToolError("insert_after 需要 name 字段")
            _start, end = _find_function(text, name)
            lines = text.splitlines()
            text = "\n".join(lines[:end] + [""] + code.splitlines() + lines[end:]) + "\n"
        elif kind in ("patch", "edit", "replace_text"):
            text = tools.apply_patch_text(text, op.get("search", ""), op.get("replace", ""), rel)
        else:
            raise tools.ToolError(f"未知 op `{kind}`（支持 append_code / replace_function / insert_after / patch）")
    return text


def _parse_edits(text: str) -> list[dict]:
    """优先解析 JSON ops/edits 协议；不成立则回退到 SEARCH/REPLACE 文本块。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json|python)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    data = None
    candidates = [t]
    m2 = re.search(r"\{.*\}", t, re.DOTALL)
    if m2:
        candidates.append(m2.group(0))
    for cand in candidates:
        try:
            data = json.loads(cand)
            break
        except Exception:
            data = None
    if isinstance(data, dict):
        for key in ("ops", "edits", "patches", "changes"):
            raw = data.get(key)
            if isinstance(raw, list) and raw:
                out = []
                for e in raw:
                    if not isinstance(e, dict):
                        continue
                    if e.get("op") or e.get("kind"):
                        out.append(e)
                        continue
                    s = e.get("search") or e.get("SEARCH") or e.get("old") or ""
                    r = e.get("replace") or e.get("REPLACE") or e.get("new") or ""
                    if s:
                        out.append({"op": "patch", "search": s, "replace": r})
                if out:
                    return out
    # 兜底：旧文本块协议
    return [{"op": "patch", **pt} for pt in tools.parse_patches(text)]


PLACEHOLDER_SYMBOLS = {"new_fn", "foo", "bar", "baz", "your_func", "new_function",
                       "func_name", "symbol", "<符号限定名>", "<函数名>"}


def _has_def(source: str, qualname: str) -> bool:
    """符号是否真的在文件里（决定 replace 还是退化成 add）。"""
    try:
        import libcst as _cst
        tree = _cst.parse_module(source)
        return symedit.find_def(tree, qualname) is not None
    except Exception:                                 # noqa: BLE001
        return True                                   # 解析不了就别改语义，交给渲染器报错


def _apply_symbol_edits(base: str, ops: list[dict]) -> tuple[str, list[str]]:
    """把模型的符号编辑交给 libcst 渲染器；失败抛 ToolError（交给窄反馈重试）。"""
    edits: list[dict] = []
    for op in ops:
        kind = str(op.get("op") or op.get("kind") or "replace").lower()
        symbol = str(op.get("symbol") or op.get("name") or "").strip()
        code = str(op.get("code") or "").strip("\n")
        if not symbol or not code:
            raise tools.ToolError("符号级编辑缺少 symbol 或 code 字段")
        if symbol in PLACEHOLDER_SYMBOLS:
            # 模型把提示词里的示例名当成了真名（实测它会往 storage.py 里加一个 new_fn）。
            # 直接跳过这条编辑，而不是让整次尝试作废——其它编辑通常是对的。
            emit_note = f"跳过占位符编辑 {symbol}"
            continue
        if kind in ("add", "append", "append_code", "insert_after"):
            edits.append({"op": "add", "symbol": symbol, "code": code,
                          "after": str(op.get("after") or "")})
        elif kind in ("replace", "replace_function", "replace_func", "edit"):
            # 目标符号不存在时退化为"新增"：实测模型会照契约写 `replace Transaction`，
            # 而骨架文件里根本没有这个符号，于是三次都"定位失败"白白浪费。
            if not _has_def(base, symbol):
                edits.append({"op": "add", "symbol": symbol, "code": code, "after": ""})
            else:
                edits.append({"op": "replace", "symbol": symbol, "code": code})
        else:
            raise tools.ToolError(f"符号级编辑不支持 op `{kind}`")
    rep = symedit.apply_edits(base, edits)
    if not rep.ok:
        raise tools.ToolError(f"符号级编辑失败：{rep.reason}")
    return rep.new_source, rep.applied


def _apply(ws: Path, node: dict, text: str,
           emit: Emitter | None = None) -> dict[str, tuple[str, str]]:
    """返回 {rel: (base_text, new_text)}；失败抛 ToolError。"""
    rel = node["path"]
    p = tools.safe_path(ws, rel)
    base = p.read_text(encoding="utf-8") if p.exists() else ""

    ops = _parse_edits(text) if (text or "").strip() else []

    # 符号级编辑：模型只给符号定义，文件由渲染器保真生成（H2 机制）
    # 必须先于 whole 分支判断：impl 节点整体是 whole 模式，但符号模式命中时应走这里
    symbol_ops = [o for o in ops if o.get("symbol") and o.get("code")]
    if node.get("stage") == "fill" and not symbol_ops:
        # 硬约束①：填桩节点只许走保真渲染。文本 patch 在文件被写残后会永久失配（实测）
        raise tools.ToolError(
            "填桩节点只接受符号级编辑（{\"edits\":[{\"op\":\"replace\",\"symbol\":…,\"code\":…}]}），"
            "不接受 SEARCH/REPLACE 或整文件输出")
    if symbol_ops:
        if node.get("kind") == "test":
            raise tools.ToolError("测试节点不得用符号替换改写既有测试；请用 add + after 追加")
        new, applied = _apply_symbol_edits(base, symbol_ops)
        if emit:
            emit("symedit", {"node": node.get("nid"), "path": rel, "applied": applied,
                             "kept_bytes": len(base), "new_bytes": len(new)})
        return {rel: (base, new)}

    if node["mode"] == "whole":
        new = _strip_fence(text).strip() + "\n"
        if not new.strip():
            raise tools.ToolError("模型返回空内容")
        return {rel: (base, new)}

    if not ops:
        raise tools.ToolError(
            "未解析到补丁（既不是 JSON ops/edits，也不是 SEARCH/REPLACE 块）。原始输出前 200 字："
            + (text or "")[:200].replace("\n", "\\n"))

    # 测试节点只允许「追加新测试」：避免模型改坏既有测试（实测它会用 replace_function 覆写 test_sub）
    if node["kind"] == "test":
        appends = [o for o in ops if str(o.get("op", "")).lower().startswith(("append", "insert_after"))]
        if not appends:
            raise tools.ToolError(
                "测试节点只允许用 append_code 追加新测试函数；不得修改或替换既有测试。"
                f"本次给出的 op：{[o.get('op') for o in ops]}")
        ops = appends

    try:
        new = _apply_ops(ws, rel, base, ops)
    except tools.ToolError:
        # 兜底：模型把行号抄进了 search —— 去掉行号前缀再试一次
        cleaned = [dict(o, search=_strip_ln_block(o.get("search", ""))) for o in ops]
        if cleaned == ops:
            raise
        new = _apply_ops(ws, rel, base, cleaned)
    return {rel: (base, new)}


class _SkipGate(Exception):
    """内部用：这个闸门对当前栈不适用，跳过（不是失败，也不是通过）。

    沿用既有的 try/except 结构来跳过，避免把三处调用改成嵌套 if
    （那会显著增加这块已经很密的代码的复杂度）。
    """


def _paired_tests(test_files: list[str], node_path: str) -> list[str]:
    """只跑与这个节点配对的测试文件，避免多模块项目里互相污染。

    例：节点 models.py 只跑 test_models.py；找不到配对时才退回全部。
    """
    stem = Path(node_path).stem
    paired = [t for t in test_files
              if re.sub(r"^test_|_test$", "", Path(t).stem) == stem]
    return paired or list(test_files)


def _stack_gate_skip(ws: Path, emit: Emitter) -> set[str]:
    """这个项目不该跑哪些闸门（阶段 3，2026-11-24）。

    以前 _gate_node 是硬编码的闸门序列。其中 API / SQL行 / 安全 三个
    假设"这是个 HTTP 服务"。CLI 项目跑它们必然假红，用户看到的是
    "你写错了"，其实是引擎不认识这种项目。

    用 core.judges 的栈识别，把这些栈相关闸门按 applies_to 过滤掉。
    栈无关的闸门（语法/桩/导入…）照跑。

    **跳过不等于通过**：跳过会 emit 一条 notice 说明原因，
    且不产生 ok=True 的闸门记录 —— 后者会污染"这份报告证明了什么"。
    """
    try:
        from . import judges as _J
        stack = _J.project_stack(ws)
        applicable = {j.key for j in _J.judges_for_project(ws)[1]}
    except Exception as exc:                          # noqa: BLE001
        emit("notice", {"message": f"栈识别失败，按全量闸门跑：{exc}"})
        return set()

    web_only = {"api": "gate_api", "db_rows": "gate_db_rows", "security": "gate_security"}
    skipped = {fn for key, fn in web_only.items() if key not in applicable}

    if skipped:
        emit("notice", {
            "message": (f"栈={stack}：跳过 {len(skipped)} 个 Web 专用闸门"
                        f"（{", ".join(sorted(skipped))}）"
                        "—— 对当前项目不适用，跳过不等于通过"),
        })
    return skipped


def _gate_node(ws: Path, node: dict, changed: dict[str, tuple[str, str]],
               test_files: list[str], attempt: int, emit: Emitter,
               model: str = "") -> tuple[bool, str, list]:
    """跑该节点的闸门。返回 (ok, 反馈, 闸门结果列表)。"""
    rels = list(changed.keys())
    results = []
    scoped = _paired_tests(test_files, node["path"])
    _skip = _stack_gate_skip(ws, emit)

    def run(r: gates.GateResult, force_ok: bool | None = None) -> gates.GateResult:
        ok = r.ok if force_ok is None else force_ok
        db.record_gate(node["task_id"], node["nid"], attempt,
                       r.gate, ok, r.evidence_kind, r.evidence_ref, r.detail, r.elapsed_ms)
        emit("gate", {"node": node["nid"], "gate": r.gate, "ok": ok,
                      "evidence_kind": r.evidence_kind, "ref": r.evidence_ref,
                      "detail": r.detail, "ms": r.elapsed_ms})
        results.append((r.gate, ok, r.detail))
        return r

    # G1 语法
    r = gates.gate_syntax(ws, rels)
    run(r)
    if not r.ok:
        return False, f"【G1 语法】{r.detail}", results

    # G3 类型（mypy；缺失时如实标注跳过）。测试节点阶段把"实现还没到位"的噪音压成一行摘要。
    tr = gates.gate_types(ws, rels, node_kind=node.get("kind") or "")
    run(tr)

    # 契约签名 + 桩 + API：三条纯机械判定，**批量收集**而不是遇错即返回。
    # 教训：fail-fast 每轮只回传一条失败，3 次尝试只修 3 个问题（实测反复改正的主因）。
    static_problems: list[str] = []
    sig_text = node.get("contract") or ""
    try:
        rows = db.query("SELECT requirement FROM tasks WHERE id=?", (node.get("task_id"),))
        if rows:
            sig_text += "\n" + (rows[0].get("requirement") or "")
    except Exception:                             # noqa: BLE001
        pass
    cr = gates.gate_completeness(ws, rels, sig_text)
    run(cr)
    if not cr.ok:
        static_problems.append(f"[完整性] {cr.detail}")
    sr = gates.gate_signature(ws, rels, sig_text)
    run(sr)
    if not sr.ok:
        static_problems.append(f"[签名] {sr.detail}")
    stb = gates.gate_stub(ws, rels)
    run(stb)
    if not stb.ok:
        static_problems.append(f"[桩] {stb.detail}")
    try:
        if "gate_api" in _skip:
            raise _SkipGate("api")
        ar = gates.gate_api(ws, rels)
        run(ar)
        if not ar.ok:
            static_problems.append(f"[API] {ar.detail}")
    except _SkipGate:
        pass  # 栈不适用，已在 _stack_gate_skip 里 emit 过原因
    except Exception as exc:                      # noqa: BLE001
        emit("notice", {"message": f"API 闸门跳过：{exc}"})
    try:
        if "gate_db_rows" in _skip:
            raise _SkipGate("db_rows")
        dr = gates.gate_db_rows(ws, rels)
        run(dr)
        if not dr.ok:
            static_problems.append(f"[SQL行] {dr.detail}")
    except _SkipGate:
        pass
    except Exception as exc:                      # noqa: BLE001
        emit("notice", {"message": f"SQL行闸门跳过：{exc}"})
    try:
        dup = gates.gate_duplicates(ws, rels)
        run(dup)
        if not dup.ok:
            static_problems.append(f"[重名] {dup.detail}")
    except Exception as exc:                      # noqa: BLE001
        emit("notice", {"message": f"重名闸门跳过：{exc}"})
    try:
        # 导入闸门：注解在运行时求值，少一行 import 就是"导入即 NameError"；
        # pytest 只会报 collected error，看不出根因（实测 night8/r2 都栽在这）。
        imp = gates.gate_import_ok(ws, rels)
        run(imp)
        if not imp.ok:
            static_problems.append(f"[导入] {imp.detail}")
    except Exception as exc:                      # noqa: BLE001
        emit("notice", {"message": f"导入闸门跳过：{exc}"})
    try:
        if "gate_security" in _skip:
            raise _SkipGate("security")
        se = gates.gate_security(ws, rels)
        run(se)
        if not se.ok:
            static_problems.append(f"[安全] {se.detail}")
    except _SkipGate:
        pass
    except Exception as exc:                      # noqa: BLE001
        emit("notice", {"message": f"安全闸门跳过：{exc}"})
    if static_problems:
        # 静态问题全量回传（按可机械满足的顺序：签名 → 桩 → API）
        return False, "【静态闸门·全部问题】\n" + "\n".join(static_problems[:6]), results

    # Q9 反逃避
    r = gates.gate_anti_cheat(ws, node["kind"], changed)
    run(r)
    if not r.ok:
        return False, f"【Q9 反逃避】{r.detail}", results

    # ---- 按技术栈跑语言判据（2026-11-24 接入）----
    # 放在反逃避之后：前面那些闸门查的是"改动本身有没有作弊/桩/缺符号"，
    # 这一步查的是"这个语言的代码到底能不能过" —— 它要跑编译器/测试，最慢。
    # 不进行硬中断：语言判据可能因工程结构特殊而无法运行，
    # 判据自己会返回 ok=False + 原因，这里让它走统一的 run() 记录与回传。
    try:
        from . import judges as _J
        _ctx = _J.JudgeContext(
            ws=ws,
            paths=tuple(rels),
            changed=changed,
            contract=node.get("contract") or "",
            node_kind=node.get("kind") or "",
            extras={"timeout": 180, "test_timeout": 300},
        )
        _reg = _J.full_registry()
        _tc = None
        for _j in ("lang.typecheck", "lang.tests"):
            _judge = _reg.get(_j)
            if _judge is None:
                continue
            _lr = _judge.check(_ctx)
            run(_lr)
            if _lr.gate == "lang.typecheck":
                _tc = _lr
        # 只有 typecheck 失败才阻塞（测试失败由后面的 gate_tests 统一处理）
        if _tc is not None and not _tc.ok:
            return False, f"【语言判据·类型/语法】{_tc.detail}", results
    except Exception as exc:                      # noqa: BLE001
        emit("notice", {"message": f"语言判据跳过：{type(exc).__name__}: {exc}"})

    # 断言有效性（只看与本节点配对的测试文件）
    if scoped:
        r = gates.gate_assertions(ws, scoped)
        run(r)
        if not r.ok:
            return False, f"【断言有效性】{r.detail}", results

    # 风格/一致性偏离（只报事实，不阻塞）
    try:
        issues = style.check(ws, changed)
        run(gates.GateResult("style", True, "static_rule",
                             f"偏离 {len(issues)} 处" if issues else "ok",
                             "；".join(issues[:5]) or "与项目规约一致"))
    except Exception as exc:
        emit("notice", {"message": f"风格检查跳过：{exc}"})

    # 引擎判据 + 历史回归用例（示例/性质）：不依赖模型自写测试；无配对测试时这是唯一行为验证
    try:
        from . import props as props_mod
        from . import regression as regr_mod
        syms = props_mod.module_symbols(ws, node["path"])
        req_text = (node.get("contract") or "") + "\n" + _task_requirement(node)
        try:
            from . import contract as contract_mod
            _c = contract_mod.parse_body(node["path"], node.get("contract") or "")
            # 示例可能写在需求末尾：合并全篇需求的示例归属
            try:
                _all = contract_mod.parse_requirement(_task_requirement(node))
                if node["path"] in _all:
                    for _f in _all[node["path"]].funcs:
                        _mine = _c.by_name(_f.name)
                        if _mine is not None and _f.examples:
                            _have = {(tuple(e.args), e.expect) for e in _mine.examples}
                            for _e in _f.examples:
                                if (tuple(_e.args), _e.expect) not in _have:
                                    _mine.examples.append(_e)
            except Exception:
                pass
            checks = props_mod.from_contract(_c)
        except Exception:
            checks = props_mod.from_requirement(req_text, syms)
        # 类型冒烟：按注解生成样例参数调用一次，抓"函数体假设 str 却收到 int"这类错误
        try:
            _src = (ws / node["path"]).read_text(encoding="utf-8-sig", errors="replace")
            _excl = set()
            try:
                import re as _re
                _excl = {m.group(1) for m in _re.finditer(
                    r"([A-Za-z_]\w{2,})\s*\([^)]*\)[^。\n]{0,12}?(?:应|必须|需要)?\s*抛", req_text)}
            except Exception:
                pass
            if attempt >= 2:      # 阶段化：探索期不跑，避免开工即死
                checks = checks + props_mod.typed_checks(_src, exclude=_excl)
        except Exception:                          # noqa: BLE001
            pass
        # 并入历史回归用例：同一需求+同一模块，远端/人工成功过的判据每次都要过
        try:
            reg = regr_mod.for_task(_task_requirement(node), node["path"])
            if reg:
                seen = {json.dumps(c, sort_keys=True) for c in checks}
                for c in reg:
                    k = json.dumps(c, sort_keys=True)
                    if k not in seen:
                        checks.append(c)
                        seen.add(k)
                emit("regression", {"node": node["nid"],
                                    "applied": len(reg),
                                    "total_checks": len(checks)})
        except Exception:                 # noqa: BLE001
            pass
        if checks:
            pr = props_mod.run(ws, node["path"], checks)
            fb = props_mod.as_feedback(pr)
            emit("props", {"node": node["nid"], "checks": [c["desc"] for c in checks][:6],
                           "ok": pr["ok"], "passed": pr.get("passed"),
                           "total": pr.get("total")})
            db.record_gate(node["task_id"], node["nid"], attempt, "props",
                           pr["ok"], "assertion", "engine-oracle", fb)
            if not pr["ok"]:
                return False, f"【引擎判据】{fb}", results
    except Exception as exc:          # noqa: BLE001
        emit("notice", {"message": f"引擎判据跳过：{exc}"})

    # G5 测试：测试节点要求「红」，实现节点要求「绿」
    if scoped:
        r = gates.gate_tests(ws, scoped)
        if node["kind"] == "test":
            if r.ok:
                detail = "测试在实现之前就通过 —— 视为无效测试（先红后绿失败）"
                ok = False
            else:
                meaningful, why = gates.red_is_meaningful(r.detail)
                if meaningful:
                    ok = True
                    detail = "测试如期失败（红），符合先红后绿"
                else:
                    ok = False
                    detail = why
            run(gates.GateResult("tests", ok, "exit_code", r.evidence_ref, detail, r.elapsed_ms))
            if not ok:
                return False, f"【先红后绿】{detail}\n{r.detail}", results
        else:
            run(r)
            if not r.ok:
                # B：先判断"是不是测试文件自己坏了"。测试文件坏 = 换谁来改实现都没用，
                # 必须把修复目标切到测试文件（引擎重渲染），而不是继续改实现。
                broken = _test_file_self_broken(r.detail, scoped)
                if broken:
                    node["_test_file_broken"] = broken
                    return (False,
                            f"【G5 测试】测试文件自身坏了（不是实现的问题）：{broken}\n"
                            f"下一步会先用引擎判据重渲染测试文件，再继续。", results)
                miss = _missing_symbols(r.detail, ws, _impl_rel_of(node), node.get("contract") or "")
                if miss:
                    node["_missing"] = miss
                    extra = ("\n【下一步必须做】测试因缺少符号而失败，请在本文件里补上："
                             + "、".join(miss))
                else:
                    extra = ""
                return False, f"【G5 测试】{gates.narrow_feedback(r.detail)}{extra}", results
            # Q6 变异测试（只对实现节点）
            if node["kind"] in ("impl", "create"):
                mut = gates.gate_mutation(ws, changed, scoped)
                run(mut)
                if not mut.ok:
                    return False, f"【Q6 变异测试】{mut.detail}", results

                # M1 参照实现差分（可选）：用一份"笨但正确"的实现做对撞
                if _ref_enabled() and model:
                    try:
                        from . import reference
                        ref_rel = node.get("_ref_rel") or ""
                        if not ref_rel:
                            ref_rel, rres = reference.build_reference(
                                ws, node["path"], node.get("contract") or "", model)
                            node["_ref_rel"] = ref_rel
                            emit("reference", {"node": node["nid"], "ref": ref_rel,
                                               "ms": rres.get("ms", 0),
                                               "skipped": rres.get("skipped", "")})
                        if ref_rel and (ws / ref_rel).exists():
                            src = (ws / node["path"]).read_text(encoding="utf-8-sig", errors="replace")
                            pure = reference._pure_funcs(src)
                            funcs = [f["name"] for f in pure]
                            res = reference.differential(ws, node["path"], ref_rel,
                                                         reference.gen_cases(pure), funcs)
                            emit("differential", {"node": node["nid"], "ref": ref_rel,
                                                  "ok": res["ok"], "checked": res["checked"],
                                                  "divergences": res["divergences"][:3],
                                                  "error": (res.get("error") or "")[:200]})
                            fb = reference.as_feedback(res)
                            db.record_gate(node["task_id"], node["nid"], attempt,
                                           "differential", res["ok"], "assertion",
                                           "ref-diff", fb)
                            if not res["ok"]:
                                return False, f"【参照差分】{fb}", results
                    except Exception as exc:          # noqa: BLE001
                        emit("notice", {"message": f"参照差分跳过：{exc}"})

    return True, "", results


def _regen_tests(ws: Path, test_node: dict, model: str, feedback: str) -> dict | None:
    """测试规格返工：把「实现节点跑测试失败」的输出当反馈，重新生成测试规约并重渲染。

    为什么需要：3B 常把异常用例的参数填成合法值（例：clamp 的 lo>hi 用例写成 (1,0,5)），
    于是实现怎么改都过不了。正确做法是**回到测试节点重做**，而不是让实现节点去改测试
    （那样会破坏既有测试保护，被 anti_cheat 拦住）。
    """
    rel = test_node["path"]
    base = test_node.get("_baseline", "")
    ctx = {"kind": "test", "mode": "patch", "path": rel,
           "contract": test_node["contract"], "content": base or "（新建）",
           "anchors": _anchors(base)}
    spec, res = _generate_test_spec(ctx, model, 0.4, feedback)
    if not spec:
        return None
    spec, fixed_notes = oracle.fix_spec(ws, spec)
    try:
        new_text, rendered = _apply_test_spec(ws, rel, base, spec)
    except tools.ToolError as exc:
        return {"error": str(exc)}
    tools.write_file(ws, rel, new_text)
    db.save_spec(test_node.get("task_id") or "", test_node["nid"],
                 {k: v for k, v in spec.items() if not k.startswith("_")})
    return {"spec": {k: v for k, v in spec.items() if not k.startswith("_")},
            "code": rendered, "ms": res["ms"], "oracle_fixes": fixed_notes}


def _run_node(ws: Path, task_id: str, node: dict, model: str,
              test_files: list[str], emit: Emitter, baseline: str | None = None) -> dict:
    node["task_id"] = task_id
    rel = node["path"]
    p = tools.safe_path(ws, rel)
    # 基线必须来自「本任务开始前」的内容：重跑同一节点时不能把上一次的产物当基线，
    # 否则 append 型 op 会一层层叠加（实测出现两个 clamp 定义）
    if baseline is not None:
        orig_text = baseline
        # 基线为空 = 本任务开始时该文件还不存在：重试时要删除而不是写成空文件
        orig_exists = baseline != ""
    else:
        orig_exists = p.exists()
        orig_text = p.read_text(encoding="utf-8") if orig_exists else ""
    # 接口先行：impl 节点面对空文件/纯 stub 时，先让模型只产出签名，骨架由引擎渲染。
    # （实测：3B 写整文件 80-120 行稳定失败，写签名 + 单函数体可靠得多）
    if (node["kind"] == "impl" and node.get("stage") != "fill"
            and (not orig_exists or skeleton.is_skeleton(orig_text))):
        _ctx0 = {"kind": node["kind"], "mode": node["mode"],
                 "contract": _module_local_contract(node["contract"], rel),
                 "path": rel, "api": planner.api_surface(ws, exclude=rel)}
        try:
            sigs, sres = _generate_signatures(_ctx0, model, 0.2)
        except Exception as exc:
            sigs, sres = [], {"ms": 0, "eval_count": 0, "error": str(exc)}
        if sigs:
            body = skeleton.render_skeleton(rel, sigs, node["contract"])
            tools.write_file(ws, rel, body, backup=False)
            orig_text, orig_exists = body, True
            emit("skeleton", {"node": node["nid"], "path": rel,
                              "functions": [sg.name for sg in sigs],
                              "ms": sres.get("ms", 0), "cached": sres.get("cached", False)})

    # 上下文按预算裁剪：大文件不再整段注入（实测整段注入会吃掉 3.5K 预算的大半，
    # 模型反而看不到要改的函数）。保留 import + 目标函数实现 + 其它符号签名。
    view = context.build(rel, orig_text, node["contract"], budget_tokens=1200)
    if view.trimmed:
        emit("context", {"node": node["nid"], "path": rel, "trimmed": True,
                         "approx_tokens": view.approx_tokens, "chars": view.chars})
    ctx = {"kind": node["kind"], "mode": node["mode"], "contract": node["contract"], "path": rel,
           "content": view.content or "（文件不存在，将新建）",
           "anchors": _anchors(orig_text),
           # 新建/改动文件时给模型"已有模块的公开签名"，避免 import 不存在的符号
           "api": planner.api_surface(ws, exclude=rel),
           "apifacts": _api_facts(node["contract"]),
           "requirement": _task_requirement(node),
           "tests": (_tests_text(ws, test_files, rel)
                     if node["kind"] != "test" else ""),
           "allowed": _contract_symbols(ws, rel, node["contract"]) if node["kind"] == "test" else set(),
           "conventions": _style_text(ws)}
    # 符号级编辑模式：文件已存在且可解析时，模型只产出符号定义，文件由渲染器保真生成
    # （不限 mode：impl 节点是整文件模式，但骨架铺好后目标文件已存在，正是最该用符号编辑的场景）
    if node["kind"] != "test" and orig_exists and _symedit_enabled():
        sc = _symedit_ctx(orig_text, node.get("target_func") or node["contract"])
        if sc:
            targets_txt = sc[1]
            missing = node.get("_missing") or []
            if missing:
                targets_txt = ("【测试导入失败，必须提供这些符号】" + "、".join(missing)
                               + "\n\n" + targets_txt)
            ctx = dict(ctx, mode="symbol", symbols=sc[0], targets=targets_txt)

    # 节点可能是重跑（测试规格返工），先清该节点的闸门记录并 upsert，避免主键冲突
    db.execute("DELETE FROM gate_runs WHERE node_id=?", (node["nid"],))
    db.execute("INSERT INTO nodes(id,task_id,seq,kind,path,contract,status,attempts,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)"
               " ON CONFLICT(id) DO UPDATE SET status='pending', attempts=0,"
               " last_error=NULL, patch=NULL", (node["nid"], task_id, node["seq"], node["kind"], rel,
                                                node["contract"], "pending", 0, db.now()))
    # 测试节点：记下加入该节点之前的文件内容，供「测试规格返工」还原
    node["_baseline"] = orig_text
    node["_baseline_exists"] = orig_exists
    emit("node_start", {"node": node["nid"], "kind": node["kind"], "path": rel,
                        "mode": node["mode"], "contract": node["contract"]})

    feedback = ""
    cands: list[dict] = []
    _budget = budget.plan(node, node.get("contract", ""), 0)
    emit("budget", {"node": node["nid"], "attempts": _budget.attempts,
                    "why": _budget.why, "use_strong": _budget.use_strong})
    for attempt in range(1, _budget.attempts + 1):
        temp = _budget.temps[min(attempt, len(_budget.temps)) - 1]
        # 重试必须幂等：每轮开始把文件恢复到节点起始状态，否则补丁会一层层叠加
        if orig_exists:
            tools.write_file(ws, rel, orig_text, backup=False)
        elif p.exists():
            p.unlink()
        db.execute("UPDATE nodes SET attempts=?, status=? WHERE id=?", (attempt, "running", node["nid"]))
        emit("escalate" if attempt > 1 else "attempt_start",
             {"node": node["nid"], "attempt": attempt, "temperature": temp,
              "feedback": feedback[:400]})

        # B：上一轮判定"测试文件自身坏了"→ 先用引擎判据重渲染测试文件，再让实现节点继续。
        # 不这么做的话，实现改一百遍也红（坏的是测试），远端救火也会白跑。
        if node.get("_test_file_broken"):
            try:
                rep = _repair_test_files(ws, node, task_id, emit)
                touched = rep["repaired"] + rep["rewritten"]
                if touched:
                    feedback = "已修补测试文件（不删测试）：" + "、".join(touched)
                node.pop("_test_file_broken", None)
                node.pop("_missing", None)
            except Exception as exc:                     # noqa: BLE001
                emit("notice", {"message": f"测试文件重渲染失败：{exc}"})

        try:
            if node["kind"] == "test":
                # 测试节点：优先用**引擎判据**渲染 pytest（模型只补它没覆盖到的部分）
                engine_code = ""
                try:
                    from . import props as props_mod
                    impl_rel = rel[5:] if Path(rel).name.startswith("test_") else rel
                    syms = props_mod.module_symbols(ws, impl_rel)
                    echecks = props_mod.from_requirement(
                        (node.get("contract") or "") + "\n" + _task_requirement(node), syms)
                    if echecks:
                        engine_code = props_mod.render_pytest(
                            Path(impl_rel).stem, echecks, target_rel=impl_rel)
                        emit("engine_tests", {"node": node["nid"],
                                              "checks": [c["desc"] for c in echecks][:6],
                                              "code": engine_code[:800]})
                except Exception as exc:          # noqa: BLE001
                    emit("notice", {"message": f"引擎判据跳过：{exc}"})
                if engine_code:
                    ast.parse(engine_code)
                    new_text = engine_code
                    changed = {rel: (orig_text, new_text)}
                    emit("rendered_test", {"node": node["nid"], "code": new_text[:1200],
                                           "source": "engine"})
                    res = {"ms": 0, "eval_count": 0}
                    spec = None                 # 引擎出测试时没有"模型规约"，后续步骤要跳过
                else:
                    spec, res = _generate_test_spec(ctx, model, temp, feedback)
                    emit("llm_call", {"stage": "test-spec", "model": model, "ms": res["ms"],
                                      "tokens": res["eval_count"], "attempt": attempt})
                if not engine_code:
                    if not spec:
                        raise tools.ToolError("测试规约生成失败：模型未给出合法的 func/module 标识符")
                    # 计算校验：能实测的更正值直接由引擎改写（不靠模型猜）
                    spec, fixed_notes = oracle.fix_spec(ws, spec)
                    if fixed_notes:
                        emit("oracle_fix", {"node": node["nid"], "notes": fixed_notes,
                                            "spec": {k: v for k, v in spec.items()
                                                     if not k.startswith("_")}})
                    emit("test_spec", {"node": node["nid"],
                                       "spec": {k: v for k, v in spec.items()
                                                if not k.startswith("_")}})
                    db.save_spec(task_id, node["nid"], {k: v for k, v in spec.items()
                                                       if not k.startswith("_")})
                    # 计算校验：此刻实现通常还不存在，主要记录"无法验算"
                    pre = oracle.check(ws, spec)
                    emit("oracle", {"node": node["nid"], "stage": "test-spec",
                                    "checked": pre["checked"], "summary": pre["summary"],
                                    "divergences": pre["divergences"]})
                    new_text, rendered = _apply_test_spec(ws, rel, orig_text, spec)
                    changed = {rel: (orig_text, new_text)}
                    emit("rendered_test", {"node": node["nid"], "code": rendered.strip()[:1200]})
            else:
                res = _generate(ctx, model, temp, feedback)
                emit("llm_call", {"stage": node["kind"], "model": model, "ms": res["ms"],
                                  "tokens": res["eval_count"], "attempt": attempt})
                changed = _apply(ws, node, res["text"], emit)
        except tools.ToolError as exc:
            feedback = f"补丁应用失败：{exc}"
            db.execute("UPDATE nodes SET last_error=? WHERE id=?", (feedback[:500], node["nid"]))
            db.record_gate(task_id, node["nid"], attempt, "apply", False, "static_rule",
                           "parse", feedback)
            emit("gate", {"node": node["nid"], "gate": "apply", "ok": False,
                          "evidence_kind": "static_rule", "detail": feedback[:300], "ms": 0})
            continue
        except Exception as exc:      # 引擎自身异常也要变成一次"失败尝试"，而不是让整个任务崩掉
            feedback = f"引擎异常（{type(exc).__name__}）：{exc}"
            db.execute("UPDATE nodes SET last_error=? WHERE id=?", (feedback[:500], node["nid"]))
            emit("gate", {"node": node["nid"], "gate": "engine", "ok": False,
                          "evidence_kind": "static_rule", "detail": feedback[:300], "ms": 0})
            continue

        # 落盘（工具内部已有干跑语法，这里再统一确认）
        try:
            for r2, (_base, new) in changed.items():
                # 硬约束②：填桩节点只允许"目标函数被改写"，其它函数集合必须与进入节点时一致；
                # 否则拒绝写盘（实测：一次写残后 clean 模块整个函数都没了）
                if node.get("stage") == "fill":
                    before, after = _func_names(_base), _func_names(new)
                    target = node.get("target_func") or ""
                    lost = before - after
                    if lost or (target and target not in after):
                        raise tools.ToolError(
                            f"填桩节点破坏性写入被拒绝：丢失函数 {sorted(lost)[:5]}"
                            + (f"，目标函数 {target} 不在结果里"
                               if target and target not in after else ""))
                tools.write_file(ws, r2, new)
        except tools.ToolError as exc:
            # 不变量拒绝写盘 = 一次失败尝试，而不是让整个任务崩掉
            feedback = f"写入被拒绝：{exc}"
            db.execute("UPDATE nodes SET last_error=? WHERE id=?", (feedback[:500], node["nid"]))
            db.record_gate(task_id, node["nid"], attempt, "invariant", False, "static_rule",
                           "func-set", feedback)
            emit("gate", {"node": node["nid"], "gate": "invariant", "ok": False,
                          "evidence_kind": "static_rule", "detail": feedback[:300], "ms": 0})
            continue
        # 确定性自动修复：按契约对齐函数参数、补缺失 import（实测这两类占了剩余失败的大头）
        try:
            from . import autofix as autofix_mod
            for r2 in changed:
                _p = tools.safe_path(ws, r2)
                if not _p.exists():
                    continue
                _miss = []
                try:
                    _src = _p.read_text(encoding="utf-8-sig", errors="replace")
                    _find = apikb.verify_undeclared_modules(_src, apikb.ApiKB(gates.API_KB_DB))
                    _miss = sorted({f.where for f in _find})
                except Exception:
                    _miss = []
                _fx = autofix_mod.fix_file(_p, contract=node.get("contract") or "", missing_modules=_miss)
                if _fx.get("params") or _fx.get("imports"):
                    emit("autofix", {"node": node["nid"], "path": r2,
                                     "params": _fx["params"], "imports": _fx["imports"]})
            # 测试文件引用了实现模块的符号却没 import → 确定性补上（只补真实存在的符号）。
            # 实测：模型追加 `assert divide(...)` 却没扩展 `from calc import add, sub`，
            # 运行时 NameError，实现节点怎么改都红 —— 错在测试文件的 import 上。
            impl_rel_fix = _impl_rel_of(node)
            if impl_rel_fix and (ws / impl_rel_fix).exists():
                for tf in handoff._paired_test_files(ws, impl_rel_fix):
                    if not (ws / tf).exists():
                        continue
                    added = autofix_mod.ensure_test_imports(ws / tf, ws / impl_rel_fix)
                    if added:
                        emit("test_import_fixed", {"node": node["nid"], "path": tf,
                                                   "names": added,
                                                   "module": Path(impl_rel_fix).stem})
        except Exception as exc:
            emit("notice", {"message": f"自动修复跳过：{exc}"})

        for r2, (_base, new) in changed.items():
            diff = tools.diff_text(_base, new, r2)
            db.execute("UPDATE nodes SET patch=? WHERE id=?", (diff[:20000], node["nid"]))
            emit("patch", {"node": node["nid"], "path": r2, "diff": diff[:6000]})

        ok, feedback, gate_results = _gate_node(ws, node, changed, test_files, attempt, emit, model)
        if ok:
            # M2 可验证 Best-of-N：已全绿的候选不立刻收工，继续独立采样，按质量分选优
            if _bestofn_enabled() and attempt < _budget.attempts:
                score = _quality(gate_results)
                cands.append({"texts": {r: new for r, (_b, new) in changed.items()},
                              "score": score, "attempt": attempt})
                emit("candidate", {"node": node["nid"], "attempt": attempt,
                                  "score": score, "kept": len(cands)})
                feedback = ""
                continue
            snap = _snapshot(ws, f"{node['kind']}({rel}): {node['contract'][:50]}",
                             paths=list(changed))
            db.execute("UPDATE nodes SET status=?, last_error=NULL WHERE id=?", ("done", node["nid"]))
            emit("node_done", {"node": node["nid"], "snapshot": snap})
            return {"node": node["nid"], "ok": True}
        db.execute("UPDATE nodes SET last_error=? WHERE id=?", (feedback[:500], node["nid"]))

    if cands:
        best = min(cands, key=lambda c: c["score"])
        for r2, new in best["texts"].items():
            tools.write_file(ws, r2, new)
        snap = _snapshot(ws, f"{node['kind']}({rel}): best-of-{len(cands)}",
                         paths=list(best["texts"]))
        db.execute("UPDATE nodes SET status=?, last_error=NULL WHERE id=?", ("done", node["nid"]))
        emit("node_done", {"node": node["nid"], "snapshot": snap, "bestofn": len(cands),
                           "score": best["score"]})
        return {"node": node["nid"], "ok": True, "bestofn": len(cands)}

    # ---------------------------------------------------------------- 远端升级
    # 本地连续失败后不要把节点直接判死：打包给远端出方案，本地仍用自己的渲染器落盘、
    # 用自己的闸门判定（远端只出方案，不推进状态 —— docs/13 判定权分离）。
    # 成本：一次调用约 $0.0005；比起"整条任务卡住要人来救"便宜得多。
    escalation_note = ""
    if not cands and _remote_escalation_enabled():
        try:
            from . import handoff, remote as remote_mod
            ok_r, why_r = remote_mod.available()
            if not ok_r:
                escalation_note = f"远端升级跳过（{why_r}）"
                emit("notice", {"message": escalation_note})
            else:
                packet = handoff.build_packet(task_id, node["nid"])
                md = packet.get("markdown") or ""
                emit("escalate", {"node": node["nid"], "stage": "remote",
                                  "packet_chars": len(md),
                                  "reason": (feedback or "")[:300]})
                rr = remote_mod.complete(md, task_id=task_id, node_id=node["nid"],
                                        workspace=str(ws), emit=emit)
                if not rr.get("ok"):
                    escalation_note = f"远端调用失败：{rr.get('error') or '未知'}"
                    emit("notice", {"message": escalation_note})
                else:
                    ar = handoff.apply_remote(task_id, node["nid"], rr.get("answer") or "")
                    emit("remote_result", {"node": node["nid"], "ok": bool(ar.get("ok")),
                                          "detail": str(ar.get("reason") or "")[:400],
                                          "applied": ar.get("applied")})
                    if ar.get("ok"):
                        snap = _snapshot(ws,
                                         f"{node['kind']}({rel}): 远端协助（本地闸门通过）",
                                         paths=[rel])
                        db.execute("UPDATE nodes SET status=?, last_error=NULL WHERE id=?",
                                   ("done", node["nid"]))
                        emit("node_done", {"node": node["nid"], "snapshot": snap,
                                           "by": "remote"})
                        return {"node": node["nid"], "ok": True, "by": "remote"}
                    escalation_note = f"远端方案未通过本地闸门：{ar.get('reason')}"
        except Exception as exc:                         # noqa: BLE001
            escalation_note = f"远端升级异常：{type(exc).__name__}: {exc}"
            emit("notice", {"message": escalation_note[:200]})

    # 节点状态用 stuck（有节点没通过、需人工介入）；不要用 needs_human ——
    # 那个词留给"等人工审批"（精修模式的 needhuman），两个概念混了会误导人。
    db.execute("UPDATE nodes SET status=? WHERE id=?", ("stuck", node["nid"]))
    reason = feedback or "（本地没有给出更具体的原因）"
    if escalation_note:
        reason = f"{reason}\n{escalation_note}"
    db.execute("UPDATE nodes SET last_error=? WHERE id=?", (reason[:500], node["nid"]))
    emit("node_failed", {"node": node["nid"], "reason": reason[:500],
                         "remote_tried": bool(escalation_note)})
    return {"node": node["nid"], "ok": False, "reason": reason[:2000]}


def _norm_nodes(plan: list[dict], ws: Path, target: str, test_files: list[str]) -> list[dict]:
    """把模型给的计划规整成合法节点（3B 会照抄占位符、指错文件，这里兜住）。"""
    nodes: list[dict] = []
    for i, raw in enumerate(plan, 1):
        kind_raw = str(raw.get("kind", "impl")).lower()
        kind = "test" if "test" in kind_raw else "impl"
        path = str(raw.get("path") or target or "").strip().replace("\\", "/")
        if not path:
            continue

        exists = (ws / path).exists()
        mode_raw = str(raw.get("mode", "")).lower()
        if "whole" in mode_raw or "create" in mode_raw or not exists:
            mode = "whole"
        else:
            mode = "patch"

        # 测试节点必须落在测试文件上，否则纠正
        if kind == "test" and not gates.is_test_file(path):
            stem = Path(target or path).stem
            path = f"test_{stem}.py"
            mode = "patch" if (ws / path).exists() else "whole"
            if test_files:
                path = test_files[0]
                mode = "patch" if (ws / path).exists() else "whole"

        nodes.append({"nid": "", "seq": i, "kind": kind, "mode": mode,
                      "path": path, "contract": str(raw.get("contract") or "").strip() or "完成本节点"})

    # 测试节点要对准自己的被测模块：test_<模块>.py → <模块>.py（多模块项目必须逐个配对）
    impl_paths = [n["path"] for n in nodes if n["kind"] == "impl"]
    impl_contract = {n["path"]: n["contract"] for n in nodes if n["kind"] == "impl"}

    def _pair_module(test_path: str) -> str:
        stem = Path(test_path).stem
        cand = stem[5:] if stem.startswith("test_") else re.sub(r"_test$", "", stem)
        cand_path = f"{cand}.py"
        if cand_path in impl_contract or (ws / cand_path).exists():
            return cand_path
        if target:
            return target
        return impl_paths[0] if impl_paths else ""

    for n in nodes:
        if n["kind"] == "test":
            n["module"] = _pair_module(n["path"])
            ref = impl_contract.get(n["module"])
            if ref:
                n["contract"] = (f"为 {n['module']} 补测试，覆盖这条需求：{ref}。"
                                 f"注意：该功能此刻尚未实现，你的测试现在应当失败（这是预期的）。")
        else:
            n["module"] = n["path"]

    # 两段式拆分（硬约束已就位）：
    #   ① 骨架节点：整段模块契约，mode=whole，一次建出所有函数桩
    #   ② 填桩节点：每函数一个，stage=fill（只许符号级编辑 + 不得删函数 + 按节点取 baseline）
    staged: list[dict] = []
    for n in nodes:
        if n["kind"] != "impl":
            staged.append(n)
            continue
        pairs = _contract_functions(n["contract"])
        if len(pairs) < 3:
            staged.append(n)
            continue
        staged.append(dict(n, stage="skeleton", mode="whole"))
        for fname, frag in pairs:
            staged.append(dict(n, stage="fill", mode="patch", contract=frag, target_func=fname))
    nodes = staged
    for i, n in enumerate(nodes, 1):
        n["seq"] = i
    # 测试节点要覆盖整段模块契约：同路径多个节点时取最长契约
    for n in nodes:
        if n["kind"] == "test":
            ref = impl_contract.get(n["module"])
            if ref:
                n["contract"] = (f"为 {n['module']} 补测试，覆盖这条需求：{ref}。"
                                 f"注意：该功能此刻尚未实现，你的测试现在应当失败（这是预期的）。")
    return nodes


def _func_names(source: str) -> set[str]:
    """模块级函数/类名集合（用于填桩节点的"不得删函数"不变量）。"""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return set()
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _contract_functions(contract: str) -> list[tuple[str, str]]:
    """从模块契约里切出每个函数那一小段（用于把实现节点拆细）。"""
    out: list[tuple[str, str]] = []
    for part in re.split(r"[；;]|。", contract or ""):
        part = part.strip()
        # 第一段常以 "store.py：" 开头，去掉这个前缀再识别函数
        part = re.sub(r"^[\w./-]+\.py\s*[：:]\s*", "", part)
        m = re.match(r"^\s*(?:[-*]\s*)?([A-Za-z_]\w{2,})\s*\(", part)
        if not m:
            continue
        name = m.group(1)
        if name in ("if", "for", "while", "return", "print", "example", "def"):
            continue
        out.append((name, part))
    return out


# ---------------------------------------------------------------- 主流程

def run_task(task_id: str, repo: Path, requirement: str, target: str,
             model: str, emit: Emitter) -> None:
    try:
        _run_task(task_id, repo, requirement, target, model, emit)
    except Exception as exc:  # 任何异常都不让任务卡在 running
        emit("error", {"message": f"{type(exc).__name__}: {exc}"})
        db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                   ("failed", db.now(), task_id))


def _run_task(task_id: str, repo: Path, requirement: str, target: str,
              model: str, emit: Emitter) -> None:
    t0 = time.time()
    # 1. 探测
    info = recon.classify(repo)
    emit("probe", {"state": info["state"], "note": info["note"],
                   "policy": info["policy"], "signals": info["signals"]})
    tcs = recon.toolchains()
    emit("toolchains", {"items": tcs})

    # 2. 工作区（隔离副本，用户仓库零风险）。同一个对话续跑时保留已有工作区，
    #    这样上一轮产出的代码/测试会累积下来，而不是每次从零覆盖。
    ws = WORKSPACES / task_id
    if ws.exists() and any(ws.iterdir()):
        emit("workspace", {"path": str(ws), "mode": "continue"})
    else:
        if ws.exists():
            shutil.rmtree(ws, ignore_errors=True)
        shutil.copytree(repo, ws, ignore=IGNORE, dirs_exist_ok=True)
        emit("workspace", {"path": str(ws), "mode": "fresh"})
    db.execute("UPDATE tasks SET workspace=?, updated_at=? WHERE id=?", (str(ws), db.now(), task_id))

    files = tools.list_files(ws, limit=120)
    test_files = [f for f in files if gates.is_test_file(f)]
    seq_base = 0
    if node_rows := db.query("SELECT seq FROM nodes WHERE task_id=?", (task_id,)):
        seq_base = max(int(r["seq"]) for r in node_rows)
    emit("files", {"count": len(files), "tests": test_files})

    # 3. 计划：模块清单由引擎确定性抽取，模型只做补充（不再决定"做几个模块"）
    existing = {p.name for p in ws.glob("*.py")}
    modules = planner.extract_modules(requirement)
    emit("modules", {"count": len(modules),
                     "modules": [{"path": m.path, "name": m.name,
                                  "mentions": m.mentions,
                                  "contract": m.contract[:180]} for m in modules]})

    model_plan = _plan({"files": "\n".join(files[:120]), "tests": ", ".join(test_files) or "（无）",
                        "requirement": requirement, "target": target or "（未指定）",
                        "state": info["state"], "policy": info["policy"],
                        "max_nodes": planner.MAX_NODES}, model, emit)

    skeleton = planner.build_skeleton(modules, existing)
    plan, merge_notes = planner.merge_model_plan(skeleton, model_plan)
    plan, fix_notes = planner.sanitize(plan, modules)
    merge_notes += fix_notes
    if merge_notes:
        emit("plan_merge", {"notes": merge_notes})
    issues = planner.validate(plan, modules)
    emit("plan_check", {"ok": not issues, "issues": issues, "modules": len(modules),
                        "nodes": [{"kind": n["kind"], "path": n["path"],
                                   "source": n.get("source", "engine")} for n in plan]})

    if not plan:
        emit("error", {"message": "计划为空：需求里没有可识别的模块（请在需求中写出 xxx.py）"})
        db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", ("failed", db.now(), task_id))
        return

    nodes = _norm_nodes(plan, ws, target, test_files)
    for n in nodes:
        n["seq"] = int(n["seq"]) + seq_base
        n["nid"] = f"{task_id}-n{n['seq']}"
    if not nodes:
        emit("error", {"message": "计划规整后无有效节点，任务终止"})
        db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", ("failed", db.now(), task_id))
        return
    emit("plan_normalized", {"nodes": [{"kind": n["kind"], "mode": n["mode"], "path": n["path"]}
                                       for n in nodes]})

    # 4. 逐节点
    results = []
    test_nodes = [n for n in nodes if n["kind"] == "test"]
    # 任务开始前的文件基线：重跑节点时必须回到这里，而不是"上一次跑完的样子"
    baselines: dict[str, str] = {}
    for n in nodes:
        rel = n["path"]
        if rel in baselines:
            continue
        f = tools.safe_path(ws, rel)
        baselines[rel] = f.read_text(encoding="utf-8") if f.exists() else ""

    for node in nodes:
        if node["path"] not in test_files and node["kind"] == "test":
            test_files.append(node["path"])
        r = _run_node(ws, task_id, node, model, test_files, emit,
                      baseline=None)
        # 实现节点因「测试不匹配」失败 → 先做计算校验，拿到"实测值"，再决定返工
        if (not r["ok"] and node["kind"] == "impl" and test_nodes
                and "G5 测试" in (r.get("reason") or "") and not node.get("_test_regenerated")):
            spec = db.get_spec(task_id)
            orc = oracle.check(ws, spec) if spec else None
            if orc:
                emit("oracle", {"node": node["nid"], "stage": "impl-failed",
                                "checked": orc["checked"], "summary": orc["summary"],
                                "divergences": orc["divergences"], "probes": orc["probes"]})
            donor = next((t for t in test_nodes if t.get("module") == node["path"]), test_nodes[0])
            emit("notice", {"message": "实现节点因测试不匹配失败 → 计算校验实测算出真实返回值 → 触发「测试规格返工」"})
            fb = (r.get("reason") or "")
            if orc:
                fb += "\n" + oracle.as_feedback(orc)
            regen = _regen_tests(ws, donor, model, feedback=fb)
            if regen and "error" not in regen:
                emit("test_regenerated", {"node": donor["nid"], "spec": regen["spec"],
                                          "code": regen["code"], "ms": regen["ms"]})
                if regen.get("oracle_fixes"):
                    emit("oracle_fix", {"node": donor["nid"], "notes": regen["oracle_fixes"],
                                        "spec": regen["spec"]})
                node["_test_regenerated"] = True
                r = _run_node(ws, task_id, node, model, test_files, emit,
                              baseline=None)
                if not r["ok"]:
                    # 返工后仍失败：再做一次计算校验，把最终分歧写进节点错误
                    spec2 = db.get_spec(task_id)
                    orc2 = oracle.check(ws, spec2) if spec2 else None
                    if orc2:
                        emit("oracle", {"node": node["nid"], "stage": "final",
                                        "checked": orc2["checked"], "summary": orc2["summary"],
                                        "divergences": orc2["divergences"]})
                        if orc2["divergences"]:
                            r["reason"] = (r.get("reason") or "") + "\n" + oracle.as_feedback(orc2)
            elif regen and regen.get("error"):
                emit("notice", {"message": f"测试规格返工失败（规约不完整）：{regen['error'][:160]}；"
                                           f"保持原测试，节点标记 stuck"})
        results.append(r)
        if not results[-1]["ok"]:
            emit("notice", {"message": "该节点标记 stuck（有节点没通过，需人工介入），"
                                       "继续执行其余节点（docs/06 §4-L7）"})

    # 5. 审计 + 收尾
    a = db.audit(task_id)
    a["wall_ms"] = int((time.time() - t0) * 1000)
    a["nodes_ok"] = sum(1 for r in results if r["ok"])
    a["nodes_total"] = len(results)
    db.execute("UPDATE tasks SET audit_json=?, updated_at=? WHERE id=?",
               (json.dumps(a, ensure_ascii=False), db.now(), task_id))
    # 任务终态只有三种：done / stuck（有节点没通过，需人工介入）/ failed。
    # 注意 stuck 与"等人工审批"无关——后者只在精修模式出现，且不会落到任务状态里。
    state = "done" if a["nodes_ok"] == a["nodes_total"] and a["nodes_total"] else "stuck"
    db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", (state, db.now(), task_id))
    # 交付件：根目录 README + MIT LICENSE（内容由 AST 扫描生成，不是模型编的）
    if state == "done":
        try:
            info = docsgen.generate(ws, requirement, project=Path(repo).name)
            emit("docs", {"node": "delivery", **info})
            db.record_gate(task_id, "delivery", 1, "docs", True, "static_rule",
                           "readme+license", f"README({info['modules']} 模块) + MIT LICENSE", 0)
            _snapshot(ws, "docs: 生成 README 与 MIT LICENSE",
                      paths=[info["readme"], info["license"]])
        except Exception as exc:
            emit("notice", {"message": f"README/LICENSE 生成失败：{exc}"})

    rc, diff = tools.git(ws, "diff", "--stat", "HEAD~%d" % max(1, a["nodes_ok"]), "HEAD") \
        if a["nodes_ok"] else (1, "")
    emit("audit", a)
    emit("done", {"state": state, "workspace": str(ws), "diff_stat": diff})


def new_task(repo: Path, requirement: str, target: str, model: str) -> str:
    """一个仓库 = 一个对话：该仓库已有未删除的对话就续用，否则新建。"""
    key = str(Path(repo).resolve())
    rows = db.query("SELECT id,state FROM tasks WHERE lower(repo)=lower(?) ORDER BY created_at DESC",
                    (key,))
    reusable = [r for r in rows if r["state"] != "running"]
    if reusable:
        task_id = reusable[0]["id"]
        db.execute("UPDATE tasks SET requirement=?, target=?, model=?, state=?, updated_at=?"
                   " WHERE id=?", (requirement, target, model, "running", db.now(), task_id))
        db.add_event(task_id, "requirement", {"text": requirement, "target": target,
                                              "model": model, "mode": "continue"})
        return task_id
    task_id = "t-" + time.strftime("%m%d%H%M") + "-" + uuid.uuid4().hex[:4]
    db.execute("INSERT INTO tasks(id,repo,workspace,requirement,target,state,model,created_at,updated_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (task_id, key, "", requirement, target, "running", model, db.now(), db.now()))
    db.add_event(task_id, "requirement", {"text": requirement, "target": target,
                                          "model": model, "mode": "new"})
    return task_id


def start_thread(task_id: str, repo: Path, requirement: str, target: str,
                 model: str, emit: Emitter) -> threading.Thread:
    th = threading.Thread(target=run_task, args=(task_id, repo, requirement, target, model, emit),
                          daemon=True)
    th.start()
    return th
