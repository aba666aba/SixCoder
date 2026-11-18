"""开工三件事 + 规模推导（§8.2 / §8.3 / §8.4）—— **甲方模式与全自主模式共用**。

2026-11-10 的由来（真实事故，不是设计想象）：
甲方模式（`company.night`）长期**绕开 blueprint 这条线**，于是：
  · 写出来的模块**路由接不上**（契约只验函数、不验路由 → 35/35 全过但端到端 13/84）
  · 端到端红灯**没人知道**（甲方模式从不跑能力覆盖，账本停在 16:34）
  · 完成判据**只看模块契约**（齐了就收工）

根因就是"开工三件事"（骨架树 / 接口锁定 / 扩展点）只接在 `night_auto` 上。
本模块把那段**抽成共享函数**，两条模式走同一份真相源 —— 复制两份必然漂移。

这里全是**零 token 的确定性事实**：看仓库现状、锁接口、验扩展点、推规模。
越早算越能挡住后面的无效切片。

用法：
    from . import openhouse
    openhouse.prepare(ws, bp, emit=emit)
"""

from __future__ import annotations

import time
from pathlib import Path


def target_lines(goal: str) -> int:
    """从需求原文读目标代码行数（声明优先）。"""
    from . import auto as _auto
    try:
        return int(_auto._target_lines(goal) or 0)
    except Exception:                                    # noqa: BLE001
        return 0


def prepare(ws: Path, bp, emit=None, llm=None) -> dict:
    """开工段：规模推导 → 骨架树 → 接口锁定 → 扩展点验收 → 规模扩产。

    `bp` 必须已保存（blueprint.save 已在调用方做过）。返回各段结果供报告使用。
    """
    ws = Path(ws)
    out: dict = {"target_lines": 0, "scale": None, "tree": None,
                 "interfaces": 0, "extpoints": {}, "extended": 0,
                 "injected": [], "http_modules_filled": 0}

    # ---- 0) 机制注入 + HTTP 能力补落点（auto 原有，甲方模式同享）----
    # 鉴权拦截 / 页面可打开 / 数据真落盘 —— 这三类是"能上线"的硬要求，
    # 但远端模型经常"忘了做"，于是系统给绿灯、交付物却不能用（实测 2026-11-07）。
    try:
        from . import shipgate as _ship0
        _inj = _ship0.inject_caps(bp, getattr(bp, "goal", "") or "", ws=ws)
        if _inj:
            out["injected"] = list(_inj)
            bp.notes.append("机制注入：" + "、".join(_inj))
            # 注入的能力是按 FastAPI 写的（app/main.py、app/api/*.py、$${...}），
            # 立刻按当前栈归一，否则这一轮就有一批注定失败的探针。
            try:
                from . import blueprint as _bp_norm
                _nn = _bp_norm.normalize_injected(bp, ws=ws)
                if _nn and emit:
                    emit("caps_normalized", {"changed": _nn})
            except Exception as exc:                 # noqa: BLE001
                if emit:
                    emit("notice", {"message": "注入能力归一失败：%s" % type(exc).__name__})
            if emit:
                emit("caps_injected", {"caps": _inj})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"机制注入跳过：{type(exc).__name__}"[:160]})
    try:
        from . import blueprint as _bp_mod
        if _bp_mod.fill_http_modules(bp, ws=ws):
            out["http_modules_filled"] = 1
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"HTTP 落点补齐跳过：{type(exc).__name__}"})

    # ---- 1) 规模推导 + 层数规划（§8.2）----
    _target0 = target_lines(getattr(bp, "goal", "") or "")
    out["target_lines"] = _target0
    try:
        from . import plan_scale as _ps
        _avg_funcs = int(sum(c.funcs for c in bp.caps) / max(1, len(bp.caps)))
        _psr = _ps.plan(bp.goal, ws, caps=len(bp.caps), funcs=_avg_funcs)
        _target0 = int(_psr.get("target") or 0) or _target0
        out["target_lines"] = _target0
        out["scale"] = _psr
        if emit:
            emit("plan_scale", {"target": _target0, "source": _psr.get("source"),
                                "layers": (_psr.get("layers") or {}).get("layers"),
                                "band": (_psr.get("layers") or {}).get("band"),
                                "freeze": (_psr.get("layers") or {}).get("freeze"),
                                "conflict": _psr.get("conflict"),
                                "estimated": (_psr.get("estimate") or {}).get("estimated")})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"规模推导跳过：{type(exc).__name__}"})

    # ---- 2) 骨架树（§8.2：先看仓库，再画蓝图）----
    try:
        from . import blueprint_tree as _bt
        _tree = _bt.build(ws, target_lines=_target0)
        _bt.save(ws, _tree)
        out["tree"] = _tree
        if emit:
            emit("skeleton", {"lines": _tree["root"]["lines"],
                              "public": _tree["root"]["public"],
                              "subsystems": {n["id"]: n["lines"] for n in _tree["nodes"]},
                              "checks": _tree["checks"],
                              "gap": _bt.gaps(_tree, _target0)["gap"] if _target0 else 0,
                              "target": _target0})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"骨架树跳过：{type(exc).__name__}: {exc}"[:150]})

    # ---- 3) 接口清单锁定（§8.4：接口只能走显式通道改）----
    try:
        from . import interfaces as _ifc0
        if not _ifc0.load(ws):
            _ifc0.save(ws)
            if emit:
                emit("interface_locked", {"modules": len(_ifc0.load(ws)), "at": "开工"})
        out["interfaces"] = len(_ifc0.load(ws))
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"接口锁定跳过：{type(exc).__name__}"})

    # ---- 4) 扩展点验收（§8.3）----
    try:
        from . import archrules as _ar0, extpoints as _ep0
        _epr = _ep0.verify_all(ws, _ar0.load_spec(ws))
        out["extpoints"] = _epr
        if _epr.get("count") and emit:
            emit("extpoints", {"ok": _epr["ok"], "count": _epr["count"],
                               "problems": _epr["problems"][:3]})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"扩展点验收跳过：{type(exc).__name__}"})

    # ---- 5) 规模扩产（做完一轮不能报 100% 收工：目标写着 N 行就得有 N 行）----
    if _target0:
        try:
            from . import scale as _scale0
            _prod0 = int((_scale0.count_lines(ws) or {}).get("prod_lines") or 0)
            _all_done = all(c.status == "done" for c in bp.caps)
            if _all_done and _prod0 < _target0 * 0.8:
                from . import blueprint as _bp_mod
                _llm = llm or _bp_mod._default_llm
                _need = max(2, min(8, int((_target0 - _prod0) / 120) or 2))
                _got = _bp_mod.extend(bp, bp.goal, need=_need, llm=_llm, ws=ws)
                if _got.get("added"):
                    bp.notes.append(f"规模扩产：+{len(_got['added'])} 条能力（目标 {_target0} 行，"
                                    f"现有 {_prod0} 行）")
                    out["extended"] = len(_got["added"])
                    if emit:
                        emit("blueprint_extended", {"added": _got["added"], "target": _target0,
                                                    "lines": _prod0})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"规模扩产跳过：{type(exc).__name__}"})
    return out


