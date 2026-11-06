"""迭代驱动器：不问"一次能不能生成可用代码"，只保证"每一轮之后项目更好"。

循环体（每轮）：
  1. 体检：读契约 + 扫描产物 → 每个函数的真实状态（missing / stub / implemented）
            + 机械判据结果（props from_contract）
  2. 分诊：本地机械三诊（workorder.triage）出工作单；
           若配置了远端，则把证据发给"架构师"（远端）拿根因 + 工作单（最多 5 张）
  3. 执行：本地模型逐张执行工作单（一次只改一个符号），落盘走 libcst 保真渲染
  4. 验收：机械判据重跑，记账（progress），关闭工作单
  5. 终止：没有可开的单，或本轮 verified 不再增长

用法：
  python cli.py iterate --repo <dir> --requirement-file <f> [--rounds 3] [--remote]
"""
from __future__ import annotations

from . import tokens as _tokens_mod  # noqa: E402

import ast
import json
import os
import re
import time
from pathlib import Path

from . import contract as contract_mod
from . import db, gates, ollama, props, symedit, tools, workorder


def ensure_scaffold(ws: Path, contracts: dict) -> list[str]:
    """按契约确定性生成缺失模块的骨架（全部函数桩），供工作单去填。

    没有这一步，迭代第一轮所有工作单都会因为目标文件不存在直接失败（实测）。
    """
    from . import skeleton as sk
    created = []
    for path, c in contracts.items():
        p = ws / path
        if p.exists():
            continue
        sigs = []
        for f in c.funcs:
            params = [sk.Param(name=p_.name,
                               type=(p_.type if p_.type and p_.type != "object" else "object"),
                               default=p_.default)
                      for p_ in f.params]
            sigs.append(sk.Sig(name=f.name, params=params,
                               ret=(f.returns or "object"),
                               # **不许切到 60/80 字**：实测计算器契约里 pow 的说明
                               # "…不支持分数指数与负数底数的非整数次幂" 被切在"负数底"，
                               # 远端照着坏 docstring 写成"负数底一律 ValueError"——截断
                               # 直接把语义搞丢了，而且坏 docstring 还留在交付产物里。
                               doc=_tokens_mod.cut(f.doc, 300),
                               is_class=False))
        # 类 API（双模式）：契约里是 class X，就渲染成类骨架；否则平铺函数骨架。
        _cls = next((f.owner_class for f in c.funcs if getattr(f, "owner_class", "")), "")
        _plain = [f for f in c.funcs if not getattr(f, "owner_class", "")]
        if not sigs:
            continue
        # 模块 docstring 以前切 80 字，实测切在注解中间（`add(a: fl"""`）→ 语法坏掉、
        # 之后每一轮都在修这个坏的桩。与 author.render_skeleton 统一用 tokens.cut(300)。
        _contract_txt = _tokens_mod.cut(c.raw, 300)
        if _cls and sigs:
            body = sk.render_class_skeleton(path, _cls, sigs, _contract_txt)
        else:
            body = sk.render_skeleton(path, sigs, _contract_txt)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        created.append(path)
    return created


# ------------------------------------------------------------------ 体检
def _single_func_contract(c, f):
    """只保留一个函数的契约副本（逐函数验收用）。"""
    try:
        return contract_mod.Contract(path=c.path, module=c.module, funcs=[f], raw=c.raw)
    except Exception:                                # noqa: BLE001
        return c


def _sig_text(contract, name: str) -> str:
    """把契约里某个函数的签名渲染成文本（给 autofix 补参数用）。

    为什么不用 workorder 的 acceptance 文案：那是"契约判据通过（upsert）"这种自然语言，
    里面没有任何参数信息，于是 `autofix` 的补参数器从来没被触发过 —— 夜班两次
    抽到 `upsert() takes 1 positional argument`，而契约里 `upsert(conn, rows: list[dict])`
    写得清清楚楚。这里直接读解析出来的 Contract，参数名与注解都是权威值。
    """
    for f in (getattr(contract, "funcs", None) or []):
        if f.name != name:
            continue
        parts = []
        for p in (getattr(f, "params", None) or []):
            pname = str(getattr(p, "name", "") or "")
            if not pname or pname in ("self", "cls"):
                continue
            ptype = str(getattr(p, "type", "") or "")
            parts.append(f"{pname}: {ptype}" if ptype and ptype != "object" else pname)
        ret = str(getattr(f, "returns", "") or "")
        return f"{name}({', '.join(parts)})" + (f" -> {ret}" if ret else "")
    return ""


def _signature_problem(contract, source: str, name: str) -> str:
    """静态判签名：契约声明的参数**缺失**，或多出**必填**参数 → 返回问题描述。

    只判"调用一定会炸"的两种：
      · 契约要 `upsert(conn, rows)`，产物只有 `upsert(conn)` → 少参数（TypeError）；
      · 产物比契约多一个必填参数 → 调用方按契约写就 TypeError。
    多出的**可选**参数（带默认值）放行，那是向后兼容的增强，不该判死。
    """
    for f in (getattr(contract, "funcs", None) or []):
        if f.name != name:
            continue
        want_names = [str(getattr(p, "name", "") or "")
                      for p in (getattr(f, "params", None) or []) if getattr(p, "name", "")]
        want_names = [p for p in want_names if p not in ("self", "cls")]
        if not want_names:
            return ""
        actual: list[str] | None = None
        required = 0
        try:
            tree = ast.parse(source or "")
        except SyntaxError:
            return ""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                args = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
                actual = [a.arg for a in args if a.arg not in ("self", "cls")]
                required = len(args) - len(node.args.defaults)
                required += sum(1 for d in node.args.kw_defaults if d is None)
                break
        if actual is None:
            return ""
        missing = [p for p in want_names if p not in actual]
        if missing:
            return (f"签名缺参数：契约要求 {name}({', '.join(want_names)})，"
                    f"产物是 {name}({', '.join(actual)})，缺 {', '.join(missing)}（调用必 TypeError）")
        if required > len(want_names):
            return (f"签名多出必填参数：契约是 {name}({', '.join(want_names)})，"
                    f"产物是 {name}({', '.join(actual)})，调用方按契约写必 TypeError")
        return ""
    return ""


def _func_state(source: str) -> tuple[set[str], set[str]]:
    """返回 (已定义符号, 其中是桩的符号)。"""
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return set(), set()
    have, stubs = set(), set()

    def _mark(node) -> None:
        have.add(node.name)
        try:
            if gates._is_stub(node):
                stubs.add(node.name)
        except Exception:                            # noqa: BLE001
            pass

    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _mark(n)
        elif isinstance(n, ast.ClassDef):
            # 类 API（双模式）：方法也要算"已定义/仍是桩"，否则整个类看不出有没有干活
            have.add(n.name)
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _mark(m)
    return have, stubs


_E2E_CACHE: dict[str, tuple] = {}


def _briefing_enabled() -> bool:
    """任务简报开关（默认开：让模型写"这次要做什么"，规则骨架仍归引擎）。

    SIXCODER_BRIEFING=0 关闭 → 提示词与旧版完全一致，用于 A/B 对照。
    """
    import os
    return os.environ.get("SIXCODER_BRIEFING", "1") != "0"


def _e2e_evidence(ws: Path, contracts: dict) -> dict:
    """跑一次跨模块业务流并缓存（按文件 mtime 失效）：inspect 会被反复调用。"""
    from . import e2e
    key = str(Path(ws).resolve())
    sig = []
    for path in contracts:
        p = Path(ws) / path
        sig.append((path, p.stat().st_mtime_ns if p.exists() else 0))
    sig_t = tuple(sig)
    hit = _E2E_CACHE.get(key)
    if hit and hit[0] == sig_t:
        return hit[1]
    try:
        res = e2e.write(ws, contracts)
    except Exception as exc:                             # noqa: BLE001
        res = {"ok": True, "skipped": True, "reason": f"业务流判据出错：{type(exc).__name__}"}
    _E2E_CACHE[key] = (sig_t, res)
    return res


