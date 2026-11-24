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
import os
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

from core import agent, db, gitops, ollama, permissions, probe, recon, review as review_mod, tools  # noqa: E402


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
            "questions", "answered", "samples", "ids",
            # 远端能力闸门的可观测字段（2026-11-09）：read_ok / max_out 这些**不在白名单**
            # → 日志里只剩 path=，"远端到底满不满血"根本看不见（第九次同一种病：
            # 机制写好了，某条出口没接上）。这几个字段是判断"上下文给全没有、
            # 配额够不够"的唯一证据，必须打出来。
            "read_ok", "mode", "tiers", "chars", "max_out", "est_tokens", "basis",
            "grew_from", "tools", "rounds", "by", "applied", "line_patch",
            # 回归守护的可观测字段（2026-11-10 23:10）：不在白名单 → 日志只剩 why=，
            # "这次到底守没守、基线几条、拦了几条"全靠猜（当晚就猜错一次）。
            "guarded_n", "base_n", "guard_note",
            "first_pass_rate", "mod_total", "mod_first_pass",
            "prompt_tokens", "completion_tokens", "reasoning_tokens", "used", "truncated",
            # 失败证据：bodies_feedback / decision 的"为什么错"（原来只剩 path+attempt，
            # 失败原因在日志里看不见 → 只能靠猜。跟 read_ok 同一类病：出口没接上）
            "evidence", "only", "findings", "precision", "targets", "action", "level")
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
    items = recon.toolchains()
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
    info = recon.classify(Path(args.repo))
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


def _run_estimate(repo, text: str, model: str) -> None:
    """两步法第一步：构建蓝图 + 估算花费（不跑夜班），把「预计需要多少钱」展示给用户充值。

    单价（谷/峰）与系统预算都来自 .sixcoder/budget.json（用户/运维填写），引擎不编造价格。
    """
    if not text.strip():
        print("[FAIL] 估算需要 --goal 或 --requirement")
        sys.exit(2)
    from core import blueprint as _bp
    from core import budget_estimate as _be
    print("> 估算模式：构建蓝图并估算花费（不实际开工）")
    got = _bp.derive(text, llm=_bp._default_llm, ws=repo)
    if not got.get("ok"):
        print(f"[FAIL] 蓝图推导失败：{got.get('problems')}")
        sys.exit(2)
    bp = got["blueprint"]
    try:
        _bp.save(bp, repo)
    except Exception:                                     # noqa: BLE001
        pass
    est = _be.estimate_from_blueprint(repo)
    if not est.get("ok"):
        print(f"[WARN] 估算不可用：{est.get('reason')}")
        return
    print(_be.format_report(est))
    snap = _be.budget_snapshot(repo)
    if snap.get("has_stop_line"):
        print(f"> 系统预算 ¥{snap['system_budget_cny']:.2f}；总花费超过 ¥{snap['stop_threshold_cny']:.2f} 自动收工。")
    else:
        print("> 未设系统预算（.sixcoder/budget.json 无 system_budget_cny）→ 不启停机线。")
    print("> 开工前把对应金额充入 API 账户；填好 .sixcoder/budget.json 的单价字段可看到金额估算。")


