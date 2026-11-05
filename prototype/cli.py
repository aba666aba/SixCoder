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
            "passed", "model_assertion_rate", "mutation_kill_rate", "nodes_ok", "nodes_total")
    parts = [f"{k}={payload[k]}" for k in keep if k in payload]
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
    requirement = args.requirement or ""
    if getattr(args, "requirement_file", ""):
        requirement = Path(args.requirement_file).read_text(encoding="utf-8-sig")
    if not requirement.strip():
        print("[FAIL] 需要 --requirement 或 --requirement-file")
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

    def printer(kind, payload) -> None:
        if kind in ("round", "strategy", "architect", "escalate", "rescue", "scale",
                    "governance", "workingset", "protected_zone", "structure_gate",
                    "output_budget", "write_cap", "stop",
                    "order_failed", "needhuman", "dictfill", "autofix"):
            print(f"   {kind:<12} {_slim(payload or {})[:140]}", flush=True)

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
