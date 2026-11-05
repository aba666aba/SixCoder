"""业务流 e2e 自检：证明"函数都对、模块也接得上"不等于"业务是对的"。

为什么要这条（44/44 之后剩下的真正缺口）：
    集成冒烟只证明模块能被一起 import、函数能被调用；单函数判据只证明自己符合契约。
    两边全绿时，下面这些业务错误照样能过：
      · 一个学生被两个教师同时录取（一人两录）
      · 教师录取人数超过名额（超名额）
      · 低分学生抢走了高分学生的高志愿（低分抢位）
      · 统计口径对不上：未匹配 + 已配对 ≠ 学生总数（漏统计）
    这些都是 3 个学生 / 2 个教师 / 5 条志愿这种小规模下就能稳定复现的真错误。

判定口径（两面都要）：
    · 正确实现必须绿（含"拿到 rank1 就不该再拿 rank2 说事"这个**误杀陷阱**）；
    · 4 个反例必须红，而且要红在**对应的那条不变量**上，不是"整体红了就算"；
    · 契约里没有这条业务链 → applicable=False，**不算通过**（不许跳过即绿）。

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

REQ = """代码规模：S

1) models.py：
   - make_student(sid: str, name: str, score: float, bonus: float) -> dict：返回学生字典
   - make_teacher(tid: str, name: str, quota: int) -> dict：返回教师字典
   - make_choice(sid: str, tid: str, rank: int) -> dict：返回志愿字典
2) scoring.py：
   - rank_students(students: list[dict]) -> list[str]：按合计分降序
3) matching.py：
   - greedy_match(students: list[dict], teachers: list[dict], choices: list[dict], pref: dict) -> list[dict]：贪心匹配
4) rounds.py：
   - free_quota(teachers: list[dict], pairs: list[dict]) -> dict：剩余名额
   - unmatched_students(students: list[dict], pairs: list[dict]) -> list[str]：未匹配学生
   - second_round(students: list[dict], teachers: list[dict], pairs: list[dict], choices: list[dict]) -> list[dict]：补录
5) stats.py：
   - summary(students: list[dict], teachers: list[dict], pairs: list[dict]) -> dict：统计
   - by_teacher(teachers: list[dict], pairs: list[dict]) -> dict：按教师统计
