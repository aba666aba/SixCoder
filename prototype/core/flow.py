"""跨模块集成冒烟：证明"模块之间接得上"，这是单函数判据看不见的部分。"""
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
# 为什么要有这一层（这是 44/44 之后剩下的那个洞）：集成冒烟只证明"模块接得上"，
# 每个函数也只证明"符合自己的契约"。两边都绿，业务照样可能是错的——
# 学生被两个教师同时录取、教师超额、低分抢走高分的高志愿、统计口径对不上，
# 这些在单函数判据里全都看不见，因为它们每一条都"单独看是对的"。
#
# 所以这里跑一条**固定业务链**：
#   make_student → rank_students → greedy_match → second_round → summary
# 并用两条场景（名额够 / 名额不足）钉住四条业务不变量。

# 业务链需要的角色 → 由契约里的函数名反查模块，不写死文件名
BIZ_REQUIRED = ("make_student", "make_teacher", "make_choice",
                "rank_students", "greedy_match", "second_round", "summary")
BIZ_OPTIONAL = ("free_quota", "unmatched_students", "by_teacher")

BIZ_RUNNER = r'''
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
spec = json.loads(sys.argv[2])
out = {"checks": [], "error": "", "skipped": ""}


def add(name, ok, detail=""):
    out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})


mods = {}
for name, stem in (spec.get("roles") or {}).items():
    if stem in mods:
        continue
    try:
        mods[stem] = importlib.import_module(stem)
    except Exception as exc:
        out["error"] = "import %s 失败：%s: %s" % (stem, type(exc).__name__, str(exc)[:120])
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(0)


def fn(name):
    stem = (spec.get("roles") or {}).get(name)
    m = mods.get(stem)
    if m is None or not hasattr(m, name):
        return None
    return getattr(m, name)


missing = [n for n in spec["required"] if fn(n) is None]
if missing:
    out["skipped"] = "契约里没有这些函数：" + "、".join(missing)
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)

make_student, make_teacher = fn("make_student"), fn("make_teacher")
make_choice = fn("make_choice")
rank_students, greedy = fn("rank_students"), fn("greedy_match")
second_round, summary = fn("second_round"), fn("summary")
free_quota, unmatched_of, by_teacher = fn("free_quota"), fn("unmatched_students"), fn("by_teacher")


def scenario(label, quotas):
    """一条业务链跑完，返回 (students, teachers, choices, pairs, summary)。"""
    teachers = [make_teacher(tid, "T" + tid, q) for tid, q in quotas]
    # 3 个学生：95 / 80 / 70；5 条志愿（s1 填 2 条、s2 填 2 条、s3 填 1 条）
    students = [make_student("s1", "A", 90.0, 5.0),
                make_student("s2", "B", 80.0, 0.0),
                make_student("s3", "C", 70.0, 0.0)]
    choices = [make_choice("s1", "t1", 1), make_choice("s1", "t2", 2),
               make_choice("s2", "t2", 1), make_choice("s2", "t1", 2),
               make_choice("s3", "t1", 1)]
    order = rank_students(students)
    add("%s：rank_students 给出综合分降序" % label,
        list(order) == ["s1", "s2", "s3"], "实际 %s" % (order,))
    greedy_pairs = list(greedy(students, teachers, choices, {}) or [])
    extra = list(second_round(students, teachers, greedy_pairs, choices) or [])
    pairs = greedy_pairs + extra
    stat = summary(students, teachers, pairs)
    return students, teachers, choices, pairs, stat


def invariant_no_double(students, teachers, choices, pairs, stat, label):
    seen, dup = set(), []
    for p in pairs:
        if p.get("sid") in seen:
            dup.append(p.get("sid"))
        seen.add(p.get("sid"))
    add("%s：① 一个学生只能被录取一次" % label, not dup, "重复录取 %s" % dup[:3])


def invariant_quota(students, teachers, choices, pairs, stat, label):
    quota = {t["tid"]: int(t["quota"]) for t in teachers}
    cnt = {}
    for p in pairs:
        cnt[p.get("tid")] = cnt.get(p.get("tid"), 0) + 1
    bad = ["%s 超名额 %d/%d" % (tid, n, quota.get(tid, 0))
           for tid, n in cnt.items() if n > quota.get(tid, 0)]
    add("%s：② 教师不能超名额" % label, not bad, "；".join(bad[:3]))


def invariant_priority(students, teachers, choices, pairs, stat, label):
    """③ 高分优先：**比实际拿到的更靠前**的志愿都没拿到时，那个教师必须已满员、
    且占位者分数不比自己低。

    只比"更靠前"的志愿：学生已经拿到 rank1，那"没拿到 rank2"根本不是违规
    （第一版判据没排除这种情况，正确实现会被误判红——实测踩过）。
    补录（method=assign）或在志愿里找不到实际去处时，按"全部志愿"比较。
    """
    by_sid = {s["sid"]: s for s in students}
    quota = {t["tid"]: int(t["quota"]) for t in teachers}
    assigned, method = {}, {}
    for p in pairs:
        if p.get("sid") not in assigned:
            assigned[p.get("sid")] = p.get("tid")
            method[p.get("sid")] = p.get("method")
    bad = []
    for s in students:
        got = assigned.get(s["sid"])
        got_rank = None
        if got is not None:
            for c in choices:
                if c["sid"] == s["sid"] and c["tid"] == got:
                    got_rank = int(c["rank"])
        for c in choices:
            if c["sid"] != s["sid"]:
                continue
            if got_rank is not None and int(c["rank"]) >= got_rank:
                continue                       # 不比实际拿到的志愿更靠前 → 不适用
            occ = [p for p in pairs if p.get("tid") == c["tid"]]
            if len(occ) < quota.get(c["tid"], 0):
                bad.append("%s 的 rank%s 志愿 %s 还有空位却没录上"
                           % (s["sid"], c["rank"], c["tid"]))
                continue
            best = max([by_sid[x["sid"]]["total"] for x in occ if x["sid"] in by_sid] or [-1])
            if best < s["total"]:
                bad.append("%s(%.2f) 没拿到 rank%s 的 %s，占位者最高只有 %.2f"
                           % (s["sid"], s["total"], c["rank"], c["tid"], best))
    add("%s：③ 综合分高者优先拿到高志愿" % label, not bad, "；".join(bad[:3]))


def invariant_stats(students, teachers, choices, pairs, stat, label):
    """④ 统计自洽：未匹配 + 已配对 == 学生总数，且与实际配对一致。"""
    bad = []
    try:
        n_students = int(stat.get("students"))
        n_pairs = int(stat.get("pairs"))
        n_unmatched = int(stat.get("unmatched"))
    except Exception as exc:
        add("%s：④ 统计字典口径完整" % label, False, "%s: %s" % (type(exc).__name__, exc))
        return
    if n_students != len(students):
        bad.append("students=%d，实际 %d" % (n_students, len(students)))
    if n_pairs != len(pairs):
        bad.append("pairs=%d，实际 %d" % (n_pairs, len(pairs)))
    real_unmatched = len({s["sid"] for s in students} - {p.get("sid") for p in pairs})
    if n_unmatched != real_unmatched:
        bad.append("unmatched=%d，实际 %d" % (n_unmatched, real_unmatched))
    if n_unmatched + n_pairs != n_students:
        bad.append("unmatched+pairs=%d ≠ students=%d" % (n_unmatched + n_pairs, n_students))
    add("%s：④ 未匹配+已配对 == 学生总数" % label, not bad, "；".join(bad[:3]))


def optional_checks(students, teachers, choices, pairs, stat, label):
    quota = {t["tid"]: int(t["quota"]) for t in teachers}
    cnt = {}
    for p in pairs:
        cnt[p.get("tid")] = cnt.get(p.get("tid"), 0) + 1
    if free_quota is not None:
        try:
            fq = free_quota(teachers, pairs)
            bad = ["%s 剩余 %s ≠ %d" % (tid, fq.get(tid), quota[tid] - cnt.get(tid, 0))
                   for tid in quota if int(fq.get(tid, -999)) != quota[tid] - cnt.get(tid, 0)]
            add("%s：剩余名额与已录人数自洽" % label, not bad, "；".join(bad[:3]))
        except Exception as exc:
            add("%s：剩余名额与已录人数自洽" % label, False,
                "%s: %s" % (type(exc).__name__, str(exc)[:120]))
    if unmatched_of is not None:
        try:
            got = list(unmatched_of(students, pairs) or [])
            want = {s["sid"] for s in students} - {p.get("sid") for p in pairs}
            add("%s：未匹配名单与实际一致" % label, set(got) == want, "得到 %s，应为 %s"
                % (got, sorted(want)))
        except Exception as exc:
            add("%s：未匹配名单与实际一致" % label, False,
                "%s: %s" % (type(exc).__name__, str(exc)[:120]))
    if by_teacher is not None:
        try:
            bt = by_teacher(teachers, pairs)
            bad = ["%s=%s，实际 %d" % (tid, bt.get(tid), cnt.get(tid, 0))
                   for tid in quota if int(bt.get(tid, 0)) != cnt.get(tid, 0)]
            add("%s：按教师统计与实际一致" % label, not bad, "；".join(bad[:3]))
        except Exception as exc:
            add("%s：按教师统计与实际一致" % label, False,
                "%s: %s" % (type(exc).__name__, str(exc)[:120]))


try:
    n0 = len(out["checks"])
    # 场景 A：名额够（t1=1, t2=2，3 个学生都能配上，第三个走补录 assign）
    a = scenario("名额够", [("t1", 1), ("t2", 2)])
    invariant_no_double(*a, label="名额够")
    invariant_quota(*a, label="名额够")
    invariant_priority(*a, label="名额够")
    invariant_stats(*a, label="名额够")
    optional_checks(*a, label="名额够")
    # 场景 B：名额不足（t1=1, t2=1 → 必有 1 个学生配不上，走"未匹配"分支）
    b = scenario("名额不足", [("t1", 1), ("t2", 1)])
    invariant_no_double(*b, label="名额不足")
    invariant_quota(*b, label="名额不足")
    invariant_priority(*b, label="名额不足")
    invariant_stats(*b, label="名额不足")
    optional_checks(*b, label="名额不足")
    if len(out["checks"]) - n0 < 8:          # 防"零断言也算绿"
        out["error"] = "业务流断言不足 8 条（跑出来的判据太少，不算通过）"
except Exception as exc:
    out["error"] = "业务流执行异常：%s: %s" % (type(exc).__name__, str(exc)[:200])
print(json.dumps(out, ensure_ascii=False))
'''


