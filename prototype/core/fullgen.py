"""全量生成 + 错误报告流程（开发文档 §13，替代切片增量）。

## 一条命令跑完的四阶段

```
阶段 0  全量契约（一次生成、锁定；不再按片重合成）
阶段 1  远端按三层分批生成：models → services → api（复用 author / author_modules）
阶段 2  本地全量检测 D1–D6（detectors，零 token）→ 错误报告（errorreport）
阶段 3  错误报告整体交给远端修（按文件分组，evidence 就是报告片段）
阶段 2' 重新检测 → 只报新增+未修 → converge 判据决定继续/换策略/叫人
阶段 4  P0+P1 清零 且 端到端全绿 → 收工
```

## 与旧流程的关系

`cli.py night`（切片增量）**原样保留**，`cli.py full` 是新入口。
任何一步不通都能退回旧线 —— 不删旧代码，只加新路径。

## 为什么"阶段 1"能跳过

产物已有 46 个模块时，**不需要从零生成**：直接进阶段 2 出报告、阶段 3 修。
只有产物为空（或 `--from-scratch`）才走阶段 1。这样同一个入口既能"新建项目"，
也能"接手一个写得半对的项目"—— 后者才是当前的真实处境。
"""
from __future__ import annotations

import time
from pathlib import Path

from . import converge as _conv
from . import detectors as _det
from . import errorreport as _rep
from . import routefuncs as _rf

__all__ = ["run", "detect", "repair", "LAYERS"]

# 三层分批（依赖顺序，不可乱序）
LAYERS = (
    ("models", ("app/models.py", "app/schemas.py")),
    ("services", None),          # None = 动态取 app/services/*.py
    ("api", None),               # None = 动态取 app/api/*.py（引擎渲染，远端正则只写业务层）
)


def _layer_targets(ws, bp, name: str, fixed) -> list[str]:
    """这一层要写哪些文件 —— **按栈与蓝图落点决定**（2026-11-15）。

    旧实现写死 `app/models.py` / `app/services/*.py` / `app/api/*.py`：
    Django 仓（`apps/<域>/views.py`）上一个落点都没有 → scratch 生成**什么也不写**，
    然后检测全绿（因为根本没生成），最后报告"没有错误"——最坏的那种假绿。
    """
    ws = Path(ws)
    try:
        from . import stack as _st
        _ad = _st.from_workspace(ws)
    except Exception:                                    # noqa: BLE001
        _ad = None

    # P0（2026-11-24）：先走可插拔的生成计划；取不到才回退到下面的旧 if 分支。
    # 为什么保留旧分支：这是"生成骨架"重构的第一刀，行为必须逐字不变。
    # 旧分支留在这里既是回退路径，也是这段逻辑的对照物 —— 等 P1/P2 补完
    # 新形态、且快照证明没差异之后，再单独一刀删掉。
    try:
        from . import layerplan as _lp
        _plan = _lp.plan_for(ws, _ad)
        if _plan is not None:
            _ctx = _lp.LayerContext(ws=ws, blueprint=bp)
            return _plan.targets(_ctx, name)
    except Exception:                                    # noqa: BLE001
        pass                                             # 计划层坏了就按旧逻辑跑

    is_django = getattr(_ad, "name", "") == "django"
    # **组件类栈**（前端 SPA / Electron …）判定不写死栈名：看适配器的落点模板
    # 是不是组件后缀。这样以后再加 Vue/Native 生态包不用再改这里（2026-11-19）。
    is_frontend = str(getattr(_ad, "module_file_fmt", "") or "").endswith(
        (".jsx", ".tsx", ".vue"))

    # **前端 SPA / Electron**（2026-11-19 生态包）：落点按蓝图 modules 里声明的
    # .jsx/.tsx/.vue 文件写，不碰 Python 分层（app/models.py 那套对前端不存在）。
    if is_frontend:
        mods = sorted({m for c in ((bp.caps if bp is not None else []))
                       if getattr(c, "status", "") != "descoped"
                       for m in (getattr(c, "modules", None) or [])})
        return [m for m in mods
                if m.endswith((".jsx", ".tsx", ".vue", ".js", ".ts"))]

    if is_django:
        # 数据层已建好（19 张表）；重生成会把已有模型和迁移冲掉
        if name == "models":
            return []
        # 业务层：只写一次（Django 的分层是 views 里调算法/模型，不存在 services/api 两批）
        if name != "services":
            return []
        mods = sorted({m for c in ((bp.caps if bp is not None else []))
                       if getattr(c, "status", "") != "descoped"
                       for m in (getattr(c, "modules", None) or [])})
        return [m for m in mods
                if m.startswith(("apps/", "matching_core/")) and m.endswith(".py")]

    if fixed:
        return [p for p in fixed if (ws / p).is_file()]
    if name == "services":
        d = ws / "app" / "services"
        return ([str(p.relative_to(ws)).replace("\\", "/")
                 for p in sorted(d.glob("*.py"))] if d.is_dir() else [])
    d = ws / "app" / "api"
    return ([str(p.relative_to(ws)).replace("\\", "/")
             for p in sorted(d.glob("*.py"))] if d.is_dir() else [])