def _known_keys(c) -> set[str]:
    """"契约承认存在的字段名" = 表列 + 契约示例里出现过的字典键。

    这是字段幻觉闸门的白名单来源：模型只要用别的键名，基本就是凭空造的。
    """
    keys: set[str] = set()
    raw = str(getattr(c, "raw", "") or "")
    try:
        keys |= {name for name, _ in props._table_cols(raw)}
    except Exception:                                # noqa: BLE001
        pass
    for f in getattr(c, "funcs", []):
        for ex in getattr(f, "examples", []):
            for arg in getattr(ex, "args", []):
                keys |= _dict_keys_in_text(str(arg))
        for keys_attr in (getattr(f, "keys", []) or []):
            keys.add(str(keys_attr))
    return {k for k in keys if re.match(r"^[A-Za-z_]\w*$", k)}


def _dict_keys_in_text(text: str) -> set[str]:
    """从示例参数文本里抠出字典键（`[{"age": 10, "email": "a@x.com"}]` → {age, email}）。"""
    out: set[str] = set()
    try:
        obj = ast.literal_eval(text)
    except Exception:                                # noqa: BLE001
        return out

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if isinstance(k, str):
                    out.add(k)
                walk(v)
        elif isinstance(node, (list, tuple)):
            for x in node:
                walk(x)

    walk(obj)
    return out


