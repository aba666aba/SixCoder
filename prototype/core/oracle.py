"""计算校验（oracle）：用真实调用反推测试期望值，解决「测试断言与实现数值打架」。

为什么需要：3B 常把 expected 猜错（实测 clamp(5,1,3) 写成期望 2，正确的是 3）。
这时测试永远红，模型会一直改实现去凑一个错误期望，或者干脆放弃。

本模块做**确定性校验**，不判定谁对谁错，只产出「分歧事实」：
  - normal 用例：真实调用 func(*args) 拿到返回值，与 expected 比对
  - error  用例：真实调用是否抛出声明的异常类型
分歧供三处使用：
  1. 注入 repair 反馈（让模型看到"实际返回 3"，自己决定改实现还是改期望）
  2. 触发测试规格返工时，把实际值作为反馈
  3. 写进交付报告，标为 NEEDS_HUMAN（真分歧时交给人裁决）
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path

from . import tools

TIMEOUT = 20


def _literal(raw: str):
    """把模型给的字符串字面量安全求值（只允许字面量，不允许表达式）。"""
    try:
        return True, ast.literal_eval(str(raw))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def normalize_literal(raw: str) -> str:
    """把模型给的参数规范成合法 Python 字面量源码。

    3B 经常把字符串参数写成裸 token（`test.json` 而不是 `"test.json"`）。
    这里能确定地补上引号，而不是整条规约作废。
    """
    s = str(raw).strip()
    if not s:
        return s
    try:
        ast.literal_eval(s)
        return s
    except Exception:
        pass
    if re.match(r"^[\w./\\\-:@+]+$", s):        # 裸 token → 当字符串
        return repr(s)
    raise ValueError(f"参数不是合法 Python 字面量：{raw!r}")


def _probe(ws: Path, module: str, func: str, args: list[str]) -> dict:
    """在沙箱环境里子进程执行 module.func(*args)。返回 {ok, repr, error_type, error}"""
    values, bad = [], []
    for a in args:
        try:
            values.append(ast.literal_eval(normalize_literal(a)))
        except Exception as exc:
            bad.append(f"参数不合法：{a!r}（{exc}）")
    if bad:
        return {"ok": False, "error": "；".join(bad), "error_type": "SpecError", "repr": None}

    payload = json.dumps(values, ensure_ascii=False)
    code = (
        "import json, sys\n"
        "sys.path.insert(0, '.')\n"
        f"import {module} as _m\n"
        f"_f = getattr(_m, {func!r})\n"
        f"_args = json.loads({json.dumps(payload)})\n"
        "try:\n"
        "    _r = _f(*_args)\n"
        "    print('RESULT::' + repr(_r))\n"
        "except Exception as _e:\n"
        "    print('RAISED::' + type(_e).__name__ + '::' + str(_e)[:200])\n"
    )
    try:
        sb = tools.sandbox(ws)
        p = subprocess.run(["python", "-c", code], cwd=str(ws), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT, env=sb.env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"调用超时 {TIMEOUT}s", "error_type": "Timeout", "repr": None}
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.startswith("RESULT::"):
            return {"ok": True, "repr": line[len("RESULT::"):], "error_type": None, "error": ""}
        if line.startswith("RAISED::"):
            _, etype, msg = (line.split("::", 2) + ["", ""])[:3]
            return {"ok": False, "repr": None, "error_type": etype, "error": msg}
    return {"ok": False, "repr": None, "error_type": "NoOutput",
            "error": (out.strip().splitlines() or ["(无输出)"])[-1][:200]}


def check(ws: Path, spec: dict) -> dict:
    """对照测试规约（checks 列表）逐条验算。返回 {checked, divergences[], summary, probes}"""
    ws = Path(ws)
    module = str(spec.get("module") or "").strip()
    checks = spec.get("checks") or []
    result = {"checked": False, "divergences": [], "summary": "", "probes": []}
    if not module or not checks:
        return result

    src = ws / f"{module}.py"
    if not src.exists():
        result["summary"] = f"{module}.py 尚未实现，无法验算（测试先行阶段正常）"
        return result
    try:
        tree = ast.parse(src.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError as exc:
        result["summary"] = f"{module}.py 有语法错误，无法验算：{exc.msg}"
        return result
    defined = {n.name for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

    result["checked"] = True
    for c in checks:
        func = str(c.get("func") or "")
        args = [a for a in (c.get("args") or []) if str(a).strip()]
        want_ret = c.get("expect_return")
        want_exc = c.get("expect_exception")
        if func not in defined:
            result["divergences"].append({
                "case": "missing-symbol", "func": func, "args": args,
                "expected": f"{module}.{func} 存在", "actual": "模块里没有该函数/类",
                "verdict": "规约引用了不存在的符号",
                "hint": f"{module}.py 当前只有：{sorted(defined) or '（空）'}",
            })
            continue
        probe = _probe(ws, module, func, args)
        result["probes"].append({"func": func, "args": args, **probe})

        if want_exc:
            if probe["ok"]:
                result["divergences"].append({
                    "case": "error", "func": func, "args": args,
                    "expected": f"抛出 {want_exc}", "actual": f"正常返回 {probe['repr']}",
                    "verdict": "异常用例的参数其实是合法输入",
                    "hint": "换一组真正非法的输入",
                })
            elif probe["error_type"] != want_exc:
                result["divergences"].append({
                    "case": "error", "func": func, "args": args,
                    "expected": f"抛出 {want_exc}",
                    "actual": f"抛出 {probe['error_type']}: {probe['error']}",
                    "verdict": "异常类型与期望不符",
                    "hint": "按契约要求的异常类型调整",
                })
        else:
            if not probe["ok"]:
                result["divergences"].append({
                    "case": "normal", "func": func, "args": args,
                    "expected": "正常返回",
                    "actual": f"抛出 {probe['error_type']}: {probe['error']}",
                    "verdict": "正常用例在实现上抛了异常",
                    "hint": "正常参数可能选错，或实现缺少分支",
                })
            elif want_ret is not None:
                good, exp = _literal(str(want_ret))
                if not good:
                    result["divergences"].append({
                        "case": "normal", "func": func, "args": args,
                        "expected": str(want_ret), "actual": probe["repr"],
                        "verdict": "期望值不是合法字面量",
                        "hint": "用 Python 字面量，如 5 / 'abc' / True",
                    })
                else:
                    okv, act = _literal(probe["repr"] or "")
                    same = (act == exp) if okv else (str(probe["repr"]) == str(want_ret))
                    if not same:
                        result["divergences"].append({
                            "case": "normal", "func": func, "args": args,
                            "expected": str(want_ret), "actual": probe["repr"],
                            "verdict": "测试期望值与实现返回值不一致",
                            "hint": f"实际返回 {probe['repr']}；确认是期望写错还是实现算错",
                        })

    result["summary"] = (f"{len(result['divergences'])} 处分歧（测试断言 vs 实现实测）"
                         if result["divergences"] else
                         f"{len(checks)} 条检查与实现一致，验算通过")
    return result


def suggest_error_args(ws: Path, module: str, func: str, etype: str,
                       seed: list[str], limit: int = 10) -> list[str] | None:
    """引擎实测找反例：在候选参数里搜一个真的会抛 `etype` 的组合。

    为什么让引擎找：3B 反复把「非法输入」写成合法值（实测连写 3 次
    `lo=0,hi=5`）。而"这一组参数会不会抛异常"是**可实测**的，
    不需要模型猜。找到后用 repr 写回 spec，测试立刻变有效。
    """
    base: list = []
    for a in seed or []:
        good, val = _literal(a)
        if good:
            base.append(val)

    cands: list[list] = []
    if len(base) >= 2:
        cands.append(list(reversed(base)))
        for i in range(len(base)):
            for j in range(i + 1, len(base)):
                b = list(base)
                b[i], b[j] = b[j], b[i]
                cands.append(b)
        for i in range(len(base)):
            if isinstance(base[i], int):
                for repl in (0, -1, base[i] + 1):
                    b = list(base)
                    b[i] = repl
                    cands.append(b)
    # 通用兜底（常见"非法组合"形状）
    cands += [[5, 3, 1], [0, 3, 2], [5, 0, 3], [3, 2, 1], [-1, 0, 1], [1, 5, 0]]

    seen = set()
    tried = 0
    for c in cands:
        key = json.dumps(c, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        tried += 1
        if tried > limit:
            break
        probe = _probe(ws, module, func, [repr(x) for x in c])
        if (not probe["ok"]) and probe["error_type"] == etype:
            return [repr(x) for x in c]
    return None


def fix_spec(ws: Path, spec: dict) -> tuple[dict, list[str]]:
    """用实测结果修 spec 里明显错误的槽位。返回 (新 spec, 修复说明)。"""
    notes: list[str] = []
    res = check(ws, spec)
    if not res["checked"]:
        return spec, notes
    checks = spec.get("checks") or []
    for d in res["divergences"]:
        if d["case"] != "error" or "合法输入" not in d["verdict"]:
            continue
        for c in checks:
            if c.get("func") != d.get("func") or [str(a) for a in (c.get("args") or [])] != list(d.get("args") or []):
                continue
            etype = str(c.get("expect_exception") or "ValueError")
            wit = suggest_error_args(ws, spec["module"], c["func"], etype, c.get("args") or [])
            if wit:
                c["args"] = wit
                notes.append(f"异常用例 {c['func']}{tuple(wit)} 由引擎实测改正（原 {d['args']} 是合法输入）")
            break
    return spec, notes


def as_feedback(res: dict) -> str:
    """把分歧转成给模型的窄反馈（≤600 字）。"""
    if not res.get("checked"):
        return ""
    lines = ["【计算校验（引擎实测调用，不是推理）】"]
    for d in res["divergences"]:
        lines.append(f"- {d['case']} 用例 {d['args']}：期望 {d['expected']}，实测 {d['actual']}")
        lines.append(f"  → {d['verdict']}。{d['hint']}")
    if not res["divergences"]:
        lines.append(f"- {res['summary']}")
    return "\n".join(lines)[:600]


# ================================================================== oracle 升级（§13.4 第 2 步）
# 把「验收 oracle」从「只判过示例」升级为：性质测试 / 复杂度预算 / e2e 业务流 / 变异杀伤率
# 当硬判据。原因（开发文档 §12）：oracle 只能判「过示例」→ 刺激越多越贴示例（Goodhart），
# A 主张就停在小模块生成器。升级后，能被测量的能力才被奖励。

_ALLOWED_PROP_NODES = (
    ast.Expression, ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.Name,
    ast.Constant, ast.List, ast.Tuple, ast.Call, ast.Load, ast.And, ast.Or,
    ast.Not, ast.USub, ast.UAdd, ast.Add, ast.Sub, ast.Mult, ast.Div,
    ast.FloorDiv, ast.Mod, ast.Pow, ast.Eq, ast.NotEq, ast.Lt, ast.LtE,
    ast.Gt, ast.GtE, ast.In, ast.NotIn,
)
_SAFE_BUILTINS = {"len", "abs", "min", "max", "sorted", "bool", "int", "float", "str", "sum"}


def _safe_eval_bool(expr: str, ns: dict) -> bool:
    """在受限命名空间里安全地求一个布尔性质表达式（只允许比较/算术/白名单内建）。"""
    tree = ast.parse(expr, mode="eval")
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if not (isinstance(f, ast.Name) and f.id in _SAFE_BUILTINS):
                raise ValueError(f"性质表达式不允许调用 {getattr(f, 'id', f)!r}")
        elif not isinstance(n, _ALLOWED_PROP_NODES):
            raise ValueError(f"性质表达式含不允许的语法：{type(n).__name__}")
    return bool(eval(compile(tree, "<prop>", "eval"), {"__builtins__": {}}, ns))


def _invoke(ws: Path, module: str, func: str, args: list[str]) -> dict:
    """在沙箱里实调 module.func(*args)，把返回值 JSON 化回传（供性质测试断言）。"""
    values, bad = [], []
    for a in args:
        try:
            values.append(ast.literal_eval(normalize_literal(a)))
        except Exception:
            bad.append(repr(a))
    if bad:
        return {"ok": False, "error_type": "SpecError", "error": f"非法参数 {bad}", "value": None}
    payload = json.dumps(values, ensure_ascii=False)
    code = (
        "import json, sys\n"
        "sys.path.insert(0, '.')\n"
        f"import {module} as _m\n"
        f"_f = getattr(_m, {func!r})\n"
        f"_args = json.loads({json.dumps(payload)})\n"
        "try:\n"
        "    _r = _f(*_args)\n"
        "    print('RESULT::' + json.dumps({'v': _r}, ensure_ascii=False, default=str))\n"
        "except Exception as _e:\n"
        "    print('RAISED::' + type(_e).__name__ + '::' + str(_e)[:200])\n"
    )
    try:
        sb = tools.sandbox(ws)
        p = subprocess.run(["python", "-c", code], cwd=str(ws), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=TIMEOUT, env=sb.env())
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "Timeout", "error": f"调用超时 {TIMEOUT}s", "value": None}
    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.startswith("RESULT::"):
            return {"ok": True, "error_type": None, "error": "", "value": line[len("RESULT::"):]}
        if line.startswith("RAISED::"):
            _, et, msg = (line.split("::", 2) + ["", ""])[:3]
            return {"ok": False, "error_type": et, "error": msg, "value": None}
    return {"ok": False, "error_type": "NoOutput",
            "error": (out.strip().splitlines() or ["(无输出)"])[-1][:200], "value": None}


def _run_property(ws: Path, module: str, prop: dict) -> tuple[bool, str]:
    """实跑性质测试：对每组样本调 func，把返回值绑到 r、参数绑到 x0..，断言 expect。"""
    func = str(prop.get("func") or "")
    samples = prop.get("samples") or []
    expect = str(prop.get("expect") or "").strip()
    if not func or not expect:
        return False, "性质测试缺 func 或 expect"
    for args in samples:
        inv = _invoke(ws, module, func, [str(a) for a in args])
        if not inv["ok"]:
            return False, f"{func}{tuple(args)} 调用异常（{inv['error_type']}），性质无法断言"
        try:
            val = json.loads(inv["value"])["v"]
        except Exception:
            val = inv["value"]
        ns: dict = {"r": val}
        for j, a in enumerate(args):
            try:
                ns[f"x{j}"] = ast.literal_eval(str(a))
            except Exception:
                ns[f"x{j}"] = str(a)
        try:
            ok = _safe_eval_bool(expect, ns)
        except Exception as exc:
            return False, f"性质表达式无法求值：{expect}（{exc}）"
        if not ok:
            return False, f"{func}{tuple(args)} 违反性质「{expect}」（实测 r={val!r}）"
    return True, f"{func} 通过 {len(samples)} 组性质样本（{expect}）"


def _level_default(criterion: str, lv: str) -> bool:
    """某判据在本级是否默认开启。显式开关（spec 里的同名键）优先级更高。"""
    if criterion in ("computational", "property"):
        return True
    if criterion == "complexity_check":
        return lv in ("M", "L", "XL")
    if criterion == "e2e_check":
        return lv in ("M", "L", "XL")
    if criterion == "mutation_check":
        return lv in ("L", "XL")
    return False


def evaluate(ws: Path, spec: dict, level: str = "S") -> dict:
    """oracle 升级后的统一硬判据入口。

    spec 可含：
      module/path        ：目标模块（path 缺省为 module.py）
      checks             ：(已有) 计算校验的检查列表
      properties         ：[{func, samples:[[..]], expect: "r >= 0"}] 性质测试
      complexity_check   ：bool 覆盖默认（M+ 默认开）
      e2e_check          ：bool 覆盖默认（M+ 默认开）
      mutation_check     ：bool 覆盖默认（L+ 默认开）
      complexity          ：{max_complexity,max_depth,max_lines} 覆盖默认阈值

    返回 {passed, level, module, checks[], failed[], skipped[]}。
    约定（与 docs/25 一致）：测不了 → skipped（不判红也不假绿）；实测不达标 → failed（硬红）。
    """
    ws = Path(ws)
    module = str(spec.get("module") or "").strip()
    path = str(spec.get("path") or (f"{module}.py" if module else "")).strip()
    lv = str(level).upper()
    res = {"passed": True, "level": lv, "module": module,
           "checks": [], "failed": [], "skipped": []}

    def _want(crit: str) -> bool:
        if crit in spec and isinstance(spec[crit], bool):
            return spec[crit]
        return _level_default(crit, lv)

    # 1) 计算校验（已有能力，永远开）
    if spec.get("checks"):
        c = check(ws, {"module": module, "checks": spec["checks"]})
        ok = not c["divergences"]
        res["checks"].append({"criterion": "computational", "ok": ok, "detail": c["summary"]})
        if not ok:
            res["passed"] = False
            res["failed"].append("computational: " + c["summary"])

    # 2) 性质测试（永远是硬判据，便宜且确定性）
    for prop in (spec.get("properties") or []):
        ok, detail = _run_property(ws, module, prop)
        res["checks"].append({"criterion": "property", "ok": ok, "detail": detail})
        if not ok:
            res["passed"] = False
            res["failed"].append("property: " + detail)

    # 3) 复杂度预算（M+ 硬判据；纯 ast，零依赖）
    if _want("complexity_check") and path:
        try:
            from . import gates as _gates
            lim = spec.get("complexity") if isinstance(spec.get("complexity"), dict) else None
            qm = _gates.gate_quality_metrics(
                ws, [path], level=lv,
                max_complexity=int((lim or {}).get("max_complexity", 0)) or 0,
                max_depth=int((lim or {}).get("max_depth", 0)) or 0,
                max_lines=int((lim or {}).get("max_lines", 0)) or 0)
            res["checks"].append({"criterion": "complexity", "ok": qm.ok, "detail": qm.detail})
            if not qm.ok:
                res["passed"] = False
                res["failed"].append("complexity: " + qm.detail)
        except Exception as exc:                        # noqa: BLE001
            res["skipped"].append(f"complexity: 引擎未跑起来（{type(exc).__name__}）")

    # 4) e2e 业务流（M+ 硬判据；跑不通删测试，不污染仓库）
    if _want("e2e_check"):
        try:
            from . import e2e as _e2e
            er = _e2e.write(ws, {}, requirement=str(spec.get("requirement") or ""),
                            fname=f".sixcoder/_oracle_e2e_{module or 'mod'}.py", run=True)
            if er.get("skipped"):
                res["skipped"].append("e2e: " + str(er.get("reason", "跳过")))
            else:
                ok = bool(er.get("ok"))
                res["checks"].append({"criterion": "e2e", "ok": ok,
                                      "detail": _e2e.as_feedback(er)})
                if not ok:
                    res["passed"] = False
                    res["failed"].append("e2e: " + str(er.get("reason", "业务流未通过")))
        except Exception as exc:                        # noqa: BLE001
            res["skipped"].append(f"e2e: 引擎未跑起来（{type(exc).__name__}）")

    # 5) 变异杀伤率（L+ 硬判据；无 git diff 退化为全文件并如实说明）
    if _want("mutation_check"):
        try:
            from . import arch as _arch
            mr = _arch.gate_mutation_changed(ws, level=lv)
            if mr.evidence_ref in ("skipped", "no-mutants") or "skip" in (mr.evidence_kind or ""):
                res["skipped"].append("mutation: " + mr.detail)
            else:
                ok = mr.ok
                res["checks"].append({"criterion": "mutation", "ok": ok, "detail": mr.detail})
                if not ok:
                    res["passed"] = False
                    res["failed"].append("mutation: " + mr.detail)
        except Exception as exc:                        # noqa: BLE001
            res["skipped"].append(f"mutation: 引擎未跑起来（{type(exc).__name__}）")

    return res


def evaluate_feedback(res: dict) -> str:
    """把 oracle 升级结果转成给模型的窄反馈（≤600 字）。"""
    if res.get("passed"):
        if res.get("skipped"):
            return "oracle 升级版：全部可量化判据通过；" + "；".join(res["skipped"][:2])
        return "oracle 升级版：全部硬判据通过"
    lines = ["【oracle 升级版（引擎实测，不是推理）未通过】"]
    for f in res["failed"][:4]:
        lines.append(f"- {f}")
    if res.get("skipped"):
        lines.append("- 注：" + "；".join(res["skipped"][:2]))
    return "\n".join(lines)[:600]