"""

MODELS = '''
def make_student(sid, name, score, bonus):
    if not str(sid or "").strip():
        raise ValueError("sid")
    s, b = float(score), float(bonus)
    if not (0.0 <= s <= 100.0):
        raise ValueError("score")
    if b < 0:
        raise ValueError("bonus")
    return {"sid": sid, "name": name, "score": s, "bonus": b, "total": round(s + b, 2)}


def make_teacher(tid, name, quota):
    if not str(tid or "").strip():
        raise ValueError("tid")
    q = int(quota)
    if not (1 <= q <= 10):
        raise ValueError("quota")
    return {"tid": tid, "name": name, "quota": q}


def make_choice(sid, tid, rank):
    r = int(rank)
    if not (1 <= r <= 5):
        raise ValueError("rank")
    return {"sid": sid, "tid": tid, "rank": r}
'''

SCORING = '''
def rank_students(students):
    return [s["sid"] for s in sorted(students, key=lambda s: (-float(s["total"]), str(s["sid"])))]
'''

MATCHING = '''
def greedy_match(students, teachers, choices, pref=None):
    quota = {t["tid"]: int(t["quota"]) for t in teachers}
    used = {}
    pairs = []
    for s in sorted(students, key=lambda s: (-float(s["total"]), str(s["sid"]))):
        mine = sorted([c for c in choices if c["sid"] == s["sid"]], key=lambda c: int(c["rank"]))
        for c in mine:
            tid = c["tid"]
            if used.get(tid, 0) < quota.get(tid, 0):
                used[tid] = used.get(tid, 0) + 1
                pairs.append({"sid": s["sid"], "tid": tid, "method": "choice"})
                break
    return pairs
'''

ROUNDS = '''
def free_quota(teachers, pairs):
    cnt = {}
    for p in pairs:
        cnt[p["tid"]] = cnt.get(p["tid"], 0) + 1
    return {t["tid"]: int(t["quota"]) - cnt.get(t["tid"], 0) for t in teachers}


def unmatched_students(students, pairs):
    have = {p["sid"] for p in pairs}
    left = [s for s in students if s["sid"] not in have]
    return [s["sid"] for s in sorted(left, key=lambda s: (-float(s["total"]), str(s["sid"])))]


def second_round(students, teachers, pairs, choices):
    out = []
    cur = list(pairs)
    for sid in unmatched_students(students, cur):
        fq = free_quota(teachers, cur)
        free = [tid for tid, n in fq.items() if n > 0]
        if not free:
            break
        free.sort(key=lambda t: (-fq[t], str(t)))
        out.append({"sid": sid, "tid": free[0], "method": "assign"})
        cur.append(out[-1])
    return out
'''

STATS = '''
def summary(students, teachers, pairs):
    total_quota = sum(int(t["quota"]) for t in teachers)
    have = {p["sid"] for p in pairs}
    return {"students": len(students), "teachers": len(teachers), "pairs": len(pairs),
            "unmatched": len({s["sid"] for s in students} - have),
            "fill_rate": round(100.0 * len(pairs) / total_quota, 1) if total_quota else 0.0}


def by_teacher(teachers, pairs):
    cnt = {t["tid"]: 0 for t in teachers}
    for p in pairs:
        cnt[p["tid"]] = cnt.get(p["tid"], 0) + 1
    return cnt
'''

GOOD = {"models.py": MODELS, "scoring.py": SCORING, "matching.py": MATCHING,
        "rounds.py": ROUNDS, "stats.py": STATS}

# ---- 四个业务反例：每个都只坏一处，用来证明对应不变量真的能拦住
OVER_QUOTA = dict(GOOD, **{"matching.py": MATCHING.replace(
    'if used.get(tid, 0) < quota.get(tid, 0):',
    'if True:            # 反例：无视名额')})

DOUBLE_MATCH = dict(GOOD, **{"matching.py": MATCHING.replace(
    '                pairs.append({"sid": s["sid"], "tid": tid, "method": "choice"})\n'
    '                break',
    '                pairs.append({"sid": s["sid"], "tid": tid, "method": "choice"})'
    '  # 反例：不 break，一个人能录多次')})

LOW_SCORE_FIRST = dict(GOOD, **{"matching.py": MATCHING.replace(
    'for s in sorted(students, key=lambda s: (-float(s["total"]), str(s["sid"]))):',
    'for s in sorted(students, key=lambda s: (float(s["total"]), str(s["sid"]))):'
    '  # 反例：低分先挑')})

BAD_STATS = dict(GOOD, **{"stats.py": STATS.replace(
    '"unmatched": len({s["sid"] for s in students} - have),',
    '"unmatched": 0,  # 反例：漏统计')})


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws(files: dict) -> Path:
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix="bizflow_"))
    for name, src in files.items():
        (ws / name).write_text(src, encoding="utf-8")
    return ws


def _names(res: dict) -> set[str]:
    return {c.get("name") or "" for c in (res.get("checks") or []) if c.get("ok")}


def _failed(res: dict) -> str:
    return " / ".join(str(f) for f in (res.get("fails") or []))


def main() -> int:
    print("业务流 e2e 自检")
    cs = cm.parse_requirement(REQ)
    check("需求能解析出 5 个模块", len(cs) == 5, str(sorted(cs)))

    print("[1] 正确实现必须绿（含两套场景：名额够 / 名额不足）")
    good = flow.business_flow(_ws(GOOD), cs)
    check("正确实现判绿", bool(good.get("ok")), _failed(good))
    check("适用的业务链不会被当成'不适用'", good.get("applicable") is True, str(good)[:160])
    check("断言条数够（≥8，防零断言假绿）", int(good.get("total") or 0) >= 8,
          f"total={good.get('total')}")
    ok_names = _names(good)
    for inv in ("① 一个学生只能被录取一次", "② 教师不能超名额",
                "③ 综合分高者优先拿到高志愿", "④ 未匹配+已配对 == 学生总数"):
        check(f"四条业务不变量都跑到且通过：{inv}",
              any(inv in n for n in ok_names), str(sorted(ok_names))[:200])
    # 误杀回归：正确实现里 s1 拿到的是 rank1 志愿，"没拿到 rank2" 不该被判红。
    # 第一版判据就是把这种情况判红了（真实产物上 14/16）。这里直接钉住那条判据本身是绿的。
    _pri = [c for c in (good.get("checks") or []) if "③" in (c.get("name") or "")]
    check("③ 不变量本身被判绿（第一版误杀的回归）",
          bool(_pri) and all(c.get("ok") for c in _pri), str(_pri)[:200])
    check("报告行说清了通过条数", "业务流 e2e：通过" in flow.business_report_lines(good)[0],
          flow.business_report_lines(good)[0])

    print("[2] 四个反例必须红，且红在对应不变量上")
    cases = [("超名额", OVER_QUOTA, "②"), ("一人两录", DOUBLE_MATCH, "①"),
             ("低分抢位", LOW_SCORE_FIRST, "③"), ("漏统计", BAD_STATS, "④")]
    for label, files, want in cases:
        r = flow.business_flow(_ws(files), cs)
        check(f"{label}：整体判红", not r.get("ok"), "竟然通过了")
        check(f"{label}：被 {want} 抓到", want in _failed(r), _failed(r)[:160])

    print("[3] 契约里没有这条业务链 → 如实'不适用'，不算通过")
    tiny = cm.parse_requirement("代码规模：S\n\n1) calc.py：\n"
                               "   - add(a: int, b: int) -> int：返回两数之和\n")
    r = flow.business_flow(_ws({"calc.py": "def add(a, b):\n    return a + b\n"}), tiny)
    check("applicable=False", r.get("applicable") is False, str(r)[:160])
    check("不因为是'跳过'就算绿", r.get("ok") is False, str(r)[:160])
    check("报告里写清'不适用'且说明不算通过",
          "不适用" in flow.business_report_lines(r)[0]
          and "不算通过" in flow.business_report_lines(r)[0],
          flow.business_report_lines(r)[0])

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（正确实现绿 / 四个业务反例各自红 / 不适用如实说）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
