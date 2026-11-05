"""工作单（work order）：把"修整个项目"降级为"执行一张可机械验收的小单"。

这是"软件工程式迭代"的核心构件——大公司里没人试图一次看懂十万行：
    ① 架构师（远端大模型）只看证据，产出**根因 + 工作单**
    ② 执行者（本地小模型）只做单张工作单（通常是一个符号）
    ③ 机械验收判定单子是否完成，结果记账
    ④ 下一轮基于更新后的账本继续 —— 项目单调变好，而不是"一次生成可用代码"

工作单最小字段：
    id, kind(implement|fix_signature|add_import|add_test|refactor),
    target(文件), symbol(函数/类), acceptance(机械验收方式), priority
"""
from __future__ import annotations

from . import tokens as _tokens

import json
import re
import time

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT, round INTEGER, kind TEXT, target TEXT, symbol TEXT,
  acceptance TEXT, detail TEXT, status TEXT, result TEXT, created_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS progress(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, round INTEGER,
  modules_json TEXT, verified INTEGER, total INTEGER, note TEXT, created_at TEXT);
"""


def _ensure() -> None:
    for stmt in SCHEMA.split(";"):
        if stmt.strip():
            db.execute(stmt.strip())


def add(task_id: str, rnd: int, kind: str, target: str, symbol: str = "",
        acceptance: str = "", detail: str = "") -> int:
    _ensure()
    db.execute("INSERT INTO work_orders(task_id, round, kind, target, symbol, acceptance,"
               " detail, status, result, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
               (task_id, rnd, kind, target, symbol, acceptance, detail, "open", "", db.now()))
    rows = db.query("SELECT id FROM work_orders WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,))
    return int(rows[0]["id"]) if rows else 0


def open_orders(task_id: str) -> list[dict]:
    _ensure()
    return db.query("SELECT * FROM work_orders WHERE task_id=? AND status='open'"
                    " ORDER BY id", (task_id,))


def close(oid: int, ok: bool, result: str = "") -> None:
    _ensure()
    db.execute("UPDATE work_orders SET status=?, result=?, closed_at=? WHERE id=?",
               ("done" if ok else "failed", result[:800], db.now(), oid))


def record_progress(task_id: str, rnd: int, modules: dict, note: str = "") -> None:
    _ensure()
    verified = sum(m.get("verified", 0) for m in modules.values())
    total = sum(m.get("funcs", 0) for m in modules.values())
    db.execute("INSERT INTO progress(task_id, round, modules_json, verified, total, note,"
               " created_at) VALUES(?,?,?,?,?,?,?)",
               (task_id, rnd, json.dumps(modules, ensure_ascii=False,
                            default=lambda o: sorted(o) if isinstance(o, set) else str(o)),
                verified, total,
                note[:200], db.now()))


def progress_curve(task_id: str) -> list[dict]:
    _ensure()
    return db.query("SELECT round, verified, total, note FROM progress WHERE task_id=?"
                    " ORDER BY id", (task_id,))


# ------------------------------------------------------------------ 机械三诊
SETUP_HINTS = ("create_table", "init_db", "create_schema", "setup", "create_index",
               "init_table", "ensure_table")


def _is_setup(fname: str) -> bool:
    low = (fname or "").lower()
    return any(h in low for h in SETUP_HINTS)


def triage(ws, contract_by_path: dict, module_state: dict) -> list[dict]:
    """不依赖模型的分诊：从当前状态直接产出工作单（等价于"工头巡检"）。

    module_state: {path: {"symbols": set, "stubs": set, "verified_names": [...],
                          "fails_all": [...]}}
    三类单：缺失 / 桩 / **存在但判据没过**（第三类以前漏掉，导致"看起来都实现了"就收工）。

    依赖顺序（实测教训）：store.py 里 `create_table` 没过时，后面的 upsert/fetch_all 必然
    因为"表不存在"一起失败，把每张单的 2 次重试机会全烧掉。所以同一模块里只要有**未验收的
    setup 型函数**（建表/初始化），就只派它，别的先不派 —— 前一步没好，后面派了也是白烧。
    """
    from pathlib import Path

    orders: list[dict] = []
    for path, c in (contract_by_path or {}).items():
        st = module_state.get(path) or {}
        have = st.get("symbols") or set()
        stubs = st.get("stubs") or set()
        good = set(st.get("verified_names") or [])
        fails = [str(x) for x in (st.get("fails_all") or st.get("fails") or [])]
        pending_setup = [f.name for f in c.funcs if _is_setup(f.name) and f.name not in good]
        if pending_setup:
            # 只派"让它建起来"这一件事
            for f in c.funcs:
                if f.name not in pending_setup:
                    continue
                detail = (f"{f.name} 是建表/初始化步骤，必须先成：它是后续所有写入/读取的前提"
                          f"（实测：它没过时 upsert/fetch_all 必然连坐失败）")
                orders.append({"kind": "implement", "target": path, "symbol": f.name,
                               "acceptance": f"契约判据通过（{f.name}）", "detail": detail})
            continue
        for f in c.funcs:
            if f.name not in have:
                orders.append({"kind": "implement", "target": path, "symbol": f.name,
                               "acceptance": f"符号存在 + 契约判据通过（{f.name}）",
                               "detail": f"缺少函数 {f.name}，需按契约实现"})
            elif f.name in stubs:
                orders.append({"kind": "implement", "target": path, "symbol": f.name,
                               "acceptance": f"符号不是桩 + 契约判据通过（{f.name}）",
                               "detail": f"{f.name} 仍是 NotImplementedError/pass 桩"})
            elif f.name not in good:
                mine = [x for x in fails if f.name in x]
                detail = ("机械判据未通过，按证据逐条修（这是引擎实测，不是猜测）："
                          + "；".join(mine[:2])) if mine else \
                    (f"{f.name} 没有任何机械判据能证明它对；补一条可判定的实现"
                     f"（同一模块的端到端判据也会跑它）")
                orders.append({"kind": "implement", "target": path, "symbol": f.name,
                               "acceptance": f"契约判据通过（{f.name}）",
                               "detail": detail[:600]})
    return orders


WORKORDER_PROMPT = """你是架构师（远端）。下面是本地执行器卡住的项目当前状态与**硬证据**。
你的任务不是给代码，而是给出**根因**和**工作单**（每张单只做一件小事、可被机械验收）。