def cmd_ui_watch(args) -> None:
    """界面监督：按轮跑 D7，记台账，按"连续失败"给判定（开发文档 §13.10）。"""
    from core import blueprint as _bp
    from core import uiwatch as _uw

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"> 仓库不存在：{repo}")
        return
    if getattr(args, "history", False):
        print(_uw.render_history(repo, limit=int(getattr(args, "limit", 20) or 20)))
        return
    base = str(getattr(args, "ui_base", "") or "")
    if not base:
        print("> 缺 --ui-base（前端服务地址，如 http://127.0.0.1:5173）")
        print("> 只想看历史台账：加 --history")
        return
    b = _bp.load(repo)
    only = None
    raw = str(getattr(args, "only", "") or "").strip()
    if raw:
        only = [x.strip() for x in raw.split(",") if x.strip()]

    def printer(kind, payload) -> None:
        if kind == "ui_round":
            print("   round %(round)d  %(caps)d 条探针  失败 %(fail)d  %(ms)dms"
                  % payload, flush=True)
        elif kind == "notice":
            print("   注意：%s" % payload.get("message", ""), flush=True)

    rounds = int(getattr(args, "rounds", 1) or 1)
    interval = int(getattr(args, "interval", 0) or 0)
    print(f"> 界面监督  repo={repo}  base={base}  轮数={rounds}  间隔={interval}s")
    v = _uw.watch(repo, b, base_url=base, rounds=rounds, interval_s=interval,
                  only=only, emit=printer)
    print()
    print("> 判定 %s：%s" % (v.get("action"), v.get("why")))
    if not rounds or v.get("rounds"):
        print("> 本轮 %s 轮，覆盖 %s 条探针" % (v.get("rounds"), v.get("caps")))
    if v.get("median_ms"):
        print("> 耗时 本轮 %sms，历史中位数 %sms%s"
              % (v.get("cur_ms"), v.get("median_ms"), "（偏慢）" if v.get("slow") else ""))
    print()
    print(_uw.render_history(repo, limit=10))


def cmd_full(args) -> None:
    """全量生成 + 错误报告流程（开发文档 §13）。

    默认**只出报告不改代码**（安全）；要它动手修加 `--repair`。
    """
    from core import fullgen

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"> 仓库不存在：{repo}")
        return
    detect_only = not bool(getattr(args, "repair", False))
    print(f"> 仓库 {repo}  模式={'仅检测（出报告）' if detect_only else '检测+修复循环'}")
    print(f"> 全量检测 D1–D6（D6 端到端默认跳过，加 --e2e 才跑，很慢）")

    def printer(kind, payload) -> None:
        if kind == "full_round":
            _ch = payload.get("cache_hit", -1)
            _chtxt = ("  缓存命中 %.0f%%" % (_ch * 100)) if isinstance(_ch, (int, float)) and _ch >= 0 else ""
            print("   round %(round)d  E=%(errors)d（P0=%(p0)d P1=%(p1)d）"
                  "  done=%(done)s/%(total)s  → %(action)s" % payload + _chtxt, flush=True)
            if payload.get("why"):
                print(f"      {payload['why']}", flush=True)
        elif kind == "full_repair":
            print(f"   repair  {payload['file']}  错误 {payload['errors']} 条", flush=True)
        elif kind == "full_switch":
            print(f"   换策略：{payload.get('why','')}", flush=True)
        elif kind == "full_mode":
            print("   场景：%s（%d 个业务模块）"
                  % ("空项目→从零写" if payload.get("mode") == "scratch"
                     else "已有仓库→分析优化", payload.get("modules", 0)), flush=True)
        elif kind == "full_domain_done":
            print("   域 %s %s（%d 字符）" % (payload.get("domain"),
                                          "✓" if payload.get("ok") else "✗",
                                          payload.get("chars", 0)), flush=True)
        elif kind == "full_domain_start":
            print("   域 %s（%d 条能力）生成中…" % (payload.get("domain"),
                                                  payload.get("caps") or 0), flush=True)
        elif kind == "detector" and payload.get("name") == "d7":
            print("   d7 界面: %d 条" % payload.get("errors", 0), flush=True)
        elif kind == "detector":
            print(f"   {payload['name']}: {payload['errors']} 条", flush=True)
        elif kind == "full_layer":
            print(f"   生成层 {payload['layer']}（{payload['files']} 文件）", flush=True)
        elif kind == "notice":
            print(f"   注意：{payload.get('message','')}", flush=True)

    if getattr(args, "gen_contract_only", False):
        from core import blueprint as _bp
        print("> 只生成全项目契约（分域）。这一步慢，跑完就锁定了。")
        b = _bp.load(repo)
        g = fullgen.generate_contract_by_domain(
            repo, b, emit=printer, max_domains=int(getattr(args, "max_domains", 0) or 0))
        print()
        print("> ok=%s 域数=%s 函数=%s 端点=%s 字符=%s"
              % (g.get("ok"), len(g.get("domains") or []), g.get("funcs"),
                 g.get("routes"), g.get("chars")))
        for d in (g.get("domains") or []):
            print("   %-10s %s  %d 条能力  %d 字符 %s"
                  % (d.get("name"), "✓" if d.get("ok") else "✗",
                     d.get("caps") or 0, d.get("chars") or 0,
                     ("问题：" + str(d.get("problems"))) if not d.get("ok") else ""))
        print("> 锁定文件 %s" % g.get("path"))
        if not g.get("ok"):
            print("> 失败原因：%s" % (g.get("problems") or []))
        return

    r = fullgen.run(repo, emit=printer, max_rounds=int(args.max_rounds or 6),
                    detect_only=detect_only,
                    from_scratch=bool(getattr(args, "from_scratch", False)),
                    max_files=int(getattr(args, "max_files", 6) or 6),
                    per_file=bool(getattr(args, "per_file", False)),
                    refresh_contract=bool(getattr(args, "refresh_contract", False)),
                    mode=str(getattr(args, "gen_mode", "auto") or "auto"),
                    ui_base=str(getattr(args, "ui_base", "") or ""),
                    only_e2e=None if getattr(args, "e2e", False) else None)
    print()
    print("> 结果：" + ("收敛/可继续" if r.get("ok") else "已停（见下）"))
    print(f"> 场景 {r.get('mode','?')}  轮数 {r['rounds']}  耗时 {r['elapsed_s']}s")
    st = r.get("stats") or {}
    if st:
        print(f"> 错误 {st.get('total')} 条（P0={st.get('P0')} P1={st.get('P1')} "
              f"P2={st.get('P2')} P3={st.get('P3')}）")
    v = r.get("verdict") or {}
    if v:
        print(f"> 判据 {v.get('action')}：{v.get('why')}")
    print(f"> 报告 {r.get('report')}")