def _ensure_module_files(ws, rels: list[str]) -> list[str]:
    """还没被创建过的落点先放一个空模块（只有 docstring），让作者有地方写。

    为什么不"跳过不存在的文件"：Django 仓首次生成时 `apps/<域>/views.py` 都不存在，
    跳过就等于什么都生成（实测旧实现：0 个文件，检测全绿）。
    """
    made = []
    for rel in rels:
        p = Path(ws) / rel
        if p.exists():
            continue
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            # 注释符按语言选：前端（jsx/tsx/vue/js/ts）用 `//`，Python 用 """。
            # 用错会让骨架文件成语法错误 → 后续检测全红（假红）。
            if rel.endswith((".jsx", ".tsx", ".js", ".ts", ".vue")):
                p.write_text("// %s\n" % rel, encoding="utf-8")
            else:
                p.write_text('"""%s"""\n' % rel, encoding="utf-8")
            made.append(rel)
        except OSError:
            continue
    return made


def _emit(emit, kind: str, payload: dict) -> None:
    if emit:
        try:
            emit(kind, payload)
        except Exception:                                # noqa: BLE001
            pass


CONTRACT_LOCK = ".sixcoder/full_contract.txt"


def _lock_contract(ws, *, refresh: bool = False, emit=None) -> str:
    """**全量契约锁定**：一次生成，之后不再变。

    为什么这是"缓存命中率"的关键（2026-11-14 实测数据）：
    最近 14 次远端调用的缓存命中率是 **45.8%**，但波动极大 ——
    同为 32k 的 prompt，一次 **68.1%**、另一次 **9.1%**。
    差别只有一个来源：**前缀是不是同一份**。
    旧流程**每片重新合成契约** → 前缀每次都变 → 缓存全丢（那些 3.8% / 7.7% / 9.1%
    就是"契约刚换过"的那几次）。命中部分按 1/10 计价，**丢缓存等于把 prompt 费用
    放大 ~9 倍**，而这部分占大头（prompt 32k vs completion 几百）。

    同时它还消掉今天最大的坑：**契约命名每轮漂移**
    （同一函数三轮叫 `teachers_me` / `teacher_me` / `get_teacher_me`）。

    锁定文件：`.sixcoder/full_contract.txt`。
    · 存在且 `refresh=False` → **直接用**（前缀稳定）
    · 不存在 → 从现状（最新切片契约 / interfaces）拷一份并锁定
    · `refresh=True` → 重新生成后覆盖（**会导致缓存全丢，慎用**）
    """
    ws = Path(ws)
    lock = ws / CONTRACT_LOCK
    if lock.is_file() and not refresh:
        txt = lock.read_text(encoding="utf-8-sig", errors="replace")
        if txt.strip():
            _emit(emit, "full_contract_locked", {"path": str(lock), "chars": len(txt),
                                                "note": "用锁定契约（前缀稳定 → 命中缓存）"})
            return txt
    # 首次：从现状取一份（不花 token —— 只读已有产物）
    txt = ""
    for cand in (ws / ".sixcoder" / "slices",):
        try:
            files = sorted(cand.glob("*.txt"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            files = []
        if files:
            txt = files[0].read_text(encoding="utf-8-sig", errors="replace")
            break
    if not txt.strip():
        ifc = ws / ".sixcoder" / "interfaces.json"
        if ifc.is_file():
            txt = ifc.read_text(encoding="utf-8-sig", errors="replace")
    if txt.strip():
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            lock.write_text(txt, encoding="utf-8")
            _emit(emit, "full_contract_lock", {"path": str(lock), "chars": len(txt)})
        except OSError as exc:
            _emit(emit, "notice", {"message": "契约锁定失败：%s" % exc})
    return txt


# ── 两种场景的自动判定（2026-11-14 用户定稿）──────────────────────────────
#
# **空项目 → 直接重新写**（三步：全量契约 → models/services/api 分批生成 → 检测/修）
# **已有仓库 → 分析优化**（蓝图 + 报告，让远端只改有错的地方）
#
# 判据不用人记：产物里**业务模块少于 3 个**就当空项目。
# 为什么不按"有没有 .sixcoder"：新仓库也可能已经有蓝图（先规划后写码），
# 那时照样该从零写；而 3 个以上模块说明骨架已经立起来了，重写=推翻已验证能力。
SCRATCH_MIN_MODULES = 3


def decide_mode(ws) -> str:
    """`'scratch'`（空项目，从零写）| `'repair'`（已有仓库，分析优化）"""
    try:
        mods = [m for m in _det.list_modules(ws) if not m.endswith("__init__.py")]
    except Exception:                                    # noqa: BLE001
        mods = []
    return "scratch" if len(mods) < SCRATCH_MIN_MODULES else "repair"


def _domains(bp) -> dict[str, list[str]]:
    """按能力 id 的第二段分域：`cap.topic.create_draft` → `topic`。

    为什么要分域生成契约：**单次输出太长会超时** ——
    实测 2026-11-14：3 条能力的契约在"最强档 + thinking"下跑了 >4.7 分钟仍没回，
    被超时杀掉，¥0.58 白花、连账都没记。分域后每次只输出 5–15 条能力的签名，
    单次 1–3 分钟能回，且**某域失败不影响其它域**（失败的域留空，其余照样锁定）。
    """
    out: dict[str, list[str]] = {}
    for c in (getattr(bp, "caps", None) or []):
        cid = str(getattr(c, "id", ""))
        parts = cid.split(".")
        key = parts[1] if len(parts) > 2 else (parts[0] if parts else "misc")
        out.setdefault(key or "misc", []).append(cid)
    return out


def generate_contract_by_domain(ws, bp=None, *, emit=None,
                                max_domains: int = 0) -> dict:
    """**分域生成全项目契约**，拼成一份并锁定（空项目的第一步）。

    返回 {ok, path, chars, funcs, routes, domains: [{name, caps, ok, chars}]}。
    某域失败**不中断**（记进 domains[i].ok=False），拼出来的契约照常可用 ——
    缺的域下一轮再补，比"一失败就整体放弃"好得多。
    """
    from . import blueprint as _bp
    ws = Path(ws)
    if bp is None:
        bp = _bp.load(ws)
    try:
        from . import attribution as _attr_fg
        _attr_fg.set_blueprint(bp)
    except Exception:                                    # noqa: BLE001
        pass
    doms = _domains(bp)
    if not doms:
        return {"ok": False, "problems": ["蓝图为空，无法生成契约"]}
    names = sorted(doms, key=lambda k: (-len(doms[k]), k))
    if max_domains:
        names = names[:max_domains]

    parts: list[str] = []
    detail: list[dict] = []
    for name in names:
        caps = doms[name]
        _emit(emit, "full_domain_start", {"domain": name, "caps": len(caps)})
        r = generate_full_contract(ws, bp, cap_ids=caps, emit=emit)
        ok = bool(r.get("ok"))
        detail.append({"name": name, "caps": len(caps), "ok": ok,
                       "chars": int(r.get("chars") or 0),
                       "problems": (r.get("problems") or [])[:2]})
        _emit(emit, "full_domain_done", {"domain": name, "ok": ok,
                                         "chars": int(r.get("chars") or 0)})
        if ok:
            parts.append(str(r.get("text") or ""))

    if not parts:
        return {"ok": False, "problems": ["所有域都生成失败"], "domains": detail}

    merged = "\n\n".join(p.strip() for p in parts if p.strip())
    # 接线约束只需一份（`_wire_constraints` 幂等）
    try:
        from . import auto as _auto
        merged = _auto._wire_constraints(merged)
    except Exception:                                    # noqa: BLE001
        pass
    lock = ws / CONTRACT_LOCK
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(merged, encoding="utf-8")
    except OSError as exc:                               # noqa: BLE001
        return {"ok": False, "problems": ["锁定文件写不了：%s" % exc],
                "domains": detail}

    n_funcs, n_routes = 0, 0
    try:
        from . import contract as _cm
        n_funcs = sum(len(getattr(c, "funcs", None) or [])
                      for c in _cm.parse_requirement(merged).values())
    except Exception:                                    # noqa: BLE001
        n_funcs = 0
    try:
        n_routes = len(_rf.parse(merged))
    except Exception:                                    # noqa: BLE001
        n_routes = 0
    _emit(emit, "full_contract_merged",
          {"domains": len(detail), "funcs": n_funcs, "routes": n_routes,
           "chars": len(merged)})
    return {"ok": True, "path": str(lock), "chars": len(merged),
            "funcs": n_funcs, "routes": n_routes, "domains": detail}


def generate_full_contract(ws, bp=None, *, cap_ids=None, emit=None,
                            max_caps: int = 0, step_index="strongest") -> dict:
    """调远端**生成全项目契约**（一次），落盘并锁定。

    这就是「大片远端生成」的第一步：**范围一次性说清**，之后不再变。
    与切片契约的区别：切片只给 5-6 条能力，这份给**全部**（或指定的域）。

    **为什么要 step_index="strongest"**：契约是后续所有生成的"考试大纲"，
    大纲错了后面全错（今天实测：契约把凭据参数写成 `user: dict`，
    引擎供给不了 → 8 轮写码全白写）。这一步值得用最强档。

    `max_caps` 用于**小范围试跑**（先花 ¥0.1 验证流程和质量，再放开）。
    """
    from . import blueprint as _bp
    from . import contractgen as _cg
    from . import slicer as _sl
    ws = Path(ws)
    if bp is None:
        bp = _bp.load(ws)
    caps = [c.id for c in (getattr(bp, "caps", None) or [])]
    if cap_ids:
        keep = set(cap_ids)
        caps = [c for c in caps if c in keep]
    if max_caps and len(caps) > max_caps:
        caps = caps[:max_caps]
    if not caps:
        return {"ok": False, "problems": ["没有可写的能力（蓝图为空？）"]}

    _emit(emit, "full_contract_gen", {"caps": len(caps), "step": str(step_index)})
    try:
        sl = {"id": "full", "caps": caps}
        brief_text = _sl.brief(sl, bp, ws=ws)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "problems": ["简报构造失败：%s: %s"
                                          % (type(exc).__name__, exc)]}
    try:
        got = _cg.synthesize(brief_text, task_id="full-contract",
                             step_index=step_index, thinking="on",
                             ambiguity="decide")
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "problems": ["契约合成异常：%s: %s"
                                          % (type(exc).__name__, exc)]}
    if not got.get("ok"):
        return {"ok": False, "problems": got.get("problems") or ["契约合成失败"],
                "usage": got.get("usage")}

    text = str(got.get("text") or "")
    if not text.strip():
        return {"ok": False, "problems": ["契约文本为空"], "usage": got.get("usage")}
    # 接线约束必须带（§11.4k：远端不知道"路由层能供给什么参数"）
    try:
        from . import auto as _auto
        text = _auto._wire_constraints(text)
    except Exception:                                    # noqa: BLE001
        pass
    # 落盘 + 锁定
    lock = ws / CONTRACT_LOCK
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(text, encoding="utf-8")
    except OSError as exc:                               # noqa: BLE001
        return {"ok": False, "problems": ["锁定文件写不了：%s" % exc]}

    # 自检：契约里解析出多少「端点 → 函数」
    n_routes = 0
    try:
        n_routes = len(_rf.parse(text))
    except Exception:                                    # noqa: BLE001
        n_routes = 0
    n_funcs = 0
    try:
        from . import contract as _cm
        cs = _cm.parse_requirement(text)
        n_funcs = sum(len(getattr(c, "funcs", None) or []) for c in cs.values())
    except Exception:                                    # noqa: BLE001
        n_funcs = 0
    _emit(emit, "full_contract_gen_done",
          {"chars": len(text), "routes": n_routes, "funcs": n_funcs,
           "usage": got.get("usage") or {}})
    return {"ok": True, "text": text, "path": str(lock), "chars": len(text),
            "routes": n_routes, "funcs": n_funcs, "caps": len(caps),
            "usage": got.get("usage")}


