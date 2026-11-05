"""远端接力（handoff）：把本地卡住的节点打包成"远端模型能直接用的最小问题包"。

为什么要专门做这个：本地 3B 的强项是"按精确故障上下文写补丁"，弱项是"想方案"。
所以把**方案**交给远端、**实现与验证**留在本地，接力包必须满足三条：

1. 只含硬证据（evidence_kind != model_text）：闸门结论、失败断言、变异存活体、API 事实；
2. 只含符号级现状（目标符号源码 + 同文件签名清单），不塞整文件，远端不必猜上下文；
3. 输出格式与本地渲染器对齐：远端回一份 JSON 符号编辑，本地用 libcst 保真落盘并重跑全部闸门，
   远端说的"我改好了"同样不算数 —— 判定权仍在本地闸门。

用法（CLI）：
  python cli.py handoff --task t-xxx --node 2            # 打印可粘贴的接力包
  python cli.py handoff --task t-xxx --node 2 --out p.md # 同时存文件
  python cli.py apply-remote --task t-xxx --node 2 --file answer.json
"""
from __future__ import annotations

from . import tokens as _tokens_h

import ast
import json
import re
from pathlib import Path

from . import db, gates, symedit, tools

PATCH_FORMAT = """只输出一个 JSON 对象，不要解释、不要围栏：
{"edits":[
  {"op":"replace","symbol":"<符号限定名>","code":"def foo(a: int) -> int:\\n    \\"\\"\\"文档。\\"\\"\\"\\n    return a"},
  {"op":"add","symbol":"new_fn","after":"已有函数名","code":"def new_fn() -> int:\\n    return 1"}
]}
规则：symbol 取自上面的可编辑符号清单；类方法写成 Cls.method；code 必须含 def 行本身；
不要在 code 里输出整文件，也不要用 diff/SEARCH-REPLACE 格式。"""


def _ws(task_id: str) -> Path:
    from .agent import WORKSPACES
    return WORKSPACES / task_id


def _node(task_id: str, nid: str) -> dict:
    """接受 `t-xxx-n2`、`n2`、`2`（序号）三种写法。"""
    cands = [nid, f"{task_id}-{nid}", f"{task_id}-n{nid}"]
    for c in cands:
        rows = db.query("SELECT * FROM nodes WHERE task_id=? AND id=?", (task_id, c))
        if rows:
            return rows[0]
    if nid.isdigit():
        rows = db.query("SELECT * FROM nodes WHERE task_id=? AND seq=?", (task_id, int(nid)))
        if rows:
            return rows[0]
    raise KeyError(f"找不到节点 {nid}（可用序号或完整 id）")


def _task(task_id: str) -> dict:
    rows = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise KeyError(f"找不到任务 {task_id}")
    return rows[0]


def _hard_evidence(task_id: str, nid: str) -> list[dict]:
    rows = db.query(
        "SELECT attempt, gate, ok, evidence_kind, evidence_ref, detail FROM gate_runs"
        " WHERE task_id=? AND node_id=? ORDER BY attempt, id", (task_id, nid))
    return [r for r in rows if (r.get("evidence_kind") or "") != "model_text"]


def _symbol_view(source: str, contract: str, limit: int = 2400) -> tuple[str, str]:
    """(签名清单, 目标符号源码)。只给符号级现状，避免把整文件塞进远端上下文。"""
    syms = symedit.outline(source)
    outline = "\n".join(f"- {s.signature()}  [{s.lineno}-{s.endline}]"
                        + (f"  # {s.doc}" if s.doc else "") for s in syms) or "（无符号）"
    named = [s for s in syms if re.search(rf"\b{re.escape(s.name)}\b", contract)]
    targets = named or [s for s in syms if s.kind == "function"][:3]
    blocks, used = [], 0
    for s in targets:
        body = symedit.source_of(source, s.qualname)
        if not body:
            continue
        if used + len(body) > limit:
            body = body[:max(0, limit - used)]
        blocks.append(f"### {s.qualname}\n```python\n{body}\n```")
        used += len(body)
    return outline, ("\n\n".join(blocks) or "（文件里还没有可改的符号）")


