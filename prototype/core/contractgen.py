"""把甲方的自然语言需求翻译成**可机判的契约**（远端做翻译，引擎做校验）。

为什么需要它（实测）：
    需求："写一个计算器，支持加减乘除、根号、求导、不定积分"
    旧行为：parse_requirement → 空 → 夜班直接结束（0/0），报"请按 1) xxx.py：f(a: int) -> int …… 格式写"
    也就是说：**甲方必须自己写契约**。这跟"甲方模式内部做完所有决策"是矛盾的。

现在的做法：需求解析不出契约时，让远端按固定 JSON 结构产出契约 →
渲染成我们自己的契约文本 → 用 `parse_requirement` 解析 → **校验能不能机判** →
不过就带着具体缺项再问一次 → 仍不过就如实报"做不了"，绝不假装。

校验口径（决定后面所有闸门能不能跑）：
1. 模块路径必须以 .py 结尾；
2. 每个函数的参数**必须有类型注解**（否则边界矩阵造不出样例）；
3. 每个函数至少 1 条示例，且示例参数必须是**字面量**（`sqrt(9.0)` 可以，`sqrt(x)` 不行）。

对"求导/不定积分"这类符号运算：不强行要求通解，只要求**限定成一条示例能判定的形式**
（例如多项式系数列表、或对简单幂函数的确定性规则），并在 desc 里写清适用范围。
"""
from __future__ import annotations

import json
import re

from pathlib import Path

from . import contract as contract_mod
from . import remote

PROMPT = """你是需求分析师。把下面这段**甲方需求**翻译成"可机判的契约"，只输出 JSON。

JSON 结构（严格）：
{{"modules":[{{"path":"calc.py","purpose":"一句话","functions":[
  {{"name":"sqrt","params":[{{"name":"x","type":"float"}}],"returns":"float",
    "desc":"开平方；x<0 抛 ValueError",
    "examples":["sqrt(9.0) == 3.0"],
    "raises":[{{"cond":"x < 0","exc":"ValueError"}}]}}
]}}]}}

硬性要求（不满足就会被判红重来）：
1. path 必须以 .py 结尾；函数不超过 8 个。
2. **每个参数都必须有类型注解**，只能用 str / int / float / bool / dict / list[int] / list[str] / list[dict]。
3. **每个函数至少 1 条示例**，示例必须是"能用字面量算出来"的等式，写成 `f(字面量) == 字面量`，
   参数里不许出现变量名（`sqrt(x)` 不行，`sqrt(9.0)` 可以）。
4. 符号运算（求导、不定积分）不要追求通解：把它**限定成一条示例能判定的形式**
   （例如输入是多项式系数列表 `[3, 0, 2]`，或输入是单幂函数字符串 `"x^3"`），
   并在 desc 里写清适用范围与不支持的情形。
5. **需求里点名的每个能力都要有函数覆盖**（例如"求导"和"不定积分"各至少一个函数）；
   实在无法机判的，要么限定成可判形式，要么在 module 的 purpose 里写明放弃哪一项、为什么。
6. 宁可少而可判，不要多而含糊。拿不准就拆成"能做对的那一小步"。

甲方需求：
{requirement}
"""


def _norm_type(t: str) -> str:
    t = str(t or "").strip().replace(" ", "").lower()
    alias = {"integer": "int", "number": "float", "double": "float", "string": "str",
             "boolean": "bool", "any": "dict", "listofint": "list[int]"}
    t = alias.get(t, t)
    ok = {"str", "int", "float", "bool", "dict", "list", "list[int]", "list[str]",
          "list[dict]", "list[float]", "dict[str, int]", "object"}
    return t if t in ok else ""


def render_text(modules: list[dict]) -> str:
    """把远端给的 JSON 结构渲染成我们自己的契约文本（下游全部复用现有解析与判据）。"""
    lines: list[str] = []
    for i, mod in enumerate(modules, 1):
        path = str(mod.get("path") or "").strip()
        if not path.endswith(".py"):
            continue
        purpose = str(mod.get("purpose") or "").strip()
        lines.append(f"{i}) {path}{'：' + purpose if purpose else '：'}")
        for f in (mod.get("functions") or [])[:8]:
            name = str(f.get("name") or "").strip()
            if not name:
                continue
            params = []
            for p in (f.get("params") or []):
                pname = str(p.get("name") or "").strip()
                ptype = _norm_type(p.get("type"))
                if pname:
                    params.append(f"{pname}: {ptype or 'object'}")
            ret = _norm_type(f.get("returns")) or str(f.get("returns") or "object").strip()
            desc = str(f.get("desc") or "").strip().replace("\n", " ")
            lines.append(f"   - {name}({', '.join(params)}) -> {ret}：{desc}")
        lines.append("")
        lines.append("示例（必须满足）：")
        got_example = False
        for f in (mod.get("functions") or [])[:8]:
            for ex in (f.get("examples") or [])[:2]:
                ex = str(ex).strip()
                if ex:
                    lines.append(ex)
                    got_example = True
        if not got_example:
            lines.append("（无）")
        lines.append("")
    return "\n".join(lines).strip()


