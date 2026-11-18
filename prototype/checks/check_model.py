"""领域模型层自检（2026-11-08）。

钉住四件事：
  1. 资源识别不把动作/查询串/数字当实体（曾经的 45 个垃圾实体）
  2. 外键按 `*_id` 反推，指向不存在的实体时置空并记 note（不猜）
  3. 初始态取**第一次出现的状态**（不是字典序最小 —— Candidate 会判成 adopted）
  4. 主干流按实体拓扑串起来，且负向用例不冒充建立端点
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as bpmod  # noqa: E402
from core import model as md  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def cap(cid, steps, modules=None):
    return bpmod.Cap(id=cid, desc="x", funcs_est=1, modules=modules or [],
                     probe={"kind": "http", "steps": steps})


def main() -> int:
    print("[1] 资源识别")
    check("/org/majors → major", md._resource("/api/v1/org/majors") == "major",
          md._resource("/api/v1/org/majors"))
    check("/topics/{id}/submit → topic",
          md._resource("/api/v1/topics/{topic_id}/submit") == "topic",
          md._resource("/api/v1/topics/{topic_id}/submit"))
    check("查询串不进资源名",
          "action" not in md._resource("/api/v1/audit/logs?action=appeal.create").lower()
          and "=" not in md._resource("/api/v1/audit/logs?action=appeal.create"),
          md._resource("/api/v1/audit/logs?action=appeal.create"))
    check("纯数字 id 不算资源", md._resource("/api/v1/topics/999999") == "topic",
          md._resource("/api/v1/topics/999999"))
    check("auth/register 不造实体", md._resource("/api/v1/auth/register") == "",
          md._resource("/api/v1/auth/register"))

    print("\n[2] 实体字段与外键")
    b = bpmod.Blueprint(goal="g")
    b.add(cap("cap.college", [{"method": "POST", "path": "/api/v1/org/colleges",
                               "json": {"code": "CS", "name": "计算机学院"},
                               "expect_status": 201}]))
    b.add(cap("cap.major", [{"method": "POST", "path": "/api/v1/org/majors",
                             "json": {"code": "SE", "name": "软工", "college_id": "1"},
                             "expect_status": 201}]))
    m = md.extract(b)
    e = m.by_resource("major")
    check("major 实体存在", e is not None)
    if e:
        fk = [f for f in e.fields if f.name == "college_id"]
        check("college_id 连到 College", bool(fk) and fk[0].fk == "College",
              str([(f.name, f.fk) for f in e.fields]))
        check("必填字段标出来", any(f.name == "name" and f.required for f in e.fields),
              str([(f.name, f.required) for f in e.fields]))

    print("\n[3] 初始态取首次出现")
    b2 = bpmod.Blueprint(goal="g")
    b2.add(cap("cap.gen", [{"method": "POST", "path": "/api/v1/llm/candidates",
                            "json": {"prompt_version": "v1"}, "expect_status": 201,
                            "expect_json": {"status": "pending_teacher"}}]))
    b2.add(cap("cap.adopt", [{"method": "POST",
                              "path": "/api/v1/llm/candidates/{cid}/adopt",
                              "expect_status": 200,
                              "expect_json": {"status": "adopted"}}]))
    m2 = md.extract(b2)
    ce = m2.by_resource("candidate")
    check("Candidate 初始态是 pending_teacher",
          bool(ce) and ce.initial == "pending_teacher",
          f"initial={getattr(ce, 'initial', None)} statuses={getattr(ce, 'statuses', None)}")
    check("有转换边", bool(ce) and len(ce.transitions) >= 1,
          str([t.to_dict() for t in (ce.transitions if ce else [])]))

    print("\n[4] 主干流与负向用例")
    b3 = bpmod.Blueprint(goal="g")
    b3.add(cap("cap.neg", [{"method": "POST", "path": "/api/v1/topics",
                            "json": {"title": "越权"}, "expect_status": 403}]))
    b3.add(cap("cap.topic", [{"method": "POST", "path": "/api/v1/topics",
                              "json": {"title": "正常题", "quota": 2},
                              "expect_status": 201}]))
    b3.add(cap("cap.pref", [{"method": "POST", "path": "/api/v1/preferences",
                             "json": {"topic_id": "1"}, "expect_status": 201}]))
    m3 = md.extract(b3)
    main = next((f for f in m3.flows if f.name == "main"), None)
    check("有主干流", main is not None and len(main.steps) >= 2,
          str(len(main.steps) if main else 0))
    if main:
        first_topic = [s for s in main.steps if s["path"] == "/api/v1/topics"]
        check("发题端点不来自负向用例",
              bool(first_topic) and first_topic[0]["cap"] == "cap.topic",
              str(first_topic))
        order = [s["path"] for s in main.steps]
        check("题目排在志愿前面",
              order.index("/api/v1/topics") < order.index("/api/v1/preferences"),
              str(order))

    print("\n[5] 四张图都能渲染")
    mg = md.to_mermaid(m3)
    for k in ("er", "state", "sequence", "component"):
        check(f"{k} 非空", bool((mg.get(k) or "").strip()), k)
    check("er 用 erDiagram", (mg.get("er") or "").startswith("erDiagram"))
    check("state 用 stateDiagram-v2", (mg.get("state") or "").startswith("stateDiagram-v2"))
    check("sequence 用 sequenceDiagram",
          (mg.get("sequence") or "").startswith("sequenceDiagram"))
    check("component 用 graph TD", (mg.get("component") or "").startswith("graph TD"))

    print("\n[6] 校验能抓错")
    bad = md.Model(entities=[md.Entity(name="X", resource="x", fields=[
        md.Field(name="a", fk="Ghost")])])
    probs = md.validate(bad)
    check("外键指向不存在实体被抓",
          any("Ghost" in p for p in probs), str(probs))
    check("没主键被抓", any("主键" in p for p in probs), str(probs))

    print("\n[7] 语义补全只信合法输入（幻觉拦截）")
    b4 = bpmod.Blueprint(goal="g")
    b4.add(cap("cap.topic", [{"method": "POST", "path": "/api/v1/topics",
                              "json": {"title": "t", "quota": 2},
                              "expect_status": 201,
                              "expect_json": {"status": "draft"}}]))
    b4.add(cap("cap.pub", [{"method": "POST", "path": "/api/v1/topics/{tid}/publish",
                            "expect_status": 200,
                            "expect_json": {"status": "published"}}]))
    b4.add(cap("cap.off", [{"method": "POST", "path": "/api/v1/topics/{tid}/offline",
                            "expect_status": 200,
                            "expect_json": {"status": "offline"}}]))
    m4 = md.extract(b4)
    te = m4.by_resource("topic")
    # 机械边：draft→published、draft→offline（后者是错的）
    check("机械边先有 draft→offline（错的）",
          any(t.to == "offline" and t.from_ == "draft" for t in te.transitions),
          str([t.to_dict() for t in te.transitions]))

    data = {
        "transitions": [
            {"entity": "Topic", "from": "draft", "to": "published", "action": "publish"},
            {"entity": "Topic", "from": "published", "to": "offline", "action": "offline"},
            {"entity": "Ghost", "from": "a", "to": "b"},
            {"entity": "Topic", "from": "draft", "to": "archived"},
            {"entity": "Topic", "from": "draft", "to": "draft"},
        ],
        "invariants": [
            {"entity": "Topic", "rule": "配额 1..100", "why": "需求§五"},
            {"entity": "NoSuch", "rule": "x"},
        ],
    }
    applied, dropped = md.merge_semantics(m4, data)
    check("采纳 2 条合法转换", applied == 2, f"applied={applied}")
    check("未知实体被丢弃", any("Ghost" in d for d in dropped), str(dropped))
    check("枚举外的状态被丢弃", any("archived" in d for d in dropped), str(dropped))
    check("自环被丢弃", any("自环" in d for d in dropped), str(dropped))
    check("错误的机械边被替换掉",
          not any(t.from_ == "draft" and t.to == "offline" for t in te.transitions),
          str([t.to_dict() for t in te.transitions]))
    check("正确边补上 published→offline",
          any(t.from_ == "published" and t.to == "offline" for t in te.transitions),
          str([t.to_dict() for t in te.transitions]))
    check("不变式指向未知实体被丢弃",
          any("NoSuch" in d for d in dropped), str(dropped))
    check("合法不变式留下了", len(m4.invariants) == 1,
          str(m4.invariants))
    check("替换后仍自洽", not md.validate(m4), str(md.validate(m4)))

    print("\n[8] 缺口机制：不猜，把不知道的显式列出来")
    b5 = bpmod.Blueprint(goal="g")
    b5.add(cap("cap.confirm", [{"method": "POST", "path": "/api/v1/adjustments/confirm",
                                "json": {"batch_id": "1"}, "expect_status": 200,
                                "expect_json": {"status": "rerun", "blocking_pairs": 0}}]))
    m5 = md.extract(b5)
    ae = m5.by_resource("adjustment")
    check("单状态实体不硬填初始态", ae is not None and ae.initial == "",
          f"initial={getattr(ae, 'initial', None)!r}")
    gs = md.gaps(m5)
    kinds = {g.kind for g in gs}
    check("报 single_state 缺口", "single_state" in kinds, str(kinds))
    check("报 initial_unknown 缺口", "initial_unknown" in kinds, str(kinds))
    sg = next(g for g in gs if g.kind == "single_state")
    check("缺口写了需要什么证据", bool(sg.how) and "探针" in sg.how, sg.how)

    # 远端补状态时必须给依据，否则丢弃
    applied, dropped = md.merge_semantics(m5, {"new_statuses": [
        {"entity": "Adjustment", "name": "pending", "why": "需求§十一 推荐后待确认"},
        {"entity": "Adjustment", "name": "confirmed"},
    ]})
    check("有依据的新状态被采纳", "pending" in ae.statuses, str(ae.statuses))
    check("无依据的新状态被丢弃", "confirmed" not in ae.statuses, str(ae.statuses))
    check("新状态标成 declared", ae.status_src.get("pending") == "declared",
          str(ae.status_src))
    check("原观测状态仍是 observed", ae.status_src.get("rerun") == "observed",
          str(ae.status_src))

    # 补出转换后，初始态由源点机械推出
    md.merge_semantics(m5, {
        "transitions": [{"entity": "Adjustment", "from": "pending", "to": "rerun",
                         "action": "confirm"}],
        "initials": [{"entity": "Adjustment", "state": "pending"}],
    })
    check("初始态补成 pending", ae.initial == "pending", ae.initial)
    check("转换边建起来了",
          any(t.from_ == "pending" and t.to == "rerun" for t in ae.transitions),
          str([t.to_dict() for t in ae.transitions]))

    print("\n[9] 同层顺序按入度，不按字典序")
    b6 = bpmod.Blueprint(goal="g")
    b6.add(cap("cap.c", [{"method": "POST", "path": "/api/v1/org/colleges",
                          "json": {"name": "x"}, "expect_status": 201}]))
    b6.add(cap("cap.m", [{"method": "POST", "path": "/api/v1/org/majors",
                          "json": {"name": "y", "college_id": "1"},
                          "expect_status": 201}]))
    b6.add(cap("cap.a", [{"method": "POST", "path": "/api/v1/algorithms/selftest",
                          "json": {"students": 6}, "expect_status": 200}]))
    m6 = md.extract(b6)
    main6 = next(f for f in m6.flows if f.name == "main")
    paths = [s["path"] for s in main6.steps]
    check("colleges 排在 algorithms 前面",
          paths.index("/api/v1/org/colleges") < paths.index("/api/v1/algorithms/selftest"),
          str(paths))

    print("\n[10] 不变式缺口：能转的转，不能转的标手工")
    m6.invariants = [
        {"entity": "Preference", "rule": "一个学生最多填三个志愿", "why": "§三"},
        {"entity": "Topic", "rule": "名额不得转借给他人", "why": "§八"},
    ]
    gs6 = md.gaps(m6)
    lim = [g for g in gs6 if g.kind == "invariant_limit"]
    man = [g for g in gs6 if g.kind == "invariant_manual"]
    check("数量上限被识别成负向探针缺口", len(lim) == 1 and lim[0].probe.get("limit") == 3,
          str([g.detail for g in lim]))
    check("无限可数上限的标成手工", len(man) == 1, str([g.detail for g in man]))

    print("\n[11] 缺口先认领已有能力，不重复造探针")
    b7 = bpmod.Blueprint(goal="g")
    b7.add(cap("cap.pref.reject_fourth", [
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": 1},
         "expect_status": 201},
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": 1},
         "expect_status": 422},
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": 2},
         "expect_status": 201},
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": 3},
         "expect_status": 201},
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": 4},
         "expect_status": 422},
    ]))
    b7.add(cap("cap.pref.fill", [
        {"method": "POST", "path": "/api/v1/preferences",
         "json": {"rank": 1, "topic_id": "1"}, "expect_status": 201}]))
    m7 = md.extract(b7)
    m7.invariants = [{"entity": "Preference", "rule": "一个学生最多填三个志愿",
                      "why": "§三"}]
    cover = md._invariant_covered(m7, b7, "Preference", 3)
    check("认领到 reject_fourth", cover == "cap.pref.reject_fourth", cover)
    check("已被覆盖的不再报缺口",
          not [g for g in md.gaps(m7, b7) if g.kind == "invariant_limit"],
          str([g.detail for g in md.gaps(m7, b7) if g.kind == "invariant_limit"]))
    check("成功次数不够就不算覆盖",
          md._invariant_covered(m7, b7, "Preference", 5) == "", "5 次成功没测过")
    check("给了 bp 才敢认领（不知情时不假装覆盖）",
          md._invariant_covered(m7, None, "Preference", 3) == "", "bp=None")

    print("\n[12] 不变式 → 负向探针（模板化，绝不编字段）")
    b8 = bpmod.Blueprint(goal="g")
    b8.add(cap("cap.pref.fill", [
        {"method": "POST", "path": "/api/v1/preferences",
         "json": {"topic_id": 1, "rank": 1, "acceptability": 1.0},
         "expect_status": 201}]))
    m8 = md.extract(b8)
    m8.invariants = [
        {"entity": "Preference", "rule": "一个学生最多填三个志愿", "why": "§三",
         "assert_kind": "over_limit", "limit": 3},
        {"entity": "Preference", "rule": "名额不得转借给他人", "why": "§八",
         "assert_kind": "manual"},
    ]
    probes = md.invariant_probes(m8, b8)
    check("只给可执行的生成探针", len(probes) == 1, str([p["cap_id"] for p in probes]))
    if probes:
        p0 = probes[0]
        check("上限 3 → 4 步", len(p0["steps"]) == 4, str(len(p0["steps"])))
        check("前 3 步期望成功",
              all(s["expect_status"] == 201 for s in p0["steps"][:3]),
              str([s["expect_status"] for s in p0["steps"]]))
        check("第 4 步期望被拒",
              isinstance(p0["steps"][3]["expect_status"], list)
              and 400 in p0["steps"][3]["expect_status"],
              str(p0["steps"][3]["expect_status"]))
        check("用真实字段（topic_id/acceptability 都在）",
              {"topic_id", "acceptability"} <= set(p0["steps"][0]["json"]),
              str(p0["steps"][0]["json"]))
        check("序号字段递进",
              [s["json"]["rank"] for s in p0["steps"]] == [1, 2, 3, 4],
              str([s["json"].get("rank") for s in p0["steps"]]))
    # 已被现有能力覆盖 → 不生成
    b9 = bpmod.Blueprint(goal="g")
    b9.add(cap("cap.pref.reject_fourth", [
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": i},
         "expect_status": 201} for i in (1, 2, 3)] + [
        {"method": "POST", "path": "/api/v1/preferences", "json": {"rank": 4},
         "expect_status": 422}]))
    m9 = md.extract(b9)
    m9.invariants = [{"entity": "Preference", "rule": "一个学生最多填三个志愿",
                      "why": "§三", "assert_kind": "over_limit", "limit": 3}]
    check("已覆盖的不重复造", md.invariant_probes(m9, b9) == [],
          str([p["cap_id"] for p in md.invariant_probes(m9, b9)]))
    # 没有真实创建样本 → 不造
    b10 = bpmod.Blueprint(goal="g")
    b10.add(cap("cap.getonly", [{"method": "GET", "path": "/api/v1/preferences",
                                 "expect_status": 200}]))
    m10 = md.extract(b10)
    m10.invariants = [{"entity": "Preference", "rule": "最多三个", "why": "§三",
                       "assert_kind": "over_limit", "limit": 3}]
    check("没真实请求体就不造", md.invariant_probes(m10, b10) == [])

    print("\n[13] state_guard 两种形态")
    b11 = bpmod.Blueprint(goal="g")
    # 初始态 created ≠ locked → 形态 A：刚创建就调匹配，必须被拒
    b11.add(cap("cap.batch.create", [{"method": "POST", "path": "/api/v1/batches",
                                      "json": {"name": "b1"}, "expect_status": 201,
                                      "expect_json": {"status": "created"}}]))
    b11.add(cap("cap.batch.run", [{"method": "POST",
                                   "path": "/api/v1/batches/{batch_id}/run",
                                   "expect_status": 200,
                                   "expect_json": {"status": "matched"}}]))
    # 初始态 draft == guard(draft) → 形态 B：连调两次，第二次被拒
    b11.add(cap("cap.topic.create", [{"method": "POST", "path": "/api/v1/topics",
                                      "json": {"title": "t"}, "expect_status": 201,
                                      "expect_json": {"status": "draft"}}]))
    b11.add(cap("cap.topic.submit", [{"method": "POST",
                                      "path": "/api/v1/topics/{topic_id}/submit",
                                      "expect_status": 201,
                                      "expect_json": {"status": "pending"}}]))
    m11 = md.extract(b11)
    be = m11.by_resource("batch")
    if be is not None:
        if not be.initial:
            be.initial = "created"
        if "locked" not in be.statuses:      # 真实仓库里由远端补成 declared
            be.statuses.append("locked")
            be.status_src["locked"] = "declared"
    te2 = m11.by_resource("topic")
    if te2 is not None and not te2.initial:
        te2.initial = "draft"
    m11.invariants = [
        {"entity": "Batch", "rule": "只有 locked 状态的批次可执行匹配", "why": "§九",
         "assert_kind": "state_guard", "guard_state": "locked",
         "action_path": "/api/v1/batches/{id}/run_matching"},
        {"entity": "Topic", "rule": "只有草稿状态的题目可提交审核", "why": "§七",
         "assert_kind": "state_guard", "guard_state": "draft",
         "action_path": "/api/v1/topics/{id}/submit"},
    ]
    pr11 = md.invariant_probes(m11, b11)
    by_ent = {p["cap_id"].split(".")[2]: p for p in pr11}
    check("两条 state_guard 都生成", len(pr11) == 2, str([p["cap_id"] for p in pr11]))
    if "batch" in by_ent:
        s = by_ent["batch"]["steps"]
        check("形态A：先创建再调操作", len(s) == 2 and "save" in s[0], str(s[0]))
        check("形态A：操作步期望被拒",
              isinstance(s[1]["expect_status"], list) and 422 in s[1]["expect_status"],
              str(s[1]["expect_status"]))
        check("形态A：路径参数换成变量",
              "${batch_id}" in s[1]["path"], s[1]["path"])
        check("形态A：假 action_path 被换成真实端点",
              s[1]["path"].endswith("/run"), s[1]["path"])
    if "topic" in by_ent:
        s = by_ent["topic"]["steps"]
        check("形态B：同一操作连调两次",
              len(s) == 2 and s[0]["path"] == s[1]["path"], str([x["path"] for x in s]))
        check("形态B：第二次期望被拒",
              isinstance(s[1]["expect_status"], list) and 422 in s[1]["expect_status"],
              str(s[1]["expect_status"]))

    print("\n[14] guard_state 标错实体时按状态反查纠正")
    m12 = md.extract(b11)
    # 纠正的前提是 guard_state 能在某个实体里唯一找到：给 Topic 补上 draft 已有，
    # 但要让 Preference 不存在 + draft 仅出现在 Topic，才不会歧义
    for e in m12.entities:
        if e.name != "Topic":
            e.statuses = [s for s in e.statuses if s != "draft"]
    m12.invariants = [{"entity": "Preference", "rule": "只有草稿题目可提交审核",
                       "why": "§七", "assert_kind": "state_guard",
                       "guard_state": "draft", "action_path": ""}]
    pr12 = md.invariant_probes(m12, b11)
    check("纠正后能生成", len(pr12) == 1, str([p["cap_id"] for p in pr12]))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（资源识别 / 外键 / 初始态 / 主干流 / 四张图 / 校验）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