def inspect(ws: Path, contracts: dict[str, contract_mod.Contract],
            specs: dict | None = None) -> dict:
    """逐模块体检：符号状态 + 契约判据通过情况。

    specs: 可选 {path: oracle_spec} —— §13.4 第 2 步 oracle 升级的硬判据规格。
    提供时，每个模块的验收会并入 oracle.evaluate 的计算校验/性质/复杂度/e2e/变异结果，
    实测不达标的部分作为硬失败计入 fails（整模块判红）。
    """

    state: dict = {}
    # 工作区实测规模（oracle 升级的判据开关按级别走）
    try:
        from . import scale as _scale_i
        _level = _scale_i.resolve("", Path(ws))["level"]
    except Exception:                            # noqa: BLE001
        _level = "S"
    # 共享文件的示例（同一个 csv 先写后读）逐函数跑必然失败 → 逐函数时跳过，
    # 由模块级 seq 判据按顺序覆盖（见 props.file_pipeline_checks）。
    _shared = props.shared_artifact_exprs(contracts)
    # 字段白名单必须是**全契约并集**：api.py 这类编排模块本身没有示例，只看自己
    # 会把 student 的 bonus 判成"凭空造字段"（实测毕设双选系统 0/44 的主因之一）。
    _all_keys: set[str] = set()
    for _c in contracts.values():
        try:
            _all_keys |= _known_keys(_c)
        except Exception:                                # noqa: BLE001
            pass
    try:
        from . import project as _proj_k
        _all_keys |= _proj_k.global_keys(contracts, _proj_k.load_map(ws))
    except Exception:                                    # noqa: BLE001
        pass
    for path, c in contracts.items():
        p = ws / path
        src = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
        have, stubs = _func_state(src)
        verified = set()
        untested: list[str] = []
        notes: list[str] = []          # 要人看、但**不算失败**的提示（不进 fails/ok 判定）
        engine_err = False             # 判据自身跑不动（引擎问题，不是产物问题）
        _e_note = "引擎判据自身没跑起来（探针无输出/输出不可解析）——这不是产物错，是引擎的问题"
        fails: list[str] = []
        # 显式记录"机械判据已经判红的函数"。不能只靠 fails 文本里搜名字：
        # fails 只留前 4 条，靠后的失败会被截断，e2e 就会把红函数重新算成绿的。
        hard_failed: set[str] = set()
        passed_n = total_n = 0
        typed = []
        try:
            typed = props.typed_checks(src, exclude=stubs)
        except Exception:                            # noqa: BLE001
            typed = []
        # 属性驱动：按注解派生边界矩阵（空/None/缺字段/超长/类型错）——只验示例会漏掉空输入崩溃
        try:
            # 契约声明了会抛异常的输入，要从"不许崩"判据里剔除（改由 raises 判据覆盖）。
            # 不这么做就是两条判据互斥：div(1,0) 既要抛 ZeroDivisionError 又不许崩。
            _must_raise: dict[str, set[str]] = {}
            for _f in c.funcs:
                if not _f.raises:
                    continue
                # 声明了会抛异常的函数：数值参数取 0 时**不许**再要求"不崩"。
                # 否则 div(1, 0) 既要抛 ZeroDivisionError、又被要求不崩 —— 正确实现必红
                # （计算器那次 6 轮同一状态就是这么来的）。
                if any(str(getattr(p, "type", "")).lower().endswith(
                        ("int", "float", "object")) for p in (_f.params or [])):
                    _must_raise[_f.name] = {"0", "0.0", "0.00"}
                _ptype = (_f.params[0].type if _f.params else "")
                # 同样是"单参数才造探针"：多参数只给一个实参是缺参调用，不是非法输入
                _probes = (props._invalid_probes(_f.name, getattr(c, "raw", "") or "", _ptype)
                           if len(_f.params) == 1 else []) or []
                if _probes:
                    _must_raise.setdefault(_f.name, set())
                    _must_raise[_f.name] |= {str(x).strip() for x in _probes}
            boundary = props.boundary_checks(src, exclude=stubs, must_raise=_must_raise)
        except Exception:                            # noqa: BLE001
            boundary = []
        for f in c.funcs:
            if f.name not in have or f.name in stubs:
                continue                       # 桩/缺失不算验收
            # 签名先判：参数缺了/多了必炸，这不需要跑模型，也不该等到端到端才现形
            sig_bad = _signature_problem(c, src, f.name)
            if sig_bad:
                fails.append(sig_bad)
                hard_failed.add(f.name)
                continue
            sub = props.from_contract(_single_func_contract(c, f), _shared)
            if not sub:
                sub = [x for x in typed if str(x.get("expr", "")).startswith(f"m.{f.name}(")]
            sub = sub + [x for x in boundary if str(x.get("expr", "")).startswith(f"m.{f.name}(")]
            # 契约声明了会抛异常，但没有任何 raises 判据（多参数函数造不出安全探针）→ 如实提示
            if f.raises and not any(x.get("kind") == "raises" and
                                    str(x.get("expr", "")).startswith(f"m.{f.name}(")
                                    for x in sub):
                # 建议要按真实签名造：以前一律写 `f(1, 0)`，对单参数函数
                # （clean_row(row) / parse(argv)）是指**缺参调用**的建议 —— 胡说。
                _sample = []
                for _p in (f.params or [])[:3]:
                    _t = str(getattr(_p, "type", "") or "").lower()
                    _sample.append("''" if "str" in _t else
                                   "[]" if _t.startswith("list") else
                                   "{}" if "dict" in _t else "0")
                _txt = ", ".join(_sample) or "0"
                _note = (f"{f.name} 声明会抛 {'/'.join(f.raises)}，但契约没给出可判输入；"
                         f"建议补一条示例（例：{f.name}({_txt}) 应当抛 {f.raises[0]}）")
                # **提示不是失败**：以前 append 进 fails 会把整模块判红，
                # 正确的实现被挡在门外，还进了 order_failed 的原因里。
                if _note not in notes:
                    notes.append(_note)
            if not sub:
                untested.append(f.name)        # 无机械判据 → 不给信用
                continue
            r = props.run(ws, path, sub)
            passed_n += int(r.get("passed") or 0)
            total_n += int(r.get("total") or 0)
            if r.get("engine_error"):
                # 判据自己跑不动（探针没输出/输出不是 JSON）→ **不许算成产物错**。
                # 实测：探针路径 bug 让 5/5 的正确产物被判 0/5，夜班空转 6 轮远端调用。
                engine_err = True
                untested.append(f.name)
                if _e_note not in notes:
                    notes.append(_e_note)
            elif r.get("ok"):
                verified.add(f.name)
            else:
                hard_failed.add(f.name)
                fails += [str(x.get("detail") if isinstance(x, dict) else x)
                          for x in (r.get("fails") or [])][:1]
        # 模块级判据：数据库写→读端到端、共享文件的按序读写。
        # 这两类在单函数视图里永远看不到（前者要真连接，后者要先写后读），必须单独跑。
        try:
            mod_checks = [x for x in props.from_contract(c, _shared)
                          if x.get("kind") in ("db_end2end", "seq", "expr_eq", "expr_seq")]
            mod_checks += [x for x in props.file_pipeline_checks({path: c})
                           if x not in mod_checks]
        except Exception:                            # noqa: BLE001
            mod_checks = []
        for chk in mod_checks:
            r = props.run(ws, path, [chk])
            passed_n += int(r.get("passed") or 0)
            total_n += int(r.get("total") or 0)
            covers = [nm for nm in (chk.get("covers") or
                                    [chk.get("writer"), chk.get("reader")]) if nm]
            if r.get("ok"):
                for nm in covers:              # 端到端跑通 → 这一串函数都有机械证据
                    if nm in have and nm not in stubs and \
                            not any(nm in str(x) for x in fails):
                        verified.add(nm)
                        if nm in untested:
                            untested.remove(nm)
            elif r.get("engine_error"):
                engine_err = True
                if _e_note not in notes:
                    notes.append(_e_note)
            else:
                detail = str((r.get("fails") or [{}])[0].get("detail") or "") \
                    if isinstance((r.get("fails") or [{}])[0], dict) else \
                    str((r.get("fails") or [""])[0])
                fails.append(detail)
                if chk.get("kind") in ("seq", "expr_seq", "expr_eq"):
                    # 按序判据失败是**精确证据**：它点名了哪一步的返回值不对，
                    # 所以这一步涉及的函数必须失去信用（否则"类型冒烟"会给它兜底发绿）。
                    # 实测：write_csv 写成 return 0（应为 2），模块照样 3/3 通过 —— 假绿。
                    _blamed = [nm for nm in covers if nm and nm in detail] or covers
                    for nm in _blamed:
                        if nm in verified:
                            verified.discard(nm)
                        if nm in have and nm not in stubs:
                            hard_failed.add(nm)
                            if nm not in untested:
                                untested.append(nm)
                # db_end2end 失败**只上报、不剥夺**单函数证据：它证不了"是哪一步坏的"。
                # 一旦摘掉整串信用，分诊会重新派单、执行者把本来正确的函数改坏
                # （night4：8/8 掉到 3/8；night5：create_table 被改回 pass，且无人回滚）。
        # 游标行形态：静态可判、修法唯一（只摘掉被点名的那个函数）
        try:
            dr = gates.gate_db_rows(ws, [path])
            if not dr.ok:
                fails.append(dr.detail)
                for nm in re.findall(r"(\w+)\(\) 对游标行", dr.detail or ""):
                    if nm in verified:
                        verified.discard(nm)
                        untested.append(nm)
                        hard_failed.add(nm)
        except Exception:                            # noqa: BLE001
            pass
        # 可维护性：圈复杂度/嵌套深度/行数（纯 ast，零误判空间）
        try:
            # 第5层：阈值按"规模档位 + 模块角色"取；编排/展示层放宽复杂度与行数
            _lim = None
            try:
                from . import project as _proj5
                from . import scale as _scale5
                _base = _scale5.quality_limits(_scale5.resolve("", ws)["level"])
                _lim = _proj5.limits_for(path, c, {"cx": _base[0], "nest": _base[1],
                                                   "lines": _base[2]})
            except Exception:                            # noqa: BLE001
                _lim = None
            qm = gates.gate_quality_metrics(
                ws, [path],
                max_complexity=int((_lim or {}).get("cx") or 0),
                max_depth=int((_lim or {}).get("nest") or 0),
                max_lines=int((_lim or {}).get("lines") or 0))
            if not qm.ok:
                fails.append(qm.detail)
        except Exception:
            pass
        # 字段名幻觉：函数里下标的键必须是契约知道的键（实测 row['domain'] 这类必错）
        try:
            dk, adv = gates.gate_dict_keys(ws, [path], _all_keys,
                                           getattr(c, "raw", "") or "")
            if not dk.ok:
                fails.append(dk.detail)
                for nm in re.findall(r"::(\w+)\(\) 用了字段", dk.detail or ""):
                    if nm in verified:
                        verified.discard(nm)
                        untested.append(nm)
                        hard_failed.add(nm)
            for extra in adv[:2]:
                # 字段名"契约里提到过但位置对不上"= 需求歧义提示，**不是失败**。
                # 以前进 fails 会把整模块判红：实验台 pipeline 那次 clean_row 用了契约
                # 明确要求的 birth 字段，两版正确实现都被打回，夜班空转到振荡。
                if extra not in notes:
                    notes.append(extra)
        except Exception:                            # noqa: BLE001
            pass
        # 契约函数"整段消失"（被删/改名）必须单独记账：
        # 它既不是桩、也不是判红，旧逻辑于是什么都没记 → 夜班以为没事可做（假绿）。
        missing = [f.name for f in c.funcs if f.name not in have]
        if missing:
            fails.append(f"契约函数缺失：{', '.join(missing)}（文件里根本没有这些函数）")
        # === §13.4 第 2 步：oracle 升级（HARD 判据）===
        # 提供 specs 时，把 oracle.evaluate 的实测结果并入本模块验收：
        # 计算校验/性质/复杂度/e2e/变异中任一可量化的不达标 → 整模块硬红。
        if specs and path in specs:
            try:
                from . import oracle as _oracle
                _or = _oracle.evaluate(ws, specs[path], _level)
                for _f in _or.get("failed") or []:
                    fails.append(f"oracle[{_or.get('module') or path}]: {_f}")
                if _or.get("skipped"):
                    if "oracle 部分判据未能量化（如实说明，不判红）" not in notes:
                        notes.append("oracle 部分判据未能量化（如实说明，不判红）")
            except Exception:                        # noqa: BLE001
                pass
        # docstring 是契约说明的**残句**：引擎以前把 doc 切到 60/80 字，残句还留在交付物里。
        # 实测代价：pow 的"…不支持分数指数与负数底数的非整数次幂"被切成"…负数底"，
        # 远端照着残句写成"负数底一律 ValueError"（契约只排除负数底的**非整数**次幂）。
        # 这不是崩，是语义漂移 → 只记提示，判红会再次引发回滚死循环。
        try:
            _tree2 = ast.parse(src) if src else None
        except SyntaxError:
            _tree2 = None
        if _tree2 is not None:
            _docs = {n.name: (ast.get_docstring(n) or "") for n in _tree2.body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for f in c.funcs:
                got_doc = (_docs.get(f.name) or "").strip()
                want_doc = (f.doc or "").strip()
                if (got_doc and want_doc and len(want_doc) > len(got_doc) + 3
                        and want_doc.startswith(got_doc)):
                    notes.append(f"{f.name} 的 docstring 是契约说明的残句（少 "
                                 f"{len(want_doc) - len(got_doc)} 字，缺："
                                 f"{want_doc[len(got_doc):]}）——实现可能按残句漂移了")
        res = {"passed": passed_n, "total": total_n, "ok": not fails, "fails": fails}
        state[path] = {"exists": p.exists(), "symbols": have, "stubs": stubs,
                       "missing": missing,
                       "funcs": len(c.funcs), "verified": len(verified),
                       "verified_names": sorted(verified),
                       "hard_failed": sorted(hard_failed),
                       "untested": untested,
                       "notes": notes[:4],
                       "engine_errors": engine_err,
                       "fails_all": fails[:6],
                       "checks": {"passed": res.get("passed"), "total": res.get("total")},
                       "fails": [str(f.get("detail") if isinstance(f, dict) else f)
                                 for f in (res.get("fails") or [])][:4]}
    # 跨模块业务流：单元判据证明不了"连起来对不对"，通过后给相关函数补强证据
    ev = _e2e_evidence(ws, contracts)
    covers = list(ev.get("covers") or [])
    if covers:
        owner = {f.name: p for p, c in contracts.items() for f in c.funcs}
        for name in covers:
            path = owner.get(name)
            st = state.get(path)
            if not st or name not in st["symbols"] or name in st["stubs"]:
                continue
            if ev.get("ok") and not ev.get("skipped"):
                # 只有当这个函数**没有**hard 失败时才给 e2e 信用。
                # 实测踩过：night2/night3 的 summarize 在空输入上 ZeroDivisionError（边界判据红），
                # 但 e2e 跑通就把它加回 verified，于是 8/8 里混进一个已知会崩的函数。
                if name in (st.get("hard_failed") or ()):
                    continue
                if any(name in str(x) for x in (st.get("fails") or [])):
                    continue
                if name not in st["verified_names"]:
                    st["verified_names"] = sorted(set(st["verified_names"]) | {name})
                    st["verified"] = len(st["verified_names"])
                if name in st["untested"]:
                    st["untested"].remove(name)
            else:
                # 业务流失败**不剥夺**单函数的既有证据：整条链没打通往往只是某个阶段还缺失，
                # 由 triage 按"桩/缺失"去补即可。这里一旦反过来撤销，
                # 会让"已经对的函数"被反复重写（night4 实测：8/8 掉到 3/8，6 轮原地打转）。
                msg = f"业务流未跑通：{ev.get('reason')}"
                if msg not in st["fails"]:
                    st["fails"] = (st["fails"] + [msg])[:4]
                    st["fails_all"] = (st["fails_all"] + [msg])[:6]
    return state


def last_e2e(ws: Path) -> dict:
    """最近一次业务流结果（供交付报告/追溯矩阵读取，不必重跑）。"""
    hit = _E2E_CACHE.get(str(Path(ws).resolve()))
    return dict(hit[1]) if hit else {}


# ------------------------------------------------------------------ 架构师（远端）
def ask_architect(ws: Path, task_id: str, contracts: dict, state: dict,
                  requirement: str, thinking: str = "off", plan: str = "",
                  want_plan: bool = False, step_index: int = 0) -> tuple[str, list[dict]]:
    """让远端当架构师：只看机械证据，给根因 + 工作单。

    thinking 由调用方按"升级阶梯"决定：第一次 off（便宜）；真卡住了再 on（更会想）。
    实测依据见 docs/模型门槛 §4.0：符号级编辑关思考不掉质量，但开放式诊断有风险，
    所以这件事必须由阶梯控制，不能全局一刀切。

    plan：人给的实现方案（开发者模式）。给了就是**权威**，远端只负责在它里面派工，
    不许另起一套架构 —— 这一档的上限是人，不是远端。
    """
    from . import remote
    ok, why = remote.available()
    if not ok:
        return "", []
    lines = ["# 项目状态（机械体检结果）", ""]
    for path, st in state.items():
        lines.append(f"## {path}: 符号 {sorted(st['symbols'])}；桩 {sorted(st['stubs'])}；"
                     f"契约判据 {st['checks']['passed']}/{st['checks']['total']}")
        for f in st["fails"]:
            lines.append(f"  - 失败：{f}")
    lines.append("")
    lines.append("# 契约（结构化）")
    for path, c in contracts.items():
        lines.append(contract_mod.describe(c))
    lines.append("")
    lines.append("# 需求原文")
    lines.append(_tokens_mod.cut(requirement or "", 2500))
    lines.append("")
    if (plan or "").strip():
        lines.append("# 实现方案（人给的，必须照它派工，不许另起架构）")
        lines.append(_tokens_mod.cut(plan.strip(), 2500))
        lines.append("")
    src_dump = []
    for path in state:
        p = ws / path
        if p.exists():
            src = p.read_text(encoding="utf-8-sig", errors="replace")
            if len(src) < 6000:
                src_dump.append(f"## {path}\n```python\n{src}\n```")
    if src_dump:
        lines.append("# 当前源码（小文件全文）")
        lines.extend(src_dump)
    if want_plan:
        # 甲方模式开局：先要"方案"，再要"工作单"。方案定了，后面每一张单都照它做，
        # 避免系统在实现过程中自己改架构（这种事在夜班里极难事后发现）。
        lines.append("")
        lines.append("# 本轮额外要求（方案优先）")
        lines.append("除了 root_cause 与 orders，JSON 里**必须**再给一个 plan 字段："
                     "一个字符串数组，3~6 条，写清——")
        lines.append("1) 各模块职责与边界；2) 数据怎么在模块之间流动（谁是写方谁是读方）；"
                     "3) 哪些地方最容易出错（要提前防住的边界）。")
        lines.append("plan 要短到能直接照着写代码，不要复述需求。")
    prompt = workorder.WORKORDER_PROMPT + ("\n\n方案优先：见下文【本轮额外要求】。" if want_plan else "") \
        + "\n\n" + "\n".join(lines)
    r = remote.complete(prompt, task_id=task_id, node_id="architect", workspace=str(ws),
                        thinking=thinking, step_index=step_index)
    if not r.get("ok"):
        return "", []
    answer = r.get("answer") or ""
    root, orders = workorder.parse_orders(answer)
    if want_plan:
        plan_txt = workorder.parse_plan(answer)
        if plan_txt:
            root = plan_txt
    return root, orders


# ------------------------------------------------------------------ 执行者（本地）
EXEC_PROMPT = """你是执行者，只能完成**这一张工作单**，只输出 JSON。

格式（严格，不要解释、不要围栏）：
{"edits":[{"op":"replace","symbol":"<<TARGET_SYMBOL>>","code":"def <<TARGET_SYMBOL>>(...):\\n    ..."}]}

铁律：
1. symbol 必须是 <<TARGET_SYMBOL>>（这张单指定的那个），**不要**改任何其它符号。
2. 只输出这一个函数的完整定义；不要输出整文件、不要输出 diff。
3. code 必须含 def 行本身、4 空格缩进、类型注解、docstring。
4. 需要辅助常量/内部函数时写在**函数体内**，不要新增模块级符号。
<<PROTOCOL>>
5. **空输入/空值不许崩**：列表可能为空、字典的键可能缺失、值可能是 None。
   除零（`sum(x)/len(x)`）、直接下标（`row["k"]`）、对 None 做运算都会被边界判据判红。
   先给空/缺省情形一个确定结果（如 `0`、`{}`、`[]`），再算正常情形。
6. **信息不足以下结论时不许猜**：只输出
   {"question":"要问人的一句话","options":["方案A","方案B"],"why":"为什么必须问"}，
   不要输出代码。适用于：契约没定义异常类型/返回字段/边界语义、同一行为有两种合理解释。

【同文件其它内容（只读参考，不许改）】
`python
<<SOURCE>>
`
【目标符号现状（照它的签名写）】
`python
<<TARGET_SOURCE>>
`
【工作单】<<ORDER>>
<<EXTRA>>
<<BRIEFING>>
"""

# 顺序说明（省钱的关键）：DeepSeek 的前缀缓存只认**逐字相同的前缀**。
# 所以提示词按"稳定 → 多变"排：铁律（全局稳定）→ 同文件全文（同一模块内稳定）
# → 目标符号（每单变）→ 工作单与证据（每单变，且重试时只在尾部追加）。
# 以前把【工作单】放在前面，导致同一模块的多张单一上来前缀就分叉，
# 每次都要按"未命中"全价付输入钱 —— 命中价只有 1/50（flash 0.02 vs 1 元/百万）。


def _extract_question(text: str) -> dict:
    """模型选择"不猜、发问"时，从回复里抽 {question, options, why}。"""
    import json as _json
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            data = _json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(data, dict) and str(data.get("question") or "").strip():
            opts = data.get("options") or []
            return {"question": str(data["question"]).strip()[:400],
                    "options": [str(x)[:80] for x in (opts if isinstance(opts, list) else [])][:4],
                    "why": str(data.get("why") or "")[:300]}
    return {}


def _extract_edits(text: str) -> list[dict]:
    """从模型回复里抽 edits（容错：整段 JSON 不合法时逐个完整对象救回来）。"""
    import json as _json
    import re as _re

    t = (text or "").strip()
    m = _re.search(r"```(?:json)?\s*(.*?)```", t, _re.DOTALL)
    if m:
        t = m.group(1).strip()
    cands = [t]
    brace = _re.search(r"\{.*\}", t, _re.DOTALL)
    if brace:
        cands.append(brace.group(0))
    for cand in cands:
        try:
            data = _json.loads(cand)
        except Exception:                              # noqa: BLE001
            continue
        if isinstance(data, dict):
            got = data.get("edits") or data.get("ops") or []
            if isinstance(got, list):
                picked = [e for e in got if isinstance(e, dict)]
                if picked:
                    return picked
    out, stack, in_str, esc = [], [], False, False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            st = stack.pop()
            try:
                obj = _json.loads(t[st:i + 1])
            except Exception:                          # noqa: BLE001
                continue
            if isinstance(obj, dict) and obj.get("symbol") and obj.get("code"):
                out.append(obj)
    return out


def _prompt_safe(src: str) -> str:
    """让"喂给模型的上下文"在 JSON 转义上不出事。

    实测事故（projects/night4/5/6 反复出现）：契约 docstring 里带 dict 字面量，
    例如 `返回 {"count": 人数, ...}`。模型把它抄进 JSON 的 code 字段时，
    要把三引号和内部的双引号全部转义，7B 在这里必炸 —— 输出被截断成
    `... 返回 {`，下游 ParserSyntaxError，一整轮白跑。

    所以上下文里的三引号换单三引号、docstring 内的双引号换单引号：
    信息一点不少（docstring 还是人看得懂的那句话），但不再要求模型做双重转义。
    """
    if not src:
        return src
    out = src.replace('"""', "'''")
    try:
        tree = ast.parse(out)
    except SyntaxError:
        return out
    lines = out.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if not doc:
                continue
            for j in range(node.body[0].lineno - 1, (getattr(node.body[0], "end_lineno",
                                                           node.body[0].lineno) or 1)):
                if j < len(lines):
                    lines[j] = lines[j].replace('"', "'")
    return "\n".join(lines)


def _ensure_body(code: str) -> str:
    """抢救后半成品常常只剩 `def f(...):`，没有函数体 → 补一个 pass 让它能解析。

    注意：这样救回来的是**桩**，闸门会如实判它没过（不会假绿），
    但至少文件是合法的、这一轮记成"这张单失败"，不会把整轮拖成 ParserSyntaxError。
    """
    lines = code.rstrip().splitlines()
    if not lines:
        return code
    last = lines[-1]
    if last.rstrip().endswith(":"):
        indent = " " * (len(last) - len(last.lstrip()) + 4)
        return code.rstrip() + "\n" + indent + "pass\n"
    return code


def _repair_code(code: str) -> list[str]:
    """渲染失败时的确定性抢救（不改模型、只做机械变换），按顺序尝试。"""
    cands = [code.replace('"""', "'''")]
    for cand in list(cands):
        if cand.count("'''") % 2 == 1:
            cut = cand[:cand.rfind("'''")]
            cands.append(cut.rstrip() + "\n")
    cands.append(_ensure_body(code.replace('"""', "'''")))
    for cand in list(cands):
        if cand.count("'''") % 2 == 1:
            cands.append(_ensure_body(cand[:cand.rfind("'''")].rstrip() + "\n"))
    # 删掉整个 docstring（签名与实现保留），信息可在注释里补
    stripped = []
    for cand in cands:
        try:
            tree = ast.parse(cand)
        except SyntaxError:
            continue
        lines = cand.splitlines()
        drop: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(
                        getattr(first, "value", None), ast.Constant) and isinstance(
                        first.value.value, str):
                    for j in range(first.lineno - 1, (first.end_lineno or first.lineno)):
                        drop.add(j)
        if drop:
            stripped.append("\n".join(ln for k, ln in enumerate(lines) if k not in drop))
    out = []
    for cand in cands + stripped:
        if cand not in out:
            out.append(cand)
    return out


def _dictfill_enabled() -> bool:
    """字段级填充开关（默认开；SIXCODER_DICTFILL=0 关闭，用于 A/B 对照）。"""
    import os
    return os.environ.get("SIXCODER_DICTFILL", "1") != "0"


def _examples_text(c, f) -> str:
    """把契约里的示例渲染成"可读的一条"，给字段级填充当口径参考。"""
    lines = []
    for ex in (getattr(f, "examples", None) or [])[:3]:
        lines.append(f"{f.name}({', '.join(ex.args)}) == {ex.expect}")
    return "\n".join(lines) or "（契约没给样例，仍按类型口径写）"


def try_dict_fill(ws: Path, path: str, order: dict, func, contract, model: str,
                  emit=None) -> dict:
    """给"返回 dict"的函数走字段级填充：引擎出骨架，模型只填表达式。

    成功 → 落盘并返回值；失败 → **不改文件**，返回 ok=False，调用方继续走普通符号编辑
    （所以这条路只有加分项，没有副作用）。
    """
    from . import dictfill
    from . import props as _props
    p = tools.safe_path(ws, path)
    source = p.read_text(encoding="utf-8-sig", errors="replace")
    prompt0, sk = dictfill.build_prompt(
        func, getattr(contract, "raw", "") or "", "",
        _examples_text(contract, func))
    last = ""
    fields: dict = {}
    # 重试预算按"这张单是不是因为边界判红才派下来的"给：
    # 实测（tools/exp_dictfill.py，n=3）无条件给 3 次不但没提高边界通过率，
    # 还把每次失败的单变成"多烧 1~2 次模型调用再退回普通路径"。所以：
    #   · 首次实现（单子里没提边界）→ 最多 2 次，够修普通口径错；
    #   · 已被边界判红再派下来 → 3 次，这时候多试才有意义。
    order_text = str(order.get("detail") or "") + str(order.get("acceptance") or "")
    default_tries = "3" if "边界" in order_text else "2"
    tries = max(1, int(os.environ.get("SIXCODER_DICTFILL_TRIES", default_tries)))
    for attempt in range(1, tries + 1):
        # 第二遍起不是"再猜一次"，而是把上一遍**被判红的具体反例**+**针对该报错的改法**塞回去：
        # 实测 summarize 的 avg_age 写成 sum(...)/len(...) 就是空列表除零，
        # 光给 traceback，7B 会换个写法继续崩；给"缺键就用 .get('k')"这种一句改法才动得动。
        if attempt == 1:
            prompt = prompt0
        else:
            bad = "；".join(f'{k} = {str(v)[:60]}' for k, v in (fields or {}).items())[:300]
            prompt = (prompt0
                      + "\n\n【上一版被判红，必须改掉】\n"
                      + f"你上次给的表达式：{bad}\n"
                      + f"崩在：{last[:200]}\n"
                      + f"针对性改法：{_df.advice(last)}")
        try:
            res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                              temperature=0.15, num_predict=700)
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "reason": f"字段级填充调用失败：{type(exc).__name__}"}
        fields = dictfill.parse(res.get("text") or "")
        if not fields:
            last = "模型没给出 fields（只输出 {\"fields\": {...}}）"
            continue
        code = dictfill.fill(sk, fields)
        if not code:
            last = "表达式越界或漏填某个键（只能是一个表达式，不能是语句）"
            continue
        rep = symedit.apply_edits(source, [{"op": "replace", "symbol": func.name, "code": code}])
        if not rep.ok:
            return {"ok": False, "reason": f"字段级填充渲染失败：{rep.reason}"}
        p.write_text(rep.new_source, encoding="utf-8")
        # 立刻验收：先契约判据，再边界判据。不过就回滚，别把坏东西留在文件里。
        try:
            checks = _props.from_contract(_single_func_contract(contract, func))
            r = _props.run(ws, path, checks) if checks else {"ok": True}
            if r.get("ok"):
                bchecks = [x for x in _props.boundary_checks(rep.new_source, hard=True)
                           if str(x.get("expr", "")).startswith(f"m.{func.name}(")]
                if bchecks:
                    r = _props.run(ws, path, bchecks)
        except Exception:                                # noqa: BLE001
            r = {"ok": False}
        if not r.get("ok"):
            p.write_text(source, encoding="utf-8")       # 回滚
            last = str(_props.as_feedback(r))[:240]
            continue
        if emit:
            emit("dictfill", {"symbol": func.name, "path": path, "attempt": attempt,
                              "fields": {k: str(v)[:60] for k, v in fields.items()}})
        return {"ok": True, "applied": rep.applied, "by": "dictfill", "attempt": attempt}
    return {"ok": False, "reason": "字段级填充两遍都未通过：" + (last or "未知")}


