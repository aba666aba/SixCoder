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

import json

from pathlib import Path

from . import blueprint as _bp

# 切片规模的两个上限（2026-11-09 用户铁律：完全相信远端 → 只按物理约束封顶）。
#
# 旧口径（4 / 16）来自"远端写不完"的年代，而那个判断的依据已被证明不可靠：
# 当时"写不完"的真实原因是**引擎自己造坏数据**（多行签名截断、新函数被静默丢掉、
# 路由接线把 batches 判成没有匹配函数），不是远端能力不足。那些缺陷今天已修。
#
# 现在：
#   · SLICE_MAX_MODULES = 8：一片横跨多少个**模块**。8 足够覆盖一个完整的
#     业务纵切（如"批次创建 → 运行 → 归因"跨 4 个模块），又不至于让上下文散掉。
#     真正封顶的是**函数数**（由 dosing 按输出配额算），不是模块数。
#   · DEFAULT_DOSE = 24：拿不到档位/校准信息时的兜底剂量（不是"保守"，
#     是"照物理量级估"：输出配额 6000 token ÷ 每函数约 190 token ≈ 31，取 24 起步）。
SLICE_MAX_MODULES = 8
DEFAULT_DOSE = 24          # 拿不到档位/校准信息时的兜底剂量；dosing 会给更准的值


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
    cur_sub = ""
    for c in ready:
        # 只按能力**显式声明**的模块数收口；没声明的当未知（函数数上限已经兜住了）。
        # 早先按"每条预留 2 个文件"估过一个版本：给不出模块提示的能力会把切片
        # 无故收窄成 1 条能力一片，等于把剂量定义偷换掉。
        mods = set(c.modules or [])
        over_funcs = bool(cur) and cur_funcs + c.funcs > max_funcs
        over_mods = bool(cur) and len(cur_mods | mods) > max_modules
        # **不再按子系统硬断**（旧口径：一片只碰一个子系统的文件面，意图是"接口先冻结"）。
        # 实测 2026-11-08：19 个子系统平均 4 条能力，硬不跨导致**每片只装 1 条**
        # （76 条能力切出 22 片，每片 5 个函数，而剂量是 16 —— 带宽浪费 2/3）。
        # 片段一多，固定开销（契约生成/作者调用/探针/门禁）就吃掉大部分预算：
        # 一天只推进 10 条，而主干一条都没通。
        # "不写坏别人的文件"用剂量 + 模块数上限表达已经够：真正会被改坏的，
        # 是同一片里模块数爆掉、上下文散掉，不是"跨了子系统"这件事本身。
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
            # 落点按**文件所有权**筛选（2026-11-11 治本，§11.4e）：
            # 契约只列**远端能写**的地方（业务层）——
            # 接线层（app/api/）是引擎拥有的文件，每次接线都整块重渲染，
            # 远端写进去会被冲掉，且必然撞上「承载已通过能力的文件不许整文件重写」
            # 的护栏 → 两道锁叠成死锁（实测 56 条红灯里 52 条落在 app/api/*）。
            # 路由不用远端管：引擎接线会遍历候选模块找到真正有匹配函数的模块。
            # 判定实现只有一处：`openhouse.writable_modules`（与派单共用，不重复造逻辑）。
            try:
                from . import openhouse as _oh
                _mods = _oh.writable_modules(c.id, c.modules or [])
            except Exception:                            # noqa: BLE001
                _mods = list(c.modules or [])
            for m in _mods:
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
    if kind == "http":
        # Web 能力：把请求链逐字写出来（作者要照着实现路由，路径/字段名一个都不许改）
        steps = probe.get("steps") or []
        if steps:
            out = []
            for st in steps:
                st = st or {}
                hdr = ""
                hs = st.get("headers") or {}
                if isinstance(hs, dict) and hs:
                    hdr = "  " + " ".join(f"-H {k}:{v}" for k, v in hs.items())
                body = ""
                if st.get("json") is not None:
                    body = "  -d '" + json.dumps(st.get("json"), ensure_ascii=False) + "'"
                out.append(f"{str(st.get('method') or 'GET').upper()} "
                           f"{st.get('path') or '/'}{hdr}{body}")
            return "；然后 ".join(out)
        return f"{str(probe.get('method') or 'GET').upper()} {probe.get('path') or '/'}"
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


