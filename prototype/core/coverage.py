"""覆盖：项目的**真实进度**（实测出来的），不是计划完成度（自己声明的）。

这个量以前不存在。内循环只知道自己那份契约里 20/20；它不知道自己"少了一个子系统"，
于是计划一旦切错，第 7 轮、第 12 轮照样全绿 —— 验证是局部的，错误是全局的。
这里补上的就是这一条：每轮跑完，重新按**目标**（蓝图）观测一遍整个项目。

顺带白拿一个能力：**项目级回归网**。每次全量重测已通过的能力，
所以"第 9 轮改坏了第 2 轮的东西"会当场变红，而不是等人去读交付报告。

五种状态，每种都有明确含义（不许含糊）：
  done        探针真跑通（唯一的"完成"）
  absent      探针跑了，不通过 —— 功能不存在或没写对
  blocked     只在**报告层**用：依赖没完成但它自己也没通过（观测不跳过，见下）
  descoped    主动放弃（必须有 why）
  unsupported 探针种类还没有执行器（永远不算 done，不假装支持）

**依赖没完成也照样测**（实测 run 7 的教训）：一条 e2e 能力红了，挂在它下面的 7 条 CLI
能力全被判 blocked 不测 → 整轮没有任何信号。依赖只是计划概念，观测要量现实。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

from . import blueprint as _bp
from . import probe as _probe

COVERAGE_FILE = ".sixcoder/coverage.jsonl"
FAILURES_FILE = ".sixcoder/last_failures.json"   # 最近一次观测的失败现象快照（协商读它）
HISTORY_KEEP = 400          # 历史只留最近这么多次观测（够判停滞，不至于无限长）

# 失败步骤的请求体带进证据的长度上限（防整条链 16 步把报告撑爆）。
# 只带**断言失败的那一步** + 期望，够远端看出"发的是 items 不是 volunteers"。
# 单行必须 < 300：openhouse.red_modules 和 report_from_state 都用 [:300] 截，
# 行内超 300 会把"期望字段"这类后半段信息静默切掉（实测 items 这行 294 已贴边）。
_MAX_BODY_CHARS = 200
_MAX_REQ_CHARS = 900


def failed_step_request(probe: dict | None, detail: str, *, evidence: dict | None = None) -> str:
    """从探针定义 + 执行证据里，把**断言失败那几步真实发出的请求**拼成可读文本。

    为什么必须有（2026-11-18 实测血泪）：
      失败证据原本只有「第16步 JSON 字段不符：total 实际 0 期望 3」，**没有请求体**。
      远端只能盯着产物源码瞎猜键名（topicIds / preferenceList / … 猜了 5 轮，
      探针真正发的是 `{"items":[...]}`），因为它根本看不到请求发了什么。
      这是判据证据层的洞，不是模型写得差 —— 把证据补全，远端一轮就能改对。

    只取 detail 里点名的步骤号（"第N步"），不铺全链；每步给 method / path /
    请求 json / 期望；查不到请求体时如实标注，不编造。纯函数、幂等、不改入参。
    """
    text = str(detail or "")
    nums = sorted({int(x) for x in re.findall(r"第\s*(\d+)\s*步", text)})
    if not nums:
        return ""
    pr = probe or {}
    steps = pr.get("steps") or []
    ev_steps = ((evidence or {}).get("steps") or [])
    ev_by_no = {int(s.get("step") or 0): s for s in ev_steps if isinstance(s, dict)}
    out: list[str] = []
    used = 0
    for n in nums:
        st = steps[n - 1] if 1 <= n <= len(steps) else None
        if not isinstance(st, dict):
            continue
        method = str(st.get("method") or "GET").upper()
        path = str(st.get("path") or "")
        # **json 写成字符串也要如实展示**（2026-11-19 实测事故）：探针里
        # `"json": "{\"students\": [...]}"`（字符串，不是对象）时，旧逻辑
        # `body = st["json"] if isinstance(..., dict) else st["data"]` 得到 None，
        # 于是打印「(该步无 json 请求体)」——**这是假的**：请求体确实发了，只是
        # 以字符串形态发出去（产物 json.loads 得到 str 而非 dict → 当空处理 → 0 命中）。
        # 远端看到"无 body"就坚信 probe 没传数据，于是去改**本来正确**的产物规则
        # 改了 5 轮（见产物 apps/rules/urls.py 的 docstring）。反馈被包错语义框 = 没到。
        _jb = st.get("json")
        if isinstance(_jb, str):
            _trim = _jb.strip()
            if _trim[:1] in ("{", "["):
                try:
                    _parsed = json.loads(_trim)
                except Exception:                         # noqa: BLE001
                    _parsed = None
                if isinstance(_parsed, dict):
                    _jb = _parsed                     # 能解析就按对象展示
        body = _jb if isinstance(_jb, (dict, list)) else st.get("data")
        parts = [f"第{n}步 {method} {path}"]
        if isinstance(body, (dict, list)):
            try:
                _bj = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            except Exception:                                     # noqa: BLE001
                _bj = str(body)
            if len(_bj) > _MAX_BODY_CHARS:
                _bj = _bj[:_MAX_BODY_CHARS] + "…(截断)"
            parts.append("请求体=" + _bj)
        elif isinstance(_jb, str) and _jb.strip():
            # 仍是字符串（解析不出对象）：原样给出 + 明确标注它会被**当字符串**发出
            _bs = _jb if len(_jb) <= _MAX_BODY_CHARS else _jb[:_MAX_BODY_CHARS] + "…(截断)"
            parts.append("请求体(字符串，会被当字符串发出而不能当对象解析)=%s" % _bs)
        ev = ev_by_no.get(n)
        if isinstance(ev, dict) and ev.get("status") is not None:
            parts.append("实际状态=%s" % ev.get("status"))
        # **鉴权状态要可见**（2026-11-19 实测事故）：步骤期望 2xx 却没带
        # Authorization 时，产物回 403「权限不足」，远端只能看到状态码，
        # 完全无法判断"是我没带 token 还是产物真没权限" → 去改本来正确的权限逻辑。
        # （cap.inv.topic.guard_pending_1751：建题步骤排在 login 之前、且无鉴权头。）
        _hdr = st.get("headers")
        _has_auth = isinstance(_hdr, dict) and any(
            str(k).lower() == "authorization" for k in _hdr)
        _want_ok = False
        _es = st.get("expect_status")
        if isinstance(_es, list):
            _want_ok = any(str(x).isdigit() and 200 <= int(x) < 300 for x in _es)
        elif str(_es or "").isdigit():
            _want_ok = 200 <= int(str(_es)) < 300
        if not _has_auth and _want_ok:
            parts.append("未带鉴权头")
        # 实际响应体（2026-11-18 关键）：远端必须看到"产物返回了哪些字段"才能对准
        # 契约。实测 trace: 判据要 unacceptable_count，产物只给 unacceptable 列表 +
        # count，字段名错位 —— 但报告原来只说"字段不符 实际0 期望1"，远端看不见全貌，
        # 于是把 teacher_username 误当 topic ID 改了 5 轮。响应字段一列出就能自己对齐。
        resp_line = ""
        if isinstance(ev, dict) and ev.get("body"):
            _resp = ev.get("body")
            if isinstance(_resp, str):
                try:
                    _resp = json.loads(_resp)
                except Exception:                             # noqa: BLE001
                    _resp = _resp[:_MAX_BODY_CHARS]
            if isinstance(_resp, dict):
                _pairs = []
                for _k, _v in _resp.items():
                    _vs = json.dumps(_v, ensure_ascii=False, separators=(",", ":"))
                    if len(_vs) > 60:
                        _vs = _vs[:60] + "…"
                    _pairs.append("%s=%s" % (_k, _vs))
                _rb = "实际响应={" + ", ".join(_pairs) + "}"
            else:
                _rb = "实际响应=" + str(_resp)
            # 行总长必须 < 300（下游 report_from_state / red_modules 都 [:300] 截）
            if len(_rb) > 294:
                _rb = _rb[:293] + "…"
            # 单独一行（不放请求行里），避免和请求体/期望挤在一行被 [:300] 切掉
            resp_line = "  ↳ " + _rb
        for key, label in (("expect_status", "期望状态"), ("expect_json", "期望字段")):
            if st.get(key) is not None:
                try:
                    v = json.dumps(st[key], ensure_ascii=False, separators=(",", ":"))
                except Exception:                                 # noqa: BLE001
                    v = str(st[key])
                parts.append("%s=%s" % (label, v))
        line = "  ".join(parts)
        if not body:
            line += "  (该步确实没有请求体)"
        if used + len(line) > _MAX_REQ_CHARS:
            line = line[:max(0, _MAX_REQ_CHARS - used)]
            out.append(line)
            break
        out.append(line)
        used += len(line) + 1
        if resp_line and used + len(resp_line) <= _MAX_REQ_CHARS + 200:
            out.append(resp_line)          # 紧跟在它的请求行后
            used += len(resp_line) + 1
    return "\n".join(out)


def run(bp, ws, *, timeout: int = _probe.PROBE_TIMEOUT, only=None, emit=None,
        include_descoped: bool = False) -> dict:
    """按蓝图观测整个项目。返回 cov（含每条能力的真实状态）。

    `only` 只测指定能力（增量用）；默认全量重测 —— 全量才有回归网。

    **观测前先把判据自洽化**（确定性、零 token、幂等）。
    为什么必须在这一层而不是只挂在造物模式的 `openhouse.observe`：
    实测 2026-11-11 —— 全自主模式（`auto.night_auto`）直接调 `covmod.run`，
    **绕过** openhouse → 挂在 openhouse 上的自洽化在全自主模式下**根本不生效**，
    而看门狗固定用 `--goal-file`（= 全自主模式）→ 修了个寂寞。
    放在这一层，两个模式一次覆盖。

    自洽化改的是 bp 里探针的断言值（内存）；调用方随后 `mark` + `save` 就会落盘。
    """
    ws = Path(ws).resolve()
    try:
        _n = _bp.align_probe_expectations(bp)
        if _n and emit:
            emit("probe_aligned", {"fields": _n,
                                   "note": "探针期望值自洽化（判据自相矛盾会让覆盖永远红）"})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"判据自洽化跳过：{type(exc).__name__}"})
    order, _missing = _bp.topo(bp)
    want = set(only) if only is not None else None
    done: set[str] = set()
    caps: dict[str, dict] = {}
    t0 = time.time()
    for cid in order:
        c = bp.by_id(cid)
        if c is None:
            continue
        # descoped 是**终态判定**，不是"这次没测到"。2026-11-19 实测：旧条件
        # `and (want is None or cid not in want)` 在 only 显式点名时会把已降级的
        # 能力重新观测成 absent —— 而蓝图自己的 why 已写明"概念重复/占位符无来源"，
        # 于是引擎为这些**已废弃判据**反复派单改产物，覆盖恒 74/85、净增长 0、烧钱。
        # 口径：descope 只有在显式 include_descoped=True（确需重评）时才重新观测。
        if c.status == "descoped" and not include_descoped:
            caps[cid] = {"status": "descoped", "detail": c.why or "已降级"}
            continue
        if want is not None and cid not in want:
            # 不测的能力按已知状态记（不能被当成 done）
            caps[cid] = {"status": c.status if c.status in _bp.STATUSES else "absent",
                         "detail": c.why or "（本次未观测）"}
            if c.status == "done":
                done.add(cid)
            continue
        dep_missing = [d for d in c.deps if d not in done]
        # **照样观测**，不跳过。早先的做法是"依赖没完成就不测，免得假红"，实测（run 7）
        # 代价太大：一条 e2e 能力红了，挂在它下面的 7 条 CLI 能力全部变成 blocked，
        # 于是整轮没有任何信号，只能靠重规划兜 —— 而它们其实可能是各自独立的命令。
        # 依赖只是**计划**的概念；观测要量现实：探针能跑通就算通。
        r = _probe.run(ws, c.probe, timeout=timeout)
        if r.get("ok"):
            status, detail = "done", str(r.get("detail") or "通过")
            done.add(cid)
        elif r.get("invalid"):
            status, detail = "unsupported", "探针不合规：" + str(r.get("detail") or "")
        else:
            status = "absent"
            detail = str(r.get("detail") or "不通过")
            if dep_missing:
                detail += "（依赖未完成：" + ", ".join(dep_missing) + "）"
            if r.get("engine_error"):
                detail = "（探针引擎出错，不是产物的问题）" + detail
            # 把"失败那几步真实发了什么请求"补进证据（2026-11-18）：
            # 只有 actual/expected 没有请求体，远端只能瞎猜键名 —— 实测猜了 5 轮。
            # 详情从这一层进 cov → 整仓报告 + 工作单两条路都拿到，不在下游各自拼。
            try:
                _req = failed_step_request(c.probe, detail,
                                           evidence=r.get("evidence"))
                if _req:
                    detail += "\n【失败步骤的请求（判据实际发的，别猜键名）】\n" + _req
            except Exception as exc:                              # noqa: BLE001
                # 证据补全失败不许吞：留痕，但绝不让它把观测本身搞崩。
                detail += f"\n（请求体补全失败：{type(exc).__name__}）"
        caps[cid] = {"status": status, "detail": detail,
                     "took_s": r.get("took_s"), "evidence": r.get("evidence")}
        if emit:
            emit("coverage", {"cap": cid, "status": status, "detail": detail[:120]})
    total = len([c for c in bp.caps if c.status != "descoped"])
    cov = {"at": round(time.time(), 1), "goal": bp.goal, "version": bp.version,
           "total": total, "done": sorted(done), "caps": caps,
           "took_s": round(time.time() - t0, 1)}
    return cov


def done_count(cov: dict) -> int:
    return len(cov.get("done") or [])


def ratios(cov: dict) -> dict:
    total = int(cov.get("total") or 0)
    d = done_count(cov)
    return {"done": d, "total": total, "pct": round(100.0 * d / total, 1) if total else 0.0}


def record(ws, cov: dict, note: str = "") -> Path:
    """落一条观测历史（停滞判断要用；也是"每轮真实进度"的账）。"""
    p = Path(ws) / COVERAGE_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.fromtimestamp(cov.get("at") or time.time()).strftime("%H:%M:%S"),
           "ts": cov.get("at") or round(time.time(), 1),
           "version": cov.get("version"), "done": done_count(cov),
           "total": cov.get("total"),
           "statuses": {k: v.get("status") for k, v in (cov.get("caps") or {}).items()},
           "note": note[:120]}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _trim(p)
    return p


def save_failures(ws, cov: dict) -> Path:
    """把本轮观测到的**失败现象**落成快照，供死局协商读取（2026-11-19）。

    为什么必须单独存一份（血泪）：
      coverage.jsonl 每轮只记 `statuses`（状态名），**不含 detail/evidence**；
      于是三方协商时审核者只看得到"判据定义 + 回滚叙事"，看不到
      "探针实际跑了什么、返回了什么、和期望差在哪"。实测 11-19 两个审核者
      在信息不全的框架里做了正确推理，却一直改错对象（改判据文本而非夹具/
      产物），端到端连续 6 轮卡 67。这里把完整失败现象（含 failed_step_request
      拼好的真实请求/响应/期望）存下来，协商包直接读——证据先到位，再谈裁决。

    只存非 done 的能力；纯 I/O、幂等、异常只留痕不炸主流程。
    """
    p = Path(ws) / FAILURES_FILE
    rows = {}
    for cid, row in (cov.get("caps") or {}).items():
        if str(row.get("status") or "") == "done":
            continue
        rows[str(cid)] = {
            "status": str(row.get("status") or "absent"),
            "detail": str(row.get("detail") or "")[:4000],
        }
    payload = {"at": round(time.time(), 1), "total": int(cov.get("total") or 0),
               "done": len(cov.get("done") or []), "failures": rows}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:                               # noqa: BLE001
        print("[coverage] 失败现象快照写盘失败: %s" % exc)
    return p


def load_failures(ws, cap_ids: list[str] | None = None) -> dict:
    """读回失败现象快照；cap_ids 给了就只取这些能力，没给取全部。"""
    p = Path(ws) / FAILURES_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}
    fails = d.get("failures") or {}
    if not isinstance(fails, dict):
        return {}
    if cap_ids is None:
        return fails
    want = {str(c) for c in cap_ids}
    return {k: v for k, v in fails.items() if k in want}


def _trim(p: Path) -> None:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    if len(lines) <= HISTORY_KEEP:
        return
    try:
        p.write_text("\n".join(lines[-HISTORY_KEEP:]) + "\n", encoding="utf-8")
    except OSError:
        pass


def history(ws, limit: int = 20) -> list[dict]:
    p = Path(ws) / COVERAGE_FILE
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:                                # noqa: BLE001
            continue
    return out[-limit:] if limit else out


def stalled(ws, bp=None, n: int | None = None) -> bool:
    from . import replan as _replan
    hs = history(ws, limit=max(6, (n or _replan.STALL_ROUNDS) + 3))
    pending = len(bp.pending()) if bp is not None else -1
    return _replan.stalled(hs, n or _replan.STALL_ROUNDS, pending=pending)


def mark(bp, cov: dict):
    """把观测结果写回蓝图（返回副本，不动调用方那份）。

    降级状态**保留**：探针通过不算"撤销降级"，那要人来说。
    **status 必须忠实于观测**（2026-11-08 二次修正）：曾试过"done 不被探针洗掉"，
    但那会切断自愈链路——派单逻辑消费 bp.pending()（非 done），回归能力保持 done
    就没人修它（run15"零订单空转"的变体）。正确的分工：
      · 蓝图 status = 观测真相（探针挂 = absent，引擎据此派单修复）；
      · 回归留痕在 why（"[探针回归待复核]"标记，人/巡检可见）；
      · 中间态噪声由巡检的覆盖回退确认窗口滤掉（连续 3 轮仍低才报警），
        不靠让蓝图说谎。
    """
    out = _bp.Blueprint.from_dict(bp.to_dict())
    regressed: list[str] = []
    for cid, row in (cov.get("caps") or {}).items():
        c = out.by_id(cid)
        if c is None:
            continue
        st = row.get("status") or "absent"
        if c.status == "descoped" and st != "descoped":
            continue
        if c.status == "done" and st != "done":
            regressed.append(cid)
        c.status = st if st in _bp.STATUSES else "absent"
        if st != "done":
            _mark = ("[探针回归待复核 %s] " % time.strftime("%m-%d %H:%M")
                     if cid in regressed else "")
            c.why = (_mark + str(row.get("detail") or ""))[:200]
        elif c.status == "done":
            c.why = ""
    if regressed:
        # 报告可见的回归痕迹（运行期属性，不进 to_dict）
        out.regressed_now = regressed
    return out


def report(bp, cov: dict) -> str:
    """给人看：完成了什么、缺什么、为什么缺。"""
    r = ratios(cov)
    caps = cov.get("caps") or {}
    n_absent = sum(1 for v in caps.values() if v.get("status") == "absent")
    n_block = sum(1 for v in caps.values() if v.get("status") == "blocked")
    n_desc = sum(1 for v in caps.values() if v.get("status") == "descoped")
    n_unsup = sum(1 for v in caps.values() if v.get("status") == "unsupported")
    head = (f"覆盖 {r['done']}/{r['total']}（{r['pct']}%）"
            f"｜缺 {n_absent}｜受阻 {n_block}｜降级 {n_desc}"
            + (f"｜无执行器 {n_unsup}" if n_unsup else "")
            + f"｜观测耗时 {cov.get('took_s')}s")
    lines = [head]
    marks = {"done": "✓", "absent": "✗", "blocked": "⊘", "descoped": "降级", "unsupported": "?"}
    for c in bp.caps:
        row = caps.get(c.id) or {}
        st = row.get("status") or c.status
        detail = str(row.get("detail") or "")
        lines.append(f"  {marks.get(st, st):<4}{c.id:<26}{c.desc[:32]:<34}"
                     + ("" if st == "done" else detail[:60]))
    gaps = [c for c in bp.caps if (caps.get(c.id) or {}).get("status") != "done"
            and (caps.get(c.id) or {}).get("status") != "descoped"]
    if gaps:
        lines.append(f"缺口 {len(gaps)} 条（下一步由 frontier 决定先修哪条）")
    return "\n".join(lines)