只输出 JSON：
{"root_cause":"一句话根因","orders":[
  {"kind":"implement|fix_signature|add_import|add_test","target":"文件.py",
   "symbol":"函数名","acceptance":"这条单完成的机械判据","detail":"给执行者的精确指令（做什么、约束是什么）"}
]}
规则：最多 5 张单；优先修"缺失/桩"，再修签名，最后补测试；不要把多件事塞进一张单。
"""


def parse_orders(text: str) -> tuple[str, list[dict]]:
    """解析架构师回复：返回 (root_cause, orders)。容忍被截断的 JSON。"""
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
    if not isinstance(data, dict):
        return "", []
    orders = []
    for o in (data.get("orders") or [])[:5]:
        if not isinstance(o, dict):
            continue
        kind = str(o.get("kind") or "implement").lower()
        if kind not in ("implement", "fix_signature", "add_import", "add_test", "refactor"):
            kind = "implement"
        orders.append({"kind": kind, "target": str(o.get("target") or "").strip(),
                       "symbol": str(o.get("symbol") or "").strip(),
                       "acceptance": str(o.get("acceptance") or "")[:200],
                       "detail": str(o.get("detail") or "")[:600]})
    return str(data.get("root_cause") or "")[:400], orders


def parse_plan(text: str) -> str:
    """从架构师回复里取 plan 字段（字符串数组或字符串），渲染成一段可直接进提示词的方案。

    甲方模式的"方案优先"要用它：方案由远端定、后面所有单都照它做，
    所以这里只做"取出来 + 拉平"，不做任何改写。
    """
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
    if not isinstance(data, dict):
        return ""
    plan = data.get("plan")
    if isinstance(plan, (list, tuple)):
        items = [str(x).strip() for x in plan if str(x).strip()]
        return "\n".join(f"- {x}" for x in items)[:1200]
    if isinstance(plan, str):
        return _tokens.cut(plan.strip(), 1500)
    return ""


def describe(order: dict) -> str:
    return (f"[{order['kind']}] {order['target']}::{order.get('symbol') or '-'}"
            f" | 验收: {order.get('acceptance') or '-'}")