def _protocol_block() -> str:
    """本地↔远端分工协议（告诉远端：测试/报告/落盘这些机械活本地做，别重复）。"""
    try:
        from . import protocol
        return protocol.render("edits")
    except Exception:                                    # noqa: BLE001
        return ""


def _build_exec_prompt(order: dict, want: str, target_src: str, source: str,
                       plan: str = "", extra: str = "", brief: str = "") -> str:
    """执行者提示词（本地与远端共用一套）：只有"谁来执行"不同，约束必须完全一致。"""
    if (plan or "").strip():
        try:
            from . import policy as _policy
            extra = (extra or "") + _policy.plan_block(plan)
        except Exception:                                # noqa: BLE001
            pass
    return (EXEC_PROMPT
            .replace("<<ORDER>>", workorder.describe(order) + "\n" + (order.get("detail") or ""))
            .replace("<<TARGET_SYMBOL>>", want or "（新增）")
            .replace("<<TARGET_SOURCE>>", _prompt_safe(target_src) or
                     "（该符号当前不存在，需要新建）")
            .replace("<<SOURCE>>", _prompt_safe(source)[:2000])
            .replace("<<BRIEFING>>", brief or "")
            .replace("<<EXTRA>>", extra or "")
            .replace("<<PROTOCOL>>", _protocol_block()))