def _snapshot_files(ws: Path) -> dict:
    """接线前给可能被改的文件拍快照（bytes）。

    **同时记住"接线前不存在"的文件**（§11.4m.1）：接线会**新建** `app/main.py`
    与 `app/__init__.py`（`write_web_entry`）—— 回滚时若只恢复已存在的，
    这些**新建出来的半成品会留在仓库里** → 下一次接线基于它继续错。
    所以快照里记 `None` = 接线前不存在 → 回滚时**删掉**。
    """
    out: dict = {}
    cands = []
    try:
        from . import auto as _auto
        cands = list(_auto._candidate_modules(ws))
    except Exception:                                    # noqa: BLE001
        pass
    for rel in cands + ["app/main.py", "app/__init__.py"]:
        p = Path(ws) / rel
        if p.is_file():
            try:
                out[rel] = p.read_bytes()
            except OSError:
                pass
        else:
            out[rel] = None                            # 接线前不存在 → 回滚时删掉
    return out


def _restore_files(ws: Path, snap: dict) -> int:
    """把快照恢复回去（接线把 app 弄坏时用）。

    快照值是 `None` 的 = 接线前**不存在**这个文件 → 回滚时**删掉**（§11.4m.1）。
    """
    n = 0
    for rel, data in (snap or {}).items():
        p = Path(ws) / rel
        if data is None:
            # 接线新建的文件 → 删掉，回到"接线前不存在"的状态
            try:
                if p.is_file():
                    p.unlink()
                    n += 1
            except OSError:
                pass
            continue
        try:
            p.write_bytes(data)
            n += 1
        except OSError:
            pass
    return n


def _emit_note(emit, kind: str, payload: dict) -> None:
    """发事件但不许因为 emit 本身出错而中断接线。"""
    if emit is None:
        return
    try:
        emit(kind, payload)
    except Exception:                                    # noqa: BLE001
        pass


