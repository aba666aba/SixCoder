"""SixCoder 命令行（CLI）。

一个入口同时覆盖两件事：
1. **命令行执行能力**：probe / doctor / run / status / gates / diff —— 不用开浏览器也能跑完整任务
2. **命令行调用工具的能力**：tool read|list|edit|bash|git / git status|log|diff|undo —— 把 Agent 的工具当独立命令用

用法示例：
    python cli.py doctor
    python cli.py probe demo-repo
    python cli.py run --repo demo-repo --requirement "给 calc.py 增加 divide，除零抛 ValueError，并补测试"
    python cli.py status t-xxxx
    python cli.py tool read demo-repo calc.py --start 1 --end 20
    python cli.py tool bash demo-repo "python -m pytest -q"
    python cli.py tool edit demo-repo calc.py --file-search s.txt --file-replace r.txt
    python cli.py git status demo-repo
    python cli.py git undo demo-repo calc.py
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，统一改 UTF-8，避免中文/符号抛 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

from core import agent, db, gitops, ollama, permissions, probe, review as review_mod, tools  # noqa: E402


def out(data, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2))


def _slim(payload: dict) -> str:
    keep = ("state", "policy", "gate", "ok", "evidence_kind", "path", "attempt",
            "temperature", "model", "ms", "tokens", "snapshot", "reason", "message",
            "passed", "model_assertion_rate", "mutation_kill_rate", "nodes_ok", "nodes_total",
            # 全自主模式的进度字段（蓝图/切片/覆盖/花费）：以前这些不在白名单里，
            # 事件打出来是一行空白 —— 命令行看不到进度，等于把可观测性丢了
            "phase", "goal", "version", "caps", "funcs", "slice", "cap", "gained", "left",
            "budget_cny", "cost_cny", "spent_cny", "pct", "coverage", "replans", "descoped",
            "contract", "why", "added", "rewired", "funcs_cap", "problems", "next_funcs_cap", "streak", "entry", "n", "changes",
            "questions", "answered", "samples", "ids")
    parts = [f"{k}={payload[k]}" for k in keep if k in payload]
    if not parts:
        # 新事件的字段没进白名单时别再打一行空白（文档 18.5 踩过、dev1106a 又复现：
        # scale / governance / round 全打成空行，命令行等于没有进度）。
        parts = [f"{k}={str(v)[:60]}" for k, v in list(payload.items())[:6]
                 if isinstance(v, (str, int, float, bool)) or v is None]
    if "diff" in payload:
        d = payload["diff"] or ""
        add = len([l for l in d.splitlines() if l.startswith("+") and not l.startswith("+++")])
        dele = len([l for l in d.splitlines() if l.startswith("-") and not l.startswith("---")])
        parts.append(f"diff=+{add}/-{dele} lines")
    if "detail" in payload:
        parts.append("detail=" + str(payload["detail"]).replace("\n", " ")[:120])
    if "nodes" in payload and isinstance(payload["nodes"], list):
        parts.append("plan=" + ",".join(f"{n.get('kind')}:{n.get('path')}" for n in payload["nodes"]))
    return " ".join(parts)


# ---------------------------------------------------------------- 命令实现

def cmd_doctor(args) -> None:
    db.init()
    items = probe.toolchains()
    data = {
        "ollama": ollama.alive(),
        "models": ollama.models(),
        "default_model": ollama.pick_default() if ollama.alive() else None,
        "python": sys.version.split()[0],
        "toolchains": items,
        "toolchains_ok": sum(1 for i in items if i["ok"]),
        "toolchains_total": len(items),
        "db": str(db.DB_PATH),
    }
    if args.json:
        out(data, True)
        return
    print(f"Ollama        {'[在线]' if data['ollama'] else '[离线]'}  models={', '.join(data['models']) or '无'}")
    print(f"默认模型      {data['default_model']}")
    print(f"Python        {data['python']}")
    print(f"数据库        {data['db']}")
    print(f"工具链        {data['toolchains_ok']}/{data['toolchains_total']} 可用")
    for t in items:
        flag = "[OK]" if t["ok"] else ("[WARN]" if t["version"] else "[FAIL]")
        print(f"  {flag} {t['id']:<7} {str(t['version'] or '—'):<12} {t['path'] or '未找到':<55} {t['source'] or ''}")
        if not t["ok"] and t.get("fix_hint"):
            print(f"       └─ 帮助条目：{t['fix_hint']}")


def cmd_inspect_docs(args) -> None:
    """定期巡检：缓存命中率趋势 + 文档健康（只读，不改任何文件）。"""
    from core import doctor
    res = doctor.run(Path(args.repo), args.scale or "M")
    if args.json:
        out(res, True)
        return
    print(f"巡检 @ {args.repo}（{res['level']} 级）｜需处理 {res['warn']} 条")
    for line in doctor.report_lines(res):
        if line.strip() and not line.startswith("###"):
            print(line)
    if not res["ok"]:
        sys.exit(1)


def cmd_probe(args) -> None:
    info = probe.classify(Path(args.repo))
    if args.json:
        out(info, True)
        return
    s = info["signals"]
    print(f"仓库     {s['path']}")
    print(f"形态     {info['state']}  （{info['note']}）")
    print(f"策略     {info['policy']}")
    print(f"提交数   {s.get('commit_count')}   作者 {s.get('author_count')}   脏文件 {s.get('dirty_files')}")
    print(f"代码文件 {s.get('code_files')}   测试文件 {s.get('test_files')}   测试状态 {s.get('tests_green')}")
    print(f"CI       {s.get('has_ci')}   迁移 {s.get('has_migrations')}   语言 {s.get('lang_composition')}")


def cmd_run(args) -> None:
    _need("agent.run")
    db.init()
    if getattr(args, "requirement_file", ""):
        args.requirement = Path(args.requirement_file).read_text(encoding="utf-8-sig")
    if not (args.requirement or "").strip():
        print("[FAIL] 需要 --requirement 或 --requirement-file")
        sys.exit(2)
    repo = Path(args.repo)
    if not repo.exists():
        print(f"[FAIL] 仓库不存在：{repo}")
        sys.exit(2)
    if not ollama.alive():
        print("[FAIL] Ollama 不可达，请先 `ollama serve`")
        sys.exit(3)
    model = args.model or ollama.pick_default()
    try:
        from core import model_policy
        model_policy.require(model)
    except Exception as exc:                             # ModelTooSmall / 任何门槛异常都不许静默
        print(f"[FAIL] {exc}")
        sys.exit(4)
    task_id = agent.new_task(repo, args.requirement, args.target, model)
    print(f"> 任务 {task_id}  model={model}")

    def printer(ev) -> None:
        print(f"  [{ev['ts'][11:]}] {ev['kind']:<12} {_slim(ev['payload'])}", flush=True)

    em = agent.Emitter(task_id)
    if not args.quiet:
        em.bind(printer)
    th = agent.start_thread(task_id, repo, args.requirement, args.target, model, em)
    th.join()

    rows = db.query("SELECT state,workspace,audit_json FROM tasks WHERE id=?", (task_id,))
    state = rows[0]["state"] if rows else "unknown"
    audit = db.audit(task_id)
    if rows and rows[0].get("audit_json"):
        try:
            audit.update(json.loads(rows[0]["audit_json"]))
        except Exception:
            pass
    print(f"\n■ 结果 state={state}")
    print(f"  节点 {audit['nodes_ok']}/{audit['nodes_total']}   通过证据 {audit['passed']}   闸门失败 {audit['gates_failed']}/{audit['gates_total']}")
    print(f"  模型断言率 {audit['model_assertion_rate']}（必须 0）   变异杀死率 {audit['mutation_kill_rate']}")
    print(f"  证据构成 {audit['evidence_by_kind']}")
    if rows and rows[0]["workspace"]:
        print(f"  工作区 {rows[0]['workspace']}")
    if state != "done" and args.exit_code:
        sys.exit(1)


def cmd_night(args) -> None:
    """夜班（公司制）：甲方模式提需求 → 内部做完所有决策 → 早上读交付报告。

    与 `run` 的区别：run 是"一个需求 → 一对测试/实现节点"；night 是"反复体检/派工/开发/
    机械验收，直到收敛或预算耗尽"，并写出 DELIVERY.md / TRACE.md。
    模式决定协作方式（core/policy.py）：甲方模式由远端拍板、本地判红即远端接管写代码；
    开发者模式必须给 --plan（或 --plan-file），没有方案就抛 needhuman、不替人做架构决策。
    """
    _need("agent.run")
    from core import company, policy

    pol = policy.current()
    if pol.code_escalation == policy.OFF:
        print(f"[FAIL] {pol.name}不写文件，不能跑夜班；请切到甲方模式或开发者模式")
        sys.exit(2)
    db.init()
    goal = str(getattr(args, "goal", "") or "").strip()
    requirement = args.requirement or ""
    if getattr(args, "requirement_file", ""):
        requirement = Path(args.requirement_file).read_text(encoding="utf-8-sig")
    if not requirement.strip() and not goal:
        print("[FAIL] 需要 --goal（一句话目标，全自主）或 --requirement / --requirement-file")
        sys.exit(2)
    if goal and requirement.strip():
        print("[FAIL] --goal 与 --requirement 只能用一个：目标模式自己规划切片，不再吃需求清单")
        sys.exit(2)
    plan = args.plan or ""
    if getattr(args, "plan_file", ""):
        plan = Path(args.plan_file).read_text(encoding="utf-8-sig")
    if pol.plan_authority == "human" and not plan.strip() and not args.allow_no_plan:
        print("[FAIL] 开发者模式需要实现方案：用 --plan / --plan-file 给出"
              "（模块划分、函数职责、数据流）。系统不会替你决定架构。")
        sys.exit(2)
    repo = Path(args.repo)
    if not repo.exists():
        if goal:
            # 全自主模式是从 0 建仓库：--repo 允许指向还不存在的目录。
            # 实测踩过：按需求模式的旧口径直接 exit(2)，一句话目标根本跑不起来。
            repo.mkdir(parents=True, exist_ok=True)
            print(f"[auto] 已新建工作区：{repo}")
        else:
            print(f"[FAIL] 仓库不存在：{repo}")
            sys.exit(2)
    if not ollama.alive():
        print("[FAIL] Ollama 不可达，请先 `ollama serve`")
        sys.exit(3)
    model = args.model or ollama.pick_default()
    try:
        from core import model_policy
        model_policy.require(model)
    except Exception as exc:                             # noqa: BLE001
        print(f"[FAIL] {exc}")
        sys.exit(4)

    def printer(kind, payload) -> None:
        # 默认打印**所有**事件。以前用 kind 白名单决定可见性，结果新加的
        # selfcheck_done / wiring / probe_aligned / regression 全被吞掉，
        # 命令行看不到片内自验在干什么（实测 dev1106a/b 每轮空耗两分钟却查不出）。
        # 白名单容易漏，改成全打 + 限长，宁可多几行也不要丢可观测性。
        if kind in ("context_layout", "usage", "event"):
            return
        print(f"   {kind:<18} {_slim(payload or {})[:150]}", flush=True)

    if goal:
        # ---- 全自主模式：只给一句话目标，切片与验收对象由系统自己规划 ----
        from core import auto as _auto
        from core import blueprint as _bp
        print(f"> 全自主模式：目标「{goal}」")
        print(f"> 仓库 {repo}  model={model}")
        print(f"> 总预算 " + (f"¥{args.max_cny:.2f}" if args.max_cny else "（未设，不封顶）")
              + (f"｜时间 {args.hours}h" if args.hours else ""))
        approve = None
        if getattr(args, "approve_plan", False):
            def approve(bp) -> bool:                     # noqa: F811
                print("\n" + _bp.summary(bp) + "\n")
                try:
                    return input("蓝图认可吗？y 开跑，其它键停止：").strip().lower() in ("y", "yes")
                except EOFError:
                    return False
        _samples = ""
        _sp = getattr(args, "spec_sample", "") or ""
        if _sp:
            try:
                _samples = Path(_sp).read_text(encoding="utf-8-sig")
                print(f"> 验收样例：{_sp}")
            except OSError as exc:
                print(f"[FAIL] 读不了验收样例：{exc}")
                sys.exit(2)
        # ---- 阶段 1：架构问答（人只在最贵的位置介入这一次）----
        _arch = {}
        if getattr(args, "clarify", False):
            # 新澄清循环：多轮 / 单文档 / 多选 / 双闸门；结果转 arch 格式。
            from core import auto as _auto
            _ans = None
            if getattr(args, "answers", ""):
                try:
                    _ans = _read_answers_file(args.answers)
                except OSError as exc:
                    print(f"[FAIL] 读不了答案文件：{exc}")
                    sys.exit(2)
            _arch = _auto.run_clarify_phase(repo, goal, answers=_ans, interactive=not _ans)
            if not _arch.get("answers"):
                print("[WARN] 澄清未完成（无答案），将按无安排直接生成，质量会明显下降")
        elif getattr(args, "ask", False) or getattr(args, "answers", "") or getattr(args, "interview", False):
            _arch = _arch_interview(repo, goal,
                                    force_ask=bool(getattr(args, "ask", False)),
                                    answers_path=str(getattr(args, "answers", "") or ""),
                                    interactive=bool(getattr(args, "interview", False)))
            if _arch.get("asked_only"):
                sys.exit(0)
            _arch.pop("asked_only", None)
        res = _auto.night_auto(repo, goal, budget_cny=args.max_cny, hours=args.hours,
                               model=model,
                               max_slices=getattr(args, "max_slices", 0) or 0,
                               max_rounds=args.max_rounds,
                               slice_hours=getattr(args, "slice_hours", 0.0) or 0.0,
                               fresh=getattr(args, "fresh", False),
                               emit=None if args.quiet else printer, approve=approve,
                               spec_samples=_samples, arch=_arch or None,
                               selfcheck=not getattr(args, "no_selfcheck", False))
        print("\n■ " + _auto.describe(res))
        if res.get("report"):
            print(f"  自主报告 {res['report']}")
        for row in (res.get("descoped") or [])[:6]:
            print(f"  降级 {row}")
        for row in (res.get("blocked") or [])[:6]:
            print(f"  受阻 {row.get('cap')}：{row.get('why')}")
        sys.exit(0 if res.get("ok") else 1)

    # 规模分级：不同代码规模该有的约束不一样，提需求时就要写明目标规模。
    from core import scale as _scale
    scope = _scale.resolve(requirement, repo)
    if args.scale:
        scope = {**scope, "level": str(args.scale).upper(), "source": "cli"}
    if scope.get("warning") and scope.get("source") != "cli":
        print(f"[WARN] {scope['warning']}")
    print(f"> 代码规模 {scope['level']} 级（{_scale.profile(scope['level']).label}）"
          f"｜依据：{scope['source']}")
    print(f"> 门禁：执行 {', '.join(_scale.gate_plan(scope['level'])['run'])}")
    print(f">       不做 {', '.join(n for n, _ in _scale.gate_plan(scope['level'])['skip']) or '（无）'}")
    print(f"> {policy.describe(pol)}")
    print(f"> 仓库 {repo}  model={model}  预算 {args.hours}h")
    if plan.strip():
        print("> 实现方案（权威）已注入：", plan.strip().splitlines()[0][:60])

    # 预算：没给就按代码规模取默认（S 0.5h / M 2h / L 6h / XL 12h）
    hours = args.hours or _scale.default_hours(scope["level"])
    print(f"> 预算 {hours}h（{'按规模默认' if not args.hours else '命令行指定'}）")
    res = company.night(repo, requirement, model, hours=hours,
                        use_remote=not args.no_remote,
                        max_rounds=args.max_rounds,
                        emit=None if args.quiet else printer, plan=plan,
                        remote_profile=getattr(args, "remote_model", "") or "",
                        max_cny=getattr(args, "max_cny", 0.0) or 0.0,
                        scale_level=(scope.get("level") if args.scale else ""))
    print(f"\n■ 夜班结束：{res.verified}/{res.total} 验收通过｜{res.spent_s:.0f}s｜{res.reason}")
    if res.delivery_path:
        print(f"  交付报告 {res.delivery_path}")
    if not res.ok:
        sys.exit(1)


def _read_answers_file(path: str) -> dict:
    """答案文件两种写法都收：JSON 字典，或 `Q1: 答案` 逐行（含多行续写）。"""
    from core import architect as _arch_mod
    return _arch_mod.parse_answers(Path(path).read_text(encoding="utf-8-sig"))


def _arch_interview(ws: Path, goal: str, *, force_ask: bool = False,
                    answers_path: str = "", interactive: bool = True,
                    ask_only: bool = False) -> dict:
    """阶段 1：远端就架构安排提问 → 人给安排 → 落盘。之后全自动，人不再介入。

    省钱的规则：问题已经落盘就不重复问（`--ask` 才强制重问）；
    人不回答就用默认安排（保证仍然能开跑，且默认可追责）。
    """
    from core import architect as arch
    qs = [] if force_ask else arch.load_questions(ws)
    if not qs:
        if not goal.strip():
            print("[FAIL] 没有问题缓存、也没给目标：无法提问")
            sys.exit(2)
        print("> 阶段 1：远端就架构安排提问（只花这一次，之后人不再介入）")
        got = arch.ask(goal)
        if not got.get("ok"):
            print("[WARN] 提问失败（" + "；".join(got.get("problems") or []) + "）"
                  "——按无安排直接生成，质量会明显下降")
            return {}
        qs = got["questions"]
        arch.save(ws, qs, None, goal)
        print(f"> 共 {len(qs)} 个问题 → {arch.QUESTIONS_FILE}"
              f"（填好后用 --answers 传回，或直接交互答）")
    for q in qs:
        opt = ("／".join(q["options"]) + "  ") if q.get("options") else ""
        print(f"\n[{q['id']}] {q['q']}")
        if q.get("why"):
            print(f"     为什么问：{q['why']}")
        print(f"     建议安排：{q.get('default') or '（空）'}    {opt}".rstrip())
    if ask_only:
        print(f"\n> 问题已写入 {ws / arch.QUESTIONS_FILE}；"
              f"把答案写进 {arch.ANSWERS_FILE} 后重跑 --answers 即可")
        return {"questions": qs, "answers": arch.load_answers(ws), "asked_only": True}

    ans: dict = arch.load_answers(ws) if not answers_path else {}
    if answers_path:
        try:
            ans = _read_answers_file(answers_path)
        except OSError as exc:
            print(f"[FAIL] 读不了答案文件：{exc}")
            sys.exit(2)
        print(f"> 架构安排来自 {answers_path}")
    elif interactive:
        for q in qs:
            try:
                line = input(f"  {q['id']} 回答（回车＝用建议）：").strip()
            except EOFError:
                line = ""
            if line:
                ans[q["id"]] = line
    arch.save(ws, qs, ans, goal)
    done = sum(1 for q in qs if str(ans.get(q["id"]) or "").strip())
    print(f"> 架构安排已定：{done}/{len(qs)} 条由人拍板，其余走建议默认"
          f" → {ws / arch.ARCH_FILE}")
    return {"questions": qs, "answers": ans}


def cmd_plan(args) -> None:
    """自主规划层的只读视图：蓝图 / 切片 / 覆盖 / 单条能力溯源（不花一次远端调用）。

    这四个视图对应"计划错得看不出来"的四种看法：
      show   —— 全场一眼看完（人审那一页）
      slices —— 下一刀会切在哪（剂量、闭合、永远轮不到的）
      cover  —— 现在到底做完没有（真执行探针）
      trace  —— 某条能力的来龙去脉（能力 → 切片 → 契约 → 该片交付报告）
    """
    _need("agent.run")
    from core import auto as _auto
    from core import blueprint, coverage, probe, slicer
    ws = Path(args.repo)
    what = (getattr(args, "what", "") or "show").lower()
    if what == "ask":
        # 阶段 1 单独跑：只提问、不生成、不花钱在代码上
        _arch_interview(ws, str(getattr(args, "goal", "") or ""),
                        force_ask=True, answers_path=str(getattr(args, "answers", "") or ""),
                        interactive=False, ask_only=not getattr(args, "answers", ""))
        return
    bp = blueprint.load(ws)
    if not bp.caps:
        print("[FAIL] 还没有蓝图：先跑一次 "
              '`sixcoder night --repo <目录> --goal "一句话目标" --max-cny 1`')
        sys.exit(2)
    if what == "show":
        print(blueprint.summary(bp))
        return
    if what == "slices":
        plan = slicer.plan(bp)
        print(f"待办切片 {len(plan['slices'])} 个｜合计 {plan['funcs']} 个函数")
        for sl in plan["slices"]:
            print(f"  {sl['id']}  函数 {sl['funcs']:>3}  {sl['title']}"
                  f"  模块 {', '.join(sl['modules']) or '-'}")
        for cid, why in plan["blocked"]:
            print(f"  ⊘ {cid} 永远轮不到：{why}")
        for p in plan["problems"]:
            print(f"  ! {p}")
        return
    if what == "cover":
        cov = coverage.run(bp, ws)
        coverage.record(ws, cov, "手工观测")
        bp2 = coverage.mark(bp, cov)
        blueprint.save(bp2, ws)
        print(coverage.report(bp2, cov))
        sys.exit(0 if coverage.ratios(cov)["pct"] >= 100.0 else 1)
    if what == "trace":
        cid = str(getattr(args, "cap", "") or "").strip()
        c = bp.by_id(cid)
        if c is None:
            print(f"[FAIL] 蓝图里没有 `{cid}`；用 `sixcoder plan show --repo {args.repo}` 看清单")
            sys.exit(2)
        dependents = [x.id for x in bp.caps if c.id in x.deps]
        print(f"能力 {c.id}")
        print(f"  内容：{c.desc}")
        print(f"  状态：{c.status}" + (f"（{c.why}）" if c.why else ""))
        print(f"  观测：{probe.describe(c.probe)}")
        print(f"  依赖：{', '.join(c.deps) or '-'}")
        print(f"  被依赖：{', '.join(dependents) or '-'}")
        print(f"  切片：{c.slice_id or '-'}")
        if c.slice_id:
            for label, p in (("契约", Path(ws) / _auto.SLICE_DIR / f"{c.slice_id}.txt"),
                             ("该片交付报告",
                              Path(ws) / _auto.SLICE_DIR / f"{c.slice_id}.DELIVERY.md")):
                print(f"  {label}：{p}{'（存在）' if p.exists() else '（缺失）'}")
        hs = coverage.history(ws, limit=1)
        if hs:
            st = dict(hs[-1].get("statuses") or {}).get(cid)
            print(f"  最近一次观测：{hs[-1].get('at')}  → {st}")
        return
    print(f"[FAIL] 不认识的视图：{what}（show / slices / cover / trace）")
    sys.exit(2)


def cmd_questions(args) -> None:
    """列出待人回答的问题（开发者模式里本地模型主动提的）。"""
    from core import questions
    ws = Path(args.repo)
    rows = questions.pending(ws)
    if not rows:
        print("没有待回答的问题")
        return
    for q in rows:
        print(f"[{q.get('qid')}] {q.get('question')}")
        if q.get("options"):
            print("   可选项：" + " / ".join(q["options"]))
        if q.get("why"):
            print(f"   为什么问：{q['why']}")
        print(f"   答法：python cli.py answer {q.get('qid')} \"你的回答\" --repo {args.repo}")


def cmd_answer(args) -> None:
    """回答一个问题（答案作为 human 证据入账，同一问题以后不再问）。"""
    from core import questions
    r = questions.answer(Path(args.repo), args.qid, args.text, by="cli")
    print(("已记录：" if r.get("ok") else "失败：") + str(r.get("answer") or r.get("error")))


def cmd_selfcheck(args) -> None:
    """系统自检：建一个两函数的演示仓库 → 发一条固定需求 → 跑完整流水线 → 报数字。

    这是**排查用的**，不是演示能力用的（界面上刻意没有这个按钮）：
    它只回答一个问题——"这台机器 + 这套流程现在好不好"。
    真实水平看 projects/ 里的 DELIVERY.md 与 TRACE.md，或用 tools/bench_night.py。
    """
    from core.demo import ensure_demo

    _need("agent.run")
    db.init()
    repo = ensure_demo(reset=args.reset)
    print(f"> 演示仓库 {repo}")
    requirement = args.requirement or (
        "给 calc.py 增加一个 divide(a, b) 函数：返回 a/b，当 b 为 0 时抛 "
        'ValueError("除数不能为 0")。并补上对应的 pytest 测试，覆盖正常除法与除零两种路径。')
    if not ollama.alive():
        print("[FAIL] Ollama 不可达，请先 `ollama serve`")
        sys.exit(3)
    model = args.model or ollama.pick_default()
    try:
        from core import model_policy
        model_policy.require(model)
    except Exception as exc:                             # noqa: BLE001
        print(f"[FAIL] {exc}")
        sys.exit(4)
    ns = argparse.Namespace(repo=str(repo), requirement=requirement, target="calc.py",
                            model=model, requirement_file="", quiet=args.quiet,
                            exit_code=False)
    print(f"> 模型 {model}｜模式 {_current_mode_name()}")
    cmd_run(ns)


def _current_mode_name() -> str:
    try:
        from core import permissions
        return permissions.current_mode().name
    except Exception:                                    # noqa: BLE001
        return "?"


def cmd_status(args) -> None:
    rows = db.query("SELECT * FROM tasks WHERE id=?", (args.task_id,))
    if not rows:
        print(f"[FAIL] 任务不存在：{args.task_id}")
        sys.exit(2)
    t = rows[0]
    nodes = db.query("SELECT id,seq,kind,path,status,attempts,last_error FROM nodes WHERE task_id=? ORDER BY seq",
                     (args.task_id,))
    gates = db.query("SELECT node_id,attempt,gate,ok,evidence_kind,detail FROM gate_runs WHERE task_id=? ORDER BY id",
                     (args.task_id,))
    audit = db.audit(args.task_id)
    if args.json:
        out({"task": t, "nodes": nodes, "gates": gates, "audit": audit}, True)
        return
    print(f"任务     {t['id']}  state={t['state']}  model={t['model']}")
    print(f"需求     {t['requirement'][:100]}")
    print(f"工作区   {t['workspace']}")
    print("\n节点：")
    for n in nodes:
        print(f"  {n['id'].split('-')[-1]:<4} {n['kind']:<6} {n['path']:<22} {n['status']:<12} attempts={n['attempts']}")
        if n["last_error"]:
            print(f"       └─ {n['last_error'][:120]}")
    print("\n闸门：")
    for g in gates:
        print(f"  {str(g['node_id']).split('-')[-1]:<4} {g['gate']:<12} {'PASS' if g['ok'] else 'FAIL':<5} "
              f"evidence={g['evidence_kind']:<12} {str(g['detail'] or '').replace(chr(10),' ')[:100]}")
    print(f"\n审计     通过证据 {audit['passed']}  模型断言率 {audit['model_assertion_rate']}  "
          f"变异杀死率 {audit['mutation_kill_rate']}")


def _ws_for_task(task_id: str) -> Path:
    rows = db.query("SELECT workspace FROM tasks WHERE id=?", (task_id,))
    if not rows or not rows[0]["workspace"]:
        print(f"[FAIL] 任务无工作区：{task_id}")
        sys.exit(2)
    return Path(rows[0]["workspace"])


def cmd_diff(args) -> None:
    ws = _ws_for_task(args.task_id)
    rc, diff = tools.git(ws, "diff", args.rev or "HEAD")
    print(diff or "(无差异)")


def cmd_handoff(args) -> None:
    """生成远端接力包；--remote 时直接发给 DeepSeek 并（可选）自动应用。"""
    from core import handoff, remote
    packet = handoff.build_packet(args.task_id, str(args.node))
    md = packet["markdown"]
    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"已写入 {args.out}（{len(md)} 字符）")
    if not getattr(args, "remote", False):
        if args.json:
            out(packet["json"], True)
        else:
            print(md)
        return
    ok, why = remote.available()
    if not ok:
        print(f"远端不可用：{why}")
        sys.exit(2)
    res = remote.ask(md, args.task_id, str(args.node))
    if not res.get("ok"):
        print(f"远端调用失败：{res.get('error')}")
        sys.exit(1)
    if res.get("cached"):
        print(f"（{res.get('error')}）")
        # 命中缓存也要继续：--apply 时必须把复用到的答复落盘并过闸门，否则等于什么都没做
        if not getattr(args, "apply", False):
            return
        apply_res = handoff.apply_remote(args.task_id, str(args.node), res.get("answer") or "")
        print("---- 本地闸门 ----")
        out(apply_res, True)
        sys.exit(0 if apply_res.get("ok") else 1)
    u = res["usage"]
    print(f"用量：prompt={u['prompt_tokens']} hit={u['cache_hit_tokens']}"
          f"（{u['cache_hit_rate']:.0%}）completion={u['completion_tokens']} {res['ms']}ms")
    print("---- 远端回复 ----")
    print(res["answer"])
    if getattr(args, "apply", False):
        apply_res = handoff.apply_remote(args.task_id, str(args.node), res["answer"])
        print("---- 本地闸门 ----")
        out(apply_res, True)
        sys.exit(0 if apply_res.get("ok") else 1)


def cmd_remote(args) -> None:
    """远端模型设置与用量（api key 可随时更新；明文永不回显）。"""
    from core import db, remote, settings_store
    patch = {}
    if getattr(args, "key", None) is not None:
        patch["api_key"] = args.key
        patch["configured_at"] = db.now()
    for field, attr in (("model", "model"), ("base_url", "base_url")):
        val = getattr(args, attr, "")
        if val:
            patch[field] = val
    if getattr(args, "enable", False):
        patch["enabled"] = True
    if getattr(args, "disable", False):
        patch["enabled"] = False
    if patch:
        _cfg, changed = settings_store.update_remote(patch)
        if changed:
            print("已更新远端设置：" + "、".join(changed)
                  + ("（api key 只存本地设置文件，未回显）" if "api_key" in changed else ""))
        else:
            print("参数与当前一致，未做任何改动")
    ok, why = remote.available()
    print(json.dumps({"remote": settings_store.public_remote(), "available": ok,
                      "reason": why, "usage": remote.usage_summary(getattr(args, "task", "") or "")},
                     ensure_ascii=False, indent=2))
    u = remote.usage_summary(getattr(args, "task", "") or "")
    win = u.get("window") or {}
    print(f"\n用量（本地统计，{win.get('start', '?')}–{win.get('end', '?')} {win.get('tz', 'UTC')} 为闲时）")
    print(f"  调用 {u['calls']} 次 | prompt {u['prompt_tokens']} (命中 {u['cache_hit_tokens']}"
          f" = {u['cache_hit_rate']:.0%}) | completion {u['completion_tokens']}"
          f" | reasoning {u.get('reasoning_tokens', 0)}")
    print(f"  忙时 {u['peak']['calls']} 次 → ${u['peak']['cost_usd']:.6f}"
          f" | 闲时 {u['offpeak']['calls']} 次 → ${u['offpeak']['cost_usd']:.6f}")
    print(f"  总成本 ${u['cost_usd']:.6f} | 缓存共省下 ${u['cache_saved_usd']:.6f}"
          f" | 同证据复用 {u['cached_replies']} 次（0 token）")
    print(f"  （{u.get('pricing_note', '')}）")


def cmd_apply_remote(args) -> None:
    """应用远端模型的符号级方案，并跑本地闸门。"""
    from core import handoff
    if args.file:
        answer = Path(args.file).read_text(encoding="utf-8", errors="replace")
    else:
        answer = sys.stdin.read()
    res = handoff.apply_remote(args.task_id, str(args.node), answer)
    out(res, args.json)
    sys.exit(0 if res.get("ok") else 1)


def cmd_clean(args) -> None:
    """清理基准/测试任务（只动 projects\\ 下的测试仓库，不碰用户自己的仓库）。"""
    import shutil
    from core import db, tools as tools_mod
    roots = Path(tools_mod.__file__).resolve().parent.parent.parent  # SixCoder/
    bench = roots / "projects"
    rows = db.query("SELECT id, repo, workspace, state FROM tasks")
    targets, kept = [], 0
    for r in rows:
        repo = (r.get("repo") or "")
        # 判断"是不是实验台留下的任务"：只看路径里的 projects/ 段，不写死仓库目录名
        # （改名过两次，写死目录名会让历史任务认不出来）
        is_bench = ("\\projects\\" in repo) or ("/projects/" in repo) \
            or repo.startswith(str(bench))
        if args.benchmarks:
            if not is_bench:
                kept += 1
                continue
        elif args.task:
            if r["id"] != args.task:
                kept += 1
                continue
        else:
            kept += 1
            continue
        targets.append(r)
    if not targets:
        print(f"没有匹配的任务（保留 {kept} 个）")
        return
    if not args.yes:
        print(f"将删除 {len(targets)} 个任务（保留 {kept} 个）：")
        for r in targets[:20]:
            print(f"  {r['id']} {r['state']:<12} {(r.get('repo') or '')[-52:]}")
        print("确认请加 --yes")
        return
    removed_ws = 0
    for r in targets:
        db.delete_task(r["id"])
        ws = Path(r.get("workspace") or "")
        if ws and ws.exists() and str(ws).startswith(str(roots / "prototype" / "workspaces")):
            shutil.rmtree(ws, ignore_errors=True)
            removed_ws += 1
    if args.benchmarks and bench.exists():
        for d in bench.iterdir():
            if d.is_dir() and (d.name.startswith("exp_") or d.name.startswith("ledger_")):
                shutil.rmtree(d, ignore_errors=True)
    if args.cache:
        db.execute("DELETE FROM remote_cache")
        print("已清空远端去重缓存")
    print(f"已删除 {len(targets)} 个任务、{removed_ws} 个工作区；保留 {kept} 个任务")


def cmd_regressions(args) -> None:
    """回归用例库：远端/人工成功过的判据，以后每次都要过。"""
    from core import regression
    if args.clear:
        n = regression.clear()
        print(f"已清空 {n} 组回归用例")
        return
    if args.delete:
        print("已删除" if regression.delete(int(args.delete)) else "没有该 id")
        return
    rows = regression.listing(args.limit)
    if not rows:
        print("回归用例库为空（远端成功一次后会自动登记）")
        return
    print(f"共 {len(rows)} 组：")
    for r in rows:
        print(f"  #{r['id']:<3} {r['module']:<22} 来源={r['source']:<7} "
              f"task={r['task_id'] or '-':<20} {r['bytes']}B  {r['created_at']}")


def cmd_git(args) -> None:
    ws = Path(args.repo)
    op = args.op
    cap = GIT_OP_CAP.get(op)
    if cap:
        _need(cap)
    try:
        if op == "status":
            out(gitops.status(ws), args.json)
        elif op == "log":
            out(gitops.log(ws, args.n), args.json)
        elif op == "diff":
            out(gitops.diff(ws, args.rev, args.path, args.staged), args.json)
        elif op == "branches":
            out(gitops.branches(ws), args.json)
        elif op == "init":
            out(gitops.init(ws, args.name or "main"), args.json)
        elif op == "stage":
            out(gitops.stage_all(ws), args.json)
        elif op == "commit":
            out(gitops.snapshot(ws, args.message or "cli commit"), args.json)
        elif op == "new-branch":
            out(gitops.new_branch(ws, args.name or "", not args.no_checkout), args.json)
        elif op == "switch":
            out(gitops.switch(ws, args.name or ""), args.json)
        elif op == "merge":
            out(gitops.merge(ws, args.name or "", args.message or ""), args.json)
        elif op == "remote":
            out(gitops.set_remote(ws, args.remote or "origin", args.url or ""), args.json)
        elif op == "fetch":
            out(gitops.fetch(ws, args.remote or "origin"), args.json)
        elif op == "pull":
            out(gitops.pull(ws, args.remote or "origin", args.name or "", args.ff_only), args.json)
        elif op == "push":
            out(gitops.push(ws, args.remote or "origin", args.name or "", args.force), args.json)
        elif op == "sync":
            out(gitops.sync(ws, args.remote or "origin"), args.json)
        elif op == "undo":
            if not args.path:
                print("[FAIL] undo 需要 --path")
                sys.exit(2)
            out(gitops.undo_file(ws, args.path), args.json)
        elif op == "undo-sandbox-file":
            if not args.path:
                print("[FAIL] undo-sandbox-file 需要 --path")
                sys.exit(2)
            out(gitops.sandbox_undo_file(ws, args.path), args.json)
        elif op == "undo-sandbox":
            out(gitops.sandbox_undo(ws, args.steps), args.json)
        else:
            print(f"[FAIL] 未知 git 操作：{op}")
            sys.exit(2)
    except Exception as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}")
        sys.exit(1)


GIT_OP_CAP = {
    "init": "git.commit", "stage": "git.commit", "commit": "git.commit",
    "undo": "git.commit", "undo-sandbox": "git.commit", "undo-sandbox-file": "git.commit",
    "new-branch": "git.branch", "switch": "git.branch", "merge": "git.branch",
    "remote": "git.remote", "fetch": "git.remote", "pull": "git.remote",
    "push": "git.remote", "sync": "git.remote",
    "status": "git.read", "log": "git.read", "diff": "git.read", "branches": "git.read",
}


def _need(cap: str) -> None:
    """CLI 侧权限卡口（与服务端同一套 capability）。"""
    try:
        permissions.require(cap)
    except PermissionError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(3)


def cmd_modes(args) -> None:
    d = permissions.describe()
    if args.json:
        out(d, True)
        return
    print(f"当前模式：{d['name']}（{d['mode']}）—— {d['desc']}")
    print(f"开放能力 {len(d['capabilities'])} 项，禁用 {len(d['denied'])} 项")
    for m in d["modes"]:
        mark = "*" if m["is_current"] else " "
        print(f" {mark} {m['name']:<8} {len(m['capabilities']):>2} 项  {m['desc'][:52]}")
    print("\n能力清单：")
    for cap in d["capabilities"]:
        print(f"  + {cap:<14} {d['capability_labels'].get(cap, '')}")
    for cap in d["denied"]:
        print(f"  - {cap:<14} {d['capability_labels'].get(cap, '')}")


def cmd_review(args) -> None:
    if not permissions.allows("review.run"):
        print("[FAIL] 当前模式不允许代码审查")
        sys.exit(3)
    use_model = args.model_flag and permissions.allows("review.model")
    model = ollama.pick_default() if (use_model and ollama.alive()) else None
    r = review_mod.review(Path(args.repo), args.expectation, model, bool(model))
    if args.json:
        out(r, True)
        return
    print(f"审查 {r['files_scanned']} 个文件：P0={r['counts']['P0']} P1={r['counts']['P1']} P2={r['counts']['P2']}")
    print(f"规则分布：{r['by_rule']}")
    if r["model"].get("used"):
        print(f"模型语义比对：{r['model']['tokens']} tok / {r['model']['ms']}ms — {r['model'].get('summary','')[:60]}")
    elif r["model"].get("note"):
        print(f"模型：{r['model']['note']}")
    print()
    for f in r["findings"]:
        print(f"[{f['severity']}] {f['rule']:<18} {f['file']}:{f['line']}  {f['title']}")
        if f["expected"]:
            print(f"      预期: {f['expected'][:90]}")
            print(f"      实际: {f['actual'][:90]}")
            print(f"      建议: {f['suggestion'][:110]}")


def cmd_tool(args) -> None:
    ws = Path(args.repo)
    if not ws.exists():
        print(f"[FAIL] 目录不存在：{ws}")
        sys.exit(2)
    # 位置参数会先落到 path 再落到 args，统一还原成"剩余参数"
    rest = [x for x in ([args.path] + list(args.args)) if x]
    if args.op == "read":
        _need("file.read")
    elif args.op == "list":
        _need("file.read")
    elif args.op == "bash":
        _need("exec.run")
    elif args.op == "edit":
        _need("file.write")
    elif args.op == "git":
        _need("git.read")
    try:
        if args.op == "list":
            for f in tools.list_files(ws, (args.sub or (rest[0] if rest else "")), limit=args.limit):
                print(f)
        elif args.op == "read":
            if not rest:
                print("[FAIL] 需要文件路径：tool read <repo> <path>")
                sys.exit(2)
            print(tools.read_file(ws, rest[0], args.start, args.end))
        elif args.op == "bash":
            cmd = args.cmd or " ".join(rest)
            rc, o = tools.run_bash(ws, cmd, timeout=args.timeout)
            print(o)
            print(f"[exit={rc}]")
        elif args.op == "edit":
            if not rest:
                print("[FAIL] 需要文件路径：tool edit <repo> <path> ...")
                sys.exit(2)
            rel = rest[0]
            search = Path(args.file_search).read_text(encoding="utf-8") if args.file_search else args.search
            replace = Path(args.file_replace).read_text(encoding="utf-8") if args.file_replace else args.replace
            if not search:
                print("[FAIL] 需要 --search/--file-search 与 --replace/--file-replace")
                sys.exit(2)
            base = tools.read_file(ws, rel, 1, 1)  # 触发存在性/越界校验
            new = tools.apply_patch(ws, rel, search, replace)
            if args.apply:
                tools.write_file(ws, rel, new)
                print(f"[OK] 已写入 {rel}（已备份，可 `git undo-sandbox` 撤销）")
            else:
                print(f"[DRY] SEARCH 唯一命中（{base.splitlines()[0]}），未写入；加 --apply 生效")
        elif args.op == "git":
            rc, o = tools.git(ws, *rest)
            print(o)
            print(f"[exit={rc}]")
        else:
            print(f"[FAIL] 未知工具：{args.op}")
            sys.exit(2)
    except tools.ToolError as exc:
        print(f"[FAIL] {exc}")
        sys.exit(1)


def cmd_sandbox(args) -> None:
    ws = Path(args.repo)
    sb = tools.sandbox(ws, mode="readonly" if args.readonly else "write")
    info = sb.describe()
    info["journal"] = sb.journal()[-5:]
    out(info, args.json)


# ---------------------------------------------------------------- argparse

def build() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sixcoder", description="SixCoder CLI")
    p.add_argument("--json", action="store_true", help="机器可读输出")
    p.add_argument("--mode", choices=list(permissions.MODES), default=None,
                   help="权限模式：readonly / edit / full（默认 edit）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="环境体检：Ollama / 工具链 / 数据库").set_defaults(fn=cmd_doctor)
    sub.add_parser("modes", help="查看权限模式与能力清单").set_defaults(fn=cmd_modes)
    sp = sub.add_parser("selfcheck", help="系统自检：跑一个最小的完整任务，判断这套流程当前好不好")
    sp.add_argument("--reset", action="store_true", help="重建演示仓库（默认沿用）")
    sp.add_argument("--model", default="")
    sp.add_argument("--requirement", default="", help="默认用内置的 divide 需求")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_selfcheck)

    sp = sub.add_parser("review", help="代码审查：改进意见 + 与预期功能不符之处")
    sp.add_argument("repo")
    sp.add_argument("--expectation", default="", help="预期功能（用于找实现与预期不符）")
    sp.add_argument("--no-model", dest="model_flag", action="store_false", default=True,
                    help="只跑确定性规则，不调模型")
    sp.set_defaults(fn=cmd_review)

    sp = sub.add_parser("probe", help="仓库形态识别")
    sp.add_argument("repo")
    sp.set_defaults(fn=cmd_probe)

    sp = sub.add_parser("run", help="跑一个完整任务（探测→计划→补丁→闸门→审计）")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--requirement", default="", help="需求文本（短需求）")
    sp.add_argument("--requirement-file", default="", help="需求文件路径（长需求/含引号时用这个）")
    sp.add_argument("--target", default="")
    sp.add_argument("--model", default="")
    sp.add_argument("--quiet", action="store_true", help="不打印过程事件")
    sp.add_argument("--exit-code", action="store_true", help="非 done 时以 1 退出（便于 CI）")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("night", help="夜班：提需求→自动反复体检/派工/开发→早上取交付报告")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--requirement", default="", help="需求文本（短需求）")
    sp.add_argument("--requirement-file", default="", help="需求文件路径（长需求用这个）")
    sp.add_argument("--goal", default="",
                    help="一句话目标（全自主模式）：切片、验收对象、预算分配全部由系统自己规划")
    sp.add_argument("--max-slices", type=int, default=0,
                    help="全自主模式：最多做几个切片（0 = 不限，直到覆盖完成/钱用完）")
    sp.add_argument("--slice-hours", type=float, default=0.0,
                    help="全自主模式：单个切片的时间预算（默认 0.5h）")
    sp.add_argument("--fresh", action="store_true",
                    help="全自主模式：忽略已有蓝图重新推导（默认续跑，已通过的能力不重做）")
    sp.add_argument("--spec-sample", default="",
                    help="验收样例文件（人给的权威判据）：怎么跑、输入什么、该看到什么；"
                         "每条样例变成一条能力，模型不再猜规格")
    sp.add_argument("--approve-plan", action="store_true",
                    help="全自主模式：先打印一页蓝图等人点头再开跑（第一次用推荐）")
    sp.add_argument("--ask", action="store_true",
                    help="阶段 1：先让远端就架构安排详细提问（强制重问；有问题缓存就不重复花钱）")
    sp.add_argument("--answers", default="",
                    help="阶段 1 的答案文件（JSON 字典或 `Q1: ...` 逐行）；给了就不再问人，直接开跑")
    sp.add_argument("--interview", action="store_true",
                    help="阶段 1：在终端逐条交互答完再开跑（人不答则用建议默认）")
    sp.add_argument("--clarify", action="store_true",
                    help="阶段①澄清循环：远端最强模型列全量待定项+每项多选，落进同一份设计文档"
                         "（越改越细），人类选完、AI 判足够才进生成；结果转 arch 喂给夜班")
    sp.add_argument("--no-selfcheck", action="store_true",
                    help="关掉片内自验（作者不能自己跑验收命令，只能事后判红整片重来；"
                         "只在做对照实验时用，平时别关）")
    sp.add_argument("--plan", default="", help="实现方案（开发者模式下是权威，不许系统改架构）")
    sp.add_argument("--plan-file", default="", help="实现方案文件路径")
    sp.add_argument("--allow-no-plan", action="store_true",
                    help="开发者模式下没有方案也允许跑（默认不允许：架构决策归人）")
    sp.add_argument("--hours", type=float, default=0.0,
                    help="预算小时数（默认按代码规模：S 0.5 / M 2 / L 6 / XL 12）")
    sp.add_argument("--max-rounds", type=int, default=40)
    sp.add_argument("--model", default="")
    sp.add_argument("--scale", default="", choices=["", "S", "M", "L", "XL", "s", "m", "l", "xl"],
                    help="目标代码规模（S/M/L/XL）。不写就在需求里写『代码规模：M』，"
                         "再没有就按仓库实测推断并在报告里注明")
    sp.add_argument("--max-cny", type=float, default=0.0,
                    help="花费硬上限（人民币）；超过即收工。0 = 不设上限")
    sp.add_argument("--remote-model", default="",
                    help="只走这个远端模型档案（flash / pro）；不写 = 整条八档阶梯")
    sp.add_argument("--no-remote", action="store_true", help="不用远端（纯本地，质量会明显下降）")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_night)

    sp = sub.add_parser("plan", help="自主规划：看蓝图/切片/覆盖/单条能力溯源（只读，不花 token）")
    sp.add_argument("what", nargs="?", default="show",
                    choices=["show", "slices", "cover", "trace", "ask"],
                    help="show=蓝图一页；slices=下一刀切在哪；cover=真跑探针看做完没有；"
                         "trace=某条能力的来龙去脉；ask=只让远端就架构提问（阶段 1）")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--cap", default="", help="trace 用：能力 id（如 cap.cli.count）")
    sp.add_argument("--goal", default="", help="ask 用：一句话目标（提问的依据）")
    sp.add_argument("--answers", default="", help="ask 用：答案文件；给了就直接把安排定下来")
    sp.set_defaults(fn=cmd_plan)

    sp = sub.add_parser("inspect", help="定期巡检：缓存命中率趋势 + 文档健康（只读）")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--scale", default="", choices=["", "S", "M", "L", "XL"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_inspect_docs)

    sp = sub.add_parser("questions", help="列出待人回答的问题（开发者模式的主动提问）")
    sp.add_argument("--repo", required=True, help="工作区目录")
    sp.set_defaults(fn=cmd_questions)

    sp = sub.add_parser("answer", help="回答一个问题")
    sp.add_argument("qid")
    sp.add_argument("text")
    sp.add_argument("--repo", required=True)
    sp.set_defaults(fn=cmd_answer)

    sp = sub.add_parser("status", help="查看任务、节点与闸门")
    sp.add_argument("task_id")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("diff", help="查看任务工作区的 Git 差异")
    sp.add_argument("task_id")
    sp.add_argument("--rev", default="")
    sp.set_defaults(fn=cmd_diff)

    sp = sub.add_parser("handoff", help="生成远端接力包（粘贴给云端模型：它出方案，本地实现+验证）")
    sp.add_argument("task_id")
    sp.add_argument("--node", required=True, help="节点 id（status 命令里能看到）")
    sp.add_argument("--out", default="", help="同时写入文件")
    sp.add_argument("--json", action="store_true", help="输出结构化 JSON")
    sp.add_argument("--remote", action="store_true", help="直接发给 DeepSeek（需已配置 api key）")
    sp.add_argument("--apply", action="store_true", help="拿到远端回复后立即落盘并跑本地闸门")
    sp.set_defaults(fn=cmd_handoff)

    sp = sub.add_parser("remote", help="远端模型设置（api key 可随时更新）与 token 用量")
    sp.add_argument("--key", default=None, help="设置 DeepSeek api key（只存本地，不回显）")
    sp.add_argument("--model", default="", help="模型名，默认 deepseek-chat")
    sp.add_argument("--base-url", default="", help="自定义 base url")
    sp.add_argument("--enable", action="store_true")
    sp.add_argument("--disable", action="store_true")
    sp.add_argument("--task", default="", help="只看某个任务的用量")
    sp.set_defaults(fn=cmd_remote)

    sp = sub.add_parser("apply-remote", help="应用远端模型返回的符号级方案并跑本地闸门")
    sp.add_argument("task_id")
    sp.add_argument("--node", required=True)
    sp.add_argument("--file", default="", help="远端回复文件；省略则从 stdin 读")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_apply_remote)

    sp = sub.add_parser("clean", help="清理基准/测试任务（只清 projects\\ 下的测试仓库）")
    sp.add_argument("--benchmarks", action="store_true", help="删除所有测试任务与 projects/exp_*、ledger_*")
    sp.add_argument("--task", default="", help="只删某个任务 id")
    sp.add_argument("--cache", action="store_true", help="同时清空远端去重缓存")
    sp.add_argument("--yes", action="store_true", help="确认执行（不带只预览）")
    sp.set_defaults(fn=cmd_clean)

    sp = sub.add_parser("regressions", help="回归用例库（远端成功过的判据，以后每次都要过）")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--clear", action="store_true", help="清空全部")
    sp.add_argument("--delete", default="", help="按 id 删除")
    sp.set_defaults(fn=cmd_regressions)

    sp = sub.add_parser("git", help="Git 客户端能力（人类通道）：init/commit/pull/push/sync/merge/...")
    sp.add_argument("op", choices=["status", "log", "diff", "branches", "init", "stage", "commit",
                                   "new-branch", "switch", "merge", "remote", "fetch", "pull",
                                   "push", "sync", "undo", "undo-sandbox", "undo-sandbox-file"])
    sp.add_argument("repo")
    sp.add_argument("--path", default="")
    sp.add_argument("--rev", default="")
    sp.add_argument("--message", default="")
    sp.add_argument("--name", default="")
    sp.add_argument("--remote", default="origin")
    sp.add_argument("--url", default="")
    sp.add_argument("--n", type=int, default=12)
    sp.add_argument("--steps", type=int, default=1)
    sp.add_argument("--staged", action="store_true")
    sp.add_argument("--ff-only", action="store_true")
    sp.add_argument("--force", action="store_true", help="推送用 force-with-lease（会单独记审计）")
    sp.add_argument("--no-checkout", action="store_true")
    sp.set_defaults(fn=cmd_git)

    sp = sub.add_parser("tool", help="直接调用 Agent 的工具（sandbox 生效）")
    sp.add_argument("op", choices=["list", "read", "edit", "bash", "git"])
    sp.add_argument("repo")
    sp.add_argument("path", nargs="?", default="")
    sp.add_argument("args", nargs="*", default=[])
    sp.add_argument("--sub", default="")
    sp.add_argument("--limit", type=int, default=200)
    sp.add_argument("--start", type=int, default=None)
    sp.add_argument("--end", type=int, default=None)
    sp.add_argument("--cmd", default="")
    sp.add_argument("--timeout", type=int, default=180)
    sp.add_argument("--search", default="")
    sp.add_argument("--replace", default="")
    sp.add_argument("--file-search", default="")
    sp.add_argument("--file-replace", default="")
    sp.add_argument("--apply", action="store_true")
    sp.set_defaults(fn=cmd_tool)

    sp = sub.add_parser("sandbox", help="查看沙箱策略与写盘日志")
    sp.add_argument("repo")
    sp.add_argument("--readonly", action="store_true")
    sp.set_defaults(fn=cmd_sandbox)
    return p


def main() -> None:
    args = build().parse_args()
    if getattr(args, "mode", None):
        permissions.set_mode(args.mode)
    args.fn(args)


if __name__ == "__main__":
    main()
