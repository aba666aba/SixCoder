"""失败分诊：先判"谁该修"，再决定要不要花钱。

为什么必须有（2026-11-15 用户定调："本地补齐各种工程的安全措施诊断"）：
实测 2026-11-12 抽检 40 条待办：**85% 是"函数级全过、端到端却红"**，
失败原因 404 路由未命中 20 条 / 响应字段不符 10 条 / 422 参数 4 条 / 状态码 3 条。
其中 404 与 422 属于**接线层**（本地能补，零 token），却一直被派给远端重写函数
——**付钱让远端修它修不到的东西**，这就是"废单"。

分诊输出四类：
    local_wiring  本地接线/适配能修（重接线、补 import、补适配视图）→ **不派单**
    contract      契约与实现对不上（签名/字段口径）→ 先修契约，不盲目派单
    remote_code   业务实现确实不对 → 派单（带证据）
    env           环境问题（依赖/端口/服务没起）→ 不派单，报环境
    unknown       判据不足 → 按 remote_code 处理，但记一笔（逼着补判据）

口径：**宁可不派单让它红着，也不花错钱**——错派的代价不只是钱，还会把
已通过的能力改坏（11-11 实测整文件重写打掉 9 条 done）。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# (正则, 类别, 动作, 人话)
RULES: list[tuple[str, str, str, str]] = [
    # 路由层：端点根本没接上 / 接到别的函数
    (r"404|Not Found|没??有匹配|无匹配函数|未命中|路由", "local_wiring", "rewire", "端点没接上（本地重接线）"),
    # 参数/请求体：适配视图能补
    (r"422|参数校验|请求体|body.*(缺失|为空)|invalid_request|Missing|required", "local_wiring", "rewire", "请求体/参数没供给（本地适配视图补）"),
    # 导入层：本地补 import
    (r"NameError|ImportError|ModuleNotFoundError|is not defined", "local_wiring", "fix_imports", "缺 import / 名字未定义（本地补）"),
    # 环境
    (r"连不上|Connection refused|ECONNREFUSED|端口|No module named '?(fastapi|django|httpx|pytest)", "env", "report_env", "环境问题（依赖/服务没起）"),
    (r"超时|timeout|Timeout", "env", "report_env", "超时（环境或性能，不是逻辑）"),
    # 业务实现
    (r"字段不符|值不符|实际.*!=|!=\s*\{|返回体|响应字段|state|status.*(期望|实际)", "remote_code", "dispatch", "实现与契约不符（派单）"),
    (r"500|Internal Server Error|Traceback|AttributeError|TypeError|KeyError|IndexError|ValueError", "remote_code", "dispatch", "运行期异常（业务实现，派单）"),
]

ACTION_OF = {
    "local_wiring": "rewire",
    "contract": "fix_contract",
    "remote_code": "dispatch",
    "env": "report_env",
    "unknown": "dispatch",
}

CLASS_ORDER = ["env", "local_wiring", "contract", "remote_code", "unknown"]


def classify(detail: str) -> tuple[str, str, str]:
    """一条失败证据 → (类别, 动作, 依据)。**第一条命中即返回**（规则按优先级排）。"""
    text = str(detail or "")
    for pattern, cls, action, why in RULES:
        m = re.search(pattern, text, re.I)
        if m:
            return cls, action, "%s（命中 %r）" % (why, m.group(0)[:24])
    return "unknown", ACTION_OF["unknown"], "证据里没有可判定的信号"


def classify_module(details: list) -> dict:
    """一个模块的多条失败证据 → 合并判定。

    同一模块可能同时有 404 和字段不符：**取最"该远端修"的那个**（保守）——
    因为本地修完接线后业务错还得远端改，先派单不会白跑；
    反过来只派 local_wiring 就会把业务错漏掉。
    """
    votes: dict[str, list[str]] = {}
    for d in (details or []):
        cls, action, why = classify(d)
        votes.setdefault(cls, []).append(why)
    if not votes:
        return {"class": "unknown", "action": ACTION_OF["unknown"],
                "why": "没有失败证据", "votes": {}}
    for cls in ("remote_code", "contract", "env", "local_wiring", "unknown"):
        if cls in votes:
            return {"class": cls, "action": ACTION_OF[cls],
                    "why": votes[cls][0], "votes": {k: len(v) for k, v in votes.items()}}
    return {"class": "unknown", "action": ACTION_OF["unknown"],
            "why": "未分类", "votes": {}}


def log(ws, rows: list) -> None:
    """把本轮分诊结果 append 进 `.sixcoder/triage.jsonl`（供成本报表 join）。"""
    if not rows:
        return
    p = Path(ws) / ".sixcoder" / "triage.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    except OSError:
        pass


def triage_round(ws, red_modules, details: dict, *, caps_of=None) -> dict:
    """一轮红灯 → 分诊结果 + 落盘。返回 {by_module, dispatch[], local[], env[], counts}。"""
    rows, out = [], {"by_module": {}, "dispatch": [], "local": [], "env": [],
                     "counts": {}}
    for mod in sorted(red_modules or []):
        verdict = classify_module((details or {}).get(mod) or [])
        caps = list(caps_of(mod) if caps_of else [])
        out["by_module"][mod] = verdict
        out["counts"][verdict["class"]] = out["counts"].get(verdict["class"], 0) + 1
        if verdict["action"] == "dispatch":
            out["dispatch"].append(mod)
        elif verdict["action"] == "rewire":
            out["local"].append(mod)
        elif verdict["action"] == "report_env":
            out["env"].append(mod)
        rows.append({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "module": mod,
                     "cap_id": ",".join(caps), "class": verdict["class"],
                     "action": verdict["action"], "why": verdict["why"],
                     "evidence": ((details or {}).get(mod) or [""])[0][:300]})
    log(ws, rows)
    return out


def describe(result: dict) -> str:
    c = result.get("counts") or {}
    return "红灯 %d 个：本地可修 %d / 该派单 %d / 环境 %d" % (
        sum(c.values()), c.get("local_wiring", 0), c.get("remote_code", 0),
        c.get("env", 0))