_ROUTE_NOISE = {"api", "rest", "internal", "v1", "v2", "v3", "v4", "v5"}


def route_home(path: str) -> str:
    """路由应该落在哪个模块：按**路径自己的资源段**定，不看它属于哪一片。

    为什么必须这样（实测 2026-11-09）：`cap.topic.create_draft` 的链中间带一条
    `POST /api/v1/auth/login`，契约统一写在落点模块 `app/api/topics.py` 里。
    作者照写 → 函数进了 topics.py → 引擎接线时 `_locate_route` 去 auth.py 找 login
    找不到 → 照样 404。**路由的落点由路径决定，不由切片决定**，这条要说给作者听。
    """
    import re as _re
    segs = [x for x in str(path or "").split("?")[0].strip("/").split("/")
            if x and not x.startswith("{") and not x.startswith("$")
            and x.lower() not in _ROUTE_NOISE
            and not _re.fullmatch(r"v\d+", x.lower())
            and not _re.fullmatch(r"\d+", x)]
    if not segs:
        return ""
    # **取第一段**，不是最后一段：模块落点看的是资源域（auth/batches/org/llm），
    # 不是动作（login/run/unacceptable）。取末段会得出 app/api/login.py 这种不存在的路径。
    seg = segs[0]
    return f"app/api/{seg}.py" if _re.fullmatch(r"[a-z_]+", seg) else ""


def handler_hint(path: str) -> str:
    """给"没人负责"的端点推一个**能被接线层认出**的处理函数名。

    为什么必须给名字（实测 2026-11-09）：契约只说"要提供 POST /auth/login 路由"，
    作者就写了 `parse_login_body` / `build_login_response`——而接线层的
    `_NON_HANDLER_PREFIX` 正是要否决 parse_/build_ 开头的构造器（防止构造器当端点）。
    作者照契约写得再对，`_pick_func` 仍返回空，端点永远 404。
    **契约规则与接线规则自相矛盾**，这是纯机制问题，与模型能力无关。

    判据只取路径结构：最后一个非参数/非版本/非噪声的小写段。拿不准就返回空（不猜）。
    """
    import re as _re
    segs = [x for x in str(path or "").split("?", 1)[0].strip("/").split("/")
            if x and not x.startswith("{") and not x.startswith("$")
            and x.lower() not in _ROUTE_NOISE
            and not _re.fullmatch(r"v\d+", x.lower())
            and not _re.fullmatch(r"\d+", x)]
    if not segs:
        return ""
    seg = segs[-1].lower()
    return seg if _re.fullmatch(r"[a-z_]+", seg) else ""


def owned_routes(bp) -> set:
    """已经被某条能力**当产出负责**的端点集合（每条链的 steps[-1]）。

    给 `web_route_spec` 判断"链里这一步有没有人管"用：没人管的必须写进契约，
    否则作者不会实现它（login 黑洞就是这么来的）。
    """
    out: set = set()
    for c in getattr(bp, "caps", []) or []:
        pr = c.probe or {}
        if str(pr.get("kind") or "") != "http":
            continue
        steps = pr.get("steps") or []
        if not steps:
            continue
        st = steps[-1] or {}
        out.add((str(st.get("method") or "GET").upper(),
                 str(st.get("path") or "/")))
    return out