def execute_local(ws: Path, order: dict, model: str, attempts: int = 1,
                  emit=None, contract=None, plan: str = "") -> dict:
    path = order["target"]
    p = tools.safe_path(ws, path)
    if not p.exists():
        return {"ok": False, "reason": "目标文件不存在"}
    source = p.read_text(encoding="utf-8-sig", errors="replace")
    extra = ""
    if order["kind"] == "add_import":
        extra = "本单是补 import：直接用 op=add 加一条 `import X` 或 `from X import Y`。"
    want = (order.get("symbol") or "").strip()
    # 字段级填充：返回 dict 的函数一律先试这条路（比"写整段代码"小一个量级）
    if _dictfill_enabled() and want and contract is not None and not source.startswith("#x"):
        try:
            from . import dictfill as _df
            f = contract.by_name(want) if hasattr(contract, "by_name") else None
            if f is not None and _df.supported(f):
                r = try_dict_fill(ws, path, order, f, contract, model, emit)
                if r.get("ok"):
                    return r
                # 字段级填充失败的原因要跟着进下一段提示词：
                # 它是最窄的证据（"你上一次的表达式在空列表上除零"），
                # 不带过去的话，普通符号编辑会从头再犯一遍同样的错。
                why = str(r.get("reason") or "").strip()
                if why:
                    extra += ("\n【上一次的自动修复没有通过，原因（必须避开）】\n" + why[:400])
                if emit:
                    emit("dictfill_skip", {"symbol": want, "reason": why[:160]})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"字段级填充跳过：{type(exc).__name__}: {exc}"})
    target_src = ""
    try:
        from . import symedit as _se
        target_src = _se.source_of(source, want) if want else ""
    except Exception:
        target_src = ""
    # 任务简报：让"看过证据的模型"写"这次要做什么"，规则骨架仍由引擎持有（见 core/briefing.py）。
    # 校验不过就丢弃 → 提示词与今天完全一致（fail-safe）。开关 SIXCODER_BRIEFING=0 可关。
    brief_txt = ""
    brief_info = {"ok": False, "reason": "关"}
    if _briefing_enabled():
        try:
            from . import briefing as _brief
            evidence = (order.get("acceptance") or "") + "\n" + (order.get("detail") or "")
            brief_info = _brief.generate(ws, want, order.get("acceptance") or "", evidence,
                                         target_src or _tokens_mod.cut(source, 3000), task_id=str(ws.name))
            if brief_info.get("ok"):
                brief_txt = str(brief_info.get("text") or "")
        except Exception as exc:                     # noqa: BLE001
            brief_info = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
    if emit and brief_info.get("ok"):
        emit("briefing", {"symbol": want, "by": brief_info.get("by"),
                          "text": brief_txt[:400]})
    prompt = _build_exec_prompt(order, want, target_src, source, plan=plan,
                                extra=extra, brief=brief_txt)
    text = ""
    for _try in range(max(1, attempts)):
        try:
            res = ollama.chat([{"role": "user", "content": prompt}], model, json_mode=True,
                              temperature=0.2 + 0.15 * _try, num_predict=900)
        except Exception as exc:                     # Ollama 500/超时都不许打断整班
            if _try + 1 >= max(1, attempts):
                return {"ok": False, "reason": f"模型调用失败：{type(exc).__name__}"}
            continue
        text = res.get("text") or ""
        if re.search(r'"edits"', text):
            break
        # 模型选择"不猜、发问"（开发者模式）：登记成待答；已经有人答过就直接复用答案重问。
        q = _extract_question(text)
        if q:
            try:
                from . import questions as _questions
                got = _questions.ask(ws, q["question"], q.get("options"), q.get("why") or "",
                                     node=want, task_id=str(ws.name), emit=emit)
            except Exception as exc:                     # noqa: BLE001
                got = {"ok": False, "error": f"{type(exc).__name__}"}
            if got.get("ok"):
                extra += ("\n【人工已确认（必须照此实现，不要再问）】\n"
                          + str(got.get("answer"))[:800])
                prompt = _build_exec_prompt(order, want, target_src, source,
                                            plan=plan, extra=extra, brief=brief_txt)
                continue
            return {"ok": False, "need_human": got,
                    "reason": f"需要人回答：{q['question'][:120]}"}
        prompt = prompt + "\n\n【上次没有产出 JSON，必须只输出 JSON】"
    edits = _extract_edits(text)
    if not edits:
        # 上报资深（远端）：同一条工作单，把提示词原样交给远端，回来的编辑仍由本地渲染与闸门判定
        try:
            from . import remote as _remote
            ok_r, _why = _remote.available()
            if ok_r:
                rr = _remote.complete(prompt, task_id="worker-escalation", node_id="escalate", workspace=str(ws))
                if rr.get("ok"):
                    edits = _extract_edits(rr.get("answer") or "")
                    if edits:
                        text = rr.get("answer") or ""
        except Exception:                                # noqa: BLE001
            pass
    if not edits:
        return {"ok": False, "reason": "执行者没有产出符号编辑"}
    want = (order.get("symbol") or "").strip()
    # add_import 单天然没有"目标函数"：它的产物是一条 import 语句。
    # 实测事故（night8）：架构师正确诊断出"clean.py 缺少 datetime 导入"并下了 add_import 单，
    # 但执行器强制"改的符号必须等于单上的符号"，于是三连失败、lint 一直缺 import，
    # parse_date 到收工都是 NameError。这里对 import 单取消符号强制。
    kind_is_import = (order.get("kind") or "") == "add_import"
    fixed = []
    for e in edits:
        code = str(e.get("code") or "")
        sym = str(e.get("symbol") or "").strip()
        if want and not kind_is_import:
            if re.search(rf"(^|\n)\s*(?:async\s+)?(?:def|class)\s+{re.escape(want)}\b", code):
                sym = want               # 代码确实是目标函数 → 强制对齐到单上的符号
            elif sym != want:
                return {"ok": False,
                        "reason": f"执行者改的是 {sym or '未命名'}，而这张单要求改 {want}"}
        fixed.append({"op": e.get("op") or "replace", "symbol": sym or want,
                      "code": code, "after": e.get("after") or ""})
    edits = fixed
    rep = symedit.apply_edits(source, edits)
    if not rep.ok:
        # 确定性抢救：模型把 docstring 里的引号搞乱时，机械地修一版再渲染
        # （实测这是 7B 最常见的失败形态；能救就不用再花一轮模型时间）
        salvaged = None
        for e in edits:
            for cand in _repair_code(str(e.get("code") or "")):
                trial = [dict(x) for x in edits]
                trial[edits.index(e)]["code"] = cand
                r2 = symedit.apply_edits(source, trial)
                if r2.ok:
                    rep, salvaged = r2, cand
                    break
            if salvaged:
                break
        if not rep.ok:
            return {"ok": False, "reason": f"渲染失败：{rep.reason}"}
    # 关键节点守卫：开发者模式在这里等人点头（甲方模式自动放行；只读模式根本走不到）
    try:
        from . import guards
        g = guards.gate_file_write(ws, path, source, rep.new_source, emit=emit)
        if not g.get("ok"):
            return {"ok": False, "reason": f"{path} 未写入：{g.get('reason')}"}
    except ImportError:                                  # pragma: no cover
        pass
    p.write_text(rep.new_source, encoding="utf-8")
    # 写盘后跑确定性自动修复（不靠模型）：补"用了模块却没 import"和"注解里缺的名字"。
    # 实测事故（night8）：parse_date 用了 datetime 却没 import，架构师诊断对了、工作单也下了，
    # 但夜班链路没有这一步确定性修复，最后交付里 parse_date 还是 NameError。
    fixed_imports: list[str] = []
    try:
        from . import apikb as _apikb, autofix as _autofix
        src_now = p.read_text(encoding="utf-8-sig", errors="replace")
        miss_mods: list[str] = []
        try:
            kb = _apikb.ApiKB(gates.API_KB_DB)
            miss_mods = sorted({f.where for f in _apikb.verify_undeclared_modules(src_now, kb)})
        except Exception:                                # noqa: BLE001
            miss_mods = []
        fx = _autofix.fix_file(p, contract=(_sig_text(contract, want) or
                                            order.get("acceptance") or ""),
                               missing_modules=miss_mods)
        fixed_imports = list(fx.get("imports") or []) + list(fx.get("params") or [])
        if fixed_imports and emit:
            emit("autofix", {"path": path, "imports": fx.get("imports") or [],
                             "params": fx.get("params") or []})
    except Exception:                                    # noqa: BLE001
        fixed_imports = []
    # 写完立刻过机械静态闸门：SQL 行形态这类问题语义上"看着对、跑起来必炸"，
    # 在写盘处就地拦下并回传唯一修法，比等下一轮远端架构师发现便宜得多。
    try:
        dr = gates.gate_db_rows(ws, [path])
        if not dr.ok:
            return {"ok": False, "reason": f"【SQL行】{dr.detail}", "applied": rep.applied}
        imp = gates.gate_import_ok(ws, [path])
        if not imp.ok:
            return {"ok": False, "reason": f"【导入】{imp.detail}", "applied": rep.applied}
        # 签名闸门：确定性补参数已在上面跑过，这里复核。还不对就当场退单，
        # 别让"参数不对"拖到端到端才以 TypeError 的形态出现（那种反馈定位不到函数）。
        if contract is not None and want:
            sig_bad = _signature_problem(contract, p.read_text(encoding="utf-8-sig",
                                                               errors="replace"), want)
            if sig_bad:
                return {"ok": False, "reason": f"【签名】{sig_bad}", "applied": rep.applied}
    except Exception:                                    # noqa: BLE001
        pass
    return {"ok": True, "applied": rep.applied, "imports_fixed": fixed_imports}