def wire(ws: Path, bp, emit=None, route_funcs: dict | None = None) -> list[str]:
    """接线：把能力表里期望的路由真正挂到产物引擎段（`apply_web_wiring` + 刷新入口）。

    为什么甲方模式必须做这件事：
    契约判据只验**函数**，不验**路由**。远端把处理函数写对了、判据全过，
    但路由没挂上去 → 端到端 404 → 甲方模式却认为"这个模块做完了"。
    实测：`web_routes.json` 停在 2026-11-09 16:34，之后 4 轮甲方夜班
    写出的新模块路由**一个都没接上**。

    接线是**纯机械动作**（AST 定位 + 重渲染），零 token。

    **顺序很关键**：先接线（决定哪些模块真的有路由）→ 再刷新入口 `app/main.py`
    （按接线产物生成 include 列表）。反过来做会 include 一堆不存在的模块，
    结果一个路由都没注册（实测 2026-11-10：全线 404，71 项红灯）。

    **接线后必须冒烟**：接线会重渲染各模块的路由段、刷新入口 ——
    弄坏的话整个工作区导不进来，**所有**能力一起变红（实测：某一轮 pct 0.0）。
    所以：接线前拍快照 → 接线 → `import app.main` 冒烟 → 坏了就恢复快照。

    **接线后还要复验"已通过的能力"**（2026-11-11 实测，§11.4m）：
    接线会**批量重写所有接线文件**（实测一次提交改了 38 个文件），
    而这些改动**不经过作者写码路径的回归守卫**（守卫只覆盖作者写的那一个文件）。
    实测（切片 s33）：接线把 `app/api/topics.py` 的路由换成引擎渲染版 →
    该文件承载的 `cap.topic.create_draft` / `cap.inv.topic.no_neg_quota_3f14` 一起变红
    → **引擎自己打坏已通过的能力**（切片级 `regression` 事件实证）。
    口径：接线前记下"当前已 done 的能力" → 接线后**只复验这些**（探针，零 token）
    → 有变红的就**恢复快照**（本次接线不采用）→ 记 `wire_reverted` 事件。
    """
    # 进程级栈：一次设定，后续所有 `_stack()`（不带 ws 的几十处调用）都用它。
    # 不设 = 恒落回 fastapi（Django 项目会补出 `router = APIRouter()` 之类）。
    try:
        from . import stack as _stk
        _stk.activate(ws)
    except Exception:                                    # noqa: BLE001
        pass
    ws = Path(ws)
    # **Django 项目走独立接线路径**（2026-11-15）。
    # 下面全部逻辑都建立在 FastAPI 的装饰器 / include_router / 请求体模型上，
    # 对 Django 的 `path()` URLconf 完全不适用（硬套会把 urls.py 写坏）。
    # `django_wire` 自带"快照 → 写 → manage.py check → 失败回滚"守卫，
    # 满足铁律"每个会写盘的机制都必须自带复验"。
    try:
        from . import django_wire as _djw
        if _djw.is_django(ws) and _djw.app_url_map(ws):
            _touched = _djw.wire(ws, bp, route_funcs=route_funcs, emit=emit)
            _emit_note(emit, "django_wire_done",
                       {"touched": len(_touched), "files": _touched[:8]})
            return _touched
    except Exception as exc:                             # noqa: BLE001
        _emit_note(emit, "notice",
                   {"message": "Django 接线异常：%s: %s" % (type(exc).__name__, exc)})

    snap = _snapshot_files(ws)
    # 接线前的"已通过能力"基线（接线后只复验这些；探针，零 token）
    _baseline: set[str] = set()
    try:
        for _c in (getattr(bp, "caps", []) or []):
            if str(getattr(_c, "status", "")) == "done":
                _baseline.add(str(_c.id))
    except Exception:                                    # noqa: BLE001
        _baseline = set()
    # 接线**真改了哪些文件**（空 = 没改 → 不用复验，省掉几十秒探针时间）
    _touched: list[str] = []
    try:
        from . import auto as _auto
        from . import stack as _st
        # §20.7 接缝 3：栈特有骨架（settings.py / urls.py / manage.py）必须在
        # 接线之前落盘，否则 Django 的 get_asgi_application() 找不到 ROOT_URLCONF，
        # 入口 import 就炸。FastAPI 适配器是 no-op（老行为零变化）。
        try:
            _sc = _st.current(ws).scaffold(ws)
            if emit and _sc.get("wrote"):
                emit("stack_scaffold", {"files": _sc.get("files") or [],
                                        "why": str(_sc.get("why") or "")[:120]})
        except Exception as exc:                     # noqa: BLE001
            if emit:
                emit("notice", {"message": f"栈骨架跳过：{type(exc).__name__}"})
        _wired = _auto.apply_web_wiring(ws, bp, route_funcs=route_funcs, touched=_touched)
        # 入口必须跟着接线一起刷新：include 列表要按**实际有路由的模块**生成
        try:
            _entry = _auto.write_web_entry(ws, bp)
            if emit and _entry.get("wrote"):
                emit("web_entry", {"file": "app/main.py",
                                   "why": str(_entry.get("why") or "")[:120]})
        except Exception as exc:                     # noqa: BLE001
            if emit:
                emit("notice", {"message": f"入口刷新跳过：{type(exc).__name__}"})
        # 接线后冒烟：入口导不进来 = 整个工作区废了
        try:
            from . import gates as _g
            _sm = _g.gate_import_smoke(ws)
            if not _sm.ok:
                _restored = _restore_files(ws, snap)
                if emit:
                    emit("wire_reverted", {
                        "restored": _restored,
                        "detail": str(getattr(_sm, "detail", "") or "")[:300],
                        "note": "接线把入口弄坏了 → 已恢复快照（本轮不采用这次接线）"})
                return []
        except Exception as exc:                     # noqa: BLE001
            if emit:
                emit("notice", {"message": f"接线冒烟跳过：{type(exc).__name__}"})
        # **接线回归复验**（§11.4m）：接线批量重写文件，可能把已通过的能力打坏。
        # 只复验**承载在被改文件上的**已 done 能力（探针，零 token）
        # → 有变红的就恢复快照（本次接线不采用）。
        # 没改任何文件 → 不复验（省掉几十秒探针时间）。
        if _baseline and _touched:
            _tset = {str(x).replace("\\", "/") for x in _touched}
            _scope: set[str] = set()
            try:
                # ① 直接承载：落点落在被改文件上的已 done 能力
                _owner: dict[str, set[str]] = {}
                for _c in (getattr(bp, "caps", []) or []):
                    if str(getattr(_c, "status", "")) != "done":
                        continue
                    if str(getattr(_c, "id", "")) not in _baseline:
                        continue
                    _mods = {str(m).replace("\\", "/")
                             for m in (getattr(_c, "modules", []) or [])}
                    for _m in _mods:
                        _owner.setdefault(_m, set()).add(str(_c.id))
                for _m in _tset:
                    _scope |= _owner.get(_m, set())
                # ② **依赖被改文件的能力**（§11.4m.1 修 4）：
                # 跨文件影响 —— 被改文件承载的能力 A 变红，会连坐**依赖 A** 的能力 B。
                # 只守①会漏掉这一类（B 的落点文件根本没被改，但它的前置坏了）。
                _deps_of: dict[str, set[str]] = {}
                for _c in (getattr(bp, "caps", []) or []):
                    if str(getattr(_c, "status", "")) != "done":
                        continue
                    if str(getattr(_c, "id", "")) not in _baseline:
                        continue
                    for _d in (getattr(_c, "deps", []) or []):
                        _deps_of.setdefault(str(_d), set()).add(str(_c.id))
                _q = list(_scope)
                while _q:
                    _cur = _q.pop()
                    for _nxt in _deps_of.get(_cur, set()):
                        if _nxt not in _scope:
                            _scope.add(_nxt)
                            _q.append(_nxt)
            except Exception:                            # noqa: BLE001
                _scope = set(_baseline)
            if _scope:                                  # 收窄后为空 = 无从复验
                try:
                    _cov = observe(ws, bp, emit=None, only=set(_scope))
                    _caps_now = (_cov or {}).get("caps") or {}
                    _now_done = {str(k) for k, v in _caps_now.items()
                                 if str((v or {}).get("status") or "") == "done"}
                    _lost = sorted(_scope - _now_done)
                except Exception as exc:                 # noqa: BLE001
                    _lost = []
                    if emit:
                        emit("notice", {"message": f"接线复验跳过：{type(exc).__name__}"})
                if _lost:
                    _restored = _restore_files(ws, snap)
                    if emit:
                        emit("wire_reverted", {
                            "restored": _restored,
                            "regression": _lost[:8],
                            "touched": sorted(_tset)[:8],
                            "note": "接线把已通过的能力改坏了（%d 条）→ 已恢复快照，"
                                    "本轮不采用这次接线" % len(_lost)})
                    return []
        if emit and _wired:
            emit("web_wiring", {"routes": len(_wired), "sample": _wired[:5]})
        return list(_wired or [])
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"接线跳过：{type(exc).__name__}"})
        return []