def web_route_spec(probes: list[dict], owned: set | None = None,
                   home: str = "") -> list[str]:
    """把 http 探针还原成**路线的硬要求**（方法/路径/请求体字段/状态码/返回字段）。

    为什么必须写死在简报里（实测 2026-11-07）：契约只声明服务层函数
    `register_user(username, password) -> dict`，作者就只写函数、不挂路由，
    探针真发 `POST /auth/register` 时 404 → 整片红，然后重试还在同一个地方打转。

    `owned`：**已经被某条能力当产出负责**的 (method, path) 集合。
    旧口径只取 `steps[-1]`，链中间的端点就成了没人负责的黑洞：
    `cap.auth.login_token` 的链是 [register, login, me]，契约只要求 me，作者就只写 me，
    login 永远不存在 → 所有需要 token 的链全 404（实测 2026-11-09，10 done 卡一整天）。
    现在把链里**没人负责**的步骤也写进契约；owned=None 时保持旧行为。
    """
    out: list[str] = []
    seen: set = set()
    unowned: set = set()
    for p in probes:
        if not isinstance(p, dict) or str(p.get("kind") or "") != "http":
            continue
        steps = p.get("steps") or []
        targets: list[dict] = []
        if steps:
            last = steps[-1] or {}
            lk = (str(last.get("method") or "GET").upper(),
                  str(last.get("path") or "/"))
            targets.append(last)
            if owned is not None:
                for st0 in steps[:-1]:
                    st0 = st0 or {}
                    k = (str(st0.get("method") or "GET").upper(),
                         str(st0.get("path") or "/"))
                    if k not in owned and k != lk:
                        targets.append(st0)
                        unowned.add(k)
        else:
            targets.append(p)
        for st in targets:
            method = str(st.get("method") or "GET").upper()
            path = str(st.get("path") or "/")
            key = (method, path)
            if key in seen:
                continue
            seen.add(key)
            body = st.get("json")
            status = st.get("expect_status")
            want = st.get("expect_json")
            bits = [f"{method} {path}"]
            if isinstance(body, dict) and body:
                bits.append("请求体 JSON 字段：" + "、".join(str(k) for k in body))
            if isinstance(status, list):
                bits.append("状态码 " + "/".join(map(str, status)))
            elif status is not None:
                bits.append(f"状态码 {status}")
            if isinstance(want, dict) and want:
                bits.append("返回必须含："
                            + "、".join(f"{k}={v!r}" for k, v in list(want.items())[:6]))
            _h = route_home(path)
            if home and _h and _h != home:
                # 2026-11-11 治本（§11.4e）：旧口径让远端把这条的处理函数写进
                # **引擎拥有的路由文件**（引擎每次接线都整块重渲染它 → 远端写的被冲掉，
                # 且必然撞上「承载已通过能力不许整文件重写」的护栏 → 死锁）。
                # 新口径：函数写在**上面列的落点模块**（业务层，远端拥有），
                # 路由由引擎接线层按**函数名**接上。
                #
                # 2026-11-11 12:45 撤回一次改动：曾想在这里"点名函数名"，
                # 但 `handler_hint` 返回的是**路径末段**（topics / me / login）——
                # 点名 `topics` 会让远端写 `topics()` 这种函数名，与契约签名
                # `create_topic` 打架，**制造新矛盾**。
                # 保留原口径（名字要含路径末段）：它就是接线层 `_pick_func` 的真实规则
                # （含单复数变体），远端照它写就能接上。
                bits.append("这条的处理函数写在**上面列的落点模块**里"
                            "（不要写进引擎的路由文件 —— 那里会被重渲染冲掉）；"
                            "函数名要含路径末段，接线层靠名字接上；"
                            "**凭据参数必须叫 `authorization` 且类型是 str**"
                            "（路由层只能给凭据字符串；"
                            "不要写 `user`/`account`/`principal` 这类对象参数 —— "
                            "路由层供给不了，接不上就是 404）")
            if key in unowned:
                _hint = handler_hint(path)
                if _hint:
                    bits.append(f"必须提供处理函数 `{_hint}`（名字含路径末段；"
                                f"parse_/build_/make_ 等构造器前缀会被接线层否决；"
                                f"路由由引擎自动生成）")
            out.append("；".join(bits) + "（字段名逐字一致）")
    return out


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
        # 落点：**只给远端能写的地方**（2026-11-11 治本，§11.4e）。
        # 简报是契约合成的输入 —— 这里写了 `app/api/topics.py`，
        # 契约就会要求远端在**引擎拥有的文件**里写服务层函数，
        # 而那个文件 ① 承载已通过能力 → 护栏不许整文件重写
        #        ② 引擎每次接线都整块重渲染 → 远端写的被冲掉
        # 两道锁叠在一起 = 死锁（实测：56 条红灯里 52 条落在 app/api/*）。
        try:
            from . import openhouse as _oh
            _wm = _oh.writable_modules(cid, c.modules or [])
        except Exception:                                # noqa: BLE001
            _wm = list(c.modules or [])
        if _wm:
            lines.append(f"  落点模块（**必须逐字用这些路径**，不许自创 calc.py 之类）："
                         + "、".join(_wm))
        if str((c.probe or {}).get("kind") or "") == "http":
            # 路由归属要说清：**业务层只写服务层函数，路由由引擎接线层挂**。
            # 旧口径让远端"在业务层挂 router 装饰器" → 与引擎的接线段两个 router 打架。
            try:
                from . import openhouse as _oh2
                _all_wire = bool(_wm) and all(_oh2.is_wiring_layer(m) for m in _wm)
            except Exception:                            # noqa: BLE001
                _all_wire = False
            if _all_wire:
                lines.append("  Web：本模块要写 `router = APIRouter()` 并挂路由；"
                             "**不要写 app/main.py**（引擎生成，你的 router 会自动挂上）")
            else:
                lines.append("  Web：**只写服务层函数**（纯函数/纯字典进出）——"
                             "**不要在这里挂 `@router` 装饰器**，路由由引擎接线层按契约自动挂；"
                             "也不要写 app/main.py")
        lines.append(f"  观测方式：{_probe.describe(c.probe)}")
        # **口径冲突显式化**（§11.4m）：能力描述（人话）与探针断言（真正会跑的东西）
        # 对同一个字段给了不同取值时，**探针说了算** —— 但必须**写明**，
        # 否则契约合成/诊断架构师/写码三方各读各的 → 诊断出"返回 A 而不是要求的 A"
        # 这种自相矛盾的话（实测切片 s33），白烧一轮。
        try:
            from . import capconf as _capconf
            _cf = _capconf.describe_conflict(c)
        except Exception:                                # noqa: BLE001
            _cf = ""
        if _cf:
            lines.append(f"  ⚠ {_cf}")
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
    _home = ""
    try:
        _home = str((sl.get("modules") or [""])[0] or "")
    except Exception:                                   # noqa: BLE001
        _home = ""
    _routes = web_route_spec(probes, owned_routes(bp) if bp is not None else None,
                             home=_home)
    if _routes:
        # 路由归属：**业务层只写服务层函数，路由由引擎接线层挂**（§11.4e）。
        # 旧口径写"路由挂在落点模块的 router 上" —— 落点现在是业务层，
        # 远端会在业务层挂 `@router` → 与引擎接线段的 router **两个实例**，
        # 路由注册到谁身上不确定 → 表现为"端点 404 / 字段不对"。
        try:
            from . import openhouse as _oh3
            _sl_wire = bool(sl.get("modules")) and all(
                _oh3.is_wiring_layer(m) for m in (sl.get("modules") or []))
        except Exception:                                # noqa: BLE001
            _sl_wire = False
        _route_note = ("请求体用 Pydantic 模型接收 JSON，再调用上面契约里的函数；"
                       "路由挂在「落点模块」的 `router` 上，**不要写 app/main.py**（引擎生成）。"
                       if _sl_wire else
                       "请求体用 Pydantic 模型接收 JSON，再调用上面契约里的函数；"
                       "**只写服务层函数，不要挂 `@router` 装饰器** —— "
                       "路由由引擎接线层按契约自动挂；**不要写 app/main.py**（引擎生成）。")
        lines += ["", "# 本片必须实现的路由（探针会真的发这些请求：路径 / 字段名 / 状态码一个都不许改）",
                  *[f"- {r}" for r in _routes],
                  f"- {_route_note}"]
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
