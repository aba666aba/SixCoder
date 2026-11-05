"""M1 参照实现差分：把"想清楚算法"降级成"写一个显然正确但很慢的参照 + 差分对撞"。

小模型的失败里，最难的是"逻辑想错"。但很多逻辑有第二条廉价通路：
  写一个暴力/直译的参照实现（穷举、逐元素、O(n^2) 都行），在小输入上逐例比对主实现。
参照实现的要求比优雅解低一个量级 —— 这正是 3B 能胜任的；而"两份实现是否一致"是机械判定。

约束（避免把机制做成噪音）：
- 参照独立成文件 `<module>_ref.py`，接口与主实现同名同签名；
- 只在小规模输入上比对（默认 ≤ 24 例），保证秒级；
- 不一致就给出**具体反例**（输入 + 两边输出），可直接给模型当窄反馈，也可进接力包。
"""
from __future__ import annotations

from . import tokens as _tokens

import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import ollama, tools

REF_PROMPT = """你是"参照实现"生成器。为下列函数写一个**最笨但绝对正确**的参照实现。

规则：
1. 允许 O(n^2)、允许穷举、允许逐元素循环 —— 越直白越好，不要优化、不要技巧。
2. 接口必须与契约完全一致（同名、同参数名、同返回类型）。
3. 只输出一个 python 代码块，内容是完整文件。
4. 不要写 if __name__ == "__main__"，不要读文件、不要联网、不要 sleep。
5. 如果行为依赖外部状态（文件、时间、随机），改成显式参数；做不到就输出注释 `# NOT_PURE`。

【目标模块】{path}
【契约】{contract}
【主实现现状（供你对齐签名，不要抄它的逻辑）】
```python
{impl}
```
"""

RUNNER = r'''
import importlib, json, sys, traceback
impl_mod, ref_mod, cases_json, func_json = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
cases = json.loads(cases_json)
funcs = json.loads(func_json)
impl = importlib.import_module(impl_mod)
ref = importlib.import_module(ref_mod)
out = []
for fn in funcs:
    a = getattr(impl, fn, None)
    b = getattr(ref, fn, None)
    if a is None or b is None:
        out.append({"func": fn, "error": "missing function", "impl": a is not None, "ref": b is not None})
        continue
    for args in cases:
        ra = rb = None
        ea = eb = None
        try:
            ra = a(*args)
        except Exception as exc:
            ea = type(exc).__name__
        try:
            rb = b(*args)
        except Exception as exc:
            eb = type(exc).__name__
        if ea != eb or (ea is None and ra != rb):
            out.append({"func": fn, "args": args, "impl": ea or repr(ra)[:80],
                        "ref": eb or repr(rb)[:80]})
            break
print(json.dumps(out, ensure_ascii=False))
'''