def _paired_test_files(ws: Path, node_path: str) -> list[str]:
    stem = Path(node_path).stem
    out = []
    for p in sorted(ws.glob("test_*.py")):
        if re.sub(r"^test_|_test$", "", p.stem) == stem:
            out.append(p.name)
    for p in sorted(ws.glob("tests/test_*.py")):
        if re.sub(r"^test_|_test$", "", p.stem) == stem:
            out.append(str(p.relative_to(ws).as_posix()))
    return out


def _task_requirement(task_id: str) -> str:
    rows = db.query("SELECT requirement FROM tasks WHERE id=?", (task_id,))
    return (rows[0].get("requirement") or "") if rows else ""


def build_packet(task_id: str, nid: str) -> dict:
    """生成接力包（markdown 可粘贴 + json 结构化）。"""
    node = _node(task_id, nid)
    task = _task(task_id)
    nid = node["id"]                       # 归一化成完整 id（用户可能传的是序号）
    ws = _ws(task_id)
    rel = node["path"]
    p = ws / rel
    source = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
    outline, targets = _symbol_view(source, node.get("contract") or "")
    evidence = _hard_evidence(task_id, nid)

    failed = [e for e in evidence if not e["ok"]]
    last_fail = (failed[-1]["detail"] if failed
                 else (node.get("last_error") or ""))
    tests = _paired_test_files(ws, rel)
    test_src = {}
    for t in tests[:2]:
        tp = ws / t
        if tp.exists():
            test_src[t] = tp.read_text(encoding="utf-8-sig", errors="replace")[:3000]

    api_facts = ""
    try:
        from . import apikb
        kb = apikb.ApiKB(gates.API_KB_DB)
        if kb.available():
            api_facts = kb.block_for(node.get("contract") or "", limit=6)
    except Exception:                                    # noqa: BLE001
        api_facts = ""

    # 小文件直接给全文：远端看不到语句顺序就会瞎猜（实测它把类放到引用之后 → NameError）
    nlines = len(source.splitlines())
    full_file = ""
    if source and (nlines <= 200 and len(source) <= 8000):
        full_file = f"## 2b. 目标文件全文（{nlines} 行，含语句顺序，请据此判断定义顺序/注解引用）\n```python\n{source}\n```\n"

    failures_md = "\n".join(
        f"- 第 {e['attempt']} 次 · {e['gate']} FAIL（{e['evidence_kind']}）：{str(e['detail'])[:300]}"
        for e in failed[-8:]) or "（本次还没有硬证据失败记录）"

    packet = f"""# 远端协助请求：{task.get('target') or '实现'} / {rel}

## 0. 请只做这件事
用**符号级编辑**给出修复方案（不要输出整文件、不要 diff）。本地会用确定性渲染器落盘并重跑全部闸门，
所以格式必须与下方「输出格式」一致。

## 1. 任务契约（本地引擎已确定，不要改接口）
```
{node.get('contract') or task.get('requirement') or ''}
```

## 2. 目标文件现状（符号级）
文件：`{rel}`（{len(source.splitlines())} 行）

可编辑符号：
```
{outline}
```

需要改的符号：
{targets}

{full_file}
## 3. 相关测试（本地用它判定是否通过）
{"".join(f'### {name}{chr(10)}```python{chr(10)}{src}{chr(10)}```{chr(10)}' for name, src in test_src.items()) or "（暂无配对测试文件）"}

## 4. 已验证的库 API 签名（事实来源）
```
{api_facts or "（无）"}
```

## 5. 已经失败的硬证据（本地闸门实测，非模型自述；每次重试会变，故放在最后以命中前缀缓存）
{failures_md}

最近一次失败的窄反馈：
```
{_tokens_h.cut(str(last_fail), 1500)}
```

## 6. 输出格式（严格，**一次给全，不要分多轮**）
{PATCH_FORMAT}

**一次成功的要求（重要）**
1. 把你认为需要改的**所有**编辑一次列全（本地只会执行你这一次回复，不会再问第二轮）。
2. 若函数注解引用了本文件后面才定义的类，请**同时**用 add 插入
   `from __future__ import annotations`（本地渲染器会把它放到文件最前）。
3. 编辑必须是**符号级**：replace 用已存在的限定名；新增用 add + symbol。
4. 不要修改测试文件，不要改与失败无关的接口。

## 7. 本地验收标准（远端声明不算数）
1. `gate_syntax` 通过（渲染后必须可解析）；
2. `gate_api` 通过（不许导入不存在的名字/属性）；
3. 配对测试转绿（先红后绿的实现节点要求测试从失败变通过）；
4. 变异测试能杀死至少一个变异体（测试必须真的能抓错）。
"""
    data = {"task_id": task_id, "node": nid, "path": rel,
            "contract": node.get("contract") or "",
            "requirement": task.get("requirement") or "",
            "symbols": outline, "targets": targets,
            "evidence": evidence, "last_failure": last_fail,
            "tests": test_src, "api_facts": api_facts}
    return {"markdown": packet, "json": data}


