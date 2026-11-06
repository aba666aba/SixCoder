"""切片：能力 DAG → 若干"一轮能干完"的切片（竖切，不是横切）。

一条切片必须**闭合**：切完之后，它含的每条能力都能立刻被观测。
这是竖切的机械定义，也是 autonomy 能自我纠正的前提 ——
切片做完了，覆盖探针还是红的，下一轮自动补，不需要人发现。
横切（先把所有模型层写完）会让前 N 轮**没有任何东西能被观测**，等于盲写，
那正是"计划切错了也看不出来"的具体形态。

剂量：每个切片 ≤ dosing.suggest_max_funcs() 的函数数（按 funcs_est 累加），
      单个切片最多 SLICE_MAX_MODULES 个模块（再多，一轮的上下文就散了）。
DAG 顺序保证：能力在切片里的位置严格按拓扑序 → 依赖一定在前序切片或同切片，
所以"闭合"不是靠人保证，是靠打包顺序结构性保证（closure_problems 只是把它钉住）。
"""
from __future__ import annotations

from pathlib import Path

from . import blueprint as _bp

SLICE_MAX_MODULES = 4
DEFAULT_DOSE = 16          # 拿不到档位/校准信息时的保守剂量；dosing 会给更准的值


def dose(ws=None, step: dict | None = None, deps: int = 0) -> tuple[int, list[str]]:
    """这一轮能写多少函数（验收带宽）。拿不到信息就保守，不假装知道。"""
    try:
        from . import dosing as _dosing
        budget = _dosing.context_for(ws, step, deps=deps)
        n, why = _dosing.suggest_max_funcs(**budget)
        return int(n), list(why)
    except Exception as exc:                             # noqa: BLE001
        return DEFAULT_DOSE, [f"拿不到带宽信息（{type(exc).__name__}），按保守剂量 {DEFAULT_DOSE}"]


def _pending(bp, done) -> list:
    have = set(done) if done is not None else bp.done()
    out = []
    for c in bp.caps:
        if c.status in ("done", "descoped") or c.id in have:
            continue
        out.append(c)
    return out


def pack(bp, *, done=None, max_funcs: int = DEFAULT_DOSE,
         max_modules: int = SLICE_MAX_MODULES) -> list[list]:
    """按"地基优先"把待办能力装进若干个 ≤ 剂量的包。

    排序不是简单按插入序（实测 run 16）：能力表里 CLI 端到端能力如果排在最前，
    第一片就会去写入口，而它依赖的解析/求和模块还不存在 —— 于是整片白跑。
    口径：**被最多能力依赖的排在最前**（地基先做），其次按拓扑序，最后按 id（确定性）。
    端到端能力天然解锁少，自然落到后面。
    """
    from . import frontier as _front
    order, _missing = _bp.topo(bp)
    rank = {cid: i for i, cid in enumerate(order)}
    have = set(done) if done is not None else bp.done()
    pend = _pending(bp, done)
    pend.sort(key=lambda c: (-_front.unlocks(bp, c.id, have), rank.get(c.id, 10 ** 6), c.id))
    # **依赖必须真的完成，才允许进片**（不只是"排在前面"）。
    # 实测 exp1c：`cli_run` 依赖全部 helper，helper 还是空桩时就被排进去写 → 反复写不出来、
    # 整片空转。拓扑序只保证"顺序"，不保证"前置真的做完了"。
    ready = [c for c in pend if all(d in have or bp.by_id(d) is None for d in c.deps)]
    packs: list[list] = []
    cur: list = []
    cur_funcs = 0
    cur_mods: set[str] = set()
    for c in ready:
        # 只按能力**显式声明**的模块数收口；没声明的当未知（函数数上限已经兜住了）。
        # 早先按"每条预留 2 个文件"估过一个版本：给不出模块提示的能力会把切片
        # 无故收窄成 1 条能力一片，等于把剂量定义偷换掉。
        mods = set(c.modules or [])
        over_funcs = bool(cur) and cur_funcs + c.funcs > max_funcs
        over_mods = bool(cur) and len(cur_mods | mods) > max_modules
        if over_funcs or over_mods:
            packs.append(cur)
            cur, cur_funcs, cur_mods = [], 0, set()
        cur.append(c)
        cur_funcs += c.funcs
        cur_mods |= mods
    if cur:
        packs.append(cur)
    return packs