def _cache_hit(ws) -> tuple[float, int]:
    """最近一次远端调用的缓存命中率（0-1, prompt_tokens）。查不到返回 (-1, 0)。"""
    try:
        import sqlite3
        from pathlib import Path as _P
        db = _P(__file__).resolve().parents[1] / "data" / "proto.db"
        con = sqlite3.connect(str(db))
        cur = con.cursor()
        cur.execute("SELECT prompt_tokens, cache_hit_tokens FROM remote_usage "
                    "WHERE prompt_tokens>0 ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        con.close()
        if not row or not row[0]:
            return -1.0, 0
        return (row[1] or 0) / row[0], int(row[0])
    except Exception:                                    # noqa: BLE001
        return -1.0, 0


def detect(ws, bp=None, contracts=None, *, only_e2e=None, skip=None,
           emit=None, ui_base: str = "", ui_only=None,
           screenshot_dir=None) -> _rep.Report:
    """阶段 2：全量检测 → 报告（**只读，不改产物**）。

    `ui_base` 给了才跑 D7（界面）；没给就不跑 —— 没起前端服务时
    "界面红"是环境问题，不该冒充"界面写错了"。
    """
    ws = Path(ws)
    if skip is None:
        skip = ("d6",) if ui_base else ("d6", "d7")
    if screenshot_dir is None and ui_base:
        screenshot_dir = ws / ".sixcoder" / "screenshots"
    errs = _det.run_all(ws, bp, contracts, only_e2e=only_e2e, skip=skip,
                        emit=emit, ui_base=ui_base, ui_only=ui_only,
                        screenshot_dir=screenshot_dir)
    rep = _rep.Report(ws)
    rep.load()                     # 只为复用历史编号
    rep.add_many(errs)
    rep.adopt_ids()
    return rep


def _group_by_file(rep: _rep.Report) -> dict[str, list]:
    """把错误按**文件**分组（派单粒度 = 文件；引擎一次重写一个文件）。"""
    out: dict[str, list] = {}
    for e in rep.sorted():
        where = str(e.where or "")
        rel = where.split(":")[0].strip()
        if not rel.endswith(".py"):
            # 端点类错误（`POST /api/...`）→ 归到 e2e，由"接线的下一个文件"或全局修
            out.setdefault("（端到端，不属于单文件）", []).append(e)
            continue
        out.setdefault(rel, []).append(e)
    return out


def repair(ws, rep: _rep.Report, contracts=None, *, emit=None,
           max_files: int = 6, attempts: int = 3, whole: bool = True,
           round_no: int = 1, total_rounds: int = 1,
           diagnose_only: bool = False) -> dict:
    """阶段 3：把**整个仓库 + 整仓权限**交给远端（默认），或按文件派单（兜底）。

    两种口径的差别就是用户 2026-11-15 定稿的那句话：
      · 整仓（`whole=True`，默认）：远端一次看到**全仓源码**，可以改**任何**文件，
        先归因（深层共同因）再改根因、最后才治孤立小问题；
      · 按文件（`whole=False`）：只给 `max_files` 个红文件的错误清单。
        跨文件的问题在这里**结构上看不见** —— 所以只当兜底。
    """
    from . import author as _author
    ws = Path(ws)

    if whole:
        from . import wholerepo as _whole
        _extra = ""
        if diagnose_only:
            _extra = ("本轮**只做诊断，不许改任何文件**：把 `<rootcause>` 写到你认为"
                      "足以定位全部 P0 的程度，然后只输出 `=== DONE ===`。"
                      "下一轮你会在整仓权限下动手。")
        r = _whole.session(ws, rep, emit=emit, contracts=contracts,
                           round_no=round_no, total_rounds=total_rounds,
                           extra=_extra)
        _emit(emit, "full_repair_whole",
              {"ok": r.get("ok"), "written": len(r.get("written") or []),
               "deleted": len(r.get("deleted") or []),
               "rootcause": bool(r.get("rootcause")),
               "done": r.get("done"), "detail": str(r.get("detail") or "")[:200]})
        if r.get("ok"):
            # 成功（含"远端自认完成、本轮无改动"）→ **不回落**按文件。
            # 回落等于把同一批红再按文件派一遍：同一份报告花两次钱，
            # 而且按文件根本改不动跨文件的根因。
            return {"files": list(r.get("written") or []),
                    "repaired": list(r.get("written") or []),
                    "whole": True, "detail": r.get("detail"),
                    "rootcause": r.get("rootcause"), "done": r.get("done")}
        _emit(emit, "notice",
              {"message": "整仓会话未落地，回落到按文件修：%s"
                          % str(r.get("detail") or "")[:200]})

    groups = _group_by_file(rep)
    # 只派**有 P0/P1 的文件**（P2 量大，先把必红的清掉）
    todo: list[tuple[str, list]] = []
    for rel, errs in groups.items():
        if not rel.endswith(".py"):
            continue
        if any(e.level in ("P0", "P1") for e in errs):
            todo.append((rel, errs))
    # 按"最严重"排序，取前 max_files 个（一次改太多会互相冲突）
    todo.sort(key=lambda x: (min(_rep.P_ORDER.get(e.level, 9) for e in x[1]), x[0]))
    todo = todo[:max_files]

    done: list[str] = []
    for rel, errs in todo:
        if not (ws / rel).is_file():
            continue
        brief = _render_file_errors(rel, errs)
        _emit(emit, "full_repair", {"file": rel, "errors": len(errs),
                                     "levels": sorted({e.level for e in errs})})
        try:
            r = _author.author_module(
                ws, rel, (contracts or {}).get(rel), contracts or {},
                evidence=brief, attempts=attempts, emit=emit)
            if r and (r.get("ok") or r.get("applied")):
                done.append(rel)
        except Exception as exc:                         # noqa: BLE001
            _emit(emit, "notice", {"message": "修复跳过 %s：%s: %s"
                                   % (rel, type(exc).__name__, exc)})
    return {"files": [x[0] for x in todo], "repaired": done}


def _render_file_errors(rel: str, errs: list, limit: int = 20) -> str:
    """一个文件的错误清单（给远端的 evidence）。"""
    lines = ["## 检测器报出的问题（只改这些，不要重写无关部分）", ""]
    for e in sorted(errs, key=lambda x: _rep.P_ORDER.get(x.level, 9))[:limit]:
        lines.append("- [%s/%s] %s" % (e.level, e.kind, e.where))
        lines.append("  问题：%s" % (e.what or ""))
        if e.evidence:
            ev = str(e.evidence).strip().splitlines()
            lines.append("  证据：%s" % (ev[0][:300] if ev else ""))
            for ln in ev[1:4]:
                lines.append("        %s" % ln[:300])
        lines.append("")
    if len(errs) > limit:
        lines.append("（另有 %d 条同级问题，先修上面这些）" % (len(errs) - limit))
    return "\n".join(lines)


def _wire(ws, bp, emit=None) -> list[str]:
    """接线（复用带快照+冒烟保护的 `openhouse.wire`，点名表自动兜底）。"""
    if bp is None:
        return []
    try:
        from . import openhouse as _oh
        rf = {}
        try:
            rf = _rf.from_workspace(ws)
        except Exception:                                # noqa: BLE001
            rf = {}
        return _oh.wire(Path(ws), bp, emit=emit, route_funcs=rf) or []
    except Exception as exc:                             # noqa: BLE001
        _emit(emit, "notice", {"message": "接线跳过：%s: %s" % (type(exc).__name__, exc)})
        return []


def run(repo, *, emit=None, max_rounds: int = 6, only_e2e=None,
        detect_only: bool = False, from_scratch: bool = False,
        max_files: int = 6, refresh_contract: bool = False,
        mode: str = "auto", ui_base: str = "",
        per_file: bool = False) -> dict:
    """跑完整流程。返回 {ok, rounds, report, verdict, done, total}。

    `per_file=False`（默认）= **整仓会话**：远端每轮拿到整个仓库 + 整仓写权限，
    先归因后修改（见 `core/wholerepo.py` 的模块注释）。
    `per_file=True` 是旧行为（按错误文件派单，一次最多 `max_files` 个），
    只在整仓会话不可用（仓太大/远端连续失败）时做兜底。
    """
    # 进程级栈：一次设定，后续所有 `_stack()`（不带 ws 的几十处调用）都用它。
    # 不设 = 恒落回 fastapi（Django 项目会补出 `router = APIRouter()` 之类）。
    try:
        from . import stack as _stk
        _stk.activate(repo)
    except Exception:                                    # noqa: BLE001
        pass
    ws = Path(repo)
    t0 = time.time()

    # ── 阶段 0：蓝图 + 全量契约 ────────────────────────────────────────────
    bp = None
    contracts = {}
    try:
        from . import blueprint as _bp
        bp = _bp.load(ws)
        _emit(emit, "full_blueprint", {"caps": len(getattr(bp, "caps", []) or [])})
    except Exception as exc:                             # noqa: BLE001
        _emit(emit, "notice", {"message": "蓝图加载失败：%s: %s"
                               % (type(exc).__name__, exc)})
    # **两种场景自动判定**（用户 2026-11-14 定稿）：
    #   空项目（模块 < 3）→ 直接重新写：分域契约 → 三层生成 → 检测/修
    #   已有仓库        → 分析优化：蓝图 + 报告，只改有错的地方
    _mode = str(mode or "auto").lower()
    if _mode == "auto":
        _mode = "scratch" if from_scratch else decide_mode(ws)
    if from_scratch:
        _mode = "scratch"
    _emit(emit, "full_mode", {"mode": _mode,
                              "modules": len(_det.list_modules(ws))})
    try:
        from . import contract as _cm
        # **锁定契约**（一次生成、之后不变）→ 前缀稳定 → 缓存命中 + 消掉命名漂移
        _lock = ws / CONTRACT_LOCK
        _has = _lock.is_file() and bool(_lock.read_text(encoding="utf-8-sig",
                                                       errors="replace").strip())
        if _mode == "scratch" and (refresh_contract or not _has):
            # 空项目 / 首次：分域生成（单次输出短、不会超时；某域失败不影响其它域）
            _g = generate_contract_by_domain(ws, bp, emit=emit)
            if not _g.get("ok"):
                _emit(emit, "notice", {"message": "契约生成失败：%s"
                                       % (_g.get("problems") or [])[:1]})
            text = _lock_contract(ws, refresh=False, emit=emit)
        else:
            text = _lock_contract(ws, refresh=bool(refresh_contract), emit=emit)
        contracts = _cm.parse_requirement(text) if text else {}
        _emit(emit, "full_contract", {"modules": len(contracts),
                                      "funcs": sum(len(c.funcs) for c in contracts.values())})
    except Exception as exc:                             # noqa: BLE001
        _emit(emit, "notice", {"message": "契约解析跳过：%s: %s"
                               % (type(exc).__name__, exc)})

    # ── 阶段 1：（可选）三层分批生成 ───────────────────────────────────────
    if _mode == "scratch":
        _emit(emit, "full_generate_start", {"layers": [x[0] for x in LAYERS]})
        from . import author as _author
        for name, fixed in LAYERS:
            paths = _layer_targets(ws, bp, name, fixed)
            if not paths:
                continue
            _made = _ensure_module_files(ws, paths)
            if _made:
                _emit(emit, "full_layer_files", {"layer": name, "created": _made[:8]})
            _emit(emit, "full_layer", {"layer": name, "files": len(paths)})
            try:
                _author.author_modules(ws, paths, contracts, emit=emit)
            except Exception as exc:                     # noqa: BLE001
                _emit(emit, "notice", {"message": "层 %s 生成跳过：%s: %s"
                                       % (name, type(exc).__name__, exc)})
            # 批间只做语法+导入（秒级、零 token）
            _det.d1_syntax(ws)
            _det.d2_import(ws)

    # ── 阶段 2-4：检测 → 报告 → 修 → 判据 ─────────────────────────────────
    history: list[dict] = []
    rep: _rep.Report | None = None
    verdict = None
    done_n, total_n = -1, -1
    diagnose_only = False          # 由 converge 的 switch_strategy 置位（见下）
    for rnd in range(1, max_rounds + 1):
        # 接线要先做：报告里的路由/端点类错误依赖真实注册表。
        # **但 `detect_only` 模式是纯只读**（只出报告，不许动产物）——
        # 否则"看一眼报告"就会改坏别人的工作区。
        if not detect_only:
            _wire(ws, bp, emit=emit)

        rep = detect(ws, bp, contracts, only_e2e=only_e2e, emit=emit,
                     ui_base=ui_base)
        done_n, total_n = _e2e_counts(ws, rep, only_e2e)
        st = rep.stats()
        verdict = _conv.judge(history, fatal=st["fatal"], done=done_n, total=total_n)
        _ch, _pt = _cache_hit(ws)
        _emit(emit, "full_round", {"round": rnd, "errors": st["total"],
                                   "p0": st["P0"], "p1": st["P1"],
                                   "done": done_n, "total": total_n,
                                   "cache_hit": round(_ch, 3) if _ch >= 0 else -1,
                                   "action": verdict.action, "why": verdict.why[:160]})
        rep.save()
        (ws / ".sixcoder").mkdir(parents=True, exist_ok=True)
        (ws / ".sixcoder" / "errors_report.md").write_text(
            rep.render(), encoding="utf-8")

        if verdict.action in ("done", "stop_human"):
            break
        if verdict.action == "switch_strategy":
            # **换打法（2026-11-15 改口径）**：整仓会话下不再靠"少改几个文件"降风险
            # （那只治症状），而是换**归因视角**：让远端先只做诊断、不许改代码，
            # 下一轮再带着这份诊断动手。缩小批次的做法已废弃 —— 跨文件问题
            # 正是被"少给文件"挡住的（用户定稿）。
            _emit(emit, "full_switch", {"strategy": "diagnose_only",
                                        "why": "P0+P1 连续不降 → 本轮只诊断不改码"})
            diagnose_only = True
        if detect_only:
            break

        repair(ws, rep, contracts, emit=emit, max_files=max_files,
               whole=(not per_file), round_no=rnd, total_rounds=max_rounds,
               diagnose_only=diagnose_only)
        diagnose_only = False
        history.append({"fatal": st["fatal"], "p0": st["P0"], "done": done_n})

    return {"ok": bool(verdict and verdict.action in ("done", "continue")),
            "rounds": len(history) or 1,
            "report": str(ws / ".sixcoder" / "errors_report.md"),
            "stats": (rep.stats() if rep else {}),
            "verdict": (verdict.as_dict() if verdict else {}),
            "done": done_n, "total": total_n,
            "mode": _mode,
            "elapsed_s": round(time.time() - t0, 1)}


def _e2e_counts(ws, rep, only_e2e) -> tuple[int, int]:
    """从账本读端到端 done/total（**不重复跑探针**，用 coverage 最近一条）。"""
    try:
        import json
        p = Path(ws) / ".sixcoder" / "coverage.jsonl"
        rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        if rows:
            last = rows[-1]
            return int(last.get("done") or 0), int(last.get("total") or 0)
    except Exception:                                    # noqa: BLE001
        pass
    return -1, -1
