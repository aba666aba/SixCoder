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
from . import remote, settings_store

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
   返回类型同样只能用这些；**不许写 None / void**（副作用函数也要返回一个可判的小结果，
   例如 `{{"id": 1, "username": "new_user"}}` 或 `"ok"`，否则没有判据、永远没法验收）。
   元组可以，但必须写清类型形状，例如 `tuple[int, str]`。
3. **每个函数至少 1 条示例**，示例必须是"能用字面量算出来"的等式，写成 `f(字面量) == 期望值`。
   参数里不许出现变量名（`sqrt(x)` 不行，`sqrt(9.0)` 可以）。
   **只有调用、没有 `== 期望值` 的一律判红**（`register('a','b')` 不合格，`register('a','b') == {{"id": 1}}` 合格）。
4. 符号运算（求导、不定积分）不要追求通解：把它**限定成一条示例能判定的形式**
   （例如输入是多项式系数列表 `[3, 0, 2]`，或输入是单幂函数字符串 `"x^3"`），
   并在 desc 里写清适用范围与不支持的情形。
5. **需求里点名的每个能力都要有函数覆盖**（例如"求导"和"不定积分"各至少一个函数）；
   实在无法机判的，要么限定成可判形式，要么在 module 的 purpose 里写明放弃哪一项、为什么。
6. 宁可少而可判，不要多而含糊。拿不准就拆成"能做对的那一小步"。
7. **模块路径以简报里的「落点模块」为准**：写了 `app/auth.py` 就必须逐字用 `app/auth.py`，
   没写才自己定。**绝对不要用 calc.py / main.py**（那只是上面的格式示例，不是这个项目的文件）。
8. **禁止恒真函数**：`main() -> str` 配 `main() == 'ok'`、`noop() == None` 这类
   除了骗过判据什么也没做的函数，一律不收。每个函数都要有真实的输入输出语义。
9. Web 项目里，函数是**路由背后的服务层函数**（`register_user(username, password) -> dict`），
   不是 `def main()`。路由由作者在模块里用 `router = APIRouter()` 挂，不进契约。

{ambiguity_block}