def observe(ws: Path, bp, emit=None, only=None, timeout: int = 0) -> dict:
    """端到端能力观测（§三「机械验收：判据 + 闸门 + 端到端业务流」）。

    返回 coverage 字典（每条能力真实状态 + 证据）。**不写账本** ——
    记账由调用方决定（甲方模式要写，全自主模式自己写）。
    """
    try:
        from . import coverage as _covmod
        from . import probe as _probe
        _to = int(timeout or _probe.PROBE_TIMEOUT)
        # **观测前先把判据自洽化**（零 token、幂等）。
        # 为什么必须每轮都做：重规划会改写能力表，探针的 expect_json 可能又和
        # 它自己发的请求对不上（实测 2026-11-10：注册 u_xxx 却断言返回 stu1）。
        # 这种判据永远红 → 引擎把"修产品"的工作单反复派给远端 → 覆盖停在 17/84
        # 却每轮烧钱（实测一轮 201 次调用 ≈ ¥3.07）。
        try:
            from . import blueprint as _bpmod
            _n = _bpmod.align_probe_expectations(bp)
            if _n:
                _bpmod.save(bp, Path(ws))
                if emit:
                    emit("probe_aligned", {"fields": _n,
                                           "note": "探针期望值自洽化（判据自相矛盾会让覆盖永远红）"})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"判据自洽化跳过：{type(exc).__name__}"})
        _cov = _covmod.run(bp, Path(ws), only=only, emit=emit, timeout=_to)
        return _cov
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"能力观测跳过：{type(exc).__name__}"})
        return {}


