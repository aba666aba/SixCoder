"""跨模块冒烟 + 业务流 e2e：证明"模块接得上"与"业务流程是对的"。

两层都补的是单函数判据看不见的东西：
  · integration_smoke —— 所有模块一起 import、公开函数按签名可调用
  · business_flow     —— 跑一条端到端业务链，断言通用业务不变量
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

RUNNER = r'''
import importlib, json, os, sys, traceback
sys.path.insert(0, sys.argv[1])
spec = json.loads(sys.argv[2])
out = {"imports": {}, "calls": {}}
mods = {}
for stem in spec["modules"]:
    try:
        mods[stem] = importlib.import_module(stem)
        out["imports"][stem] = "ok"
    except Exception as exc:
        out["imports"][stem] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
SAMPLE = {"int": 0, "float": 0.0, "str": "", "bool": False, "dict": {},
          "list[dict]": [], "list[str]": [], "list[int]": [], "list[float]": [], "object": None}
for rel, stem, fname, params in spec["calls"]:
    m = mods.get(stem)
    if m is None or not hasattr(m, fname):
        out["calls"]["%s.%s" % (stem, fname)] = "missing"
        continue
    args = [SAMPLE.get((p or "object").replace(" ", "").lower(), None) for p in params]
    try:
        getattr(m, fname)(*args)
        out["calls"]["%s.%s" % (stem, fname)] = "ok"
    except ValueError as exc:
        out["calls"]["%s.%s" % (stem, fname)] = "value_error(合法拒绝)"
    except Exception as exc:
        out["calls"]["%s.%s" % (stem, fname)] = "%s: %s" % (type(exc).__name__, str(exc)[:90])
print(json.dumps(out))
'''


def integration_smoke(ws: Path, contracts: dict, timeout: int = 120) -> dict:
    """跑一次跨模块集成冒烟，返回 {ok, imports, calls, fails}。"""
    ws = Path(ws).resolve()
    try:
        from . import project as _proj
        m = _proj.build_map(contracts)
    except Exception:                                    # noqa: BLE001
        m = {"modules": {}, "deps": {}}
    mods = sorted({(info.get("module") or Path(rel).stem)
                   for rel, info in (m.get("modules") or {}).items()})
    calls = []
    for rel, info in (m.get("modules") or {}).items():
        stem = info.get("module") or Path(rel).stem
        for f in (info.get("funcs") or [])[:8]:
            if f.get("owner"):                            # 类方法不能在模块级别调用
                continue
            calls.append([rel, stem, f["name"],
                          [p.get("type") for p in (f.get("params") or [])]])
    tmp = ws / ".sixcoder" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    runner = tmp / "_flow_run.py"
    runner.write_text(RUNNER, encoding="utf-8")
    spec = json.dumps({"modules": mods, "calls": calls}, ensure_ascii=False)
    try:
        p = subprocess.run([sys.executable, "-B", str(runner), str(ws), spec],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", cwd=str(tmp),
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "imports": {}, "calls": {},
                "fails": [f"集成冒烟超时 {timeout}s"]}
    finally:
        try:
            runner.unlink()
        except OSError:
            pass
    lines = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "imports": {}, "calls": {},
                "fails": ["集成冒烟没有输出：" + (p.stderr or "")[-200:]]}
    try:
        got = json.loads(lines[-1])
    except Exception:                                    # noqa: BLE001
        return {"ok": False, "imports": {}, "calls": {},
                "fails": ["集成冒烟输出不可解析：" + lines[-1][:200]]}
    fails: list[str] = []
    for stem, how in (got.get("imports") or {}).items():
        if how != "ok":
            fails.append(f"模块 import 失败：{stem}（{how}）")
    for name, how in (got.get("calls") or {}).items():
        if how not in ("ok", "value_error(合法拒绝)"):
            fails.append(f"跨模块调用失败：{name}（{how}）")
    return {"ok": not fails, "imports": got.get("imports") or {},
            "calls": got.get("calls") or {}, "fails": fails[:8]}


def report_lines(res: dict) -> list[str]:
    if res.get("ok"):
        return [f"- 集成冒烟：通过（{len(res.get('imports') or {})} 个模块可一起 import、"
                f"{len(res.get('calls') or {})} 个公开函数可调用）"]
    return ["- **集成冒烟：未通过** —— " + "；".join((res.get("fails") or [])[:3])
            + "（模块之间接不上，单函数判据全绿也不算可用）"]


# ============================================================ 业务流 e2e
#
# 补的是这个洞：集成冒烟只证明"接得上"，单函数判据只证明"自己符合契约"。
# 两边全绿时，下面这些业务错误照样能过：
#   · 同一个申请者被分配了两个名额（重复占用）
#   · 某个名额方被超额分配（容量越界）
#   · 低优先级的申请者抢走了高优先级者的靠前偏好（优先级被违反）
#   · 汇总口径对不上：未分配 + 已分配 ≠ 总数（统计不自洽）
#
# 配方是**通用"稀缺资源分配"形态**（排期、派单、分房、分机、名额分配都属于它）：
#   造申请者 → 造名额方 → 造偏好 → 排名 → 贪心分配 → 补录 → 汇总
#
# 不绑定任何具体领域：
#   · 角色名走同义词表（BIZ_ROLES），按契约里的函数名反查模块；
#   · 字段名走别名表（FIELD_ALIASES / SUMMARY_ALIASES），运行时从产物对象里解析；
#   · 解析不出来就如实写"不适用"，绝不当成通过。

BIZ_ROLES = {
    "make_applicant": ("make_student", "make_applicant", "make_candidate",
                       "make_requester", "make_client"),
    "make_slot": ("make_teacher", "make_slot", "make_resource", "make_position",
                  "make_worker"),
    "make_preference": ("make_choice", "make_preference", "make_bid", "make_pick",
                        "make_request"),
    "rank": ("rank_students", "rank_applicants", "rank_candidates", "sort_by_score",
             "rank_atoms", "rank"),
    "allocate": ("greedy_match", "allocate", "assign", "greedy_assign", "match"),
    "fill": ("second_round", "fill_round", "second_pass", "supplement", "top_up"),
    "summary": ("summary", "stats", "summarize", "totals"),
}

BIZ_OPTIONAL_ROLES = {
    "free_slot": ("free_quota", "free_capacity", "remaining", "leftover_capacity"),
    "unassigned_of": ("unmatched_students", "unassigned", "leftovers", "pending"),
    "by_slot": ("by_teacher", "by_slot", "per_slot", "per_resource"),
}

# 语义字段 → 候选键名（按顺序取第一个真实存在的）
FIELD_ALIASES = {
    "applicant_id": ("sid", "applicant_id", "aid", "cid", "uid", "id"),
    "slot_id": ("tid", "slot_id", "rid", "pid", "wid", "id"),
    "capacity": ("quota", "capacity", "limit", "slots", "max_slots"),
    "score": ("total", "score", "weight", "rank_score"),
    "pref_rank": ("rank", "priority", "pref", "order"),
    "method": ("method", "via", "how", "source"),
}

SUMMARY_ALIASES = {
    "total": ("students", "applicants", "candidates", "total", "entities"),
    "assigned": ("pairs", "assigned", "matches", "allocations", "allocated"),
    "unassigned": ("unmatched", "unassigned", "left", "pending", "remaining"),
}

BIZ_RUNNER = r'''
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
spec = json.loads(sys.argv[2])
out = {"checks": [], "error": "", "skipped": ""}


def add(name, ok, detail=""):
    out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})


mods = {}
for role, pair in (spec.get("roles") or {}).items():
    stem = pair[0]
    if stem in mods:
        continue
    try:
        mods[stem] = importlib.import_module(stem)
    except Exception as exc:
        out["error"] = "import %s 失败：%s: %s" % (stem, type(exc).__name__, str(exc)[:120])
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(0)


def fn(role):
    pair = (spec.get("roles") or {}).get(role)
    if not pair:
        return None
    stem, name = pair
    m = mods.get(stem)
    if m is None or not hasattr(m, name):
        return None
    return getattr(m, name)


missing = [r for r in spec["required"] if fn(r) is None]
if missing:
    out["skipped"] = "契约里找不到这些角色对应的函数：" + "、".join(missing)
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)


def pick(obj, cands):
    """从产物对象里解析出语义字段的真实键名（解析不出返回空串）。"""
    if not isinstance(obj, dict):
        return ""
    for k in cands:
        if k in obj:
            return k
    return ""


# ---- 先造一份探针对象，用它们反查字段别名（真正干活用的对象随后再造）
make_applicant, make_slot = fn("make_applicant"), fn("make_slot")
make_pref = fn("make_preference")
rank_fn, allocate_fn = fn("rank"), fn("allocate")
summary_fn, fill_fn = fn("summary"), fn("fill")
free_fn, unassigned_fn, by_slot_fn = fn("free_slot"), fn("unassigned_of"), fn("by_slot")

try:
    probe_a = make_applicant("p1", "probe", 80.0, 0.0)
    probe_s = make_slot("q1", "probe", 1)
    probe_p = make_pref("p1", "q1", 1)
except Exception as exc:
    out["error"] = "构造探针对象失败：%s: %s" % (type(exc).__name__, str(exc)[:160])
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)

FA = spec["fields"]
K_AID = pick(probe_a, FA["applicant_id"])
K_SCORE = pick(probe_a, FA["score"])
K_SID = pick(probe_s, FA["slot_id"])
K_CAP = pick(probe_s, FA["capacity"])
K_PA = pick(probe_p, FA["applicant_id"])
K_PS = pick(probe_p, FA["slot_id"])
K_PR = pick(probe_p, FA["pref_rank"])
K_METHOD = pick(probe_p, FA["method"])
bad_keys = [n for n, k in (("申请人ID", K_AID), ("分数", K_SCORE), ("名额方ID", K_SID),
                           ("容量", K_CAP), ("偏好-申请人", K_PA), ("偏好-名额方", K_PS),
                           ("偏好优先级", K_PR)) if not k]
if bad_keys:
    out["skipped"] = ("产物对象的字段名无法识别：" + "、".join(bad_keys) +
                      "（可在 core/flow.py 的 FIELD_ALIASES 里补别名）")
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)


def scenario(label, caps, aid_names):
    """跑一条业务链：造对象 → 排名 → 分配 → 补录 → 汇总。"""
    slots = [make_slot(sid, "S" + sid, c) for sid, c in caps]
    applicants = [make_applicant(aid, "A" + aid, base, extra) for aid, base, extra in aid_names]
    prefs = []
    for a, s, r in spec["prefs"]:
        prefs.append(make_pref(a, s, r))
    order = list(rank_fn(applicants) or [])
    expect = [a for a, _b, _e in aid_names]
    add("%s：排名字段与分数一致" % label, order == expect, "得到 %s，应为 %s" % (order[:6], expect))
    first = list(allocate_fn(applicants, slots, prefs, {}) or [])
    extra = list(fill_fn(applicants, slots, first, prefs) or []) if fill_fn else []
    assigns = first + extra
    stat = summary_fn(applicants, slots, assigns)
    return applicants, slots, prefs, assigns, stat


def inv_unique(applicants, slots, prefs, assigns, stat, label):
    seen, dup = set(), []
    for p in assigns:
        if p.get(K_PA) in seen:
            dup.append(p.get(K_PA))
        seen.add(p.get(K_PA))
    add("%s：① 同一申请者只能被分配一次" % label, not dup, "重复分配 %s" % dup[:3])


def inv_capacity(applicants, slots, prefs, assigns, stat, label):
    cap = {s[K_SID]: int(s[K_CAP]) for s in slots}
    cnt = {}
    for p in assigns:
        cnt[p.get(K_PS)] = cnt.get(p.get(K_PS), 0) + 1
    bad = ["%s 超容量 %d/%d" % (sid, n, cap.get(sid, 0))
           for sid, n in cnt.items() if n > cap.get(sid, 0)]
    add("%s：② 名额方不能被超额分配" % label, not bad, "；".join(bad[:3]))


def inv_priority(applicants, slots, prefs, assigns, stat, label):
    """③ 高优先级占优：**比实际拿到的更靠前**的偏好没满足时，
    那个名额方必须已满、且占位者的分数不比自己低。

    只比"更靠前"的偏好：已经拿到优先级 1 的对象，"没拿到优先级 2" 不是违规
    （第一版判据没排除这种情况，正确实现会被误判红——实测踩过）。
    未分配、或去处不在自己的偏好里（补录）时，按全部偏好比较。
    """
    by_aid = {a[K_AID]: a for a in applicants}
    cap = {s[K_SID]: int(s[K_CAP]) for s in slots}
    got_of = {}
    for p in assigns:
        got_of.setdefault(p.get(K_PA), p.get(K_PS))
    bad = []
    for a in applicants:
        aid = a[K_AID]
        got = got_of.get(aid)
        got_rank = None
        if got is not None:
            for c in prefs:
                if c.get(K_PA) == aid and c.get(K_PS) == got:
                    got_rank = int(c[K_PR])
        for c in prefs:
            if c.get(K_PA) != aid:
                continue
            if got_rank is not None and int(c[K_PR]) >= got_rank:
                continue
            occ = [p for p in assigns if p.get(K_PS) == c.get(K_PS)]
            if len(occ) < cap.get(c.get(K_PS), 0):
                bad.append("%s 的优先级 %s 偏好 %s 还有空位却没分到"
                           % (aid, c[K_PR], c.get(K_PS)))
                continue
            best = max([by_aid[x[K_PA]][K_SCORE] for x in occ if x.get(K_PA) in by_aid] or [-1])
            if best < a[K_SCORE]:
                bad.append("%s(%.2f) 没分到优先级 %s 的 %s，占位者最高只有 %.2f"
                           % (aid, a[K_SCORE], c[K_PR], c.get(K_PS), best))
    add("%s：③ 高优先级者优先拿到靠前偏好" % label, not bad, "；".join(bad[:3]))


def inv_summary(applicants, slots, prefs, assigns, stat, label):
    """④ 汇总自洽：未分配 + 已分配 == 总数，且与实际一致。"""
    SA = spec["summary_fields"]
    bad = []
    try:
        n_total = int(stat.get(pick(stat, SA["total"])))
        n_assigned = int(stat.get(pick(stat, SA["assigned"])))
        n_un = int(stat.get(pick(stat, SA["unassigned"])))
    except Exception as exc:
        add("%s：④ 汇总字典口径完整" % label, False, "%s: %s" % (type(exc).__name__, exc))
        return
    if n_total != len(applicants):
        bad.append("总数=%d，实际 %d" % (n_total, len(applicants)))
    if n_assigned != len(assigns):
        bad.append("已分配=%d，实际 %d" % (n_assigned, len(assigns)))
    real_un = len({a[K_AID] for a in applicants} - {p.get(K_PA) for p in assigns})
    if n_un != real_un:
        bad.append("未分配=%d，实际 %d" % (n_un, real_un))
    if n_un + n_assigned != n_total:
        bad.append("未分配+已分配=%d ≠ 总数=%d" % (n_un + n_assigned, n_total))
    add("%s：④ 未分配 + 已分配 == 总数" % label, not bad, "；".join(bad[:3]))


def inv_optional(applicants, slots, prefs, assigns, stat, label):
    cap = {s[K_SID]: int(s[K_CAP]) for s in slots}
    cnt = {}
    for p in assigns:
        cnt[p.get(K_PS)] = cnt.get(p.get(K_PS), 0) + 1
    if free_fn is not None:
        try:
            fq = free_fn(slots, assigns)
            bad = ["%s 剩余 %s ≠ %d" % (sid, fq.get(sid), cap[sid] - cnt.get(sid, 0))
                   for sid in cap if int(fq.get(sid, -999)) != cap[sid] - cnt.get(sid, 0)]
            add("%s：剩余容量与已分配自洽" % label, not bad, "；".join(bad[:3]))
        except Exception as exc:
            add("%s：剩余容量与已分配自洽" % label, False,
                "%s: %s" % (type(exc).__name__, str(exc)[:120]))
    if unassigned_fn is not None:
        try:
            got = list(unassigned_fn(applicants, assigns) or [])
            want = {a[K_AID] for a in applicants} - {p.get(K_PA) for p in assigns}
            add("%s：未分配名单与实际一致" % label, set(got) == want,
                "得到 %s，应为 %s" % (got, sorted(want)))
        except Exception as exc:
            add("%s：未分配名单与实际一致" % label, False,
                "%s: %s" % (type(exc).__name__, str(exc)[:120]))
    if by_slot_fn is not None:
        try:
            bs = by_slot_fn(slots, assigns)
            bad = ["%s=%s，实际 %d" % (sid, bs.get(sid), cnt.get(sid, 0))
                   for sid in cap if int(bs.get(sid, 0)) != cnt.get(sid, 0)]
            add("%s：按名额方统计与实际一致" % label, not bad, "；".join(bad[:3]))
        except Exception as exc:
            add("%s：按名额方统计与实际一致" % label, False,
                "%s: %s" % (type(exc).__name__, str(exc)[:120]))


try:
    n0 = len(out["checks"])
    for label, caps in (("容量充足", spec["caps_loose"]), ("容量紧张", spec["caps_tight"])):
        s = scenario(label, caps, spec["applicants"])
        inv_unique(*s, label=label)
        inv_capacity(*s, label=label)
        inv_priority(*s, label=label)
        inv_summary(*s, label=label)
        inv_optional(*s, label=label)
    if len(out["checks"]) - n0 < 8:          # 防"零断言也算绿"
        out["error"] = "业务流断言不足 8 条（跑出来的判据太少，不算通过）"
except Exception as exc:
    out["error"] = "业务流执行异常：%s: %s" % (type(exc).__name__, str(exc)[:200])
print(json.dumps(out, ensure_ascii=False))
'''

# 固定夹具：3 个申请者（权重 95 / 80 / 70）、5 条偏好、两套容量
BIZ_APPLICANTS = [("a1", 90.0, 5.0), ("a2", 80.0, 0.0), ("a3", 70.0, 0.0)]
BIZ_PREFS = [("a1", "s1", 1), ("a1", "s2", 2),
             ("a2", "s2", 1), ("a2", "s1", 2),
             ("a3", "s1", 1)]


def _role_map(contracts: dict) -> dict:
    """角色 → (模块 stem, 函数名)：按同义词表反查，不写死文件名与函数名。"""
    table = {**{r: v for r, v in BIZ_ROLES.items()},
             **{r: v for r, v in BIZ_OPTIONAL_ROLES.items()}}
    roles: dict = {}
    for rel, c in (contracts or {}).items():
        stem = Path(rel).stem
        for f in getattr(c, "funcs", []) or []:
            name = getattr(f, "name", "")
            for role, cands in table.items():
                if role in roles:
                    continue
                if name in cands:
                    roles[role] = [stem, name]
    return roles


def business_flow(ws: Path, contracts: dict, timeout: int = 180) -> dict:
    """跑一条通用业务链并断言业务不变量。

    返回 {ok, applicable, passed, total, checks, fails, reason}。
    诚实边界：角色或字段解析不出来 → `applicable=False`，**不算通过**；
    判据跑不动 → ok=False 并写明原因。
    """
    ws = Path(ws).resolve()
    roles = _role_map(contracts)
    missing = [r for r in BIZ_ROLES if r not in roles]
    if missing:
        return {"ok": False, "applicable": False, "passed": 0, "total": 0, "checks": [],
                "fails": [],
                "reason": "契约里找不到这些角色对应的函数：" + "、".join(missing)}
    tmp = ws / ".sixcoder" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    runner = tmp / "_biz_run.py"
    runner.write_text(BIZ_RUNNER, encoding="utf-8")
    spec = json.dumps({
        "roles": roles,
        "required": list(BIZ_ROLES),
        "fields": FIELD_ALIASES,
        "summary_fields": SUMMARY_ALIASES,
        "applicants": BIZ_APPLICANTS,
        "prefs": BIZ_PREFS,
        "caps_loose": [["s1", 1], ["s2", 2]],
        "caps_tight": [["s1", 1], ["s2", 1]],
    }, ensure_ascii=False)
    try:
        p = subprocess.run([sys.executable, "-B", str(runner), str(ws), spec],
                           capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace", cwd=str(tmp),
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "applicable": True, "passed": 0, "total": 0, "checks": [],
                "fails": [f"业务流超时 {timeout}s"]}
    finally:
        try:
            runner.unlink()
        except OSError:
            pass
    lines = [ln for ln in (p.stdout or "").strip().splitlines() if ln.strip()]
    if not lines:
        return {"ok": False, "applicable": True, "passed": 0, "total": 0, "checks": [],
                "fails": ["业务流没有输出：" + (p.stderr or "")[-200:]]}
    try:
        got = json.loads(lines[-1])
    except Exception:                                    # noqa: BLE001
        return {"ok": False, "applicable": True, "passed": 0, "total": 0, "checks": [],
                "fails": ["业务流输出不可解析：" + lines[-1][:200]]}
    if got.get("skipped"):
        return {"ok": False, "applicable": False, "passed": 0, "total": 0, "checks": [],
                "fails": [], "reason": got["skipped"]}
    checks = got.get("checks") or []
    passed = sum(1 for c in checks if c.get("ok"))
    fails = [f"{c.get('name')}（{c.get('detail') or ''}）" for c in checks if not c.get("ok")]
    if got.get("error"):
        fails.insert(0, got["error"])
    return {"ok": bool(checks) and not fails, "applicable": True,
            "passed": passed, "total": len(checks),
            "checks": checks, "fails": fails[:8]}


def business_report_lines(res: dict) -> list[str]:
    if res.get("applicable") is False:
        return ["- 业务流 e2e：**不适用**（" + (res.get("reason") or "没有这条业务链") +
                "）—— 不算通过，也不当成失败"]
    if res.get("ok"):
        names = [c.get("name") for c in (res.get("checks") or []) if c.get("ok")]
        return [f"- **业务流 e2e：通过**（{res.get('passed')}/{res.get('total')} 条业务不变量："
                + "、".join(names[:6]) + "）"]
    return ["- **业务流 e2e：未通过** —— " + "；".join((res.get("fails") or [])[:3])
            + "（函数级判据全绿也拦不住这类业务错误）"]