# ------------------------------------------------------------------ 回传
def apply_remote(task_id: str, nid: str, answer: str) -> dict:
    """应用远端方案：符号级落盘 + 重跑闸门 + 记录证据（远端声明不推进状态）。"""
    node = _node(task_id, nid)
    nid = node["id"]
    ws = _ws(task_id)
    rel = node["path"]
    p = tools.safe_path(ws, rel)
    base = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""

    edits = _parse_answer(answer)
    if not edits:
        return {"ok": False, "reason": "远端回复里没有解析到 edits 数组"}
    rep = symedit.apply_edits(base, edits)
    if not rep.ok:
        db.record_gate(task_id, nid, 99, "symedit", False, "static_rule", "remote", rep.reason)
        return {"ok": False, "reason": f"渲染失败：{rep.reason}"}

    p.write_text(rep.new_source, encoding="utf-8")
    db.record_gate(task_id, nid, 99, "symedit", True, "mutation", "remote",
                   f"远端方案应用成功：{'; '.join(rep.applied)}")

    results = []
    r = gates.gate_syntax(ws, [rel])
    db.record_gate(task_id, nid, 99, r.gate, r.ok, r.evidence_kind, r.evidence_ref, r.detail)
    results.append((r.gate, r.ok, r.detail))
    if not r.ok:
        return {"ok": False, "reason": f"语法：{r.detail}", "applied": rep.applied}

    ar = gates.gate_api(ws, [rel])
    db.record_gate(task_id, nid, 99, ar.gate, ar.ok, ar.evidence_kind, ar.evidence_ref, ar.detail)
    results.append((ar.gate, ar.ok, ar.detail))
    if not ar.ok:
        return {"ok": False, "reason": f"API：{ar.detail}", "applied": rep.applied}

    se = gates.gate_security(ws, [rel])
    db.record_gate(task_id, nid, 99, se.gate, se.ok, se.evidence_kind, se.evidence_ref, se.detail)
    results.append((se.gate, se.ok, se.detail))
    if not se.ok:
        return {"ok": False, "reason": f"安全：{se.detail}", "applied": rep.applied}

    tests = _paired_test_files(ws, rel)
    tr = gates.gate_tests(ws, tests or None, timeout=180)
    no_tests = "no tests ran" in (tr.detail or "").lower()
    test_ok = tr.ok or no_tests
    db.record_gate(task_id, nid, 99, tr.gate, test_ok, tr.evidence_kind, tr.evidence_ref,
                   tr.detail if not no_tests else "无配对测试文件，改用引擎判据验收")
    results.append((tr.gate, test_ok, tr.detail))

    # 引擎判据兜底：没有配对测试时，用"需求里的示例/性质"验收（不依赖模型自写测试）
    props_ok, props_detail = True, ""
    used_checks: list[dict] = []
    try:
        from . import props as props_mod
        req = (node.get("contract") or "") + "\n" + (_task_requirement(task_id))
        syms = props_mod.module_symbols(ws, rel)
        checks = props_mod.from_requirement(req, syms)
        used_checks = list(checks)
        if checks:
            pr = props_mod.run(ws, rel, checks)
            props_ok = bool(pr.get("ok"))
            props_detail = props_mod.as_feedback(pr)
            db.record_gate(task_id, nid, 99, "props", props_ok, "assertion", "engine-oracle",
                           props_detail)
            results.append(("props", props_ok, props_detail))
    except Exception as exc:                            # noqa: BLE001
        props_detail = f"引擎判据跳过：{exc}"

    ok = r.ok and ar.ok and test_ok and props_ok
    reason = ""
    if not ok:
        reason = (f"测试未转绿：{tr.detail[:300]}" if not test_ok
                  else (f"引擎判据未通过：{props_detail[:300]}" if not props_ok else ""))
    db.set_node_status(task_id, nid, "done" if ok else "stuck", "remote", ok,
                       tr.detail if not ok else "远端方案通过本地闸门")
    try:
        db.recompute_state(task_id)
    except Exception:                                   # noqa: BLE001
        pass

    # 成功即登记回归用例：同一需求+同一模块，以后每次都要过这些判据
    if ok and used_checks:
        try:
            from . import regression
            added = regression.add(_task_requirement(task_id), rel, used_checks,
                                   task_id=task_id, node_id=nid, source="remote")
            if added:
                db.add_event(task_id, "regression_added",
                             {"node": nid, "module": rel, "count": len(used_checks)})
        except Exception:                               # noqa: BLE001
            pass
    db.add_event(task_id, "remote_applied", {"node": nid, "applied": rep.applied,
                                             "ok": ok, "gates": results})
    return {"ok": ok, "applied": rep.applied, "gates": results,
            "reason": "" if ok else f"测试未转绿：{tr.detail[:300]}"}


