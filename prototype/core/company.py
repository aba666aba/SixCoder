"""公司制度与夜班：把"甲方提需求、夜里自动干、早上交付"落成一个可运行的东西。

制度（章法）写在代码里，不靠模型自觉：

  岗位            用什么模型      职责                                        禁止
  ─────────────  ─────────────  ──────────────────────────────────────────  ─────────────────────
  甲方(你)         —              提需求、改需求、验收成品                     —
  架构师(Architect) 远端大模型     只看证据：体检+契约+失败判据 → 根因 + 工作单  不写代码
  派工(Dispatch)   机械            体检结果 → 工作单（缺函数/桩/签名/导入）    不调用模型
  开发(Worker)     本地小模型      一次只做一张单，只改单上那个符号           不改别的符号/不整文件重写
  质检(Inspector)  机械            契约判据 + 闸门，通过才记账                 不接受模型自述
  交付(Deliverer)  机械            汇总成品/未完成/风险，写 DELIVERY.md       不虚构"已完成"

夜班规则（night shift）：
  - 有硬预算：`--hours` 墙钟上限、每轮最多 5 张单、单张单最多 2 次尝试；
  - 收敛判据：连续 2 轮"已验收函数数"不增长 → 提前收工（不空转烧钱）；
  - 交接物：`DELIVERY.md`（含验收清单、未完成项、下次建议）+ `_logs/night-*.json`；
  - 只声明有硬证据的事：`verified` 只由机械判据产生，模型说什么都不算。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import contract as contract_mod
from . import db, iterate, ollama, policy, settings_store, workorder

ROLES = {
    "architect": {"model": "remote", "may": ["读证据", "出根因", "下工作单"], "may_not": ["写代码"]},
    "dispatch": {"model": "mechanical", "may": ["体检", "拆分工作单"], "may_not": ["调用模型"]},
    "worker": {"model": "local", "may": ["改单上指定的那个符号"], "may_not": ["改其它符号", "整文件重写"]},
    "inspector": {"model": "mechanical", "may": ["跑判据与闸门", "记账"], "may_not": ["采信模型自述"]},
    "deliverer": {"model": "mechanical", "may": ["汇总交付物"], "may_not": ["虚构完成度"]},
}


@dataclass
class NightResult:
    ok: bool
    rounds: list[dict] = field(default_factory=list)
    verified: int = 0
    total: int = 0
    modules: dict = field(default_factory=dict)
    delivery_path: str = ""
    reason: str = ""
    spent_s: float = 0.0


def _single_module_fallback(requirement: str) -> dict:
    """自然语言需求兜底：能认出目标 .py 与带类型注解的函数签名，就当成单模块契约。

    为什么做这个：界面上甲方直接写"给 calc.py 加个 divide(a, b)"是很自然的事，
    但夜班没有契约就跑不起来（没有判据就无法验收）。这里做的是**最保守**的一步：
    只认"明确出现的文件名 + 明确写全签名的函数"，认不出就返回空（宁可拒绝，不许瞎猜）。
    """
    try:
        text = requirement or ""
        m = re.search(r"([A-Za-z_][\w/\\]*\.py)", text)
        if not m:
            return {}
        path = Path(m.group(1)).name
        # 只收"函数名(参数: 类型, ...) -> 返回类型"这种写全了的签名
        sigs = re.findall(
            r"([A-Za-z_]\w*)\s*\(\s*([^()]*?)\s*\)\s*->\s*([\w\[\], \.|]+?)\s*[:：]", text)
        sigs = [(n, a, r) for n, a, r in sigs if n not in ("if", "for", "while", "return", "print")]
        if not sigs:
            return {}
        lines = [f"1) {path}："]
        for name, args, ret in sigs:
            desc = ""
            tail = text.split(f"{name}(", 1)[-1]
            if "：" in tail:
                desc = tail.split("：", 1)[1].split("；")[0].strip()[:80]
            lines.append(f"   - {name}({args.strip()}) -> {ret.strip()}：{desc or '按签名实现'}")
        rebuilt = "\n".join(lines)
        return contract_mod.parse_requirement(rebuilt)
    except Exception:                                  # noqa: BLE001
        return {}


def _is_transient_fail(reason) -> bool:
    """这一次失败是"偶发"（限流/网络/超时）还是"真做不出来"。

    偶发失败不该触发升档（花更贵的钱解决一个网络抖动问题）；只有真做不出来才升。
    """
    t = str(reason or "")
    return any(k in t for k in ("限流", "429", "超时", "timed out", "Timeout", "网络",
                                "HTTP 502", "HTTP 503", "请求失败"))


def _is_hard_failure(reason) -> bool:
    """这张单的失败是不是**硬判据**给的（机械证据），而不是"模型没吐 JSON"这类软失败。

    为什么用它决定升级：甲方模式的上限是远端推理能力，本地被机械判据判红说明"它做不出来
    这件事已经被证明了"，再本地重试只是烧时间；而"没吐 JSON""超时"属于偶发，值得本地重试。
    """
    t = str(reason or "")
    tokens = ("【签名】", "【SQL行】", "【导入】", "判据", "边界输入", "契约判据", "变异",
              "回归：", "字段", "重复定义", "AssertionError", "TypeError", "KeyError",
              "ZeroDivisionError", "AttributeError", "IndexError")
    return any(k in t for k in tokens)


def _remote_available() -> bool:
    """远端现在能不能用（给"隔离前救一次"判断用）。失败一律当"不能用"，不抛。"""
    try:
        from . import remote as _remote
        ok, _why = _remote.available()
        return bool(ok)
    except Exception:                                    # noqa: BLE001
        return False


def _param_billions(name: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", (name or "").lower())
    return float(m.group(1)) if m else 0.0


def _stronger_alternative(current: str) -> str:
    """找一个"参数更大或同为 7B 级、且更会写代码"的已安装模型；明显更弱的一律不换。

    依据（实测）：3B 在同一个夜班任务上是 2/8 机械验收、2/16 独立验收，且大多数失败是
    "生成代码无法解析"。把工人从 7B 换成 3B 只能浪费一轮预算。
    同等参数量时**代码专精模型优先**（coder 更听指令、更少废话）。
    """
    try:
        from . import model_policy
        avail = [m for m in ollama.models() if m != current]
    except Exception:                                    # noqa: BLE001
        return ""
    floor = max(7.0, _param_billions(current) - 1.0)
    cands = [m for m in avail if _param_billions(m) >= floor]
    if not cands:
        return ""
    cands.sort(key=lambda m: (-int(model_policy.is_coder(m)), _param_billions(m), m))
    return cands[0]


def _night_foreign(ws: Path, requirement: str, model: str, lang_info: dict,
                   t0: float, emit=None, max_cny: float = 0.0,
                   task_id: str = "") -> NightResult:
    """非 Python 语言的夜班：契约解析照旧（语法是语言无关的），验证换成本语言的工具链。

    **绝不冒充验收通过**：契约示例级判据目前只有 Python 有，这里能给出的是
    "真编译器/真运行时通过"。报告与结果里都按这个口径写，verified 一直是 0。
    """
    from . import author as _author
    from . import contract as cm
    from . import lang as _lang
    from . import remote as _remote

    lang = lang_info["lang"]
    task_id = task_id or f"foreign-{int(time.time())}"     # 调用点还没生成 task_id
    contracts = cm.parse_requirement(requirement)
    if not contracts:
        return NightResult(False, spent_s=round(time.time() - t0, 1), reason=(
            f"识别为 {lang_info['name']}，但需求里没解析出模块契约。\n"
            "该语言同样要写清模块名与签名，例如：\n"
            "  「1) Foo.java：public class Foo：add(int a, int b) -> int：两数相加」"))

    def _verify_foreign(ws2: Path, path: str) -> dict:
        v = _lang.verify(ws2, path, lang)
        return {"ok": bool(v.get("ok")),
                "feedback": (f"{v.get('how')}：{v.get('detail')}" if not v.get("ok")
                             else "")}

    def _foreign_author(rel: str, cfg: dict, attempts: int = 3) -> dict:
        """外语作者回路：紧凑提示词 → JSON 解析 → 落盘 → 本语言工具链验证 → 带错重试。"""
        from . import author as _a
        from . import remote as _r
        err = ""
        for attempt in range(1, attempts + 1):
            body = (
                f"你是 {lang_info['name']} 程序员。按下面的需求写 **{rel}** 的完整源码。\n\n"
                f"【需求】\n{requirement}\n\n"
                f"【硬性要求】\n"
                f"1. 只输出一个 JSON 对象，形如 "
                f'{{"files":[{{"path":"{rel}","content":"完整源码"}}]}}\n'
                f"2. content 必须是**能直接编译/运行的完整文件**（含必要的 import/包声明/main）\n"
                f"3. 不要输出解释、不要输出 diff、不要写测试文件、不要用 markdown 代码块\n"
                f"4. 语言是 {lang_info['name']}，扩展名就是 {rel}\n"
            )
            if err:
                body += f"\n【上一版没通过，必须改掉】\n{err[:1200]}\n"
            r = _r.complete(body, task_id=task_id, node_id=f"author:{rel}",
                            workspace=str(ws), emit=emit, json_mode=True, max_out=8000)
            if not r.get("ok"):
                return {"ok": False, "reason": f"远端失败：{r.get('error')}"[:200]}
            files, _issues, _dl = _a._parse_reply(r.get("answer") or "")
            mine = [x for x in files if str(x.get("path", "")).endswith(Path(rel).name)] or files[:1]
            if not mine or not str(mine[0].get("content") or "").strip():
                err = '没有给出 {"files":[{"path":...,"content":...}]} 形式的内容'
                continue
            content = _a._strip_fence(str(mine[0]["content"]))
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text(content, encoding="utf-8")
            v = _lang.verify(ws, rel, lang)
            if v.get("ok"):
                return {"ok": True, "applied": [rel], "attempt": attempt,
                        "by": r.get("by") or "", "how": v.get("how")}
            err = f"{v.get('how')} 报错：{v.get('detail')}"
        return {"ok": False, "reason": err or "未通过验证", "attempt": attempts}

    results: dict[str, dict] = {}
    spent = 0.0
    for rel in sorted(contracts):
        if max_cny and spent > max_cny:
            results[rel] = {"ok": False, "reason": f"花费到上限 ¥{max_cny}"}
            continue
        before = _remote.usage_summary("")
        r = _foreign_author(rel, contracts[rel])
        after = _remote.usage_summary("")
        spent += float(after["cost_cny"]) - float(before["cost_cny"])
        results[rel] = r
        if emit:
            emit("foreign_verify", {"path": rel, "ok": bool(r.get("ok")),
                                    "by": lang_info["name"],
                                    "how": _lang.judge_level(lang),
                                    "attempt": r.get("attempt")})

    ok_files = [p for p, r in results.items() if r.get("ok")]
    bad = {p: str(r.get("reason") or r.get("feedback") or "")[:300]
           for p, r in results.items() if not r.get("ok")}
    reason = (f"{_lang.LANGS.get(lang, {}).get('name', lang)}：{len(ok_files)}/{len(results)} "
              f"个模块通过{_lang.judge_level(lang)}。**契约示例未机判，不计入验收通过**。")
    if bad:
        reason += " 未通过：" + "；".join(f"{k}（{v[:120]}）" for k, v in list(bad.items())[:3])
    try:
        write_delivery(ws, requirement, {}, [], reason, time.time() - t0,
                       scope={"level": "S", "lang": lang})
    except Exception:                                    # noqa: BLE001
        pass
    return NightResult(False, spent_s=round(time.time() - t0, 1), reason=reason)


def _night_inner(repo: Path, requirement: str, model: str, hours: float = 6.0,
          use_remote: bool = True, max_rounds: int = 40, emit=None,
          plan: str = "", contract_file: str = "",
          remote_profile: str = "", max_cny: float = 0.0,
          scale_level: str = "") -> NightResult:
    """夜班：在预算内反复"体检→派工→开发→质检"，直到收敛或预算耗尽。

    三档模式的**协作方式**不同（见 core/policy.py）：
      甲方模式：任何决策由远端拍板；本地被硬判据判红就立刻交给远端写代码；
      开发者模式：实现方案必须由人给；要架构决策而没有方案时抛 needhuman，不替人拍板；
      只读模式：不写任何文件，直接拒绝跑夜班。
    """
    t0 = time.time()
    from . import permissions as _perms
    from . import policy as _policy
    pol = _policy.current()
    if pol.code_escalation == _policy.OFF or not _perms.allows("file.write"):
        return NightResult(False, reason=f"{pol.name}不写文件，不能跑夜班；请切到甲方模式或开发者模式",
                           spent_s=round(time.time() - t0, 1))

    budget_s = max(60.0, hours * 3600)
    ws = Path(repo)
    # 规模分级：不同代码规模该有的约束不一样（见 core/scale.py）。
    # 甲方模式的要求是"提需求时写明目标规模"；没写就按实测推断，并在交付报告里写明。
    from . import scale as _scale
    scope = _scale.resolve(requirement, ws)
    if emit:
        emit("scale", {"level": scope["level"], "source": scope["source"],
                       "declared": scope.get("declared", ""),
                       "inferred": scope.get("inferred", ""),
                       "total_lines": scope.get("total_lines", 0),
                       "measured": scope.get("measured", {}),
                       "gates_run": _scale.gate_plan(scope["level"])["run"],
                       "gates_skipped": [n for n, _ in _scale.gate_plan(scope["level"])["skip"]],
                       "warning": scope.get("warning", "")})
    # 门槛：本地工人必须 ≥7B（实测 3B 机械验收 2/8、独立验收 2/16，不能当工人）
    from . import model_policy
    model, model_warn = model_policy.enforce(model)
    if model_warn and emit:
        emit("notice", {"message": model_warn})
    # ---- 语言闸门（自动识别 + 模式切换）----
    # **必须排在契约合成之前**：能真验证的才放行 —— Java/C/C++ 用真编译器、JS/TS 用 node；
    # 没工具链的语言"开跑前就拒绝（0 花费）"。
    # 实测回归（2026-11-05）：把闸门挪到契约合成之后，会让"本机无法验证的语言"
    # 先花掉一次远端调用再被拒绝，`check_lang` 的"零花费"断言立刻抓住。
    from . import lang as _lang_mod
    try:
        # 用 planner 的那一份正则，别再抄一遍：抄出来的这版把 `c` 排在 `cpp` 前面、
        # 又没有词尾边界，于是 `stats.cpp` 被判成 C（实测：C++ 任务用 gcc 验的，
        # 那个"通过"根本不能证明 C++ 正确性）。
        from .planner import MODULE_RE as _MOD_RE
        _req_paths = [m.group(1) for m in _MOD_RE.finditer(requirement or "")]
        _linfo = _lang_mod.detect(requirement or "", _req_paths)
    except Exception as exc:                             # noqa: BLE001
        _linfo = {"lang": "python", "name": "Python", "ok": True, "by": "识别失败",
                  "why": f"{type(exc).__name__}", "install": "", "judge": "full"}
    if emit:
        emit("lang_detect", {"lang": _linfo["lang"], "name": _linfo["name"],
                             "by": _linfo["by"], "ok": _linfo["ok"],
                             "evidence": _lang_mod.judge_level(_linfo["lang"])})
    if _linfo["lang"] != "python":
        if not _linfo["ok"]:
            _why = (f"识别为 {_linfo['name']}（依据：{_linfo['by']}），但本机**无法验证**它："
                    f"{_linfo['why']}。\n"
                    f"装好之后再来：{_linfo['install']}\n"
                    "在此之前系统不会替你生成「看起来对但没验证过」的代码。")
            try:
                write_delivery(ws, requirement, [], [], _why, 0.0,
                               scope={"level": "S", "lang": _linfo["lang"]})
            except Exception:                    # noqa: BLE001
                pass
            return NightResult(False, rounds=[], reason=_why,
                               spent_s=round(time.time() - t0, 1))
        return _night_foreign(ws, requirement, model, _linfo, t0, emit=emit,
                              max_cny=max_cny)
    locked = ""
    if contract_file:
        try:
            locked = Path(contract_file).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            locked = ""
    if locked.strip():
        contracts = contract_mod.parse_requirement(locked)
        if contracts:
            requirement = locked + "\n\n【甲方原始需求】\n" + requirement
            if emit:
                emit("contract_locked", {"file": str(contract_file),
                                         "modules": sorted(contracts)})
    else:
        contracts = {}
    if not contracts:
        contracts = contract_mod.parse_requirement(requirement)
    # 「解析出模块但一个函数都没有」等于没解析：
    # 实测踩过 —— 需求里用 `1) calc/core/numeric.py：` + `- 行为` 这种形状写，
    # 会被确定性解析器认成"8 个模块、0 个函数"的契约，于是远端合成被跳过、
    # 夜班拿着一堆空模块跑完 0/0，还只花 66 秒（**静默零工作量**，最毒的一种失败）。
    # 现在这种半成品一律当成"没解析出来"，继续往下走远端合成；永远不许悄悄空跑。
    _nf = _contract_func_count(contracts)
    if contracts and _nf == 0:
        if emit:
            emit("notice", {"message": f"需求里识别出 {len(contracts)} 个模块但 0 个函数签名"
                                       "（只是文件名清单，还不是契约）→ 交给远端合成契约"})
        contracts = {}
    if not contracts:
        # 夜班要"机械判据"，就得先有**结构化契约**。但甲方提的需求常常是自然语言
        # （实测："写一个计算器，支持加减乘除、根号、求导、不定积分" → 解析不出任何模块）。
        # 甲方模式的契约本来就该由系统内部搞定：先试确定性兜底，再让远端把需求翻译成契约。
        fallback = _single_module_fallback(requirement)
        if fallback:
            contracts = fallback
            if emit:
                emit("notice", {"message": "需求里没有清晰的模块结构，已按单模块兜底解析："
                                           + ", ".join(sorted(contracts))})
        elif use_remote and _remote_available() and pol.plan_authority == "remote":
            from . import contractgen
            if emit:
                emit("contract_synth", {"phase": "start", "requirement": requirement[:200]})
                # 规格的档位/思考/歧义策略单独记一条事件（不动上面那条已裁决的展示字段）
                emit("spec_policy", {"tier": pol.spec_tier, "thinking": pol.spec_thinking,
                                     "ambiguity": pol.ambiguity,
                                     "why": "规格是全链最高杠杆的一步：用最强档+思考产出，"
                                            "之后每个模块都照它做"})
            # 规格优先：甲方模式用**最强档 + 思考开**产出规格（这是全链最高杠杆的一步）；
            # 开发者模式走阶梯默认，且歧义会停下来问人而不是自己拍板。
            # 一轮写多少**不是常数 16**：按这一档的输出配额、依赖数、近期一次通过率算（core/dosing）。
            from . import dosing as _dosing
            _spec_step = None
            try:
                _idx = settings_store.strongest_step_index() \
                    if pol.spec_tier == "strongest" else 0
                _spec_step = settings_store.ladder_step(_idx)
            except Exception:                    # noqa: BLE001
                _spec_step = None
            _budget = _dosing.context_for(ws, _spec_step)
            if emit:
                _n, _why = _dosing.suggest_max_funcs(**_budget)
                emit("dosing", {"round_allowance": _n, "why": _why,
                                "capacity_tokens": _budget["capacity"],
                                "first_pass": _budget["first_pass"]})
            gen = contractgen.load_or_synthesize(
                ws, requirement,
                step_index="strongest" if pol.spec_tier == "strongest" else None,
                thinking=pol.spec_thinking, ambiguity=pol.ambiguity, budget=_budget)
            if gen.get("need_human"):
                # 规格歧义 → 停下来问人（只会在 developer 的 ambiguity="ask" 时发生）
                try:
                    from . import questions as _qs
                    _qs.ask(ws, gen.get("question") or "规格有歧义", gen.get("options"),
                            gen.get("why") or "", emit=emit)
                except Exception:                    # noqa: BLE001
                    pass
                return NightResult(False, spent_s=round(time.time() - t0, 1), reason=(
                    "规格有歧义，已停下等人拍板（不会猜着往下写）：\n- "
                    + (gen.get("question") or "")
                    + (f"\n可选：{' / '.join(gen.get('options') or [])}" if gen.get("options") else "")))
            if gen.get("ok"):
                contracts = gen["contracts"]
                # 后续所有提示词都用"翻译出来的契约"作为需求原文，语义不会跑偏
                requirement = gen["text"] + "\n\n【甲方原始需求】\n" + requirement
                if emit:
                    emit("contract_synth", {"phase": "done", "by": gen.get("by"),
                                            "modules": sorted(contracts),
                                            "text": gen["text"][:1200]})
            else:
                return NightResult(False, spent_s=round(time.time() - t0, 1), reason=(
                    "远端也翻译不出可机判的契约：\n- " + "\n- ".join(gen.get("problems") or [])
                    + "\n可以改写成「1) 文件名.py：函数名(参数: 类型) -> 返回类型：一句行为 + 一条示例」。"))
        else:
            return NightResult(False, spent_s=round(time.time() - t0, 1), reason=(
                "需求里没有解析出模块契约（当前模式下不会让远端替你翻译）。请按这个格式写：\n"
                "  「1) clean.py：normalize_age(x: str) -> int：……；parse_date(s: str) -> str：……」\n"
                "也就是：先点名文件（xxx.py），再逐个给函数名、参数类型、返回类型与一句行为要求。"))
    # 兜底：走到这里契约必须**真的有函数**。否则立刻如实失败 ——
    # 绝不许拿着一堆空模块跑完 0/0（实测踩过：66 秒花 4 次调用、产出 0 行）。
    if _contract_func_count(contracts) == 0:
        return NightResult(False, spent_s=round(time.time() - t0, 1), reason=(
            "契约里 0 个函数：需求里只有模块名/自然语言，没有可判定的函数签名，"
            "远端合成也没给出可用契约。请补上「函数名(参数: 类型) -> 返回类型：一句行为 + 一条示例」。"))
    # ---- 项目地图（第3层）：跨模块事实源，落盘后供判据与提示词共用 ----
    try:
        from . import project as _proj
        _map = _proj.build_map(contracts)
        _proj.save_map(ws, _map)
        _st = _map.get("stats") or {}
        if emit:
            emit("project_map", {"modules": _st.get("modules"), "funcs": _st.get("funcs"),
                                 "est_lines": _st.get("est_lines"),
                                 "deps": {k: v for k, v in list((_map.get("deps") or {}).items())[:8]}})
        _est = _proj.estimate(contracts)
        _fit = _proj.budget_fits(contracts, max_cny)
        _batches = _proj.batch_plan(_proj.order_modules(contracts), 4)
        if emit:
            emit("estimate", {"est": _est, "budget_ok": _fit["ok"], "note": _fit["note"][:160],
                              "batches": len(_batches)})
        scope = {**(scope or {}), "estimate": _est, "budget_note": _fit["note"],
                 "batches": [len(b) for b in _batches]}
    except Exception as _exc:                            # noqa: BLE001
        if emit:
            emit("notice", {"message": f"项目地图生成失败：{type(_exc).__name__}"})

    # ---- 契约自查（先判示例对不对，别让远端去追一个错的答案）----
    from . import contractcheck
    cc = contractcheck.check(contracts)
    if cc["hard"]:
        detail = contractcheck.render(cc)
        if emit:
            emit("contract_check", {"ok": False, "hard": cc["hard"][:4], "advisory": cc["advisory"][:2]})
        # 两条示例互相打架（同一组参数两个期望值）= 甲方要的答案只有一个，系统**不许替他选**。
        # 实测（实验台 contradictory）：让它"修契约"的结果是删掉一条继续跑，最后还报
        # "验收 1/1 通过" —— 花钱追了个错答案。这类只允许拒绝并要求澄清。
        _unfix = contractcheck.unfixable(cc["hard"])
        if _unfix:
            reason = ("契约里有需要甲方拍板的矛盾，未开始写代码：\n- "
                      + "\n- ".join(_unfix[:4])
                      + "\n这两条不可能同时成立，删哪条都是替甲方做决定；"
                        "请改成你真正想要的那一条（并删掉另一条），再跑一次。")
            state = iterate.inspect(ws, contracts) if contracts else {}
            try:
                write_delivery(ws, requirement, state, [], reason, 0.0)
            except Exception:                       # noqa: BLE001
                pass
            if emit:
                emit("stop", {"round": 0, "reason": reason, "verified": 0})
            return NightResult(False, rounds=[], reason=reason,
                               spent_s=round(time.time() - t0, 1))
        if use_remote and _remote_available() and pol.plan_authority == "remote":
            from . import contractgen as _cg
            _before = list(cc["hard"])
            retry = _cg.synthesize(
                requirement + "\n\n【上一版契约自相矛盾，必须改掉】\n" + detail, task_id="contract")
            if retry.get("ok"):
                cc2 = contractcheck.check(retry["contracts"])
                if not cc2["hard"]:
                    contracts = retry["contracts"]
                    requirement = retry["text"] + "\n\n【甲方原始需求】\n" + requirement
                    cc = cc2
                    if emit:
                        # 自动改契约必须留痕：改了哪几条要写清楚，不能悄悄替甲方改需求
                        emit("contract_check", {"phase": "fixed", "by": retry.get("by"),
                                                "fixed_items": [str(x)[:120] for x in _before[:4]]})
                        try:
                            _log_contract_fix(ws, _before)
                        except Exception:           # noqa: BLE001
                            pass
        if cc["hard"]:
            # 仍然矛盾 → 不写代码，如实记进交付报告（省下整夜无效救援）
            reason = ("契约自相矛盾，未开始写代码（避免追错答案）：\n- "
                      + "\n- ".join(cc["hard"][:6]))
            state = iterate.inspect(ws, contracts) if contracts else {}
            try:
                write_delivery(ws, requirement, state, [], reason, 0.0)
            except Exception:                       # noqa: BLE001
                pass
            return NightResult(False, rounds=[], reason=reason,
                               verified=sum(s.get("verified", 0) for s in state.values()),
                               total=sum(s.get("funcs", 0) for s in state.values()),
                               spent_s=round(time.time() - t0, 1))
    elif emit:
        emit("contract_check", {"ok": True,
                                "advisory": cc["advisory"][:3]})
    iterate.ensure_scaffold(ws, contracts)
    task_id = f"night-{int(time.time())}"

    hist: list[dict] = []
    prev_verified, stall = -1, 0
    fatal_rounds = 0
    reason = ""
    # 思考升级：默认 off；off 没解决才升 on，且整夜最多升 MAX_THINK_ON 次（成本上限）
    architect_turns = 0
    stall_rounds = 0
    think_on_used = 0
    MAX_THINK_ON = 1
    # 稳压器：无增长不立刻收工，而是按"策略阶梯"换一种干法继续
    strategies = ["mechanical", "architect", "swap_model", "decompose", "force_scaffold"]
    strategy_i = 0
    blocked: dict[str, int] = {}        # 单 key → 连续失败次数（≥3 隔离，不再派）
    abandoned: dict[str, dict] = {}     # 被隔离/放弃的单（写进交付报告，让甲方看得见）
    tried_blocked = set()
    # 远端写代码的计数与"八档阶梯"的逐单档位
    remote_code_used = 0
    plan_waiting = False
    from . import remote as _remote_mod
    steps = _remote_mod.ladder(remote_profile) if use_remote else []
    if emit and use_remote:
        emit("remote_model", {"profile": remote_profile or "（自动：整条阶梯）",
                               "steps": [f"{x['profile_id']}:{x['effort']}" for x in steps]})
    escalate_after = max(1, int(settings_store.get_remote().get("escalate_after") or 1))  # 唯一真相源：settings（默认 1）
    dec_used: dict[str, dict] = {}      # 模块 → {动作: 已用次数}（决策表预算，跨轮累计）
    # 整夜同一模块的重写次数预算（默认 3）。模块级、跨轮累计、成功清零。
    write_used: dict[str, int] = {}
    import os as _os_cap
    try:
        # 0 = 由代码规模档位决定（S 3 / M 2 / L 1）；环境变量只用于覆盖
        write_per_tier = int(_os_cap.environ.get("SIXCODER_WRITE_PER_TIER") or 0)
    except (TypeError, ValueError):
        write_per_tier = 0
    tier_of: dict[str, int] = {}        # 单 key → 当前档位（0 = flash:none）
    tier_fail: dict[str, int] = {}      # 单 key → 当前档连续失败次数
    used_tier: dict[str, int] = {}
    # 甲方模式：开局先让远端出实现方案（远端推理能力是这一档的上限，方案先由它定，
    # 细节也由它定；后面每一张单都按这个方案做，避免"系统自己边做边改架构"）
    auto_plan = ""
    # #17 甲方模式两段式：`SIXCODER_CLIENT_PLAN_GATE=1` 或设置里打开后，
    # 先只出计划、写进 .sixcoder/plan.md，然后**停下等人批**；
    # 人批之后就按 --plan-file 传入继续跑（计划成为权威，不许系统自己改架构）。
    client_gate = str(_os_cap.environ.get("SIXCODER_CLIENT_PLAN_GATE") or "") == "1"
    if client_gate and pol.mode == "client" and not (plan or "").strip():
        try:
            gate_plan, _gpo = iterate.ask_architect(ws, task_id, contracts,
                                                    iterate.inspect(ws, contracts),
                                                    requirement, thinking="off",
                                                    plan="", want_plan=True)
            pfile = ws / ".sixcoder" / "plan.md"
            pfile.parent.mkdir(parents=True, exist_ok=True)
            pfile.write_text(gate_plan or "（远端没给出计划）", encoding="utf-8")
            if emit:
                emit("plan_gate", {"stage": "awaiting_approval", "file": str(pfile),
                                   "text": (gate_plan or "")[:600],
                                   "next": f"人批后：night --plan-file {pfile}"})
            return NightResult(False, spent_s=round(time.time() - t0, 1), reason=(
                "甲方模式计划门禁：已生成实现计划并停下等人批（不会先写代码）。"
                f"计划在 {pfile}；批/改后用 --plan-file {pfile} 继续跑"))
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"计划门禁跳过：{type(exc).__name__}: {exc}"})

    if pol.plan_first and use_remote and _remote_available():
        try:
            # 方案/规格的档位策略由 policy 决定：
            #   甲方模式（spec_tier="strongest"）→ **先用最强档**再思考开：方案定了之后
            #   每一张单都照它做，用最低档定方案等于用最弱的算力做最高的决定；
            #   开发者模式（spec_tier="ladder"）→ 仍从最低档起（人会给方案，这里只是兜底）。
            _strong = settings_store.strongest_step_index()
            if pol.spec_tier == "strongest" and _strong > 0:
                _order = [_strong, 0]        # 最强档顶不住（欠费/限流）再退到最低档，别整夜起不来
            else:
                _order = list(range(max(1, len(steps))))
            plan_step = _order[0]
            auto_plan = ""
            _po: list[dict] = []
            for plan_step in _order:
                auto_plan, _po = iterate.ask_architect(
                    ws, task_id, contracts, iterate.inspect(ws, contracts), requirement,
                    thinking=pol.thinking_plan, plan="", want_plan=True,
                    step_index=plan_step)
                if (auto_plan or "").strip() or _po:
                    break
            if emit:
                emit("plan", {"by": (steps[plan_step].get("profile_id") + ":" +
                                     steps[plan_step].get("effort")) if steps else "remote",
                              "step": plan_step + 1, "of": len(steps),
                              "tier_policy": pol.spec_tier, "thinking": pol.thinking_plan,
                              "text": (auto_plan or "")[:600],
                              "orders": len(_po)})
        except Exception as exc:                         # noqa: BLE001
            auto_plan = ""
            if emit:
                emit("notice", {"message": f"开局方案生成跳过：{type(exc).__name__}: {exc}"})
    effective_plan = (plan or "").strip() or auto_plan

    # 治理文件：开跑就把"这一级的常驻规则"落到工作区。
    # 没有它，规模约束只活在这次的报告里，下一夜又得从零猜项目惯例（S 级就要求一份 AGENTS.md）。
    try:
        from . import governance as _gov
        gov = _gov.write_governance(ws, scope["level"], contracts, requirement)
        guides = _gov.write_guides(ws, scope["level"], contracts)
        if _scale.profile(scope["level"]).id in ("L", "XL"):
            _gov.write_dep_whitelist(ws, scope["level"])
        lock = _gov.lock_governance(ws, contracts)
        if emit:
            emit("governance", {"level": scope["level"], "wrote": gov.get("wrote") or [],
                                "skipped": gov.get("skipped") or [],
                                "lines": gov.get("lines"),
                                "guides": guides.get("wrote") or [],
                                "guides_over_limit": guides.get("over_limit") or [],
                                "locked": len((lock.get("files") or {}))})
        # 治理文件是否被上一轮改过（XL 级算硬，其余只报告）
        ver = _gov.verify_governance(ws, scope["level"], contracts)
        if ver.get("changed") and emit:
            emit("governance_changed", {"ok": ver.get("ok"), "note": ver.get("note")})
        # 升级触发：该换档了就说，不让人靠记性
        uh = _scale.upgrade_hint(scope.get("measured") or {}, scope["level"])
        if uh["should_upgrade"] and emit:
            emit("scale_upgrade", {"from": uh["from"], "to": uh["to"],
                                   "reasons": uh["reasons"], "note": uh["note"]})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"治理文件生成跳过：{type(exc).__name__}: {exc}"})

    # 临时方案台账：开跑先记一遍（谁在什么时候引入的临时方案，可追溯）
    try:
        from . import debt as _debt0
        _d0 = _debt0.sync(ws, scope["level"], 0)
        if emit:
            emit("debt", {"level": scope["level"], "found": len(_d0["found"]),
                          "new": len(_d0["new"]), "resolved": len(_d0["resolved"])})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"临时方案台账跳过：{type(exc).__name__}"})

    # 健康度基线：开跑时记一次，收工时对比才知道有没有漂移（只在收工算一次）
    try:
        from . import arch as _arch0
        _arch0.save_health(ws, _arch0.health_metrics(ws))
    except Exception:                                    # noqa: BLE001
        pass

    from . import budget_cap as _cap
    from . import oscillation as _osc
    # 定期巡检（cache-doctor / Doc-gardening）：开跑先扫一遍，结论进交付报告
    doctor_res: dict = {}
    try:
        from . import doctor as _doctor
        doctor_res = _doctor.run(ws, scope["level"])
        if emit:
            emit("doctor", {"level": scope["level"], "warn": doctor_res["warn"],
                            "findings": doctor_res["findings"][:4]})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"巡检跳过：{type(exc).__name__}"})

    # 腐烂分：开跑先算一遍（哪些模块已经烂到不该再打补丁）
    rot_map: dict[str, dict] = {}
    try:
        from . import rot as _rot0
        rot_map = _rot0.scores(ws, sorted(contracts), scope["level"])
        if emit:
            emit("rot_scores", {r: {"score": v["score"], "action": v["action"],
                                    "why": v["reasons"][:2]}
                                for r, v in rot_map.items()})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"腐烂分计算跳过：{type(exc).__name__}"})
    osc_tracker = _osc.Tracker()
    iso_degraded: list[dict] = []          # 隔离层降级记录（进交付报告）
    cost_cap = _cap.resolve_cap(max_cny)
    if emit and cost_cap:
        emit("cost_cap", {"cap_cny": cost_cap, "note": f"花费上限 ¥{cost_cap:.2f}，超过即收工"})

    for rnd in range(1, max_rounds + 1):
        # 钱到顶就停：只记花费不拦人的问题在这里补上
        if cost_cap:
            hit, info = _cap.over_cost_cap(task_id, cost_cap)
            if hit:
                reason = (f"花费到上限（¥{info.get('spent'):.4f} ≥ ¥{cost_cap:.2f}），"
                          f"收工（已列入交付报告）")
                if emit:
                    emit("cost_cap", {"cap_cny": cost_cap, "spent_cny": info.get("spent"),
                                      "action": "收工", "reason": reason})
                break
        left = budget_s - (time.time() - t0)
        if left <= 30:
            reason = f"预算耗尽（{hours}h）"
            break
        try:
            state = iterate.inspect(ws, contracts)
            verified = sum(s["verified"] for s in state.values())
            workorder.record_progress(task_id, rnd, state, note=f"夜班体检({strategies[strategy_i]})")
            if emit:
                emit("round", {"round": rnd, "verified": verified,
                               "total": sum(s["funcs"] for s in state.values()),
                               "left_min": round(left / 60, 1),
                               "strategy": strategies[strategy_i]})
            if verified > prev_verified:
                stall = 0
                stall_rounds = 0
            else:
                stall += 1
                stall_rounds += 1
            prev_verified = max(prev_verified, verified)

            # 振荡检测：回到来过的失败状态 = 这一轮改动没有净效果
            try:
                fp = _osc.fingerprint(state)
                osc = osc_tracker.add(rnd, fp)
                if osc["oscillating"]:
                    act = _osc.decide_action(osc, len(strategies) - 1 - strategy_i)
                    if emit:
                        emit("oscillation", {"round": rnd, "fp": fp,
                                             "first_round": osc["first_round"],
                                             "action": act["action"],
                                             "summary": _osc.summarize(state),
                                             "note": osc["note"]})
                    if act["action"] == "stop":
                        reason = "检测到振荡：" + act["reason"]
                        break
                    strategy_i = min(strategy_i + 1, len(strategies) - 1)
                    stall = 0
                    strat = strategies[strategy_i]
                    if emit:
                        emit("strategy", {"round": rnd, "strategy": strat,
                                          "why": "振荡后换打法"})
                    continue
            except Exception as exc:                     # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"振荡检测跳过：{type(exc).__name__}"})

            # 无增长 → 换策略继续（而不是收工）；阶梯走完一轮才考虑收工
            if stall >= 1 and strategy_i + 1 < len(strategies):
                strategy_i += 1
                stall = 0
                if emit:
                    emit("strategy", {"round": rnd, "strategy": strategies[strategy_i]})
            elif stall >= 3:
                reason = "策略阶梯已试完且连续 3 轮无增长，收工（已列入交付报告）"
                break
            strat = strategies[strategy_i]

            # 取单：优先架构师（architect 档及之后都问），否则机械派工
            root, orders = "", []
            ask_architect_ok = use_remote and strat in ("architect", "swap_model", "decompose")
            if ask_architect_ok and pol.arbiter == "human" and not (plan or "").strip():
                # 开发者模式：架构决策归人。没有方案就不许让远端替人拍板 —— 抛 needhuman 停下问人，
                # 这一轮只做机械派工（triage），不产生任何"系统自己定的架构"。
                ask_architect_ok = False
                if emit:
                    emit("needhuman", {
                        "round": rnd, "node": "architect",
                        "question": "需要架构决策，但还没有实现方案。请给出具体实现方案"
                                    "（模块划分/函数职责/数据流），系统只会照它实现，不会自己改架构。",
                        "why": "开发者模式下 plan_authority=human"})
                plan_waiting = True
            if ask_architect_ok:
                # 思考模式按阶梯走：第一次 off（便宜）；off 没解决（已经问过架构师且仍无增长）
                # 才升到 on，且整夜最多升一次 —— 别开局就为思考买单（docs/模型门槛 §4.0）。
                thinking = "off"
                if architect_turns >= 1 and stall_rounds >= 1 and think_on_used < MAX_THINK_ON:
                    thinking = "on"
                root, orders = iterate.ask_architect(ws, task_id, contracts, state, requirement,
                                                     thinking=thinking,
                                                     plan=effective_plan)
                architect_turns += 1
                if thinking == "on":
                    think_on_used += 1
                if emit:
                    emit("architect_thinking", {"round": rnd, "thinking": thinking,
                                                "architect_turns": architect_turns,
                                                "think_on_used": think_on_used})
                if emit and (root or orders):
                    emit("architect", {"round": rnd, "root_cause": root,
                                       "orders": [workorder.describe(o) for o in orders]})
            if not orders:
                orders = workorder.triage(ws, contracts, state)

            # 稳压器①：缺测试文件时先建空文件，避免 add_test 单永远"目标文件不存在"
            if strat in ("force_scaffold", "mechanical", "decompose"):
                for o in list(orders):
                    if o["kind"] == "add_test":
                        tp = ws / o["target"]
                        if not tp.exists():
                            tp.parent.mkdir(parents=True, exist_ok=True)
                            tp.write_text('"""测试（由夜班自动创建）。"""\n', encoding="utf-8")

            # 稳压器②：反复失败的单隔离，不再占用每轮配额
            def okey(o: dict) -> str:
                return f"{o['target']}::{o.get('symbol') or ''}::{o['kind']}"

            orders = [o for o in orders if blocked.get(okey(o), 0) < 3
                      and okey(o) not in tried_blocked]
            if not orders:
                # 全部被隔离 → 换更细的干法：对失败函数下"先写常量实现"的单
                pending = [(p, f) for p, st in state.items() for f in sorted(st["stubs"])]
                orders = [{"kind": "implement", "target": p, "symbol": f,
                           "acceptance": f"{f} 不再是桩", "detail":
                           f"先写出最小可运行实现：返回符合类型的常量/空聚合，之后再补逻辑"}
                          for p, f in pending[:3]]
                if emit and orders:
                    emit("strategy", {"round": rnd, "strategy": "decompose-minimal"})
            if not orders:
                tried_blocked |= {k for k, n in blocked.items() if n >= 3}
                # 原因必须按真实状态算：以前一律写"其余已隔离"，5/5 满分也这么说
                reason = no_work_reason(state, contracts)
                if blocked:
                    reason += f"（另有 {len(blocked)} 个反复失败的单已隔离）"
                break

            # 稳压器③：换一个**确实更强**的已安装模型
            # （以前是"7B 换 3B"，但实测 3B 交不出可解析代码，往下换等于浪费一轮）
            worker_model = model
            if strat == "swap_model":
                alt = _stronger_alternative(model)
                if alt:
                    worker_model = alt

            # ===== 新路径（唯一）：整文件生成 =====
            # 旧路径（逐符号本地写 + 远端救单）已废弃：把最弱的一环放在写代码的位置上，
            # 再用远端反复返工，既慢又贵。这里整体交给远端写整文件，本地只做机械验收。
            if use_remote and _remote_available() and pol.code_author in ("remote", "ladder"):
                _cst = _code_start_tier(pol, steps)
                if emit:
                    emit("code_tier", {"mode": getattr(pol, "effort_mode", "ladder"),
                                       "start": _cst, "of": len(steps),
                                       "model": (steps[_cst].get("profile_id") if steps else "?"),
                                       "why": ("单模型：思考强度由系统按需求/难度自动选"
                                               if getattr(pol, "effort_mode", "") == "auto"
                                               else "从最低强度起，逐档升")})
                stats = _author_round(ws, contracts, state, effective_plan, steps,
                                      tier_of, tier_fail, escalate_after, task_id,
                                      emit=emit, local_model=worker_model,
                                      budget_left=budget_s - (time.time() - t0),
                                      dec_used=dec_used, write_used=write_used,
                                      write_per_tier=write_per_tier,
                                      requirement=requirement, start_tier=_cst,
                                      effort_mode=str(getattr(pol, "effort_mode", "ladder")))
                remote_code_used += sum(1 for m in stats["modules"] if m.get("ok"))
                if stats.get("isolate_degraded"):
                    iso_degraded.extend(stats["isolate_degraded"])
                if stats.get("system_issues"):
                    try:
                        from . import author as _a2
                        _a2.save_issues(ws, stats["system_issues"])
                    except Exception:            # noqa: BLE001
                        pass
                if emit and stats["modules"]:
                    emit("author_round", {"round": rnd,
                                          "modules": [(m["path"], m["ok"], m["tier"])
                                                      for m in stats["modules"]],
                                          "issues": len(stats.get("system_issues") or [])})
                # 没有可写的模块了 → 收工（实测：8/8 之后还空转了 3 轮 architect，
                # 每轮都是"没有可执行的工作单"，纯白花）
                # ===== 第3项：批间闭环 =====
                # 当前批全部满分 → 冻结写盘 → 批次指针 +1（落盘，重启可续跑）
                try:
                    from . import project as _proj_bt
                    _order = _proj_bt.order_modules(contracts)
                    _batches = _proj_bt.batch_plan(_order, 4)
                    _bfile = ws / ".sixcoder" / "batches.json"
                    _bidx = 0
                    if _bfile.exists():
                        try:
                            _bidx = int(json.loads(_bfile.read_text(encoding="utf-8")
                                                   ).get("index") or 0)
                        except Exception:                # noqa: BLE001
                            _bidx = 0
                    if _batches:
                        _bidx = max(0, min(_bidx, len(_batches) - 1))
                        _cur = _batches[_bidx]
                        _all_done = all(needs_work_state(state, m) is False for m in _cur)
                        if _all_done and _bidx + 1 < len(_batches):
                            for _m in _cur:
                                _stb = state.get(_m) or {}
                                if int(_stb.get("verified") or 0) >= int(_stb.get("funcs") or 0):
                                    _proj_bt.freeze(ws, _m,
                                                    list(_stb.get("verified_names") or []),
                                                    reason=f"第 {_bidx + 1} 批完成")
                            _bidx += 1
                            _bfile.parent.mkdir(parents=True, exist_ok=True)
                            _bfile.write_text(json.dumps(
                                {"index": _bidx, "total": len(_batches),
                                 "batches": _batches, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                ensure_ascii=False, indent=1), encoding="utf-8")
                            if emit:
                                emit("batch_done", {"done_batch": _bidx,
                                                    "of": len(_batches),
                                                    "next": _batches[_bidx][:4],
                                                    "frozen": _cur[:6]})
                except Exception:                        # noqa: BLE001
                    pass
                if not stats["modules"]:
                    reason = no_work_reason(state, contracts)
                    if emit:
                        emit("stop", {"round": rnd, "reason": reason, "verified": verified})
                    break
                # 全都撞了写次数上限 → 再转下去只会白花 architect 的钱（实测空转了 7 轮）
                capped = stats.get("capped") or []
                if capped and not any(m.get("ok") for m in stats["modules"]):
                    reason = (f"写次数到顶，停止重写：{', '.join(capped[:4])}"
                              f"（每档最多 {stats.get('write_per_tier') or write_per_tier} 次，可用档位已试完，已列入交付报告）")
                    if emit:
                        emit("stop", {"round": rnd, "reason": reason})
                    break
                continue                       # 下一轮重新体检，看还差什么

            done = 0
            for o in orders[:5]:
                if budget_s - (time.time() - t0) <= 20:
                    break
                oid = workorder.add(task_id, rnd, o["kind"], o["target"], o.get("symbol", ""),
                                    o.get("acceptance", ""), o.get("detail", ""))
                # 回归保护①：动手前留一份字节快照（worker 可能把"已经正确"的函数改坏）
                target = o.get("target") or ""
                fp = (ws / target) if target.endswith(".py") else None
                try:
                    before_bytes = fp.read_bytes() if fp and fp.exists() else None
                except OSError:
                    before_bytes = None
                prev_mod_verified = dict(state.get(target) or {}).get("verified", 0)
                try:
                    if (o.get("kind") or "") == "add_test":
                        # add_test 不走模型：断言由契约渲染（模型写的弱测试是实测过的坑）
                        r = iterate.add_engine_tests(ws, contracts, o.get("target") or "")
                    elif use_remote and _remote_available() and \
                            min(tier_of.get(okey(o), 0), max(0, len(steps) - 1)) > 0:
                        # 阶梯第 0 档是**本地模型**（0 token，省钱的默认）。
                        # 只有本地连续失败到阈值，才升到远端：flash:none → … → pro:max。
                        lvl = min(tier_of.get(okey(o), 0), max(0, len(steps) - 1))
                        step = steps[lvl]
                        r = iterate.execute_order_remote(
                            ws, o, evidence=str(o.get("detail") or ""), brief="",
                            task_id=task_id, emit=emit, plan=effective_plan,
                            contract=contracts.get(o.get("target")), as_author=True,
                            step_index=lvl)
                        used_tier[okey(o)] = lvl
                        remote_code_used += 1
                        if emit:
                            emit("tier", {"order": workorder.describe(o)[:60],
                                          "tier": lvl + 1, "of": len(steps),
                                          "by": f"{step.get('profile_id')}:{step.get('effort')}",
                                          "ok": bool(r.get("ok"))})
                        if not r.get("ok") and not _is_transient_fail(r.get("reason")):
                            n_try = tier_fail.get(okey(o), 0) + 1
                            tier_fail[okey(o)] = n_try
                            if n_try >= escalate_after and lvl + 1 < len(steps):
                                tier_of[okey(o)] = lvl + 1
                                tier_fail[okey(o)] = 0
                                if emit:
                                    nxt = steps[lvl + 1]
                                    emit("escalate", {
                                        "order": workorder.describe(o)[:60],
                                        "from": f"{step.get('profile_id')}:{step.get('effort')}",
                                        "to": f"{nxt.get('profile_id')}:{nxt.get('effort')}",
                                        "fails": n_try})
                        elif r.get("ok"):
                            tier_fail[okey(o)] = 0
                    else:
                        # 第 0 档：本地模型（0 token）。失败达阈值后下一轮自动升到远端。
                        r = iterate.execute_local(ws, o, worker_model, attempts=2, emit=emit,
                                                  contract=contracts.get(o.get("target")),
                                                  plan=effective_plan)
                        if not r.get("ok") and not _is_transient_fail(r.get("reason")):
                            n_try = tier_fail.get(okey(o), 0) + 1
                            tier_fail[okey(o)] = n_try
                            if n_try >= escalate_after and len(steps) > 1:
                                tier_of[okey(o)] = 1
                                tier_fail[okey(o)] = 0
                                if emit:
                                    nxt = steps[1]
                                    emit("escalate", {
                                        "order": workorder.describe(o)[:60],
                                        "from": "local",
                                        "to": f"{nxt.get('profile_id')}:{nxt.get('effort')}",
                                        "fails": n_try})
                except Exception as exc:             # 单张异常一律降级为"这张没过"
                    r = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:120]}
                after = iterate.inspect(ws, contracts)
                # 回归保护②：该模块已验收数掉了 → 回滚文件，这张单判失败
                if before_bytes is not None and \
                        (after.get(target) or {}).get("verified", 0) < prev_mod_verified:
                    try:
                        fp.write_bytes(before_bytes)
                    except OSError:
                        pass
                    after = iterate.inspect(ws, contracts)
                    r = {"ok": False,
                         "reason": f"回归：这张单让 {target} 的已验收数从 "
                                   f"{prev_mod_verified} 掉到 "
                                   f"{(after.get(target) or {}).get('verified', 0)}，已回滚"}
                now_verified = sum(s["verified"] for s in after.values())
                improved = now_verified > verified or bool(r.get("files"))
                workorder.close(oid, bool(r.get("ok") and improved),
                                json.dumps(r, ensure_ascii=False)[:800])
                if emit:
                    emit("order_done" if (r.get("ok") and improved) else "order_failed",
                         {"round": rnd, "order": workorder.describe(o),
                          "applied": r.get("applied"), "reason": r.get("reason")})
                if r.get("ok") and improved:
                    done += 1
                    blocked[okey(o)] = 0
                    abandoned.pop(okey(o), None)
                    # 解决之后**立刻回到最低档**：高档次只用来"把这一单救回来"，
                    # 不该被后面的单继承 —— 否则一次难题会让整晚都用最贵的档跑。
                    if tier_of.get(okey(o), 0) > 0:
                        if emit:
                            emit("tier_reset", {"order": workorder.describe(o)[:60],
                                                "from": tier_of.get(okey(o), 0) + 1})
                        tier_of[okey(o)] = 0
                    tier_fail[okey(o)] = 0
                else:
                    # 隔离前的最后一档：让远端直接把这张单做出来。
                    # 什么时候升级**由模式决定**（core/policy.py）：甲方模式下一旦本地被硬判据
                    # 判红就升级（不再等满 2 次，本地小模型做不出来的东西多试只是烧时间）；
                    # 开发者模式仍按"连续失败 2 次 + 人工批准"走。
                    n_fail = blocked.get(okey(o), 0) + 1
                    hard = _is_hard_failure(r.get("reason"))
                    go, why = _policy.should_escalate(pol, n_fail, hard, remote_code_used)
                    if go and _remote_available():
                        remote_code_used += 1
                        if emit:
                            emit("escalate", {"round": rnd, "order": workorder.describe(o),
                                              "why": why, "used": remote_code_used,
                                              "budget": pol.remote_code_budget})
                        try:
                            rr = iterate.execute_order_remote(
                                ws, o, evidence=str(r.get("reason") or ""),
                                brief=str(o.get("detail") or ""), task_id=task_id, emit=emit,
                                plan=effective_plan,
                                contract=contracts.get(o.get("target")))
                        except Exception as exc:         # noqa: BLE001
                            rr = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
                        if emit:
                            emit("rescue", {"round": rnd, "order": workorder.describe(o),
                                            "ok": bool(rr.get("ok")),
                                            "reason": str(rr.get("reason") or "")[:200]})
                        if rr.get("ok"):
                            after2 = iterate.inspect(ws, contracts)
                            grew = sum(s["verified"] for s in after2.values()) > now_verified
                            blocked[okey(o)] = 0
                            abandoned.pop(okey(o), None)
                            if grew:
                                done += 1
                            workorder.close(oid, True, json.dumps(rr, ensure_ascii=False)[:400])
                            continue
                        r = rr                           # 远端也没救回来：把它的原因记进阻断统计
                    blocked[okey(o)] = n_fail
                    reason_txt = str(r.get("reason") or "（没有更具体的原因）")[:300]
                    rec = abandoned.setdefault(okey(o), {
                        "order": workorder.describe(o), "target": o.get("target"),
                        "symbol": o.get("symbol") or "", "fails": 0,
                        "reason": reason_txt, "detail": str(o.get("detail") or "")[:160],
                    })
                    rec["fails"] = n_fail
                    rec["reason"] = reason_txt
                    rec["remote_tried"] = bool(_remote_available() and n_fail >= 2)
            hist.append({"round": rnd, "verified": verified, "orders": len(orders),
                         "done": done, "strategy": strat})
            if done:
                fatal_rounds = 0
        except Exception as exc:                     # 整轮异常也不许收工（连续 3 轮才认输）
            fatal_rounds += 1
            if emit:
                emit("round_error", {"round": rnd, "error": f"{type(exc).__name__}: {exc}"[:160]})
            hist.append({"round": rnd, "verified": prev_verified if prev_verified > 0 else 0,
                         "orders": 0, "done": 0, "strategy": "error"})
            if fatal_rounds >= 3:
                reason = f"连续 3 轮异常，收工：{type(exc).__name__}"
                break
            time.sleep(2)
            continue

    final = iterate.inspect(ws, contracts)
    verified = sum(s["verified"] for s in final.values())
    total = sum(s["funcs"] for s in final.values())
    # 关键节点守卫：开发者模式在"标记可交付"前要人点头（甲方模式自动放行）
    approved = True
    try:
        from . import guards
        g = guards.gate_deliver(ws, verified, total, reason, emit=emit)
        approved = bool(g.get("ok"))
        if not approved and emit:
            emit("notice", {"message": f"交付未获批准：{g.get('reason')}"})
    except Exception as exc:                           # noqa: BLE001
        if emit:
            emit("notice", {"message": f"交付审批跳过：{type(exc).__name__}"})
    if not approved:
        reason = f"人工未批准交付（{reason}）"
    # 分级门禁（L/XL 的覆盖率/变异/类型）与"契约验收"是两套标准。实测（实验台 large-l）：
    # 报告抬头写"验收 7/7 全部通过"，而同一份报告下面写着"变异杀死率 33%（要求 ≥70%）未通过"、
    # "typecheck 未通过：已阻断" —— 甲方只看抬头就等于被误导。这里把门禁结论提到收工原因里，
    # 一次算好塞进 scope，避免后面重复跑变异测试。
    try:
        from . import arch as _arch0
        from . import scale as _scale0
        _lv = str((scope or {}).get("level") or "S")
        _chg = {p: ("", (Path(ws) / p).read_text(encoding="utf-8", errors="replace"))
                for p in sorted((final or {}).keys()) if (Path(ws) / p).exists()}
        _rows = list(_arch0.level_gates(ws, _lv, _chg))
        _blocked = [g for g in _rows if not g.ok and g.evidence_ref not in
                    ("skipped", "unavailable", "no-layers", "no-mutants", "error")]
        if _blocked:
            _names = "、".join(g.gate for g in _blocked[:4])
            reason += f"｜**但 {_lv} 级门禁 {len(_blocked)} 项未通过（{_names}）** —— 按本级标准不算合格"
            if emit:
                emit("notice", {"message": f"{_lv} 级门禁未通过：{_names}"})
        scope = {**(scope or {}), "level_gates": _rows}
    except Exception as exc:                            # noqa: BLE001
        if emit:
            emit("notice", {"message": f"分级门禁复核跳过：{type(exc).__name__}"})
    try:
        from . import project as _proj_b
        _proj_b.save_baseline(ws, final, tag=f"run-{rnd}")
        _dif = _proj_b.diff_baseline(ws, final)
        if _dif.get("regressed") and emit:
            emit("notice", {"message": f"与上一份基线相比有退化：{_dif['note'][:140]}"})
    except Exception:                                    # noqa: BLE001
        pass
    # 跨模块接线 + 业务流：这两层是"单函数判据看不见"的地方，必须进交付报告。
    # 注意 integration_smoke 之前**只被自己的 check 调用过**（有函数没接入点）——
    # 这里一并补上，免得机制清单上写着"已实现"而夜班从来没用过。
    flow_smoke = biz_flow = None
    try:
        from . import flow as _flow_mod
        flow_smoke = _flow_mod.integration_smoke(ws, contracts)
        if emit:
            emit("flow_smoke", {"ok": flow_smoke.get("ok"),
                                "fails": (flow_smoke.get("fails") or [])[:3]})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"集成冒烟跳过：{type(exc).__name__}"})
    try:
        from . import flow as _flow_mod2
        biz_flow = _flow_mod2.business_flow(ws, contracts)
        if emit:
            emit("business_flow", {"ok": biz_flow.get("ok"),
                                   "applicable": biz_flow.get("applicable"),
                                   "passed": biz_flow.get("passed"),
                                   "total": biz_flow.get("total")})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"业务流跳过：{type(exc).__name__}"})
    # 能力上限台账：收工时现算"这次做到哪一步"，并把证据登记进台账（不是手写）。
    # 自主实证的"人工介入数"从事件库里数（needhuman/question/plan_gate/approval），
    # 数不到就写 None —— None 不算自主实证，宁可承认没有。
    ceiling_res = None
    try:
        from . import ceiling as _ceil
        _usage: dict = {}
        try:
            from . import remote as _rm
            _usage = _rm.usage_summary(task_id) or {}
        except Exception:                            # noqa: BLE001
            _usage = {}
        _cost = float(_usage.get("cost_cny") or 0.0)
        _human: int | None = None
        try:
            from . import db as _dbx
            _r = _dbx.query(
                "SELECT count(*) AS n FROM events WHERE task_id=? AND kind IN "
                "('needhuman','question','plan_gate','approval','approval_pending')",
                (task_id,))
            _human = int((_r[0].get("n") if _r else 0) or 0)
        except Exception:                            # noqa: BLE001
            _human = None
        _lv_now = str((scope or {}).get("level") or "S")
        _ceil.record_scale_evidence(ws, _lv_now, verified, total, cost_cny=_cost,
                                    reason=str(reason or ""))
        _ceil.record_autonomy(ws, _lv_now, verified, total,
                              mode=str(getattr(pol, "mode", "") or ""),
                              human_interventions=_human, cost_cny=_cost,
                              # 「真干了活」不能只看轮次统计里的 orders 字段：
                              # 实测（round-01a 全部 20/20 通过、写了 4 个模块 226 行）
                              # 却被记成 did_work=False —— 因为那个字段那一轮没被填。
                              # 只要这一轮有函数通过机械验收，就说明真的写了代码。
                              did_work=bool(verified) or any(
                                  int(h.get("orders") or 0) > 0 for h in (hist or [])))
        ceiling_res = _ceil.probe_all(ws, contracts, level=_lv_now)
        if emit:
            emit("ceiling", {"counts": ceiling_res.get("counts"),
                             "lines": ceiling_res.get("lines")})
        # 收工时按真实账本校准一次（auto 可关；样本不足的项会自动保持原样）
        try:
            from . import calibration as _cal_end
            _cal_out = _cal_end.recompute(ws)
            if emit:
                emit("calibration", {"changed": _cal_out.get("changed") or [],
                                     "applied": {k: v for k, v in (_cal_out.get("applied") or {}).items()
                                                 if k in ("reasoning_reserve", "content_floor",
                                                          "safety_cap", "module_budget_cny",
                                                          "code_effort_ceiling")},
                                     "basis": _cal_out.get("basis") or {}})
        except Exception as exc:                     # noqa: BLE001
            if emit:
                emit("notice", {"message": f"校准跳过：{type(exc).__name__}"})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"能力上限台账跳过：{type(exc).__name__}"})
    delivery = write_delivery(ws, requirement, final, hist, reason, time.time() - t0,
                              abandoned=abandoned, remote_used=remote_code_used,
                              plan_used=bool((effective_plan or "").strip()),
                              plan_by=("human" if (plan or "").strip() else
                                       ("remote" if (auto_plan or "").strip() else "")),
                              scope={**(scope or {}), "task_id": task_id, "max_cny": cost_cap, "rounds": rnd,
                                     "rot": rot_map, "doctor": doctor_res,
                                     "isolate_degraded": iso_degraded,
                                     "flow_smoke": flow_smoke, "business_flow": biz_flow,
                                     "ceiling": ceiling_res,
                                     "oscillation": osc_tracker.to_dict()})
    _save_abandoned(ws, abandoned)
    # 追溯矩阵：需求条目 → 契约 → 判据 → 证据 → 产物（甲方要能"三十秒答出来"）
    try:
        from . import e2e as e2e_mod, trace as trace_mod
        trace_mod.write(ws, requirement, contracts, final,
                        iterate.last_e2e(ws) or e2e_mod.write(ws, contracts, requirement))
    except Exception as exc:                           # noqa: BLE001
        if emit:
            emit("notice", {"message": f"追溯矩阵生成跳过：{type(exc).__name__}"})
    res = NightResult(True, rounds=hist, verified=verified, total=total,
                      modules={k: {"funcs": v["funcs"], "verified": v["verified"],
                                   "stubs": sorted(v["stubs"])} for k, v in final.items()},
                      delivery_path=str(delivery), reason=reason or "完成",
                      spent_s=round(time.time() - t0, 1))
    # 日志固定写到仓库根的 _logs/，不跟随 cwd（否则从不同目录启动会散成好几处，实测踩过）
    logs = Path(__file__).resolve().parent.parent.parent / "_logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / f"night-{task_id}.json").write_text(
        json.dumps({"result": res.__dict__, "curve": workorder.progress_curve(task_id)},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return res


def _missing_deps(ws: Path) -> list[str]:
    """扫工作区里的 import，找出**本机装不上**的第三方模块（只报告，绝不安装）。

    为什么只报告：`pip install` 会在安装期执行别人写的构建脚本，而这台机器上有明文远端 Key；
    甲方明确选择"自己装、不放开 pip"。所以引擎只负责**说清楚缺什么、建议装哪个版本**，
    执行权留在人手里（对齐 docs/13 判定权分离与工作区边界）。
    """
    import ast
    import importlib.util
    import sys as _sys

    names: set[str] = set()
    local = {p.stem for p in Path(ws).glob("*.py")}
    for p in Path(ws).glob("*.py"):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    std = getattr(_sys, "stdlib_module_names", set())
    miss: list[str] = []
    for n in sorted(names):
        if not n or n in std or n in local or n.startswith("_"):
            continue
        try:
            if importlib.util.find_spec(n) is None:
                miss.append(n)
        except Exception:                                # noqa: BLE001  （父包缺失等）
            miss.append(n)
    return miss


def _load_abandoned(ws: Path) -> dict:
    """读回上一次夜班记下的"被放弃的工作单"（让重新生成报告时不丢这段）。"""
    p = Path(ws) / ".sixcoder" / "abandoned.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _save_abandoned(ws: Path, abandoned: dict) -> None:
    try:
        d = Path(ws) / ".sixcoder"
        d.mkdir(parents=True, exist_ok=True)
        (d / "abandoned.json").write_text(json.dumps(abandoned, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass


def _quality_section(ws: Path, rels: list[str], state: dict, level: str = "S") -> list[str]:
    """交付报告里"代码质量与证据强度"这一节。

    为什么必须写进来：甲方早上要看的不只是"几个函数过了判据"，还有三件更要命的事——
    1. 哪些输入会把已经通过的函数打崩（边界矩阵判红的，机械证据在手）；
    2. 代码还能不能改（圈复杂度/嵌套/行数）；
    3. 交付的测试文件到底有没有信息量（变异体一个都杀不死 = 等于没测）。
    这一节里所有数字都是现场跑出来的，跑不出来就写"跳过"，不写猜测值。
    """
    from . import gates as _gates

    lines = ["", "## 代码质量与证据强度", ""]

    # 1) 边界矩阵判红的函数：这些输入会把"已通过"的实现打崩
    crashed = []
    for path, s in (state or {}).items():
        names = s.get("hard_failed") or []
        if not names:
            continue
        detail = [str(x) for x in (s.get("fails") or []) if any(n in str(x) for n in names)]
        crashed.append((path, names, detail[:2]))
    if crashed:
        lines += ["### 已知会崩的输入（边界判据判红）", "",
                  "| 模块 | 函数 | 具体反例 |", "|---|---|---|"]
        for path, names, detail in crashed:
            lines.append(f"| {path} | {', '.join(names)} | "
                         f"{(detail[0] if detail else '见 TRACE.md')[:150]} |")
        lines += ["", "> 这些函数**不算验收通过**，分诊会把它们继续派单。"
                      "示例驱动看不见这一类错：它的样本永远是最舒服的那一条。"]
    else:
        lines += ["- 边界矩阵（空/None/缺字段/超长/类型错）：全部通过"]

    # 2) 可维护性（阈值按代码规模档位取，不是一刀切）
    try:
        from . import scale as _scale2
        lim = _scale2.quality_limits(level)
        rows = _gates.measure_quality(ws, rels)
        qm = _gates.gate_quality_metrics(ws, rels, level=level)
        if rows:
            worst = max(rows, key=lambda r: (r["complexity"], r["lines"]))
            over = qm.ok is False
            lines += ["", "### 可维护性（纯 ast 计算）", "",
                      f"- {'全部在阈值内' if not over else '**有超出阈值的地方**'}"
                      f"（{level} 级阈值：复杂度≤{lim['complexity']}、嵌套≤{lim['depth']}、"
                      f"单函数≤{lim['lines']} 行）：{qm.detail[:200]}",
                      f"- 最复杂的一处：`{worst['file']}::{worst['func']}()` "
                      f"复杂度 {worst['complexity']}、嵌套 {worst['depth']} 层、{worst['lines']} 行"]
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 可维护性：跳过（{type(exc).__name__}）")

    # 3) 测试有效性：变异体一个都杀不死 = 测试没碰到这段代码
    try:
        from . import scale as _scale
        mut_mode = _scale.gate_mode("mutation", level)
        tests = sorted(p.name for p in Path(ws).glob("test_*.py"))
        if mut_mode == "skip":
            lines += ["", "### 测试有效性", "",
                      f"- **{level} 级不跑变异测试**：这一级的判断是覆盖率与判据足以代表测试有效性，"
                      f"变异测试的固定成本高于它能揭示的问题（L 级起才跑，且只对变更行）。",
                      f"- 现有测试文件：{', '.join(tests) if tests else '（无）'}"]
        elif not tests:
            lines += ["", "### 测试有效性", "", "- 工作区里没有 pytest 测试文件，**测试交付这一项未达标**"]
        else:
            lines += ["", "### 测试有效性（变异测试）", "",
                      "| 模块 | 变异体 | 杀死 | 结论 |", "|---|---|---|---|"]
            any_zero = False
            base_red = False
            for rel in rels:
                p = Path(ws) / rel
                if not rel.endswith(".py") or not p.exists():
                    continue
                src = p.read_text(encoding="utf-8-sig", errors="replace")
                mm = _gates.gate_mutation(ws, {rel: ("", src)}, tests)
                ref = str(mm.evidence_ref or "")
                got, _, tot = ref.replace("killed=", "").partition("/")
                if ref == "baseline-red":
                    lines.append(f"| {rel} | — | — | **测试自己就跑不过，先修测试** |")
                    base_red = True
                    continue
                zero = bool(got) and got.isdigit() and int(got) == 0
                any_zero = any_zero or zero
                verdict = ("**测试没盯住这段代码（不能当证据用）**" if zero
                           else "有信息量" if mm.ok else "偏弱")
                lines.append(f"| {rel} | {tot or '—'} | {got or '—'} | {verdict} |")
            if base_red:
                lines += ["", "> 测试基线本身就是红的：未变异时测试就不通过。"
                              "这时**任何变异都会显得被杀死了**，所以这一项不给结论，先修测试。"]
            if any_zero:
                lines += ["", "> 杀不死变异体说明这些测试只跑通了 happy path。"
                              "它们是交付物的一部分，但**不要拿它们当「这个函数对」的证据**。"]
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 测试有效性：跳过（{type(exc).__name__}）")

    return lines


# 粒度自动选择的分界线：超过这个行数就改用「骨架 + 只回函数体」。
# 为什么是 400：整份重写的输出量随文件线性增长，而真正要改的通常只有几个函数；
# 400 行以上整份重写既贵又容易顺手改坏已验证的部分（实测 152 行的 store.py 重写要 1.5k token）。
BIG_FILE_LINES = 400


def no_work_reason(state: dict, contracts: dict) -> str:
    """没有可执行工作单时，给出**真实**原因（不能一律说"其余已隔离"）。

    实测：计算器修好后 5/5 满分，收工原因却写成"其余已隔离，需人工或换模型"，
    看起来像系统放弃了 —— 实际是全部通过。同一段文字以前还有过反向的假绿
    （验收 0/8 却说"已全部通过"），所以两种错都必须在这一处算清楚。
    """
    _v = sum(int((state.get(p) or {}).get("verified") or 0) for p in contracts)
    _t = sum(int((state.get(p) or {}).get("funcs") or 0) for p in contracts)
    if any((state.get(p) or {}).get("engine_errors") for p in contracts):
        return (f"**引擎判据自身跑不动（不是产物错）**：{_v}/{_t} 已验收，"
                f"其余判不了 —— 检查探针/环境，别改代码")
    if _t and _v >= _t:
        return f"契约内函数已全部通过机械验收（{_v}/{_t}，没有可执行的工作单）"
    why: list[str] = []
    for p2, s2 in state.items():
        if s2.get("missing"):
            why.append(f"{p2} 缺 {', '.join(s2['missing'][:3])}")
        elif s2.get("stubs"):
            why.append(f"{p2} 仍是桩 {', '.join(sorted(s2['stubs'])[:3])}")
        elif s2.get("hard_failed"):
            why.append(f"{p2} 判红 {', '.join(s2['hard_failed'][:3])}")
        elif s2.get("untested"):
            why.append(f"{p2} 无判据 {', '.join(s2['untested'][:3])}")
    return (f"没有可执行的工作单，但**验收只有 {_v}/{_t}**："
            + ("；".join(why[:3]) or "详见交付报告"))


def needs_work_state(state: dict, path: str) -> bool:
    """这个模块还需不需要干活（唯一判定点，自检与探针都调它）。

    判定为"要干活"的情形：
      · 有判红函数 / 没判据函数 / 仍是桩
      · 文件不存在
      · **契约函数整段缺失**（被删或改名）—— 这一条以前漏了，
        导致"函数全没了"被当成"已完成"，收工理由还写着"全部通过验收"。
      · 验收数 < 契约函数数（兜底：只要不满分就还有活）
    """
    st = (state or {}).get(path) or {}
    # 判据自身跑不动（引擎问题）→ **不派单**：写再多遍也过不了这台坏秤，
    # 只会白花远端调用并触发振荡（实测路径 bug 空转了 6 轮）。这种情况要在报告里说清。
    if st.get("engine_errors"):
        return False
    return (bool(st.get("hard_failed")) or bool(st.get("untested"))
            or bool(st.get("stubs")) or not st.get("exists")
            or bool(st.get("missing"))
            or int(st.get("verified") or 0) < int(st.get("funcs") or 0))


def _auto_effort(contracts: dict, deps_map: dict, rel: str, fails: int = 0,
                 stub: bool = False, lines: int = 0) -> tuple[str, int]:
    """按"这次要写什么、有多难"给出思考强度（单模型下的自动挡）。

    只吃确定性事实：依赖数（项目地图）、函数数、契约示例数、历史失败次数、是不是桩、文件行数。
    返回 (想要哪一档, 难度分)，由 `effort.pick()` 在当前模型支持的那几档里落地。
    """
    from . import effort as _eff
    c = contracts.get(rel)
    fs = list(getattr(c, "funcs", None) or [])
    score = _eff.difficulty(
        deps=len((deps_map or {}).get(rel) or []),
        funcs=len(fs),
        examples=sum(len(getattr(f, "examples", None) or []) for f in fs),
        fails=int(fails or 0), stub=bool(stub), lines=int(lines or 0))
    return _eff.route("code", score), score


def _code_start_tier(pol, steps: list[dict]) -> int:
    """代码从哪一档起（**单模型**：就是"从哪一档思考强度起"）。

    `effort_mode="auto"`（甲方模式）→ 规格/方案走最深、代码按难度路由（见 _auto_effort）；
    `"ladder"`（开发者模式）→ 第 0 档（最省）；
    `"max"` → 该模型最深的一档。
    """
    try:
        mode = str(getattr(pol, "effort_mode", "ladder"))
        if not steps:
            return 0
        if mode == "max":
            return max(0, len(steps) - 1)
        return 0
    except Exception:                                    # noqa: BLE001
        return 0


def _author_round(ws: Path, contracts: dict, state: dict, plan: str, steps: list[dict],
                  tier_of: dict, tier_fail: dict, escalate_after: int, task_id: str,
                  emit=None, local_model: str = "", max_modules: int = 3,
                  budget_left: float = 600.0, dec_used: dict | None = None,
                  write_used: dict | None = None, write_per_tier: int = 0,
                  requirement: str = "", start_tier: int = 0,
                  effort_mode: str = "ladder") -> dict:
    """**新路径（唯一）**：按模块整文件生成 → 本地全判据验收 → 窄证据重试 → 失败升档。

    旧路径（逐符号本地写 + 远端救单）已废弃：它把最弱的一环放在写代码的位置上，
    再用远端反复返工，既慢又贵。整文件路径把"写"整体交给远端，本地只做机械验证。
    """
    from . import author as _author
    from . import scale as _scale_mod      # 分级参数（阈值/档位/上限）
    import os as _osr                     # 环境变量覆盖（round/隔离后端）

    # 依赖图（只读一次）：思考强度路由要按"这个模块有几个依赖"判断难度
    try:
        from . import project as _proj_map
        _deps_map = dict((_proj_map.load_map(ws) or {}).get("deps") or {})
        if not _deps_map:
            _deps_map = dict(_proj_map.build_map(contracts).get("deps") or {})
    except Exception:                                    # noqa: BLE001
        _deps_map = {}
    # 本轮的起点：单模块预算只统计**这一轮**的花费（历史账不能拖累新的一轮）
    try:
        _round_since = db.now()
    except Exception:                                    # noqa: BLE001
        _round_since = ""

    def needs_work(path: str) -> bool:
        return needs_work_state(state, path)

    def _verify_module(path: str) -> dict:
        """行级补丁打完后的复验：只验这一个模块，判据全过才算过。

        以前这里写的是 `_verify(...)`，而那个函数在旧路径删掉时一起没了 ——
        行级补丁这条路一旦真用上就 NameError（用静态未定义名字检查抓到的）。
        """
        try:
            st = iterate.inspect(ws, {path: contracts.get(path)})
            cur = st.get(path) or {}
            total = int(cur.get("funcs") or 0)
            verified = int(cur.get("verified") or 0)
            ok = bool(total) and verified >= total
            fb = "；".join([str(x) for x in (cur.get("fails") or [])[:2]])
            if not ok and not fb:
                fb = f"仍差 {total - verified} 个函数没过判据"
            return {"ok": ok, "feedback": fb}
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "feedback": f"复验出错：{type(exc).__name__}: {exc}"}

    # #16「单 PR 单主题」的等价物：M 级以上一轮只动一个模块。
    # 一次改多个模块会让"这次改动到底是谁引起的失败"说不清，也让 diff 巨大无法审查。
    try:
        _lv16 = _scale_mod.resolve("", ws)["level"]
    except Exception:                                    # noqa: BLE001
        _lv16 = "S"
    if _lv16 in ("M", "L", "XL"):
        # **第4层（依赖序批量）**：模块多的时候一轮只推 1 个 = 永远写不完
        # （实测 15 模块大项目 355 秒只推进 3 个）。按依赖序取一批，批内并行写。
        _nmod = len(contracts or {})
        max_modules = 1 if _nmod <= 6 else (3 if _nmod <= 12 else 4)
    try:
        from . import project as _proj2
        _ordered = [p for p in _proj2.order_modules(contracts) if p in contracts]
    except Exception:                                    # noqa: BLE001
        _ordered = sorted(contracts)
    mods = [p for p in _ordered if needs_work(p)][:max_modules]
    if not mods:
        return {"modules": [], "ok": True}

    stats = {"modules": [], "ok": True, "system_issues": []}
    # 断点续跑：开跑前先把"哪些已完成/可续写/全新"算出来并留痕（进程重启后照样能接着写）
    try:
        from . import project as _proj_r
        _plan = _proj_r.resume_plan(ws, contracts, state)
        if emit:
            emit("resume_plan", {"done": len(_plan["done"]), "resume": len(_plan["resume"]),
                                 "fresh": len(_plan["fresh"]), "next": _plan["next"],
                                 "note": _plan["note"][:160]})
        _guard = _proj_r.regression_guard(ws, state)
        if _guard.get("blocked"):
            if emit:
                emit("notice", {"message": f"回归硬门禁：{_guard['reason'][:160]}"})
            stats.setdefault("regressed", []).extend(_guard["blocked"])
    except Exception:                                    # noqa: BLE001
        pass
    # 决策表预算：**跨轮累计**（由 _night_inner 持有）。回滚不清零，
    # 否则"整文件只重写一次"会退化成"每轮都重写一次"，把升档预算一起烧掉。
    dec_used = dec_used if dec_used is not None else {}
    # 写次数预算：**按档**计，不是整夜一刀切。
    # 实测两面：
    #   · 不管它 → clean.py 被问 17–24 次、吃掉某次 59–86% 的钱（几乎都在同一个档上重复）；
    #   · 整夜只给 3 次 → store.py 在第 3 次（flash:high）被拦，而历史上正是第 4 档
    #     flash:max 把它救回来的（night-v2b 实测：6/8 收工，差的就是最后那一档）。
    # 所以规则是"同一档最多重写 cap_per_tier 次，换档不占额度"，另加一个整夜总上限兜底。
    # cap_per_tier 按**代码规模**给：S 3 次 / M 2 次 / L 1 次（大项目每次重写都是审查负担）。
    write_used = write_used if write_used is not None else {}
    # 受保护区与写次数都按**规模**定：先定级，再用档位参数
    from . import scale as _scale_mod
    _lv = _scale_mod.resolve("", ws)["level"]
    zone_mode = _scale_mod.gate_mode("protected_zones", _lv)
    if not write_per_tier:
        write_per_tier = _scale_mod.write_per_tier(_lv)
    cap_per_tier = max(1, int(write_per_tier))
    cap_total = max(cap_per_tier * max(1, len(steps)), cap_per_tier * 4)
    stats["write_per_tier"] = cap_per_tier      # 回传给收工消息（按规模解析后的真值）

    def _wu(rel: str) -> dict:
        rec = write_used.get(rel)
        if not isinstance(rec, dict):
            rec = {"total": 0, "tiers": {}}
            write_used[rel] = rec
        return rec

    for rel in mods:
        if budget_left <= 20:
            break
        hit = _scale_mod.protected_hits(ws, [rel])
        if hit and zone_mode == "block":
            stats["modules"].append({"path": rel, "ok": False, "tier": 0, "by": "-",
                                     "reason": "受保护区：这类文件不允许自动改（L 级以上策略）",
                                     "applied": []})
            stats.setdefault("capped", []).append(rel)
            if emit:
                emit("protected_zone", {"path": rel, "action": "拒绝",
                                        "message": "认证/部署/治理文件不在自动修改范围内"})
            continue
        if hit and emit:
            emit("protected_zone", {"path": rel, "action": "警告",
                                    "message": f"{_scale_mod.resolve('', ws)['level']} 级只警告不阻断"})
        # 单模型的"自动挡"：按这次要写的模块有多难，决定从哪一档思考强度起。
        # 只在首轮生效（之后仍然按失败逐档升强度），所以它只是"起点更聪明"，不改升级语义。
        if str(effort_mode) == "auto" and steps:
            try:
                from . import effort as _eff_m
                _c = contracts.get(rel)
                _fails = int((_wu(rel).get("total") or 0))
                try:
                    _src_txt = (ws / rel).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    _src_txt = ""
                _want, _score = _auto_effort(contracts, _deps_map, rel, _fails,
                                             stub=(not _src_txt.strip()
                                                   or "raise NotImplementedError" in _src_txt),
                                             lines=len(_src_txt.splitlines()))
                _lvl0 = _eff_m.pick(steps, _want)
                tier_of.setdefault(rel, _lvl0)
                if emit:
                    _ev = _eff_m.explain("code", _score, _want, steps, _lvl0)
                    _ev["path"] = rel
                    emit("effort_route", _ev)
            except Exception as _exc:                # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"思考强度路由跳过：{type(_exc).__name__}"})
        lvl = min(tier_of.get(rel, start_tier), max(0, len(steps) - 1))
        rec = _wu(rel)
        used_total = int(rec.get("total") or 0)
        used_tier = int((rec.get("tiers") or {}).get(lvl) or 0)
        # **单模块预算**：一个模块最多吃这么多钱，超了就停手、把额度留给别的模块。
        # 数值优先用校准值（按模块花费 p75 × 1.5 算出来的），再受"剩余预算 ÷ 模块数"约束。
        # 实测（2026-11-05 round-01a 第 3 次）：一次 round 只有 ¥0.6，而 rounding.py 一个模块
        # 连打 5 次、单次思考 12 096 token（¥0.0637），把预算吃穿 → 后面的 interval 还没轮到
        # 就被"花费到上限"收工，最终 10/20（比不设上限时的 15/20 更差）。
        _cal_mod = 0.15
        _mod_funcs = 0
        try:
            _cal_b = __import__("core.calibration", fromlist=["x"])
            _cal_mod = float(_cal_b.current().get("module_budget_cny") or 0.15)
        except Exception:                    # noqa: BLE001
            _cal_mod = 0.15
        try:
            _c_now = contracts.get(rel)
            _mod_funcs = len(list(getattr(_c_now, "funcs", None) or []))
        except Exception:                    # noqa: BLE001
            _mod_funcs = 0
        # 预算按**模块大小**给：1 个函数和 8 个函数不该用同一个数
        # （实测：csvtool/cli.py 4 个函数 103 行，¥0.15 根本不够，只过 1/4）
        try:
            from . import calibration as _cal_c
            _mod_budget_raw = _cal_c.module_budget_for(_mod_funcs)
        except Exception:                    # noqa: BLE001
            _mod_budget_raw = _cal_mod
        _mod_budget = float((rec.get("budget_cny") or 0) or
                            max(0.05, min(_mod_budget_raw,
                                          float(budget_left) / max(1.0, float(max_modules)))))
        _spent_mod = _module_spend(ws, rel, _round_since)
        if _spent_mod >= _mod_budget:
            stats["modules"].append({"path": rel, "ok": False, "tier": lvl + 1, "by": "-",
                                     "reason": f"单模块预算用完（已花 ¥{_spent_mod:.3f} ≥ "
                                               f"¥{_mod_budget:.3f}），把额度留给别的模块",
                                     "applied": []})
            if emit:
                emit("module_budget_stop", {"path": rel, "spent_cny": round(_spent_mod, 4),
                                            "budget_cny": round(_mod_budget, 4),
                                            "remaining": round(_mod_budget - _spent_mod, 4)})
            continue
        if used_total >= cap_total or used_tier >= cap_per_tier:
            # 同一档已经写得够多 / 整夜额度用尽 → 不再重写；能升档就升档（升档不占本档额度）
            can_step = used_tier >= cap_per_tier and lvl + 1 < len(steps)
            if can_step:
                tier_of[rel] = lvl + 1
                tier_fail[rel] = 0
                if emit:
                    emit("write_cap", {"path": rel, "tier": lvl + 1, "used_in_tier": used_tier,
                                       "write_per_tier": cap_per_tier, "action": "换档重试",
                                       "message": f"这一档已写 {used_tier} 次，升到下一档再试"})
                continue
            stats["modules"].append({"path": rel, "ok": False, "tier": lvl + 1, "by": "-",
                                     "reason": f"写次数到顶（本档 {used_tier}/{cap_per_tier}，"
                                               f"整夜 {used_total}/{cap_total}），不再重写",
                                     "applied": []})
            stats.setdefault("capped", []).append(rel)
            if emit:
                emit("write_cap", {"path": rel, "used_in_tier": used_tier,
                                   "used_total": used_total, "cap_total": cap_total,
                                   "action": "停止重写",
                                   "message": "同一模块同一档写了太多次，停下来交给报告"})
            continue
        step = steps[lvl] if steps else {}
        tag = f"{step.get('profile_id')}:{step.get('effort')}" if step else "?"
        oid = workorder.add(task_id, 0, "implement_module", rel, "", f"整文件通过全部判据（{rel}）",
                            f"按契约写完整文件；当前档 {tag}")
        fp = ws / rel
        try:
            before = fp.read_bytes() if fp.exists() else None
        except OSError:
            before = None
        prev_verified = int((state.get(rel) or {}).get("verified") or 0)
        if emit:
            emit("author_start", {"path": rel, "tier": lvl + 1, "of": len(steps), "by": tag})
        # 微分支隔离：这次尝试在隔离层里做。成功才并回，失败整层丢弃 ——
        # 以前只回滚"本轮要改的那个文件"，顺带改到的其它文件会残留到下一轮。
        sand = None
        try:
            from . import isolate as _iso
            sand = _iso.open_attempt(ws, rel, int(_osr.environ.get("SIXCODER_ROUND", "0") or 0))
            if emit:
                emit("isolate_begin", sand.to_dict())
        except Exception as exc:                     # noqa: BLE001
            sand = None
            # 降级**必须留痕**：既要发事件，也要进交付报告（不许静默降级）
            stats.setdefault("isolate_degraded", []).append(
                {"path": rel, "reason": f"{type(exc).__name__}: {exc}"[:120]})
            if emit:
                emit("notice", {"message": f"隔离层开启失败（继续但用旧回滚）：{type(exc).__name__}"})
        # 工作集声明（M 级起）：写之前先说清"改哪个文件、依赖谁的接口、相关测试、非目标"。
        # 反向依赖由 ast 算（谁 import 了我），一起给模型 —— 事故里"顺手改坏"多半是不知道这事。
        ws_text = ""
        # 第3层：项目地图 + 第2层：稳定的函数清单（不许改）
        try:
            from . import project as _proj3
            ws_text = _proj3.prompt_block(ws, contracts, rel)
            _fz = _proj3.load_frozen(ws).get(rel) or set()
            if _fz:
                ws_text += ("\n【已通过验收、不许改动或删除的函数】\n"
                            + "、".join(sorted(_fz))
                            + "\n（要改先说明为什么它的判据变红了）")
        except Exception:                                # noqa: BLE001
            pass
        if _scale_mod.gate_mode("workingset", _lv) != "skip":
            try:
                from . import governance as _gov2
                decl = _gov2.workingset([rel], contracts, requirement,
                                        non_goals=["不改其它模块的对外签名", "不新增第三方依赖"])
                rev = _gov2.reverse_deps(ws, [rel])
                ws_text = _gov2.render_workingset(decl)
                if rev:
                    ws_text += ("\n【反向依赖：这些文件 import 了本模块，改坏了会波及它们】\n"
                                + "\n".join(f"- {a} → {b}" for a, b in rev[:10]))
                if emit:
                    emit("workingset", {"path": rel, "changed": decl["changed"],
                                        "tests": decl["tests"], "reverse_deps": len(rev)})
            except Exception as exc:                     # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"工作集声明跳过：{type(exc).__name__}"})
        # 粒度**自动选**，不再靠一个要用户记住的隐藏开关：
        #   · 小文件（< BIG_FILE_LINES 行）→ 整文件：一次生成保证内部一致，实测最稳；
        #   · 大文件 → 骨架 + 只回函数体：整份重写的输出量是"要改的那几个函数"的几十倍，
        #     而且大文件整份重写最容易顺手改坏已验证的部分。
        # 环境变量 SIXCODER_GRANULAR 只作为**覆盖**（排查/对照用），不是正常使用路径。
        import os as _os
        try:
            cur_lines = len((ws / rel).read_text(encoding="utf-8", errors="replace").splitlines()) \
                if (ws / rel).exists() else 0
        except OSError:
            cur_lines = 0
        auto = "bodies" if cur_lines >= BIG_FILE_LINES else "whole_file"
        # **只要这个模块已经有函数通过验收，就必须走"只回函数体"**。
        # 实测（exp1d）：logparse.py 6 个函数里 5 个写对、1 个写错 → 整文件重写把 5 个正确的
        # 一起扔掉 → 下一轮从头再写一遍 → 同一模块连输三次，整片零收益（无效调用）。
        # 粒度必须跟着"已通过多少"走，而不是只跟着文件大小走。
        try:
            _prev_state = state.get(rel) or {}
            if int(_prev_state.get("verified") or 0) > 0:
                auto = "bodies"
        except Exception:                                # noqa: BLE001
            pass
        granular = (_os.environ.get("SIXCODER_GRANULAR") or "").strip().lower() or auto
        if emit and granular != auto:
            emit("notice", {"message": f"粒度被环境变量覆盖：{granular}（自动判断是 {auto}）"})
        # 输出配额记忆：这个模块被截断过一次，下一轮直接从放大后的值开始，
        # 不再"每轮从 3000 重算、再被截断一次"（实测 search-index 因此连输三次）。
        # 自愈轮数由 policy 决定（甲方模式 4 轮：AI 内部改到判据全绿，人不在中间）；
        # stop_on_repeat 保证"反复刺激"只在有新证据时才发生，不白烧调用。
        try:
            from . import policy as _pol_h
            _heal = max(1, int(_pol_h.current().heal_attempts))
            _stop_rep = bool(_pol_h.current().heal_stop_on_repeat)
        except Exception:                                # noqa: BLE001
            _heal, _stop_rep = 2, True
        _mo_hint = int((_wu(rel).get("max_out") or 0))
        if granular in ("bodies", "line", "patch"):
            r = _author.author_bodies(ws, rel, contracts.get(rel), contracts, model_plan=plan,
                                      step_index=lvl, task_id=task_id, emit=emit,
                                      local_model=local_model, workingset=ws_text,
                                      max_out=_mo_hint, attempts=_heal)
            if not r.get("ok") and not (ws / rel).exists():
                r = _author.author_module(ws, rel, contracts.get(rel), contracts,
                                          model_plan=plan, step_index=lvl, task_id=task_id,
                                          emit=emit, local_model=local_model,
                                          workingset=ws_text, max_out=_mo_hint,
                                          attempts=_heal, stop_on_repeat=_stop_rep)
        else:
            r = _author.author_module(ws, rel, contracts.get(rel), contracts, model_plan=plan,
                                      step_index=lvl, task_id=task_id, emit=emit,
                                      local_model=local_model, workingset=ws_text,
                                      max_out=_mo_hint, attempts=_heal,
                                      stop_on_repeat=_stop_rep)
        _mo_used = int(r.get("max_out") or 0)
        if _mo_used > _mo_hint:
            _wu(rel)["max_out"] = _mo_used
        stats["system_issues"] += (r.get("system_issues") or [])
        rec = _wu(rel)
        rec["total"] = int(rec.get("total") or 0) + 1
        try:
            from . import project as _proj_p
            _st_p = iterate.inspect(ws, {rel: contracts[rel]}).get(rel) or {}
            _proj_p.save_progress(ws, rel, {
                "verified": list(_st_p.get("verified_names") or []),
                "stubs": sorted(_st_p.get("stubs") or []),
                "attempts": int(rec["total"]),
                "ok": bool(r.get("ok"))})
        except Exception:                                # noqa: BLE001
            pass
        tiers_used = rec.setdefault("tiers", {})
        tiers_used[lvl] = int(tiers_used.get(lvl) or 0) + 1
        # 结构体检：语法/重名定义/契约函数还是桩/引用了没定义的名字。
        # 实测缺口：整文件模式 8/8「已验收」而交付的 store.py 少了一整列；
        # bodies 模式交付过无 import 的模块和"两代常量名并存"的模块 —— 都是这一关能挡的。
        # 只在**作者自称成功**时才体检：作者失败时盘上还是旧骨架，
        # 拿骨架去体检会把真实原因（例如输出被配额截断）替换成"还是桩"，诊断信号就没了。
        sg = None
        if r.get("ok"):
            try:
                from . import gates as _gates
                want = [f.name for f in (getattr(contracts.get(rel), "funcs", None) or [])]
                sg = _gates.gate_structure(ws, [rel], contract_funcs=want)
            except Exception as exc:                     # noqa: BLE001
                sg = None
                if emit:
                    emit("notice", {"message": f"结构体检跳过：{type(exc).__name__}"})
        if sg is not None and not sg.ok:
            if before is not None:
                try:
                    fp.write_bytes(before)
                except OSError:
                    pass
            r = {"ok": False,
                 "reason": f"结构体检不过（已回滚）：{sg.detail}"[:300],
                 "feedback": f"结构体检：{sg.detail}"[:600]}
            if emit:
                emit("structure_gate", {"path": rel, "ok": False, "detail": sg.detail[:300]})
        elif sg is not None and emit:
            emit("structure_gate", {"path": rel, "ok": True, "detail": sg.detail[:120]})
        # 回归保护：这次写让它已验收数掉了 → 回滚
        after = iterate.inspect(ws, contracts)
        now_verified = int((after.get(rel) or {}).get("verified") or 0)
        if now_verified < prev_verified and before is not None:
            try:
                fp.write_bytes(before)
            except OSError:
                pass
            r = {"ok": False, "reason": f"回归：{rel} 已验收数 {prev_verified}→{now_verified}，已回滚"}
            after = iterate.inspect(ws, contracts)
        ok = bool(r.get("ok"))
        workorder.close(oid, ok, str(r.get("reason") or "")[:400])
        stats["modules"].append({"path": rel, "ok": ok, "tier": lvl + 1, "by": tag,
                                 "reason": str(r.get("reason") or "")[:200],
                                 "attempt": int(r.get("attempt") or 0),
                                 "applied": r.get("applied") or []})
        # 记一条写作尝试：给"按各档成功率校准封顶"和"按模块大小校准预算"提供证据
        try:
            from . import calibration as _cal_rec
            _cal_rec.record_attempt(ws, path=rel, effort=str(step.get("effort") or "none"),
                                   ok=bool(ok), funcs=_mod_funcs,
                                   cost_cny=_module_spend(ws, rel, _round_since))
        except Exception:                    # noqa: BLE001
            pass
        # 「一次通过率」是甲方模式唯一的自检指标：**没有重试就全绿**的模块占比。
        # 它比"最终通过率"严格得多——最终通过率可以靠反复刺激刷出来，一次通过率不能。
        _tot = int(stats.get("mod_total") or 0) + 1
        _first = int(stats.get("mod_first_pass") or 0) + (
            1 if (ok and int(r.get("attempt") or 1) <= 1) else 0)
        stats["mod_total"] = _tot
        stats["mod_first_pass"] = _first
        stats["first_pass_rate"] = round(_first / max(1, _tot), 3)
        if emit:
            emit("order_done" if ok else "order_failed",
                 {"round": 0, "order": f"整文件 {rel}",
                  "applied": r.get("applied"), "reason": str(r.get("reason") or "")[:160]})
        # 失败时按**决策表**决定下一步（定位到位就做最小改动）：
        #   行级 → 函数级 → 整文件 → 升档 → 放弃，全部记进事件流，便于事后追。
        if not ok:
            try:
                from . import decision as _decision
                # 喂**判据原文**（feedback），不是包装摘要：包装语里只有文件名，
                # 会让定位退化成"模块级"，于是每次都去整文件重写。
                raw_evidence = str(r.get("feedback") or r.get("reason") or "")
                findings = _decision.localize(ws, raw_evidence, rel=rel)
                d = _decision.decide(findings, rel=rel,
                                    attempts_done=dec_used.setdefault(rel, {}))
                if emit:
                    emit("decision", {"path": rel, "action": d.action,
                                      "targets": d.targets[:5],
                                      "precision": _decision.precision_of(findings),
                                      "findings": [f.as_row() for f in findings[:3]],
                                      "reason": d.reason})
                # 腐烂分覆盖：模块已经烂到阈值，就别再打补丁了（打补丁只会让临时方案叠加）
                try:
                    from . import rot as _rot
                    rebuild, rinfo = _rot.should_rebuild(ws, rel, _lv)
                    # 腐烂分覆盖：行级补丁 **和** 函数级重写都算"打补丁"
                    if rebuild and d.action in (_decision.ACT_PATCH_LINE,
                                                _decision.ACT_REWRITE_FUNC):
                        if emit:
                            emit("rot_rebuild", {"path": rel, "score": rinfo["score"],
                                                 "from_action": d.action,
                                                 "reasons": rinfo["reasons"][:3],
                                                 "note": "腐烂分达阈值：改成整模块按契约重刷"})
                        d = _decision.Decision(action=_decision.ACT_REWRITE_MODULE,
                                                targets=[], file=rel, findings=findings,
                                                reason=(f"腐烂分 {rinfo['score']} ≥ 阈值，"
                                                        f"改整模块重刷而不是打补丁"))
                except Exception:                    # noqa: BLE001
                    pass
                # 行级补丁：只改那几行（失败不影响后续降级）。
                # 行级档真的能修好一类东西（实测回放：缺 import / 缺常量定义，
                # 两行插入 = 66 token；同一件事重写整文件要 ~1,500 token，23 倍）。
                if d.action == _decision.ACT_PATCH_LINE:
                    from . import patch as _patch
                    fix = _patch.request_line_fix(
                        ws, rel, str(r.get("reason") or ""), step_index=lvl, task_id=task_id,
                        emit=emit,
                        verify=lambda: _verify_module(rel))
                    _decision.record(dec_used.setdefault(rel, {}), _decision.ACT_PATCH_LINE)
                    if fix.get("ok"):
                        r = {"ok": True, "applied": [rel], "line_patch": fix.get("applied")}
                        ok = True
                        stats["modules"][-1]["line_patch"] = fix.get("applied")
                elif d.action == _decision.ACT_REWRITE_FUNC:
                    # 下一轮只重写这些函数体（bodies 路径会读 tier_of/tier_fail，不额外传）
                    stats.setdefault("next_targets", {})[rel] = d.targets
                    _decision.record(dec_used.setdefault(rel, {}), _decision.ACT_REWRITE_FUNC)
                else:
                    _decision.record(dec_used.setdefault(rel, {}), d.action)
            except Exception as exc:                     # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"决策表跳过：{type(exc).__name__}"})
        # 隔离层做决定：成功并回、失败整层丢弃（放在所有判定之后）
        if sand is not None:
            try:
                _partial = False
                _partial_why = ""
                if not ok:
                    # 第1层：部分成功也要落盘。判据给出"这个模块有几个函数过了"，
                    # 只要文件能解析且过了 ≥1 个函数，就保留它（部分 > 桩），
                    # 并把过了的函数写进稳定基线（第2层冻结）。
                    try:
                        _st1 = iterate.inspect(ws, {rel: contracts[rel]}).get(rel) or {}
                        _ok_n = int(_st1.get("verified") or 0)
                        _src_ok = True
                        try:
                            import ast as _ast1
                            _ast1.parse((ws / rel).read_text(encoding="utf-8",
                                                             errors="replace"))
                        except Exception:                # noqa: BLE001
                            _src_ok = False
                        if _ok_n > 0 and _src_ok:
                            _partial = True
                            _partial_why = f"保留部分成果：{_ok_n} 个函数已过判据"
                            try:
                                from . import project as _proj4
                                _proj4.freeze(ws, rel,
                                              list(_st1.get("verified_names") or []),
                                              reason=_partial_why)
                            except Exception:                # noqa: BLE001
                                pass
                    except Exception:                    # noqa: BLE001
                        _partial = False
                if ok or _partial:
                    sand.commit()
                    if emit:
                        emit("isolate_commit", {**sand.to_dict(),
                                                "partial": _partial,
                                                "why": _partial_why})
                else:
                    sand.rollback()
                    if emit:
                        emit("isolate_rollback", sand.to_dict())
            except Exception as exc:                 # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"隔离层收尾失败：{type(exc).__name__}"})
        if ok:
            # #14 XL 级即时反馈：每次改完立刻算健康度/分层，不等收工
            if _lv16 in ("XL",) or _scale_mod.profile(_lv16).flags.get("health_metrics"):
                try:
                    from . import arch as _arch_i
                    hm = _arch_i.health_metrics(ws)
                    lay = _arch_i.gate_layers(ws, _lv16)
                    if emit:
                        emit("instant_health", {"path": rel, "modules": hm["modules"],
                                                "edges": hm["edges"],
                                                "max_depth": hm["max_depth"],
                                                "acyclic": hm["acyclic"],
                                                "layers_ok": lay.ok,
                                                "note": "改完即测（XL 级要求即时反馈）"})
                except Exception as exc:             # noqa: BLE001
                    if emit:
                        emit("notice", {"message": f"即时健康度跳过：{type(exc).__name__}"})
            tier_of[rel] = 0                     # 解决即回最低档
            tier_fail[rel] = 0
            dec_used.pop(rel, None)
            write_used.pop(rel, None)            # 解决了就清额度（下次是"新问题"，不该继承旧账）
            # 测试文件由引擎渲染（本地，不花 token），顺手解决"目标文件不存在"
            try:
                _tr = iterate.add_engine_tests(ws, contracts, rel)
                if not (_tr or {}).get("ok"):
                    # **不许静默**：实测（实验台 search-index）index.py 明明验收通过，
                    # 却一个测试文件都没生成，报告里只写"现有测试文件：test_text.py"，
                    # 没人知道 index.py 没测试。生成失败要记账、要进交付报告。
                    _why = str((_tr or {}).get("reason") or "未知")[:200]
                    iterate.log_test_gap(ws, rel, _why)
                    if emit:
                        emit("notice", {"message": f"{rel} 没生成测试：{_why}"})
            except Exception as exc:                 # noqa: BLE001
                iterate.log_test_gap(ws, rel, f"{type(exc).__name__}: {exc}"[:200])
                if emit:
                    emit("notice", {"message": f"{rel} 生成测试时出错：{type(exc).__name__}"})
        else:
            stats["ok"] = False
            n = tier_fail.get(rel, 0) + 1
            tier_fail[rel] = n
            # 思考强度上限：写代码不往最深档升（深档留给规格/诊断）。
            # 实测：rounding.py 升到 max 后单次思考 22 452 token、一次 ¥0.108，5 个函数照样全红 ——
            # 深思考没换来正确率，只换来账单；同一轮 high 档写的三个模块全绿。
            _cap_lvl = lvl
            try:
                from . import calibration as _cal, effort as _eff_cap
                # 封顶优先用**校准值**（按各档成功率算出来的），policy 字段只是兜底默认
                _ceil_eff = str(_cal.current().get("code_effort_ceiling")
                                or getattr(policy.current(), "code_effort_ceiling", "high"))
                _cap_lvl = _eff_cap.pick(steps, _ceil_eff)
            except Exception:                # noqa: BLE001
                _cap_lvl = len(steps) - 1
            if n >= escalate_after and lvl + 1 < len(steps):
                if lvl + 1 > _cap_lvl:
                    if emit:
                        emit("escalate_capped", {"order": f"整文件 {rel}", "from": tag,
                                                 "cap": f"{steps[_cap_lvl].get('profile_id')}:"
                                                        f"{steps[_cap_lvl].get('effort')}",
                                                 "why": "写代码的思考强度封顶（深档留给规格与诊断）"})
                else:
                    tier_of[rel] = lvl + 1
                    tier_fail[rel] = 0
                    if emit:
                        nxt = steps[lvl + 1]
                        emit("escalate", {"order": f"整文件 {rel}", "from": tag,
                                          "to": f"{nxt.get('profile_id')}:{nxt.get('effort')}",
                                          "fails": n})
    return stats


def _log_contract_fix(ws: Path, items: list[str]) -> Path:
    """记下"系统自动改过你的契约"这件事（改了哪几条）。不许悄悄改甲方需求。"""
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "contract_fixed.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"at": time.strftime("%Y-%m-%d %H:%M:%S"),
                             "fixed_items": [str(x)[:200] for x in items]},
                            ensure_ascii=False) + "\n")
    return p


def _load_contract_fixes(ws: Path) -> list[str]:
    p = Path(ws) / ".sixcoder" / "contract_fixed.jsonl"
    if not p.exists():
        return []
    out: list[str] = []
    try:
        for ln in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if ln.strip():
                out.extend(json.loads(ln).get("fixed_items") or [])
    except Exception:                                    # noqa: BLE001
        return []
    return out


def write_delivery(ws: Path, requirement: str, state: dict, hist: list[dict],
                   reason: str, spent: float, abandoned: dict | None = None,
                   remote_used: int = 0, plan_used: bool = False,
                   plan_by: str = "", scope: dict | None = None) -> Path:
    """早上交付物：一份能直接看的报告（完成度只写有硬证据的部分）。"""
    if abandoned is None:
        abandoned = _load_abandoned(ws)
    verified = sum(s["verified"] for s in state.values())
    total = sum(s["funcs"] for s in state.values())
    lines = ["# 交付报告", "",
             f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"- 用时：{spent/60:.1f} 分钟｜收工原因：{reason}",
             f"- **验收通过函数：{verified}/{total}**（只统计机械判据通过者）"]
    if (scope or {}).get("oscillation"):
        o = scope["oscillation"]
        if o.get("oscillations"):
            lines.append(f"- **振荡 {o['oscillations']} 次**（回到过 {o['distinct_states']} "
                         f"个不同状态中的旧状态；已按打法切换/收工）")
        else:
            lines.append(f"- 未检测到振荡（{o['distinct_states']} 个不同失败状态，无重复）")
    try:
        from . import budget_cap as _cap2
        lines.append(_cap2.report_line(str((scope or {}).get("task_id") or ""),
                                       _cap2.resolve_cap((scope or {}).get("max_cny"))))
    except Exception:                                    # noqa: BLE001
        pass
    if (scope or {}).get("remote_profile"):
        lines.append(f"- 远端模型：只走 `{scope['remote_profile']}` 档案（右上角「远端模型」指定）")
    _fixes = _load_contract_fixes(ws)
    if _fixes:
        lines.append(f"- **系统自动改过你的契约 {len(_fixes)} 处**（原文里有矛盾/写不出判据，"
                     f"已让远端重写）：")
        for _f in _fixes[:4]:
            lines.append(f"  - {_f}")
        lines.append("  - 如果这些不是你的本意，请改契约重跑；这类改动会改变交付内容。")
    # 抬头必须自洽：分级门禁没过就不能只写"验收全通过"（实验台 large-l 亲眼见到的误导）
    try:
        _lv0 = str((scope or {}).get("level") or "S")
        _bl = [g for g in ((scope or {}).get("level_gates") or [])
               if (not g.ok) and getattr(g, "evidence_ref", "") not in
               ("skipped", "unavailable", "no-layers", "no-mutants", "error")]
        if _bl:
            lines.append(f"- **{_lv0} 级门禁 {len(_bl)} 项未通过**："
                         + "、".join(g.gate for g in _bl[:4])
                         + " —— 按本级标准这次交付**不算合格**（明细见下方门禁表）")
    except Exception:                                    # noqa: BLE001
        pass
    lines.append("")
    # 规模与约束等级：不同代码规模该有的约束不一样，必须写清"按哪级跑的、依据是什么"
    if scope:
        try:
            from . import scale as _scale
            lines += _scale.report_lines(scope.get("level") or "S", scope)
        except Exception as exc:                         # noqa: BLE001
            lines += ["", f"## 规模与约束等级", "", f"- 无法生成（{type(exc).__name__}）"]
    lines += ["## 模块状态", "",
              "| 模块 | 函数 | 已验收 | 无机械判据 | 仍是桩 |", "|---|---|---|---|---|"]
    for path, s in state.items():
        lines.append(f"| {path} | {s['funcs']} | {s['verified']} | "
                     f"{', '.join(s.get('untested') or []) or '—'} | "
                     f"{', '.join(sorted(s['stubs'])) or '—'} |")
    if hist:
        lines += ["", "## 进度曲线", "", "| 轮 | 已验收 | 工作单 | 完成 |", "|---|---|---|---|"]
        for h in hist:
            lines.append(f"| {h['round']} | {h['verified']} | {h['orders']} | {h.get('done', 0)} |")
    todo = [f"- `{p}`::{f}" for p, s in state.items() for f in sorted(s["stubs"])]
    todo += [f"- `{p}`::{f} 没有机械判据（引擎判不了，不算交付）"
             for p, s in state.items() for f in (s.get("untested") or [])]
    # 需要人看、但引擎不该替甲方拍板的事（判断不了/判据够不着）→ 写进交付物，
    # 不能只留在内部状态里。实测：pow 的"负数底的整数次幂算不算支持"就够不着判据，
    # 交付物里只字不提，等于把语义漂移藏起来了。
    todo += [f"- `{p}`：{n}" for p, s in state.items() for n in (s.get("notes") or [])]
    # e2e 红 = 整体没达标：单函数判据只覆盖示例与边界，看不见"少一列/字段名不对"
    try:
        from . import e2e as _e2e0
        ev0 = iterate.last_e2e(ws) or _e2e0.write(ws, contract_mod.parse_requirement(requirement),
                                                  requirement)
        if ev0 and not ev0.get("ok") and not ev0.get("skipped"):
            todo.insert(0, "- **跨模块业务流（e2e）没跑通 → 单函数达标 ≠ 可用**："
                           + str(_e2e0.as_feedback(ev0))[:200])
    except Exception:                                    # noqa: BLE001
        pass
    lines += ["", "## 未完成（下一夜建议从这里开始）", ""]
    lines += todo or ["- 无（契约内函数均已通过机械验收，且 e2e 已跑通）"]

    # 被放弃/隔离的工作单：以前只写一句"其余已隔离"，甲方看不出被放弃了什么
    if abandoned:
        lines += ["", "## 被放弃的工作单（需要你决定怎么处理）", "",
                  "| 工作单 | 连续失败 | 远端试过 | 最近失败原因 |", "|---|---|---|---|"]
        for key, rec in sorted(abandoned.items(), key=lambda kv: -kv[1].get("fails", 0)):
            lines.append(f"| {rec.get('order', key)[:70]} | {rec.get('fails')} | "
                         f"{'是' if rec.get('remote_tried') else '否'} | "
                         f"{str(rec.get('reason') or '')[:140]} |")
        lines += ["", "> 这些单在夜班里连续失败到被隔离、不再派。常见原因是本地小模型做不出来；"
                      "可以人工补、或换更强模型/直接让我改。"]

    # 协作方式：这一夜是谁在拍板、远端写了几张单 —— 让甲方看懂"这活是谁干的"
    try:
        from . import policy as _policy
        pol = _policy.current()
        lines += ["", "## 协作方式（这一夜是怎么跑完的）", "",
                  f"- 模式：{pol.name}",
                  f"- 一句话：{_policy.describe(pol)}"]
        if pol.remote_code_budget:
            lines.append(f"- 远端写代码：{remote_used}/{pol.remote_code_budget} 次")
        else:
            lines.append(f"- 远端写代码：{remote_used} 次（不设次数上限，由时间预算兜）")
        if plan_used:
            who = "人给的" if plan_by == "human" else "远端自己定的"
            lines.append(f"- 实现方案：{who}，已作为权威注入执行者与架构师提示词")
        for n in pol.notes:
            lines.append(f"- {n}")
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 协作方式：跳过（{type(exc).__name__}）")

    # 工程环节自查：集成/安全/依赖/歧义 —— 失败也照实写，不许粉饰
    lines += ["", "## 工程环节自查", ""]
    rels = sorted((state or {}).keys())
    try:
        from . import e2e as _e2e
        contracts = contract_mod.parse_requirement(requirement)
        ev = _e2e.write(ws, contracts, requirement)
        lines.append(f"- 集成（跨模块业务流）：{_e2e.as_feedback(ev)}")
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 集成（跨模块业务流）：无法生成（{type(exc).__name__}）")
    try:
        from . import gates as _gates
        se = _gates.gate_security(ws, rels)
        lines.append(f"- 安全：{'通过' if se.ok else '**未通过**'} — {se.detail[:160]}")
        dp = _gates.gate_deps(ws)
        lines.append(f"- 依赖：{'通过' if dp.ok else '**需处理**'} — {dp.detail[:160]}")
        try:
            from . import deps_audit as _da2
            _ares = _da2.audit(ws, use_network=False)     # 交付时不再打网络，用缓存
            lines.append(f"- 依赖审计（幻觉包/已知漏洞）：{_da2.summarize(_ares)[:220]}")
        except Exception as exc:                     # noqa: BLE001
            lines.append(f"- 依赖审计：跳过（{type(exc).__name__}）")
        fm = _gates.gate_field_map_result(ws, rels, requirement)
        if "歧义" in (fm.evidence_ref or ""):
            lines.append(f"- **待甲方确认的歧义**：{fm.detail[:220]}")
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 安全/依赖自查跳过：{type(exc).__name__}")

    # 测试缺口：有些模块验收通过却没生成测试文件（模型/门禁的原因），必须让甲方看见
    try:
        _gaps = iterate.load_test_gaps(ws)
        if _gaps:
            lines += ["", "## 没有测试文件的模块（验收通过 ≠ 有测试）", ""]
            for _g in _gaps[-6:]:
                lines.append(f"- `{_g.get('path')}`：{str(_g.get('reason'))[:160]}")
            lines.append("- 这些模块只有判据跑过，仓库里没有对应测试；建议下一夜补上。")
    except Exception:                                    # noqa: BLE001
        pass

    # 按规模档位要求的额外门禁（M+：架构方向/覆盖率；L+：变更行变异/依赖白名单；XL：健康度）
    # 每条都写实测结果；**测不了的如实写"测不了"，不给通过结论**（这是本项目的纪律）。
    try:
        from . import arch as _arch
        lv = (scope or {}).get("level") or "S"
        lines += ["", f"### 按 {lv} 级要求的门禁（实测）", ""]
        changed = {p: ("", (Path(ws) / p).read_text(encoding="utf-8", errors="replace"))
                   for p in rels if (Path(ws) / p).exists()}
        # 复用收工前算好的那一份（变异测试贵，不能算两遍）
        _rows = (scope or {}).get("level_gates")
        for g in (_rows if _rows is not None else _arch.level_gates(ws, lv, changed)):
            mark = "通过" if g.ok else "**未通过**"
            if g.evidence_ref in ("skipped", "unavailable", "no-layers", "no-mutants", "error"):
                mark = "未判定"
            # "只报告：xxx"说明**确实查出了问题**，只是本级不阻断。
            # 以前仍写"通过 — 只报告：unused_import×1"，读起来像没有问题。
            elif g.ok and str(g.detail or "").startswith("只报告"):
                mark = "通过（本级只报告，但发现问题）"
            lines.append(f"- {g.gate}：{mark} — {g.detail[:200]}")
        hm = _arch.health_metrics(ws)
        lines.append(f"- 架构健康度（XL 级的持续指标）：{hm['modules']} 个模块、{hm['edges']} 条依赖、"
                     f"最大深度 {hm['max_depth']}、无环={hm['acyclic']}")
        dr = _arch.health_drift(ws)
        if dr.get("has_baseline"):
            lines.append(f"- 漂移（与上次对比）：{'**在退化** — ' if dr.get('regressed') else ''}"
                         f"{dr.get('note')}（模块 {dr['delta']['modules']:+d}、"
                         f"依赖边 {dr['delta']['edges']:+d}、深度 {dr['delta']['max_depth']:+d}）")
        else:
            lines.append(f"- 漂移：{dr.get('note')}")
        try:
            from . import governance as _gov3
            rev = _gov3.reverse_deps(ws, rels)
            if rev:
                lines.append("- 反向依赖（改这些文件会波及）："
                             + "、".join(f"`{a}`" for a, _ in rev[:8]))
        except Exception:                                # noqa: BLE001
            pass
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 规模门禁自查跳过：{type(exc).__name__}")

    # 缺依赖：只报告、不安装（甲方明确要求：pip 不放开）
    try:
        miss = _missing_deps(ws)
        if miss:
            (Path(ws) / "requirements.missing.txt").write_text(
                "# 需要你手动安装（引擎不会执行 pip install）\n"
                + "\n".join(miss) + "\n", encoding="utf-8")
            lines += ["", "## 需要你安装的依赖（引擎不执行安装）", "",
                      "工作区里用到了这些模块，本机装不上：", ""]
            lines += [f"- `{m}` → 建议：`pip install {m}`（请自行锁定版本后写进 "
                      f"requirements.txt）" for m in miss]
            lines += ["", "对应清单：`requirements.missing.txt`（引擎只写清单，不运行 pip）"]
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 缺依赖检查跳过：{type(exc).__name__}")

    lines += _quality_section(ws, rels, state, level=(scope or {}).get("level") or "S")
    _deg = (scope or {}).get("isolate_degraded") or []
    if _deg:
        lines += ["", "## 运行期降级（不许静默，逐条列在这里）", ""]
        for d in _deg[:10]:
            lines.append(f"- `{d.get('path')}`：隔离层没能开启（{d.get('reason')}）"
                         f" → 这一轮用旧的单文件回滚，**不是**整层隔离")
        lines.append("")
        lines.append("> 影响：失败时只能还原那一个模块，顺带改到的其它文件可能残留。"
                     "L/XL 级要求整层隔离，出现降级应当人工看一次。")
    try:
        from . import doctor as _doctor2
        dres = (scope or {}).get("doctor") or _doctor2.run(ws, (scope or {}).get("level") or "M")
        lines += _doctor2.report_lines(dres)
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 定期巡检", "", f"- 跳过（{type(exc).__name__}）"]
    try:
        from . import rot as _rot2
        rmap = (scope or {}).get("rot") or _rot2.scores(ws, rels, (scope or {}).get("level") or "S")
        lines += _rot2.report_lines(rmap)
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 模块腐烂分", "", f"- 跳过（{type(exc).__name__}）"]
    try:
        from . import debt as _debt
        lv = (scope or {}).get("level") or "S"
        _debt.sync(ws, lv, int((scope or {}).get("rounds") or 0))
        lines += _debt.report_lines(ws, lv, int((scope or {}).get("rounds") or 0))
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 临时方案台账", "", f"- 跳过（{type(exc).__name__}）"]
    # 单函数判据看不见的两层：模块之间接得上 / 业务流程是对的
    try:
        from . import flow as _flow3
        lines += ["", "### 跨模块接线（集成冒烟）", ""]
        _fs = (scope or {}).get("flow_smoke")
        lines += (_flow3.report_lines(_fs) if isinstance(_fs, dict)
                  else ["- 本轮没有计算集成冒烟（不是通过，也不是失败）"])
        lines += ["", "### 业务流 e2e（业务不变量）", ""]
        _bf = (scope or {}).get("business_flow")
        lines += (_flow3.business_report_lines(_bf) if isinstance(_bf, dict)
                  else ["- 本轮没有计算业务流（不是通过，也不是失败）"])
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 跨模块接线 / 业务流", "", f"- 跳过（{type(exc).__name__}）"]
    # 能力上限台账：8 格的当前值（探针现算，测不了就写测不了）
    try:
        from . import ceiling as _ceil2
        lines += _ceil2.report_lines((scope or {}).get("ceiling") or {})
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 能力上限台账", "", f"- 跳过（{type(exc).__name__}）"]

    lines += ["", "## 如何验证我交付的东西", "",
              "```powershell", "cd prototype",
              "python cli.py run --repo <本目录> --requirement-file <需求文件> --model qwen2.5-coder:7b",
              "# 独立验收（与引擎不共享代码，自己连库、自己断言）",
              f"python tools\\hq_verify.py {ws}",
              "# 追溯矩阵：需求条目 ↔ 判据 ↔ 证据",
              f"python tools\\trace.py {Path(ws).name}",
              "```", "", "## 需求原文", "", "```", (requirement or "")[:1500], "```", ""]
    p = Path(ws) / "DELIVERY.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p

def _module_spend(ws: Path, rel: str, since: str = "") -> float:
    """这个模块**本轮**已经花掉的远端钱（¥）。

    按远端账本的 node_id 精确匹配 `author:<rel>`，并且**只统计 since 之后的调用**。
    为什么必须带 since：第一版写成"最近 12 小时"，于是把历史几轮的账也算进来了 ——
    实测（2026-11-05 round-01a 第 4 次）rounding.py 历史已花 ¥0.92，
    一进本轮就被判定"超预算"直接跳过，compare/interval 同样被历史账拖累，
    结果 5/20、¥0.21 就收工，比不设刹车更差。
    读不到账本就返回 0：宁可少刹车，也不能因为记账故障卡住生产。
    """
    try:
        from . import db
        stamp = since or db.now()
        rows = db.query(
            "SELECT COALESCE(SUM(cost_cny), 0) AS s FROM remote_usage "
            "WHERE node_id = ? AND created_at >= ?", (f"author:{rel}", stamp))
        return float((rows[0] or {}).get("s") or 0.0) if rows else 0.0
    except Exception:                                    # noqa: BLE001
        return 0.0


def _contract_func_count(contracts: dict | None) -> int:
    """契约里到底有几个函数签名。0 = 这份契约没法验收（只有模块名不算契约）。"""
    return sum(len(getattr(c, "funcs", None) or []) for c in (contracts or {}).values())


def night(repo: Path, requirement: str, model: str, hours: float = 6.0,
          remote_profile: str = "", max_cny: float = 0.0, 
          use_remote: bool = True, max_rounds: int = 40, emit=None,
          plan: str = "", contract_file: str = "",
          scale_level: str = "") -> NightResult:
    """夜班入口：内部任何异常都不许让"早上没有交付物"，一定写 DELIVERY.md。

    plan：人给的实现方案（开发者模式用它当权威；甲方模式留空，由系统内部自己决定）。
    """
    try:
        return _night_inner(repo, requirement, model, hours, use_remote, max_rounds, emit,
                            remote_profile=remote_profile, max_cny=max_cny,
                            plan=plan, contract_file=contract_file,
                            scale_level=scale_level)
    except Exception as exc:                       # noqa: BLE001
        ws = Path(repo)
        state: dict = {}
        try:
            contracts = contract_mod.parse_requirement(requirement)
            if contracts:
                iterate.ensure_scaffold(ws, contracts)
                state = iterate.inspect(ws, contracts)
        except Exception:                          # noqa: BLE001
            state = {}
        p = write_delivery(ws, requirement, state, [],
                           "夜班异常中断：%s: %s" % (type(exc).__name__, exc), 0.0)
        return NightResult(False, rounds=[],
                           verified=sum(s.get("verified", 0) for s in state.values()),
                           total=sum(s.get("funcs", 0) for s in state.values()),
                           modules={}, delivery_path=str(p),
                           reason=("异常中断：%s: %s" % (type(exc).__name__, exc))[:200])
