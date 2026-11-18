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
# spec["modules"]: {点分导入名: 展示短名}
# 导入名必须来自**契约里的路径**（app/api/batches.py → app.api.batches），
# 不能用短名（batches）→ 必然 ModuleNotFoundError（实测 2026-11-09）。
for dotted, label in spec["modules"].items():
    try:
        mods[label] = importlib.import_module(dotted)
        out["imports"][label] = "ok"
    except Exception as exc:
        out["imports"][label] = "%s: %s" % (type(exc).__name__, str(exc)[:120])
SAMPLE = {"int": 0, "float": 0.0, "str": "", "bool": False, "dict": {},
          "list[dict]": [], "list[str]": [], "list[int]": [], "list[float]": [], "object": None}
for rel, label, dotted, fname, params in spec["calls"]:
    m = mods.get(label)
    if m is None or not hasattr(m, fname):
        out["calls"]["%s.%s" % (label, fname)] = "missing"
        continue
    args = [SAMPLE.get((p or "object").replace(" ", "").lower(), None) for p in params]
    try:
        getattr(m, fname)(*args)
        out["calls"]["%s.%s" % (label, fname)] = "ok"
    except ValueError as exc:
        out["calls"]["%s.%s" % (label, fname)] = "value_error(合法拒绝)"
    except Exception as exc:
        out["calls"]["%s.%s" % (label, fname)] = "%s: %s" % (type(exc).__name__, str(exc)[:90])
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
    def _dotted(rel: str) -> str:
        """契约里的文件路径 → 可导入的点分名（app/api/batches.py → app.api.batches）。"""
        d = str(rel or "").replace("\\", "/")
        if d.endswith(".py"):
            d = d[:-3]
        return d.replace("/", ".").lstrip(".")

    mods = {}
    for rel, info in (m.get("modules") or {}).items():
        label = info.get("module") or Path(rel).stem
        mods[_dotted(rel)] = label
    calls = []
    for rel, info in (m.get("modules") or {}).items():
        label = info.get("module") or Path(rel).stem
        for f in (info.get("funcs") or [])[:8]:
            if f.get("owner"):                            # 类方法不能在模块级别调用
                continue
            calls.append([rel, label, _dotted(rel), f["name"],
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
        # 不删 runner：下一个回合会原地覆盖。每次调用都删一个文件，
        # 长跑累计会撞上外层"单轮删除"保护（实测 2026-11-07 直接打断夜班）。
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
    "applicant_id": ("sid", "applicant_id", "aid", "cid", "uid", "id",
                     "student", "student_id", "user", "username"),
    "slot_id": ("tid", "slot_id", "rid", "pid", "wid", "id",
                "topic", "topic_id", "lecturer", "lecturer_id", "teacher", "teacher_id"),
    "capacity": ("quota", "capacity", "limit", "slots", "max_slots", "seats"),
    "score": ("total", "score", "weight", "rank_score", "priority"),
    "pref_rank": ("rank", "priority", "pref", "order", "pref_rank"),
    "method": ("method", "via", "how", "source"),
}

SUMMARY_ALIASES = {
    "total": ("students", "applicants", "candidates", "total", "entities",
              "participating", "students_count"),
    "assigned": ("pairs", "assigned", "matches", "allocations", "allocated",
                 "matched", "matching"),
    "unassigned": ("unmatched", "unassigned", "left", "pending", "remaining",
                   "unallocated"),
}

# ---------------------------------------------------------------------------
# §B（2026-11-12）：角色识别的**签名口径**
#
# 为什么加这个：角色识别原来只靠**函数名字面**同义词表 →
#   产物函数叫 `make_student` / `allocate` / `summary` 才认得出。
#   实测（毕设项目）：35 个契约函数**一个都没命中** → `applicable=False`
#   → 业务流永远"不算通过"，而项目的业务链其实是有的（匹配分配链）。
# 现在改成**两级**：
#   ① 函数名命中 → 仍是最优先（老项目行为**逐字不变**）；
#   ② 契约签名粗筛 → 候选（参数量 + 返回类型）；
#   真正的裁决交给 runner：**试调用 + 结果形状验证**（见 BIZ_RUNNER）。
# ---------------------------------------------------------------------------

# 角色 → 契约里的**参数名**该含哪些语义词（§B 的静态候选过滤）。
# 为什么用参数名而不是参数个数/返回类型：产物的参数个数与 runner 的调用约定
# 常常不同（实测毕设的 DA 是 6 参，约定是 4 参）→ 按个数筛会全筛掉。
# 参数名是**产物自己写的意图**（students/topics/prefs/quotas…）→ 更可靠。
# 这层只是**静态过滤，不调用任何函数** → 没有副作用。
ROLE_PARAM_WORDS = {
    # 分配链：吃"申请者 + 槽位 + 偏好"，吐分配对
    "allocate": ("student", "applicant", "candidate", "topic", "slot",
                 "lecturer", "teacher", "pref", "choice", "quota"),
    "fill": ("student", "applicant", "candidate", "topic", "slot",
             "pref", "choice", "quota", "unfilled", "remaining"),
    "rank": ("student", "applicant", "candidate", "score", "weight", "topic"),
    "summary": ("student", "applicant", "candidate", "topic", "slot",
                "matching", "assign", "pair", "result"),
    # 三个"造对象"角色：自适应路径下 runner 自己造，产物有就更好
    "make_applicant": ("student", "applicant", "candidate", "score", "weight"),
    "make_slot": ("topic", "slot", "lecturer", "teacher", "quota", "capacity"),
    "make_preference": ("pref", "choice", "rank", "student", "topic", "slot"),
}

# 角色 → (参数量, 返回类型)：runner 的调用约定是硬约束（见 BIZ_RUNNER 的调用处）
ROLE_SIG = {
    "make_applicant": (4, "dict"),
    "make_slot": (3, "dict"),
    "make_preference": (3, "dict"),
    "rank": (1, "list"),
    "allocate": (4, "list"),
    "fill": (4, "list"),
    "summary": (3, "dict"),
}

# 角色 → 契约里的返回类型别名（"list[dict]" 也算 "list"）
def _ret_kind(ret: str) -> str:
    r = str(ret or "").replace(" ", "").lower()
    if r.startswith("list"):
        return "list"
    if r.startswith("dict"):
        return "dict"
    if r.startswith("tuple"):
        return "tuple"
    return r


BIZ_RUNNER = r'''
import importlib, inspect, json, sys
sys.path.insert(0, sys.argv[1])
spec = json.loads(sys.argv[2])
out = {"checks": [], "error": "", "skipped": ""}


def add(name, ok, detail=""):
    out["checks"].append({"name": name, "ok": bool(ok), "detail": str(detail)[:300]})


mods = {}
for role, pairs in (spec.get("roles") or {}).items():
    for pair in (pairs if isinstance(pairs, list) else [pairs]):
        stem = pair[0]
        if stem in mods:
            continue
        # §B：模块名可能是点号形式（app.topics）；失败再退回末段名（老口径）
        _names = [stem]
        if "." in stem:
            _names.append(stem.rsplit(".", 1)[-1])
        _err = ""
        for _n in _names:
            try:
                mods[stem] = importlib.import_module(_n)
                break
            except Exception as exc:
                # 2026-11-12 修复：**记下第一个（点号名）失败的真实原因**。
                # 原来只报"最后一个候选名"的错误 → 点号名失败的真实原因被掩盖
                # （实测产物仓缺 fastapi，报出来的是末段名 `da` 的
                # "No module named 'da'" → 让人以为是路径问题，
                # 真实原因"da.py 第 1 行 import fastapi 失败"被丢掉）。
                if not _err:
                    _err = "%s: %s" % (type(exc).__name__, str(exc)[:200])
                if _n == _names[-1]:
                    out["error"] = ("import %s 失败：%s"
                                    % (stem, _err or "未知错误"))
                    print(json.dumps(out, ensure_ascii=False))
                    raise SystemExit(0)


def candidates(role):
    """该角色的候选函数（按优先级）。"""
    raw = (spec.get("roles") or {}).get(role) or []
    if not isinstance(raw, list):
        raw = [raw]
    out_ = []
    for item in raw:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            out_.append((item[0], item[1]))
        elif isinstance(item, str) and "::" in item:
            stem, name = item.split("::", 1)
            out_.append((stem, name))
    return out_


def fn(role):
    """取角色的函数：候选里第一个**存在**的（裁决在调用处做）。"""
    for stem, name in candidates(role):
        m = mods.get(stem)
        if m is None or not hasattr(m, name):
            continue
        return getattr(m, name)
    return None


def pick(obj, cands):
    """从产物对象里解析出语义字段的真实键名（解析不出返回空串）。"""
    if not isinstance(obj, dict):
        return ""
    for k in cands:
        if k in obj:
            return k
    return ""


# ---- §B：按**参数名 + 类型注解**造数据（产物不提供工厂函数时也能调）----
def _ann(p):
    a = getattr(p, "annotation", None)
    if a is None or a is inspect.Parameter.empty:
        return ""
    return str(a).replace(" ", "").lower()


def _kind(t):
    if t.startswith("list[str]"):
        return "strlist"
    if t.startswith("list[dict]"):
        return "dictlist"
    if t.startswith("list[int]"):
        return "intlist"
    if t.startswith("list"):
        return "list"
    if t.startswith("dict"):
        return "dict"
    if t.startswith("int"):
        return "int"
    if t.startswith("float"):
        return "float"
    if t.startswith("bool"):
        return "bool"
    if t.startswith("str"):
        return "str"
    return ""


# 探针池：每个语义槽给**几种形态**，按参数名 + 类型注解挑一个
def _pool(kind, sem, ctx=None):
    """sem ∈ applicant/slot/pref/teacher_pref/quota/match/empty

    **kind 优先**（按类型注解给值）—— 传错类型会让产物函数内部抛错，
    候选就被误判成"不是这个角色"（实测：给 int 参数塞 list → 函数崩）。

    **ctx 优先**（调用方按场景给的真实数据，如"每个槽位的实际容量"）——
    实测：名额列表必须按槽位容量给，给通用值会让产物超配。
    """
    if ctx:
        # **按 kind 匹配**（ctx 覆盖不能无视类型）：产物的 `students: list[str]`
        # 要的是**id 列表**，不是对象列表（给错类型 → 产物内部解析错 →
        # 匹配不出结果 → 断言误判"没分到"）。
        if sem == "applicant":
            if kind == "strlist" and ctx.get("applicant_ids") is not None:
                return ctx["applicant_ids"]
            if kind in ("dictlist", "dict", "") and ctx.get("applicant") is not None:
                return ctx["applicant"]
        if sem == "slot":
            if kind == "strlist" and ctx.get("slot_ids") is not None:
                return ctx["slot_ids"]
            if kind in ("dictlist", "dict", "") and ctx.get("slot") is not None:
                return ctx["slot"]
        if sem == "pref" and kind in ("dictlist", "dict", "") \
                and ctx.get("pref") is not None:
            return ctx["pref"]
        if sem == "teacher_pref" and kind in ("dictlist", "dict", "") \
                and ctx.get("teacher_pref") is not None:
            return ctx["teacher_pref"]
        if sem == "quota" and ctx.get("quota") is not None:
            return ctx["quota"]
    stu = ["s1", "s2", "s3"]
    top = ["t1", "t2"]
    # ---- 先按类型分派 ----
    if kind == "int":
        return 1
    if kind == "float":
        return 80.0
    if kind == "bool":
        return True
    if kind == "str":
        return "s1"
    if kind == "intlist":
        return [1, 1, 1]
    if kind == "list":
        return []
    if kind == "dict":
        if sem == "applicant":
            return {"aid": "a1", "sid": "a1", "id": "a1", "name": "A1",
                    "student": "a1", "student_id": "a1",
                    "score": 90.0, "weight": 90.0, "total": 90.0}
        if sem == "slot":
            return {"rid": "s1", "tid": "s1", "id": "s1", "name": "S1",
                    "topic": "s1", "topic_id": "s1",
                    "capacity": 1, "quota": 1, "limit": 1}
        if sem == "pref":
            return {"aid": "a1", "sid": "a1", "student": "a1", "student_id": "a1",
                    "rid": "s1", "tid": "s1", "slot_id": "s1", "topic": "s1",
                    "topic_id": "s1", "rank": 1, "priority": 1, "pref_rank": 1,
                    "prefs": ["s1", "s2"], "topics": ["s1", "s2"],
                    "choices": ["s1", "s2"]}
        if sem == "teacher_pref":
            return {"rid": "s1", "tid": "s1", "topic": "s1", "topic_id": "s1",
                    "slot_id": "s1", "capacity": 1, "quota": 1, "limit": 1,
                    "prefs": ["a1", "a2"], "students": ["a1", "a2"],
                    "choices": ["a1", "a2"]}
        if sem == "quota":
            return 1
        if sem == "match":
            return {"aid": "a1", "sid": "s1", "student": "s1",
                    "rid": "q1", "tid": "t1", "slot_id": "q1", "topic": "t1",
                    "topic_id": "t1"}
        return {}
    if kind == "strlist":
        if sem == "applicant":
            return ["a1", "a2", "a3"]
        if sem in ("slot", "teacher_pref"):
            return ["s1", "s2"]
        if sem == "pref":
            return ["s1", "s2"]
        return ["a1", "a2", "a3"]
    if kind == "dictlist":
        if sem == "applicant":
            return [_pool("dict", "applicant"), _pool("dict", "applicant")]
        if sem == "slot":
            return [_pool("dict", "slot"), _pool("dict", "slot")]
        if sem == "pref":
            return [_pool("dict", "pref"), _pool("dict", "pref")]
        if sem == "teacher_pref":
            return [_pool("dict", "teacher_pref"), _pool("dict", "teacher_pref")]
        if sem == "quota":
            return [1, 1, 1]
        if sem == "match":
            return [_pool("dict", "match")]
        return []
    # ---- 没有类型注解 → 按语义猜 ----
    if sem == "applicant":
        return [{"aid": "a1", "sid": "a1", "id": "a1", "name": "A1",
                 "student": "a1", "student_id": "a1",
                 "score": 90.0, "weight": 90.0, "total": 90.0},
                {"aid": "a2", "sid": "a2", "id": "a2", "name": "A2",
                 "student": "a2", "student_id": "a2",
                 "score": 80.0, "weight": 80.0, "total": 80.0},
                {"aid": "a3", "sid": "a3", "id": "a3", "name": "A3",
                 "student": "a3", "student_id": "a3",
                 "score": 70.0, "weight": 70.0, "total": 70.0}]
    if sem == "slot":
        return [{"rid": "s1", "tid": "s1", "id": "s1", "name": "S1",
                 "topic": "s1", "topic_id": "s1",
                 "capacity": 1, "quota": 1, "limit": 1},
                {"rid": "s2", "tid": "s2", "id": "s2", "name": "S2",
                 "topic": "s2", "topic_id": "s2",
                 "capacity": 1, "quota": 1, "limit": 1}]
    if sem == "pref":
        return [{"aid": "a1", "sid": "s1", "student": "s1", "student_id": "s1",
                 "rid": "q1", "tid": "t1", "slot_id": "q1", "topic": "t1",
                 "topic_id": "t1", "rank": 1, "priority": 1, "pref_rank": 1,
                 "prefs": ["t1", "t2"], "topics": ["t1", "t2"],
                 "choices": ["t1", "t2"]},
                {"aid": "a1", "sid": "s1", "student": "s1", "student_id": "s1",
                 "rid": "q2", "tid": "t2", "slot_id": "q2", "topic": "t2",
                 "topic_id": "t2", "rank": 2, "priority": 2, "pref_rank": 2,
                 "prefs": ["t2"], "topics": ["t2"], "choices": ["t2"]},
                {"aid": "a2", "sid": "s2", "student": "s2", "student_id": "s2",
                 "rid": "q2", "tid": "t2", "slot_id": "q2", "topic": "t2",
                 "topic_id": "t2", "rank": 1, "priority": 1, "pref_rank": 1,
                 "prefs": ["t2", "t1"], "topics": ["t2", "t1"],
                 "choices": ["t2", "t1"]}]
    if sem == "teacher_pref":
        return [{"rid": "q1", "tid": "t1", "topic": "t1", "topic_id": "t1",
                 "slot_id": "q1", "capacity": 1, "quota": 1, "limit": 1,
                 "prefs": ["s1", "s2"], "students": ["s1", "s2"],
                 "choices": ["s1", "s2"]},
                {"rid": "q2", "tid": "t2", "topic": "t2", "topic_id": "t2",
                 "slot_id": "q2", "capacity": 1, "quota": 1, "limit": 1,
                 "prefs": ["s2", "s1"], "students": ["s2", "s1"],
                 "choices": ["s2", "s1"]}]
    if sem == "quota":
        return [1, 1, 1]
    if sem == "match":
        return [{"aid": "a1", "sid": "s1", "student": "s1",
                 "rid": "q1", "tid": "t1", "slot_id": "q1", "topic": "t1",
                 "topic_id": "t1"}]
    return []


def _sem_of(pname, t):
    """按参数名 + 类型注解判语义槽。"""
    n = str(pname or "").lower()
    if "unacceptable" in n or "block" in n or "reject" in n:
        return "empty"
    if "quota" in n or "capacity" in n or "limit" in n or "seat" in n:
        return "quota"
    if "teacher" in n or "lecturer" in n or "supervisor" in n:
        if "pref" in n or "choice" in n or "rank" in n or "prefer" in n:
            return "teacher_pref"
        return "slot"
    if "student" in n or "applicant" in n or "candidate" in n or "user" in n:
        if "pref" in n or "choice" in n or "wish" in n or "rank" in n or "prefer" in n:
            return "pref"
        return "applicant"
    if "topic" in n or "slot" in n or "position" in n or "resource" in n or "project" in n:
        if "pref" in n or "choice" in n or "rank" in n or "prefer" in n:
            return "teacher_pref"
        return "slot"
    if "pref" in n or "choice" in n or "bid" in n or "pick" in n or "wish" in n:
        return "pref"
    if "match" in n or "assign" in n or "result" in n or "pair" in n:
        return "match"
    if "student" in n or "applicant" in n or "candidate" in n:
        return "applicant"
    return "empty"


def call_adaptive(f, sem, ctx=None):
    """按被调函数的**真实签名**组装参数并调用（产物不提供工厂函数时的路径）。"""
    try:
        sig = inspect.signature(f)
    except Exception:
        return None, False
    args = []
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        k = _kind(_ann(p))
        s = _sem_of(p.name, _ann(p))
        if p.default is not inspect.Parameter.empty:
            args.append(p.default)
            continue
        args.append(_pool(k, s, ctx))
    try:
        return f(*args), True
    except Exception:
        return None, False


def call_role(role, sem, validator=None, ctx=None, exclude=None):
    """取角色的函数并**试调用**；第一个通过验证的候选采用（§B 的裁决处）。

    `exclude`：**别的角色已经选中的函数**（同一函数不能兼任两个角色 ——
    实测 fill 选中了 allocate 那个 → 分配算了两遍 → "重复分配""超容量"假红）。
    """
    used = set(exclude or ())
    for stem, name in candidates(role):
        if name in used or (stem, name) in used:
            continue
        m = mods.get(stem)
        if m is None or not hasattr(m, name):
            continue
        f = getattr(m, name)
        r, ok = call_adaptive(f, sem, ctx)
        if not ok:
            continue
        if validator is None or validator(r):
            return f, r
    return None, None



# ---- 先造一份探针对象，用它们反查字段别名（真正干活用的对象随后再造）
# §B（2026-11-12）：**产物不一定提供工厂函数** —— 很多项目的"造对象"是内联的，
# 契约里根本没有 make_student 这类函数。这时改走**自适应路径**：
# 由 runner 自己按参数名/类型造数据（见 call_adaptive），并用**分配结果**
# 反查字段别名（分配对里的键名才是判据真正关心的）。
make_applicant, make_slot = fn("make_applicant"), fn("make_slot")
make_pref = fn("make_preference")
rank_fn, allocate_fn = fn("rank"), fn("allocate")
summary_fn, fill_fn = fn("summary"), fn("fill")
free_fn, unassigned_fn, by_slot_fn = fn("free_slot"), fn("unassigned_of"), fn("by_slot")

FA = spec["fields"]
probe_a = probe_s = probe_p = None
_adaptive = False

if make_applicant and make_slot and make_pref:
    try:
        probe_a = make_applicant("p1", "probe", 80.0, 0.0)
        probe_s = make_slot("q1", "probe", 1)
        probe_p = make_pref("p1", "q1", 1)
    except Exception:
        # §B：候选是**参数名过滤**来的，可能不是真正的工厂函数（实测：
        # submit_preferences 被当成 make_applicant，调不通）→
        # **调不通就降级到自适应路径**，不要在这里报错退出。
        _adaptive = True
        probe_a = _pool("dict", "applicant")
        probe_s = _pool("dict", "slot")
        probe_p = _pool("dict", "pref")
else:
    # 自适应路径：产物没提供工厂函数 → runner 自己造探针
    _adaptive = True
    probe_a = _pool("dict", "applicant")
    probe_s = _pool("dict", "slot")
    probe_p = _pool("dict", "pref")

# §B：自适应路径下，三个"造对象"角色由 **runner 自己**提供 → 不算缺；
# 必需要求降为"分配链"（allocate / summary）。老路径要求全部（行为不变）。
_needed = list(spec["required"])
if _adaptive:
    _needed = [r for r in _needed
               if r not in ("make_applicant", "make_slot", "make_preference")]
_missing = [r for r in _needed if fn(r) is None]
if _missing:
    out["skipped"] = "契约里找不到这些角色对应的函数：" + "、".join(_missing)
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)

# §B：自适应路径下，**分配函数要靠"试调用 + 返回形状"裁决** ——
# 候选[0] 只是"参数名像"，不一定是真正的分配器（实测候选[0] 是
# reserve_topic_quota，它返回 dict 而不是分配对列表）。
if _adaptive:
    def _is_pairs(r):
        """分配对：list，元素同时有申请人键和名额方键。"""
        return (isinstance(r, list) and r and isinstance(r[0], dict)
                and pick(r[0], FA["applicant_id"]) and pick(r[0], FA["slot_id"]))

    def _is_ranked(r):
        """排序结果：list，元素是**申请者对象**（有申请人键 + 分数键）。"""
        return (isinstance(r, list) and r and isinstance(r[0], dict)
                and pick(r[0], FA["applicant_id"]) and pick(r[0], FA["score"]))

    def _is_stat(r):
        """汇总：dict，且至少含 total/assigned/unassigned 之一。"""
        if not isinstance(r, dict):
            return False
        return any(pick(r, SA) for SA in (spec["summary_fields"]["total"],
                                          spec["summary_fields"]["assigned"],
                                          spec["summary_fields"]["unassigned"]))

    allocate_fn, _probe_res = call_role("allocate", "match", _is_pairs)
    if allocate_fn is None:
        out["skipped"] = ("契约里找不到能返回分配对的分配函数"
                          "（候选里没有 list[dict] 且含申请人/名额方两个键的函数）")
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(0)
    # §B：**自适应路径下不启用 fill**（补位）。
    # 为什么：补位函数返回的是"**增量**"（只报补上的那几条），
    # 而候选里往往只有另一个"全量分配"函数 —— 拿它当补位 → 分配算两遍 →
    # "重复分配""超容量"假红（实测）。宁可少一条断言，也不断言错。
    fill_fn = None
    rank_fn, _ = call_role("rank", "applicant", _is_ranked)
    summary_fn, _ = call_role("summary", "match", _is_stat)
    # §B：**分配结果里的键名才是判据真正关心的**（产物可能用 student/topic
    # 而不是 aid/rid）→ 用它校正 K_PA/K_PS，后面自造对象时也用这套键名。
    if isinstance(_probe_res, list) and _probe_res and isinstance(_probe_res[0], dict):
        K_PA = pick(_probe_res[0], FA["applicant_id"]) or K_PA
        K_PS = pick(_probe_res[0], FA["slot_id"]) or K_PS

K_AID = pick(probe_a, FA["applicant_id"])
K_SCORE = pick(probe_a, FA["score"])
K_SID = pick(probe_s, FA["slot_id"])
K_CAP = pick(probe_s, FA["capacity"])
K_PA = pick(probe_p, FA["applicant_id"])
K_PS = pick(probe_p, FA["slot_id"])
K_PR = pick(probe_p, FA["pref_rank"])
K_METHOD = pick(probe_p, FA["method"])

# §B：分配结果里的键名才是判据真正关心的（产物可能用 student/topic 而不是 aid/rid）
if allocate_fn is not None:
    try:
        _probe_assign = call_adaptive(allocate_fn, "match")[0]
    except Exception:
        _probe_assign = None
    if isinstance(_probe_assign, list) and _probe_assign and isinstance(_probe_assign[0], dict):
        K_PA = pick(_probe_assign[0], FA["applicant_id"]) or K_PA
        K_PS = pick(_probe_assign[0], FA["slot_id"]) or K_PS

bad_keys = [n for n, k in (("申请人ID", K_AID), ("分数", K_SCORE), ("名额方ID", K_SID),
                           ("容量", K_CAP), ("偏好-申请人", K_PA), ("偏好-名额方", K_PS),
                           ("偏好优先级", K_PR)) if not k]
if bad_keys:
    out["skipped"] = ("产物对象的字段名无法识别：" + "、".join(bad_keys) +
                      "（可在 core/flow.py 的 FIELD_ALIASES 里补别名）")
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)


def scenario(label, caps, aid_names):
    """跑一条业务链：造对象 → 排名 → 分配 → 补录 → 汇总。

    §B：产物**不提供工厂函数**时，runner 自己造对象（形态按产物的参数名/类型），
    并**自适应调用**分配函数（参数个数/顺序由被调函数的真实签名决定）。
    """
    if _adaptive:
        # 申请者/槽位/偏好：runner 自造（**键名对齐产物实际用的**：
        # K_AID/K_PA/K_PS/K_PR 是从产物对象/分配结果里解析出来的）
        applicants = []
        for _aid, _base, _extra in aid_names:
            applicants.append({K_AID: _aid, "aid": _aid, "sid": _aid, "id": _aid,
                               "student": _aid, "student_id": _aid,
                               "name": "A" + _aid, "score": _base,
                               "weight": _base, "total": _base})
        slots = []
        for _sid, _cap in caps:
            slots.append({K_SID: _sid, "rid": _sid, "tid": _sid, "id": _sid,
                          "topic": _sid, "topic_id": _sid, "name": "S" + _sid,
                          "capacity": _cap, "quota": _cap, "limit": _cap})
        # 偏好：产物侧常按"**每人一条**，prefs 列表含其全部偏好"读
        # （实测 DA 只取同一申请者的第一条 → 若每人拆成多条，产物只看到首选，
        #  断言却按全部条目算 → 误判"有空位却没分到"）。
        # → 这里按人聚合：每人一条，prefs 列表按 rank 排好。
        _by_app: dict = {}
        for _a, _s, _r in spec["prefs"]:
            _by_app.setdefault(_a, []).append((_r, _s))
        prefs = []
        for _a, _lst in _by_app.items():
            _lst.sort()
            _alls = [_s for _r, _s in _lst]
            _top_r = _lst[0][0]
            _top_s = _lst[0][1]
            prefs.append({K_PA: _a, "aid": _a, "sid": _a, "student": _a,
                          "student_id": _a,
                          K_PS: _top_s, "rid": _top_s, "tid": _top_s,
                          "slot_id": _top_s, "topic": _top_s, "topic_id": _top_s,
                          K_PR: _top_r, "rank": _top_r, "priority": _top_r,
                          "pref_rank": _top_r,
                          "prefs": _alls, "topics": _alls, "choices": _alls})
        # ctx：按**本场景**的真实容量给名额（给通用值会让产物超配，
        # 实测"② 名额方不能被超额分配"因此判红）
        # 讲席偏好（配额方）：**容量必须与 slots 一致** —— 产物按这里的 quota 分配，
        # 断言按 slots 的 capacity 算；两套不一致就会误判"还有空位"（实测 ③ 因此判红）
        _tprefs = []
        for _sl, _cc in zip(slots, caps):
            _tprefs.append({K_SID: _sl.get(K_SID), "rid": _sl.get(K_SID),
                             "tid": _sl.get(K_SID), "topic": _sl.get(K_SID),
                             "topic_id": _sl.get(K_SID), "slot_id": _sl.get(K_SID),
                             "capacity": _cc, "quota": _cc, "limit": _cc,
                             "prefs": [a.get(K_AID) for a in applicants],
                             "students": [a.get(K_AID) for a in applicants],
                             "choices": [a.get(K_AID) for a in applicants]})
        _ctx = {"quota": [c for _s, c in caps],
                "applicant": applicants, "slot": slots, "pref": prefs,
                "teacher_pref": _tprefs,
                "applicant_ids": [a.get(K_AID) for a in applicants],
                "slot_ids": [s.get(K_SID) for s in slots]}
        order = []
        if rank_fn is not None:
            _r, _ok = call_adaptive(rank_fn, "applicant", _ctx)
            order = list(_r or []) if _ok else []
        expect = [a for a, _b, _e in aid_names]
        # §B：产物里**没有独立的排序函数**时（如排序内嵌在 DA 里），
        # 这一条断言**不适用** → 如实跳过，不判红（"认不出就跳过"的原则）
        if rank_fn is not None and order:
            add("%s：排名字段与分数一致" % label, order == expect,
                "得到 %s，应为 %s" % (order[:6], expect))
        first = []
        if allocate_fn is not None:
            _r, _ok = call_adaptive(allocate_fn, "match", _ctx)
            first = list(_r or []) if _ok else []
        extra = []
        if fill_fn is not None:
            _r, _ok = call_adaptive(fill_fn, "match", _ctx)
            extra = list(_r or []) if _ok else []
        assigns = first + extra
        stat = {}
        if summary_fn is not None:
            _r, _ok = call_adaptive(summary_fn, "match", _ctx)
            stat = _r if (_ok and isinstance(_r, dict)) else {}
        return applicants, slots, prefs, assigns, stat

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
    if not stat:
        return                      # §B：产物没有汇总函数 → 这一条不适用，跳过
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
    _min_checks = 8 if not _adaptive else 4   # §B：自适应路径下部分断言不适用
    if len(out["checks"]) - n0 < _min_checks:   # 防"零断言也算绿"
        out["error"] = "业务流断言不足 %d 条（跑出来的判据太少，不算通过）" % _min_checks
except Exception as exc:
    out["error"] = "业务流执行异常：%s: %s" % (type(exc).__name__, str(exc)[:200])
print(json.dumps(out, ensure_ascii=False))
'''

# 固定夹具：3 个申请者（权重 95 / 80 / 70）、5 条偏好、两套容量
BIZ_APPLICANTS = [("a1", 90.0, 5.0), ("a2", 80.0, 0.0), ("a3", 70.0, 0.0)]
BIZ_PREFS = [("a1", "s1", 1), ("a1", "s2", 2),
             ("a2", "s2", 1), ("a2", "s1", 2),
             ("a3", "s1", 1)]


def _modname(rel: str) -> str:
    """契约里的模块路径 → **可导入**的模块名（§B，2026-11-12）。

    为什么不能只用 `Path(rel).stem`：分层项目里 `app/api/topics.py` 与
    `app/services/topics.py` 的 stem **都叫 topics** → 互相覆盖，
    而且 stem 根本不是可导入的模块名（`import topics` 失败，
    实测报错 "No module named 'topics'"）。
    口径：路径去掉 `.py` 后用点号连接（`app/topics.py` → `app.topics`）；
    扁平项目（`atoms.py` → `atoms`）**逐字不变**。
    """
    r = str(rel or "").replace("\\", "/").strip()
    if r.endswith(".py"):
        r = r[:-3]
    parts = [p for p in r.split("/") if p and p not in (".", "")]
    return ".".join(parts)


def _role_map(contracts: dict) -> dict:
    """角色 → **候选函数列表** `[(模块 stem, 函数名), ...]`（按优先级）。

    为什么返回列表而不是单个（§B，2026-11-12）：
        角色识别原来只靠**函数名字面**同义词表 → 产物函数不叫
        `make_student` / `allocate` / `summary` 就一个都认不出
        → `applicable=False` → 业务流永远"不算通过"。
        而**真正的裁决必须靠试调用**（光看签名不够：产物的参数个数
        与 runner 的调用约定常常不同，实测毕设的 DA 是 6 参而约定是 4 参）。

    候选来源（按优先级）：
        ① 名字命中同义词表（老口径，**老项目行为逐字不变**）
        ② 参数名含**角色语义词**的契约函数（静态过滤，**不调用、无副作用**）
    ② 只是一张**候选清单**：runner 会逐个**试调用 + 验证返回形状**，
    第一个通过验证的才采用（见 BIZ_RUNNER 的 call_role）。
    """
    table = {**{r: v for r, v in BIZ_ROLES.items()},
             **{r: v for r, v in BIZ_OPTIONAL_ROLES.items()}}
    roles: dict = {}
    # ① 名字命中（老口径，优先级最高）
    for rel, c in (contracts or {}).items():
        stem = _modname(rel)
        for f in getattr(c, "funcs", []) or []:
            name = getattr(f, "name", "")
            for role, cands in table.items():
                if name in cands and role not in roles:
                    roles[role] = [(stem, name)]
    # ② 参数名语义过滤（静态，不调用）→ 候选清单
    for role, words in ROLE_PARAM_WORDS.items():
        if role in roles:
            continue                                  # 已由名字命中，候选就够
        picked: list = []
        for rel, c in (contracts or {}).items():
            stem = _modname(rel)
            for f in getattr(c, "funcs", []) or []:
                names = " ".join(str(getattr(p, "name", "") or "")
                                 for p in (getattr(f, "params", []) or []))
                low = names.lower()
                hit = [w for w in words if w in low]
                if not hit:
                    continue
                pair = (stem, getattr(f, "name", ""))
                if pair not in picked:
                    # 命中的语义词越多 → 越像这个角色 → 排前面（runner 逐个试，
                    # 先试最像的，省掉大量无谓试调用）
                    picked.append((pair, len(hit)))
        if picked:
            picked.sort(key=lambda x: -x[1])
            roles[role] = [p for p, _n in picked]
    return roles


def _merge_contracts(ws: Path, contracts: dict) -> dict:
    """把**全量契约**并进来（§C，2026-11-12）。

    为什么：业务流要凑齐 7 个角色，而调用方传的 `contracts` 是**本片**的契约
    （实测只有 5~6 个函数）→ 角色几乎必然凑不齐 → `applicable=False`。
    全量契约在 `.sixcoder/contract.txt`（engine 写的），并进来后候选集从
    "本片的几个函数"变成"全仓的几十个函数" → 认角色的机会大增。

    口径：**全量优先**（它是最新合成的），本片的同名模块覆盖它
    （本片契约更新的部分要生效）。
    """
    out = dict(contracts or {})
    p = Path(ws) / ".sixcoder" / "contract.txt"
    try:
        if p.is_file():
            txt = p.read_text(encoding="utf-8", errors="replace")
            from . import contract as _cm
            full = _cm.parse_requirement(txt)
            for rel, c in (full or {}).items():
                if rel not in out:
                    out[rel] = c
    except Exception:                                    # noqa: BLE001
        pass
    return out


def business_flow(ws: Path, contracts: dict, timeout: int = 180) -> dict:
    """跑一条通用业务链并断言业务不变量。

    返回 {ok, applicable, passed, total, checks, fails, reason}。
    诚实边界：角色或字段解析不出来 → `applicable=False`，**不算通过**；
    判据跑不动 → ok=False 并写明原因。

    §C（2026-11-12）：`contracts` 是**本片**的契约，凑不齐 7 个角色 →
    先并入**全量契约**（`.sixcoder/contract.txt`）再认角色。
    """
    ws = Path(ws).resolve()
    contracts = _merge_contracts(ws, contracts)
    roles = _role_map(contracts)
    missing = [r for r in BIZ_ROLES if r not in roles]
    if missing:
        # §B：自适应路径下，三个"造对象"角色由 **runner 自己**提供 → 不算缺。
        # 只有"分配链"的角色（allocate/summary）认不出才是真的没法判。
        _core_missing = [r for r in missing
                          if r not in ("make_applicant", "make_slot", "make_preference")]
        if _core_missing:
            return {"ok": False, "applicable": False, "passed": 0, "total": 0, "checks": [],
                    "fails": [],
                    "reason": "契约里找不到这些角色对应的函数：" + "、".join(_core_missing)}
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
        # 不删 runner：下一个回合会原地覆盖。每次调用都删一个文件，
        # 长跑累计会撞上外层"单轮删除"保护（实测 2026-11-07 直接打断夜班）。
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
