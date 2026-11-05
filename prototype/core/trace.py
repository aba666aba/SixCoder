"""追溯矩阵（TRACE.md）：需求条目 → 契约函数 → 机械判据 → 通过证据 → 产物位置。

传统工程里这张表的作用是"审计"：甲方问"这条需求对应哪条证据"，三十秒答出来。
AI 生成代码特别容易丢掉这件事——东西看起来完成了，但没人能说出依据。

本模块只做"组装"，不产生新判断：
- 需求出处：需求原文里含该函数名的行；
- 判据：契约判据（示例/异常/返回键/类型冒烟）+ 数据库端到端 + 跨模块业务流；
- 证据：来自 `iterate.inspect` 的机械验收状态（**不含模型自述**）；
- 强度：engine_assertion（引擎断言，强）> static_rule（静态规则，中）> none（无判据，不计）。
它同时会列出**缺口**（需求点名但没实现、有函数但无判据、需求行没落到任何函数）与
**待甲方确认的歧义**（例如清洗器处理的字段名与表列对不上）。
"""
from __future__ import annotations

import re
import time
from pathlib import Path

STRENGTH = {
    "call_eq": "engine_assertion",
    "raises": "engine_assertion",
    "keys": "engine_assertion",
    "roundtrip": "engine_assertion",
    "call_ok": "engine_assertion",
    "db_end2end": "engine_assertion",
    "e2e": "engine_assertion",
}
ITEM_MARK = ("：", ":", "）", ")")          # 需求条目行常见分隔符
SKIP_LINE = ("示例", "必须满足", "```", "注意", "说明")
MODULE_LINE_RE = re.compile(r"^\s*\d+\s*[)）.、]\s*[\w./\\]+\.(py|ts|js|java|go|rs)\b[：: ]*$")
EVIDENCE_WORDS = ("应", "必须", "返回", "需要", "支持", "不得", "至少", "要求")


def _func_names(contracts: dict) -> dict[str, str]:
    """函数名 → 模块路径。"""
    out: dict[str, str] = {}
    for path, c in (contracts or {}).items():
        for f in getattr(c, "funcs", []):
            out[f.name] = path
    return out


def _requirement_lines(requirement: str) -> list[str]:
    return [ln.strip() for ln in (requirement or "").splitlines() if ln.strip()]


def attribute(requirement: str, contracts: dict) -> dict:
    """需求行 ↔ 函数 的双向归属。返回 {by_func, orphan_lines}。"""
    names = _func_names(contracts)
    by_func: dict[str, str] = {}
    orphan: list[str] = []
    for ln in _requirement_lines(requirement):
        hit = [n for n in names if n in ln]
        for n in hit:
            by_func.setdefault(n, ln[:160])
        if hit or any(s in ln for s in SKIP_LINE):
            continue
        if MODULE_LINE_RE.match(ln):            # "1) clean.py：" 这类是模块标题，不是条目
            continue
        if not any(w in ln for w in EVIDENCE_WORDS):
            continue                            # 项目概述句，不是可验收条目
        if len(ln) >= 8:
            orphan.append(ln[:160])
    return {"by_func": by_func, "orphan_lines": orphan}


def _checks_of(ws: Path, contracts: dict, path: str, fname: str) -> list[dict]:
    """某个函数当前拥有的全部判据（引擎侧，不含模型文本）。"""
    from . import props
    from .iterate import _single_func_contract

    c = (contracts or {}).get(path)
    if c is None:
        return []
    f = next((x for x in c.funcs if x.name == fname), None)
    if f is None:
        return []
    out: list[dict] = []
    try:
        out += props.from_contract(_single_func_contract(c, f))
    except Exception:                                    # noqa: BLE001
        pass
    if not out:                                          # 退一步：类型冒烟（与 inspect 同口径）
        try:
            src = (Path(ws) / path).read_text(encoding="utf-8-sig", errors="replace")
            out += [x for x in props.typed_checks(src)
                    if str(x.get("expr", "")).startswith(f"m.{fname}(")]
        except Exception:                                # noqa: BLE001
            pass
    try:
        for chk in props.from_contract(c):
            if chk.get("kind") == "db_end2end" and fname in (chk.get("covers") or []):
                out.append(chk)
    except Exception:                                    # noqa: BLE001
        pass
    return out