def slices(bp, *, done=None, max_funcs: int = DEFAULT_DOSE,
           max_modules: int = SLICE_MAX_MODULES) -> list[dict]:
    """待办能力 → [{"id","caps","funcs","modules","title"}]。同输入永远同输出。

    切片 id **必须由内容决定**，不能是"第几片"。原因（这是个真 bug，不是洁癖）：
    每片做完之后剩下的能力会重新装包，于是"第 2 片"下次可能变成"第 1 片"；
    而契约按片 id 缓存 → 第二片会拿到第一片那张考卷，**用错的判据去验收**。
    现在 id = 拓扑位置 + 能力集合指纹：同集合永远同 id（缓存可复用、可复现），
    集合一变立刻换 id（不会串用旧考卷）。
    """
    import hashlib
    order, _missing = _bp.topo(bp)
    pos = {cid: i for i, cid in enumerate(order, start=1)}
    out = []
    for one in pack(bp, done=done, max_funcs=max_funcs, max_modules=max_modules):
        mods: list[str] = []
        for c in one:
            for m in c.modules or []:
                if m not in mods:
                    mods.append(m)
        caps = [c.id for c in one]
        sig = hashlib.sha1("|".join(sorted(caps)).encode("utf-8")).hexdigest()[:8]
        out.append({"id": f"s{pos.get(caps[0], 0):02d}-{sig}",
                    "caps": caps,
                    "funcs": sum(c.funcs for c in one),
                    "modules": mods,
                    "title": "、".join(c.id.replace("cap.", "") for c in one[:3])
                             + ("…" if len(one) > 3 else "")})
    return out


def closure_problems(items: list[dict], bp, done=None) -> list[str]:
    """每条切片的依赖是否都在"已完成 + 前序切片 + 本切片"里。空表示闭合。"""
    have = set(done) if done is not None else bp.done()
    seen = set(have)
    problems: list[str] = []
    for sl in items:
        cur = set(sl["caps"])
        for cid in sl["caps"]:
            c = bp.by_id(cid)
            if c is None:
                problems.append(f"{sl['id']}: 引用了不存在的 {cid}")
                continue
            for d in c.deps:
                if d not in seen and d not in cur:
                    problems.append(f"{sl['id']}: {cid} 依赖 {d}，但 {d} 排在它后面")
        seen |= cur
    return problems


def plan(bp, *, done=None, max_funcs: int = DEFAULT_DOSE,
         max_modules: int = SLICE_MAX_MODULES) -> dict:
    """完整切法：切片 + 永远轮不到的 + 校验结果。"""
    items = slices(bp, done=done, max_funcs=max_funcs, max_modules=max_modules)
    from . import frontier as _front
    blocked = [(c.id, why) for c, why in _front.blocked_caps(bp, done=done)]
    return {"slices": items, "blocked": blocked,
            "problems": closure_problems(items, bp, done=done),
            "funcs": sum(s["funcs"] for s in items)}


def cmd_line(probe: dict) -> str:
    """把探针还原成**逐字命令行**（简报里给作者看的那一行，参数名不许改）。"""
    if not isinstance(probe, dict) or not probe:
        return ""
    kind = str(probe.get("kind") or "")
    def _q(a) -> str:
        s = str(a)
        return f'"{s}"' if (" " in s or "\n" in s) else s
    if kind == "cli":
        args = " ".join(_q(a) for a in (probe.get("args") or []))
        return f"python {probe.get('entry')}" + (f" {args}" if args else "")
    if kind == "py_call":
        args = ", ".join(repr(a) for a in (probe.get("args") or []))
        return f"{probe.get('module')}.{probe.get('func')}({args})"
    if kind == "pytest":
        args = " ".join(_q(a) for a in (probe.get("args") or []))
        return "python -m pytest -v" + (f" {args}" if args else "")
    return ""


def entry_example(probes: list[dict]) -> str:
    """入口脚本 main(argv) 的示例行：让函数级判据也能覆盖命令行形状。

    为什么必须有（实测 2026-11-05）：探针要求 `--file/--group/--value`，
    而作者写成了位置参数 `<path>`；契约里只有函数级示例，于是内循环判 20/20、
    真验收 0/8 —— 两套判据不一致时，必须先让函数级示例里出现这条调用。
    退出码用探针自己的 `expect_exit`（错误路径本来就该非 0）。
    """
    for p in probes:
        if isinstance(p, dict) and str(p.get("kind") or "") == "cli":
            argv = ", ".join(repr(str(a)) for a in (p.get("args") or []))
            code = int(p.get("expect_exit") or 0)
            return f"main([{argv}]) == {code}"
    return ""