def _pure_funcs(source: str) -> list[dict]:
    """抽出可直接用位置参数调用的公开函数，并推断每个参数的类型。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    funcs = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") or node.decorator_list:
            continue
        args = [a for a in list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)]
        if node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            continue
        types = []
        for a in args:
            ann = ""
            if a.annotation is not None:
                try:
                    ann = ast.unparse(a.annotation)
                except Exception:                       # noqa: BLE001
                    ann = ""
            types.append(ann)
        funcs.append({"name": node.name, "types": types})
    return funcs


def gen_cases(funcs: list[dict], n: int = 12) -> list[list]:
    """按参数类型生成小规模样例；类型未知时按小整数猜（保守，不生成危险输入）。"""
    import random
    rnd = random.Random(20261103)
    ints = [-3, -1, 0, 1, 2, 3, 7, 10, -10]
    strs = ["", "a", "abc", "2024-01-01", "  x  "]
    cases: list[list] = []
    if not funcs:
        return cases
    arity = min(len(f["types"]) for f in funcs)
    kinds = []
    for i in range(arity):
        ann = "".join(f["types"][i] for f in funcs).lower()
        kinds.append("str" if "str" in ann else "float" if "float" in ann else "int")
    for _ in range(n):
        row = []
        for kind in kinds:
            if kind == "str":
                row.append(rnd.choice(strs))
            elif kind == "float":
                row.append(rnd.choice([0.0, 1.0, -1.5, 2.25, 100.0]))
            else:
                row.append(rnd.randint(-10, 10) if rnd.random() < 0.7 else rnd.choice(ints))
        cases.append(row)
    return cases


def build_reference(ws: Path, rel: str, contract: str, model: str,
                    temperature: float = 0.2) -> tuple[str, dict]:
    """让模型写参照实现，落盘为 <stem>_ref.py。返回 (ref_rel, llm 结果)。"""
    p = tools.safe_path(ws, rel)
    impl = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
    prompt = REF_PROMPT.format(path=rel, contract=contract, impl=_tokens.cut(impl, 2500))
    res = ollama.chat([{"role": "user", "content": prompt}], model,
                      temperature=temperature, num_predict=1200)
    text = res.get("text") or ""
    m = re.search(r"```(?:python)?\s*\n(.*?)```", text, re.DOTALL)
    code = (m.group(1) if m else text).strip()
    if "# NOT_PURE" in code or not code:
        return "", {**res, "skipped": "参照不适用（依赖外部状态或空输出）"}
    try:
        ast.parse(code)
    except SyntaxError:
        return "", {**res, "skipped": "参照实现语法错误"}
    stem = Path(rel).stem
    ref_rel = f"{stem}_ref.py"
    (ws / ref_rel).write_text(code + "\n", encoding="utf-8")
    return ref_rel, res


def differential(ws: Path, rel: str, ref_rel: str, cases: list, funcs: list[str],
                 timeout: int = 60) -> dict:
    """跑差分：返回 {ok, checked, divergences}。"""
    runner = ws / "_diff_run.py"
    runner.write_text(RUNNER, encoding="utf-8")
    impl_mod = Path(rel).stem
    ref_mod = Path(ref_rel).stem
    # 关键：同一秒内改写文件且字节数不变时，CPython 会复用旧 pyc，导致"对着旧代码通过"
    for mod in (impl_mod, ref_mod):
        cache = ws / "__pycache__"
        if cache.exists():
            for f in cache.glob(f"{mod}.*.pyc"):
                try:
                    f.unlink()
                except OSError:
                    pass
    cmd = [sys.executable, "-B", runner.name, impl_mod, ref_mod,
           json.dumps(cases, ensure_ascii=False), json.dumps(funcs)]
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True, env=env,
                              timeout=timeout, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return {"ok": False, "checked": 0, "divergences": [],
                "error": f"差分超时（{timeout}s）—— 主实现可能有死循环"}
    if proc.returncode != 0:
        return {"ok": False, "checked": 0, "divergences": [],
                "error": (proc.stderr or proc.stdout or "")[-800:]}
    try:
        div = json.loads((proc.stdout or "[]").strip().splitlines()[-1])
    except Exception:                                   # noqa: BLE001
        return {"ok": False, "checked": 0, "divergences": [],
                "error": f"差分输出无法解析：{(proc.stdout or '')[:300]}"}
    return {"ok": not div, "checked": len(cases) * max(1, len(funcs)), "divergences": div[:5]}


def as_feedback(result: dict) -> str:
    if result.get("error"):
        return f"差分测试失败：{result['error'][:300]}"
    if result["ok"]:
        return f"差分通过（{result['checked']} 例与参照实现一致）"
    lines = [f"与参照实现不一致（{len(result['divergences'])} 处）："]
    for d in result["divergences"][:3]:
        if d.get("error"):
            lines.append(f"  {d['error']}（impl={d.get('impl')} ref={d.get('ref')}）")
        else:
            lines.append(f"  {d['func']}{tuple(d['args'])} → 你的实现={d['impl']}  参照={d['ref']}")
    return "\n".join(lines)