甲方需求：
{requirement}
"""


def _ambiguity_block(mode: str) -> str:
    """规格歧义怎么处理 —— 这是两种模式最本质的差别之一。

    甲方模式（decide）：没人可问，所以**远端自己拍板**，但必须把假设写进 purpose，
    交付报告会把这些假设列出来 —— 事后可以追责，事前不阻塞。
    开发者模式（ask）：不许猜。只输出 {"question":...,"options":[...]}，系统停下等人回答。
    """
    if str(mode).lower() == "ask":
        return ("7. **有歧义就停下来问**：如果需求里有会影响接口/语义/边界的分歧，"
                "不要猜、不要自己拍板，只输出：\n"
                '   {"question":"一句话问题","options":["选项A","选项B"],"why":"为什么必须问"}\n'
                "   没有歧义才输出 modules。")
    return ("7. **有歧义自己拍板**：没有人可以问，所以你必须自己做出决定，"
            "但每个被你拍板的点都要以「假设：……」写进对应模块的 purpose，"
            "让甲方在交付报告里能一眼看到你替他决定了什么。"
            "唯一例外：需求在**能力上无法机判**（例如要求通解求导）时，按第 4/5 条限定成可判形式并在 purpose 说明。")


def _norm_type(t: str) -> str:
    t = str(t or "").strip().replace(" ", "").lower()
    alias = {"integer": "int", "number": "float", "double": "float", "string": "str",
             "boolean": "bool", "any": "dict", "listofint": "list[int]"}
    t = alias.get(t, t)
    ok = {"str", "int", "float", "bool", "dict", "list", "list[int]", "list[str]",
          "list[dict]", "list[float]", "dict[str, int]", "object"}
    return t if t in ok else ""


def _raw_index(modules: list[dict]) -> dict:
    """(模块路径, 函数名) → 远端给出的原始函数 JSON。用于**精确**诊断。

    为什么需要它（实测 2026-11-07）：模型给了
    `"examples": ["register('new_user', 'password123')"]` —— 有示例、但只有调用没有期望值。
    旧校验统一报"没有示例"，模型照着这句话重写还是给裸调用，
    于是同一个错连撞 3 次、4 条能力被降级、整轮 9.1% 就收工。反馈说错了，重试就永远不会对。
    """
    idx: dict = {}
    for mod in modules or []:
        path = str((mod or {}).get("path") or "").strip()
        for f in ((mod or {}).get("functions") or []):
            name = str((f or {}).get("name") or "").strip()
            if path and name:
                idx[(path, name)] = f or {}
    return idx


def _example_problem(exs: list) -> str:
    """示例一句话诊断：缺 / 只有调用没期望值。空串 = 合格。

    exs 里既可能是解析出来的 `contract.Example`，也可能是远端原始字符串。
    `Example.expect == ""` 就是"只有调用、没有期望值"——它判定必然失效，
    但旧文案统一写成"没有示例"，把模型引向错误方向（实测连撞 3 次）。
    """
    if not exs:
        return "没有示例（没有示例就没有 call_eq 判据）"
    def _txt(e) -> str:
        if isinstance(e, str):
            return e
        func = str(getattr(e, "func", "") or "")
        args = ", ".join(str(a) for a in (getattr(e, "args", None) or []))
        exp = str(getattr(e, "expect", "") or "")
        return f"{func}({args})" + (f" == {exp}" if exp else "")
    if not any(str(getattr(e, "expect", "") or ("==" in e if isinstance(e, str) else "")) for e in exs):
        return (f"示例没有期望值：必须写成 `f(...) == 期望值`，"
                f"而你给的是 {_txt(exs[0])[:80]!r}（只有调用、无从比对 → 判不了）")
    return ""


def _returns_problem(raw: dict) -> str:
    ret = str((raw or {}).get("returns") or "").strip()
    if not ret:
        return "缺 returns（没有返回类型就不知道拿什么去比）"
    if ret.lower() in ("none", "void", "null", "nil"):
        return (f"返回类型是 {ret}（副作用函数也必须返回一个可判的小结果，"
                f"否则没有期望值可比）→ 改成 str/int/float/bool/dict/list[...] 或 tuple[...]")
    return ""


def render_text(modules: list[dict], per_module: int = 8) -> str:
    """把远端给的 JSON 结构渲染成我们自己的契约文本（下游全部复用现有解析与判据）。

    per_module：每个模块最多收几个函数。默认 8 是"什么都没算"时的保守值；
    调用方应按 `core/dosing` 算出的验收带宽传进来（见 synthesize）。
    """
    cap = max(1, int(per_module or 8))
    lines: list[str] = []
    for i, mod in enumerate(modules, 1):
        path = str(mod.get("path") or "").strip()
        if not path.endswith(".py"):
            continue
        purpose = str(mod.get("purpose") or "").strip()
        lines.append(f"{i}) {path}{'：' + purpose if purpose else '：'}")
        for f in (mod.get("functions") or [])[:cap]:
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
        for f in (mod.get("functions") or [])[:cap]:
            for ex in (f.get("examples") or [])[:2]:
                ex = str(ex).strip()
                if ex:
                    lines.append(ex)
                    got_example = True
        if not got_example:
            lines.append("（无）")
        lines.append("")
    return "\n".join(lines).strip()


def validate(modules: list[dict], contracts: dict, limit: int | None = None,
             why: list[str] | None = None) -> list[str]:
    """校验"这份契约真的能机判"。返回问题清单（空 = 通过）。

    limit：这一轮允许几个函数（**动态**，由 core/dosing 按验收带宽算）。
           旧的写死 16 已废弃：16 不是物理上限，只是"一轮能验收多少"的经验值，
           而它随输出配额、思考强度、依赖数、近期一次通过率变化。
           why：算这个数的依据（人话），会拼进拒绝信息里，方便追"凭什么"。
    """
    problems: list[str] = []
    if not contracts:
        return ["契约解析结果为 0 个模块（路径没以 .py 结尾，或没有函数签名）"]
    raw_idx = _raw_index(modules)
    n_funcs = 0
    n_examples = 0
    n_raises = 0
    for path, c in contracts.items():
        for f in c.funcs:
            n_funcs += 1
            raw = raw_idx.get((path, f.name)) or {}
            params = list(getattr(f, "params", None) or [])
            untyped = [p.name for p in params if str(getattr(p, "type", "") or "") in ("", "object")]
            if untyped:
                problems.append(f"{path}::{f.name} 的参数字段缺类型：{', '.join(untyped)}"
                                f"（没有类型就造不出边界输入、判据跑不起来）")
            ret_bad = _returns_problem(raw)
            if ret_bad:
                problems.append(f"{path}::{f.name} {ret_bad}")
            exs = list(getattr(f, "examples", None) or []) or list(raw.get("examples") or [])
            n_examples += len(exs)
            n_raises += len(list(getattr(f, "raises", None) or []))
            ex_bad = _example_problem(exs)
            if ex_bad:
                problems.append(f"{path}::{f.name} {ex_bad}")
    if n_funcs == 0:
        problems.append("一个函数都没解析出来")
    if limit is None:
        from . import dosing
        limit, auto_why = dosing.suggest_max_funcs(
            examples=max(1, n_examples // max(1, n_funcs)),
            raises=max(0, n_raises // max(1, n_funcs)))
        why = why if why is not None else auto_why
    if n_funcs > int(limit):
        basis = ("；依据：" + "；".join(why[:3])) if why else ""
        problems.append(
            f"函数太多（{n_funcs} 个 > 本轮验收带宽 {limit} 个）："
            f"先做最能判定的 {max(3, int(limit) // 2)}~{limit} 个，剩下的下一轮再加{basis}")
    return problems



WIRING_NAMES = ("router", "app", "create_app", "api", "application")


def drop_wiring_funcs(modules: list[dict]) -> list[str]:
    """剔掉"接线"伪函数（`router() -> APIRouter` 这种）。

    实测（2026-11-07）：Web 项目的契约里混进 `router() -> APIRouter`，
    示例写成 `router = APIRouter()`（赋值，不是调用），于是：
      · 判据会去调一个不存在的 `router()`；
      · 作者被要求"实现 router 函数"，而正确写法是在模块级 `router = APIRouter()`。
    接线是**结构性**的，不该出现在"可调用函数"清单里。
    """
    dropped: list[str] = []
    for mod in modules or []:
        keep = []
        for f in ((mod or {}).get("functions") or []):
            name = str((f or {}).get("name") or "").strip().lower()
            ret = str((f or {}).get("returns") or "").lower()
            if name in WIRING_NAMES and ("router" in ret or "fastapi" in ret or "app" in ret):
                dropped.append(f"{mod.get('path')}::{name}")
                continue
            keep.append(f)
        mod["functions"] = keep
    return dropped


def synthesize(requirement: str, task_id: str = "contract", step_index: int | str | None = None,
               retries: int = 2, thinking: str = "off", ambiguity: str = "decide",
               budget: dict | None = None) -> dict:
    """需求 → 契约。返回 {ok, contracts, text, by, problems, usage, allowance}
    （或 need_human）。

    step_index 可以传 "strongest" → 用阶梯最强档（甲方模式的规格优先）。
    ambiguity="ask" 时，远端被允许只回一个问题，本函数以 need_human 返回（开发者模式）。
    budget：这一轮的验收带宽参数（`core/dosing.context_for()` 的产物）；
            没有就按"什么都没测到"的保守值算，绝不假装知道。
    """
    problems: list[str] = []
    from . import dosing as _dosing
    from . import tokens as _tokens
    if isinstance(step_index, str):
        step_index = settings_store.strongest_step_index() if step_index.lower() in ("strongest", "best") else None
    allow, allow_why = _dosing.suggest_max_funcs(**(budget or {}))
    # 正文配额：显式传给 remote（它会再把思考额度加在上面）。
    # 不传的话 remote 只能拿档案里的 8000，实测被思考吃光 → 正文 0 token。
    out_budget = int((budget or {}).get("capacity") or 0)
    prompt = PROMPT.format(requirement=_tokens.cut((requirement or "").strip(), 8000),
                           ambiguity_block=_ambiguity_block(ambiguity))
    prompt += (f"\n\n【本轮可用验收带宽】最多 {allow} 个函数"
               f"（依据：{'；'.join(allow_why[:2])}）。宁可少而可判，不要多而含糊。")
    for attempt in range(1, retries + 2):
        r = remote.complete(prompt if attempt == 1 else
                            prompt + "\n\n【上一次的问题，必须改掉】\n" + "\n".join(problems[:6]),
                            task_id=task_id, node_id="contract", json_mode=True,
                            step_index=step_index, thinking=thinking,
                            max_out=out_budget or None,
                            reuse_scope=f"{task_id}:contract")
        if not r.get("ok"):
            return {"ok": False, "problems": [f"远端调用失败：{r.get('error')}"],
                    "contracts": {}, "text": "", "by": ""}
        data = _parse_json(r.get("answer") or "")
        # 「正文被配额截断」≠「模型不会做」：加预算重试一次（实测 max 档思考 12.8k，
        # 正文只剩 3k，JSON 写到一半就被切了，那一次花掉的 ¥0.06 完全白费）。
        if data is None and remote.content_truncated(r.get("usage"), out_budget):
            out_budget = min(64000, max(4000, out_budget) * 2)
            problems = [f"上一次输出被截断（正文配额 {out_budget // 2} token 用满，JSON 没写完）。"
                        "这次直接输出完整 JSON：不要解释、不要省略、不要 Markdown 围栏外文字。"]
            if attempt > retries:
                break
            continue
        # 远端选择"不猜、发问"（只在 ambiguity="ask" 时会被允许）：如实上报，不许当成失败重试
        ask = str((data or {}).get("question") or "").strip()
        if ask:
            # 不在这里截断：问题原文要原样交给人看（questions.ask 自己管展示长度）
            return {"ok": False, "need_human": True, "question": ask,
                    "options": list((data or {}).get("options") or [])[:4],
                    "why": str((data or {}).get("why") or ""),
                    "problems": [f"规格有歧义需要人拍板：{ask}"],
                    "contracts": {}, "text": "", "by": r.get("by") or ""}
        modules = (data or {}).get("modules") or []
        drop_wiring_funcs(modules)     # 接线不是能力：剔掉 router()/app() 这类伪函数
        per_mod = _dosing.per_module_cap(allow, len(modules) or 1)
        text = render_text(modules, per_module=per_mod)
        contracts = contract_mod.parse_requirement(text) if text else {}
        problems = validate(modules, contracts, limit=allow, why=allow_why)
        if not problems:
            return {"ok": True, "contracts": contracts, "text": text,
                    "by": r.get("by") or "", "problems": [], "usage": r.get("usage") or {},
                    "allowance": allow, "allowance_why": allow_why}
    return {"ok": False, "problems": problems, "contracts": {}, "text": "",
            "by": "", "error": "远端给出的契约仍然不能机判",
            "allowance": allow, "allowance_why": allow_why}


CONTRACT_FILE = ".sixcoder/contract.txt"
CONTRACT_META = ".sixcoder/contract.meta.json"


def _req_hash(requirement: str) -> str:
    """需求指纹：契约锁存要按"这次的需求"判，而不是按"这个仓库有没有契约"。"""
    import hashlib
    return hashlib.sha1((requirement or "").strip().encode("utf-8")).hexdigest()[:16]


def load_locked(ws, requirement: str = "") -> str:
    """读仓库里锁存的契约（没有就返回空串）。

    **必须带需求指纹**：旧实现只看"仓库里有没有 contract.txt"，
    于是同一个工作区跑第二轮时会把第一轮的契约原样复用 ——
    "多轮把项目长到 1 万行"这条路直接断掉（第二轮根本不会合成新契约）。
    现在只有指纹一致才复用；不一致就重新合成并覆盖锁存。
    """
    p = Path(ws) / CONTRACT_FILE
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8-sig", errors="replace")
    meta = Path(ws) / CONTRACT_META
    try:
        import json
        got = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
    except Exception:                                    # noqa: BLE001
        got = {}
    want = _req_hash(requirement)
    if requirement and got.get("requirement_hash") != want:
        return ""                                        # 换了需求 → 不复用
    return text


def save_locked(ws, text: str, requirement: str = "") -> None:
    p = Path(ws) / CONTRACT_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    try:
        import json
        meta = Path(ws) / CONTRACT_META
        meta.write_text(json.dumps({"requirement_hash": _req_hash(requirement),
                                    "requirement_head": (requirement or "").strip()[:200]},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass


def load_or_synthesize(ws, requirement: str, step_index: int | str | None = None,
                       task_id: str = "contract", thinking: str = "off",
                       ambiguity: str = "decide", budget: dict | None = None) -> dict:
    """仓库里已有锁存契约就直接用（0 调用）；没有才合成并锁存。

    为什么必须锁存（实测）：同一个需求合成两次会得到**不完全一样**的契约（示例细节不同），
    于是"8/8 通过"只对当次那份契约成立 —— 复跑、裁判、回归全都失去可比性。
    """
    text = load_locked(ws, requirement)
    if text.strip():
        contracts = contract_mod.parse_requirement(text)
        if contracts:
            return {"ok": True, "contracts": contracts, "text": text, "by": "locked",
                    "problems": []}
    got = synthesize(requirement, task_id=task_id, step_index=step_index,
                     thinking=thinking, ambiguity=ambiguity, budget=budget)
    if got.get("ok"):
        save_locked(ws, got["text"], requirement)
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