# 收敛判据叫人（振荡/停滞/预算/远端配置）时的退出码：watchdog 见到它必须**真的停下**，
# 不许当 rc=0 的"正常跑完"续跑（2026-11-16 实测空转烧 ¥5）。
# 与 tools/watchdog_night.py 的 HUMAN_STOP_RC 同值（check_watchdog_schedule.py 钉一致）。
HUMAN_STOP_RC = 42


def _night_finish_sweep(repo: str, quiet: bool = False) -> None:
    """夜班**收尾**清理 + 归档（2026-11-19 用户口径）。

    用户定稿："保存到引擎本地作为备用，不是有删除仓库记录功能，如果删除就顺便
    删除备份的临时目录。" 所以收尾分两步，顺序不能反：

      ① **归档**（core/scratch_store）：产物仓 `.sixcoder/tmp` 里**有内容**的
         引擎临时项 **move** 到引擎本地 `prototype/data/scratch/<仓库指纹>/`，
         留作排查备用；空目录壳直接删（实测 2200+ 个空壳，搬走没意义）。
      ② **清理**（probe.sweep_on_finish）：把剩下的引擎命名空间超龄项、
         `__pycache__`、`.gc-*` 批次残留、引擎自身 `.tmp` 一次性收干净。

    删除任务/仓库记录时由 server.py 调 `scratch_store.purge_repo()` 清掉归档
    （记录都没了，归档就成了孤儿）。

    覆盖所有收工路径（正常跑完 / 振荡收工 / 叫人 / 预算停 / 异常退出），
    因为调用方把它放在 finally 里。硬约束：任何失败都不许影响收工结果。
    """
    # ① 归档：有内容的搬到引擎本地（空壳交给 ② 的 gc_scratch 用 rename 批处理删）
    try:
        from core import scratch_store as _ss
        ar = _ss.archive_scratch(repo)
        if not quiet:
            if ar.get("moved"):
                print(f"  临时区归档：搬 {ar['moved']} 项到引擎本地"
                      f"（{round((ar.get('moved_bytes') or 0) / 1048576, 1)} MB，备用）")
            for e in (ar.get("errors") or [])[:3]:
                print(f"  [WARN] 归档跳过一项：{e}")
    except BaseException:                              # noqa: BLE001
        pass

    # ② 清理：剩下的超龄项 + 引擎自身 .tmp
    try:
        from core import probe as _probe_fin
        sw = _probe_fin.sweep_on_finish(repo)
        if (sw.get("removed") or 0) and not quiet:
            print(f"  收尾清理：删除 {sw['removed']} 条临时项，"
                  f"释放 {round((sw.get('freed_bytes') or 0) / 1048576, 1)} MB")
        for e in (sw.get("errors") or [])[:3]:
            if not quiet:
                print(f"  [WARN] 收尾清理跳过一项：{e}")
    except BaseException:                              # noqa: BLE001
        pass                                           # 清理失败不许改退出码


