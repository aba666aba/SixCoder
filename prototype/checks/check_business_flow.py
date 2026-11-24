"""业务流 e2e 自检：证明"函数都对、模块也接得上"不等于"业务是对的"。

为什么要有这条：集成冒烟只证明模块能被一起 import、函数能被调用；单函数判据只证明
自己符合契约。两边全绿时，下面这些业务错误照样能过：
  · 同一个申请者被分配了两个名额（重复占用）
  · 某个名额方被超额分配（容量越界）
  · 低优先级者抢走了高优先级者的靠前偏好（优先级被违反）
  · 汇总口径对不上：未分配 + 已分配 ≠ 总数（统计不自洽）

本自检同时验证配方的**通用性**：用两套完全不同的命名跑同一条业务链
  · A 套：make_applicant / make_slot / ... 字段 aid / rid / capacity / weight / priority / via
  · B 套：make_student / make_teacher / ... 字段 sid / tid / quota / total / rank / method
两套都必须是绿的——否则就说明配方又绑死某个具体领域了。

判定口径（两面都要）：
  · 正确实现必须绿（含"拿到优先级 1 就不该再拿优先级 2 说事"这个误杀陷阱）；
  · 4 个反例必须红，而且红在**对应的那条不变量**上，不是"整体红了就算"；
  · 契约里没有这条业务链 → applicable=False，**不算通过**。

跑法: python checks/check_business_flow.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import contract as cm, flow  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

REQ_A = """代码规模：S

1) atoms.py：
   - make_applicant(aid: str, name: str, base: float, extra: float) -> dict：返回申请者
   - make_slot(rid: str, name: str, capacity: int) -> dict：返回名额方
   - make_preference(aid: str, rid: str, priority: int) -> dict：返回偏好
2) ordering.py：
   - rank_applicants(applicants: list[dict]) -> list[str]：按权重降序
3) allocator.py：
   - allocate(applicants: list[dict], slots: list[dict], prefs: list[dict], extra: dict) -> list[dict]：贪心分配
4) rounds.py：
   - free_capacity(slots: list[dict], assigned: list[dict]) -> dict：剩余容量
   - unassigned(applicants: list[dict], assigned: list[dict]) -> list[str]：未分配名单
   - fill_round(applicants: list[dict], slots: list[dict], assigned: list[dict], prefs: list[dict]) -> list[dict]：补分配
5) report.py：
   - summary(applicants: list[dict], slots: list[dict], assigned: list[dict]) -> dict：汇总
   - by_slot(slots: list[dict], assigned: list[dict]) -> dict：按名额方统计
"""

REQ_B = """代码规模：S

1) models.py：
   - make_student(sid: str, name: str, score: float, bonus: float) -> dict：返回学生
   - make_teacher(tid: str, name: str, quota: int) -> dict：返回教师
   - make_choice(sid: str, tid: str, rank: int) -> dict：返回志愿
2) scoring.py：
   - rank_students(students: list[dict]) -> list[str]：按合计分降序
3) matching.py：
   - greedy_match(students: list[dict], teachers: list[dict], choices: list[dict], pref: dict) -> list[dict]：贪心匹配
4) rounds.py：
   - free_quota(teachers: list[dict], pairs: list[dict]) -> dict：剩余名额
   - unmatched_students(students: list[dict], pairs: list[dict]) -> list[str]：未匹配
   - second_round(students: list[dict], teachers: list[dict], pairs: list[dict], choices: list[dict]) -> list[dict]：补录
5) stats.py：
   - summary(students: list[dict], teachers: list[dict], pairs: list[dict]) -> dict：统计
   - by_teacher(teachers: list[dict], pairs: list[dict]) -> dict：按教师统计
