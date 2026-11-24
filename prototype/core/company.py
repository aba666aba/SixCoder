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
  - 每轮最多 5 张单、单张单最多 2 次尝试；
  - 收敛判据：连续 2 轮"已验收函数数"不增长 → 提前收工（不空转烧钱）；
  - 交接物：`DELIVERY.md`（含验收清单、未完成项、下次建议）+ `_logs/night-*.json`；
  - 只声明有硬证据的事：`verified` 只由机械判据产生，模型说什么都不算。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import contract as contract_mod
from . import db, enterprise, iterate, ollama, policy, settings_store, workorder

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
    # 非空 = 收敛判据叫人（振荡/停滞/预算/远端配置），watchdog 必须**真的停下**，
    # 不许把这种"收工"当 rc=0 的"跑完再来一遍"（2026-11-16 实测空转烧 ¥5）。
    need_human: str = ""


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


# 网络中断时，**原地等网络恢复**的最长秒数（0 = 不等）。
# 口径：网络不通不是"模型不会写"，把每个模块都判一次失败、整轮 0 产出是最坏解读。
_NET_WAIT_SECONDS = float(os.environ.get("SIXCODER_NET_WAIT_SECONDS", "900"))


def _wait_network(ws=None, emit=None, max_seconds: float = 900.0,
                  probe_interval: float = 15.0) -> bool:
    """等网络恢复。返回 True = 通了，False = 超时还没通。

    探活方式：**真的发一次极小的远端请求**（比探 DNS 更准 ——
    DNS 能解析不代表 API 能连上）。
    """
    import time as _time
    limit = max(0.0, float(max_seconds))
    if limit <= 0:
        return False
    deadline = _time.time() + limit
    attempt = 0
    while _time.time() < deadline:
        attempt += 1
        try:
            from . import remote as _rm
            if _rm.available():
                return True
        except Exception:                            # noqa: BLE001
            pass
        if emit:
            emit("network_wait", {"attempt": attempt,
                                  "left_s": max(0, int(deadline - _time.time())),
                                  "note": "网络不可达，等恢复中（与模型能力无关）"})
        _time.sleep(max(1.0, float(probe_interval)))
    return False


def _is_hard_failure(reason) -> bool:
    """这张单的失败是不是**硬判据**给的（机械证据），而不是"模型没吐 JSON"这类软失败。

    为什么用它决定升级：造物模式的上限是远端推理能力，本地被机械判据判红说明"它做不出来
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


def done_count_of(cov: dict | None) -> int | None:
    """端到端通过能力数；观测没跑/没这个字段时返回 None（= 未知，不是 0）。

    为什么单独抽出来（2026-11-18 血泪）：stall 判定原来只看函数级 verified，
    实测端到端 done 从 43 一路涨到 55（coverage 54%→64%）时 verified 恒 6 不动，
    被误判"连续 3 轮无增长"收工 —— 把"未知"当 0、把契约层当业务事实，两个都坑。
    """
    if not isinstance(cov, dict) or not cov:
        return None
    done = cov.get("done")
    if done is None:
        return None
    if isinstance(done, (list, tuple, set)):
        return len(done)
    if isinstance(done, int):
        return done
    return None


def progress_grew(verified: int, prev_verified: int, done: int | None,
                  prev_done: int | None) -> bool:
    """这一轮有没有真进展：函数级 verified 或端到端 done 任一在涨就算。

    done 为 None（本轮观测跳过）时退回只看 verified —— 绝不把"没测到"当成"退步/停滞"。
    两条线都不涨、且 done 可比时，才由调用方累加 stall。
    """
    if verified > prev_verified:
        return True
    return done is not None and prev_done is not None and done > prev_done


def _stronger_alternative(current: str) -> str:
    """找一个"参数更大或同为 7B 级、且更会写代码"的已安装模型；明显更弱的一律不换。

    依据（实测）：3B 在同一个夜班任务上是 2/8 机械验收、2/16 独立验收，且大多数失败是
    "生成代码无法解析"。把本地模型从 7B 换成 3B 只能浪费一轮预算。
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


def _test_name(rel: str, lang: str) -> str:
    """各语言的测试文件命名（写进提示词，让远端知道该交哪个文件）。

    只给**语言自带的测试约定**，不引第三方框架 —— 远端写出来的文件本地
    才跑得动（这也是"本地有编译器就能验"的前提）。
    """
    p = Path(rel)
    return {
        "go": f"{p.stem}_test.go",
        "java": f"Test{p.stem}.java",
        "javascript": f"{p.stem}.test.js",
        "typescript": f"{p.stem}.test.ts",
        "csharp": f"Test{p.stem}.cs",
        "rust": f"test_{p.stem}.rs",
        "python": f"test_{p.stem}.py",
        "php": f"{p.stem}Test.php",
        "ruby": f"test_{p.stem}.rb",
        "swift": f"{p.stem}Tests.swift",
    }.get(lang, f"{p.stem}_test{p.suffix}")