def cmd_night(args) -> None:
    """夜班（公司制）：造物模式提需求 → 内部做完所有决策 → 早上读交付报告。

    与 `run` 的区别：run 是"一个需求 → 一对测试/实现节点"；night 是"反复体检/派工/开发/
    机械验收，直到收敛或预算耗尽"，并写出 DELIVERY.md / TRACE.md。
    模式决定协作方式（core/policy.py）：造物模式由远端拍板、本地判红即远端接管写代码；
    精修模式必须给 --plan（或 --plan-file），没有方案就抛 needhuman、不替人做架构决策。
    """
    _need("agent.run")
    from core import company, policy

    # 加载生态包（协商补全的前端 SPA / 桌面 / 中间件等栈适配器），注册进栈表。
    # 失败不阻断夜班（生态包是增量能力，坏了退回原有两栈）。
    try:
        from core import ecosystem as _eco
        _loaded = _eco.load_all()
        if _loaded.get("loaded"):
            print("[INFO] 已加载生态包：%s" % ", ".join(_loaded["loaded"]))
        for _f in _loaded.get("failed") or []:
            print("[WARN] 生态包加载失败：%s（%s）" % (_f.get("name"), _f.get("why")))
    except Exception as _exc:                            # noqa: BLE001
        print("[WARN] 生态包加载异常（继续）：%s" % _exc)

    pol = policy.current()
    if pol.code_escalation == policy.OFF:
        print(f"[FAIL] {pol.name}不写文件，不能跑夜班；请切到造物模式或精修模式")
        sys.exit(2)
    db.init()
    goal = str(getattr(args, "goal", "") or "").strip()
    _goal_file = str(getattr(args, "goal_file", "") or "").strip()
    if _goal_file:
        if goal:
            print("[FAIL] --goal 与 --goal-file 只能用一个")
            sys.exit(2)
        try:
            goal = Path(_goal_file).read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            print(f"[FAIL] 读不了目标文件：{exc}")
            sys.exit(2)
        if not goal:
            print("[FAIL] 目标文件是空的")
            sys.exit(2)
        print(f"> 目标文件：{_goal_file}（{len(goal)} 字符）")
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
        print("[FAIL] 精修模式需要实现方案：用 --plan / --plan-file 给出"
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

    # 两步法第一步：只构建蓝图 + 估算花费，不实际开工（供用户充值参考）
    if getattr(args, "estimate", False):
        _run_estimate(repo, (goal or requirement), model)
        sys.exit(0)

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
        print("> 预算机制：无用户上限；唯一停机线 = 系统总预算 ×1.05（见 .sixcoder/budget.json）")
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
        # **新路径（唯一）**：判型 → 供料 → 整仓施工 → 全量验证（core/night.py）
        # 旧路径（night_auto：切片 + 每片契约 + 片预算）已冻结，只在"人参与设计"
        # 的那几个参数（--clarify/--ask/--interview/验收样例）出现时才回落，
        # 因为那属于另一个模式，新路径不承担架构问答。
        _need_legacy = bool(
            getattr(args, "clarify", False) or getattr(args, "ask", False)
            or getattr(args, "answers", "") or getattr(args, "interview", False)
            or getattr(args, "spec_sample", ""))
        # 应急逃生口：新路径若当晚出问题，设 SIXCODER_NIGHT_LEGACY=1 即可回到旧路径，
        # 不用改代码（改代码要重启看门狗，深夜里代价更大）。
        if os.environ.get("SIXCODER_NIGHT_LEGACY", "") not in ("", "0", "false", "False"):
            _need_legacy = True
            print("[NOTE] SIXCODER_NIGHT_LEGACY=1 → 强制走旧路径（切片式 night_auto）")
        if _need_legacy:
            print("[NOTE] 检测到架构问答/验收样例参数 → 走旧路径（night_auto，切片式）；"
                  "纯夜班请只给 --repo/--goal-file，走整仓路径")
            res = _auto.night_auto(repo, goal, model=model,
                                   max_slices=getattr(args, "max_slices", 0) or 0,
                                   max_rounds=args.max_rounds,
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

        from core import night as _night_mod
        try:
            res = _night_mod.run(repo, goal, model=model,
                                 max_rounds=args.max_rounds,
                                 fresh=getattr(args, "fresh", False),
                                 emit=None if args.quiet else printer,
                                 remote_profile=getattr(args, "remote_model", "") or "",
                                 plan=plan,
                                 refresh_contract=bool(getattr(args, "refresh_contract", False)))
        finally:
            # 同一套收尾清理（这条是 fullgen scratch/repair 的夜班路径，也要清）
            _night_finish_sweep(repo, quiet=bool(args.quiet))
        print(f"\n■ 夜班结束（{getattr(res, 'mode', '?')}）："
              f"{res.verified}/{res.total} 验收通过｜{res.spent_s:.0f}s｜{res.reason}")
        if getattr(res, "delivery_path", ""):
            print(f"  交付报告 {res.delivery_path}")
        _human = str(getattr(res, "need_human", "") or "")
        if _human:
            print(f"  [叫人] {_human}（退出码 {HUMAN_STOP_RC}：看门狗不会续跑）")
            sys.exit(HUMAN_STOP_RC)
        sys.exit(0 if res.ok else 1)

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
    print(f"> 仓库 {repo}  model={model}")
    if plan.strip():
        print("> 实现方案（权威）已注入：", plan.strip().splitlines()[0][:60])

    try:
        res = company.night(repo, requirement, model,
                            use_remote=not args.no_remote,
                            max_rounds=args.max_rounds,
                            emit=None if args.quiet else printer, plan=plan,
                            remote_profile=getattr(args, "remote_model", "") or "",
                            scale_level=(scope.get("level") if args.scale else ""))
    finally:
        # 收尾清理放 finally：**所有**收工路径都清（正常/振荡/叫人/预算停/异常）。
        # 否则"这一夜留下的垃圾"只能等下一次开跑才被收（实测堆到 2583 条 / 8.9MB）。
        _night_finish_sweep(repo, quiet=bool(args.quiet))
    print(f"\n■ 夜班结束：{res.verified}/{res.total} 验收通过｜{res.spent_s:.0f}s｜{res.reason}")
    if res.delivery_path:
        print(f"  交付报告 {res.delivery_path}")
    _human = str(getattr(res, "need_human", "") or "")
    if _human:
        print(f"  [叫人] {_human}（退出码 {HUMAN_STOP_RC}：看门狗不会续跑）")
        sys.exit(HUMAN_STOP_RC)
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
              '`sixcoder night --repo <目录> --goal "一句话目标" --estimate` 看估算，'
              '再去掉 --estimate 正式开工')
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


def cmd_model(args) -> None:
    """领域模型：一份 model.json + 四张图（ER / 状态 / 时序 / 组件）。只读、零花费。

    为什么要这一层（2026-11-08）：流水线原来是"需求 → 能力清单 → 切片 → 代码"，
    中间没有模型，于是每一处改都只改自己那一小块 —— 没有"整体"可以对照，
    改一个字段不知道会影响谁。模型层把实体/状态/主干/组件固化成唯一真相，
    代码校验和文档图都从它出，才不会一处改完全盘散。

    只建四种关系（用户明确"只构建需要的关系即可"）：用例/DFD/甘特/WBS 那些
    要么是本层四张图的另一种画法，要么是项目管理产物，不另建真相。
    """
    _need("agent.run")
    from core import blueprint as _bp
    from core import model as _md
    ws = Path(args.repo)
    bp = _bp.load(ws)
    m = _md.ensure(bp, ws, write=args.write)
    if args.enrich:
        from core import remote as _rm
        print("语义补全（远端只补状态转换与不变式，事实由引擎校验）…")
        got = _md.enrich(m, _rm.complete, bp.goal or "")
        if got.get("ok"):
            print(f"  采纳 {got.get('applied', 0)} 条转换；丢弃 {len(got.get('dropped') or [])} 条")
            for d in (got.get("dropped") or [])[:6]:
                print(f"    丢弃：{d}")
        else:
            print(f"  跳过：{got.get('error')}")
    probs = _md.validate(m)
    # 缺口：先自动消解能消解的（图论源点 / 入度排序），剩下的如实列出来
    left = _md.resolve_gaps(m, bp)

    print(f"仓库 {ws}")
    print(f"实体 {len(m.entities)}｜主干步数 {sum(len(f.steps) for f in m.flows)}"
          f"｜组件 {len(m.components)}｜模型自洽问题 {len(probs)}")
    print()
    for e in m.entities:
        fk = [f"{f.name}→{f.fk}" for f in e.fields if f.fk]
        st = f" 状态 {','.join(e.statuses)}（初始 {e.initial}）" if e.statuses else ""
        print(f"  {e.name:16s} {len(e.fields):2d} 字段"
              + (f" 外键 {','.join(fk)}" if fk else "") + st)
    for fl in m.flows:
        print(f"\n  主干流 {fl.name}（{len(fl.steps)} 步）：")
        for i, s in enumerate(fl.steps, 1):
            print(f"    {i:2d}. {s['method']:5s} {s['path']}")
    if probs:
        print("\n  模型问题：")
        for p in probs[:10]:
            print(f"    - {p}")
    if left:
        print(f"\n  缺口 {len(left)} 项（引擎不猜，未消解的如实列出）：")
        for g in left[:12]:
            who = f"{g.entity}: " if g.entity else ""
            print(f"    [{g.kind}] {who}{g.detail}")
            print(f"        消解方式：{g.how}")
    else:
        print("\n  缺口 0 项（能机械消解的都消解了）")

    # 不变式 → 可执行探针（模板化；造不出真实请求体就不造，绝不编字段）
    inv_caps = _md.invariant_probes(m, bp)
    if inv_caps:
        print(f"\n  可从不变式生成的负向探针 {len(inv_caps)} 条：")
        for ic in inv_caps:
            print(f"    {ic['cap_id']}（{ic['kind']}，{len(ic['steps'])} 步）{ic['desc'][:60]}")
        if args.apply_invariants:
            from core import blueprint as _bp2
            added = 0
            for ic in inv_caps:
                if bp.by_id(ic["cap_id"]):
                    continue
                bp.add(_bp2.Cap(id=ic["cap_id"], desc=ic["desc"], funcs_est=3,
                                probe={"kind": "http", "steps": ic["steps"]}))
                added += 1
            if added:
                _bp2.save(bp, ws)
                print(f"    已写入蓝图 {added} 条（下一轮切片会带上）")
        else:
            print("    （加 --apply-invariants 写进蓝图）")
    if m.notes:
        print("\n  说明：")
        for n in m.notes[:6]:
            print(f"    - {n}")

    if args.write:
        p = _md.save(m, ws)
        print(f"\n已写 {p}")
        d = ws / ".sixcoder" / "diagrams"
        d.mkdir(parents=True, exist_ok=True)
        for name, txt in _md.to_mermaid(m).items():
            (d / f"{name}.mmd").write_text(txt, encoding="utf-8")
            print(f"已写 {d / (name + '.mmd')}")
    else:
        print("\n（审阅模式；加 --write 写入 .sixcoder/model.json 与 diagrams/*.mmd）")


def cmd_contract(args) -> None:
    """契约反推：读现有代码 → 结构化契约（存量仓库的入场券，只读、零花费）。

    产物与需求契约同构（同一个 Contract/FuncSig），闸门/判据/骨架可直接吃；
    只提取代码里真写出的事实（签名/注解/raise/返回 dict 字面量/doctest），不猜。
    """
    from core import contract_reverse
    ws = Path(args.repo)
    if not ws.is_dir():
        print(f"[FAIL] 目录不存在：{ws}")
        sys.exit(2)
    paths = [args.path] if args.path else None
    cons, issues = contract_reverse.from_code(ws, paths, include_private=args.private)
    if not cons:
        print("没有可反推的模块（全是测试/配置/忽略目录，或没有 .py 文件）")
        for it in issues[:5]:
            print(f"  - {it}")
        return
    print(contract_reverse.render(cons))
    if issues:
        print(f"\n【如实报告的跳过项 {len(issues)} 条】")
        for it in issues[:8]:
            print(f"  - {it}")


def cmd_questions(args) -> None:
    """列出待人回答的问题（精修模式里本地模型主动提的）。"""
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
    sp.add_argument("--goal-file", default="",
                    help="目标文本文件路径（长需求用这个，内容整份作为 goal；与 --goal 二选一）")
    sp.add_argument("--max-slices", type=int, default=0,
                    help="全自主模式：最多做几个切片（0 = 不限，直到覆盖完成）")
    sp.add_argument("--estimate", action="store_true",
                    help="两步法第一步：只构建蓝图 + 估算花费（不跑夜班），供充值参考")
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
    sp.add_argument("--refresh-contract", action="store_true",
                    help="重新生成全量契约并覆盖锁定文件（**缓存全部失效**，只在需求变了时用）")
    sp.add_argument("--plan", default="", help="实现方案（精修模式下是权威，不许系统改架构）")
    sp.add_argument("--plan-file", default="", help="实现方案文件路径")
    sp.add_argument("--allow-no-plan", action="store_true",
                    help="精修模式下没有方案也允许跑（默认不允许：架构决策归人）")
    sp.add_argument("--max-rounds", type=int, default=40)
    sp.add_argument("--model", default="")
    sp.add_argument("--scale", default="", choices=["", "S", "M", "L", "XL", "s", "m", "l", "xl"],
                    help="目标代码规模（S/M/L/XL）。不写就在需求里写『代码规模：M』，"
                         "再没有就按仓库实测推断并在报告里注明")
    sp.add_argument("--remote-model", default="",
                    help="只走这个远端模型档案（flash / pro）；不写 = 整条八档阶梯")
    sp.add_argument("--no-remote", action="store_true", help="不用远端（纯本地，质量会明显下降）")
    sp.add_argument("--quiet", action="store_true")
    sp.set_defaults(fn=cmd_night)

    sp = sub.add_parser("full",
                        help="全量生成+错误报告：本地工具全量检测→出报告→交远端修（开发文档 §13）")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--repair", action="store_true",
                    help="真的让远端按报告修（默认只出报告，不改代码）")
    sp.add_argument("--e2e", action="store_true",
                    help="连端到端探针一起跑（D6，很慢；默认跳过）")
    sp.add_argument("--from-scratch", action="store_true",
                    help="按三层（models→services→api）从零分批生成")
    sp.add_argument("--max-rounds", type=int, default=6)
    sp.add_argument("--per-file", action="store_true",
                    help="旧口径兜底：按错误文件派单、一轮最多改 --max-files 个。"
                         "默认**不用**：远端拿整仓快照 + 整仓写权限，"
                         "先归因再改（跨文件根因只有整仓视角才看得见）")
    sp.add_argument("--max-files", type=int, default=6,
                    help="只在 --per-file 下有效：一轮最多改几个文件")
    sp.add_argument("--gen-contract-only", action="store_true",
                    help="只生成全项目契约（分域、可续跑），不写代码。"
                         "空项目第一步：**这一步很慢（单域 1-3 分钟），"
                         "建议在你自己终端跑，别让 AI 会话把它回收**")
    sp.add_argument("--ui-base", default="",
                    help="前端服务地址（如 http://127.0.0.1:5173）。"
                         "给了才跑 D7 界面检测（Playwright + axe-core）")
    sp.add_argument("--max-domains", type=int, default=0,
                    help="本次最多生成几个域（0=全部）；跑不完下次接着跑剩下的")
    sp.add_argument("--mode", dest="gen_mode", default="auto",
                    choices=["auto", "scratch", "repair"],
                    help="生成口径：auto=自动判定（模块<3 当空项目从零写，否则已有仓库分析优化）；"
                         "scratch=强制从零写；repair=强制只改有错的地方。"
                         "**注意**：这个 --mode 是「生成口径」，与全局 --mode（权限模式 "
                         "client/developer/readonly）不是一回事；以前两者共用 dest=mode，"
                         "跑 `full` 会把权限模式静默改成 client（2026-11-15 修）")
    sp.add_argument("--refresh-contract", action="store_true",
                    help="重新生成全量契约并覆盖锁定文件（**会导致缓存全部失效**，只在需求变了时用）")
    sp.set_defaults(fn=cmd_full)

    sp = sub.add_parser("ui-watch",
                        help="界面监督：按轮跑 D7 并记台账，连续失败才告警（开发文档 §13.10）")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--ui-base", default="", help="前端服务地址，如 http://127.0.0.1:5173")
    sp.add_argument("--rounds", type=int, default=1, help="跑几轮（默认 1）")
    sp.add_argument("--interval", type=int, default=0, help="轮间隔秒数（默认 0）")
    sp.add_argument("--only", default="", help="只跑这些能力 id，逗号分隔")
    sp.add_argument("--history", action="store_true", help="只读台账历史（不跑浏览器）")
    sp.add_argument("--limit", type=int, default=20, help="--history 时显示最近几条")
    sp.set_defaults(fn=cmd_ui_watch)

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

    sp = sub.add_parser("model", help="领域模型：实体/状态/主干流/组件 + 四张图（只读，不花 token）")
    sp.add_argument("--repo", required=True, help="仓库目录")
    sp.add_argument("--write", action="store_true",
                    help="写入 .sixcoder/model.json 与 .sixcoder/diagrams/*.mmd")
    sp.add_argument("--enrich", action="store_true",
                    help="让远端补状态转换与业务不变式（会花钱；事实部分由引擎校验拦截）")
    sp.add_argument("--apply-invariants", action="store_true",
                    help="把可机械执行的不变式写成负向探针，加进蓝图")
    sp.set_defaults(fn=cmd_model)

    sp = sub.add_parser("contract", help="契约反推：从现有代码反推结构化契约（只读，不花 token）")
    sp.add_argument("--repo", required=True, help="存量仓库目录")
    sp.add_argument("--path", default="", help="只反推这个相对路径（不给就全仓）")
    sp.add_argument("--private", action="store_true", help="把 _ 开头的函数也收进契约面")
    sp.set_defaults(fn=cmd_contract)

    sp = sub.add_parser("inspect", help="定期巡检：缓存命中率趋势 + 文档健康（只读）")
    sp.add_argument("--repo", required=True)
    sp.add_argument("--scale", default="", choices=["", "S", "M", "L", "XL"])
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_inspect_docs)

    sp = sub.add_parser("questions", help="列出待人回答的问题（精修模式的主动提问）")
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
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    main()
