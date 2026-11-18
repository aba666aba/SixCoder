"""项目级能力：项目地图 / 稳定基线冻结 / 模块角色 / 依赖序 / 规模估算。

为什么需要这一层（实测：毕设双选系统 15 模块 44 函数 3 次真跑）
  · 判据的知识面原来是"单模块自洽"：api.py 用 models.py 的 bonus 字段被判"凭空造字段"；
  · 失败即整层回滚：14/15 个模块退化成桩，21 次调用后净进度 = 0；
  · 编排层（app/api/server/cli）被算法层的复杂度尺子量，必红；
  · 15 模块触发"模块>8"升级到 L 级门禁，coverage/mutation 立刻阻断。

这四条对应四个能力：
  1. `build_map`   —— 项目地图：模块/签名/返回形状/字段/依赖方向，落盘可复用
  2. `freeze`      —— 稳定基线：已通过验收的函数锁住，后续不许重写
  3. `role_of`     —— 模块角色：编排层/算法层/数据层/展示层，质量阈值分档
  4. `order_modules` —— 依赖序：models → scoring → matching → api → app 批量推进
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

MAP_FILE = "project_map.json"
FROZEN_FILE = "frozen.json"

# 模块角色：决定质量阈值、写作粒度、门禁严格度
_ROLE_HINTS = (
    ("orchestration", ("app", "api", "server", "cli", "main", "router", "controller",
                       "编排", "入口", "接口层")),
    ("data", ("model", "store", "repo", "db", "schema", "persist", "数据", "存储", "模型")),
    ("algorithm", ("match", "score", "rank", "algo", "round", "pref", "算法", "匹配", "排名")),
    ("ui", ("webui", "ui", "view", "page", "html", "front", "页面", "前端")),
)


def role_of(rel: str, contract=None) -> str:
    """猜模块角色（写进项目地图，供阈值与写作策略复用）。"""
    low = (Path(str(rel)).stem + " " + str(getattr(contract, "raw", "") or "")[:200]).lower()
    for role, hints in _ROLE_HINTS:
        if any(h in low for h in hints):
            return role
    return "generic"


# 角色 → 复杂度/嵌套/单函数行数上限（编排层天生分支多，算法层要严）
ROLE_LIMITS = {
    "orchestration": {"cx": 25, "nest": 6, "lines": 120},
    "data": {"cx": 14, "nest": 4, "lines": 80},
    "algorithm": {"cx": 12, "nest": 4, "lines": 60},
    "ui": {"cx": 18, "nest": 5, "lines": 100},
    "generic": {"cx": 12, "nest": 4, "lines": 60},
}


def limits_for(rel: str, contract=None, base: dict | None = None) -> dict:
    """模块级质量阈值：以项目分级的档位为底，按角色放宽（只放宽编排/展示层）。"""
    role = role_of(rel, contract)
    lim = dict(base or {"cx": 12, "nest": 4, "lines": 60})
    if role in ("orchestration", "ui"):
        cand = ROLE_LIMITS[role]
        lim["cx"] = max(lim.get("cx", 12), cand["cx"])
        lim["nest"] = max(lim.get("nest", 4), cand["nest"])
        lim["lines"] = max(lim.get("lines", 60), cand["lines"])
    return lim


def build_map(contracts: dict) -> dict:
    """项目地图：模块 → 函数签名/返回形状/字段 + 依赖方向 + 角色 + 规模估算。"""
    mods: dict[str, dict] = {}
    fields: set[str] = set()
    for rel, c in (contracts or {}).items():
        funcs = []
        for f in getattr(c, "funcs", []) or []:
            funcs.append({
                "name": f.name,
                "params": [{"name": p.name, "type": p.type} for p in (f.params or [])],
                "returns": f.returns or "",
                "keys": list(getattr(f, "keys", []) or []),
                "owner": getattr(f, "owner_class", "") or "",
                "raises": list(getattr(f, "raises", []) or []),
            })
            fields |= {str(k) for k in (getattr(f, "keys", []) or [])}
            for ex in getattr(f, "examples", []) or []:
                for arg in getattr(ex, "args", []) or []:
                    fields |= {m.group(1) for m in re.finditer(r'"([a-z_]\w*)"\s*:', str(arg))}
                fields |= {m.group(1) for m in re.finditer(r'"([a-z_]\w*)"\s*:', str(ex.expect))}
        mods[rel] = {"role": role_of(rel, c), "funcs": funcs,
                     "module": getattr(c, "module", Path(rel).stem)}
    # 依赖：A 的模块名出现在 B 的契约原文里 → B 依赖 A
    deps: dict[str, list[str]] = {}
    for rel, info in mods.items():
        stem = info["module"]
        hit = []
        for other, oinfo in mods.items():
            if other == rel:
                continue
            raw = str(getattr(contracts.get(rel), "raw", "") or "")
            if re.search(rf"\b{re.escape(oinfo['module'])}\b", raw):
                hit.append(other)
        deps[rel] = sorted(hit)
    n_funcs = sum(len(v["funcs"]) for v in mods.values())
    return {"built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "modules": mods, "deps": deps, "fields": sorted(fields),
            "stats": {"modules": len(mods), "funcs": n_funcs,
                      "est_lines": n_funcs * 28}}   # 经验值：每函数约 28 行（含 docstring）


def save_map(ws: Path, m: dict) -> Path:
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    p = d / MAP_FILE
    p.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_map(ws: Path) -> dict:
    p = Path(ws) / ".sixcoder" / MAP_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                    # noqa: BLE001
        return {}


def global_keys(contracts: dict, saved: dict | None = None) -> set[str]:
    """全项目字段白名单（表列 + 所有示例里出现过的键 + 已落盘地图里的字段）。"""
    keys: set[str] = set((saved or {}).get("fields") or [])
    keys |= set(build_map(contracts).get("fields") or [])
    return {k for k in keys if re.match(r"^[A-Za-z_]\w*$", k)}


def order_modules(contracts: dict) -> list[str]:
    """按依赖序排模块：被依赖的在前（models→scoring→matching→api→app），无依赖按名字。"""
    m = build_map(contracts)
    deps = m["deps"]
    done: list[str] = []
    seen: set[str] = set()

    _prio = {"data": 0, "algorithm": 1, "generic": 2, "orchestration": 3, "ui": 4}
    def _role_first(rel):
        role = (m["modules"].get(rel) or {}).get("role") or "generic"
        return (_prio.get(role, 2), rel)
    guard = 0
    while len(done) < len(deps) and guard < 500:
        guard += 1
        progressed = False
        for rel in sorted(deps, key=_role_first):
            if rel in seen:
                continue
            if all(d in seen for d in deps[rel]):
                done.append(rel)
                seen.add(rel)
                progressed = True
        if not progressed:                       # 有环：剩下的按名字排，不卡死
            for rel in sorted(deps):
                if rel not in seen:
                    done.append(rel)
                    seen.add(rel)
            break
    return done


def freeze(ws: Path, rel: str, funcs: list[str], reason: str = "") -> dict:
    """把已通过验收的函数写进稳定基线（后续轮次不许重写）。"""
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    p = d / FROZEN_FILE
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:                                # noqa: BLE001
            data = {}
    cur = set((data.get(rel) or {}).get("funcs") or [])
    cur |= {str(f) for f in funcs if f}
    data[rel] = {"funcs": sorted(cur), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                 "why": reason or "通过机械判据"}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data


def load_frozen(ws: Path) -> dict[str, set[str]]:
    p = Path(ws) / ".sixcoder" / FROZEN_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                    # noqa: BLE001
        return {}
    return {k: set((v or {}).get("funcs") or []) for k, v in data.items()}


def prompt_block(ws: Path, contracts: dict, current: str = "") -> str:
    """给模型的**项目地图**（跨模块知识）：模块清单、角色、公开签名、依赖、字段。

    实例教训：没有这块，模型写 api.py 时不知道 models.py 产出的字段叫什么，
    于是造字段、字段名漂移，判据全红。
    """
    m = load_map(ws) or build_map(contracts)
    lines = ["【项目地图（自动生成，跨模块事实源）】"]
    st = m.get("stats") or {}
    lines.append(f"- 规模：{st.get('modules', 0)} 个模块 / {st.get('funcs', 0)} 个函数 / "
                 f"预计约 {st.get('est_lines', 0)} 行")
    lines.append(f"- 全局字段名（只能用这些，别自造）：{'/'.join((m.get('fields') or [])[:40])}")
    for rel, info in (m.get("modules") or {}).items():
        mark = " ← 本次要写" if rel == current else ""
        deps = (m.get("deps") or {}).get(rel) or []
        lines.append(f"- {rel}（角色 {info.get('role')}，依赖 {('、'.join(deps) or '无')}）{mark}")
        for f in (info.get("funcs") or [])[:12]:
            params = ", ".join(f"{p['name']}: {p['type']}" for p in f.get("params") or [])
            ret = f.get("returns") or "object"
            keys = f"/{'/'.join(f['keys'])}" if f.get("keys") else ""
            lines.append(f"    · {f['name']}({params}) -> {ret}{keys}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 规模化四件套
PROGRESS_FILE = "progress.json"
BASELINE_FILE = "regression_baseline.json"


def estimate(contracts: dict, cny_per_call: float = 0.02, sec_per_call: float = 20.0) -> dict:
    """预算↔行数换算：**跑之前**就能算出这个项目要多少调用/多少钱/多久。

    依据（实测）：单模块整文件写入平均 1–3 次调用、约 20–40 秒、¥0.003–0.03。
    这里按"每 4 个函数 ≈ 1 次调用"估，宁可高估不要低估（低估会中途断粮）。
    """
    m = build_map(contracts)
    st = m.get("stats") or {}
    funcs = int(st.get("funcs") or 0)
    calls = max(1, (funcs + 3) // 4) * 2            # 每个模块平均 2 次（含一次重试）
    return {"modules": st.get("modules", 0), "funcs": funcs,
            "est_lines": st.get("est_lines", 0), "est_calls": calls,
            "est_cny": round(calls * cny_per_call, 4),
            "est_min": round(calls * sec_per_call / 60.0, 1)}


# 预算封顶/缩范围逻辑已于 2026-11-10 移除：用户不再可设上限，
# 唯一停机线是系统总预算（见 core/budget_estimate.py 的 system_budget_stopped）。


def batch_plan(modules: list[str], size: int = 4) -> list[list[str]]:
    """分批调度：把模块按依赖序切成若干批（每批 size 个），批间冻结+回归。"""
    ms = [m for m in (modules or []) if m]
    size = max(1, int(size or 4))
    return [ms[i:i + size] for i in range(0, len(ms), size)]


def save_progress(ws: Path, rel: str, patch: dict) -> Path:
    """断点续跑：把"这个模块写到哪儿了"落盘（已验证函数/失败函数/尝试次数/花费）。"""
    p = Path(ws) / ".sixcoder" / PROGRESS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:                                # noqa: BLE001
            data = {}
    cur = data.get(rel) or {}
    cur.update(patch or {})
    cur["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    data[rel] = cur
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load_progress(ws: Path) -> dict:
    p = Path(ws) / ".sixcoder" / PROGRESS_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:                                    # noqa: BLE001
        return {}


def save_baseline(ws: Path, state: dict, tag: str = "") -> Path:
    """跨批回归基线：记录这一刻每个模块的验收数（下一批跑完拿来比）。"""
    p = Path(ws) / ".sixcoder" / BASELINE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    snap = {rel: {"verified": int((v or {}).get("verified") or 0),
                  "funcs": int((v or {}).get("funcs") or 0)}
            for rel, v in (state or {}).items()}
    data = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag, "modules": snap,
            "total_verified": sum(x["verified"] for x in snap.values())}
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def diff_baseline(ws: Path, state: dict) -> dict:
    """和上一份基线比：哪些模块**退化**了（验收数掉了）。退化必须报警，不许静默。"""
    p = Path(ws) / ".sixcoder" / BASELINE_FILE
    if not p.exists():
        return {"has_baseline": False, "regressed": [], "improved": []}
    try:
        old = (json.loads(p.read_text(encoding="utf-8", errors="replace")).get("modules") or {})
    except Exception:                                    # noqa: BLE001
        return {"has_baseline": False, "regressed": [], "improved": []}
    reg, imp = [], []
    for rel, v in (state or {}).items():
        was = int((old.get(rel) or {}).get("verified") or 0)
        now = int((v or {}).get("verified") or 0)
        if now < was:
            reg.append(f"{rel} {was}→{now}")
        elif now > was:
            imp.append(f"{rel} {was}→{now}")
    return {"has_baseline": True, "regressed": reg, "improved": imp,
            "note": ("有模块退化（已回滚保护）：" + "；".join(reg[:4])) if reg
                    else ("全部不退化" + (f"，推进：{'；'.join(imp[:4])}" if imp else ""))}


def regression_guard(ws: Path, state: dict) -> dict:
    """跨批回归**硬门禁**：退化的模块必须重新变成"待办"（不是只发个通知就放过）。

    为什么必须是硬的：实测出现过 `store.py 已验收数 2→0`，如果只报警，下一批会在
    一个悄悄变差的基线上继续叠加，最后谁也说不清是哪一批弄坏的。
    """
    dif = diff_baseline(ws, state)
    if not dif.get("has_baseline"):
        return {"blocked": [], "reason": "没有基线可比"}
    blocked: list[str] = []
    for item in dif.get("regressed") or []:
        rel = str(item).split()[0]
        blocked.append(rel)
    return {"blocked": blocked,
            "reason": (f"退化的模块要强制重写：{', '.join(blocked)}" if blocked
                       else "无退化")}


def resume_plan(ws: Path, contracts: dict, state: dict,
                 red_modules=None) -> dict:
    """断点续跑：进程重启后该从哪儿接着写。

    依据 `.sixcoder/progress.json`（每模块写到哪儿）+ 当前判据状态：
      · done     已经满分（不必再写）
      · resume   上次写过但有函数没通过 → 从这里接着写，带上"已验证函数"清单
      · fresh    完全没动过

    **`red_modules` = 端到端红灯的模块**（2026-11-11，§11.4c 的延伸）：
    契约全过 ≠ 做完了 —— 端到端还红的模块**不算 done**，要算成"可续写"。
    不加这一条的后果（实测 2026-11-11 16:00）：契约层 2 个模块全过、
    端到端还有 2 条红 → `resume=0 fresh=0` → 引擎认为"**全部完成**"
    → **一轮都不写码** → 只做"片内自验"（远端空转，¥0.02–0.05/次）
    → 重规划 → 再空转 → 覆盖 2 小时零增长、照烧钱。
    **判定层必须看业务事实（端到端），不能只看契约层。**
    """
    prog = load_progress(ws)
    frozen = load_frozen(ws)
    red = {str(m) for m in (red_modules or ())}
    done, resume, fresh = [], [], []
    for rel in order_modules(contracts):
        st = (state or {}).get(rel) or {}
        funcs = int(st.get("funcs") or 0)
        ver = int(st.get("verified") or 0)
        if funcs and ver >= funcs and rel not in red:
            done.append(rel)
            continue
        rec = prog.get(rel) or {}
        if rec:
            resume.append({"path": rel, "verified": list(rec.get("verified") or []),
                           "attempts": int(rec.get("attempts") or 0),
                           "stubs": list(rec.get("stubs") or []),
                           "frozen": sorted(frozen.get(rel) or [])})
        else:
            fresh.append(rel)
    return {"done": done, "resume": resume, "fresh": fresh,
            "next": (resume[0]["path"] if resume else (fresh[0] if fresh else "")),
            "note": (f"已完成 {len(done)}、可续写 {len(resume)}、全新 {len(fresh)}；"
                     f"下一个应该写 {resume[0]['path'] if resume else (fresh[0] if fresh else '（全部完成）')}")}