def _func_line(ws: Path, path: str, fname: str) -> str:
    """产物定位：文件::函数 在第几行（甲方要能一键跳过去看）。"""
    import ast
    try:
        tree = ast.parse((Path(ws) / path).read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:                                    # noqa: BLE001
        return "—"
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and node.name == fname:
            return f"L{node.lineno}"
    return "—"


def build(ws: Path, requirement: str, contracts: dict, state: dict,
          e2e_res: dict | None = None, regression_n: int = 0) -> str:
    """生成 TRACE.md 的正文。"""
    ws = Path(ws)
    att = attribute(requirement, contracts)
    if not regression_n:
        try:
            from . import regression
            regression_n = len(regression.for_task(requirement or ""))
        except Exception:                                # noqa: BLE001
            regression_n = 0
    lines = ["# 追溯矩阵（TRACE）", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             "- 口径：**只记录机械证据**；模型自述一律不计（model_assertion_rate 必须为 0）。",
             "- 强度：`engine_assertion`（引擎断言/真实运行）> `static_rule`（静态规则）> `none`（无判据）",
             ""]

    total = verified = with_criteria = 0
    strong = 0
    lines += ["## 一、需求条目 → 契约 → 判据 → 证据 → 产物", "",
              "| 需求出处 | 模块::函数 | 判据（条） | 证据强度 | 状态 | 产物 |",
              "|---|---|---|---|---|---|"]
    for path, st in (state or {}).items():
        c = (contracts or {}).get(path)
        if c is None:
            continue
        good = set(st.get("verified_names") or [])
        stubs = set(st.get("stubs") or set())
        have = set(st.get("symbols") or set())
        for f in c.funcs:
            total += 1
            chk = _checks_of(ws, contracts, path, f.name)
            e2e_cov = (e2e_res or {}).get("covers") or []
            if f.name in e2e_cov:
                chk = chk + [{"kind": "e2e", "desc": "跨模块业务流"}]
            kinds = sorted({x.get("kind", "?") for x in chk})
            strength = "engine_assertion" if kinds else "none"
            if kinds:
                with_criteria += 1
                strong += 1
            if f.name in stubs:
                status = "仍是桩"
            elif f.name not in have:
                status = "缺失"
            elif f.name in good:
                status = "已验收"
                verified += 1
            else:
                status = "未通过/无判据"
            src = att["by_func"].get(f.name, "（需求未点名）")
            lines.append("| %s | `%s::%s` | %s | %s | %s | `%s` %s |" % (
                _cell(src), path, f.name,
                ("、".join(kinds) + f"（{len(chk)}）") if kinds else "0",
                strength, status, path, _func_line(ws, path, f.name)))

    # e2e 红的时候，单函数"已验收"不能当成整体达标：实测整文件模式 8/8 已验收，
    # 而 e2e 抓出 store.py 少了一整列（单元判据只测空输入，看不见列缺失）。
    e2e_bad = bool(e2e_res) and not e2e_res.get("ok") and not e2e_res.get("skipped")
    e2e_note = ""
    if e2e_bad:
        e2e_note = "｜**e2e 未跑通 → 整体不算达标**（见 2.3）"
    lines += ["", f"**汇总**：契约函数 {total} 个｜有判据 {with_criteria} 个｜"
                  f"单函数已验收 {verified} 个｜证据全部为 `engine_assertion`"
                  f"｜回归库 {regression_n} 条{e2e_note}", ""]
    if e2e_bad:
        lines += ["> 注意：单函数判据只覆盖契约示例与边界，**跨模块的字段/列/口径**由 e2e 判定。",
                  "> e2e 未跑通时，交付物不能算\"可用\"，只能算\"单函数达标\"。", ""]

    orphan = att["orphan_lines"]
    untested = [(p, f) for p, st in (state or {}).items() for f in (st.get("untested") or [])]
    lines += ["## 二、覆盖缺口（这些地方目前没有证据）", ""]
    if orphan:
        lines += ["### 2.1 需求里这些条目没落到任何函数", ""]
        lines += [f"- {x}" for x in orphan[:12]]
        lines.append("")
    else:
        lines += ["- 需求条目均已归属到具体函数", ""]
    if untested:
        lines += ["### 2.2 有实现但没有任何机械判据（不计入验收）", ""]
        lines += [f"- `{p}::{f}`" for p, f in untested[:12]]
        lines.append("")
    else:
        lines += ["### 2.2 有实现但没有任何机械判据（不计入验收）", "",
                  "- 无：每个已实现函数都有机械判据", ""]
    if e2e_res is not None:
        lines += ["### 2.3 跨模块业务流（e2e）", ""]
        if e2e_res.get("skipped"):
            lines.append(f"- 跳过：{e2e_res.get('reason')}")
        elif e2e_res.get("ok"):
            lines.append("- 已跑通，覆盖：" +
                         "、".join(f"`{x}`" for x in (e2e_res.get("covers") or [])))
        else:
            lines.append(f"- **未跑通**：{e2e_res.get('reason')}")
        lines.append("")

    lines += ["## 三、待甲方确认的歧义与风险", ""]
    amb = ambiguity(ws, contracts, requirement)
    lines += ([f"- {x}" for x in amb] or ["- 未发现字段级歧义"])
    lines += ["", "## 四、怎么复查", "",
              "```powershell", "cd prototype",
              f"D:\\Python\\python.exe tools\\trace.py ..\\projects\\{ws.name}",
              "D:\\Python\\python.exe ..\\prototype\\tools\\legacy\\hq_verify.py "
              f"..\\projects\\{ws.name}", "```", ""]
    return "\n".join(lines)


def _cell(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")[:80]


def ambiguity(ws: Path, contracts: dict, requirement: str = "") -> list[str]:
    """跨模块字段级歧义：清洗器处理了表里没有的列名（典型"需求没说清"）。

    只报"确定对不上"的情况，且**不阻塞**——它是要甲方确认的问题，不是产物错误。
    """
    from . import gates, props

    cols = [c for c, _ in props._table_cols(requirement or "")]
    if not cols:
        return []
    out: list[str] = []
    for f in gates.gate_field_map(Path(ws), list((contracts or {}).keys()), cols):
        out.append(f)
    return out


def write(ws: Path, requirement: str, contracts: dict, state: dict,
          e2e_res: dict | None = None, regression_n: int = 0) -> Path:
    text = build(ws, requirement, contracts, state, e2e_res, regression_n)
    p = Path(ws) / "TRACE.md"
    p.write_text(text, encoding="utf-8")
    return p