def _role_map(contracts: dict) -> dict:
    """角色 → 模块 stem：按契约里的函数名反查，不写死文件名。"""
    want = set(BIZ_REQUIRED) | set(BIZ_OPTIONAL)
    roles: dict = {}
    for rel, c in (contracts or {}).items():
        stem = Path(rel).stem
        for f in getattr(c, "funcs", []) or []:
            name = getattr(f, "name", "")
            if name in want and name not in roles:
                roles[name] = stem
    return roles


def business_flow(ws: Path, contracts: dict, timeout: int = 180) -> dict:
    """跑一条固定业务链并断言业务不变量，返回 {ok, applicable, checks, passed, total, ...}。

    诚实边界：契约里没有这条链所需的函数 → `applicable=False`，
    **不算通过**（不是"跳过就当绿"）；判据跑不动 → ok=False 并写明原因。
    """
    ws = Path(ws).resolve()
    roles = _role_map(contracts)
    missing = [n for n in BIZ_REQUIRED if n not in roles]
    if missing:
        return {"ok": False, "applicable": False, "passed": 0, "total": 0, "checks": [],
                "fails": [], "reason": "契约里没有业务链所需的函数：" + "、".join(missing)}
    tmp = ws / ".sixcoder" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    runner = tmp / "_biz_run.py"
    runner.write_text(BIZ_RUNNER, encoding="utf-8")
    spec = json.dumps({"roles": roles, "required": list(BIZ_REQUIRED)},
                      ensure_ascii=False)
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