# ------------------------------------------------------------------ 远端救单
RESCUE_PROMPT = """你是资深工程师。下面这张工作单在本地小模型上已经连续失败，请给出**符号级**修复方案。

只输出 JSON（不要解释、不要围栏）：
{"edits":[{"op":"replace","symbol":"<<TARGET_SYMBOL>>","code":"def <<TARGET_SYMBOL>>(...):\\n    ..."}]}

铁律：
1. 只输出 <<TARGET_SYMBOL>> 这一个函数的完整定义（含 def 行、4 空格缩进、类型注解）。
2. 不要输出整文件、不要输出 diff、不要动其它符号。
3. 必须修掉下面【失败证据】指出的问题；不要把测试删掉或改弱。
4. 契约里的签名/异常行为不许改。

【工作单】<<ORDER>>
【失败证据（本地最近一次）】
<<EVIDENCE>>
<<BRIEFING>>
【目标符号现状】
```python
<<TARGET_SOURCE>>
```
"""


def execute_order_remote(ws: Path, order: dict, evidence: str = "", brief: str = "",
                         task_id: str = "", emit=None, plan: str = "",
                         contract=None, as_author: bool = False,
                         thinking: str | None = None, step_index: int = 0) -> dict:
    """由远端写这张单的代码（落盘与判定仍归引擎）。

    两种身份：
      · as_author=False（默认）：**救援**。本地连续失败后让远端出符号级修复（RESCUE_PROMPT）。
      · as_author=True：**主笔**。甲方模式下代码默认由远端写，用的是与本地完全相同的
        执行者提示词（约束一致），远端只负责"想得比本地好"。

    为什么需要救援那一档（实测）：`create_table` 这种明显可实现的函数，本地连失败 3 次就被
    "隔离"、整夜不再派 —— 等于**放弃**。而架构师每轮都诊断得对，说明问题不在"不知道怎么做"，
    而在"本地执行者做不出来"。

    返回 {ok, reason, applied?, by:"remote"}。
    """
    from . import remote as _remote
    ok_r, why = _remote.available()
    if not ok_r:
        return {"ok": False, "reason": f"远端不可用（{why}）"}
    p = tools.safe_path(ws, order["target"])
    if not p.exists():
        return {"ok": False, "reason": "目标文件不存在"}
    source = p.read_text(encoding="utf-8-sig", errors="replace")
    want = (order.get("symbol") or "").strip()
    target_src = ""
    try:
        from . import symedit as _se
        target_src = _se.source_of(source, want) if want else ""
    except Exception:                                    # noqa: BLE001
        target_src = ""
    if as_author:
        # 主笔：与本地执行者用同一套提示词与同一条铁律（谁来写都得守同样的约束）
        prompt = _build_exec_prompt(order, want, target_src, source, plan=plan,
                                    brief=brief)
    else:
        prompt = (RESCUE_PROMPT
                  .replace("<<TARGET_SYMBOL>>", want or "（新增）")
                  .replace("<<ORDER>>", workorder.describe(order) + "\n" + (order.get("detail") or ""))
                  .replace("<<EVIDENCE>>", (evidence or "（本地没留下更具体的原因）")[:1200])
                  .replace("<<BRIEFING>>", brief or "")
                  .replace("<<TARGET_SOURCE>>", _prompt_safe(target_src) or
                           "（该符号当前不存在，需要新建）"))
        if (plan or "").strip():
            try:
                from . import policy as _policy
                prompt += _policy.plan_block(plan)
            except Exception:                            # noqa: BLE001
                pass
    rr = _remote.complete(prompt, task_id=task_id or "rescue",
                          node_id=("author" if as_author else "rescue") + f":{want}",
                          workspace=str(ws), emit=emit, thinking=thinking,
                          step_index=step_index, json_mode=True)
    if not rr.get("ok"):
        return {"ok": False, "reason": f"远端调用失败：{rr.get('error')}"[:200]}
    edits = _extract_edits(rr.get("answer") or "")
    if not edits:
        return {"ok": False, "reason": "远端没给出可用的符号编辑"}
    fixed = []
    for e in edits:
        code = str(e.get("code") or "")
        if want and not re.search(rf"(^|\n)\s*(?:async\s+)?(?:def|class)\s+{re.escape(want)}\b", code):
            continue                                     # 不是目标函数就丢弃，别乱改
        fixed.append({"op": e.get("op") or "replace", "symbol": want or e.get("symbol"),
                      "code": code, "after": e.get("after") or ""})
    if not fixed:
        return {"ok": False, "reason": "远端的编辑没有落在目标符号上"}
    rep = symedit.apply_edits(source, fixed)
    if not rep.ok:
        # 复用提示词侧的确定性抢救
        for e in fixed:
            for cand in _repair_code(str(e.get("code") or "")):
                trial = [dict(x) for x in fixed]
                trial[fixed.index(e)]["code"] = cand
                r2 = symedit.apply_edits(source, trial)
                if r2.ok:
                    rep = r2
                    break
            if rep.ok:
                break
    if not rep.ok:
        return {"ok": False, "reason": f"渲染失败：{rep.reason}"}
    p.write_text(rep.new_source, encoding="utf-8")
    # 落盘后仍过静态闸门（远端方案也要体检）
    for gate_fn in (gates.gate_db_rows, gates.gate_duplicates):
        try:
            g = gate_fn(ws, [order["target"]])
            if not g.ok:
                return {"ok": False, "reason": f"远端方案没过静态闸门：{g.detail}"[:200]}
        except Exception:                                # noqa: BLE001
            pass
    # 远端强 ≠ 必对：行为也要现场判一次（契约判据 + 边界判据 + 签名），
    # 不过就把窄证据带回去，别让它以"远端做的"身份混过验收。
    if contract is not None and want:
        try:
            from . import props as _props
            f = next((x for x in (getattr(contract, "funcs", None) or [])
                      if x.name == want), None)
            src_now = p.read_text(encoding="utf-8-sig", errors="replace")
            sig_bad = _signature_problem(contract, src_now, want)
            if sig_bad:
                return {"ok": False, "reason": f"远端方案的签名不对：{sig_bad}"}
            if f is not None:
                checks = _props.from_contract(_single_func_contract(contract, f))
                checks += [x for x in _props.boundary_checks(src_now, hard=True)
                           if str(x.get("expr", "")).startswith(f"m.{want}(")]
                if checks:
                    rb = _props.run(ws, order["target"], checks)
                    if not rb.get("ok"):
                        return {"ok": False,
                                "reason": "远端方案没过行为判据：" + str(_props.as_feedback(rb))[:300]}
        except Exception:                                # noqa: BLE001
            pass
    return {"ok": True, "applied": rep.applied, "by": "remote"}