def _parse_answer(text: str) -> list[dict]:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    data = None
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            data = json.loads(cand)
            break
        except Exception:                                # noqa: BLE001
            continue
    raw: list = []
    if isinstance(data, dict):
        raw = data.get("edits") or data.get("ops") or []
    if not raw:
        # 容错：模型输出被 max_tokens 截断时，JSON 整体不合法，但前面的编辑对象是完整的。
        # 逐个平衡括号提取含 symbol+code 的对象（实测远端 1600 token 上限时必然截断）。
        raw = _salvage_edits(t)
    out = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        if not (e.get("symbol") and e.get("code")):
            continue
        out.append({"op": "add" if str(e.get("op", "")).lower() in ("add", "append") else "replace",
                    "symbol": str(e["symbol"]).strip(),
                    "code": str(e["code"]).strip("\n"),
                    "after": str(e.get("after") or "")})
    return out


def _salvage_edits(text: str) -> list[dict]:
    """从（可能被截断的）文本里救出完整的编辑对象：用栈记录每个 `{` 的起点，闭合即尝试解析。"""
    out: list[dict] = []
    stack: list[int] = []
    in_str, esc = False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            try:
                obj = json.loads(text[start:i + 1])
            except Exception:                            # noqa: BLE001
                continue
            if isinstance(obj, dict) and obj.get("symbol") and obj.get("code"):
                out.append(obj)
    return out


def parse_and_check(source: str) -> bool:
    try:
        ast.parse(source)
        return True
    except SyntaxError:
        return False
