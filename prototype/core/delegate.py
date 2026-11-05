"""委托：远端把**机械性的输出**交给本地模型写，自己只出"一句话规格"。

用户的设计（这是省输出的核心）：
    价格结构是"输入命中 0.02 元/百万，输出 4~8 元/百万"——差 200~400 倍。
    所以远端的输出应该只包含"必须它想的"：决策 + 核心逻辑 + 委托指令（几十 token）。
    样板、校验、日志、适配层这些机械输出，让**本地模型**来写（0 元 token，只花时间）。

协议（远端回复里）：
    {"delegate": [{"kind": "validation|boilerplate|logging|adapter",
                   "target": "upsert", "spec": "为 upsert 补参数校验与 None 兜底"}]}
    每个模块最多 3 项（用户确认的粒度）。

本地怎么执行：
    1. 每项规格交给本地模型，只准输出**这一个函数的完整定义**；
    2. 落盘 → 编译 → 跑该函数的全部机械判据（契约/边界/签名）；
    3. 不过就打回：把失败原因作为"委托失败"证据还给远端（下一轮它会自己写）；
    4. 一项都不许悄悄丢掉：成功/失败都记进报告。
"""
from __future__ import annotations

from . import tokens as _tokens

from pathlib import Path

DELEGATE_PROMPT = """你在补一个函数的**机械部分**。只输出这一个函数的完整定义，只输出 JSON。

任务：{spec}

契约（签名/类型/异常不许改）：
{contract}

现有实现（可改写，但要保留已有的正确逻辑）：
```python
{current}
```

只输出 JSON：{{"code":"def {name}(...):\\n    ..."}}

要求：含 def 行与类型注解；空输入/None 不许崩；签名与契约完全一致。
"""

KINDS = ("validation", "boilerplate", "logging", "adapter")


def normalize(items) -> list[dict]:
    """规范化远端的委托清单：最多 3 项，kind/spec 必须有效。"""
    out: list[dict] = []
    for it in (items or [])[:3]:
        if not isinstance(it, dict):
            continue
        spec = str(it.get("spec") or "").strip()
        target = str(it.get("target") or "").strip()
        kind = str(it.get("kind") or "boilerplate").strip().lower()
        if not spec or not target:
            continue
        if kind not in KINDS:
            kind = "boilerplate"
        out.append({"kind": kind, "target": target, "spec": spec[:200]})
    return out


def run(ws: Path, path: str, items: list[dict], contract, model: str,
        emit=None, max_items: int = 3) -> dict:
    """执行委托项。返回 {done:[...], failed:[{target, reason}], used_tokens:0}。"""
    from . import iterate, symedit, tools

    p = tools.safe_path(ws, path)
    source = p.read_text(encoding="utf-8-sig", errors="replace")
    done, failed = [], []
    for it in normalize(items)[:max_items]:
        name = it["target"]
        current = ""
        try:
            current = symedit.source_of(source, name) or ""
        except Exception:                                # noqa: BLE001
            current = ""
        ctx = ""
        try:
            from . import contract as cm
            ctx = cm.describe(contract) if contract is not None else ""
        except Exception:                                # noqa: BLE001
            ctx = ""
        prompt = DELEGATE_PROMPT.format(spec=it["spec"], contract=_tokens.cut(ctx, 2500),
                                        current=_tokens.cut(current, 2500) or "（还没有实现）", name=name)
        try:
            res = iterate.ollama.chat([{"role": "user", "content": prompt}], model,
                                      json_mode=True, temperature=0.1, num_predict=800)
            code = _extract_code(res.get("text") or "")
        except Exception as exc:                         # noqa: BLE001
            failed.append({"target": name, "reason": f"本地模型调用失败：{type(exc).__name__}"})
            continue
        if not code:
            failed.append({"target": name, "reason": "本地模型没给出函数代码"})
            continue
        try:
            rep = symedit.apply_edits(source, [{"op": "replace", "symbol": name, "code": code}])
        except Exception as exc:                         # noqa: BLE001
            failed.append({"target": name, "reason": f"渲染失败：{type(exc).__name__}"})
            continue
        if not rep.ok:
            failed.append({"target": name, "reason": f"渲染失败：{rep.reason}"})
            continue
        p.write_text(rep.new_source, encoding="utf-8")
        verdict = _verify_one(ws, path, contract, name)
        if verdict["ok"]:
            source = rep.new_source
            done.append({"target": name, "kind": it["kind"]})
            if emit:
                emit("delegate_done", {"path": path, "target": name, "kind": it["kind"]})
        else:
            p.write_text(source, encoding="utf-8")       # 回滚，交回远端
            failed.append({"target": name, "reason": verdict["feedback"][:200]})
            if emit:
                emit("delegate_failed", {"path": path, "target": name,
                                         "reason": verdict["feedback"][:200]})
    return {"done": done, "failed": failed}


def _extract_code(text: str) -> str:
    import json
    import re
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
        if isinstance(data, dict) and str(data.get("code") or "").strip():
            code = str(data["code"]).strip()
            fm = re.search(r"```(?:python|py)?\s*(.*?)```", code, re.DOTALL)
            if fm:
                code = fm.group(1).strip()
            return code + ("\n" if not code.endswith("\n") else "")
    return ""


def _verify_one(ws: Path, path: str, contract, name: str) -> dict:
    """只验被委托的那一个函数（不牵连别人）。"""
    from . import iterate, props
    try:
        from . import contract as cm
        f = next((x for x in (getattr(contract, "funcs", None) or []) if x.name == name), None)
        if f is None:
            return {"ok": True, "feedback": ""}
        src = (Path(ws) / path).read_text(encoding="utf-8-sig", errors="replace")
        sig_bad = iterate._signature_problem(contract, src, name)
        if sig_bad:
            return {"ok": False, "feedback": sig_bad}
        checks = props.from_contract(iterate._single_func_contract(contract, f))
        checks += [x for x in props.boundary_checks(src, hard=True)
                   if str(x.get("expr", "")).startswith(f"m.{name}(")]
        r = props.run(ws, path, checks) if checks else {"ok": True}
        return {"ok": bool(r.get("ok")), "feedback": str(props.as_feedback(r))[:300]}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "feedback": f"委托验收出错：{type(exc).__name__}"}