def log_test_gap(ws: Path, path: str, reason: str) -> None:
    """记下"这个模块没生成测试"（写进 .sixcoder/test_gaps.jsonl，供交付报告如实列出）。"""
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "test_gaps.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "path": path,
                             "reason": str(reason)[:300]}, ensure_ascii=False) + "\n")


def load_test_gaps(ws: Path) -> list[dict]:
    p = Path(ws) / ".sixcoder" / "test_gaps.jsonl"
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.strip():
                out.append(json.loads(ln))
    except Exception:                                    # noqa: BLE001
        return []
    return out


# ------------------------------------------------------------------ 引擎测试
def add_engine_tests(ws: Path, contracts: dict, target: str = "") -> dict:
    """add_test 单的确定性做法：测试文件由合约判据渲染，模型不参与写断言。

    为什么不让模型写测试：实测模型自写的 pytest 全绿而产物 TypeError（弱测试）。
    这里把契约示例/性质/数据库端到端渲染成 pytest，跑不过就删掉不留垃圾。
    """
    ws = Path(ws)
    name = Path(target or "").name
    pairs = []
    for path, c in (contracts or {}).items():
        tp = "test_" + Path(path).stem + ".py"
        if name and name not in (tp, Path(path).name):
            continue
        pairs.append((path, c, tp))
    if not pairs:
        return {"ok": False, "reason": f"没有与 {target} 对应的模块契约"}
    made: list[str] = []
    for path, c, tp in pairs:
        if not (ws / path).exists():
            continue
        checks = props.from_contract(c)
        if not checks:
            continue
        pre = props.run(ws, path, checks)
        if not pre.get("ok"):
            return {"ok": False, "reason": "产物先过不了契约判据，测试写出来也是红的："
                                          + props.as_feedback(pre)[:220]}
        fp = ws / tp
        fp.write_text(props.render_pytest(Path(path).stem, checks), encoding="utf-8")
        tr = gates.gate_tests(ws, [tp], timeout=120)
        if not tr.ok:
            try:
                fp.unlink()
            except OSError:
                pass
            return {"ok": False, "reason": f"引擎测试未通过：{tr.detail}"[:300]}
        made.append(tp)
    if not made:
        return {"ok": False, "reason": "契约没给出可渲染的判据"}
    return {"ok": True, "files": made, "applied": made}