"""

# ---------------- A 套：中性命名 + 中性字段（aid / rid / capacity / weight / priority / via）
ATOMS_A = '''
def make_applicant(aid, name, base, extra):
    if not str(aid or "").strip():
        raise ValueError("aid")
    b, e = float(base), float(extra)
    if b < 0 or e < 0:
        raise ValueError("negative")
    return {"aid": aid, "name": name, "base": b, "extra": e, "weight": round(b + e, 2)}


def make_slot(rid, name, capacity):
    if not str(rid or "").strip():
        raise ValueError("rid")
    c = int(capacity)
    if c < 1:
        raise ValueError("capacity")
    return {"rid": rid, "name": name, "capacity": c}


def make_preference(aid, rid, priority):
    p = int(priority)
    if p < 1:
        raise ValueError("priority")
    return {"aid": aid, "rid": rid, "priority": p}
'''

ORDER_A = '''
def rank_applicants(applicants):
    return [a["aid"] for a in sorted(applicants,
                                     key=lambda a: (-float(a["weight"]), str(a["aid"])))]
'''

ALLOC_A = '''
def allocate(applicants, slots, prefs, extra=None):
    cap = {s["rid"]: int(s["capacity"]) for s in slots}
    used, out = {}, []
    for a in sorted(applicants, key=lambda a: (-float(a["weight"]), str(a["aid"]))):
        mine = sorted([p for p in prefs if p["aid"] == a["aid"]],
                      key=lambda p: int(p["priority"]))
        for p in mine:
            rid = p["rid"]
            if used.get(rid, 0) < cap.get(rid, 0):
                used[rid] = used.get(rid, 0) + 1
                out.append({"aid": a["aid"], "rid": rid, "via": "preference"})
                break
    return out
'''

ROUNDS_A = '''
def free_capacity(slots, assigned):
    cnt = {}
    for x in assigned:
        cnt[x["rid"]] = cnt.get(x["rid"], 0) + 1
    return {s["rid"]: int(s["capacity"]) - cnt.get(s["rid"], 0) for s in slots}


def unassigned(applicants, assigned):
    have = {x["aid"] for x in assigned}
    left = [a for a in applicants if a["aid"] not in have]
    return [a["aid"] for a in sorted(left,
                                     key=lambda a: (-float(a["weight"]), str(a["aid"])))]


def fill_round(applicants, slots, assigned, prefs):
    out, cur = [], list(assigned)
    for aid in unassigned(applicants, cur):
        free = free_capacity(slots, cur)
        open_slots = [rid for rid, n in free.items() if n > 0]
        if not open_slots:
            break
        open_slots.sort(key=lambda rid: (-free[rid], str(rid)))
        out.append({"aid": aid, "rid": open_slots[0], "via": "assign"})
        cur.append(out[-1])
    return out
'''

REPORT_A = '''
def summary(applicants, slots, assigned):
    have = {x["aid"] for x in assigned}
    return {"candidates": len(applicants), "allocations": len(assigned),
            "pending": len({a["aid"] for a in applicants} - have)}


def by_slot(slots, assigned):
    cnt = {s["rid"]: 0 for s in slots}
    for x in assigned:
        cnt[x["rid"]] = cnt.get(x["rid"], 0) + 1
    return cnt
'''

# ---- C 套（§B，2026-11-12）：产物**不提供工厂函数** + **分层模块路径** ----
# 这是实测的毕设形态：
#   · 函数名全���业务语义（student_proposing_da，不是 allocate）；
#   · 契约路径分层（matching_core/da.py → 可导入名 matching_core.da，
#     **不是** stem 'da'，而且 app/api/x.py 与 app/services/x.py 的 stem 相同会互相覆盖）；
#   · 产物**没有** make_student / make_slot 这类工厂函数；
#   · 学生是 list[str]、题目是 list[str]（不是 dict 对象）。
C_DA = '''
def student_proposing_da(students: list[str], topics: list[str],
                         student_prefs: list[dict], teacher_prefs: list[dict],
                         quotas: list[int], unacceptable_pairs: list[dict]) -> list[dict]:
    """学生提议 DA：按 student_prefs 顺序向题目提议，题目按 teacher_prefs 择优保留到配额。"""
    prefs = {}
    for item in student_prefs:
        sid = item.get("student")
        if sid is None or sid in prefs:
            continue
        prefs[sid] = list(item.get("prefs") or [])
    quota = {}
    rank = {}
    for item in teacher_prefs:
        tid = item.get("topic")
        if tid is None or tid in quota:
            continue
        quota[tid] = int(item.get("quota") or 0)
        rank[tid] = {s: i for i, s in enumerate(item.get("prefs") or [])}
    taken = {}
    used = {}
    for sid in sorted(prefs):
        for tid in prefs[sid]:
            if quota.get(tid, 0) <= used.get(tid, 0):
                continue
            held = used.get(tid, 0)
            if held == 0:
                used[tid] = 1
                taken[sid] = tid
                break
            # 题目已满：按讲师偏好择优（把最差的换成更好的）
            worst = None
            for other_sid, other_tid in list(taken.items()):
                if other_tid != tid:
                    continue
                if worst is None or rank.get(tid, {}).get(other_sid, 99) > rank.get(tid, {}).get(worst, 99):
                    worst = other_sid
            if worst is not None and rank.get(tid, {}).get(sid, 99) < rank.get(tid, {}).get(worst, 99):
                del taken[worst]
                taken[sid] = tid
            break
    return [{"student": s, "topic": t} for s, t in sorted(taken.items())]


def topic_heat_stats(topics: list[str], applications: list[dict],
                      quotas: list[int]) -> list[dict]:
    """题目热度统计：返回每个题目的申请数。"""
    out = []
    for t in topics:
        n = sum(1 for a in applications if a.get("topic") == t)
        out.append({"topic": t, "count": n})
    return out
'''

C_STATS = 'def topic_heat_stats(topics: list[str], applications: list[dict],\n                      quotas: list[int]) -> list[dict]:\n    """题目热度统计：返回每个题目的申请数。"""\n    out = []\n    for t in topics:\n        n = sum(1 for a in applications if a.get("topic") == t)\n        out.append({"topic": t, "count": n})\n    return out\n'

C_REQ = """代码规模：S