def module_candidates(ws, path: str) -> list[str]:
    """按端点路径给出**所有**可能的落点模块（存在的那些），按优先级排。

    为什么不是只给一个（实测 2026-11-10）：
    `/api/v1/auth/login` 的候选是 `app/api/auth.py`（存在，**引擎接线生成**）
    和 `app/authz.py`（甲方模式契约点名的**业务层**）。
    只返回第一个 → 红灯落到路由文件上，而远端改路由文件没用（下次接线就覆盖）
    → 契约层的工作单永远派不出去。两层都要给，让契约层那个能落上。
    """
    import re as _re
    raw = str(path or "")
    if not raw:
        return []
    parts = [x for x in raw.strip("/").split("/")
             if x and not x.startswith("$") and not x.startswith("{")]
    skip = {"api", "rest", "internal"}
    parts = [x for x in parts if x.lower() not in skip and not _re.fullmatch(r"v\d+", x.lower())]
    seg = parts[0] if parts else ""
    if not seg:
        return []
    out: list[str] = []
    _segs = [seg]
    if seg.endswith("s") and len(seg) > 3:
        _segs.append(seg[:-1])          # adjustments → adjustment
    for _sg in _segs:
        for m in (f"app/api/{_sg}.py", f"app/{_sg}.py",
                  f"app/api/{_sg}s.py", f"app/{_sg}s.py"):
            if (Path(ws) / m).is_file() and m not in out:
                out.append(m)
    # 兜底：命名对不上时的包含关系匹配（`auth` ↔ `authz`、`explain` ↔ `explanations`…）
    # **始终跑**，不只在完全没匹配时 —— `app/api/auth.py` 匹配上了但契约层叫
    # `app/authz.py`，只返回第一个就永远落不到契约层的工作单上。
    for _base in ("app", "matching_core"):
        _d = Path(ws) / _base
        if not _d.is_dir():
            continue
        for _f in sorted(_d.glob("*.py")):
            _stem = _f.stem
            if _stem == seg or (len(seg) >= 4 and (seg in _stem or _stem in seg)):
                _rel = f"{_base}/{_f.name}"
                if _rel not in out:
                    out.append(_rel)
    return out


def module_for_path(ws, path: str) -> str:
    """按**单个端点路径**解析落点模块（文件必须已存在），解析不到返回空。

    规则：取资源段（跳过 api / v1..v9 / rest），优先 `app/api/<资源>.py`，
    其次 `app/<资源>.py`，再试复数变体。

    为什么按**路径**而不是按能力表的 modules（实测 2026-11-10）：
    能力表按"路由文件"规划落点（`app/api/analytics.py`），
    而甲方模式的契约点名的是**扁平业务模块**（`app/analytics.py`）——
    两套体系只有 4 个 `matching_core/*.py` 重叠。
    直接拿 caps.modules 去匹配契约键，71 项红灯里只有 4 项能落到工作单上。
    按端点路径反查才对得上：`/api/v1/analytics/heat` → `app/analytics.py`。

    （原在 `core/auto.py:_module_for_path`，2026-11-10 挪到这里 ——
      auto 与甲方模式共用同一份，避免两处漂移。）
    """
    _c = module_candidates(ws, path)
    return _c[0] if _c else ""