# ------------------------------------------------------------------ 主循环
def iterate(repo: Path, requirement: str, model: str, rounds: int = 3,
            use_remote: bool = False, emit=None) -> dict:
    ws = Path(repo)
    from . import model_policy
    try:
        model, model_warn = model_policy.enforce(model)
    except model_policy.ModelTooSmall as exc:
        return {"ok": False, "reason": str(exc)}
    if model_warn and emit:
        emit("notice", {"message": model_warn})
    contracts = contract_mod.parse_requirement(requirement)
    if not contracts:
        return {"ok": False, "reason": "需求里没有解析出模块契约"}
    task_id = f"iter-{int(time.time())}"
    history = []
    for rnd in range(1, rounds + 1):
        if rnd == 1:
            created = ensure_scaffold(ws, contracts)
            if created and emit:
                emit("scaffold", {"files": created})
        state = inspect(ws, contracts)
        workorder.record_progress(task_id, rnd, state, note="体检")
        before = sum(s["verified"] for s in state.values())
        total = sum(s["funcs"] for s in state.values())
        if emit:
            emit("round", {"round": rnd, "verified": before, "total": total})

        orders = workorder.triage(ws, contracts, state) if not use_remote else []
        root, remote_orders = ("", [])
        if use_remote:
            root, remote_orders = ask_architect(ws, task_id, contracts, state, requirement)
            orders = remote_orders or workorder.triage(ws, contracts, state)
            if emit:
                emit("architect", {"round": rnd, "root_cause": root,
                                   "orders": [workorder.describe(o) for o in orders]})
        if not orders:
            history.append({"round": rnd, "verified": before, "orders": 0, "note": "无待办"})
            break

        done = 0
        for o in orders[:5]:
            oid = workorder.add(task_id, rnd, o["kind"], o["target"], o.get("symbol", ""),
                                o.get("acceptance", ""), o.get("detail", ""))
            r = execute_local(ws, o, model, emit=emit, contract=contracts.get(o["target"]))
            if r.get("ok"):
                after_state = inspect(ws, contracts)
                improved = sum(s["verified"] for s in after_state.values()) >= before
                workorder.close(oid, bool(improved), json.dumps(r, ensure_ascii=False))
                if emit:
                    emit("order_done", {"round": rnd, "order": workorder.describe(o),
                                        "applied": r.get("applied")})
                if improved:
                    done += 1
            else:
                workorder.close(oid, False, r.get("reason", ""))
                if emit:
                    emit("order_failed", {"round": rnd, "order": workorder.describe(o),
                                          "reason": r.get("reason")})

        state = inspect(ws, contracts)
        after = sum(s["verified"] for s in state.values())
        workorder.record_progress(task_id, rnd, state, note=f"执行 {done} 张单")
        history.append({"round": rnd, "verified": after, "orders": len(orders), "done": done})
        if after <= before:
            break
    final = inspect(ws, contracts)
    return {"ok": True, "task_id": task_id, "rounds": history,
            "verified": sum(s["verified"] for s in final.values()),
            "total": sum(s["funcs"] for s in final.values()),
            "modules": {k: {"funcs": v["funcs"], "verified": v["verified"],
                            "stubs": sorted(v["stubs"])} for k, v in final.items()},
            "curve": workorder.progress_curve(task_id)}
