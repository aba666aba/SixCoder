"""规模推导 + 层数规划（开发文档 §8.2 的补充件）。

两个必须分开的概念（混用会同时出错）：
  · **门禁档 S/M/L/XL**（`core/scale.py`）：决定"上几道闸门"——S 档不启用架构测试，
    10 万行以上才谈架构守护。它按 10 万 / 50 万 / 100 万划线。
  · **规划层数**（本模块）：决定"拆到多细"——2 层还是 5 层、冻结粒度是文件还是接口。
    3 万行的项目在门禁档里是 S，但规划上必须拆到接口层。

目标行数三条来源，按可信度排序：
  1. 需求里**显式声明**（`30000 行` / `3 万行` / `代码规模：M`）——有就以实现声明为准；
  2. 需求**内容折算**（章节/验收条目/接口/实体 → 行数区间）；
  3. **蓝图折算**（能力数 × 每能力函数数 × 每函数 15–25 行）。
三者差距 > 3 倍时判"需求信息不足"，如实报告，不硬猜。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PLAN_FILE = ".sixcoder/plan_scale.json"

# 层数档：(上界行数, 层数, 切片文件上限, 切片函数上限, 冻结粒度)
_LEVELS = [
    (2_000, 2, 2, 6, "文件"),
    (10_000, 3, 3, 9, "文件 + 签名"),
    (50_000, 4, 3, 9, "接口清单"),
    (0, 5, 5, 12, "子系统级接口"),
]

_LINES = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*万\s*行|(\d{3,7})\s*行")
_TIER = re.compile(r"(?:代码规模|目标规模|规模)\s*[:：]?\s*(S|M|L|XL)\b", re.I)


def declared(requirement: str) -> dict:
    """显式声明的目标规模：{lines, tier, raw}。没写返回 0/空。"""
    text = str(requirement or "")
    m = _LINES.search(text)
    lines = 0
    if m:
        if m.group(1):
            lines = int(float(m.group(1)) * 10_000)
        else:
            lines = int(m.group(2))
    t = _TIER.search(text)
    return {"lines": lines, "tier": (t.group(1).upper() if t else ""), "raw": m.group(0) if m else ""}


def counts(requirement: str) -> dict:
    """需求里可数的事实（每一条都要能被人复核）。"""
    text = str(requirement or "")
    sections = len(re.findall(r"^\s*##+\s+\S", text, re.M))
    items = len(re.findall(r"^\s*(?:\d+[.、]|[-*·])\s+\S", text, re.M))
    apis = len(set(re.findall(r"/(?:api|v\d)/[\w/{}.-]+", text)))
    entities = len(set(re.findall(r"(?:表|实体)[：: ]?[`\"]?([A-Za-z_\u4e00-\u9fa5]{2,12})", text)))
    acceptance = text.count("验收")
    roles = len(set(re.findall(r"(学生|教师|管理员|教务|院系|评审|考生|顾客|用户|运营)", text)))
    return {"sections": sections, "items": items, "apis": apis, "entities": entities,
            "acceptance": acceptance, "roles": roles, "chars": len(text)}


def estimate(requirement: str, *, caps: int = 0, funcs: int = 0) -> dict:
    """内容折算 + 蓝图折算，给区间和中位数。系数写在这里，改系数就是改口径。"""
    c = counts(requirement)
    content = (1_500                      # 最小可用系统的地板
               + c["sections"] * 400      # 一个章节 ≈ 一块功能
               + c["acceptance"] * 150    # 一条验收 ≈ 一条函数链
               + c["apis"] * 60           # 一个 HTTP 接口 ≈ 60 行
               + c["entities"] * 180      # 一个实体 ≈ 数据/服务/API 各一小块
               + c["roles"] * 120)        # 一个角色 ≈ 一组权限与页面
    by_blueprint = int(caps * max(1, funcs) * 20) if caps else 0
    candidates = [content] + ([by_blueprint] if by_blueprint else [])
    mid = int(sum(candidates) / len(candidates))
    return {"content": content, "by_blueprint": by_blueprint, "estimated": mid,
            "low": int(mid * 0.7), "high": int(mid * 1.5), "counts": c,
            "basis": [f"章节 {c['sections']} × 400", f"验收 {c['acceptance']} × 150",
                      f"接口 {c['apis']} × 60", f"实体 {c['entities']} × 180",
                      f"角色 {c['roles']} × 120", "地板 1500"]
                     + ([f"蓝图 {caps} 能力 × {funcs} 函数 × 20"] if caps else [])}


def layers_for(target_lines: int) -> dict:
    """目标行数 → 需要几层、切片多大、冻结到什么粒度。"""
    t = int(target_lines or 0)
    for cap, layers, files, funcs, freeze in _LEVELS:
        if cap == 0 or t < cap:
            return {"target": t, "layers": layers,
                    "levels": ["L0", "L1", "L2", "L3", "L4"][:layers],
                    "slice_files": files, "slice_funcs": funcs, "freeze": freeze,
                    "band": ("< 2 千" if cap == 2_000 else
                             "2 千–1 万" if cap == 10_000 else
                             "1 万–5 万" if cap == 50_000 else "≥ 5 万")}
    return {}


def plan(requirement: str, ws=None, *, caps: int = 0, funcs: int = 0) -> dict:
    """综合判定：声明优先；无声明时用折算；差距过大时如实标记。"""
    d = declared(requirement)
    e = estimate(requirement, caps=caps, funcs=funcs)
    if d["lines"]:
        target, source = d["lines"], "declared"
    else:
        target, source = e["estimated"], "estimated"
    conflict = False
    if d["lines"] and e["estimated"]:
        ratio = max(d["lines"], e["estimated"]) / max(1, min(d["lines"], e["estimated"]))
        conflict = ratio > 3
    lay = layers_for(target)
    out = {"target": int(target), "source": source, "declared": d, "estimate": e,
           "layers": lay, "conflict": conflict,
           "note": ("需求声明与内容折算差 %.1f 倍：按声明规划，但内容信息量可能不足以支撑"
                    % (max(d['lines'], e['estimated']) / max(1, min(d['lines'], e['estimated'])))
                    if conflict else "")}
    if ws is not None:
        try:
            p = Path(ws) / PLAN_FILE
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
    return out


def load(ws) -> dict:
    try:
        return json.loads((Path(ws) / PLAN_FILE).read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def summary_lines(res: dict) -> list[str]:
    if not res:
        return []
    lay = res.get("layers") or {}
    src = {"declared": "需求声明", "estimated": "需求内容折算"}.get(res.get("source"), "未知")
    lines = [f"- 目标规模：**{res.get('target', 0)} 行**（来源：{src}）",
             f"- 规划层数：{lay.get('layers', 0)} 层（{lay.get('band', '')}）｜"
             f"冻结粒度：{lay.get('freeze', '')}｜切片上限 {lay.get('slice_files', 0)} 文件 / "
             f"{lay.get('slice_funcs', 0)} 函数",
             f"- 内容折算：{res.get('estimate', {}).get('low', 0)}–"
             f"{res.get('estimate', {}).get('high', 0)} 行"
             f"（{res.get('estimate', {}).get('estimated', 0)}）"]
    if res.get("conflict"):
        lines.append(f"- **注意**：{res.get('note')}")
    return lines