def cap_paths(cap) -> list[str]:
    """一条能力的探针链里涉及的所有端点路径。"""
    pr = getattr(cap, "probe", None) or {}
    out: list[str] = []
    if pr.get("path"):
        out.append(str(pr["path"]))
    for s in (pr.get("steps") or []):
        if isinstance(s, dict) and s.get("path"):
            out.append(str(s["path"]))
    return out


def endpoints_for_module(bp, ws, module: str) -> list[str]:
    """这个模块**该负责**哪些端点（`"POST /api/v1/analytics/xxx"` 清单）。

    为什么要显式告诉远端（2026-11-10 实锤）：
      · 能力表的端点是**产品语义**：`quota-suggestions` / `recommendations` / `logs`
      · 契约里的函数是**算法语义**：`quota_advice` / `recommend_adjustment` / `append_audit`
    两套是**独立的需求理解**，名字对不上 → 引擎靠字符串猜必然接错或接不上
    （实测 20 个端点"无匹配函数"）。

    引擎的字符串规则有天花板（`_pick_func` 要求末段出现在函数名里），
    但**语义映射远端做得更好** —— 它同时看得见需求和契约。
    所以把端点清单直接告诉远端，让它用 FastAPI 装饰器注册
    （`auto._route_handler_from_src` 优先读产物里的装饰器 → 接线自动用上）。
    """
    out: list[str] = []
    if bp is None:
        return out
    for c in bp.caps:
        # **降级的能力不接线**（2026-11-15）：它们被显式放弃（同一概念两个名字），
        # 但端点收集没过滤 → 每轮接线都去给它们找处理函数 → 十几条 "接线跳过" 噪声，
        # 还会把不存在的能力算进接线覆盖率（看起来该接 85 条，其实 79 条）。
        if str(getattr(c, "status", "")) == "descoped":
            continue
        pr = getattr(c, "probe", None) or {}
        if str(pr.get("kind") or "") != "http":
            continue
        _paths = []
        if pr.get("path"):
            _paths.append((str(pr.get("method") or "GET"), str(pr["path"])))
        for st in (pr.get("steps") or []):
            if isinstance(st, dict) and st.get("path"):
                _paths.append((str(st.get("method") or "GET"), str(st["path"])))
        for m, p in _paths:
            # 这个端点的落点是不是这个模块（含全部候选）
            if module in module_candidates(ws, p):
                _s = f"{m} {p}"
                if _s not in out:
                    out.append(_s)
    return out


def is_wiring_layer(rel: str) -> bool:
    """这个落点是不是**接线层**（引擎拥有，远端不该在这里写业务代码）。

    两套落点体系（实测 2026-11-11，56 条红灯里 **52 条**落在接线层）：

    | 层 | 路径形态 | 所有者 | 远端能写吗 |
    |---|---|---|---|
    | **接线层** | `app/api/<资源>.py` | **引擎**（`ensure_web_router` 整块重渲染引擎段） | ❌ 写了会被冲掉 |
    | **业务层** | `app/<资源>.py`、`app/services/<资源>.py`、`matching_core/*.py` | **远端** | ✅ |

    能力表按接线层规划落点（`fill_http_modules`），但**业务代码要写在业务层** ——
    否则必然撞上「承载已通过能力的文件不许整文件重写」的护栏：
    契约让远端在 `app/api/topics.py` 里加服务层函数，而那个文件
    ① 承载已通过能力 → 护栏不许整文件重写；
    ② 引擎每次接线都重渲染它 → 远端写在里面的东西可能被冲掉。
    **两道锁叠在一起 = 死锁**（实测表现为「修好 0 条，仍红 4 条（空转）」）。

    甲方线一直用的是业务层落点（`app/<资源>.py`）—— 两套体系本来就并存，
    是切片线（`slicer`）用了接线层那一套。
    """
    r = str(rel or "").replace("\\", "/")
    return r.startswith("app/api/") and r.endswith(".py")


def writable_modules(rel: str, modules=None) -> list[str]:
    """**远端该写的地方**（切片契约的模块清单用这个）。

    口径（§11.4e）：接线层（`app/api/*`）是**引擎拥有**的文件 ——
    引擎每次接线都整块重渲染它，远端写进去的东西会被冲掉；
    而且它通常承载已通过能力 → 触发「不许整文件重写」的护栏 → 两道锁叠成死锁。

    所以：**有业务层落点时，契约只列业务层**（远端在那里写业务代码，路由由引擎挂）；
    只有当**全部落点都是接线层**（该能力还没有业务层对应文件）时，
    才退回接线层 —— 否则这条能力就没人写了。

    ⚠️ 与 `ordered_modules` **不是一回事**（别混）：
    · `writable_modules` = **写哪儿**（给契约用，远端只写这些）
    · `ordered_modules` = **先做哪个**（给派单用，全部落点都保留，只排优先级）
    一个函数两种语义，就是今天这个 bug 的同款错误（记账/判定混用）。
    """
    mods = [str(m) for m in (modules or [])]
    biz = [m for m in mods if not is_wiring_layer(m)]
    if biz:
        return biz
    return [m for m in mods if is_wiring_layer(m)]