def validate(modules: list[dict], contracts: dict) -> list[str]:
    """校验"这份契约真的能机判"。返回问题清单（空 = 通过）。"""
    problems: list[str] = []
    if not contracts:
        return ["契约解析结果为 0 个模块（路径没以 .py 结尾，或没有函数签名）"]
    n_funcs = 0
    for path, c in contracts.items():
        for f in c.funcs:
            n_funcs += 1
            params = list(getattr(f, "params", None) or [])
            untyped = [p.name for p in params if str(getattr(p, "type", "") or "") in ("", "object")]
            if untyped:
                problems.append(f"{path}::{f.name} 的参数字段缺类型：{', '.join(untyped)}"
                                f"（没有类型就造不出边界输入、判据跑不起来）")
            exs = list(getattr(f, "examples", None) or [])
            if not exs:
                problems.append(f"{path}::{f.name} 没有示例（没有示例就没有 call_eq 判据）")
    if n_funcs == 0:
        problems.append("一个函数都没解析出来")
    if n_funcs > 16:
        problems.append(f"函数太多（{n_funcs} 个）：先做能判定的 6~8 个，剩下的下一轮再加")
    return problems


def synthesize(requirement: str, task_id: str = "contract", step_index: int | None = None,
               retries: int = 1) -> dict:
    """需求 → 契约。返回 {ok, contracts, text, by, problems, usage}。"""
    problems: list[str] = []
    from . import tokens as _tokens
    prompt = PROMPT.format(requirement=_tokens.cut((requirement or "").strip(), 8000))
    for attempt in range(1, retries + 2):
        r = remote.complete(prompt if attempt == 1 else
                            prompt + "\n\n【上一次的问题，必须改掉】\n" + "\n".join(problems[:6]),
                            task_id=task_id, node_id="contract", json_mode=True,
                            step_index=step_index)
        if not r.get("ok"):
            return {"ok": False, "problems": [f"远端调用失败：{r.get('error')}"],
                    "contracts": {}, "text": "", "by": ""}
        data = _parse_json(r.get("answer") or "")
        modules = (data or {}).get("modules") or []
        text = render_text(modules)
        contracts = contract_mod.parse_requirement(text) if text else {}
        problems = validate(modules, contracts)
        if not problems:
            return {"ok": True, "contracts": contracts, "text": text,
                    "by": r.get("by") or "", "problems": [], "usage": r.get("usage") or {}}
    return {"ok": False, "problems": problems, "contracts": {}, "text": "",
            "by": "", "error": "远端给出的契约仍然不能机判"}


CONTRACT_FILE = ".sixcoder/contract.txt"


def load_locked(ws) -> str:
    """读仓库里锁存的契约（没有就返回空串）。"""
    p = Path(ws) / CONTRACT_FILE
    return p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""


def save_locked(ws, text: str) -> None:
    p = Path(ws) / CONTRACT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def load_or_synthesize(ws, requirement: str, step_index: int | None = None,
                       task_id: str = "contract") -> dict:
    """仓库里已有锁存契约就直接用（0 调用）；没有才合成并锁存。

    为什么必须锁存（实测）：同一个需求合成两次会得到**不完全一样**的契约（示例细节不同），
    于是"8/8 通过"只对当次那份契约成立 —— 复跑、裁判、回归全都失去可比性。
    """
    text = load_locked(ws)
    if text.strip():
        contracts = contract_mod.parse_requirement(text)
        if contracts:
            return {"ok": True, "contracts": contracts, "text": text, "by": "locked",
                    "problems": []}
    got = synthesize(requirement, task_id=task_id, step_index=step_index)
    if got.get("ok"):
        save_locked(ws, got["text"])
    return got


def _parse_json(text: str) -> dict | None:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(data, dict):
            return data
    return None