def _night_foreign(ws: Path, requirement: str, model: str, lang_info: dict,
                   t0: float, emit=None, task_id: str = "") -> NightResult:
    """非 Python 语言的夜班：契约解析照旧（语法是语言无关的），验证换成本语言的工具链。

    C 档落点 1（2026-11-11）：从「编译级、永不通过」升级为**真跑该语言的测试套件**。
    三条口径（不许含糊）：
      · 远端可以交**多个文件**（含 `*_test.go` / `*.test.js` 等测试文件）——
        旧版只收 1 个文件且明令"不要写测试文件"，导致永远没有测试可跑；
      · 每个文件先过本语言编译器（`lang.verify`），全仓再跑 `run_typecheck` /
        `run_tests`（真命令、真退出码）；工具链没装 → **不产出该判据**，如实写"没测到"；
      · `NightResult.ok/verified` 只在**编译级证据 + 有测试套件真跑绿**时才算通过；
        只有编译级证据时仍是 ok=False（不冒充契约示例级验收），但要把证据写清。
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
        """外语作者回路：紧凑提示词 → JSON 解析 → **全量**落盘 → 本语言编译 → 带错重试。

        C 档：允许远端交多个文件（含测试文件），全部落盘 —— 旧版 `or files[:1]`
        只留一个，模型交 3 个文件也会被丢掉 2 个，`go test` 永远找不到测试。
        """
        from . import author as _a
        from . import remote as _r
        err = ""
        for attempt in range(1, attempts + 1):
            body = (
                f"你是 {lang_info['name']} 程序员。按下面的需求写 **{rel}** 及其配套文件的完整源码。\n\n"
                f"【需求】\n{requirement}\n\n"
                f"【硬性要求】\n"
                f"1. 只输出一个 JSON 对象，形如 "
                f'{{"files":[{{"path":"{rel}","content":"完整源码"}},'
                f'{{"path":"{_test_name(rel, lang)}","content":"测试源码"}}]}}\n'
                f"2. content 必须是**能直接编译/运行的完整文件**（含必要的 import/包声明/main）\n"
                f"3. **要写测试文件**（{_test_name(rel, lang)}），覆盖需求点名的函数；"
                f"测试用该语言标准测试写法，不引第三方测试框架\n"
                f"4. 不要输出解释、不要输出 diff、不要用 markdown 代码块\n"
                f"5. 语言是 {lang_info['name']}，扩展名就是 {Path(rel).suffix}\n"
            )
            if err:
                body += f"\n【上一版没通过，必须改掉】\n{err[:1200]}\n"
            r = _r.complete(body, task_id=task_id, node_id=f"author:{rel}",
                            workspace=str(ws), emit=emit, json_mode=True, max_out=8000)
            if not r.get("ok"):
                return {"ok": False, "reason": f"远端失败：{r.get('error')}"[:200]}
            files, _issues, _dl = _a._parse_reply(r.get("answer") or "")
            # 主文件必须交出来；配套文件（测试/辅助模块）全部落盘，不许只留一个。
            mine = [x for x in files
                    if str(x.get("path", "")).endswith(Path(rel).name)]
            if not mine or not str(mine[0].get("content") or "").strip():
                err = '没有给出 {"files":[{"path":...,"content":...}]} 形式的主文件内容'
                continue
            for f in files:
                fp = str(f.get("path") or "").strip()
                if not fp or ".." in fp.split("/") or fp.startswith("/"):
                    continue                        # _parse_reply 已过滤，双保险
                try:
                    (ws / fp).parent.mkdir(parents=True, exist_ok=True)
                    (ws / fp).write_text(_a._strip_fence(str(f.get("content") or "")),
                                         encoding="utf-8")
                except OSError:
                    continue
            v = _lang.verify(ws, rel, lang)
            if v.get("ok"):
                return {"ok": True, "applied": [str(f.get("path")) for f in files],
                        "attempt": attempt, "by": r.get("by") or "",
                        "how": v.get("how")}
            err = f"{v.get('how')} 报错：{v.get('detail')}"
        return {"ok": False, "reason": err or "未通过验证", "attempt": attempts}

    results: dict[str, dict] = {}
    spent = 0.0
    for rel in sorted(contracts):
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

    # C 档落点 1：全仓真跑该语言的**类型检查 + 测试套件**。
    # 只在至少有一个模块过了编译级证据时才跑（全红时跑测试只会得到一堆
    # "文件不存在"的噪音，还浪费时间）；工具链缺失时跳过并如实写"没测到"。
    typecheck = {"ok": False, "how": "skipped", "detail": "没有编译通过的模块，未跑"}
    tests = {"ok": False, "how": "skipped", "detail": "没有编译通过的模块，未跑"}
    if ok_files:
        typecheck = _lang.run_typecheck(ws, lang)
        tests = _lang.run_tests(ws, lang)
        if emit:
            emit("foreign_suite", {"lang": lang,
                                   "typecheck": {k: typecheck.get(k) for k in ("ok", "how")},
                                   "tests": {k: tests.get(k) for k in ("ok", "how")}})

    def _suite_line(label: str, r: dict) -> str:
        if r.get("how") == "skipped":
            return f"{label}：未跑"
        # unsupported = **这个语言没有验证方式**，不是"验证失败"。
        # 2026-11-24 修正：原来它会落到最后一行的"未通过"，把"工具不认识"
        # 显示成了"代码不行"。用户口径：拿不到信号要说拿不到，不能说成不通过。
        if r.get("how") == "unsupported":
            return f"{label}：测不了——该语言没有验证方式（不是不通过）"
        if r.get("skipped"):
            return f"{label}：测不了（{r.get('how')}）"
        return f"{label}：{'通过' if r.get('ok') else '未通过'}（{r.get('how')}）"

    # 证据等级判定：编译级证据是**门槛**，测试套件真跑绿才算通过。
    # 没配测试命令 / 工具链没装 → 如实停在编译级，不冒充验收通过。
    suite_green = bool(tests.get("ok")) and not tests.get("skipped")
    passed = bool(ok_files) and not bad and suite_green
    reason = (f"{_lang.LANGS.get(lang, {}).get('name', lang)}：{len(ok_files)}/{len(results)} "
              f"个模块通过{_lang.judge_level(lang)}。\n"
              f"{_suite_line('类型/静态检查', typecheck)}；"
              f"{_suite_line('测试套件', tests)}。")
    if not suite_green:
        reason += ("\n**未计入验收通过**：测试套件没有真跑绿，或该语言没配测试命令。"
                   "契约示例级判据目前只有 Python 有，不冒充。")
    if tests.get("detail") and not tests.get("ok"):
        reason += f"\n测试输出：{str(tests.get('detail'))[:400]}"
    if bad:
        reason += " 未通过：" + "；".join(f"{k}（{v[:120]}）" for k, v in list(bad.items())[:3])
    try:
        write_delivery(ws, requirement, {}, [], reason, time.time() - t0,
                       scope={"level": "S", "lang": lang,
                              "tests_ok": bool(tests.get("ok")),
                              "typecheck_ok": bool(typecheck.get("ok"))})
    except Exception:                                    # noqa: BLE001
        pass
    return NightResult(passed, spent_s=round(time.time() - t0, 1), reason=reason,
                       verified=len(ok_files) if passed else 0,
                       total=len(results), modules=results)


def _night_inner(repo: Path, requirement: str, model: str,
          use_remote: bool = True, max_rounds: int = 40, emit=None,
          plan: str = "", contract_file: str = "",
          remote_profile: str = "",
          scale_level: str = "") -> NightResult:
    """夜班：在预算内反复"体检→派工→开发→质检"，直到收敛或预算耗尽。

    三档模式的**协作方式**不同（见 core/policy.py）：
      造物模式：任何决策由远端拍板；本地被硬判据判红就立刻交给远端写代码；
      精修模式：实现方案必须由人给；要架构决策而没有方案时抛 needhuman，不替人拍板；
      审阅模式：不写任何文件，直接拒绝跑夜班。
    """
    t0 = time.time()
    # 项目宪法：跑夜班前先落盘（幂等，已存在不覆盖）。作者提示词会把宪法当稳定前缀注入。
    try:
        from . import constitution as _const
        _const.ensure(Path(repo))
    except Exception:                                # noqa: BLE001
        pass
    # 标记"本进程当前服务的产物仓"：让不传 workspace 的远端调用也能归账（一门一账）。
    try:
        from . import project_ledger as _pl_ws
        _pl_ws.set_current(repo)
    except Exception:                                # noqa: BLE001
        pass
    # 夜班开局把项目账本真值同步回 night_spent.json 镜像（2026-11-19）。
    # 该镜像按 goal 隔离、每轮拼契约会漂移 key → 卡在旧值撒谎，误导交付报告/patrol。
    # 停机仍只读 project_ledger，这里只是让"报告总消费"的镜像不再走样。
    try:
        from . import auto as _auto_sync
        _auto_sync.sync_spent_from_ledger(Path(repo), requirement)
    except Exception:                                # noqa: BLE001
        pass
    from . import permissions as _perms
    from . import policy as _policy
    pol = _policy.current()
    if pol.code_escalation == _policy.OFF or not _perms.allows("file.write"):
        return NightResult(False, reason=f"{pol.name}不写文件，不能跑夜班；请切到造物模式或精修模式",
                           spent_s=round(time.time() - t0, 1))

    ws = Path(repo)
    # 规模分级：不同代码规模该有的约束不一样（见 core/scale.py）。
    # 造物模式的要求是"提需求时写明目标规模"；没写就按实测推断，并在交付报告里写明。
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
    # 门槛：本地模型必须 ≥7B（实测 3B 机械验收 2/8、独立验收 2/16，太小不能用）
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
        return _night_foreign(ws, requirement, model, _linfo, t0, emit=emit)
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
    # 契约点名表（2026-11-11，§11.4g）：**端点 → 处理函数名**。
    # 契约文本里本来就写着"这个端点用哪个函数"（签名行自带路径），
    # 那是唯一权威 —— 接线按它接、体检按它判、写码按它点名，
    # 三处同源，不再靠 `_pick_func` 字符串猜。
    # 不接管的代价（实测）：`GET /api/v1/people/teachers/me` 被猜到负载建议函数上
    # → 返回错结构 → 端到端永远红，而正确的函数就在业务层文件里。
    try:
        from . import routefuncs as _rfmod
        _route_funcs = _rfmod.parse(locked or requirement)
    except Exception:                                    # noqa: BLE001
        _route_funcs = {}
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
        # 造物模式的契约本来就该由系统内部搞定：先试确定性兜底，再让远端把需求翻译成契约。
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
            # 规格优先：造物模式用**最强档 + 思考开**产出规格（这是全链最高杠杆的一步）；
            # 精修模式走阶梯默认，且歧义会停下来问人而不是自己拍板。
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
        _batches = _proj.batch_plan(_proj.order_modules(contracts), 4)
        if emit:
            emit("estimate", {"est": _est, "batches": len(_batches)})
        scope = {**(scope or {}), "estimate": _est,
                 "batches": [len(b) for b in _batches]}
    except Exception as _exc:                            # noqa: BLE001        if emit:
            emit("notice", {"message": f"项目地图生成失败：{type(_exc).__name__}"})

    # ---- 造物模式也要有 blueprint（能力表 + 路由期望）----
    # 2026-11-10：造物模式长期**绕开 blueprint 这条线**，于是形成了闭环：
    #   写出来的模块**路由接不上**（契约只验函数、不验路由 → 35/35 全过）
    #   → 端到端红灯**没人知道**（从不跑能力覆盖，账本停在 16:34）
    #   → 完成判据**只看模块契约**（齐了就收工）。
    # 产物仓库里已有的 blueprint.json 就是这份需求生成的（goal 字段 = 需求原文），
    # 所以这里**优先复用**（断点续跑），没有才生成。
    bp = None
    try:
        from . import blueprint as _bp_mod
        from . import openhouse as _oh
        bp = _bp_mod.load(ws)
        # 归因映射表：模块 → 能力（见 core/attribution 的 caps_for_module）。
        # 必须在这里登记 —— 早于 bp 加载就会拿到 None，晚于第一次派单就会漏归因。
        try:
            from . import attribution as _attr_bp
            _attr_bp.set_blueprint(bp)
        except Exception:                                # noqa: BLE001
            pass
        if not bp or not bp.caps:
            bp = None
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"blueprint 加载跳过：{type(exc).__name__}"})
    if bp is None:
        try:
            from . import blueprint as _bp_mod
            _got = _bp_mod.derive(requirement, llm=None, ws=ws)
            if _got.get("ok"):
                bp = _got["blueprint"]
                _bp_mod.save(bp, ws)
            else:
                bp = None
                if emit:
                    emit("notice", {"message": "blueprint 生成失败（能力表缺失，"
                                               "本轮只按模块契约推进）"})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"blueprint 生成跳过：{type(exc).__name__}"})
            bp = None
    if bp is not None:
        if emit:
            emit("blueprint_loaded", {"caps": len(bp.caps),
                                      "note": "造物模式接上 blueprint：能力表 + 路由期望（§八）"})
        # 开工三件事（骨架树 / 接口锁定 / 扩展点）+ 规模推导 —— 与全自主共用一份实现
        try:
            from . import openhouse as _oh
            _oh.prepare(ws, bp, emit=emit)
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"开工段跳过：{type(exc).__name__}"})
    # 开局估算：蓝图构建后算"预计需要多少钱"，供用户充值参考（单价由用户填，此处只给 token 参考）
    if bp is not None and emit:
        try:
            from . import budget_estimate as _be3
            _est = _be3.estimate_from_blueprint(ws)
            if _est.get("ok"):
                emit("night_estimate", _est)
        except Exception:                            # noqa: BLE001
            pass

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
    prev_done: int | None = None      # 上一轮端到端通过数（stall 要看业务事实，不能只盯 verified）
    fatal_rounds = 0
    reason = ""
    need_human = ""      # 叫人标记：见 NightResult.need_human（只有"停下叫人"的收工才置）
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
    whole_rounds = 0                    # 整仓会话落地的轮数（交付报告里要看得出）
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
    # 造物模式：开局先让远端出实现方案（远端推理能力是这一档的上限，方案先由它定，
    # 细节也由它定；后面每一张单都按这个方案做，避免"系统自己边做边改架构"）
    auto_plan = ""
    # #17 造物模式两段式：`SIXCODER_CLIENT_PLAN_GATE=1` 或设置里打开后，
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
                "造物模式计划门禁：已生成实现计划并停下等人批（不会先写代码）。"
                f"计划在 {pfile}；批/改后用 --plan-file {pfile} 继续跑"))
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"计划门禁跳过：{type(exc).__name__}: {exc}"})

    if pol.plan_first and use_remote and _remote_available():
        try:
            # 方案/规格的档位策略由 policy 决定：
            #   造物模式（spec_tier="strongest"）→ **先用最强档**再思考开：方案定了之后
            #   每一张单都照它做，用最低档定方案等于用最弱的算力做最高的决定；
            #   精修模式（spec_tier="ladder"）→ 仍从最低档起（人会给方案，这里只是兜底）。
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

    # 临时区垃圾回收：引擎**自己**留下的探针沙箱自己清（开跑前回收一点）。
    # 以前这些目录从不删（逐个删会撞"单轮删除 ≥50 个"的保护），实测毕设产物仓库
    # 堆到 36754 个 / 210MB，连 `count_lines` / `isolate._py_files` 都被拖慢。
    # 现在：进程退出时删自己那组（run-{pid}/），跨进程残留靠这里回收 ——
    # 只删引擎自己的命名空间（probe-* / run-*）、只删 24 小时前的、
    # **每批删除条目数 ≤15**（宿主的阈值是"单轮 ≥50 条要确认"，判定是 >=，
    # 且统计的是**条目数**（目录+文件）不是目录数 —— 实测 45 个目录 = 79 条被拒）。
    # 开跑只回收 50 个（约 15 秒）—— **绝不能让夜班等在这上面**；
    # 剩下的存量由独立进程慢慢清。
    _gc = {}
    try:
        from . import probe as _probe_gc
        _gc = _probe_gc.gc_scratch(ws, max_age_hours=24.0, max_dirs=50)
        if emit and (_gc.get("removed") or 0):
            emit("scratch_gc", {"removed": _gc.get("removed"), "scanned": _gc.get("scanned"),
                               "kept": _gc.get("kept"),
                               "freed_mb": round((_gc.get("freed_bytes") or 0) / 1048576, 1),
                               "note": "回收引擎自己的探针临时目录（只删 probe-*/run-*、24 小时前、"
                                       "每批删除条目数 ≤15）"})
    except BaseException as exc:                      # noqa: BLE001
        # 宿主的删除保护抛 BaseException 子类 —— 清理失败绝不能带走夜班。
        if emit:
            emit("notice", {"message": f"临时区回收跳过：{type(exc).__name__}"})

    # 引擎自身的 .tmp 也要清（2026-11-18）：bizflow_*/httpe2e_* 等自检与 http 流
    # 测试的残留只认 48 小时龄，每批最多 4000 条目 —— 实测 647 目录 / 61MB 按天涨、
    # 从没人清。同样兜 BaseException（清理失败不能带走夜班）。
    try:
        from . import probe as _probe_gc2
        _ge = _probe_gc2.gc_engine_tmp(max_age_hours=48.0, max_entries=4000)
        if emit and (_ge.get("removed") or 0):
            emit("engine_tmp_gc", {"removed": _ge.get("removed"),
                                   "kept": _ge.get("kept"),
                                   "freed_mb": round((_ge.get("freed_bytes") or 0) / 1048576, 1),
                                   "note": "回收引擎自身 .tmp（48 小时前、条目预算分批）"})
    except BaseException as exc:                      # noqa: BLE001
        if emit:
            emit("notice", {"message": f"引擎 .tmp 回收跳过：{type(exc).__name__}"})

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
    from . import budget_estimate as _be   # 唯一停机线：系统总预算（用户不可设上限）

    # ---- 能力覆盖状态（§三「机械验收：判据 + 闸门 + 端到端业务流」）----
    # 造物模式长期**不跑**能力覆盖 → 端到端红灯没人知道 → 完成判据只看模块契约。
    # 这里是那缺失的一环：观测 → 红灯变工作单 → 写码 → 接线 → 下轮重新观测。
    cap_red: set[str] = set()               # 端到端红的模块（要干活）
    triage: dict | None = None              # 本轮失败分诊（决定派单与否）
    cap_detail: dict[str, list[str]] = {}   # 每个模块的端到端失败详情（喂给远端）
    cap_endpoints: dict[str, list[str]] = {}  # 每个模块该注册的 HTTP 端点清单
    cap_recent: set[str] = set()            # 上一轮处理过的模块（轮转用）
    cap_done: set[str] = set()              # 已观测通过的能力（增量观测用）
    cap_cov: dict = {}                      # 最近一次观测结果
    biz_flow: dict | None = None            # 最近一次业务流结果（下一轮派单用）
    if bp is not None:
        if emit:
            emit("cap_stage", {"stage": "ready", "caps": len(bp.caps),
                                "note": "能力覆盖观测已接进造物模式（§三 端到端业务流）"})

    for rnd in range(1, max_rounds + 1):
        # 夜班批开始时审核者休眠（2026-11-18 死局协商：状态按批清零，
        # 上一批的协商结论不带进新批 —— 每批的仓库状态和死局都是新的）。
        if rnd == 1:
            try:
                from . import deadlock as _dl0
                _dl0.reset(ws)
            except Exception:                            # noqa: BLE001
                pass
        # 钱到顶就停：只记花费不拦人的问题在这里补上
        # 唯一停机机制：总花费超过系统总预算 5% 自动停机（预算写在 .sixcoder/budget.json，用户不可改）
        if _be.system_budget_stopped(ws, task_id):
            # 双闸并行（2026-11-11）：说清是**哪个闸**触发的。
            # 余额闸 = 官方事实（余额耗尽）；预算闸 = 引擎记账（估算）。
            _why = "系统总预算超额 5%，自动收工（详见交付报告）"
            _by_balance = False
            try:
                from . import balance as _bal
                _hit, _bwhy = _bal.balance_exhausted()
                if _hit:
                    _by_balance = True
                    _why = ("**官方余额已耗尽**（%s）→ 自动收工"
                            "（余额是官方事实，比自算花费更硬）" % _bwhy)
            except Exception:                            # noqa: BLE001
                pass
            # **预算闸触发才升档**（2026-11-19 用户口径）：停一次后允许正常重启，
            # 下次停机线 = 上一档 ×1.2（105→126→151.2…）。余额耗尽不升档——
            # 没钱就是没钱，升线只会让它白跑一轮又停。
            if not _by_balance:
                try:
                    _lvl = _be.bump_stop_level(ws)
                    _thr = _be.stop_threshold(ws)
                    _why += "（已升到第 %d 档，下次停机线 ¥%.2f）" % (_lvl, _thr)
                except Exception:                        # noqa: BLE001
                    pass
            reason = _why
            need_human = reason     # 预算/余额耗尽：继续跑也没钱，必须叫人
            if emit:
                emit("system_budget_stop", {"action": "收工", "reason": reason})
            break
        try:
            state = iterate.inspect(ws, contracts)
            verified = sum(s["verified"] for s in state.values())
            workorder.record_progress(task_id, rnd, state, note=f"夜班体检({strategies[strategy_i]})")

            # ---- 端到端能力观测（§三「机械验收：… + 端到端业务流」）----
            # 增量：只测**还没通过**的能力（已 done 的不必重测），
            # 全量 84 项约 116 秒，增量通常几十秒。
            if bp is not None and bp.caps:
                try:
                    from . import openhouse as _oh
                    _todo = {c.id for c in bp.caps if c.id not in cap_done}
                    if _todo:
                        _cov = _oh.observe(ws, bp, emit=None, only=_todo)
                        if _cov:
                            cap_cov = _cov
                            for _cid, _c in (_cov.get("caps") or {}).items():
                                if str((_c or {}).get("status") or "") == "done":
                                    cap_done.add(_cid)
                            # 失败现象快照（2026-11-19）：让死局协商能看到
                            # "探针实际返回 vs 期望"的完整证据，而不是只看判据定义。
                            # 只 I/O、不改蓝图/产物；失败只留痕不炸主流程。
                            try:
                                from . import coverage as _covmod
                                _covmod.save_failures(ws, _cov)
                            except Exception as exc:      # noqa: BLE001
                                if emit:
                                    emit("notice", {"message": "失败现象快照跳过：%s"
                                                    % type(exc).__name__})
                            cap_red, cap_detail = _oh.red_modules(_cov, bp, ws=ws)
                            # **失败分诊**（2026-11-15）：先判"谁该修"，再决定要不要花钱。
                            # 实测依据（11-12 抽检 40 条）：85% 是"函数级全过、端到端红"，
                            # 其中 404/422 属接线层（本地零成本可修）却一直被派给远端重写函数
                            # —— 付钱让它修修不到的东西，还打坏已通过的能力。
                            triage = None
                            try:
                                from . import triage as _trimod
                                from . import attribution as _atmod
                                triage = _trimod.triage_round(
                                    ws, cap_red, cap_detail or {},
                                    caps_of=_atmod.caps_for_module)
                                if emit:
                                    emit("triage", {
                                        "counts": triage.get("counts") or {},
                                        "dispatch": len(triage.get("dispatch") or []),
                                        "local": len(triage.get("local") or []),
                                        "env": len(triage.get("env") or []),
                                        "note": _trimod.describe(triage),
                                    })
                            except Exception as exc:     # noqa: BLE001
                                if emit:
                                    emit("notice", {"message": "分诊跳过：%s" % type(exc).__name__})
                            # 端点清单：这个模块**该负责**哪些 HTTP 端点。
                            # 能力表的端点是产品语义（quota-suggestions），
                            # 契约里的函数是算法语义（quota_advice）——
                            # 引擎靠字符串猜接不上（实测 20 个端点"无匹配函数"）。
                            # 语义映射交给远端（它同时看得见需求和契约）：
                            # 明确告诉它要注册哪些端点，它用 FastAPI 装饰器注册，
                            # 接线层会优先读产物里的装饰器。
                            cap_endpoints.clear()
                            for _m in sorted(cap_red & set(contracts)):
                                _eps = _oh.endpoints_for_module(bp, ws, _m)
                                if _eps:
                                    cap_endpoints[_m] = _eps
                            _done_n = len(_cov.get("done") or [])
                            if emit:
                                emit("coverage", {"done": _done_n, "total": len(bp.caps),
                                                  "pct": round(100.0 * _done_n / max(1, len(bp.caps)), 1),
                                                  "red_modules": len(cap_red),
                                                  "note": "端到端业务流观测（契约判据测不出这一层）"})
                            # 记账：能力覆盖账本（append-only，救过命）
                            try:
                                from . import coverage as _covmod
                                _covmod.record(ws, _cov, f"造物模式第 {rnd} 轮观测")
                                from . import blueprint as _bp_mod
                                _bp_mod.save(_covmod.mark(bp, _cov), ws)
                            except Exception:            # noqa: BLE001
                                pass
                except Exception as exc:                 # noqa: BLE001
                    if emit:
                        emit("notice", {"message": f"能力观测跳过：{type(exc).__name__}"})
            # ---- 业务流优先派单的数据源（§20.4 落点 6）----
            # 2026-11-12 修复：这段原来**根本不在轮循环里** —— `biz_flow` 在
            # company.py:891 初始化为 None，而真正算出结果的赋值在 company.py:1597
            # （轮循环**结束之后**的收工段）。于是轮循环内传给 `_author_round` 的
            # `biz_flow` 永远是 None → `if biz_flow and biz_flow.get("ok") is False`
            # 永不成立 → **"业务流没过就把它涉及的角色模块提到最前"这条从来没触发过**
            # （§9.48.4 落点 6 半实现）。这里把它挪进轮循环，让**下一轮**派单真的
            # 拿得到上一轮的业务流结果。
            #
            # 口径与成本：
            #   · 业务流是**跨角色组合断言**，只在"契约全过但业务不对"时才有信息量
            #     → 只在**有端到端红灯**时算（没有红灯就不算，省掉子进程开销）；
            #   · 实测单次 1.1 秒、**零 token**（纯本地子进程跑不变量）；
            #   · 算不出来（applicable=False / 异常）→ 保持 None，如实不猜。
            if cap_red:
                try:
                    from . import flow as _flow_bf
                    _bf = _flow_bf.business_flow(ws, contracts)
                    if _bf.get("applicable"):
                        biz_flow = _bf
                    if emit:
                        emit("business_flow", {"ok": _bf.get("ok"),
                                               "applicable": _bf.get("applicable"),
                                               "passed": _bf.get("passed"),
                                               "total": _bf.get("total"),
                                               "note": "轮内业务流结果（供下一轮派单提权）"})
                except Exception as exc:                 # noqa: BLE001
                    if emit:
                        emit("notice", {"message": f"轮内业务流跳过：{type(exc).__name__}"})
            if emit:
                emit("round", {"round": rnd, "verified": verified,
                               "total": sum(s["funcs"] for s in state.values()),
                               "elapsed_min": round((time.time() - t0) / 60, 1),
                               "strategy": strategies[strategy_i]})
            # 进展判定必须看业务事实（2026-11-18）：verified 是函数级契约数，恒 6 不动；
            # 端到端 done 一路 43→55 才是真进展。任一在涨就不算停滞。
            _done_now = done_count_of(cap_cov)
            if progress_grew(verified, prev_verified, _done_now, prev_done):
                stall = 0
                stall_rounds = 0
            else:
                stall += 1
                stall_rounds += 1
            prev_verified = max(prev_verified, verified)
            if _done_now is not None:
                prev_done = _done_now if prev_done is None else max(prev_done, _done_now)

            # 停滞型死局唤醒（2026-11-19）：端到端连续多轮不涨 + 根因反复改同一批文件。
            # 这是 on_rollback / on_oscillation 都够不着的第三形状——卡住的能力本就
            # absent（弄红不了绿能力→不回滚）、每轮换不同文件（指纹一直变→不振荡），
            # 结果三方全程休眠、本地 rootcause 循环空转烧钱。
            # stall_rounds 是跨策略累计值（stall 每轮换策略被清零，不能用它判停滞）。
            # on_stall 内部有独立 attempts 守卫（最多 2 次），超次数会带 reason 返回，
            # 这里放行到后面的 stall>=3 收工逻辑兜底，不会无限烧钱。
            from . import deadlock as _dlmod_stall
            if stall_rounds >= _dlmod_stall.STALL_WAKE_ROUNDS:
                _sres: dict = {}
                try:
                    _red_caps = [str(k) for k, c in
                                 ((cap_cov or {}).get("caps") or {}).items()
                                 if str((c or {}).get("status") or "") != "done"]
                    _sres = _dlmod_stall.on_stall(
                        ws, rnd, "端到端连续 %d 轮无增长" % stall_rounds,
                        caps=_red_caps, stall_rounds=stall_rounds, emit=emit)
                except BaseException as _exc:              # noqa: BLE001
                    _tb = "".join(__import__("traceback").format_exception(
                        type(_exc), _exc, _exc.__traceback__))
                    try:
                        _dlmod_stall._log(ws, {"event": "deadlock_error",
                                               "where": "company.on_stall",
                                               "error": "%s: %s" % (type(_exc).__name__, _exc),
                                               "traceback": _tb[-1800:]})
                    except BaseException:                  # noqa: BLE001
                        pass
                    if emit:
                        emit("notice", {"message": "停滞死局协商跳过：%s: %s"
                                        % (type(_exc).__name__, str(_exc)[:120])})
                if _sres.get("continue"):
                    # 授予解锁 → 打法梯子重置，下一轮在解锁范围干活
                    strategy_i = 0
                    stall = 0
                    strat = strategies[strategy_i]
                    dec_used = write_used = 0
                    continue
                if _sres.get("judge_fixed") and int(_sres.get("changed") or 0) > 0:
                    # 改判据决议：收工但不叫人，watchdog 续跑带新判据（同振荡处置口径）
                    reason = ("停滞死局：判据已按三方投票修正（%d 处）→ 重启继续"
                              % int(_sres.get("changed") or 0))
                    if emit:
                        emit("judge_fix_applied", {
                            "changed": _sres.get("changed"),
                            "notes": (_sres.get("notes") or [])[:12],
                            "backup": _sres.get("backup") or "",
                            "kind": "stall"})
                    need_human = ""                       # 不叫人：让 watchdog 续跑
                    break
                if _sres.get("judge_fixed"):
                    reason = "停滞死局：三方判判据有病但 0 处可改 → 停下等人工"
                    need_human = reason
                    break
                # 真正走完协商且被拒（有 decision、无 reason 守卫键）→ 收工叫人，停止空转
                if "decision" in _sres and "reason" not in _sres:
                    reason = "停滞死局：三方未授权（%s）→ 停止空转" % _sres.get("decision")
                    need_human = reason
                    break
                # 否则是守卫挡住（超次数/已在流程）→ 继续正常流程，
                # 由后面的 stall>=3 收工逻辑兜底。

            # 振荡检测：回到来过的失败状态 = 这一轮改动没有净效果
            try:
                # 2026-11-10：指纹必须带上**端到端能力状态**。
                # 只看契约判据时，契约全过 → 指纹恒定 → 每轮都判"振荡"收工，
                # 而端到端能力集合明明在变（有进展）—— 那是**假振荡**，白跑。
                fp = _osc.fingerprint(state, cap_cov)
                osc = osc_tracker.add(rnd, fp)
                if osc["oscillating"]:
                    act = _osc.decide_action(osc, len(strategies) - 1 - strategy_i)
                    if emit:
                        emit("oscillation", {"round": rnd, "fp": fp,
                                             "first_round": osc["first_round"],
                                             "action": act["action"],
                                             "summary": _osc.summarize(state),
                                             "note": osc["note"]})
                    # 处置统一走 oscillation.act()：**换打法必须在本轮就干活**，不许 continue。
                    # 旧实现 switch_strategy 后直接 continue，于是这一轮什么都没做 → 下一轮观察到
                    # 完全相同的状态 → 又振荡 → 又跳过：round 2–6 五轮零 author_start（2026-11-07）。
                    _mode, _v = _osc.act(osc, act, strategy_i, len(strategies),
                                         dec_used=dec_used, write_used=write_used)
                    if _mode == "stop":
                        reason = "检测到振荡：" + str(_v)
                        # **收工前最后一问**（2026-11-18）：玩法用完、状态回到原样 =
                        # 死局形状②（不是"被回滚"那种形状①）。交给三方协商决定是否
                        # 解锁重试——不问就收工，等于把三把 key 白配了。
                        _dl_cont = False
                        # **必须先在 try 外初始化**（2026-11-19 实测事故）：
                        # on_oscillation 抛异常时 _dlr 未定义，后面 `_dlr.get(...)`
                        # 会再抛 UnboundLocalError → 被外层 except 吞成
                        # "振荡检测跳过：UnboundLocalError" → **既不 continue 也不
                        # break**，继续空转到下一轮。两个异常互相掩盖，机制整夜没生效。
                        _dlr: dict = {}
                        try:
                            from . import deadlock as _dlmod2
                            _red_caps = [str(k) for k, c in
                                         ((cap_cov or {}).get("caps") or {}).items()
                                         if str((c or {}).get("status") or "") != "done"]
                            _dlr = _dlmod2.on_oscillation(
                                ws, rnd, reason, caps=_red_caps, emit=emit)
                            _dl_cont = bool(_dlr.get("continue"))
                            if _dl_cont and emit:
                                emit("deadlock_unlock", {
                                    "round": rnd, "caps": _red_caps[:12],
                                    "note": "死局协商授予解锁 → 继续跑（解锁轮修复+三方复审）"})
                        except BaseException as _exc:          # noqa: BLE001
                            # **不许只打类型名**（铁律 7）：IndexError 是哪个下标、
                            # 在哪一行，全靠 traceback。这里落盘一行，便于事后定位。
                            _tb = "".join(__import__("traceback").format_exception(
                                type(_exc), _exc, _exc.__traceback__))
                            try:
                                _dlmod2._log(ws, {"event": "deadlock_error",
                                                  "round": rnd,
                                                  "error": "%s: %s" % (type(_exc).__name__, _exc),
                                                  "traceback": _tb[-1800:]})
                            except BaseException:                  # noqa: BLE001
                                pass
                            if emit:
                                emit("notice", {"message": "死局协商跳过：%s: %s"
                                                % (type(_exc).__name__, str(_exc)[:120])})
                        if _dl_cont:
                            # 继续跑：把打法梯子重置，下一轮在解锁范围内大胆修
                            strategy_i = 0
                            stall = 0
                            strat = strategies[strategy_i]
                            dec_used = write_used = 0
                            continue
                        if _dlr.get("judge_fixed") and int(_dlr.get("changed") or 0) > 0:
                            # **改判据决议**（三方 ≥2 票）：判据已按确定性规则修好并
                            # 落盘（含备份）。蓝图只在开跑时加载一次 → 这里**收工但不
                            # 叫人**（不设 need_human）：看门狗 10 秒后自动拉起新进程，
                            # 用新判据继续，不用人工介入。
                            reason = ("判据已按三方投票修正（%d 处）→ 重启后带着新判据继续"
                                      % int(_dlr.get("changed") or 0))
                            if emit:
                                emit("judge_fix_applied", {
                                    "changed": _dlr.get("changed"),
                                    "notes": (_dlr.get("notes") or [])[:12],
                                    "backup": _dlr.get("backup") or ""})
                            need_human = ""        # 不叫人：让 watchdog 续跑
                            break
                        if _dlr.get("judge_fixed"):
                            # **投票判"判据有病"但确定性规则一处也改不动**（2026-11-19
                            # 实测：审核者指出的是"同一教师 201 后又要求 403"这类语义
                            # 冲突，规则库覆盖不到）。此时**必须叫人**：不叫就会
                            # "投票→零改动→重启→再投票"无限循环烧钱。
                            reason = ("三方投票判定判据有病，但确定性修复无可用规则（0 处改动）"
                                      "→ 停下等人工：审核者已给出改法，见 unlock_log")
                            need_human = reason
                            break
                        need_human = reason   # 协商未授予：再跑也是同一场空转
                        break
                    strategy_i = int(_v)
                    stall = 0
                    strat = strategies[strategy_i]
                    if emit:
                        emit("strategy", {"round": rnd, "strategy": strat,
                                          "why": "振荡后换打法（本轮回合继续干活）"})
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
                need_human = reason   # 策略用尽仍不涨：叫人，不许 watchdog 反复拉起
                break
            strat = strategies[strategy_i]

            # 取单：优先架构师（architect 档及之后都问），否则机械派工
            root, orders = "", []
            # 整仓会话开关（本函数后面还要用，所以在这里定一次）：
            # 整仓会话自带 `<rootcause>` 归因，再单独问一次架构师等于同一份报告花两次钱。
            _whole_on = os.environ.get("SIXCODER_WHOLE", "1") not in ("0", "false", "False")
            ask_architect_ok = use_remote and strat in ("architect", "swap_model", "decompose")
            if _whole_on:
                ask_architect_ok = False
                if emit:
                    emit("notice", {"message": "整仓模式：跳过独立架构师调用（归因并入整仓会话的 rootcause）"})
            if ask_architect_ok and pol.arbiter == "human" and not (plan or "").strip():
                # 精修模式：架构决策归人。没有方案就不许让远端替人拍板 —— 抛 needhuman 停下问人，
                # 这一轮只做机械派工（triage），不产生任何"系统自己定的架构"。
                ask_architect_ok = False
                if emit:
                    emit("needhuman", {
                        "round": rnd, "node": "architect",
                        "question": "需要架构决策，但还没有实现方案。请给出具体实现方案"
                                    "（模块划分/函数职责/数据流），系统只会照它实现，不会自己改架构。",
                        "why": "精修模式下 plan_authority=human"})
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
            # 2026-11-10：**端到端红灯 → 生成工作单**（造物模式并入 blueprint 后的关键一环）。
            # 以前工作单只由 `workorder.triage` 按**模块级判据**分诊 ——
            # 契约 35/35 全过 → 无单 → 收工，于是"契约过了但端到端 404"的那些活
            # 永远没人干（实测 84 项能力只过 13 项，账本停在 16:34）。
            # 现在把端到端红灯涉及的模块也派成单，带上**端到端失败详情**。
            if cap_red and cap_detail:
                _have = {str(o.get("target") or "") for o in orders}
                _cap_new = []
                # 2026-11-11（§11.4e 所有权）：落点必须先过**文件所有权**判定 ——
                # 接线层（app/api/*）是引擎接线重渲染的地盘，远端写在那里
                # 会被下次接线冲掉，还跟切片线的业务层实现抢同一个落点。
                # 实测 12:11 远端在 `app/api/people.py` 写了 22KB 业务适配，
                # 与切片线的 `app/services/people.py` 互相覆盖 →
                # `teachers_me_route` 签名在两套契约里不一致（account / authorization）
                # → 卡死 4 次 → order_failed。判定只有一处：openhouse.actionable_modules。
                try:
                    from . import openhouse as _oh_act
                    _cap_mods = _oh_act.actionable_modules(cap_red, ws)
                    _cap_ev = _oh_act.actionable_details(cap_red, cap_detail, ws)
                except Exception:                            # noqa: BLE001
                    _cap_mods = sorted(str(m) for m in cap_red)
                    _cap_ev = dict(cap_detail or {})
                # 分诊闸门：只有 remote_code / contract / unknown 才派单。
                # local_wiring（404/422/缺 import）与 env（服务没起）**不派单** ——
                # 前者本地重接线零成本，后者派给远端也修不了。
                _allow = None
                if triage:
                    _allow = set(triage.get("dispatch") or [])
                    _skipped = [m for m in _cap_mods if m not in _allow]
                    if emit and _skipped:
                        emit("triage_gate", {
                            "dispatched": len(_allow),
                            "skipped": _skipped[:6],
                            "note": "triage 判 dispatch 的照常派单；这里跳过的是"
                                    "本地可修（接线/import）/环境问题，不花钱派给远端"})
                # 2026-11-17：去掉 `_m not in contracts` 窄表过滤。
                # 实测：锁定的 full_contract 只覆盖 3 个模块，triage 判 19 条红灯
                # 要派单，却有 16 条被这行滤掉 → cap_orders 从 19 掉到 1-2；
                # 而能进来的 3 个恰好都是护栏命门（accounts/views 承载 12 条已绿），
                # 改了必被回滚 → 净增长恒 0。
                # 契约的职责是命名稳定 + 缓存前缀，不是派单白名单：远端拿的是
                # 全仓写权限，落点由 actionable_modules（文件所有权判定）给出，
                # 失败证据由 actionable_details 带上，不需要契约授权。
                # 只保留 _have 去重 + _allow 分诊闸门（local_wiring/env 仍不派）。
                # 单一实现在 _cap_orders_from_red（纯函数，check 可真跑钉住）。
                _cap_new = _cap_orders_from_red(
                    _cap_mods, _have, _allow, _cap_ev)
                _have.update(o["target"] for o in _cap_new)
                if _cap_new:
                    # **放在最前面**：旧路径只取 orders[:5]，追加在末尾就永远排不上
                    # （实测 6 轮都在写同样 3 个模块，其余 10 个红灯模块从没被碰过）。
                    # 配合稳压器②（同单 3 次后隔离），13 个红灯模块会自动轮流被处理。
                    orders = _cap_new + orders
                    if emit:
                        emit("cap_orders", {"n": len(_cap_new),
                                            "modules": [o["target"] for o in _cap_new][:6],
                                            "note": "端到端红灯已转成工作单（置顶）"})
            if not orders:
                # 全部被隔离 → 换更细的干法：对失败函数下"先写常量实现"的单
                pending = [(p, f) for p, st in state.items() for f in sorted(st["stubs"])]
                orders = [{"kind": "implement", "target": p, "symbol": f,
                           "acceptance": f"{f} 不再是桩", "detail":
                           f"先写出最小可运行实现：返回符合类型的常量/空聚合，之后再补逻辑"}
                          for p, f in pending[:3]]
                if emit and orders:
                    emit("strategy", {"round": rnd, "strategy": "decompose-minimal"})
            # ── 整仓会话（2026-11-15 用户定稿，默认路径）─────────────────────
            # 用户原话："每次都要是整个仓库的权限，只给改几个文件，跨越了好几个
            # 文件的问题就找不出来，也改不了。"
            # 所以本轮只要有活，就把**全仓源码 + 整仓写权限**交给远端一次：
            # 让它先写 `<rootcause>`（深层共同因，注明解释了哪些失败），
            # 再改根因、最后才治孤立小问题。它改了就本轮收工，下一轮重新观测。
            # 只有"它一个字都没改"时才回落到逐单派工（旧口径）。
            # 关掉：环境变量 SIXCODER_WHOLE=0（应急/对照实验用）。开关在取单前已定。
            # 只在**真的有活**时开会话（有工作单或端到端红灯）。全绿时开会话等于
            # 花一次全仓输入的钱换一句"没问题"——不做。
            if _whole_on and (orders or cap_red):
                try:
                    from . import wholerepo as _whole
                    # 红线：把"已通过的能力"和"上一轮弄红的"一起交给远端。
                    # 没有这份清单时它是**闭着眼睛改**——实测 6 轮覆盖在 27~32%
                    # 之间震荡、净增长为零（2026-11-15 21:00-21:55）。
                    _caps = (cap_cov or {}).get("caps") or {}
                    _passed = [cid for cid, v in _caps.items()
                               if str((v or {}).get("status") or "") == "done"]
                    # descoped 单独拿出来：它们不是"红"，不该进红线也不该算回归
                    _descoped = [cid for cid, v in _caps.items()
                                 if str((v or {}).get("status") or "") == "descoped"]
                    _prev = _whole.load_passed(ws)
                    # 立刻把"本轮通过"落盘：下一轮拿它当红线比对（谁被弄红了）
                    _whole.save_passed(ws, _passed)
                    _wrep = _whole.report_from_state(
                        cap_cov=cap_cov, cap_detail=cap_detail, state=state,
                        triage=triage, orders=orders, ws=ws,
                        passed=_passed, prev_passed=_prev, descoped=_descoped)
                    _wres = _whole.session(ws, _wrep, emit=emit, round_no=rnd,
                                           total_rounds=max_rounds,
                                           extra=("本轮业务流：" + str(biz_flow)[:600]
                                                  if biz_flow else ""))
                except Exception as exc:                     # noqa: BLE001
                    _wres = {"ok": False,
                             "detail": f"{type(exc).__name__}: {exc}"[:200]}
                if _wres.get("ok") and (_wres.get("written") or _wres.get("deleted")):
                    # 整仓大改的**两条必做善后**（2026-11-15）：
                    # ① 重接一次线：远端可能改了 urls.py 的作者区 / 新增了端点，
                    #    而接线是引擎的机械活（零 token）。
                    # ② 清空"已观测通过"集合 → 下一轮**全量重测**。
                    #    增量观测（只测还没通过的）在整仓模式下是致命盲区：
                    #    整仓会一口气改十几个文件，把原先绿的模块改坏，
                    #    而它不在 _todo 里 → 永远发现不了 → 上一轮"变好"是假的。
                    if bp is not None and bp.caps:
                        try:
                            from . import openhouse as _oh_w
                            _oh_w.wire(ws, bp, emit=emit, route_funcs=_route_funcs)
                        except Exception as exc:             # noqa: BLE001
                            if emit:
                                emit("notice", {"message": "整仓后接线跳过：%s: %s"
                                                           % (type(exc).__name__, exc)})
                    cap_done.clear()
                    if emit:
                        emit("whole_round", {
                            "round": rnd,
                            "written": _wres.get("written"),
                            "deleted": _wres.get("deleted"),
                            "skipped": len(_wres.get("skipped") or []),
                            "detail": str(_wres.get("detail") or "")[:200],
                            "rootcause": str(_wres.get("rootcause") or "")[:600],
                            "note": "整仓会话：远端在整仓权限下改完，下一轮重新观测判红"})
                    # 注意 `done` 是**逐单施工**的计数器，在本分支之后才初始化（第 1489 行），
                    # 这里不能引用它 —— 引用过一次，整仓成功的第一轮就 NameError，
                    # 被整轮 except 吞成 round_error，连撞 3 轮就"异常收工"。
                    # 整仓轮没有"逐单完成数"，记 0（真实进展看下一轮 verified）。
                    hist.append({"round": rnd, "verified": verified,
                                 "orders": len(orders), "done": 0,
                                 "strategy": "whole-repo"})
                    whole_rounds += 1
                    continue
                if emit:
                    emit("whole_nochange", {
                        "round": rnd,
                        "done": _wres.get("done"),
                        "detail": str(_wres.get("detail") or "")[:200],
                        "note": ("整仓会话没有落地改动（远端自认完成/被守卫回滚）→ "
                                 "回落到逐单派工")})

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
                                      budget_left=None,
                                      dec_used=dec_used, write_used=write_used,
                                      write_per_tier=write_per_tier,
                                      requirement=requirement, start_tier=_cst,
                                      effort_mode=str(getattr(pol, "effort_mode", "ladder")),
                                      cap_red=cap_red, cap_detail=cap_detail,
                                      cap_endpoints=cap_endpoints,
                                      cap_recent=cap_recent, cap_round=rnd,
                                      # 上一轮的业务流结果（第一次是 None）：
                                      # 业务流没过 → 这一轮优先补它缺的角色模块（§20.4 落点 6）
                                      biz_flow=biz_flow)
                cap_recent = {str(m.get("path") or "") for m in (stats.get("modules") or [])}
                remote_code_used += sum(1 for m in stats["modules"] if m.get("ok"))
                if stats.get("isolate_degraded"):
                    iso_degraded.extend(stats["isolate_degraded"])
                # 2026-11-09：远端**配置问题**（401/402/403）→ 立刻终止整夜。
                # 理由：换 key / 充值 / 改端点才能解决，重试与继续跑都没有意义，
                # 而继续跑只会被记成"模型不会写"或"检测到振荡"，把一个配置问题
                # 变成一场 puzzling 的失败。这里 break 就在 `for rnd` 里，语义正确。
                if stats.get("remote_fatal"):
                    reason = f"**远端配置问题，已立即收工**：{stats['remote_fatal']}"
                    need_human = reason   # 换 key/充值才能解决，重试没有意义
                    break
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
                # ---- 接线（写码后立刻把新路由挂上）----
                # 2026-11-10：造物模式以前**从不接线** —— 远端把处理函数写对了、
                # 契约判据全过，但路由没挂到 app 上 → 端到端 404 → 交付物不能用。
                # 实测 `web_routes.json` 停在 2026-11-09 16:34，之后 4 轮甲方夜班
                # 写出的新模块路由**一个都没接上**。接线是纯机械动作（零 token）。
                if bp is not None and bp.caps:
                    try:
                        from . import openhouse as _oh
                        _wired = _oh.wire(ws, bp, emit=emit, route_funcs=_route_funcs)
                        if _wired:
                            cap_done.clear()          # 路由变了 → 能力要重测
                    except Exception as exc:         # noqa: BLE001
                        if emit:
                            emit("notice", {"message": f"接线跳过：{type(exc).__name__}"})
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
                # ── 收工判定：**依据是业务事实，不是记账**（2026-11-11 治本）──────
                # 记账（stats["modules"]）是实现细节；把它当"有没有活干"的判据，
                # 任何一条记账漏了都会让引擎误收工（实测 10:16 / 10:58 两轮）。
                # 业务事实 = ①端到端红灯还剩多少 ②契约层还有没有欠活的模块。
                _red_left = 0
                if cap_cov:
                    _red_left = len([c for c in (cap_cov.get("caps") or {}).values()
                                     if str((c or {}).get("status") or "") != "done"])
                _contract_work = False
                try:
                    _contract_work = any(needs_work_state(state, m) for m in contracts)
                except Exception:                            # noqa: BLE001
                    _contract_work = False
                if not stats["modules"] and (_red_left or _contract_work):
                    # 记账丢了 ≠ 没活干 → **不许收工**，换打法继续，并把异常报出来。
                    if emit:
                        emit("accounting_lost", {
                            "round": rnd, "cap_red": _red_left,
                            "contract_work": bool(_contract_work),
                            "note": "本轮记账为空但仍有活干（端到端红灯 %d 项 / 契约欠活 %s）"
                                    "→ 不收工，换打法继续"
                                    % (_red_left, "是" if _contract_work else "否")})
                    if strategy_i + 1 < len(strategies):
                        strategy_i += 1
                        strat = strategies[strategy_i]
                    stall = 0
                    continue
                if not stats["modules"]:
                    reason = no_work_reason(state, contracts)
                    # 2026-11-11：收工理由必须**自证**。
                    # 实测 10:16 那轮：因为补丁路径的记账 IndexError 被外层 except 吞掉，
                    # `stats["modules"]` 是空的 → 这里判成「没有可执行的工作」→ 收工，
                    # 而**这一轮其实写了东西**（author_start / patch_tools 都在日志里）。
                    # 收工理由不能只说「没活干」，得说清这一轮到底干了什么。
                    _wrote = [m for m in (stats.get("modules") or []) if m.get("ok")]
                    if _wrote:
                        reason = (f"本轮写了 {len(_wrote)} 个模块但记账为空（引擎记账异常）：{reason}")
                    # 2026-11-10：契约全过 ≠ 写完了。
                    # 如果还有端到端红灯能力，必须**如实写进收工理由** ——
                    # 以前一律写"全部通过验收"，交付物却有一半端点 404。
                    if cap_red:
                        _red_n = len([c for c in (cap_cov.get("caps") or {}).values()
                                      if str((c or {}).get("status") or "") != "done"])
                        reason = (f"模块契约已全过，但**端到端还有 {_red_n} 项能力未通过**"
                                  f"（涉及模块 {', '.join(sorted(cap_red)[:5])}）：{reason}")
                    # 2026-11-11（§20.4 落点 6）：**业务流没过也算"没做完"**，
                    # 收工理由必须写明 —— 否则"能力全绿"会被读成"系统可用"，
                    # 而实测 business_flow passed=0 时系统主流程一条都不通。
                    if biz_flow and biz_flow.get("ok") is False:
                        _bf_pass = int(biz_flow.get("passed") or 0)
                        _bf_tot = int(biz_flow.get("total") or 0)
                        _bf_why = str(biz_flow.get("reason") or "")[:120]
                        reason = (f"**业务流未通过**（{_bf_pass}/{_bf_tot} 条业务不变量"
                                  f"{'：' + _bf_why if _bf_why else ''}）：{reason}")
                    # §20.4 落点 4：契约声明的产物文件还没齐 → 不许说"做完了"。
                    # 语义与 needs_work 一致（不回滚已写好的代码，只是不许收工）：
                    # 回滚会惩罚无关的正确工作；"收不了工"才是对的口径。
                    try:
                        _arts_all = [a for _c in contracts.values()
                                     for a in (getattr(_c, "artifacts", None) or [])]
                        _miss_arts = (contract_mod.missing_artifacts(ws, _arts_all)
                                      if _arts_all else [])
                    except Exception:                        # noqa: BLE001
                        _miss_arts = []
                    if _miss_arts:
                        _names = "、".join(str(x.get("path")) for x in _miss_arts[:4])
                        reason = (f"**产物文件未产出**（{_names}"
                                  f"{' 等' if len(_miss_arts) > 4 else ''}）：{reason}")
                    # §20.6 C 档：工程声明（迁移/锁文件/编排/健康端点/可观测性）未兑现 →
                    # 不许只说"能力没通过"，收工理由必须逐条点名（用 enterprise.summary）。
                    _eng_res = {"ok": True, "fails": []}
                    try:
                        _eng_text = "\n".join(dict.fromkeys(
                            str(getattr(_c, "full_text", "") or getattr(_c, "raw", "") or "")
                            for _c in contracts.values()))
                        _eng_specs = enterprise.parse_sections(_eng_text)
                        if _eng_specs:
                            _eng_res = enterprise.run(ws, _eng_specs)
                        _eng_why = enterprise.summary(_eng_res)
                    except Exception:                        # noqa: BLE001
                        _eng_why = ""
                    if _eng_why:
                        reason = f"{_eng_why}：{reason}"
                    if emit:
                        emit("stop", {"round": rnd, "reason": reason, "verified": verified,
                                      "cap_red": len(cap_red),
                                      "artifacts_missing": [x.get("path") for x in _miss_arts[:6]],
                                      "engineering_missing": [x.get("path") for x in
                                                              (_eng_res.get("fails") or [])[:6]],
                                      "biz_flow": (biz_flow or {}).get("ok")})
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
                # 这一单用到的**远端思考档**（空 = 本地档跑的，不进写作台账：
                # 本地档不是远端思考档，记进去会污染"各档成功率"和 proven_floor）。
                _ord_effort = ""
                try:
                    if (o.get("kind") or "") == "add_test":
                        # add_test 不走模型：断言由契约渲染（模型写的弱测试是实测过的坑）
                        r = iterate.add_engine_tests(ws, contracts, o.get("target") or "")
                    elif use_remote and _remote_available() and \
                            min(tier_of.get(okey(o), 0), max(0, len(steps) - 1)) > 0:
                        # 阶梯第 0 档是**本地模型**（0 token 的手脚档，省 token）。
                        # 只有本地连续失败到阈值，才升到远端主力：flash:none → … → pro:max。
                        lvl = min(tier_of.get(okey(o), 0), max(0, len(steps) - 1))
                        step = steps[lvl]
                        _ord_effort = str(step.get("effort") or "none")
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
                # 记一条**工作单级**尝试（只记远端档；kind="order"）。
                # 为什么单列一种 kind：工作单成功只说明"这个函数在这档能写对"，
                # 推不出"整个模块在这档能写对" → 不能进 proven_floor（模块级证据）。
                # 2026-11-10 之前这条路径**完全不记账**，算出来的成功率是残缺的。
                if _ord_effort:
                    try:
                        from . import calibration as _cal_ord
                        _cal_ord.record_attempt(ws, path=str(o.get("target") or ""),
                                                effort=_ord_effort,
                                                ok=bool(r.get("ok") and improved),
                                                kind="order")
                    except Exception:                # noqa: BLE001
                        pass
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
                    # 什么时候升级**由模式决定**（core/policy.py）：造物模式下一旦本地被硬判据
                    # 判红就升级（不再等满 2 次，本地小模型做不出来的东西多试只是烧时间）；
                    # 精修模式仍按"连续失败 2 次 + 人工批准"走。
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
                            # 救援那次远端调用也记一条（execute_order_remote 默认 step_index=0）。
                            try:
                                from . import calibration as _cal_ord
                                _cal_ord.record_attempt(
                                    ws, path=str(o.get("target") or ""),
                                    effort=str((steps[0] if steps else {}).get("effort") or "none"),
                                    ok=bool(rr.get("ok")), kind="order")
                            except Exception:            # noqa: BLE001
                                pass
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
    # 关键节点守卫：精修模式在"标记可交付"前要人点头（造物模式自动放行）
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
                              scope={**(scope or {}), "task_id": task_id, "rounds": rnd,
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
                      spent_s=round(time.time() - t0, 1), need_human=need_human)
    # 日志固定写到运行期数据目录的 _logs/（开发态=仓库根，安装版=%LOCALAPPDATA%\SixCoder），
    # 不跟随 cwd（否则从不同目录启动会散成好几处，实测踩过）
    from . import paths as _paths
    logs = _paths.logs_dir()
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
      · **契约声明的产物文件还没产出/不达要点**（§20.4：模板/静态资源是交付物，
        缺了就是没做完 —— 否则"函数全绿、页面文件不存在"也会被当成已完工）
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
            or bool(st.get("artifacts_missing"))
            or bool(st.get("engineering_missing"))
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

    `effort_mode="auto"`（造物模式）→ 规格/方案走最深、代码按难度路由（见 _auto_effort）；
    `"ladder"`（精修模式）→ 第 0 档（最省）；
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


def _done_cap_modules(ws: Path) -> dict[str, set[str]]:
    """能力 → 它落在哪些模块上（只取**已 done** 的）。"""
    out: dict[str, set[str]] = {}
    try:
        from . import blueprint as _bpm
        bp = _bpm.load(ws)
        for c in (getattr(bp, "caps", []) or []):
            if str(getattr(c, "status", "")) != "done":
                continue
            mods = {str(m) for m in (getattr(c, "modules", []) or [])}
            if mods:
                out[str(c.id)] = mods
    except Exception:                                    # noqa: BLE001
        return {}
    return out


# 横切文件：改动它们会"顺带"影响一大片端点（共享 router / schema / 鉴权 / 装配）。
# 2026-11-10 20:00 实测：只查"直接归属该文件的能力"，漏掉 4 条（全在 app/api/topics.py）。
_SHARED_FILE_HINTS = ("/api/", "models.py", "schemas.py", "__init__.py",
                      "main.py", "deps.py", "auth.py", "db.py", "database.py")


def _is_shared_file(rel: str) -> bool:
    """是不是**横切文件**（改动会顺带影响一大片端点）。"""
    r = str(rel).replace("\\", "/").lower()
    return any(h in r for h in _SHARED_FILE_HINTS)


def _guard_ids_for(rel: str, deps: dict, done_mods: dict[str, set[str]]) -> set[str]:
    """写 `rel` 之前要量、写完要复验的**已 done 能力**集合。

    · **横切文件**（api 层 / models / schemas / __init__ / main / deps / auth / db）
      → 返回**全部**已 done 能力：这类文件一改就可能连带影响一大片端点，
      只按"直接归属"查是漏的（实测漏 4 条，白等一轮观测）。
    · **普通业务文件** → 只查**依赖邻域**（自身 + 它依赖的 + 依赖它的），
      避免每次写都跑几十个探针把一轮拖成两轮的时间。

    邻域取宽一点是有意的：漏拦一次 = 一次多花一整轮观测的钱。
    """
    r = str(rel).replace("\\", "/").lower()
    if any(h in r for h in _SHARED_FILE_HINTS):
        return set(done_mods)
    related = {rel} | set(deps.get(rel) or [])
    for p, ds in (deps or {}).items():
        if rel in (ds or []):
            related.add(p)
    picked = {cid for cid, mods in done_mods.items() if mods & related}
    # 2026-11-10 23:15：邻域算出来是**空集**时，绝不能"什么都不守"。
    # 实测代价：22:50 那次 28→27，日志里连 regression_blocked 都没有 ——
    # 因为这类文件（依赖图里没登记、能力表里也没直接归属）算出的守护集合是空的，
    # 提交前"复验 0 条、当然 0 条坏"，等于裸奔。宁可多跑几十个本地探针（零 token），
    # 也不能让一次写入悄悄写坏已通过的能力。
    return picked or set(done_mods)


def _cap_drive_candidates(cap_red, limit: int) -> list[str]:
    """契约全过时，哪些红模块该被"按能力派单"（纯函数，可单测）。

    只在**没有契约工作可干**时才用（调用方先判 `mods` 为空）；
    排序保证确定性：同一批红模块不会被随机挑走。

    **排序按文件所有权**（2026-11-11 治本，§11.4e）：
    业务层落点（`app/`、`app/services/`、`matching_core/`）**优先** ——
    那是远端拥有、可以写业务代码的地方；
    接线层落点（`app/api/`）**排后** —— 那是引擎拥有的文件
    （每次接线都整块重渲染），远端在里面写的东西会被冲掉，
    而且必然撞上「承载已通过能力的文件不许整文件重写」的护栏 → 死锁。
    旧口径是纯字典序（`app/api/...` 排在 `app/...` 前面）→ 56 条红灯里 52 条先派到接线层。
    判定实现与切片分组**共用** `openhouse.writable_modules`（不重复造逻辑）。
    """
    if not cap_red or limit <= 0:
        return []
    try:
        from . import openhouse as _oh
        _ordered = _oh.ordered_modules("", [str(p) for p in cap_red])
    except Exception:                                    # noqa: BLE001
        _ordered = sorted(str(p) for p in cap_red)
    return list(_ordered)[:int(limit)]


def _cap_orders_from_red(mods, have, allow, evidence) -> list[dict]:
    """端到端红灯落点 → 工作单（纯函数，可单测）。

    2026-11-17 从 night() 内联循环抽出。规则（单一实现，主路径真调用）：
      · 去重：`have` 里已有的模块不重复派；
      · 分诊闸门：`allow` 非 None 时，只有 triage 判 dispatch 的落点才派
        （local_wiring / env 交本地处理，不花钱给远端）；
      · **不再用契约窄表过滤**：契约只管命名/缓存，不是派单白名单。
        实测锁契约只覆盖 3 模块时旧规则把 19 条红灯滤到 1-2 条，
        且漏过的恰是护栏命门 → 净增长恒 0（2026-11-17 21:00）。
    `mods` 已是 actionable_modules（文件所有权判定）产物，无需契约授权。
    """
    have = set(have or ())
    allow = set(allow) if allow is not None else None
    evidence = evidence or {}
    out: list[dict] = []
    for m in sorted(str(x) for x in (mods or ())):
        if m in have:
            continue
        if allow is not None and m not in allow:
            continue
        ev = "；".join(evidence.get(m) or [])[:800]
        out.append({
            "kind": "implement",
            "target": m,
            "symbol": "",
            "acceptance": "端到端业务流通过（机械判据查不出这一层）",
            "detail": ("契约判据已全部通过，但**端到端验收不通过**。"
                       "请按下面的失败证据修（路由要真的挂到 app 的 include_router 上、"
                       "错误响应要带业务码、越权要真的返回 403）：\n" + ev),
        })
        have.add(m)
    return out


def _caps_done_by_file(ws: Path) -> dict[str, set[str]]:
    """模块 → 它承载的**已 done 能力**（写这个文件前先记下，写完必须复验）。

    为什么要这张表（2026-11-10 实测）：一次 `app/api/topics.py` 整文件重写，
    同时把 5 条已 done 能力（create_draft / submit_and_publish / offline /
    forbidden_role / api.not_found）的路由与鉴权一起改掉 —— 要等**下一轮全量观测**
    才发现，中间这一轮的改动已经落盘了（中午 3.5 小时里 26 次状态变化就是这么来的）。
    """
    out: dict[str, set[str]] = {}
    try:
        from . import blueprint as _bpm
        bp = _bpm.load(ws)
        for c in (getattr(bp, "caps", []) or []):
            if str(getattr(c, "status", "")) != "done":
                continue
            for m in (getattr(c, "modules", []) or []):
                out.setdefault(str(m), set()).add(str(c.id))
    except Exception:                                    # noqa: BLE001
        return {}
    return out


def _probe_caps_done(ws: Path, ids: set[str]) -> set[str] | None:
    """跑**指定能力**的端到端探针，返回其中现在仍然 done 的 id 集合。

    返回 `None` = 这次复验自己跑不动（引擎/环境问题）→ 调用方**不许据此回滚**
    （把"引擎跑不动"当成"产物被写坏"，就是 11-08 那种把机制问题记成模型能力问题的坑）。
    """
    if not ids:
        return set()
    try:
        from . import blueprint as _bpm, openhouse as _oh
        bp = _bpm.load(ws)
        if bp is None:
            return None
        cov = _oh.observe(ws, bp, emit=None, only=set(ids))
        caps = (cov or {}).get("caps") or {}
        return {cid for cid in ids
                if str((caps.get(cid) or {}).get("status") or "") == "done"}
    except Exception:                                    # noqa: BLE001
        return None


def _pick_batch(pool: list[str], round_no: int, limit: int) -> list[str]:
    """从候选池里按轮号**轮转**取一批（池已按依赖序）。取出的这批仍按池序返回。

    为什么不能"整表旋转 + 依赖修正"（2026-11-10 21:55 实测卡死 50 分钟、¥0.82）：
      切片里 8 个模块之间几乎是**全序**（models → services/people → services/quota
      → api/people → api/topics → errors → schemas → services/topics）。
      "整表旋转"后接"依赖修正"，修正会把旋转**全部还原**成同一个规范序，
      于是每轮永远取前 3 个 —— 而前 3 个恰好是契约早已满分的
      （services/people 4/4、services/quota 5/5、models 2/2），
      真正欠 9 个函数的 errors / schemas / services/topics **7 轮一次没写过**，
      `verified` 卡在 13/22 不动。日志证据：21 次 author_start 只落在 4 个模块上。

    正确做法：
      1. **在池子里轮转取**（保证公平，任何模块最多等 ceil(n/limit) 轮）；
      2. 取出来的这批**再按池序排**（保证批内下游不先于上游）。
    纯函数，无副作用，自检直接调它。
    """
    n = len(pool)
    if n == 0 or limit <= 0:
        return []
    limit = min(int(limit), n)
    k = (max(0, int(round_no)) * limit) % n
    picked = (pool[k:] + pool[:k])[:limit]
    pos = {p: i for i, p in enumerate(pool)}
    return sorted(picked, key=lambda p: pos.get(p, 0))


def _dep_respecting_order(order: list[str], contracts: dict) -> list[str]:
    """把优先级顺序修正成"依赖必在前"（贪心稳定拓扑修正）。

    为什么需要（2026-11-10）：轮号旋转和"红灯优先"都会**改写**依赖序 ——
    转一下表，ui 层就跑到 data 层前面去了（`order_modules` 明明排好了 data→ui）。
    上层先写 → 判据缺依赖 → 红 → 重写，这正是中午震荡的一部分来源。

    原则：**优先级照旧起作用，但不许插队到自己的依赖前面**。
    依赖都已就位的模块里，谁在 `order` 里靠前谁先出（所以红灯优先、旋转公平性都保留）。
    """
    if not order:
        return []
    try:
        from . import project as _proj_dep
        deps = (_proj_dep.build_map(contracts) or {}).get("deps") or {}
    except Exception:                                    # noqa: BLE001
        return list(order)
    scope = set(order)
    remaining = list(order)
    placed: set[str] = set()
    out: list[str] = []
    guard = 0
    while remaining and guard < 2000:
        guard += 1
        for i, p in enumerate(remaining):
            need = [d for d in (deps.get(p) or []) if d in scope]
            if all(d in placed for d in need):
                out.append(p)
                placed.add(p)
                remaining.pop(i)
                break
        else:
            out.extend(remaining)                        # 有环：剩下的保持原相对顺序
            break
    return out


def _author_round(ws: Path, contracts: dict, state: dict, plan: str, steps: list[dict],
                  tier_of: dict, tier_fail: dict, escalate_after: int, task_id: str,
                  emit=None, local_model: str = "", max_modules: int = 3,
                  budget_left: float = 600.0, dec_used: dict | None = None,
                  write_used: dict | None = None, write_per_tier: int = 0,
                  requirement: str = "", start_tier: int = 0,
                  effort_mode: str = "ladder",
                  oracle_specs: dict | None = None,
                  cap_red: set | None = None,
                  cap_detail: dict | None = None,
                  cap_endpoints: dict | None = None,
                  cap_recent: set | None = None,
                  cap_round: int = 0,
                  biz_flow: dict | None = None) -> dict:
    """**新路径（唯一）**：按模块整文件生成 → 本地全判据验收 → 窄证据重试 → 失败升档。

    旧路径（逐符号本地写 + 远端救单）已废弃：它把最弱的一环放在写代码的位置上，
    再用远端反复返工，既慢又贵。整文件路径把"写"整体交给远端，本地只做机械验证。
    """
    from . import author as _author
    from . import scale as _scale_mod      # 分级参数（阈值/档位/上限）
    import os as _osr                     # 环境变量覆盖（round/隔离后端）

    # 本片时间截止点：budget_left 是进入这一轮时剩余的秒数。
    # 必须一路传到作者自愈循环内部，否则 4 次自愈足以把 --slice-hours 拖穿。
    _deadline = (float("inf") if budget_left is None
                 else time.time() + max(0.0, float(budget_left)))

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
        # 2026-11-10：**端到端红灯也算"要干活"**。
        # 契约判据只验**函数**，不验**路由/接线/权限** —— 远端把处理函数写对了、
        # 判据全过，但路由没挂上去（实测 404），造物模式却认为"这个模块做完了"。
        # 能力表里每条能力都带 `modules`，说清它落在哪些文件上，
        # 于是"端到端红了"能直接变成工作单（这是契约测不出的那半边）。
        if cap_red and path in cap_red:
            return True
        return needs_work_state(state, path)

    def _verify_module(path: str) -> dict:
        """行级补丁打完后的复验：只验这一个模块，判据全过才算过。

        以前这里写的是 `_verify(...)`，而那个函数在旧路径删掉时一起没了 ——
        行级补丁这条路一旦真用上就 NameError（用静态未定义名字检查抓到的）。
        """
        try:
            st = iterate.inspect(ws, {path: contracts.get(path)},
                                 specs=(oracle_specs or {}))
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
    # 2026-11-10：**端到端红灯的模块排前面 + 轮转**。
    # 原来按依赖序取前 N 个 → 实测 6 轮都在写同样 3 个模块（llm/preferences/metrics，
    # 它们在依赖序里最靠前）→ 剩下 10 个红灯模块**永远排不上**，
    # 修不好那 3 个就一直死磕（最后被振荡检测收工）。
    # 只排序不够（每轮还是靠前那几个）→ 要把**上一轮处理过的排到后面**，
    # 让 13 个红灯模块一轮一批轮转；修好的会退出红灯集，自然收敛。
    _cap_first = set(cap_red or set())
    _cap_seen = set(cap_recent or set())
    if _cap_first:
        _ordered = ([p for p in _ordered if p in _cap_first and p not in _cap_seen]
                    + [p for p in _ordered if p in _cap_first and p in _cap_seen]
                    + [p for p in _ordered if p not in _cap_first])
    # **按轮号旋转起点**：只记上一轮处理过会在两批之间来回跳
    # （实测：llm/preferences/metrics ↔ authz/adjustment/appeal 交替，
    #   analytics/audit/topics 这几个红灯模块**从没被处理过**）。
    # 轮号旋转保证 13 个红灯模块均匀覆盖，一轮 max_modules 个。
    if _ordered and int(cap_round or 0) > 0 and max_modules > 0:
        _k = (int(cap_round) * int(max_modules)) % len(_ordered)
        _ordered = _ordered[_k:] + _ordered[:_k]
    # 2026-11-10：旋转/红灯优先只是**优先级**，不许把它们变成"依赖倒序"。
    # 这一道修正保证"被依赖的模块一定排在自己的下游前面"，同时保留上面的优先级与公平性。
    _before_fix = list(_ordered)
    _ordered = _dep_respecting_order(_ordered, contracts)
    if emit and _ordered != _before_fix:
        _moved = [p for i, p in enumerate(_ordered) if i < len(_before_fix)
                  and p != _before_fix[i]]
        emit("order_fixed", {"note": "按依赖修正了片内模块顺序（不许下游先于上游）",
                             "head": _moved[:4]})
    # ── 派单（2026-11-10 22:00 修正：轮转取批 + 契约欠活优先）──────────────────
    # 旧写法 `[p for p in _ordered if needs_work(p)][:max_modules]` 有两个洞：
    #   ① 依赖序几乎是全序 → 永远取到前 3 个（which 往往契约已满分）→ 真正欠函数的模块饿死；
    #   ② `needs_work` 把"端到端红"也算要干活，于是契约满分的模块和欠函数的模块混在一锅，
    #      按顺序取，欠函数的永远排不上。
    # 现在：先把**契约层欠活**的挑出来（只有写它们 verified 才会涨），池内轮转取批，
    # 批内仍按依赖序。契约层全绿时退回"红灯模块池"轮转（等价于原来的红灯优先 + 公平）。
    _cand_all = [p for p in _ordered if needs_work(p)]
    _short = [p for p in _cand_all if needs_work_state(state, p)]
    _pool = _short or _cand_all
    # ── 核心优先派单（2026-11-11 §20.4 落点 5）：把 priority.rank 接进来 ─────────
    # 口径（priority.py 已写好并有 29 条自检，这里只负责"接线"）：
    #   核心（承载被别的能力依赖的模块）→ 端到端红 → 角色（地基先于门面）
    #   → 历史成功率；**连续多轮没被排到的保底提权**（计数落盘，跨重启有效）。
    # 为什么必须"依赖已满足才开写"：上层先写 → 判据缺依赖 → 红 → 重写（震荡）。
    # 为什么还留 `_pick_batch` 兜底：priority 算不出来（没蓝图等）时退回旧口径，不至于空转。
    _prio_rows: list[dict] = []
    _mdeps: dict[str, list[str]] = {}
    try:
        from . import priority as _prio
        _prio_rows = _prio.rank(ws, contracts, cap_red=cap_red,
                                starved=_prio.starved_map(ws))
        _mdeps = _prio.module_deps(contracts)
    except Exception:                                    # noqa: BLE001
        _prio_rows = []

    def _deps_ready(p: str) -> bool:
        """这个模块的依赖是不是都验收完了（没验收完就别先写它）。"""
        for d in (_mdeps.get(p) or []):
            if d == p or d not in contracts:
                continue
            _st = state.get(d) or {}
            if int(_st.get("verified") or 0) < int(_st.get("funcs") or 0):
                return False
        return True

    # 业务流优先（§20.4 落点 6）：业务流没过 → 它涉及的角色模块提到最前。
    # 口径偏粗是**故意的**：业务不变量是跨角色的组合断言，失败时说不清是哪个角色坏
    # （细节在 detail 里），所以宁可多提权几个相关模块，也不要在"看起来无关"的模块上耗。
    _biz_mods: set[str] = set()
    if biz_flow and biz_flow.get("ok") is False:
        try:
            from . import flow as _flow_b
            _roles = _flow_b._role_map(contracts)
            # 2026-11-12 修复：`_role_map` 返回的是**候选列表** `[(模块名, 函数名), ...]`
            # （§11.4n 改成多条候选后，这里没跟着改）——
            # 原写法 `str(v[0])` 把**元组整个字符串化**成
            # `"('matching_core.da', 'student_proposing_da')"`，
            # 再拿去跟 `Path(_rel).stem`（`"da"`）比 → **永远匹配不上** → 提权集合恒为空。
            #
            # 只取每个角色的**首候选**（引擎认定的最优承载模块）：
            # 全取的话，参数名语义过滤会把**几乎所有模块**都算成候选
            # （实测 16 个模块命中 15 个）→ 等于没提权，失去"优先补角色模块"的意义。
            _mods_hit: set[str] = set()
            for _v in (_roles or {}).values():
                if not _v:
                    continue
                _first = _v[0]
                _m = _first[0] if isinstance(_first, (tuple, list)) and _first else _first
                if _m:
                    _mods_hit.add(str(_m))
            for _rel in contracts:
                _rel_n = str(_rel).replace("\\", "/")
                if _rel_n.endswith(".py"):
                    _rel_n = _rel_n[:-3]
                _rel_dots = ".".join(p for p in _rel_n.split("/") if p)
                _stem = Path(_rel).stem
                for _m in _mods_hit:
                    # 模块名逐段相等（matching_core.da == matching_core/da.py）
                    # 或末段相等（da == matching_core/da.py，老口径兼容）
                    if _m == _rel_dots or _m.rsplit(".", 1)[-1] == _stem:
                        _biz_mods.add(_rel)
                        break
        except Exception:                                # noqa: BLE001
            _biz_mods = set()

    if _prio_rows:
        _pmap = {str(r.get("path")): r for r in _prio_rows}
        _ready = [p for p in _pool if _deps_ready(p)]
        _ready = _ready or _pool                       # 全被依赖挡住 → 退回原池（宁可写坏也不空转）
        _oidx = {p: i for i, p in enumerate(_ordered)}

        def _pk(p: str) -> tuple:
            return (0 if p in _biz_mods else 1,
                    int((_pmap.get(p) or {}).get("rank") or 9999),
                    _oidx.get(p, 9999))

        mods = sorted(_ready, key=_pk)[:max_modules]
        if emit:
            emit("priority_pick", {
                "mods": mods[:6], "n": len(mods),
                "biz_first": sorted(_biz_mods)[:4],
                "reasons": [{"path": p, "why": str((_pmap.get(p) or {}).get("reason") or "")}
                            for p in mods[:4]],
                "why": "核心优先派单（被依赖能力多的模块先做；"
                       "业务流没通过时它涉及的角色模块置顶；"
                       "连续多轮没被排到的保底提权）"})
    else:
        mods = _pick_batch(_pool, cap_round, max_modules)
    if emit and _short:
        emit("batch_pick", {"short": len(_short), "pool": len(_pool),
                            "mods": mods[:4], "round": int(cap_round or 0),
                            "why": "契约层欠活的模块优先派单（否则 verified 永远不涨）"})
    # ── 按能力派单（B）：**契约全过但端到端还红**的结构性死结 ──────────────────
    # 实测 2026-11-10 20:47：契约 35/35 全绿、`needs_work` 一个不剩，但 28/84 条能力还红着
    # —— 那些红能力所在的模块**根本不在契约里**（app/analytics.py、app/api/adjustments.py…），
    # 于是引擎"没有可执行的工作"收工，重启再来一遍，纯空转。
    # 现在：这时就把红模块直接派单，交给远端按**端点**写，验收**只认端到端探针**。
    _cap_only: set[str] = set()
    if not mods and cap_red:
        _cand = _cap_drive_candidates(cap_red, max_modules)
        if _cand:
            _cap_only = set(_cand)
            mods = _cand
            if emit:
                emit("cap_drive", {"modules": _cand[:6], "n": len(_cand),
                                   "note": "契约全过但端到端还红 → 按端点直接派单，"
                                           "验收只认端到端探针"})
    if not mods:
        return {"modules": [], "ok": True}

    stats = {"modules": [], "ok": True, "system_issues": []}
    # 每个模块承载着哪些**已 done 能力**：写它之前量一次基线，写完复验，
    # 谁的 done 掉了 = 这次改动写坏的 → 当场回滚（不等下一轮全量观测）。
    _done_by_file = _caps_done_by_file(ws)
    # 跨文件破坏是真的：写 auth.py 能搞坏 topics.py 的端点。所以守护范围按**依赖邻域**算，
    # 而不是只看"直接归属这个文件的能力"（2026-11-10 20:00 因此漏掉 4 条）。
    _done_caps_mods = _done_cap_modules(ws)
    try:
        from . import project as _proj_dep0
        _deps_map = (_proj_dep0.build_map(contracts) or {}).get("deps") or {}
    except Exception:                                    # noqa: BLE001
        _deps_map = {}
    if emit and _done_by_file:
        emit("regress_guard", {"files": len(_done_by_file),
                               "caps": len(_done_caps_mods),
                               "note": "承载已 done 能力的模块：写完立刻按依赖邻域定向复验，坏了当场回滚"})
    # 断点续跑：开跑前先把"哪些已完成/可续写/全新"算出来并留痕（进程重启后照样能接着写）
    try:
        from . import project as _proj_r
        _plan = _proj_r.resume_plan(ws, contracts, state, red_modules=cap_red)
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

    # 并行接线（开发文档 P0-2）：写模块循环经 parallel.map_calls 走。
    # 默认 1 工人 = 串行，行为与老代码**完全一致**（map_calls 串行分支按输入序逐项跑）；
    # SIXCODER_PARALLEL=N 才真正并行 —— 429 与"读到中间态"风险见 core/parallel.py 顶部，
    # 默认不承担。`_stopped` 保留旧 `break` 语义：预算/截止一到，后续模块一律跳过、不进 stats。
    from . import parallel as _par_mod
    _stopped = {"flag": False}

    # ── 按能力派单（B）的实现：没有契约的模块，按端点写，验收只认探针 ──────────
    _bp_cap = None
    if _cap_only:
        try:
            from . import blueprint as _bp_cd
            _bp_cap = _bp_cd.load(ws)
        except Exception:                                # noqa: BLE001
            _bp_cap = None

    def _caps_of(rel: str) -> set[str]:
        """这个模块承载**全部**能力（不只 done 的）——端到端验收的对象。"""
        out: set[str] = set()
        for c in (getattr(_bp_cap, "caps", []) or []):
            if rel in (getattr(c, "modules", []) or []):
                out.add(str(c.id))
        return out

    def _cap_probe_verdict(_ws, rel: str) -> dict:
        """给 `author_module(verify_fn=...)` 用的验收钩子：**只认端到端探针**。"""
        ids = _caps_of(rel)
        if not ids:
            return {"ok": False, "feedback": "该模块没有任何端到端能力可判（不当作通过）"}
        got = _probe_caps_done(_ws, ids)
        if got is None:
            return {"ok": False, "feedback": "端到端探针跑不动（引擎问题，不是产物错）"}
        bad = sorted(ids - got)
        if not bad:
            return {"ok": True, "feedback": f"{len(ids)} 条端到端能力全部通过"}
        return {"ok": False, "feedback": "端到端仍未通过：" + "、".join(bad[:4])}

    def _write_cap_driven(rel: str) -> dict:
        """按端点写一个**没有契约**的模块：验收 = 端到端探针（提交前复验）。"""
        ids = _caps_of(rel)
        detail = "；".join((cap_detail or {}).get(rel) or [])[:1200]
        eps = ""
        try:
            from . import openhouse as _oh2
            if _bp_cap is not None:
                eps = str(_oh2.endpoints_for_module(_bp_cap, ws, rel) or "")
        except Exception:                                # noqa: BLE001
            eps = ""
        evidence = (
            "【本模块没有契约函数，验收**只看端到端能力**】\n"
            f"要修的能力：{', '.join(sorted(ids)) or '(未知)'}\n"
            + (f"本模块应负责的 HTTP 端点：{eps}\n" if eps else "")
            + (f"失败证据：{detail}\n" if detail else "")
            + "要求：路由要真的挂到 app（app.include_router 或 @router 装饰器），"
              "状态码与业务字段按证据为准；**不要动与本文件无关的模块**。"
        )
        sand = None
        try:
            from . import isolate as _iso2
            sand = _iso2.open_attempt(ws, rel,
                                      int(_osr.environ.get("SIXCODER_ROUND", "0") or 0))
            if emit:
                emit("isolate_begin", sand.to_dict())
        except Exception:                                # noqa: BLE001
            sand = None
        r: dict = {}
        try:
            from . import author as _author_cd
            r = _author_cd.author_module(ws, rel, None, contracts, model_plan=plan,
                                         evidence=evidence, step_index=start_tier,
                                         task_id=(task_id or "cap-drive"), emit=emit,
                                         attempts=2, verify_fn=_cap_probe_verdict)
        except Exception as exc:                         # noqa: BLE001
            r = {"ok": False, "feedback": f"{type(exc).__name__}: {exc}"}
        ok = bool(r.get("ok"))
        if sand is not None:
            try:
                if ok:
                    sand.commit()
                else:
                    sand.rollback()
                if emit:
                    emit("isolate_commit" if ok else "isolate_rollback", sand.to_dict())
            except Exception:                            # noqa: BLE001
                pass
        if emit:
            emit("cap_drive_done", {"path": rel, "ok": ok, "caps": sorted(ids)[:6],
                                    "feedback": str(r.get("feedback") or "")[:160]})
        stats["modules"].append({"path": rel, "ok": ok, "tier": start_tier,
                                 "by": "cap-drive", "feedback": str(r.get("feedback") or ""),
                                 "applied": [rel]})
        if not ok:
            stats["ok"] = False
        return {"ok": ok, "feedback": str(r.get("feedback") or "")}

    def _write_one(rel, _net_try=0):
        """写码入口 —— **记账的唯一出口**（2026-11-11 架构修复）。

        为什么要有这一层（治本，不是打补丁）：
        原来的写码逻辑是个千行嵌套函数，有 **7 个 `return` 出口**，
        而"这次写了什么"的记账（`stats["modules"].append`）**散落在业务逻辑里**：
        受保护区跳过、模块预算用完、写次数到顶、正常写完、补丁成功…
        每加一条新路径就得记得再写一次 append —— **漏一次的后果不是少一条日志，
        而是整个引擎把"还有 56 项端到端红灯"读成"没有活干"**，
        在项目明明没做完的时候**收工**（实测 2026-11-10 16:00→11-11 01:32 那 8.5 小时，
        净增 0 条、状态倒退 4 次；10:16 与 10:58 两轮都写了同一句
        「模块契约已全过，但端到端还有 56 项能力未通过：没有可执行的工作」）。

        所以把结构改成：**业务逻辑只管返回结果，记账只有这一个出口**。
        内层 `_write_one_body` 里已有的 append 保留（它们记的是**详细**结果：
        失败原因、档位、回滚信息）；出口层只负责保证
        **"派过工的模块一定在账上"** —— 漏了就在这里兜底补一条，
        并发 `accounting_lost` 报警（可观测，不静默）。

        这样"记账会不会漏"从**靠人记得**变成**由结构保证**：
        以后新增任何 return 分支都不可能再让账空掉。
        """
        if rel in _cap_only:
            return _write_cap_driven(rel)
        _n_before = len(stats["modules"])
        try:
            return _write_one_body(rel, _net_try)
        finally:
            if len(stats["modules"]) == _n_before:
                stats["modules"].append({
                    "path": rel, "ok": False, "tier": 0, "by": "-",
                    "reason": "写码路径没有留下结果（引擎内部异常，已由唯一记账出口兜底）",
                    "applied": []})
                if emit:
                    emit("accounting_lost", {
                        "path": rel,
                        "note": "写码结束但没有记账 → 唯一记账出口已兜底（引擎内部异常）"})

    def _write_one_body(rel, _net_try=0):
        if rel in _cap_only:
            return _write_cap_driven(rel)
        if _stopped["flag"] or (budget_left is not None and budget_left <= 20) or time.time() >= _deadline - 5:
            _stopped["flag"] = True
            return {"skipped": True}
        hit = _scale_mod.protected_hits(ws, [rel])
        if hit and zone_mode == "block":
            stats["modules"].append({"path": rel, "ok": False, "tier": 0, "by": "-",
                                     "reason": "受保护区：这类文件不允许自动改（L 级以上策略）",
                                     "applied": []})
            stats.setdefault("capped", []).append(rel)
            if emit:
                emit("protected_zone", {"path": rel, "action": "拒绝",
                                        "message": "认证/部署/治理文件不在自动修改范围内"})
            return
        if hit and emit:
            emit("protected_zone", {"path": rel, "action": "警告",
                                    "message": f"{_scale_mod.resolve('', ws)['level']} 级只警告不阻断"})
        # 单模型的"自动挡"：按这次要写的模块有多难，决定从哪一档思考强度起。
        # 只在首轮生效（之后仍然按失败逐档升强度），所以它只是"起点更聪明"，不改升级语义。
        if str(effort_mode) == "auto" and steps:
            try:
                from . import effort as _eff_m
                from . import settings_store as _ss
                if _ss.single_effort():
                    # 单档直通分路：只有一个思考强度，别按难度打分、也别发一个
                    # 「想要 max 实际 none」的假路由事件。
                    tier_of.setdefault(rel, 0)
                    if emit:
                        emit("effort_route", {
                            "mode": "single", "path": rel, "index": 0,
                            "chosen": str((steps[0] or {}).get("effort") or "none"),
                            "why": "该端点只有一个思考强度，不做难度路由与升档"})
                else:
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
                    # **起点 = 模块实测档案优先，难度分先验兜底**（2026-11-10 双向自适应）：
                    # 有档案 → 从"该模块被证明写对过的最低档"起步（自适应代码库，
                    #          台账随项目生长，越跑越准）；
                    # 无档案 → route_start(难度分) 给保守先验（max 永不当起点）。
                    # 升档封顶仍是 max（route()），失败 escalate_after=1 秒升——
                    # 救场能力不减，变的只是"简单模块不再先烧 max 学费"。
                    try:
                        _pf = _eff_m.proven_floor(ws, rel)
                    except Exception:                    # noqa: BLE001
                        _pf = ""
                    _start = _pf or _eff_m.route_start(_score)
                    _lvl0 = _eff_m.pick(steps, _start)
                    tier_of.setdefault(rel, _lvl0)
                    if emit:
                        _ev = _eff_m.explain("code", _score, _start, steps, _lvl0)
                        _ev["path"] = rel
                        if _pf:
                            _ev["why"] = f"模块实测档案：该模块在 {_pf} 档成功过 → 从此起步"
                        _ev["proven_floor"] = _pf
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
        _mod_budget = float((rec.get("budget_cny") or 0) or max(0.05, _mod_budget_raw))
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
            return
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
                return
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
            return
        step = steps[lvl] if steps else {}
        tag = f"{step.get('profile_id')}:{step.get('effort')}" if step else "?"
        oid = workorder.add(task_id, 0, "implement_module", rel, "", f"整文件通过全部判据（{rel}）",
                            f"按契约写完整文件；当前档 {tag}")
        fp = ws / rel
        try:
            before = fp.read_bytes() if fp.exists() else None
        except OSError:
            before = None
        # 提交闸门的"写入前"基准：这个文件动工之前，入口导得进来吗？
        # 写入后再冒烟一次，两边一比就知道是不是**这个文件**把入口毒化的。
        try:
            from . import gates as _gg_smoke
            _smoke_before = _gg_smoke.gate_import_smoke(ws).ok
        except Exception:                            # noqa: BLE001
            _smoke_before = True
        prev_verified = int((state.get(rel) or {}).get("verified") or 0)
        if emit:
            emit("author_start", {"path": rel, "tier": lvl + 1, "of": len(steps), "by": tag})
        # 2026-11-10：写之前先量一次"这个文件承载的已 done 能力"（基线）。
        # 只对承载已 done 能力的模块做，且是**探针**不是模型调用（零 token）。
        _guard_base: set[str] = set()
        _guard_ids = _guard_ids_for(rel, _deps_map, _done_caps_mods)
        if _guard_ids:
            _base_now = _probe_caps_done(ws, _guard_ids)
            if _base_now:
                _guard_base = set(_base_now)
            elif _base_now is None and emit:
                emit("notice", {"message": f"{rel} 回归基线复验跑不动 → 本次不做回归拦截"})
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
        # 2026-11-10（21:10，第三次同样 4 条被写坏后定稿）：**承载已 done 能力的文件不许整文件重写**。
        # 实测代价：`app/api/topics.py` / `app/api/auth.py` 每次整文件重写都会写坏
        # forbidden_role / create_draft / submit_and_publish / offline 这 4 条，
        # 每片 ¥0.5 换 -4 条，是彻底的负循环。
        # 规则（按优先级）：
        #   ① 承载已 done 能力 + 不缺函数 → **一律增量补丁**（哪怕要注册路由也不许整文件）；
        #   ② 承载已 done 能力的**横切文件** + 缺函数 → **直接不开工**（整文件重写必然写坏）；
        #   ③ 其它要注册端点的模块 → 整文件（老规则）。
        _carries_done = bool(_done_by_file.get(rel))
        _missing_now = list((state.get(rel) or {}).get("missing") or [])
        if _carries_done and not _missing_now:
            if emit and auto != "bodies":
                emit("granular_forced", {"path": rel, "to": "bodies",
                                         "why": f"该模块承载 {len(_done_by_file[rel])} 条"
                                                "已 done 能力，只允许增量补丁"})
            auto = "bodies"
        elif _carries_done and _missing_now and _is_shared_file(rel):
            # **2026-11-11 修正（实测死结）**：
            # 旧口径在这里**直接跳过**（"承载已通过能力且缺函数 → 不做整文件重写"），
            # 于是形成死锁：
            #   缺函数 → 恰恰说明这个文件要**加**新函数 → 正确的做法是"只回函数体"（bodies），
            #   整文件重写才是危险的那个动作。
            #   但旧口径把"整文件重写有风险"读成了"这个文件不能碰" → 跳过 →
            #   远端没写 → 红灯一直在 → 下一轮还是跳过 → 空转（实测一整轮 ¥0.03 换 0 条）。
            #   现场（毕设 s10-9712636b）：`app/api/topics.py` 被反复跳过，
            #   notes 连续出现「片内自验：修好 0 条，仍红 4 条（空转）」。
            #
            # 口径改为：**缺函数 → 强制 bodies（只补缺的那几个函数，不碰已通过的函数）**。
            # 路由装饰器补不了不构成阻塞 —— 切片契约把路由语义放在**服务层函数**
            # （如 `create_topic_route(body, user)`），路由由引擎接线层负责挂。
            if emit and auto != "bodies":
                emit("granular_forced", {"path": rel, "to": "bodies",
                                         "why": f"该模块承载 {len(_done_by_file[rel])} 条已 done 能力，"
                                                f"缺 {len(_missing_now)} 个函数 → 只补缺函数体，"
                                                "不整文件重写（旧口径在这里直接跳过，形成死锁）"})
            auto = "bodies"
        elif cap_endpoints and rel in cap_endpoints and cap_endpoints[rel]:
            if auto != "whole_file":
                if emit:
                    emit("granular_forced", {"path": rel, "to": "whole_file",
                                             "why": "该模块要注册 HTTP 端点，"
                                                    "只回函数体加不了路由装饰器"})
            auto = "whole_file"
        granular = (_os.environ.get("SIXCODER_GRANULAR") or "").strip().lower() or auto
        if emit and granular != auto:
            emit("notice", {"message": f"粒度被环境变量覆盖：{granular}（自动判断是 {auto}）"})
        # 输出配额记忆：这个模块被截断过一次，下一轮直接从放大后的值开始，
        # 不再"每轮从 3000 重算、再被截断一次"（实测 search-index 因此连输三次）。
        # 自愈轮数由 policy 决定（造物模式 4 轮：AI 内部改到判据全绿，人不在中间）；
        # stop_on_repeat 保证"反复刺激"只在有新证据时才发生，不白烧调用。
        try:
            from . import policy as _pol_h
            _heal = max(1, int(_pol_h.current().heal_attempts))
            _stop_rep = bool(_pol_h.current().heal_stop_on_repeat)
        except Exception:                                # noqa: BLE001
            _heal, _stop_rep = 2, True
        _mo_hint = int((_wu(rel).get("max_out") or 0))
        # 2026-11-10：把**端到端红灯详情**一起喂给远端。
        # 契约判据只验函数，不验路由/权限/错误响应体 —— 远端看不到这些，
        # 就不知道"契约全过但端到端 404"到底该改什么，只能重写一遍碰运气。
        _cap_ev = ""
        if cap_detail and rel in cap_detail:
            _cap_ev = ("【端到端验收未通过（机械判据查不出这一层，必须按这个改）】\n"
                       + "\n".join(cap_detail[rel][:4])
                       + "\n注意：路由必须真的挂到 app 的 include_router 上；"
                         "错误响应要带业务码字段；越权要真的返回 403。\n")
        # 2026-11-10：把**该模块该负责的 HTTP 端点清单**也喂给远端。
        # 能力表的端点是产品语义（quota-suggestions），契约里的函数是算法语义
        # （quota_advice）—— 引擎靠字符串猜接不上（实测 20 个端点"无匹配函数"）。
        # 语义映射交给远端：它同时看得见需求和契约，让它用装饰器注册端点，
        # 接线层会优先读产物里的装饰器（auto._route_handler_from_src）。
        _mod_plan = plan
        if cap_endpoints and rel in cap_endpoints:
            _eps = cap_endpoints[rel][:12]
            _mod_plan = (plan + "\n\n" if plan else "") + (
                "【本模块必须提供的 HTTP 端点（能力表要求，必须真的注册）】\n"
                + "\n".join("- " + e for e in _eps)
                + "\n要求：在这个文件里用 FastAPI 的 APIRouter + 装饰器注册"
                  "（例如 `router = APIRouter()`、`@router.get(\"/api/v1/...\")`），"
                  "并导出名为 `router` 的对象；引擎接线层会读产物里的装饰器并优先用它。\n"
                  "函数名请贴合端点语义（例如 quota-suggestions → quota_suggestions），"
                  "但**端点路径必须与上面完全一致**。\n"
                  "**已验收的函数必须原样保留**（它们已通过机械判据，改坏了会被回滚）："
                  "只新增/改写端点相关的那部分，其余函数逐字不动。\n")
        # **前端组件直接走整文件模式**（2026-11-19 生态包）：author_bodies 那套是
        # "只写函数体 + 引擎缩进"，对 JSX/Vue（import + export default 的整份组件）
        # 根本不适用，白跑一次还必然失败。直接 author_module（整文件 JSON files）。
        _is_frontend_file = str(rel).endswith((".jsx", ".tsx", ".vue"))
        if granular in ("bodies", "line", "patch") and not _is_frontend_file:
            r = _author.author_bodies(ws, rel, contracts.get(rel), contracts, model_plan=_mod_plan,
                                      step_index=lvl, task_id=task_id, emit=emit,
                                      local_model=local_model, workingset=ws_text,
                                      max_out=_mo_hint, attempts=_heal,
                                      evidence=_cap_ev)
            if not r.get("ok") and not (ws / rel).exists():
                r = _author.author_module(ws, rel, contracts.get(rel), contracts,
                                          model_plan=_mod_plan, step_index=lvl, task_id=task_id,
                                          emit=emit, local_model=local_model,
                                          workingset=ws_text, max_out=_mo_hint,
                                          attempts=_heal, stop_on_repeat=_stop_rep,
                                          deadline=_deadline, evidence=_cap_ev)
        else:
            r = _author.author_module(ws, rel, contracts.get(rel), contracts, model_plan=_mod_plan,
                                      step_index=lvl, task_id=task_id, emit=emit,
                                      local_model=local_model, workingset=ws_text,
                                      max_out=_mo_hint, attempts=_heal,
                                      stop_on_repeat=_stop_rep, deadline=_deadline,
                                      evidence=_cap_ev)
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
            # 先做**确定性修补**：报"用了 X 但本模块没有定义/导入"时，X 很可能就定义在
            # 另一个模块里（实测 2026-11-07：matching_core/da.py 调 run_batch，而
            # run_batch 就在 app/services/batches.py）。只因为少一行 import 就把整篇回滚、
            # 让模型重写一遍，是最没信息量的失败方式。符号在不在项目里是确定性事实。
            _lf = {"added": [], "unknown": [], "ambiguous": []}
            try:
                from . import linkfix as _linkfix
                _lf = _linkfix.patch(ws, rel, sg.detail)
                if _lf.get("unknown"):
                    # git 历史恢复被丢的模块常量（实测 2026-11-08 11:50：作者重写
                    # 连丢 _ALLOWED_ROLES/_REQUIRED_FIELDS → auth 链 NameError →
                    # 全部 register 探针红 → 已完成能力被观测打回）。
                    _res = _linkfix.restore_constants_from_history(
                        ws, rel, [x.get("name") for x in _lf.get("unknown")])
                    if _res:
                        if emit:
                            emit("constants_restored", {"path": rel, "names": _res})
                        _lf = dict(_lf)
                        _lf["unknown"] = [x for x in _lf.get("unknown", [])
                                          if x.get("name") not in _res]
                        _lf.setdefault("added", []).extend(
                            {"name": n, "module": "git-history"} for n in _res)
                if _lf.get("unknown") or _lf.get("ambiguous"):
                    _linkfix.record_need(ws, rel, (_lf.get("unknown") or [])
                                         + (_lf.get("ambiguous") or []),
                                         reason="结构体检：项目里没有这个符号")
                    if emit:
                        emit("need_interface", {"path": rel,
                                                "missing": [x.get("name") for x in
                                                            (_lf.get("unknown") or [])][:6],
                                                "ambiguous": [x.get("name") for x in
                                                              (_lf.get("ambiguous") or [])][:6]})
                if _lf.get("added"):
                    from . import gates as _gates2
                    want2 = [f.name for f in (getattr(contracts.get(rel), "funcs", None) or [])]
                    sg = _gates2.gate_structure(ws, [rel], contract_funcs=want2)
                    if emit:
                        emit("linkfix", {"path": rel,
                                         "added": [f"{a['name']} ← {a['module']}"
                                                   for a in _lf["added"]][:6],
                                         "ok": bool(sg.ok)})
            except Exception as exc:                     # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"跨模块引用修补跳过：{type(exc).__name__}"})
        # 同名重复定义 → **机械去重（保留最后一次）**再复检，不做回滚。
        # "后定义覆盖前定义"本来就是 Python 运行时语义；回滚只会让作者下一轮
        # 交出同样的重复（实测 2026-11-08 14:36：models.py 的 _get_connection/
        # _coerce_count/_init_schema 各 2 份，连滚 3 轮零进展）。
        if sg is not None and not sg.ok and sg.name == "duplicates":
            try:
                _dd = _gates.dedup_toplevel_defs(ws, rel)
            except Exception:                            # noqa: BLE001
                _dd = []
            if _dd:
                sg = _gates.gate_structure(ws, [rel],
                                           contract_funcs=[f.name for f in (
                                               getattr(contracts.get(rel), "funcs", None) or [])])
                if emit:
                    emit("duplicates_deduped", {"path": rel, "names": _dd,
                                                "ok": bool(sg.ok)})
        if sg is not None and not sg.ok:
            _miss = [x.get("name") for x in (_lf.get("unknown") or [])]
            _amb = [x.get("name") for x in (_lf.get("ambiguous") or [])]
            _hint = ""
            if _miss:
                # 光说"本模块没有定义/导入"，模型下一轮会再写一遍同样的调用。必须说清
                # **项目里也没有**这个符号 —— 那它不是"忘了 import"，是接口根本还没人实现。
                _hint += ("；这些符号在**整个项目里都不存在**：" + "、".join(_miss[:6])
                          + " —— 属于本模块职责就实现它，否则改调用，别留一个必然 NameError 的调用")
            if _amb:
                _hint += ("；这些符号有多个模块都定义了：" + "、".join(_amb[:4])
                          + " —— 用 `模块名.函数名` 明确指向哪一个")
            if before is not None:
                try:
                    fp.write_bytes(before)
                except OSError:
                    pass
            r = {"ok": False,
                 "reason": (f"结构体检不过（已回滚）：{sg.detail}" + _hint)[:300],
                 "feedback": (f"结构体检：{sg.detail}" + _hint)[:600]}
            if emit:
                emit("structure_gate", {"path": rel, "ok": False, "detail": sg.detail[:300]})
        elif sg is not None and emit:
            emit("structure_gate", {"path": rel, "ok": True, "detail": sg.detail[:120]})
        # 提交闸门：**写入后立刻冒烟**。写入前 ok、写入后坏 = 这个文件把入口毒化了
        # （虚构 from-import 单文件体检抓不住——import 就算绑定），回滚并给出证据。
        # 实测 2026-11-08：verified 7/7 的文件落盘后 `import app.main` 全炸，
        # 所有 HTTP 探针红，观测把覆盖 5/76 清成 0/76，而且坏文件不在当前片契约里
        # ——没人去修。在验收前拦住，坏文件根本不落账。
        if sg is None or sg.ok:
            try:
                from . import gates as _gg_smoke2
                _smoke_after = _gg_smoke2.gate_import_smoke(ws)
            except Exception:                        # noqa: BLE001
                _smoke_after = None
            if _smoke_after is not None and not _smoke_after.ok and _smoke_before:
                if before is not None:
                    try:
                        fp.write_bytes(before)
                    except OSError:
                        pass
                _hint = (f"这个文件把 `import app.main` 搞坏了：{_smoke_after.detail}——"
                         "要么把缺失的函数真正实现到被 import 的模块（按调用点确定签名语义），"
                         "要么改用该模块已有的函数；禁止 import 不存在的东西")
                r = {"ok": False,
                     "reason": f"入口冒烟不过（已回滚）：{_smoke_after.detail}"[:300],
                     "feedback": _hint[:600]}
                if emit:
                    emit("import_smoke", {"path": rel, "ok": False,
                                          "detail": _smoke_after.detail[:300],
                                          "rolled_back": True})
                after = iterate.inspect(ws, contracts)
                now_verified = int((after.get(rel) or {}).get("verified") or 0)
            elif _smoke_after is not None and emit:
                emit("import_smoke", {"path": rel, "ok": True, "detail": "ok"})
        # 回归保护：这次写让它已验收数掉了 → 先**只回填冻结函数**，再考虑整文件回滚。
        #
        # 为什么不能直接整文件回滚（实测 2026-11-07）：整文件模式被触发时（定位不到具体
        # 函数），prompt 里同时写着"这些函数已通过验收、不许改动"和"整模块重写"，模型
        # 重写完通常能修好目标函数，但顺手把冻结函数的实现改了 → 已验收数下降 → 整文件
        # 回滚 → 新修好的部分一起丢 → 下一轮从同一状态再来一次，无限循环（da.py 连续
        # 6 轮完全相同的失败指纹就是这么来的）。回填冻结函数只撤销"不该动的部分"，
        # 保留本轮真正的进展。
        after = iterate.inspect(ws, contracts)
        now_verified = int((after.get(rel) or {}).get("verified") or 0)
        if now_verified < prev_verified and before is not None:
            restored = _restore_frozen(ws, rel, before)
            if restored.get("ok"):
                after = iterate.inspect(ws, contracts)
                now_verified = int((after.get(rel) or {}).get("verified") or 0)
                if emit:
                    emit("frozen_restored", {"path": rel, "funcs": restored.get("restored"),
                                             "verified": now_verified})
            if now_verified < prev_verified:
                try:
                    fp.write_bytes(before)
                except OSError:
                    pass
                r = {"ok": False,
                     "reason": f"回归：{rel} 已验收数 {prev_verified}→{now_verified}，已回滚"}
                after = iterate.inspect(ws, contracts)
        ok = bool(r.get("ok"))
        workorder.close(oid, ok, str(r.get("reason") or "")[:400])
        stats["modules"].append({"path": rel, "ok": ok, "tier": lvl + 1, "by": tag,
                                 "reason": str(r.get("reason") or "")[:200],
                                 "attempt": int(r.get("attempt") or 0),
                                 "applied": r.get("applied") or []})
        # 写码失败 → 升档重试（**函数体路径原本没有这一步**，实测 2026-11-09：
        # `author_bodies` 路径在 1836–2040 之间从不改 tier_of → 同一模块永远用
        # 同一个档位反复重写，"换更高思考档"这条救援通道在这条主路上是**断的**）。
        # 升档条件：连续失败达 escalate_after 门槛，且还有更高的档可升
        # （封顶 = 模型物理上限 max，见本文件 _ceil_eff）。
        if not ok and tier_fail.get(rel, 0) + 1 >= escalate_after and lvl + 1 < len(steps):
            tier_fail[rel] = 0
            tier_of[rel] = lvl + 1
            if emit:
                _nxt = steps[lvl + 1]
                emit("escalate", {"order": f"函数体 {rel}",
                                  "from": tag,
                                  "to": f"{_nxt.get('profile_id')}:{_nxt.get('effort')}",
                                  "fails": escalate_after})
        elif ok:
            tier_fail[rel] = 0
            # 成功即回最低档（2026-11-10 补：函数体主路此前只升不降——升档通道
            # 11-09 补通后，模块一旦升到 high/max 就被锁死在那里，哪怕后面全是
            # 一发即中的简单模块也在用最贵档跑（实测 11-10 账本：74% 花费钉在 max，
            # 其中 reasoning 占输出 76%）。工作单路（tier_reset）与整文件路
            # （"解决即回最低档"）都有回落，唯独这条主力路没有。回落后若再遇到
            # 难改动，escalate_after 门槛一触发立刻升回来——升得快、回得也快，
            # 这才是完整的双向弹性。
            if tier_of.get(rel, 0) > 0:
                if emit:
                    emit("tier_reset", {"order": f"函数体 {rel}",
                                        "from": tier_of.get(rel, 0) + 1})
                tier_of[rel] = 0
        # 记一条写作尝试：给"按各档成功率校准封顶"和"按模块大小校准预算"提供证据
        try:
            from . import calibration as _cal_rec
            _cal_rec.record_attempt(ws, path=rel, effort=str(step.get("effort") or "none"),
                                   ok=bool(ok), funcs=_mod_funcs,
                                   cost_cny=_module_spend(ws, rel, _round_since))
        except Exception:                    # noqa: BLE001
            pass
        # 「一次通过率」是造物模式唯一的自检指标：**没有重试就全绿**的模块占比。
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
        # 2026-11-09：远端**配置问题**（401 key 失效 / 402 欠费 / 403）→ 立刻停机。
        # 这类错误重试没有意义（同一个坏 key 再试八次还是 401），继续跑也只是
        # 每张单立刻失败、白烧本地时间，最后还会被记成"模型不会写"或"检测到振荡"，
        # 把一个**换 key 就能解决**的问题变成一场 puzzling 的失败。
        # 注意：这里**不能**用 break —— 本处在嵌套函数 `_write_one` 里，
        # break 不是"终止整夜"（第一版就写错过，语法直接报错）。
        # 改成置标志 + return，由调用方（for rnd 那一层）统一收工。
        if r.get("remote_fatal"):
            _fatal = str(r.get("remote_error") or r.get("reason") or "")[:240]
            stats["remote_fatal"] = _fatal
            if emit:
                emit("remote_fatal", {"path": rel, "error": _fatal,
                                      "action": "立即收工",
                                      "note": "远端配置问题（鉴权/欠费/权限），不是模型能力问题；"
                                              "换 key / 充值 / 改端点后重跑即可"})
            return stats
        # 2026-11-10：**网络不通 ≠ 远端不会写**。
        # 原口径：网络一抖 → 这个模块判 order_failed → 换下一个模块 → 下一个也失败
        # → 整轮下来 0 产出，还被记成"模型不行"（实测：getaddrinfo failed 连着
        # 打掉 greedy.py / manual.py，整轮白跑）。
        # 正确口径：**原地等网络恢复，然后重试同一个模块**。
        # 等多久：`_wait_network` 默认最长 15 分钟（每 15 秒探一次），
        # 恢复不了就把这一轮记成"网络中断"，让上层决定收工还是等下一轮。
        if r.get("network_unreachable"):
            # 最多原地重试 3 次（网络反复抖动时不要无限递归）
            if _net_try >= 3:
                _netwhy = str(r.get("remote_error") or r.get("reason") or "")[:240]
                stats["network_down"] = _netwhy
                if emit:
                    emit("network_down", {"path": rel, "error": _netwhy,
                                          "action": "重试 3 次仍未恢复，交给下一轮",
                                          "note": "网络不可达与模型能力无关"})
                return stats
            _ok = _wait_network(ws, emit=emit, max_seconds=float(_NET_WAIT_SECONDS))
            if _ok:
                if emit:
                    emit("network_recovered", {"path": rel,
                                               "try": _net_try + 1,
                                               "note": "网络已恢复，重试同一个模块"})
                return _write_one(rel, _net_try + 1)   # ← 重试同一个模块（走包装层）
            _netwhy = str(r.get("remote_error") or r.get("reason") or "")[:240]
            stats["network_down"] = _netwhy
            if emit:
                emit("network_down", {"path": rel, "error": _netwhy,
                                      "action": "等网络恢复后重试本模块",
                                      "note": "网络不可达与模型能力无关，已原地等待并重试"})
            return stats
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
                        # 2026-11-11 修：这里原来直接 `stats["modules"][-1][...]`，
                        # 而**第一次写码就可能走补丁路径**（决策表判 ACT_PATCH_LINE）
                        # → 此时 stats["modules"] 还是空的 → **IndexError**
                        # → 被外层 except 吞掉 → 补丁明明成功了却**没记账**
                        # → 轮末 `if not stats["modules"]` 判成"没有可执行的工作"
                        # → **引擎在还有 56 项端到端红灯时收工**（实测 10:16 那轮）。
                        # 修：空的时候**补记一条**，补丁成功必须留下痕迹。
                        _entry = stats["modules"][-1] if stats["modules"] else None
                        if _entry is None:
                            _entry = {
                                "path": rel, "ok": True, "tier": lvl + 1, "by": "line-patch",
                                "reason": "行级补丁成功（本次是首个派工模块）",
                                "attempt": int(r.get("attempt") or 0),
                                "applied": [rel]}
                            stats["modules"].append(_entry)
                        _entry["line_patch"] = fix.get("applied")
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
                    # 2026-11-10：提交前**定向复验**这个文件承载的已 done 能力。
                    # 坏了当场回滚，不等下一轮全量观测（中午 3.5 小时 26 次状态变化就是等出来的）。
                    _bad_caps: list[str] = []
                    _guard_note = ""
                    if _guard_base:
                        _after = _probe_caps_done(ws, _guard_base)
                        if _after is None:
                            _guard_note = "复验跑不动（按原逻辑提交）"
                            if emit:
                                emit("notice", {"message": f"{rel} 回归复验跑不动 → 按原逻辑提交"})
                        else:
                            _bad_caps = sorted(_guard_base - _after)
                    if _bad_caps:
                        ok = False
                        _partial = False
                        sand.rollback()
                        if emit:
                            emit("isolate_rollback", {**sand.to_dict(),
                                                      "regression": _bad_caps[:8]})
                            emit("regression_blocked",
                                 {"path": rel, "caps": _bad_caps[:8],
                                  "note": "这次改动写坏了**已通过**的能力 → 已当场回滚"
                                          "（不等下一轮观测）"})
                        # 下一轮提示词要知道"上次这么改会写坏谁"，否则它会照着再犯一次
                        stats.setdefault("system_issues", []).append(
                            f"{rel}：上一次改法把已通过的能力写坏了"
                            f"（{'、'.join(_bad_caps[:4])}），已回滚。"
                            "本次只补需要改的部分，**不要动这些能力的路由/字段/鉴权**。")
                    else:
                        sand.commit()
                        if emit:
                            emit("isolate_commit", {**sand.to_dict(),
                                                    "partial": _partial,
                                                    "why": _partial_why,
                                                    # 2026-11-10 23:10：守护是"空转"还是"守住了"，
                                                    # 只看这一对数字 —— base=0 表示这次根本没设基线
                                                    # （等于没守），guarded=坏了几个。缺了它，
                                                    # 漏拦时只能靠猜（当晚已猜错一次）。
                                                    "guarded_n": len(_guard_base),
                                                    "base_n": len(_guard_ids),
                                                    "guarded": sorted(_guard_base),
                                                    "guard_note": _guard_note})
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
                # 写代码的思考封顶 = **模型物理上限**（2026-11-09 用户铁律：
                # 完全相信远端，整个系统接管程序开发）。不再用校准值压制 ——
                # 校准值曾被引擎自身的缺陷造成的失败污染成 none，导致
                # `escalate_capped` 恒真、远端失败后永远升不了档。
                _ceil_eff = "max"
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

    _par_res = _par_mod.map_calls(mods, _write_one)
    # ── 兜底记账（2026-11-11 治本）────────────────────────────────
    # `_write_one` 是个千行嵌套函数，有 7 个 `return` 出口，记账（append）与业务逻辑交织。
    # 任何一条路径漏了 append → `stats["modules"]` 为空 → 轮末判成"没有可执行的工作"
    # → **在还有 56 项端到端红灯时收工**（实测 10:16 与 10:58 两轮，同一句话连出两次）。
    # 这里按**本轮实际派过的模块**核对一遍：谁没留下痕迹就补一条 ——
    # "派过工"这件事**一定在账上**，不再依赖每个 return 分支都记得 append。
    _done_paths = {str(m.get("path") or "") for m in (stats.get("modules") or [])
                   if isinstance(m, dict)}
    for _rel in (mods or []):
        if str(_rel) not in _done_paths:
            stats["modules"].append({
                "path": str(_rel), "ok": False, "tier": 0, "by": "-",
                "reason": "本轮派了工但没有留下记账结果（引擎内部异常，已兜底记账）",
                "applied": []})
            if emit:
                emit("accounting_lost", {
                    "path": str(_rel),
                    "note": "派过工但没记账 → 已兜底补记（引擎内部异常，记账漏了）"})
    if _par_res.get("backend") != "serial" and emit:
        emit("parallel_write", {"backend": _par_res["backend"],
                                "workers": _par_res["workers"],
                                "modules": len(mods),
                                "errors": len(_par_res.get("errors") or {})})
    # 记录"这些模块这一轮被服务到了"（保底提权要用）。
    # **必须落盘**（priority.served）：夜班经常重启，计数只在内存里的话
    # 重启一次就清零 → 被冷落的模块会再次饿死（2026-11-10 21:40 就是这么复发的）。
    try:
        from . import priority as _prio_s
        _served = [str(m.get("path") or "") for m in (stats.get("modules") or [])
                   if isinstance(m, dict) and m.get("path")]
        if _served:
            _prio_s.mark_served(ws, _served)
    except Exception:                                    # noqa: BLE001
        pass
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
    # 2026-11-10：抬头必须同时给出**端到端能力**与**业务流**的数字。
    # 只报「验收通过函数 35/35」或「能力 done 28/84」都会骗人 ——
    # 实测 28 条能力 done 的同时 `business_flow passed=0`（主流程一条都不通）。
    try:
        from . import blueprint as _bp_hdr
        _caps0 = list(getattr(_bp_hdr.load(ws), "caps", []) or [])
        _dn0 = sum(1 for c in _caps0 if str(getattr(c, "status", "")) == "done")
        _bf0 = (scope or {}).get("business_flow")
        if not isinstance(_bf0, dict):
            _bf_txt = "未计算（**不是通过**）"
            _bf_ok = False
        elif _bf0.get("applicable") is False:
            _bf_txt = "不适用（契约里凑不齐业务链角色）——**不算通过**"
            _bf_ok = False
        else:
            _bf_ok = bool(_bf0.get("ok"))
            _bf_txt = (("通过（%d/%d 条业务不变量）"
                        % (int(_bf0.get("passed") or 0), int(_bf0.get("total") or 0)))
                       if _bf_ok else
                       ("**未通过**（%d/%d 条业务不变量）"
                        % (int(_bf0.get("passed") or 0), int(_bf0.get("total") or 0))))
        lines.append(f"- **端到端能力：{_dn0}/{len(_caps0)}**（真实 HTTP 探针）"
                     f"｜**业务流：{_bf_txt}**")
        if _dn0 and not _bf_ok:
            lines.append("  - 口径：`能力 done` 只是零件级证据；"
                         "**业务流没通过时，这个数字不能当作\"系统可用\"**")
    except Exception as exc:                             # noqa: BLE001
        lines.append(f"- 能力/业务流口径：无法统计（{type(exc).__name__}）")
    if (scope or {}).get("oscillation"):
        o = scope["oscillation"]
        if o.get("oscillations"):
            lines.append(f"- **振荡 {o['oscillations']} 次**（回到过 {o['distinct_states']} "
                         f"个不同状态中的旧状态；已按打法切换/收工）")
        else:
            lines.append(f"- 未检测到振荡（{o['distinct_states']} 个不同失败状态，无重复）")
    # 实际花费报告：直接读本任务账本（remote_usage），不依赖任何 cap 模块；缺 task_id 不显示累计值
    try:
        from . import remote as _rm2
        _tid = str((scope or {}).get("task_id") or "")
        if _tid.strip():
            _u = _rm2.usage_summary(_tid) or {}
            lines.append(f"- 实际花费：¥{float(_u.get('cost_cny') or 0.0):.4f}"
                         f"（{int(_u.get('calls') or 0)} 次远端调用），未设上限")
        else:
            lines.append("- 实际花费：本次未能统计（缺 task_id；不显示累计值以免误导）")
    except Exception:                                    # noqa: BLE001
        pass
    # ── 三账并列（2026-11-11 用户定稿：分开算，不许互相替代）──────────────
    #   ① 项目需要的预算 = 引擎估算（预测）→ 充值决策
    #   ② 实际花费       = 运行时记账（事实）→ 本行上面那条
    #   ③ 官方余额       = DeepSeek 官方接口（事实）→ 硬约束
    # 三者口径不同，混成一行就会出现"今天三个数都不一样"（实测：账本 ¥39.66 /
    # night_spent ¥33.40 / 文档 ¥31.01）——所以必须各占一行。
    try:
        from . import balance as _bal
        _b = _bal.query()
        if _b.get("ok"):
            _cur = _b.get("currency") or "CNY"
            _tot = _b.get("total")
            _s = ("¥%.2f %s" % (_tot, _cur)) if _tot is not None else "未知"
            if _b.get("granted") is not None:
                _s += f"（赠金 ¥{_b['granted']:.2f} + 充值 ¥{(_b.get('topped_up') or 0.0):.2f}）"
            lines.append(f"- 官方余额：{_s}"
                         + ("　**余额不足**" if _b.get("available") is False else ""))
        else:
            lines.append(f"- 官方余额：查不到（{_b.get('detail') or '未知原因'}）——"
                         "如实报查不到，不用估算冒充")
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import budget_estimate as _be2
        _rc = _be2.load_system_budget(ws)
        if _rc and _rc > 0:
            lines.append(f"- 系统总预算：¥{_rc:.2f}（停机线 ¥{_rc * 1.05:.2f}；"
                         "另设**官方余额闸**：余额耗尽立刻停）")
    except Exception:                                    # noqa: BLE001
        pass
    try:
        from . import balance as _bal3
        _rec = _bal3.reconcile(ws)
        if _rec.get("recharged_cny") is not None:
            lines.append(f"- 充值总额：¥{_rec['recharged_cny']:.2f}（用户登记）"
                         f"｜账外消费差额：{_rec.get('note') or '—'}")
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
    # 各模块写作成功率（**只记录，不降权**）：把"哪几个模块顶格也写不出"摆到台面上，
    # 但**不改派单、不改预算消耗** —— 等数据攒够由用户拍板（2026-11-10 用户定稿）。
    try:
        from . import known_gap as _kg
        lines += ["", *_kg.report_lines(ws)]
    except Exception:                                    # noqa: BLE001
        pass
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
        from . import archrules as _ar
        _arch = _ar.check(ws)
        lines += _ar.report_lines(_arch)
        from . import extpoints as _ep
        lines += _ep.report_lines(_ep.verify_all(ws, _ar.load_spec(ws)))
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 架构约束 / 扩展点", "", f"- 跳过（{type(exc).__name__}）"]
    try:
        from . import ceiling as _ceil2
        lines += _ceil2.report_lines((scope or {}).get("ceiling") or {})
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 能力上限台账", "", f"- 跳过（{type(exc).__name__}）"]

    # 骨架事实（§8.2）：报告里必须先说"现在长什么样、离目标还差多少"
    try:
        from . import blueprint_tree as _bt2
        _tr = _bt2.load(ws) or _bt2.build(ws)
        lines += ["", "### 骨架（L0→L1）", ""] + _bt2.summary_lines(_tr)
    except Exception as exc:                             # noqa: BLE001
        lines += ["", "### 骨架", "", f"- 跳过（{type(exc).__name__}）"]

    lines += ["", "## 这份报告能证明什么、不能证明什么", ""]
    lines += _delivery_ceiling(ws, verified, total)

    lines += ["", "## 如何验证我交付的东西", "",
              "```powershell",
              "# 引擎根目录（安装版是 <安装目录>\\resources\\prototype；源码版是 <SixCoder>\\prototype）",
              f"cd {Path(__file__).resolve().parent.parent}",
              "# 独立验收（与引擎不共享代码，自己连库、自己断言）—— 这是**唯一**能推翻本报告的证据",
              f"python tools\\hq_verify.py {ws}",
              "# 追溯矩阵：需求条目 ↔ 判据 ↔ 证据",
              f"python tools\\trace.py {Path(ws).name}",
              "# （安装版自带 Python，用 <安装目录>\\resources\\python\\python.exe 代替 python）",
              "```", "", "## 需求原文", "", "```", (requirement or "")[:1500], "```", ""]
    p = Path(ws) / "DELIVERY.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _delivery_ceiling(ws: Path, verified: int, total: int) -> list[str]:
    """交付报告的诚实边界（2026-11-23）。

    为什么必须有：`projects/night3` 的 DELIVERY.md 写「验收通过函数：8/8」，
    TRACE.md 写「证据全部为 engine_assertion」。用仓库自带的独立验收跑同一目录是
    **22/25 FAIL**：`summarize([])` 除零、`clean_row(age=None)` 属性错误——
    报告一个字的提示都没有。报告没有说谎（那些判据确实过了），但它**没说自己
    没测什么**，于是"8/8"被读成"没问题"。这里把边界写进报告本身，并明确
    "本报告不覆盖以下四类"。
    """
    out: list[str] = [
        f"- **本报告只证明：契约里写出来的判据通过了 {verified}/{total} 条。**",
        "  它**不证明**下列任何一项（这些是实测踩过的盲区，不是理论担心）：",
        "  - **输入校验 / 边界值**：契约示例通常只写正常路径。`night3` 实测"
        "`summarize([])` 除零崩溃、`clean_row(age=None)` 属性错误，而报告是 8/8。",
        "  - **业务语义正确**：机械判据拦得住\"不符合契约 / 模块接不上 / 读写不一致\"，"
        "拦不住\"需求理解错了\"。",
        "  - **性能 / 并发 / 安全 / 合规**：完全没有覆盖。",
        "  - **测试质量**：报告不统计测试是否为空壳（0 个测试函数、`assert True`、"
        "只判键名不判值都算\"测试通过\"）。看 `pytest` 的 passed 数前先看测试正文。",
    ]
    # 测试空壳：能扫就扫，扫不动如实说（绝不假装"测试没问题"）
    try:
        from . import testweight as _tw
        scan = _tw.scan(Path(ws))
        verdict = _tw.summarize(scan, limit=2)
        mark = "**空壳**" if scan["shell_files"] else "可用"
        out.append(f"- 本次产物测试静态体检：{mark} —— {verdict}")
        if scan.get("verified"):
            out.append("  - 测试在写，但覆盖的只是契约示例那一层；边界与异常路径要看独立验收。")
        elif scan["shell_files"]:
            out.append("  - 上列测试文件里没有一个真断言；它们显示「通过」不代表任何功能被验证。")
    except Exception as exc:                             # noqa: BLE001
        out.append(f"- 测试静态体检：跑不动（{type(exc).__name__}），不给结论")
    # 独立验收现算：能在夜班收工时跑一次就别让用户自己去跑
    try:
        import subprocess as _sp
        import sys as _sys
        root = Path(__file__).resolve().parent.parent          # prototype/
        hv = root / "tools" / "hq_verify.py"
        if hv.is_file():
            proc = _sp.run([_sys.executable, str(hv), str(Path(ws).resolve())],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180)
            head = [ln.strip() for ln in (proc.stdout or "").splitlines()
                    if "独立验收：" in ln or "独立验收:" in ln]
            fails = [ln.strip() for ln in (proc.stdout or "").splitlines()
                     if ln.strip().startswith("FAIL ")]
            if head:
                _verdict_line = head[0].replace("独立验收：", "").replace("独立验收:", "").strip()
                out.append(f"- **独立验收（现算，与引擎不共享代码）**：{_verdict_line}")
                for ln in fails[:5]:
                    out.append(f"  - {ln}")
                if len(fails) > 5:
                    out.append(f"  - ……另有 {len(fails) - 5} 条没列出，跑上面的命令看全量")
                if proc.returncode != 0:
                    out.append("  - **两边不一致时，以独立验收为准**：本报告的判据是"
                               "从契约生成的，独立验收的判据是手写的边界用例。")
            else:
                out.append("- 独立验收：跑不出结论（该脚本输出无法解析），请手动执行")
        else:
            out.append("- 独立验收：本安装不含 `tools/hq_verify.py`，无法自动现算")
    except Exception as exc:                             # noqa: BLE001
        out.append(f"- 独立验收：没能现算（{type(exc).__name__}: {exc}）")
    return out

def _restore_frozen(ws: Path, rel: str, before: bytes) -> dict:
    """把本轮被改坏的**冻结函数**按旧实现回填（只回填这些函数，不动别的）。

    冻结清单（`.sixcoder/frozen.json`）的语义是"这些函数已经通过判据，行为不许变"。
    整文件重写没法保证这一点 —— 模型会把它们顺手重写。直接把整文件回滚又会把本轮
    真正修好的部分一起扔掉（实测 2026-11-07：da.py 六轮完全相同的失败指纹）。
    所以这里做最小撤销：逐函数比对，把变了的冻结函数换回旧实现。

    返回 {ok, restored[], why}。ok=False 表示"回填救不回来"（通常是一个都没变，
    说明掉分的原因在别处），调用方继续走整文件回滚。
    """
    from . import project as _proj_fz, symedit
    try:
        names = sorted(_proj_fz.load_frozen(ws).get(rel) or set())
    except Exception:                                    # noqa: BLE001
        names = []
    if not names:
        return {"ok": False, "restored": [], "why": "该文件没有冻结函数"}
    try:
        before_text = before.decode("utf-8", errors="replace")
        cur = (Path(ws) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"ok": False, "restored": [], "why": f"读不了：{exc}"}
    edits: list[dict] = []
    changed: list[str] = []
    for name in names:
        old = symedit.source_of(before_text, name)
        new = symedit.source_of(cur, name)
        if not old or old.strip() == (new or "").strip():
            continue
        # 被整段删掉时 replace 定位不到符号 → 用 add 补回（顺序可能变，行为不变）
        edits.append({"op": "add", "symbol": name, "code": old, "after": ""}
                     if not new else
                     {"op": "replace", "symbol": name, "code": old})
        changed.append(name)
    if not edits:
        return {"ok": False, "restored": [], "why": "冻结函数没有被改动"}
    rep = symedit.apply_edits(cur, edits)
    if not rep.ok:
        return {"ok": False, "restored": [], "why": f"回填失败：{rep.reason}"}
    try:
        (Path(ws) / rel).write_text(rep.new_source, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "restored": [], "why": f"写不了：{exc}"}
    return {"ok": True, "restored": changed, "why": f"回填 {len(changed)} 个冻结函数"}


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
        # node_id 前缀有 10 种（author/bodies/patch/…，实测 2026-11-08）：作者走
        # "骨架+填体"时记 bodies:<rel>，机械修时记 patch:<rel>。旧版只精确匹配
        # author:<rel>，models.py 全天 bodies 模式的钱一笔都查不到（账本 115 笔
        # ¥1.47，旧口径只见 41 笔 ¥0.88）→ 模块预算熔断永不触发 → 30 次失败
        # 无限重试。改为尾部匹配：<任何前缀>:<rel>；LIKE 里 rel 的 _/% 必须
        # ESCAPE（llm_schema.py 的 _ 不许当通配符），分隔符统一正斜杠（账本
        # node_id 一律正斜杠，Windows Path 传进来也不能漏配）。
        _rel = str(rel).replace("\\", "/")
        _pat = "%:" + _rel.replace("%", "\\%").replace("_", "\\_")
        rows = db.query(
            "SELECT COALESCE(SUM(cost_cny), 0) AS s FROM remote_usage "
            "WHERE node_id LIKE ? ESCAPE '\\' AND created_at >= ?", (_pat, stamp))
        return float((rows[0] or {}).get("s") or 0.0) if rows else 0.0
    except Exception:                                    # noqa: BLE001
        return 0.0


def _contract_func_count(contracts: dict | None) -> int:
    """契约里到底有几个函数签名。0 = 这份契约没法验收（只有模块名不算契约）。"""
    return sum(len(getattr(c, "funcs", None) or []) for c in (contracts or {}).values())


def night(repo: Path, requirement: str, model: str,
          remote_profile: str = "",
          use_remote: bool = True, max_rounds: int = 40, emit=None,
          plan: str = "", contract_file: str = "",
          scale_level: str = "") -> NightResult:
    """夜班入口：内部任何异常都不许让"早上没有交付物"，一定写 DELIVERY.md。

    plan：人给的实现方案（精修模式用它当权威；造物模式留空，由系统内部自己决定）。
    """
    # 进程级栈：一次设定，后续所有 `_stack()`（不带 ws 的几十处调用）都用它。
    # 不设 = 恒落回 fastapi（Django 项目会补出 `router = APIRouter()` 之类）。
    try:
        from . import stack as _stk
        _stk.activate(repo)
    except Exception:                                    # noqa: BLE001
        pass
    try:
        return _night_inner(repo, requirement, model, use_remote, max_rounds, emit,
                            remote_profile=remote_profile,
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