def ordered_modules(rel: str, modules=None) -> list[str]:
    """落点**排序**（派单候选用这个）：业务层在前，接线层在后，**全部保留**。

    为什么接线层也要保留：它虽然不该被远端写，但它是"路由挂哪儿"的登记，
    也是"该能力还没有业务层文件"时唯一的落点 —— 直接丢掉会让能力没人做。

    **确定性**：同一层内**按名字排序**。调用方常常传的是 `set`（`cap_red` 就是 set），
    集合迭代顺序不保证 → 不排序就会"同一批随机挑走不同的模块"，
    派单顺序在轮与轮之间抖动（自检 `check_cap_drive` 钉的就是这条）。
    """
    mods = [str(m) for m in (modules or [])]
    biz = sorted({m for m in mods if not is_wiring_layer(m)})
    wire = sorted({m for m in mods if is_wiring_layer(m)})
    return biz + wire


def _is_common_prep_path(path: str) -> bool:
    """这个端点是不是**公共前置**（认证 / 造种子数据），而不是能力自己的落点。

    为什么要有（实测 2026-11-11）：几乎每条 http 能力的探针链都以
    `POST /api/v1/auth/register` 开头（有的还带 `POST /api/v1/datasets/seed`
    造数据）。把这些前置端点也拿去反查落点，落点集合就被 auth 淹没 ——
    实测 56 条红灯里 32 条被污染，auth 模块落点频次冲到 33（全场最高），
    引擎于是一轮轮派单给早满分的 auth 模块 → 空转。

    判据只看**路径本身**：`/auth/` 段 + 种子数据端点。
    """
    s = str(path or "").lower()
    if not s:
        return True
    if "/auth/" in s or s.endswith("/auth"):
        return True
    if "/datasets/seed" in s:
        return True
    return False


def red_modules(cov: dict, bp=None, ws=None) -> tuple[set[str], dict]:
    """红灯能力 → 需要动工的模块集合 + 每模块的失败详情。

    这是"契约全过但端到端不通"的桥：能力表的每条都带 `modules`，
    说清这条能力落在哪些文件上 —— 于是"端到端红了"能直接变成工作单。

    **但要注意两套路径体系**（实测 2026-11-10）：
      · 能力/接线层：`app/api/auth.py`（复数 + api 子目录，**引擎接线生成的路由文件**）
      · 契约/业务层：`app/authz.py`（单数 + 扁平，**远端写的业务模块**）
    两套只有 4 个 `matching_core/*.py` 重叠 —— 直接拿 caps.modules 去匹配契约键，
    71 项红灯里只有几项能落到工作单上。

    所以这里按**端点路径**反查（`module_for_path`）：`/api/v1/analytics/heat`
    → `app/analytics.py`（契约层落点，远端能改的那个）。

    返回 (模块集合, {模块: [失败详情…]})。
    """
    mods: set[str] = set()
    detail: dict[str, list[str]] = {}
    caps = (cov or {}).get("caps") or {}
    for cid, c in caps.items():
        if not isinstance(c, dict):
            continue
        if str(c.get("status") or "") == "done":
            continue
        # 失败详情给工作单用：不能只截前 300 —— 2026-11-18 的"请求/实际响应"段在后面，
        # 一刀切 [:300] 会把契约对照（产物返回什么字段）吃掉，远端又只能瞎猜。
        # 分段保留：主摘要 + 请求/响应段各自限长，总额可控。
        _raw_d = str(c.get("detail") or "")
        _MARK = "【失败步骤的请求"
        if _MARK in _raw_d:
            _head, _tail = _raw_d.split(_MARK, 1)
            _d = _head[:300] + "\n" + _MARK + _tail[:1200]
        else:
            _d = _raw_d[:300]
        _cap = None
        if bp is not None:
            try:
                _cap = bp.by_id(cid)
            except Exception:                            # noqa: BLE001
                _cap = None
        _land: set[str] = set()
        # ① 能力表点名的模块（可能已是契约层，如 matching_core/*.py）
        for m in (list(getattr(_cap, "modules", None) or []) if _cap is not None else []):
            _land.add(str(m))
        # ② 按端点路径反查业务模块（契约能改的落点）
        #
        # **必须跳过公共前置端点**（实测 2026-11-11，毕设项目）：
        #   几乎每条能力的探针链第一步都是 `POST /api/v1/auth/register`
        #   （还有 `/api/v1/datasets/seed` 造种子数据）—— 那是**公共前置**，
        #   不是这条能力自己的落点。把它们也拿去反查，
        #   于是 56 条红灯里有 **32 条的落点集合被 auth 污染**：
        #   `app/api/auth.py` / `app/authz.py` 落点频次冲到 33（全场最高），
        #   引擎每轮都把工作单派给**早就契约满分的 auth 模块** → 远端重写 →
        #   改坏别的能力 → 回滚 → 空转（实测 8.5 小时净增 0 条、状态倒退 4 次）。
        if ws is not None and _cap is not None:
            for _p in cap_paths(_cap):
                if _is_common_prep_path(_p):
                    continue
                for _m in module_candidates(ws, _p):
                    _land.add(_m)
        # ③ 路由文件 import 的业务模块（路由已存在时）
        for m in list(_land):
            for _biz in _biz_modules(ws, m):
                _land.add(_biz)
        for m in _land:
            mods.add(m)
            detail.setdefault(m, []).append(f"[{cid}] {_d}")
    return mods, detail