def _public_sigs(path: Path) -> list[str]:
    """一个模块的公开签名（确定性 AST 解析，不用模型、不花 token）。"""
    import ast
    try:
        tree = ast.parse(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (SyntaxError, OSError):
        return []
    out: list[str] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and not n.name.startswith("_"):
            args = [a.arg for a in n.args.args if a.arg not in ("self", "cls")]
            out.append(f"{n.name}({', '.join(args)})")
        elif isinstance(n, ast.ClassDef) and not n.name.startswith("_"):
            ms = [m.name for m in n.body
                  if isinstance(m, ast.FunctionDef) and not m.name.startswith("_")]
            out.append(f"class {n.name}({', '.join(ms[:4]) or '…'})")
    return out


def interface_digest(ws, modules: list[str], *, limit: int = 40) -> list[str]:
    """**本片依赖到的**那些模块的完整公开签名（切片级上下文）。

    为什么不能用全局签名（实测 + 规模推算）：以前每轮拼 `planner.api_surface` 的前 700
    字符 —— 仓库一大，700 字符连一个模块都覆盖不了，等于没给。到 1000 个模块时，
    每轮的 prompt 应该只跟"这一片要用的接口"有关，与全仓规模无关（O(切片) 而不是 O(仓库)）。
    """
    out: list[str] = []
    for m in modules[:limit]:
        p = Path(ws) / str(m)
        if not p.exists():
            continue
        sigs = _public_sigs(p)
        if sigs:
            out.append(f"{p.stem}: " + ", ".join(sigs))
    return out


def module_index(ws, *, limit: int = 60) -> str:
    """全局模块索引：只列**名字和函数数**（防止重复造轮子，又不吃上下文）。"""
    names: list[str] = []
    for p in sorted(Path(ws).glob("*.py")):
        if p.name.startswith("test_"):
            continue
        names.append(f"{p.stem}({len(_public_sigs(p))})")
    for p in sorted(Path(ws).glob("*/")):            # 包里的模块也算（只一层）
        for q in sorted(p.glob("*.py")):
            if q.name.startswith(("test_", "__init__")):
                continue
            names.append(f"{p.name}/{q.stem}({len(_public_sigs(q))})")
    return "、".join(names[:limit])


ARCH_HEAD = "## 架构安排"
ARCH_CAP = 1400          # 简报里最多带多少字（多了会顶掉真正要写的能力清单）


def _arch_block(bp) -> str:
    """从蓝图目标里取出"架构安排"那一段（人拍的板），没有就退回目标前 400 字。

    为什么单独抠这一段：bp.goal 前面还有一句自然语言目标，后面可能被追加过别的
    备注；把整段原样塞简报会把这一轮的活挤到后面去（作者会看漏要做的能力）。
    """
    goal = str(getattr(bp, "goal", "") or "")
    if not goal.strip():
        return ""
    i = goal.find(ARCH_HEAD)
    if i < 0:
        head = goal.strip().split("\n\n")[0][:400]
        return f"# 目标（一句话）\n{head}"
    body = goal[i:i + ARCH_CAP]
    tail = "（后略：详见 .sixcoder/architecture.md）" if len(goal) - i > ARCH_CAP else ""
    return "# 人已经拍板的架构安排（**与你的直觉冲突时以此为准**，不要自行更改）\n" + body + tail


def brief(sl: dict, bp, *, ws=None, done=None) -> str:
    """切片 → 交给契约合成的一段需求文本（这一轮的"考试范围"）。

    三样东西必须给到作者，少一样就会返工（每一条都是实测教训）：
      1. 这一轮要做的能力 + **逐字验收命令**（参数名一个都不许改）
      2. 上一次它为什么红（`c.why`）—— 不给证据，重试就是重跑同一个错误
      3. 已有模块的公开签名 + 已完成能力（不重写、不重复实现）
    """
    from . import probe as _probe
    have = set(done) if done is not None else bp.done()
    probes: list[dict] = []
    # 人拍的板必须出现在**每一片**的简报里：只放进目标文本（喂给蓝图）是不够的，
    # 真正写代码的是这一片，它看不到就等于没问（实测：入口名/空值口径各写各的）。
    # 放在最前面：后面的能力清单会被作者当成重点，架构安排被挤到末尾就会被忽略。
    _arr = _arch_block(bp)
    lines: list[str] = ([_arr, ""] if _arr else []) + \
        ["# 本轮要做的能力（只做这些，不要扩张范围）"]
    for cid in sl["caps"]:
        c = bp.by_id(cid)
        if c is None:
            continue
        probes.append(c.probe if isinstance(c.probe, dict) else {})
        lines.append(f"- {cid}：{c.desc}")
        lines.append(f"  观测方式：{_probe.describe(c.probe)}")
        cmd = cmd_line(c.probe)
        if cmd:
            lines.append(f"  验收命令（必须逐字跑通；参数名一个都不许改、不许改成位置参数）：{cmd}")
        files = (c.probe or {}).get("files") if isinstance(c.probe, dict) else None
        if files:
            lines.append("  验收素材（运行时会放在当前目录）：" + "、".join(map(str, files)))
            for name, content in list(files.items())[:3]:
                body = str(content).replace("\n", "\\n")[:160]
                lines.append(f"    {name} 的内容（**必须原样正确处理**，含引号/换行等边界）：{body}")
        if c.deps:
            lines.append("  依赖（应当已经可用）：" + "、".join(c.deps))
        if c.why and c.status != "done":
            lines.append(f"  上一次它这样红的（这次必须改掉）：{str(c.why)[:220]}")
    ex = entry_example(probes)
    if ex:
        lines += ["", "# 入口脚本的硬要求",
                  "- 入口脚本由引擎生成、**不要写 main**；你只写 cli_core.py 里的 "
                  "`cli_run(argv) -> (退出码, 文本)`",
                  "- **cli_run 只做参数解析 + 调度**：读写文件、筛选、聚合、排序、格式化各自拆到"
                  "独立模块（每模块 ≤6 个函数）。实测：全塞进 cli_run 一个函数，13 个函数的活"
                  "会反复写不完（输出顶到配额、被截断）",
                  f"- 契约里必须有一条示例**就是这个调用**（照抄，参数名不许改）：",
                  f"  {ex}"]
    if ws is not None:
        # 切片级上下文：只给"这一片会用到"的模块签名（本片模块 + 依赖模块），
        # 再加一个全局紧凑索引。两者都是确定性 AST，不花 token。
        touched: list[str] = []
        for cid in sl["caps"]:
            c = bp.by_id(cid)
            if c is None:
                continue
            for m in c.modules or []:
                if m not in touched:
                    touched.append(m)
            for d in c.deps:
                dc = bp.by_id(d)
                for m in (dc.modules if dc else []) or []:
                    if m not in touched:
                        touched.append(m)
        digest = interface_digest(ws, touched)
        if digest:
            lines += ["", "# 你可以直接调用的接口（只列本片依赖到的模块，签名为准）", *digest]
        idx = module_index(ws)
        if idx:
            lines += ["", "# 仓库现有模块（名字+函数数；已有能力就复用，不要重复实现）", idx]
    finished = [c.id for c in bp.caps if c.id in have]
    if finished:
        lines += ["", "# 不要改坏（这些能力**现在已经通过观测**；改坏等于把它们从完成名单里划掉，"
                      "而且会让下一轮重新烧钱修它们）",
                  "、".join(finished)]
        for c in bp.caps:
            if c.id not in have:
                continue
            cmd = cmd_line(c.probe)
            if cmd:
                lines.append(f"- {c.id}：{cmd}")
    lines += ["", "# 铁律",
              "1. 上面每一条能力，做完之后必须能被它自己那条验收命令验证（这是验收判据）",
              "2. 不要实现本轮清单之外的东西（不会验收，且会吃掉预算）",
              "3. 已经有同名能力时，扩展它，不要另写一个",
              "4. **每个模块最多 6 个函数**：解析/计算/输出各自拆成独立模块"
              "（如 csvio.py、agg.py），入口脚本只放 CLI 接线与 main。"
              "一个文件塞十个函数会互相拖累（实测：整文件重写一次只错一个判据，"
              "整轮就得重写十个函数）"]
    return "\n".join(lines)