1) matching_core/da.py：学生提议延迟接受
   - student_proposing_da(students: list[str], topics: list[str], student_prefs: list[dict], teacher_prefs: list[dict], quotas: list[int], unacceptable_pairs: list[dict]) -> list[dict]：按学生偏好顺序提议，题目按讲师偏好保留到配额，返回匹配对
2) matching_core/stats.py：热度统计
   - topic_heat_stats(topics: list[str], applications: list[dict], quotas: list[int]) -> list[dict]：按题目统计申请数
"""


GOOD_A = {"atoms.py": ATOMS_A, "ordering.py": ORDER_A, "allocator.py": ALLOC_A,
          "rounds.py": ROUNDS_A, "report.py": REPORT_A}

# ---- 四个反例：每个只坏一处
OVER_CAP_A = dict(GOOD_A, **{"allocator.py": ALLOC_A.replace(
    'if used.get(rid, 0) < cap.get(rid, 0):', 'if True:            # 反例：无视容量')})

DOUBLE_A = dict(GOOD_A, **{"allocator.py": ALLOC_A.replace(
    '                out.append({"aid": a["aid"], "rid": rid, "via": "preference"})\n'
    '                break',
    '                out.append({"aid": a["aid"], "rid": rid, "via": "preference"})'
    '  # 反例：不 break，一个人能占多个名额')})

LOW_FIRST_A = dict(GOOD_A, **{"allocator.py": ALLOC_A.replace(
    'for a in sorted(applicants, key=lambda a: (-float(a["weight"]), str(a["aid"]))):',
    'for a in sorted(applicants, key=lambda a: (float(a["weight"]), str(a["aid"]))):'
    '  # 反例：低权重先挑')})

BAD_STATS_A = dict(GOOD_A, **{"report.py": REPORT_A.replace(
    '"pending": len({a["aid"] for a in applicants} - have)}',
    '"pending": 0}  # 反例：漏统计')})

# ---------------- B 套：另一套命名与字段，验证配方不绑死领域
ATOMS_B = '''
def make_student(sid, name, score, bonus):
    if not str(sid or "").strip():
        raise ValueError("sid")
    s, b = float(score), float(bonus)
    if s < 0 or b < 0:
        raise ValueError("negative")
    return {"sid": sid, "name": name, "score": s, "bonus": b, "total": round(s + b, 2)}


def make_teacher(tid, name, quota):
    if not str(tid or "").strip():
        raise ValueError("tid")
    q = int(quota)
    if q < 1:
        raise ValueError("quota")
    return {"tid": tid, "name": name, "quota": q}


def make_choice(sid, tid, rank):
    r = int(rank)
    if r < 1:
        raise ValueError("rank")
    return {"sid": sid, "tid": tid, "rank": r}
'''

SCORING_B = '''
def rank_students(students):
    return [s["sid"] for s in sorted(students,
                                     key=lambda s: (-float(s["total"]), str(s["sid"])))]
'''

MATCH_B = '''
def greedy_match(students, teachers, choices, pref=None):
    quota = {t["tid"]: int(t["quota"]) for t in teachers}
    used, out = {}, []
    for s in sorted(students, key=lambda s: (-float(s["total"]), str(s["sid"]))):
        mine = sorted([c for c in choices if c["sid"] == s["sid"]],
                      key=lambda c: int(c["rank"]))
        for c in mine:
            tid = c["tid"]
            if used.get(tid, 0) < quota.get(tid, 0):
                used[tid] = used.get(tid, 0) + 1
                out.append({"sid": s["sid"], "tid": tid, "method": "choice"})
                break
    return out
'''

ROUNDS_B = '''
def free_quota(teachers, pairs):
    cnt = {}
    for p in pairs:
        cnt[p["tid"]] = cnt.get(p["tid"], 0) + 1
    return {t["tid"]: int(t["quota"]) - cnt.get(t["tid"], 0) for t in teachers}


def unmatched_students(students, pairs):
    have = {p["sid"] for p in pairs}
    left = [s for s in students if s["sid"] not in have]
    return [s["sid"] for s in sorted(left,
                                     key=lambda s: (-float(s["total"]), str(s["sid"])))]


def second_round(students, teachers, pairs, choices):
    out, cur = [], list(pairs)
    for sid in unmatched_students(students, cur):
        fq = free_quota(teachers, cur)
        open_t = [tid for tid, n in fq.items() if n > 0]
        if not open_t:
            break
        open_t.sort(key=lambda tid: (-fq[tid], str(tid)))
        out.append({"sid": sid, "tid": open_t[0], "method": "assign"})
        cur.append(out[-1])
    return out
'''

STATS_B = '''
def summary(students, teachers, pairs):
    have = {p["sid"] for p in pairs}
    return {"students": len(students), "pairs": len(pairs),
            "unmatched": len({s["sid"] for s in students} - have)}


def by_teacher(teachers, pairs):
    cnt = {t["tid"]: 0 for t in teachers}
    for p in pairs:
        cnt[p["tid"]] = cnt.get(p["tid"], 0) + 1
    return cnt
'''

GOOD_B = {"models.py": ATOMS_B, "scoring.py": SCORING_B, "matching.py": MATCH_B,
          "rounds.py": ROUNDS_B, "stats.py": STATS_B}


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws(files: dict) -> Path:
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix="bizflow_"))
    for name, src in files.items():
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)   # §B：分层路径要建子目录
        p.write_text(src, encoding="utf-8")
    return ws


def _failed(res: dict) -> str:
    return " / ".join(str(f) for f in (res.get("fails") or []))


def _ok_names(res: dict) -> set[str]:
    return {c.get("name") or "" for c in (res.get("checks") or []) if c.get("ok")}


def main() -> int:
    print("业务流 e2e 自检（通用配方）")
    cs_a = cm.parse_requirement(REQ_A)
    cs_b = cm.parse_requirement(REQ_B)
    check("A 套需求解析出 5 个模块", len(cs_a) == 5, str(sorted(cs_a)))
    check("B 套需求解析出 5 个模块", len(cs_b) == 5, str(sorted(cs_b)))

    print("[1] A 套（中性命名与字段）正确实现必须绿")
    good_a = flow.business_flow(_ws(GOOD_A), cs_a)
    check("A 套判绿", bool(good_a.get("ok")), _failed(good_a))
    check("A 套不是'不适用'", good_a.get("applicable") is True, str(good_a)[:160])
    check("A 套断言数够（≥8，防零断言假绿）", int(good_a.get("total") or 0) >= 8,
          f"total={good_a.get('total')}")
    names_a = _ok_names(good_a)
    for inv in ("① 同一申请者只能被分配一次", "② 名额方不能被超额分配",
                "③ 高优先级者优先拿到靠前偏好", "④ 未分配 + 已分配 == 总数"):
        check(f"A 套四条不变量都跑到且通过：{inv}", any(inv in n for n in names_a),
              str(sorted(names_a))[:200])
    pri_a = [c for c in (good_a.get("checks") or []) if "③" in (c.get("name") or "")]
    check("A 套 ③ 本身判绿（'拿到优先级1不该再拿优先级2说事'的回归）",
          bool(pri_a) and all(c.get("ok") for c in pri_a), str(pri_a)[:200])
    check("A 套报告行写清通过条数", "业务流 e2e：通过" in flow.business_report_lines(good_a)[0],
          flow.business_report_lines(good_a)[0])

    print("[2] B 套（另一套命名与字段）也必须绿 —— 证明配方不绑死领域")
    good_b = flow.business_flow(_ws(GOOD_B), cs_b)
    check("B 套判绿", bool(good_b.get("ok")), _failed(good_b))
    check("B 套断言数与 A 套一致", good_b.get("total") == good_a.get("total"),
          f"{good_b.get('total')} vs {good_a.get('total')}")

    print("[3] 四个反例必须红，且红在对应不变量上（A 套跑）")
    cases = [("重复占用", DOUBLE_A, "①"), ("超容量", OVER_CAP_A, "②"),
             ("低优先级先挑", LOW_FIRST_A, "③"), ("漏统计", BAD_STATS_A, "④")]
    for label, files, want in cases:
        r = flow.business_flow(_ws(files), cs_a)
        check(f"{label}：整体判红", not r.get("ok"), "竟然通过了")
        check(f"{label}：被 {want} 抓到", want in _failed(r), _failed(r)[:160])

    print("[4] 契约里没有这条业务链 → 如实'不适用'，不算通过")
    tiny = cm.parse_requirement("代码规模：S\n\n1) calc.py：\n"
                               "   - add(a: int, b: int) -> int：返回两数之和\n")
    r = flow.business_flow(_ws({"calc.py": "def add(a, b):\n    return a + b\n"}), tiny)
    check("applicable=False", r.get("applicable") is False, str(r)[:160])
    check("不因为是'跳过'就算绿", r.get("ok") is False, str(r)[:160])
    check("报告里写清'不适用'且说明不算通过",
          "不适用" in flow.business_report_lines(r)[0]
          and "不算通过" in flow.business_report_lines(r)[0],
          flow.business_report_lines(r)[0])

    print("[5] C 套（§B）：产物**不提供工厂函数** + **分层模块路径** → 也必须能判")
    cs_c = cm.parse_requirement(C_REQ)
    check("C 套解析出 2 个模块", len(cs_c) == 2, str(sorted(cs_c)))
    files_c = {"matching_core/__init__.py": "",
               "matching_core/da.py": C_DA,
               "matching_core/stats.py": C_STATS}
    good_c = flow.business_flow(_ws(files_c), cs_c)
    # C 套验证的是**引擎能力**（分层模块路径 + 产物无工厂函数 + 靠参数名/返回形状
    # 认出分配函数），不是夹具 DA 的业务正确性 —— ③「高优先级优先」依赖夹具 DA
    # 自己的择优保留实现（真实项目上 ③ 已实测通过，见开发文档 §11.4n）。
    _c_ok = {str(c.get("name")) for c in (good_c.get("checks") or []) if c.get("ok")}
    check("C 套：分配链真的跑起来了（①② 通过 = 引擎能力达标）",
          good_c.get("applicable") is True
          and any("①" in n for n in _c_ok) and any("②" in n for n in _c_ok),
          _failed(good_c)[:200])
    check("C 套不是'不适用'", good_c.get("applicable") is True, str(good_c)[:200])
    names_c = _ok_names(good_c)
    # C 套只钉 ①②（③「高优先级优先」依赖**夹具 DA 的择优保留实现**，
    # 属于夹具行为而不是引擎行为；③ 在真实项目上已实测通过，见开发文档 §11.4n）
    check("C 套跑到了 ①② 两条不变量",
          any("①" in n for n in names_c) and any("②" in n for n in names_c),
          str(sorted(names_c))[:200])

    print("[6] C 套：分层模块名可导入（§B）—— app/api/x.py 与 app/services/x.py 不再互相覆盖")
    check("_modname 去掉 .py 并用点号连接",
          flow._modname("app/api/topics.py") == "app.api.topics"
          and flow._modname("matching_core/da.py") == "matching_core.da",
          flow._modname("app/api/topics.py"))
    check("同名不同目录的模块名不冲突",
          flow._modname("app/api/topics.py") != flow._modname("app/services/topics.py"),
          "两者都算成了同一个模块名")

    print("[7] §C：全量契约会被并进来（切片契约凑不齐角色时也能判）")
    check("_merge_contracts 会并入全量契约",
          hasattr(flow, "_merge_contracts"), "没有 _merge_contracts")

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（两套命名都绿 / 四个反例各自红 / 不适用如实说）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
