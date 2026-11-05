"""字段级填充：给"返回 dict"的函数用引擎骨架 + 只让模型填**表达式**。

为什么单开这条路（实测最强的失败形态）：`summarize` 这类函数在 7 个样本里只过 2 次，
失败原因不是"不会算"，而是**产出整段代码时把口径写错/转义搞坏**。而契约其实已经把答案
写死了：`summarize([{"age": 10, "email": "a@x.com"}]) == {"count": 1, "avg_age": 10.0, ...}`。

于是把任务缩小一个量级：
  引擎渲染骨架（键名来自契约，模型不用猜、不用写签名/缩进/docstring）
  → 模型只输出 {"fields": {"count": "len(rows)", ...}}（每键一个表达式）
  → 引擎把表达式填进骨架 → 语法闸门 → 契约判据（真 call_eq + keys）验收
  → 不过就只回"哪个键的表达式错了"，模型改一行即可

这样模型要做的判断从"整个函数的控制流"降到"一个表达式"，失败反馈也变窄。
不做的事：不猜键名（键来自契约的 keys），不认识 dict 返回就不走这条路。
"""
from __future__ import annotations

from . import tokens as _tokens

import json
import re
from pathlib import Path

# 只对"参数能用样例调起来"的简单签名开放字段级填充
SIMPLE_TYPES = {"list[dict]", "dict", "list[str]", "list[int]", "str", "int"}

PROMPT = """你在补一个函数的**返回值**。函数骨架已经写好，你只需要给每个键一个 Python 表达式。

只输出 JSON（不要解释、不要围栏）：
{{"fields": {{{keys_example}}}}}

规则：
1. 每个键一个表达式，表达式的可用变量**只有入参名**：{params}。
2. 不要写 def、不要写 return、不要写语句（不能赋值、不能 for/if 块）。要用就写成推导式。
3. 不要改变键名，也不要少给或多给键。
4. 表达式要满足契约里的口径（见下）。
5. **空输入/空值不许崩**：列表可能为空、字典的键可能缺失、值可能是 None。
   `sum(x)/len(x)` 在空列表上会除零，`r["k"]` 在缺键时会 KeyError，`None + 1` 会 TypeError。
   用 `if x else 默认值`、`x or 默认值`、`r.get("k")` 先把空/缺省情形接住，再算正常情形。

【契约】{contract}
【函数骨架（引擎已渲染，键已给定）】
```python
{skeleton}
```
【样例与期望（引擎判据会真跑这一条）】
{examples}
"""


def keys_of(func) -> list[str]:
    """从契约里拿这个函数"必须返回哪些键"。"""
    return [str(k) for k in (getattr(func, "keys", None) or [])]


def supported(func) -> bool:
    """这个函数适不适合走字段级填充：有 keys、返回 dict、参数类型简单。"""
    if not keys_of(func):
        return False
    ret = str(getattr(func, "returns", "") or "").replace(" ", "").lower()
    if ret not in ("dict", ""):
        return False
    params = list(getattr(func, "params", []) or [])
    if not params:
        return False
    for p in params:
        t = str(getattr(p, "type", "") or "object").replace(" ", "").lower()
        if t not in SIMPLE_TYPES:
            return False
    return True


def skeleton(func, doc: str = "") -> str:
    """引擎渲染骨架：键名、签名、docstring 都由契约决定，模型只填空。"""
    params = []
    for p in getattr(func, "params", []) or []:
        t = str(getattr(p, "type", "") or "")
        params.append(f"{p.name}: {t}" if t and t != "object" else p.name)
    sig = f"def {func.name}({', '.join(params)}) -> dict:"
    keys = keys_of(func)
    lines = [sig]
    doc_txt = (doc or getattr(func, "doc", "") or "").strip().replace('"""', "'''")
    if doc_txt:
        lines.append(f'    """{doc_txt[:200]}"""')
    literal = ", ".join(f'"{k}": None' for k in keys)
    lines.append(f"    result = {{{literal}}}")
    for k in keys:
        lines.append(f'    result["{k}"] = __FILL_{k}__')
    lines.append("    return result")
    return "\n".join(lines)


def fill(skeleton_code: str, fields: dict) -> str:
    """把表达式填进骨架；任何越界（写语句、写 def）一律拒绝。"""
    out = skeleton_code
    for key, expr in (fields or {}).items():
        token = f"__FILL_{key}__"
        if token not in out:
            continue
        e = str(expr or "").strip().rstrip(";")
        if not e or "\n" in e or e.startswith(("def ", "return ", "for ", "if ", "while ")):
            return ""
        out = out.replace(token, e)
    if "__FILL_" in out:                                 # 有键没填 → 不完整
        return ""
    return out


def parse(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            obj = json.loads(cand)
            if isinstance(obj, dict) and isinstance(obj.get("fields"), dict):
                return {str(k): str(v) for k, v in obj["fields"].items()}
        except Exception:                                # noqa: BLE001
            continue
    return {}


def advice(error_text: str) -> str:
    """把"被判红的报错"翻译成一条针对性的改法（确定性映射，不是猜）。

    为什么需要它：重试时只把 traceback 塞回去，小模型会换个写法继续犯同一个错
    （实测 summarize 连错三遍 `row['age']`）。这里按异常类型给一句话，窄且可执行。
    """
    t = str(error_text or "")
    tips: list[str] = []
    if "ZeroDivisionError" in t or "division by zero" in t:
        tips.append("除法的分母在空列表时是 0：先判 `if rows else 默认值`，或让分母不是 len()")
    for m in re.finditer(r"KeyError:\s*['\"]?([\w.\-]+)", t):
        tips.append(f"缺键 {m.group(1)}：用 `d.get('{m.group(1)}')` 取，不要用 `d['{m.group(1)}']`")
    if "NoneType" in t:
        tips.append("值可能是 None：先过滤（`if d.get('k') is not None`）或用 `or 默认值` 兜住")
    if "IndexError" in t:
        tips.append("列表可能是空的：先判空再取下标")
    if "AttributeError" in t:
        tips.append("对象可能是 None：先判 `is not None` 再调方法")
    if not tips:
        tips.append("把空输入（[]、[{}]、值为 None）先接住，再进行正常计算")
    # 去重保序，最多两条：给多了小模型会挑着听
    seen, out = set(), []
    for x in tips:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return "；".join(out[:2])


def build_prompt(func, contract: str, doc: str, examples: str) -> tuple[str, str]:
    """返回 (提示词, 骨架代码)。骨架由引擎给，模型只补表达式。"""
    sk = skeleton(func, doc)
    keys = keys_of(func)
    keys_example = ", ".join(f'"{k}": "<表达式>"' for k in keys)
    params = ", ".join(p.name for p in (getattr(func, "params", None) or []))
    prompt = PROMPT.format(keys_example=keys_example, params=params or "（无）",
                           contract=_tokens.cut(contract or "", 1200), skeleton=sk,
                           examples=_tokens.cut(examples or "（契约没给样例，仍按类型口径写）", 800))
    return prompt, sk