def actionable_modules(mods, ws=None) -> list[str]:
    """落点集合 → **真正该派工给远端**的落点（业务层优先，接线层降级）。

    口径（§11.4e 文件所有权）：**远端只写业务层**。
    接线层（`app/api/*`）是引擎接线重渲染的地盘 —— 远端在那里写的东西
    下次接线就可能被整块重渲染（只保留作者自己注册的装饰器路由，
    其余辅助代码会被冲掉）。

    实测代价（2026-11-11 12:11）：能力表把落点规划在 `app/api/*`，
    `red_modules` 因此把**接线层**当成"要动工的模块"→
    甲方线 `cap_orders` 派单给 `app/api/people.py` →
    远端在引擎拥有的文件里写 22KB 业务适配 →
    与切片线的业务层实现**抢同一个落点**（`app/services/people.py`）→
    两处实现互相覆盖 → `teachers_me_route` 签名在两套契约里不一致
    （契约要 `account`，产物是 `authorization`）→ 卡死 4 次 → order_failed。

    规则：
      ① 业务层落点 → **保留**（远端拥有，能写）；
      ② 接线层落点 → **降级**成它 import 的业务层（`_biz_modules`）；
      ③ 接线层找不到业务层 → **剔除**（那是引擎接线层的事，远端写不了）。

    单一实现：`company.cap_orders`（甲方线派单）与切片线共用同一套所有权判定。
    """
    out: list[str] = []
    for m in sorted(str(x) for x in (mods or ())):
        if not is_wiring_layer(m):
            if m not in out:
                out.append(m)
            continue
        for b in _biz_modules(ws, m):
            if not is_wiring_layer(b) and b not in out:
                out.append(b)
    return out


def actionable_details(mods, detail: dict | None, ws=None) -> dict[str, list[str]]:
    """降级后的落点 → 失败证据（把原始接线层落点的证据带过来）。

    为什么要有：`actionable_modules` 会把 `app/api/people.py` 降级成
    `app/services/people.py`，但失败详情是按**原始落点**记的
    （`cap_detail` 的 key 是 red_modules 的原始集合）——
    不合并的话，降级后的工作单会**没有失败证据**，
    远端拿不到"为什么红"，只能瞎猜。
    """
    detail = detail or {}
    out: dict[str, list[str]] = {}
    for raw in sorted(str(x) for x in (mods or ())):
        _ev = list(detail.get(raw) or [])
        if not _ev:
            continue
        out.setdefault(raw, []).extend(_ev)
        if not is_wiring_layer(raw):
            continue
        for b in _biz_modules(ws, raw):
            if not is_wiring_layer(b):
                out.setdefault(b, []).extend(_ev)
    return out


def _biz_modules(ws, rel: str) -> list[str]:
    """一个路由文件 import 了哪些**业务层**模块（契约能改的落点）。

    `app/api/auth.py` → `from app.authz import ...` → 业务模块 `app/authz.py`。
    路由文件本身是引擎接线重渲染出来的，远端改它会被下一次接线覆盖。
    """
    if ws is None:
        return []
    try:
        src = (Path(ws) / rel).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    import re as _re
    for m in _re.finditer(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\b", src, _re.M):
        _pkg = m.group(1)
        if not _pkg.startswith("app") and not _pkg.startswith("matching_core"):
            continue
        _p = _pkg.replace(".", "/")
        for _cand in (f"{_p}.py", f"{_p}/__init__.py"):
            if (Path(ws) / _cand).exists():
                out.append(_cand)
                break
    return out
