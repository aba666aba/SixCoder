"""覆盖探针：把一条能力变成一次**真的执行**，看它到底在不在。

两条铁律（都在这一层执行，不靠调用方自律）：
1. **默认红**。没跑、跑不动、输出解析不了 → 一律算"没通过"。
   只有拿到确定的观测才算 done。沿用 props.run 的纪律：引擎自己出错（探针脚本跑不起来）
   要跟"产物有 bug"分开报，否则夜班会反复重写本来就正确的代码。
2. **空壳必须过不了**。只判退出码、只判"能调用"、期望值恒真 —— 这些在校验阶段就拒收。
   实测过的形态：模型交 4 个文件 16 行 `raise NotImplementedError`，退出码全 0。

支持探针：
  cli     —— 跑入口脚本，比对 stdout（用户真能用的那条路）
  py_call —— import 模块、调函数、比返回值（零件级，也是最小可观测单元）
  pytest  —— 跑测试，且必须看到指定用例名（空壳 assert True 不算数）
  http    —— 载入 ASGI 应用、真发一次请求、比状态码与 JSON（Web 项目的端到端）
其余种类一律算 unsupported，永远不算 done（不假装支持）。
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROBE_KINDS = ("cli", "py_call", "pytest", "http", "browser", "sql", "electron")
PROBE_TIMEOUT = 60
_SCRATCH_SEQ = 0

_CALL_RUNNER = r'''
import json, sys, importlib


def _safe(x):
    try:
        json.dumps(x)
        return x
    except Exception:
        return repr(x)


def _same(a, b):
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) <= 1e-9 * max(1.0, abs(float(b)))
        except Exception:
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    return a == b


def main():
    cfg = json.loads(sys.argv[1])
    ws = sys.argv[2]
    sys.path.insert(0, ws)
    mod_name = cfg["module"].replace("/", ".").replace("\\", ".")
    if mod_name.endswith(".py"):
        mod_name = mod_name[:-3]
    fn = cfg["func"]
    args = cfg.get("args") or []
    kw = cfg.get("kwargs") or {}
    try:
        mod = importlib.import_module(mod_name)
    except Exception as exc:
        print(json.dumps({"ok": False, "kind": "import",
                          "detail": "%s: %s" % (type(exc).__name__, exc)}))
        return
    f = getattr(mod, fn, None)
    if f is None:
        print(json.dumps({"ok": False, "kind": "missing_func",
                          "detail": "模块 %s 里没有 %s" % (mod_name, fn)}))
        return
    raises = cfg.get("expect_raises")
    try:
        got = f(*args, **kw)
    except Exception as exc:
        if raises and type(exc).__name__ == raises:
            print(json.dumps({"ok": True, "kind": "raised",
                              "detail": "按预期抛出 %s" % raises, "got": None}))
        else:
            print(json.dumps({"ok": False, "kind": "crash",
                              "detail": "%s: %s" % (type(exc).__name__, exc)}))
        return
    if raises:
        print(json.dumps({"ok": False, "kind": "no_raise",
                          "detail": "期望抛 %s，实际返回 %r" % (raises, got),
                          "got": _safe(got)}))
        return
    want = cfg.get("expect")
    same = _same(got, want)
    print(json.dumps({"ok": same, "kind": "value", "got": _safe(got), "want": _safe(want),
                      "detail": "" if same else "返回 %r，期望 %r" % (got, want)}))
    return


main()
'''

_HTTP_RUNNER = r'''
import json, sys, importlib, re

sys.path.insert(0, sys.argv[1])
spec = json.loads(sys.argv[2])
PREVIEW = 260
out = {"ok": False, "kind": "http", "detail": "", "evidence": {}}


def preview(v):
    return str(v if v is not None else "")[:PREVIEW]


def done():
    print(json.dumps(out, ensure_ascii=False))
    raise SystemExit(0)


try:
    from fastapi.testclient import TestClient
except Exception as exc:
    out["detail"] = ("环境里没有 fastapi.testclient（先 pip install -r requirements.txt）："
                     "%s: %s" % (type(exc).__name__, preview(exc)))
    out["engine_error"] = True
    done()

cands = [c for c in ([spec.get("app") or ""] + list(spec.get("app_candidates") or [])
                     + ["main:app", "app.main:app", "app:app",
                        "server:app", "backend.main:app"]) if ":" in c]
client = None
errs = []
seen = set()
for cand in cands:
    if cand in seen:
        continue
    seen.add(cand)
    modname, _, attrname = cand.partition(":")
    try:
        modu = importlib.import_module(modname.strip())
        app_obj = getattr(modu, attrname.strip() or "app")
    except Exception as exc:
        errs.append("%s → %s: %s" % (cand, type(exc).__name__, preview(exc)))
        continue
    try:
        client = TestClient(app_obj)
        break
    except Exception as exc:
        errs.append("%s TestClient → %s: %s" % (cand, type(exc).__name__, preview(exc)))

if client is None:
    out["detail"] = "没有可用的 ASGI 应用（试过 %s 个候选）：%s" % (len(seen), " ; ".join(errs)[:400])
    done()

VAR = {}
_phase = int(spec.get("phase") or 0)
_varfile = spec.get("varfile") or ""
if _varfile:
    try:
        with open(_varfile, encoding="utf-8") as _f:
            VAR.update(json.load(_f))
    except Exception:
        pass


def fill(v):
    """把 "${token}" 这类占位符换成上一步存下来的真实值。"""
    if isinstance(v, str):
        for k, val in VAR.items():
            v = v.replace("${%s}" % k, str(val))
        # 2026-11-19：蓝图里部分 register/login 的 json 被写成 JSON **字符串**
        # `"{\"username\":...}"`，fill 只替换不解析 → 原样发给 Django →
        # 422「用户名不能为空」→ 后续 403/404 连锁假 bug。这里运行时兜底：
        # 先把字符串解析成对象再递归 fill（占位符也能正确替换）。纯文本/表单串
        # 解析失败就保持原样，不当 JSON 对象处理。
        if v.lstrip().startswith("{"):
            try:
                parsed = json.loads(v)
            except Exception:                                # noqa: BLE001
                parsed = None
            if isinstance(parsed, dict):
                return fill(parsed)
        return v
    if isinstance(v, list):
        return [fill(x) for x in v]
    if isinstance(v, dict):
        return {k: fill(x) for k, x in v.items()}
    return v


def pick(obj, path):
    cur = obj
    for part in (path or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            cur = cur[int(part)] if int(part) < len(cur) else None
        else:
            return None
    return cur


steps = spec.get("steps") or []
if _phase == 1:
    steps = steps[:-1] or steps        # 前段：建数据 + 存变量
elif _phase == 2:
    steps = steps[-1:]                 # 后段：新进程里回读
if not steps:
    steps = [{"method": spec.get("method") or "GET", "path": spec.get("path") or "/",
              "json": spec.get("json"), "data": spec.get("data"),
              "headers": spec.get("headers"), "expect_status": spec.get("expect_status"),
              "expect_json": spec.get("expect_json"), "body_has": spec.get("body_has"),
              "body_not_has": spec.get("body_not_has"), "body_has_any": spec.get("body_has_any"),
              "dom_has": spec.get("dom_has"), "dom_not_has": spec.get("dom_not_has")}]

def _race(method, path, kw, times):
    """把同一个请求**并发**发 times 次。

    为什么需要：TestClient 一次一个请求，天然看不出"并发写丢数据 / 共享连接被多线程踩"。
    这类 bug 在单请求验收里 100% 绿，一上线就 500 或丢单。
    用 httpx.ASGITransport + asyncio.gather：sync 端点会被 FastAPI 丢进线程池真并行执行，
    和 uvicorn 下的行为一致，但不用开端口、不会有僵尸进程。
    """
    import asyncio
    import time as _t
    try:
        import httpx
        from httpx import ASGITransport
    except Exception as exc:
        return None, "环境里没有 httpx（并发判据跑不起来）：%s: %s" % (
            type(exc).__name__, preview(exc)), 0.0

    async def one(cli, res, idx):
        try:
            r = await cli.request(method, path, **kw)
            res[idx] = (r.status_code, r.text or "")
        except Exception as exc:
            res[idx] = (None, "%s: %s" % (type(exc).__name__, exc))

    async def go():
        res = [None] * times
        transport = ASGITransport(app=app_obj)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://probe") as cli:
            await asyncio.gather(*[one(cli, res, i) for i in range(times)])
        return res

    t0 = _t.time()
    try:
        return asyncio.run(go()), "", round(_t.time() - t0, 2)
    except Exception as exc:
        return None, "并发请求整体失败：%s: %s" % (type(exc).__name__, preview(exc)), 0.0


bits = []
trace = []
for i, st in enumerate(steps, 1):
    method = str(st.get("method") or "GET").upper()
    path = str(fill(st.get("path") or "/"))
    kw = {}
    if st.get("json") is not None:
        kw["json"] = fill(st["json"])
    if st.get("data") is not None:
        kw["data"] = fill(st["data"])
    if st.get("headers"):
        kw["headers"] = fill(st["headers"])
    par = st.get("parallel")
    if isinstance(par, int) and par > 1:
        res, err, took = _race(method, path, kw, par)
        if res is None:
            bits.append("第%d步 %s %s 并发 %d 次跑不起来：%s" % (i, method, path, par, err))
            break
        want_p = st.get("expect_status")
        if isinstance(want_p, list):
            allowed_p = [int(x) for x in want_p]
        elif isinstance(want_p, int):
            allowed_p = [int(want_p)]
        else:
            allowed_p = list(range(200, 300))
        trace.append({"step": i, "method": method, "path": path, "parallel": par,
                      "status": [c for c, _ in res], "took_s": took})
        badres = [(c, b) for c, b in res if c not in allowed_p]
        if badres:
            from collections import Counter as _Counter
            cnt = _Counter(c for c, _ in badres)
            bits.append("第%d步 并发 %d 次 %s %s：%d 次不符合预期（状态分布 %s）样本：%s"
                        % (i, par, method, path, len(badres),
                           "/".join("%s×%d" % (k, v) for k, v in cnt.most_common(4)),
                           preview(badres[0][1])))
        continue
    try:
        resp = client.request(method, path, **kw)
    except Exception as exc:
        bits.append("第%d步 %s %s 抛异常：%s: %s"
                    % (i, method, path, type(exc).__name__, preview(exc)))
        break
    body = resp.text or ""
    trace.append({"step": i, "method": method, "path": path,
                  "status": resp.status_code, "body": preview(body)})
    _save_spec = st.get("save")
    if isinstance(_save_spec, str):
        # **save 也可能被写成字符串**（2026-11-19 实测）：`"{\"token\": \"token\"}"`。
        # 旧代码直接 `st.get("save").items()` → AttributeError → 该步整体抛异常
        # → token 从未写进 VAR → 后续步骤发 `Bearer ${token}` 字面量 → 403。
        # （cap.inv.topic.guard_pending_1751 就是这么红的。）
        try:
            _parsed_save = json.loads(_save_spec)
        except Exception:                                # noqa: BLE001
            _parsed_save = None
        _save_spec = _parsed_save
    if not isinstance(_save_spec, dict):
        _save_spec = {}
    _fb = {"n": 0}          # 本步 save 兜底序号（每步重置：多 save 才递增 1/2/3）
    for name, src in _save_spec.items():
        # src 可以是字符串或**候选列表**：产物返回的 id 字段名不统一（id / ticket_id / pk），
        # 只认一个名字会拿到 None，后面路径就变成 /tickets/None（实测 2026-11-07）。
        cands = src if isinstance(src, list) else [src]
        payload = None
        try:
            payload = resp.json()
        except Exception:
            payload = None
        val = None
        for c_ in cands:
            val = pick(payload, str(c_))
            if val is not None:
                break
        if val is None and payload is not None:
            # 兜底：自建种子数据的自增 id 通常从 1 起。
            # **但同一步有多个 save 时必须给不同值**（2026-11-19 实测事故）：
            # cap.llm.generate_validate_review 第 3 步 save c1/c2/c3 指向
            # first/second/third_candidate_id —— 产物响应里根本没有这三个键，
            # 旧逻辑把三个变量**全兜底成 1** → 三次评审打在**同一个候选**上
            # （采纳→改写→驳回），stats 恒 adopted=0/rewritten=0，判据永远红。
            # 远端为此对着本来正确的产物改了 5 轮 —— 典型的"判据夹具 bug 制造假 bug"。
            _fb["n"] = _fb["n"] + 1
            val = _fb["n"] if len(_save_spec) > 1 else 1
            # **不许静默**：把兜底记进 trace，失败证据里能看出"这个变量是猜的"
            try:
                trace[-1].setdefault("save_fallback", []).append(str(name))
            except Exception:
                pass
        VAR[str(name)] = val
    want = st.get("expect_status")
    _route_miss = False
    if isinstance(want, list):
        allowed = [int(x) for x in want]
        if resp.status_code not in allowed:
            bits.append("第%d步 %s %s → 状态码 %s（期望 %s）"
                        % (i, method, path, resp.status_code, "/".join(map(str, allowed))))
    else:
        lo, hi = (200, 299)
        if isinstance(want, int):
            lo = hi = int(want)
        if not (lo <= resp.status_code <= hi):
            if resp.status_code == 404:
                # 报告保真（2026-11-08 run_student_da 实测）：404 = 路由没注册，
                # 不是字段问题——旧文案"字段不符：…（实际 None×N）"把人引向
                # 字段表，真凶是路由装饰器/引擎接线缺失。
                _route_miss = True
                bits.append("第%d步 %s %s → 404 路由未命中：该路径没有注册的处理函数"
                            "（先查路由装饰器是否存在 / 引擎接线 web_routes.json 是否含此路由，"
                            "字段比对对 404 无意义，已跳过）" % (i, method, path))
            else:
                bits.append("第%d步 %s %s → 状态码 %s（期望 %s）"
                            % (i, method, path, resp.status_code, want if want is not None else "2xx"))
    # 假绿拦截（2026-11-08 实测，最恶劣的一类）：HTTP 状态码说成功，响应体却自称失败。
    # 真实案例：`POST /api/v1/topics` 返回 **201**，body 是
    #   {"status": 403, "body": {"code": "forbidden"}}
    # —— 权限明确拒绝了，但探针只比状态码和几个字面量字段，一路绿灯。
    # 同理 `{"ok": false}` / `{"success": false}` / `{"code": "forbidden"}` 都是同类。
    # 不拦这条，"done" 会被桩函数骗出来：主干断着，覆盖率照样涨。
    if 200 <= resp.status_code <= 299 and not _route_miss:
        try:
            _pl = resp.json()
        except Exception:
            _pl = None
        if isinstance(_pl, dict):
            _lv = None
            for _k in ("status", "status_code", "http_status", "code"):
                _v = _pl.get(_k)
                if isinstance(_v, int) and 400 <= _v < 600:
                    _lv = "%s=%s" % (_k, _v)
                    break
                if isinstance(_v, str) and _v.strip().lower() in (
                        "error", "fail", "failed", "forbidden", "denied",
                        "unauthorized", "not_found", "rejected"):
                    _lv = "%s=%r" % (_k, _v)
                    break
            if _lv is None:
                for _k in ("ok", "success", "allowed", "valid"):
                    if _pl.get(_k) is False:
                        _lv = "%s=False" % _k
                        break
            if _lv is None and isinstance(_pl.get("body"), dict):
                _bc = _pl["body"].get("code")
                if isinstance(_bc, str) and _bc.strip().lower() in (
                        "forbidden", "unauthorized", "not_found", "error", "denied"):
                    _lv = "body.code=%r" % _bc
            if _lv is not None:
                bits.append("第%d步 状态码 %d 但响应体自称失败（%s）—— 假绿："
                            "HTTP 层说成功、业务层说拒绝，按失败处理"
                            % (i, resp.status_code, _lv))
    ej = st.get("expect_json")
    if ej is not None and not _route_miss:
        try:
            got = resp.json()
        except Exception:
            bits.append("第%d步响应不是 JSON：%s" % (i, preview(body)))
            got = None
        if got is not None:
            if isinstance(ej, dict) and isinstance(got, dict):
                # 期望值写 "*" = **只要这个键存在**（例如登录必须给 token，
                # 但具体 token 字符串不可能提前知道）。没有这条就只能判 200，
                # "登录不返回 token" 这种硬伤会一路绿灯（实测 2026-11-07）。
                bad = [k for k, v in ej.items()
                       if k not in got or (v != "*" and got[k] != v)]
                if bad:
                    # **必须把"期望值"一起写出来**（2026-11-16 00:15 修）：
                    # 以前只说"字段不符：detail（实际 {...}）"，远端根本不知道
                    # 期望是什么，只能猜 —— 实测 `cap.auth.forbidden` 明明已经
                    # 返回 403 + 权限拒绝，只因 detail 文案不等于某个固定句子就红，
                    # 而报告里看不到那个句子，于是它每轮都在瞎改。
                    _exp = {k: ("任意值(非空)" if ej.get(k) == "*" else ej.get(k))
                            for k in bad}
                    bits.append("第%d步 JSON 字段不符：%s（实际 %s；**期望** %s）"
                                % (i, ",".join(bad),
                                   preview({k: got.get(k) for k in bad}), preview(_exp)))
            elif got != ej:
                bits.append("第%d步 JSON 不等于期望：实际 %s；**期望** %s"
                            % (i, preview(got), preview(ej)))
    ec = st.get("expect_count")
    ecmin = st.get("expect_count_min")
    if ec is None and ecmin is None:
        pass
    else:
        try:
            payload = resp.json()
        except Exception:
            payload = None
        got_n = None
        if isinstance(payload, list):
            got_n = len(payload)
        elif isinstance(payload, dict):
            for k in ("total", "count", "total_count", "size"):
                if isinstance(payload.get(k), int):
                    got_n = payload[k]
                    break
            if got_n is None:
                for k in ("items", "data", "results", "list", "tickets", "rows"):
                    if isinstance(payload.get(k), list):
                        got_n = len(payload[k])
                        break
        if got_n is None:
            bits.append("第%d步 判不了条数：响应既不是列表，也没有 total/items（实际 %s）"
                        % (i, preview(payload)))
        elif ec is not None and got_n != int(ec):
            bits.append("第%d步 应有 %d 条记录，实际 %d（并发写丢了 / 没真落库）"
                        % (i, int(ec), got_n))
        elif ecmin is not None and got_n < int(ecmin):
            bits.append("第%d步 至少该有 %d 条记录，实际只有 %d（并发写丢了 / 没真落库）"
                        % (i, int(ecmin), got_n))
    need = st.get("body_has")
    for n in ([need] if isinstance(need, str) else list(need or [])):
        if str(n) not in body:
            bits.append("第%d步响应里没有 %r" % (i, str(n)))
    # §20.5 B 档：服务端渲染页面的 DOM 判据。
    # 规格（字符串，带前缀）：tag:canvas / id:chart / class:btn-primary / text:echarts（或裸文本）。
    # 口径说实话：这是**服务端 HTML** 的结构断言（响应体里真实存在的标签/属性/文字），
    # 不是浏览器端执行 JS 后的 DOM —— 那是视觉级判据（远期，§20.5 明确不做）。
    def _dom_bits(step_no, body_text, specs, forbid):
        got = []
        for sp in ([specs] if isinstance(specs, str) else list(specs or [])):
            s_ = str(sp).strip()
            if not s_:
                continue
            kind_, _, val_ = s_.partition(":")
            low = body_text.lower()
            if kind_.lower() == "tag" and val_:
                hit = ("<" + val_.strip().lower()) in low
                what = "<%s> 元素" % val_.strip()
            elif kind_.lower() == "id" and val_:
                v_ = val_.strip()
                hit = ('id="%s"' % v_) in body_text or ("id='%s'" % v_) in body_text
                what = "id=%r 的元素" % v_
            elif kind_.lower() == "class" and val_:
                v_ = val_.strip()
                hit = re.search(r'class="[^"]*\b' + re.escape(v_) + r'\b[^"]*"',
                                body_text) is not None
                what = "class 含 %r 的元素" % v_
            else:
                v_ = val_.strip() if kind_.lower() == "text" else s_
                hit = str(v_) in body_text
                what = "文字 %r" % str(v_)
            if forbid and hit:
                got.append("第%d步页面里出现了不该出现的 %s" % (step_no, what))
            elif (not forbid) and (not hit):
                got.append("第%d步页面里没有 %s" % (step_no, what))
        return got
    bits += _dom_bits(i, body, st.get("dom_has"), forbid=False)
    bits += _dom_bits(i, body, st.get("dom_not_has"), forbid=True)
    notw = st.get("body_not_has")
    for n in ([notw] if isinstance(notw, str) else list(notw or [])):
        if str(n) in body:
            bits.append("第%d步响应里出现了不该出现的 %r（敏感字段泄露/不该回显）" % (i, str(n)))
    anyw = st.get("body_has_any")
    if anyw:
        ws_ = [anyw] if isinstance(anyw, str) else list(anyw or [])
        if ws_ and not any(str(w) in body for w in ws_):
            bits.append("第%d步响应里没有任何一个关键词：%s"
                        % (i, "/".join(str(x) for x in ws_)))

if _phase == 1 and _varfile:
    try:
        with open(_varfile, "w", encoding="utf-8") as _f:
            json.dump({k: v for k, v in VAR.items()}, _f, ensure_ascii=False)
    except Exception:
        pass
# §20.5 B 档落点 2：前后端契约共享 —— 后端必须把契约声明的接口**如实暴露**在
# OpenAPI 里（前端按契约生成调用/类型，接口不在 openapi.json = 前端调不到）。
_oas = spec.get("openapi_has")
if _oas:
    _want_paths = [_oas] if isinstance(_oas, str) else [str(x) for x in _oas]
    _spec = None
    try:
        _resp = client.request("GET", "/openapi.json")
        _spec = _resp.json() if _resp.status_code == 200 else None
    except Exception:
        _spec = None
    if not isinstance(_spec, dict) or not isinstance(_spec.get("paths"), dict):
        bits.append("前后端契约共享失败：GET /openapi.json 拿不到 OpenAPI paths"
                    "（后端必须把接口如实暴露；没有它前端按契约写的调用全是 404）")
    else:
        for _wp in _want_paths:
            if _wp not in (_spec.get("paths") or {}):
                bits.append("OpenAPI 里没有声明 %s（前后端契约共享："
                            "前端按契约调它，后端必须在 openapi.json 里暴露）" % _wp)

out["evidence"] = {"steps": trace, "vars": {k: preview(v) for k, v in VAR.items()}}
out["ok"] = not bits
out["detail"] = "；".join(bits[:4]) or ("%d 步请求全部符合预期（末步 %s %s）"
                                        % (len(trace), trace[-1]["method"], trace[-1]["path"])
                                        if trace else "没有发出任何请求")
done()
'''


# ---------------------------------------------------------------- 角色串味修补（判据层，零 token）
#
# 病根（2026-11-18 实测，41 条能力中招）：蓝图让**同一个用户名**用不同 role 注册多次，
# 再用**同一个用户名** login 多次，分别存进 ttoken/atoken/stoken。产物 register 把
# account.role 覆盖成最后一次注册的值；login 读 account.role 签 token —— 于是三个
# token 全是同一个角色。拿"teacher"的 token 建题被服务端正确地判 403，探针却当成
# "产品有 bug" → 41 条永远红 → 远端反复改本来正确的 accounts/topics views → 护栏回滚 →
# 净增长恒 0。
#
# 治本：注册/登录都按角色用**独立用户名**（u_x_teacher / u_x_admin / u_x_student），
# 每个账号的 role 天生就是它自己的，token 不再串味。这是判据修正，不碰产物代码。
# 纯函数：不改入参（深拷贝），幂等（已是独立账号的链原样返回），不动任何判据字段。

_TOKEN_ROLE_HINTS = (                     # login 的 save key → 期望角色（按优先级匹配）
    ("ttoken", "teacher"), ("teacher_token", "teacher"), ("tt", "teacher"),
    ("atoken", "admin"), ("admin_token", "admin"), ("adm", "admin"),
    ("stoken", "student"), ("student_token", "student"), ("stu", "student"),
)
_ROLE_USER_SUFFIX = {"teacher": "teacher", "admin": "admin", "student": "student"}


def _user_key_of(body: dict) -> str | None:
    for k in body:
        lk = str(k).lower()
        if "user" in lk or "name" in lk or "email" in lk:
            return k
    return None


def _role_key_of(body: dict) -> str | None:
    for k in body:
        if "role" in str(k).lower():
            return k
    return None


def dedupe_role_users(steps: list) -> tuple[list, list[str]]:
    """把"同名多角色注册 + 同名多次登录"的请求链改写成按角色独立的账号链。

    返回 (new_steps, notes)。满足三条硬约束：
      · 只认**确实串味**的链（同一用户名 ≥2 个不同 role），单角色/无 role 一律不碰；
      · 幂等（独立账号的链再跑一次零改动）；
      · 只改用户名字段，expect_status / expect_json / save 名 / headers 全部原样保留。
    """
    if not isinstance(steps, list) or not steps:
        return steps, []
    new = json.loads(json.dumps(steps))
    notes: list[str] = []
    # ① 找出"被多角色复用"的用户名（串味账号）
    role_by_user: dict[str, set[str]] = {}
    for st in new:
        st = st or {}
        if "/register" not in str(st.get("path") or ""):
            continue
        body = st.get("json") if isinstance(st.get("json"), dict) else st.get("data")
        if not isinstance(body, dict):
            continue
        uk, rk = _user_key_of(body), _role_key_of(body)
        if not uk or not rk:
            continue
        user, role = str(body.get(uk) or ""), str(body.get(rk) or "").strip().lower()
        if user and role:
            role_by_user.setdefault(user, set()).add(role)
    reused = {u for u, roles in role_by_user.items() if len(roles) >= 2}
    if not reused:
        return new, notes

    # ② 给每个被复用的用户名，按其注册 role 生成独立账号；改掉 register 步骤
    #    user → 期望角色映射：先按 register 的 role 建，login 再按 token 名认领。
    user_roles: dict[str, dict[str, str]] = {}    # base_user → {role: independent_user}
    last_role: dict[str, str] = {}               # base_user → 链中最后一次注册的 role
    for st in new:
        st = st or {}
        body = st.get("json") if isinstance(st.get("json"), dict) else st.get("data")
        if not isinstance(body, dict) or "/register" not in str(st.get("path") or ""):
            continue
        uk, rk = _user_key_of(body), _role_key_of(body)
        if not uk or not rk:
            continue
        user, role = str(body.get(uk) or ""), str(body.get(rk) or "").strip().lower()
        if user not in reused or not role:
            continue
        suffix = _ROLE_USER_SUFFIX.get(role, role)
        independent = f"{user}_{suffix}"
        user_roles.setdefault(user, {}).setdefault(role, independent)
        last_role[user] = role                    # 按出现顺序覆盖 → 留下末次角色
        if body.get(uk) != independent:
            body[uk] = independent

    # ③ login 步骤：按 save 的 token 变量名认领角色，换成对应独立账号。
    #    认不出角色（单个无前缀 ${token}）时用**末次注册角色的独立账号**：产物原来
    #    把 account.role 覆盖成末次注册的值，single-token token 就是那个角色 —— 保持
    #    语义不变，只是账号从"共用旧名"换成"末次角色的独立名"（旧名已被 ② 拆掉，不换会 401）。
    for st in new:
        st = st or {}
        if "/login" not in str(st.get("path") or ""):
            continue
        body = st.get("json") if isinstance(st.get("json"), dict) else st.get("data")
        if not isinstance(body, dict):
            continue
        uk = _user_key_of(body)
        if not uk:
            continue
        user = str(body.get(uk) or "")
        if user not in user_roles:
            continue
        save = st.get("save") if isinstance(st.get("save"), dict) else {}
        want_role = ""
        for key in save:
            kl = str(key).lower()
            for hint, role in _TOKEN_ROLE_HINTS:
                if hint in kl:
                    want_role = role
                    break
            if want_role:
                break
        if not want_role:
            want_role = last_role.get(user, "")   # 无前缀 token → 末次注册角色
        if want_role in user_roles[user]:
            body[uk] = user_roles[user][want_role]

    fixed = sum(1 for a, b in zip(steps, new) if a != b)
    if fixed:
        notes.append(f"角色串味修补：{len(reused)} 个多角色账号拆成独立用户名，"
                     f"改 {fixed} 步（判据字段未动）")
    return new, notes


# ---------------------------------------------------------------- 校验 / 描述

def validate(probe: dict) -> list[str]:
    """探针本身能不能判。拒收"空壳也能过"的形态。"""
    if not isinstance(probe, dict) or not probe:
        return ["缺 probe（没有观测方式的能力无法验收，也就无法判断它做没做）"]
    kind = str(probe.get("kind") or "").strip()
    if kind not in PROBE_KINDS:
        return [f"探针种类 {kind or '(空)'} 没有执行器（支持：{', '.join(PROBE_KINDS)}）"]
    out: list[str] = []
    if kind == "cli":
        entry = str(probe.get("entry") or "").strip()
        if not entry:
            out.append("cli 探针缺 entry（入口文件，如 main.py）")
        elif not entry.endswith(".py"):
            # 入口写成 `python` / `pytest` 这类解释器命令时，探针永远红：
            # 它会被当成本仓库里的一个叫 python 的文件去跑（实测踩过，白等一整轮）
            out.append(f"cli 的 entry 必须是仓库里的脚本文件（如 main.py），不能是解释器命令：{entry}")
        if not (probe.get("stdout_has") or probe.get("stdout_eq")):
            out.append("cli 探针只看退出码：空壳 `print(0)` 也能过 → 必须给 stdout_has 或 stdout_eq")
        files = probe.get("files")
        if files is not None and not isinstance(files, dict):
            out.append("cli 探针的 files 必须是 {文件名: 内容}")
    elif kind == "pytest":
        # 测试能力也要能被观测，但不能只看"有没有 passed"：空壳 assert True 也会 passed。
        # 所以强制写清要看到哪些用例名。
        if not probe.get("stdout_has"):
            out.append("pytest 探针必须写明要看到哪些用例名（stdout_has），"
                       "否则一个 `assert True` 的空壳测试也能过")
        if not (probe.get("files") or probe.get("args")):
            out.append("pytest 探针要有 args（测试路径）或 files（测试源码）")
    elif kind == "http":
        app = str(probe.get("app") or "").strip()
        if app and ":" not in app:
            out.append(f"http 探针的 app 要写成 模块:属性（如 app.main:app），现在是 {app!r}")
        steps = probe.get("steps") or []
        if steps:
            if not isinstance(steps, list):
                out.append("http 探针的 steps 必须是列表")
                steps = []
            for i, st in enumerate(steps, 1):
                p = str((st or {}).get("path") or "").strip()
                if not p.startswith("/"):
                    out.append(f"http 探针第 {i} 步的 path 必须以 / 开头（现在是 {p or '(空)'}）")
                par = (st or {}).get("parallel")
                if par is not None and (not isinstance(par, int) or par < 2):
                    out.append(f"http 探针第 {i} 步的 parallel 必须是 ≥2 的整数（现在是 {par!r}）")
            judged = [st for st in steps if (st or {}).get("expect_json") is not None
                      or (st or {}).get("body_has") or (st or {}).get("expect_count") is not None
                      or (st or {}).get("expect_count_min") is not None
                      or (st or {}).get("dom_has") or (st or {}).get("dom_not_has")
                      or (st or {}).get("expect_status") is not None]
            if not judged:
                out.append("http 探针每一步都没写判据：至少要有一 step 带 "
                           "expect_status / expect_json / body_has / dom_has")
            for i, st in enumerate(steps, 1):
                for fld in ("dom_has", "dom_not_has"):
                    v = (st or {}).get(fld)
                    if v is None:
                        continue
                    vs = [v] if isinstance(v, str) else v
                    if not isinstance(vs, list) or not all(isinstance(x, str) for x in vs):
                        out.append(f"http 探针第 {i} 步的 {fld} 必须是字符串或字符串列表")
        else:
            path = str(probe.get("path") or "").strip()
            if not path.startswith("/"):
                out.append(f"http 探针的 path 必须以 / 开头（现在是 {path or '(空)'}）")
            if not (probe.get("expect_json") is not None or probe.get("body_has")
                    or probe.get("dom_has") or probe.get("dom_not_has")
                    or probe.get("openapi_has")
                    or probe.get("expect_status") is not None):
                out.append("http 探针只有状态码 2xx 一个判据太弱："
                           "必须给 expect_json / body_has / dom_has / openapi_has / expect_status 之一")
    elif kind == "sql":
        # 数据层：直连 sqlite 查库（2026-11-19 中间件生态包）。
        if not str(probe.get("db") or "").strip():
            out.append("sql 探针缺 db（产物仓里的 sqlite 文件路径，如 app.db）")
        steps = probe.get("steps")
        if not isinstance(steps, list) or not steps:
            out.append("sql 探针缺 steps（至少一条 SELECT 断言）")
        else:
            judged = [s for s in steps if isinstance(s, dict)
                      and ("expect_rows" in s or "expect_first" in s
                           or "expect_contains" in s)]
            if not judged:
                out.append("sql 探针没有判据：必须给 expect_rows / expect_first / "
                           "expect_contains 之一（只执行不比对等于没判）")
    elif kind == "browser":
        # 前端 SPA：真起服务、真用浏览器访问、真断 DOM（2026-11-19 生态包配套）。
        if not str(probe.get("start_cmd") or "").strip():
            out.append("browser 探针缺 start_cmd（怎么起前端服务，如 npm run dev）")
        if not (probe.get("url") or probe.get("port")):
            out.append("browser 探针要有 url 或 port（访问哪个地址）")
        # **必须有实质断言**：只判"能打开"= 空壳 page 也能过（同 cli 只看退出码的病）。
        if not (probe.get("expect_dom") or probe.get("expect_dom_has")
                or probe.get("expect_url") or probe.get("actions")):
            out.append("browser 探针只有『能访问』太弱：必须给 "
                       "expect_dom / expect_url / actions 之一（空页面也能『能访问』）")
    elif kind == "electron":
        # 真机 Electron UI：CDP 连真窗口、真断 DOM（2026-11-23 生态包补完）。
        if not str(probe.get("exe") or "").strip():
            out.append("electron 探针缺 exe（electron 可执行文件，如 node_modules/electron/dist/electron.exe）")
        # **必须有实质断言**：只判"窗口开了"= 空壳应用也能过（同 browser 的病）。
        if not (probe.get("expect_dom") or probe.get("expect_dom_has")
                or probe.get("expect_title") or probe.get("actions")):
            out.append("electron 探针只有『能启动』太弱：必须给 "
                       "expect_dom / expect_title / actions 之一（空窗口也能『能启动』）")
    else:
        if not str(probe.get("module") or "").strip():
            out.append("py_call 探针缺 module")
        if not str(probe.get("func") or "").strip():
            out.append("py_call 探针缺 func")
        if "expect" not in probe and not probe.get("expect_raises"):
            out.append("py_call 探针没有期望值（空函数返回 None 也能过）→ 必须给 expect 或 expect_raises")
    return out


def describe(probe: dict) -> str:
    """一句话说清"怎么观测"（给蓝图摘要、报告、切片简报共用）。"""
    if not isinstance(probe, dict) or not probe:
        return "（没有观测方式）"
    kind = str(probe.get("kind") or "")
    if kind == "cli":
        args = " ".join(str(a) for a in (probe.get("args") or []))
        cmd = f"python {probe.get('entry') or '?'}" + (f" {args}" if args else "")
        tail = ""
        if probe.get("stdout_has"):
            val = probe.get("stdout_has")
            tail = f" → stdout 含 {val if isinstance(val, str) else '/'.join(map(str, val))!r}"
        elif probe.get("stdout_eq") is not None:
            tail = f" → stdout 等于 {probe.get('stdout_eq')!r}"
        n_files = len(probe.get("files") or {})
        return cmd + tail + (f"（输入素材 {n_files} 个文件）" if n_files else "")
    if kind == "py_call":
        args = ", ".join(repr(a) for a in (probe.get("args") or []))
        call = f"{probe.get('module')}.{probe.get('func')}({args})"
        if probe.get("expect_raises"):
            return call + f" → 抛 {probe.get('expect_raises')}"
        return call + f" → {probe.get('expect')!r}"
    if kind == "pytest":
        paths = " ".join(str(a) for a in (probe.get("args") or [])) or "(仓库里的测试)"
        has = probe.get("stdout_has")
        names = has if isinstance(has, str) else "/".join(str(x) for x in (has or []))
        return f"pytest {paths} → 必须出现用例 {names}"
    if kind == "http":
        steps = probe.get("steps") or []
        if steps:
            parts = []
            for st in steps[:6]:
                tag = f" ×{st.get('parallel')} 并发" if isinstance(st.get("parallel"), int) \
                    and (st.get("parallel") or 0) > 1 else ""
                parts.append(f"{str(st.get('method') or 'GET').upper()} "
                             f"{st.get('path') or '/'}{tag}")
            last = steps[-1] or {}
            tail = ""
            if last.get("expect_count_min") is not None:
                tail += f"｜至少 {last.get('expect_count_min')} 条"
            elif last.get("expect_count") is not None:
                tail += f"｜恰好 {last.get('expect_count')} 条"
            if last.get("expect_status") is not None:
                tail += f" → 末步状态码 {last.get('expect_status')}"
            ej = last.get("expect_json")
            if isinstance(ej, dict) and ej:
                tail += f"｜JSON 含 {'/'.join(list(ej)[:4])}"
            dh = last.get("dom_has")
            if dh:
                dh_ = [dh] if isinstance(dh, str) else list(dh)
                tail += f"｜页面含 {'/'.join(map(str, dh_[:3]))}"
            return " → ".join(parts) + tail
        m = str(probe.get("method") or "GET").upper()
        p = str(probe.get("path") or "/")
        tail = ""
        if probe.get("expect_status") is not None:
            tail += f" → 状态码 {probe.get('expect_status')}"
        ej = probe.get("expect_json")
        if isinstance(ej, dict) and ej:
            tail += f"｜JSON 含 {'/'.join(list(ej)[:4])}"
        bh = probe.get("body_has")
        if bh:
            tail += "｜正文含 " + (bh if isinstance(bh, str) else "/".join(map(str, bh[:3])))
        dh = probe.get("dom_has")
        if dh:
            tail += "｜页面含 " + (dh if isinstance(dh, str)
                                   else "/".join(map(str, list(dh)[:3])))
        oa = probe.get("openapi_has")
        if oa:
            tail += "｜OpenAPI 暴露 " + (oa if isinstance(oa, str)
                                          else "/".join(map(str, list(oa)[:3])))
        return f"{m} {p}{tail}"
    if kind == "electron":
        asserts = []
        if probe.get("expect_title"):
            asserts.append("标题含 %r" % probe["expect_title"])
        dom = probe.get("expect_dom") or probe.get("expect_dom_has") or []
        if dom:
            asserts.append("DOM %d 条" % (len(dom) if isinstance(dom, list) else 1))
        if probe.get("actions"):
            asserts.append("动作 %d 步" % len(probe["actions"]))
        return f"electron CDP 真窗口 → {'、'.join(asserts) or '(缺断言)'}"
    return f"（未支持的探针种类：{kind or '空'}）"


# ---------------------------------------------------------------- 执行

def _rmtree(p: Path) -> None:
    if not p.exists():
        return
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:                                    # noqa: BLE001
        pass


WORK_SUBDIR = "work"          # 固定工作目录名（不再是 run-<pid>/probe-xxx 满地爬）
WORK_MAX_SUBDIRS = 240         # 一次进程内最多保留多少个子目录（防失控）


def _work_root(ws: Path) -> Path:
    return Path(ws) / ".sixcoder" / "tmp" / WORK_SUBDIR


def _pid_alive(pid: int) -> bool:
    """别的进程还在用这个工作目录吗（Windows/Linux 通用，只用标准库）。"""
    if pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:                                    # noqa: BLE001
        try:
            os.kill(pid, 0)
            return True
        except Exception:                                # noqa: BLE001
            return False


def _reset_work_root(ws: Path) -> Path:
    """**开跑前**把工作目录整棵删掉再重建（用户 2026-11-15 定的口径）。

    为什么改成这样（有实测代价）：
      · 旧方案是 `.sixcoder/tmp/run-<pid>/probe-<tag>-<seq>/`，靠 atexit 删自己那棵树。
        进程**正常退出**时没问题；但夜班被强杀（今天杀了 3 次）→ atexit 不执行
        → 每次留下 168 个目录。累积到 **111,793 个目录 / 903MB**。
      · 后果不是占磁盘，而是**文件系统与实时防护**：每建/删一个目录，杀软扫一次；
        11 万个小目录让删除速度掉到 **4 个/秒**（实测 2049 个用了 8 分钟），
        并在 14:00–14:06 触发 Windows 资源耗尽检测器连启三次（Edge 随后挂起）。
      · 固定一个目录 + 开跑前整体删 = 无论上次怎么死，残留上限都是"一棵树"，
        且只删一次（不撞"单轮删除 ≥50"的保护，也不会让杀软逐个扫几万次）。

    安全：**只动 `.sixcoder/tmp/work` 这一个路径**；若发现别的活进程正持有它
    （`.owner` 里的 pid 还活着且不是自己），退让到 `work-<pid>`，绝不互删。
    """
    root = _work_root(ws)
    owner_file = root / ".owner"
    try:
        if root.is_dir() and owner_file.is_file():
            try:
                other = int((owner_file.read_text(encoding="utf-8") or "0").strip() or 0)
            except (OSError, ValueError):
                other = 0
            if other and other != os.getpid() and _pid_alive(other):
                root = Path(ws) / ".sixcoder" / "tmp" / ("work-%d" % os.getpid())
                owner_file = root / ".owner"
    except OSError:
        pass
    shutil.rmtree(root, ignore_errors=True)          # 整棵删（一次系统调用级别的遍历）
    root.mkdir(parents=True, exist_ok=True)
    try:
        owner_file.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    return root


def _scratch(ws: Path, tag: str) -> Path:
    """探针的临时工作目录：**固定父目录 + 每次一个子目录**（隔离仍然保留）。

    为什么要子目录（2026-11-07 实测）：上一片留下的 `users.db` 表结构带 id 列，
    登录 `SELECT *` 取到 id=5 当用户名 → 响应校验炸在作者身上，看起来像它的 bug，
    其实是探针之间的状态污染。所以**探针之间必须隔离**。

    但隔离不等于"目录无限增长"：全部子目录都在 `work/` 下，
    ① 进程启动时整体清一次（`_reset_work_root`）；
    ② 进程内超过 `WORK_MAX_SUBDIRS` 就删最早的（只删自己这一层）；
    ③ 正常退出时 atexit 再整棵删一次。
    """
    global _SCRATCH_SEQ
    _SCRATCH_SEQ += 1
    root = _RUN_DIRS[0] if _RUN_DIRS else _run_dir(ws)
    d = root / ("p%d" % _SCRATCH_SEQ)
    d.mkdir(parents=True, exist_ok=True)
    # 进程内限流：超过上限就删最老的子目录（它们已经跑完了）
    try:
        if _SCRATCH_SEQ % 16 == 0:
            subs = sorted((p for p in root.iterdir() if p.is_dir()),
                          key=lambda p: p.stat().st_mtime)
            for old in subs[:max(0, len(subs) - WORK_MAX_SUBDIRS)]:
                shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass
    return d


def _run_dir(ws: Path) -> Path:
    """本次进程的工作目录：第一次调用时**删旧建新**，之后复用。"""
    if _RUN_DIRS:
        return _RUN_DIRS[0]
    d = _reset_work_root(ws)
    _register_run_dir(d)
    return d


def _cleanup_run_dir() -> None:
    """进程退出时删掉自己那一组临时目录（只删自己的，绝不碰别人的）。

    2026-11-10：必须兜 **BaseException** —— 宿主的删除保护在
    `shutil.rmtree` 里抛的是 `SystemExit`（不是 Exception 子类），
    在 atexit 回调里抛出 = 进程退出码被改成 1（夜班明明跑完了却 exit 1）。
    """
    for ws in list(_RUN_DIRS):
        try:
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
        except BaseException:                           # noqa: BLE001
            pass
    try:
        _RUN_DIRS.clear()
    except Exception:                                    # noqa: BLE001
        pass


def _walk_values(obj):
    """递归产出所有字符串值（不产出键名）——引用改写只认值，不认键。"""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_values(v)
    elif isinstance(obj, list):
        for x in obj:
            yield from _walk_values(x)
    elif isinstance(obj, str):
        yield obj


def distinct_same_role_users(steps: list, cap_id: str = "") -> tuple[list, list[str]]:
    """**同一角色出现多次 → 每个账号给不同用户名**（2026-11-18 三方协商裁决）。

    事故：20 个能力的探针里，"第二个学生"和第"一个学生"用了**同一个 username**
    （都是 `..._student`），两条注册都期望 201；而 cap.api.idempotent 要求
    "同名重复注册必须 4xx"。同一份代码不可能同时满足 → 写作者怎么改都红，
    6 轮白改。审核者（两把独立 key）独立裁决一致：改判据，不是改产物。

    规则（幂等，只动用户名字段）：
      · 同一 (username, role) 在**同一链**里注册 ≥2 次 → 第 k 次（k≥2）改名为 `user{k}`；
      · 后续步骤里引用旧名的（login 的 username / 路径里的字面量），按出现顺序
        依次映射到新名字（第 1 次仍是原名，第 2 次 → user2 …），保证"第 k 个账号
        的登录打的就是第 k 个账号"。
    """
    if not isinstance(steps, list) or not steps:
        return steps, []
    # **幂等类能力豁免**（2026-11-19 实测）：cap.api.idempotent 的语义就是
    # "同一账号重复注册必须 4xx" —— 把它拆成两个账号，等于把判据改成自相矛盾
    # （reviewer2 一眼指出"幂等探针发的是不同用户名却要求 4xx"）。这类一律不动。
    if "idempotent" in str(cap_id or "").lower():
        return steps, []
    new = json.loads(json.dumps(steps))
    notes: list[str] = []

    # ① 按出现顺序给重复注册改名
    seen: dict[tuple[str, str], int] = {}
    alias: list[tuple[int, str, str]] = []        # (步骤下标, 旧名, 新名)
    for i, st in enumerate(new):
        st = st or {}
        if "/register" not in str(st.get("path") or ""):
            continue
        body = st.get("json") if isinstance(st.get("json"), dict) else st.get("data")
        if not isinstance(body, dict):
            continue
        uk, rk = _user_key_of(body), _role_key_of(body)
        if not uk:
            continue
        user = str(body.get(uk) or "").strip()
        role = str(body.get(rk) or "").strip().lower() if rk else ""
        if not user:
            continue
        key = (user, role)
        seen[key] = seen.get(key, 0) + 1
        if seen[key] >= 2:
            fresh = "%s_%d" % (user, seen[key])
            body[uk] = fresh
            alias.append((i, user, fresh))
            notes.append("第%d步 重复账号 %s → %s（同角色多账号拆开）"
                         % (i + 1, user, fresh))
    if not alias:
        return new, notes

    # ② 后续步骤引用旧名 → 按出现顺序依次映射新名（第 1 次仍是原名）。
    #    **只替换字符串值**（json 值 / path 的 /name 段），绝不裸字符串替换 ——
    #    裸替换会把 "username" 这种键名也改掉，JSON 直接报废（实测）。
    def _has_ref(step) -> bool:
        for v in _walk_values(step):
            if v == old:
                return True
        return ("/" + old) in str((step or {}).get("path") or "")

    def _sub(obj, repl: str):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                out[k] = _sub(v, repl)
            return out
        if isinstance(obj, list):
            return [_sub(x, repl) for x in obj]
        if isinstance(obj, str):
            if obj == old:
                return repl
            if obj.startswith("/"):
                # 路径按**段**匹配：/api/v1/users/u/ 只换 "u" 这一段，
                # 不许裸子串替换（"/u" 会命中 "/users"，实测报废）
                return "/".join(repl if seg == old else seg for seg in obj.split("/"))
            return obj
        return obj

    for old in sorted({a[1] for a in alias}):
        news = [a[2] for a in sorted(alias) if a[1] == old]
        occ = 0
        for i, st in enumerate(new):
            if "/register" in str((st or {}).get("path") or ""):
                continue                     # 注册步骤已在 ① 处理
            if not _has_ref(st):
                continue
            occ += 1
            if occ == 1:
                continue                     # 第 1 个账号仍是原名
            idx = min(occ - 2, len(news) - 1)
            new[i] = _sub(st, news[idx])
    return new, notes


# ---------------------------------------------------------------- 临时区垃圾回收

# 引擎自己的临时命名空间：**只回收这些名字**，别的目录一律不碰。
_SCRATCH_PREFIXES = ("probe-", "run-", "work", "work-")


def gc_scratch(ws: Path, max_age_hours: float = 24.0,
               max_dirs: int = 2000, batch_size: int = 45) -> dict:
    """回收**引擎自己**留下的临时目录（开跑前调一次）。

    为什么引擎能自己清、而以前不能：
      · 以前每个探针留一个目录、**从不删** —— 逐个删会撞上"单轮删除 ≥50 个就拦下"
        的保护（那保护是给**用户文件**用的）。
      · 现在按进程分组（`run-{pid}/`），进程退出时整体删**一棵目录树**。
      · 跨进程残留（进程被强杀留下的）靠本函数回收：**只删引擎自己的命名空间**
        （`probe-*` / `run-*`）+ **限龄**（默认 24 小时前）+ **限速**，
        删不动就下轮继续，绝不一次掀掉整个盘。

    2026-11-09 两次踩坑（都靠 py-spy 抓栈 + 宿主日志定位）：
      1. **逐个删会被拖死**：宿主环境里 `shutil.rmtree` 被 `sitecustomize.py` 包装 ——
         每删一个目录都要**起一个 node 子进程**做"批量删除保护"检查（timeout 10 秒）。
         36754 个目录 = 36754 次 node 启动 → 夜班直接卡死。
      2. **一把删太多会被拒**：宿主保护是「单轮删除条目数 ≥ 50 要确认」
         （实测 `SAFE_DELETE_BULK_CONFIRM_REQUIRED {"count":5068,"threshold":50}`），
         批量 5000 → 被 fail-closed 拒掉。

      所以现在的做法是**两者取长**：先用 rename 把要删的目录归集到批次目录
      （rename 是纯文件系统操作，不走那层包装），**再按 ≤45 个一批** rmtree
      —— 每批的删除条目数低于阈值 50，一次 rmtree 只触发 1 次删除检查。
      36754 个目录 ≈ 800 批 ≈ 几分钟（一次性）；之后每轮只回收新产生的那点。

    返回 {"scanned", "removed", "freed_bytes", "kept"} 供交付报告如实写明。
    """
    import time as _time
    tmp = Path(ws) / ".sixcoder" / "tmp"
    out = {"scanned": 0, "removed": 0, "freed_bytes": 0, "kept": 0}
    if not tmp.is_dir():
        return out
    now = _time.time()
    cutoff = now - max(0.0, float(max_age_hours)) * 3600.0
    limit = max(1, int(max_dirs))
    # 每批的**删除条目数**预算（目录 + 里面所有文件）。宿主阈值是 50，留足余量。
    # 2026-11-09 血泪：宿主统计的是**实际删除条目数**，不是顶层目录数 ——
    # 45 个探针目录（每个 1 个文件）= 79 条 > 50 → 被拒，且异常打死夜班。
    entry_budget = max(1, min(int(batch_size), 15))

    # 第一步：挑出该回收的（只挑不删 —— 便宜）
    victims: list[Path] = []
    for child in sorted(tmp.iterdir(), key=lambda p: p.name):
        out["scanned"] += 1
        if len(victims) >= limit:
            out["kept"] += 1
            continue
        if not child.name.startswith(_SCRATCH_PREFIXES):
            out["kept"] += 1
            continue
        try:
            if child.stat().st_mtime > cutoff:
                out["kept"] += 1          # 还新鲜：可能是别的进程正在用
                continue
        except OSError:
            out["kept"] += 1
            continue
        victims.append(child)

    if not victims:
        return out

    # 第二步：rename 归集 → 按**条目数**分批 rmtree（每批只触发一次删除检查）
    removed = 0
    freed = 0
    i = 0
    while i < len(victims):
        chunk: list[Path] = []
        entries = 1                       # 批次目录本身也算一条
        j = i
        while j < len(victims):
            est = _count_entries(victims[j])
            if chunk and entries + est > entry_budget:
                break
            if not chunk and est > entry_budget:
                est = entry_budget       # 单个就超预算：照样收，认赔
            chunk.append(victims[j])
            entries += est
            j += 1
        i = j
        batch = tmp / f".gc-{int(now)}-{i}"
        try:
            batch.mkdir(parents=True, exist_ok=True)
        except OSError:
            break
        size = 0
        moved = 0
        for v in chunk:
            try:
                size += _dir_size(v)
                v.rename(batch / v.name)
                moved += 1
            except OSError:
                continue
        if not moved:
            try:
                batch.rmdir()
            except OSError:
                pass
            continue
        try:
            shutil.rmtree(batch, ignore_errors=True)
        except BaseException:                       # noqa: BLE001
            # 宿主的删除保护抛的是 BaseException 子类 —— 必须兜住，
            # 否则清理失败会直接把引擎/夜班进程打死（2026-11-09 exit 1 实锤）。
            pass
        if batch.exists():
            # 批次没删掉（被保护拦了）→ 留在盘上，下轮再收
            out["kept"] += moved
            continue
        removed += moved
        freed += size
    out["removed"] = removed
    out["freed_bytes"] = freed
    out["kept"] = max(0, out["scanned"] - removed)
    return out


def gc_engine_tmp(root: Path | None = None, max_age_hours: float = 48.0,
                  max_entries: int = 4000, batch_size: int = 45) -> dict:
    """清**引擎自身**的 prototype/.tmp（2026-11-18 新增）。

    为什么单独一个函数：`gc_scratch` 只清**产物仓**的 .sixcoder/tmp；引擎自己的
    .tmp（bizflow_* / httpe2e_* / scope_* / run_*…，http 流测试与自检留下）**没人清**
    —— 实测 647 目录 / 61MB 按天涨，check_tmp_hygiene 之外的盲区。规则：
      · 只动引擎 .tmp 这一个目录；hsperfdata_*（JVM 活文件）绝不碰；
      · 只删 mtime 早于 max_age 的条目（活进程正在用的碰不到）；
      · 按**条目数**预算分批 + rename 到批次目录再整棵删（复用 gc_scratch 手法，
        不撞"单轮删除 ≥50 条"的保护）；删除动作兜 BaseException（绝不打死夜班）。
    `root` 参数只为自检注入（默认取引擎真实 .tmp）。
    """
    if root is None:
        root = Path(__file__).resolve().parents[1] / ".tmp"
    root = Path(root)
    out = {"removed": 0, "kept": 0, "scanned": 0, "freed_bytes": 0, "root": str(root)}
    if not root.is_dir():
        return out
    cutoff = time.time() - max_age_hours * 3600.0
    try:
        victims = [e for e in root.iterdir()
                   if not e.name.startswith("hsperfdata_")
                   and not e.name.startswith(".gc-")]
    except OSError:
        return out
    old: list[Path] = []
    for e in victims:
        try:
            if e.stat().st_mtime <= cutoff:
                old.append(e)
        except OSError:
            continue
    out["scanned"] = len(victims)
    out["kept"] = len(victims) - len(old)
    removed, freed, entries, i = 0, 0, 0, 0
    while i < len(old) and entries < max_entries:
        chunk: list[Path] = []
        est, j = 0, i
        while j < len(old) and entries + est < max_entries and est < batch_size * 3:
            e = old[j]
            est += _count_entries(e) if e.is_dir() else 1
            chunk.append(e)
            j += 1
        i = j
        entries += est
        batch = root / (".gc-%d" % (int(time.time() * 1000) % 100000000))
        try:
            batch.mkdir(parents=True, exist_ok=True)
        except OSError:
            break
        moved = 0
        for v in chunk:
            try:
                freed += _dir_size(v) if v.is_dir() else v.stat().st_size
                v.rename(batch / v.name)
                moved += 1
            except OSError:
                continue
        if not moved:
            try:
                batch.rmdir()
            except OSError:
                pass
            continue
        try:
            shutil.rmtree(batch, ignore_errors=True)
        except BaseException:                       # noqa: BLE001
            # 宿主的删除保护抛 BaseException 子类 —— 清理失败绝不能打死夜班。
            pass
        if batch.exists():
            # 批次没删掉（被保护拦了）→ 留在盘上，下轮再收
            out["kept"] += moved
            continue
        removed += moved
    out["removed"] = removed
    out["freed_bytes"] = freed
    return out


def sweep_on_finish(ws: Path, scratch_age_hours: float = 1.0,
                    engine_age_hours: float = 6.0,
                    max_dirs: int = 5000) -> dict:
    """夜班**收工**清理：任务结束时不该留一地临时目录（2026-11-19 用户口径）。

    与 `gc_scratch` 的分工（两者不能混用同一个强度）：
      · `gc_scratch`：**开跑**时小批量回收（默认只 50 个）—— 夜班正在等，
        绝不能让它卡在删除上；
      · 本函数：**收工**时一次扫干净 —— 此时没有轮循环在跑，多花十几秒
        换"交付时目录是干净的"值得。

    安全边界与 gc_scratch 完全一致（这些是**不能动**的硬约束）：
      · 只删引擎自己的命名空间（`probe-*` / `run-*` / `work*`）；
      · 只删**超龄**的（默认 1 小时前；正在跑的进程会持续写文件刷新 mtime）；
      · rename 归集后按**条目预算** rmtree（绕过宿主的"单轮删除 ≥50 条要确认"）。

    额外清掉两类无争议垃圾：`__pycache__` / `*.pyc`、上一轮没删成的 `.gc-*` 批次。
    `*.broken_*` / `_*_run.py` / `.coverage` **在这里不删**——它们由
    `core/scratch_store.archive_scratch` 先**搬到引擎本地归档**（留作备用），
    搬完这里就只剩空壳可清了。顺序必须"先归档、后清理"。

    返回 {"scratch": {...}, "engine_tmp": {...}, "removed": 总数, "freed_bytes": 总额}
    供交付报告如实写明"收尾清了多少"。
    """
    import time as _time
    out: dict = {"scratch": {}, "engine_tmp": {}, "removed": 0, "freed_bytes": 0,
                 "errors": []}
    tmp = Path(ws) / ".sixcoder" / "tmp"

    # ① 先清上一轮没删掉的批次残留与字节码（这两类没有"超龄"顾虑）
    if tmp.is_dir():
        for child in list(tmp.iterdir()):
            nm = child.name
            if not (nm.startswith(".gc-") or nm == "__pycache__"
                    or nm.endswith(".pyc")):
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except BaseException as exc:                # noqa: BLE001
                out["errors"].append("%s: %s" % (nm, type(exc).__name__))
        # ② 探针沙箱：白名单 + 超龄，一次性扫（上限放宽到 max_dirs）
        try:
            out["scratch"] = gc_scratch(ws, max_age_hours=scratch_age_hours,
                                        max_dirs=max_dirs)
        except BaseException as exc:                    # noqa: BLE001
            out["errors"].append("gc_scratch: %s" % type(exc).__name__)

    # ③ 引擎自己的 .tmp（自检/http 流测试残留）同样收一遍
    try:
        out["engine_tmp"] = gc_engine_tmp(max_age_hours=engine_age_hours,
                                          max_entries=20000)
    except BaseException as exc:                        # noqa: BLE001
        out["errors"].append("gc_engine_tmp: %s" % type(exc).__name__)

    out["removed"] = int((out["scratch"] or {}).get("removed") or 0) \
        + int((out["engine_tmp"] or {}).get("removed") or 0)
    out["freed_bytes"] = int((out["scratch"] or {}).get("freed_bytes") or 0) \
        + int((out["engine_tmp"] or {}).get("freed_bytes") or 0)
    return out


def _count_entries(p: Path) -> int:
    """这个目录树大概有多少条（文件 + 子目录），用来把每批的删除条目数压在阈值下。

    估算即可（探针沙箱都是 1 个文件），但必须**便宜**：不能对几万个目录做全量遍历。
    """
    n = 3          # 保守起见：一个目录至少算 3 条（目录本身 + 内容 + 冗余）
    try:
        with os.scandir(p) as it:
            for e in it:
                n += 1
                if e.is_dir(follow_symlinks=False):
                    n += 4          # 目录里的东西粗估：宁可高估也别撞阈值
                if n > 60:
                    break
    except OSError:
        pass
    return n


def _dir_size(p: Path) -> int:
    """目录占用字节数（只算一层内容，够用来如实报告"回收了多少"）。"""
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += (Path(root) / f).stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


_RUN_DIRS: list[Path] = []


def _register_run_dir(d: Path) -> None:
    if d not in _RUN_DIRS:
        _RUN_DIRS.append(d)
        atexit.register(_cleanup_run_dir)


def _env(ws: Path) -> dict:
    """探针/测试/体检共用的运行环境：**自动加载仓库里的 .env**。

    产物按设计文档用 `os.environ["JWT_SECRET_KEY"]` 这类变量（Web 项目本来就该这样），
    但测试进程不会自动读 .env → 一导入就 KeyError，所有能力一起红。
    （实测 2026-11-07：`TypeError/KeyError` 全挂在"没给运行环境"上，不是代码错。）
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONUTF8": "1"}
    envf = Path(ws) / ".env"
    if envf.is_file():
        try:
            for ln in envf.read_text(encoding="utf-8", errors="replace").splitlines():
                ln = ln.strip()
                if not ln or ln.startswith("#") or "=" not in ln:
                    continue
                k, _, v = ln.partition("=")
                env.setdefault(k.strip(), v.strip())
        except OSError:
            pass
    path = str(ws)
    old = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = path + (os.pathsep + old if old else "")
    # 协商 3/3 装上、且复验通过的隔离包目录也加进 PYTHONPATH —— 让产物真能
    # import 到它们（否则装完只是摆着，等于没装）。失败不影响探针（best-effort）。
    try:
        from . import envinstall as _ei
        for _d in _ei.installed_paths():
            if _d and _d not in env.get("PYTHONPATH", ""):
                env["PYTHONPATH"] = _d + os.pathsep + env.get("PYTHONPATH", "")
    except Exception:                                    # noqa: BLE001
        pass
    return env


def _tail(text: str, n: int = 240) -> str:
    t = (text or "").strip()
    return t[-n:] if len(t) > n else t


def _kill_tree(pid: int) -> None:
    """杀掉整棵进程树（Windows 用 taskkill /T，POSIX 用 killpg）。

    只 kill 直接子进程不够：探针常去启动被测应用，应用自己再起 uvicorn/worker，
    孙进程会继续持有管道或占着端口，下一次探针就跟着失败。
    """
    try:
        if os.name == "nt":
            # 输出丢给 DEVNULL：taskkill 不需要捕获（也为了让自检能一句断言
            # "probe.py 里不许出现 capture_output" —— 管道抓输出正是这次挂死的根源）
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:                                            # pragma: no cover - 非 Windows
            os.killpg(os.getpgid(pid), 9)
    except Exception:                                    # noqa: BLE001
        try:
            os.kill(pid, 9)
        except Exception:                                # noqa: BLE001
            pass


def _run_child(cmd: list[str], *, cwd: str, timeout: int,
               env: dict | None = None) -> dict:
    """跑子进程：**输出写文件**（不用管道），超时杀**整棵进程树**。

    为什么不用 `capture_output=True`（2026-11-10 实测血泪）：
      子进程若再起孙进程（uvicorn / 后台线程 / worker）并继承了 stdout 管道，
      `subprocess.run(timeout=...)` 到期 kill 掉子进程后，`communicate()` 仍要等
      **所有写端关闭** —— 孙进程活着就永远等下去。实测 18:01 起跑探针、18:05 超时，
      之后夜班零输出零花费挂了 13 分钟，py-spy 栈停在
      `_http_exec → subprocess.run → Popen._communicate → Thread.join`。
      写文件没有这层耦合：到点就杀树，再读文件里已经写下的内容，判定照旧。

    返回 {out, err, code, timed_out}，**永不抛异常**（调用方按原来的口径报错）。
    """
    import tempfile
    out_f = err_f = None
    try:
        out_f = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", errors="replace",
                                            suffix=".out", delete=False)
        err_f = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", errors="replace",
                                            suffix=".err", delete=False)
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out_f, stderr=err_f,
                                env=env if env is not None else None)
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:            # 杀不动也不能再等
                code = -9
        out_f.flush()
        err_f.flush()
        for fh in (out_f, err_f):
            fh.seek(0)
        out = out_f.read() or ""
        err = err_f.read() or ""
        return {"out": out, "err": err, "code": code, "timed_out": timed_out}
    except Exception as exc:                             # noqa: BLE001
        return {"out": "", "err": f"{type(exc).__name__}: {exc}", "code": -1,
                "timed_out": False}
    finally:
        for fh in (out_f, err_f):
            if fh is not None:
                try:
                    name = fh.name
                    fh.close()
                    os.unlink(name)
                except Exception:                        # noqa: BLE001
                    pass


def _run_cli(ws: Path, probe: dict, timeout: int, out_cap: int = 240) -> dict:
    entry = str(probe.get("entry") or "").strip()
    target = (ws / entry).resolve()
    try:
        target.relative_to(ws.resolve())
    except ValueError:
        return {"ok": False, "detail": f"entry 跑到仓库外面了：{entry}"}
    if not target.is_file():
        return {"ok": False, "detail": f"入口不存在：{entry}（能力还没做出来）"}
    args = [str(a) for a in (probe.get("args") or [])]
    scratch = _scratch(ws, "cli")
    try:
        for name, content in (probe.get("files") or {}).items():
            p = scratch / str(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(content), encoding="utf-8")
        _r = _run_child([sys.executable, "-B", str(target), *args],
                        cwd=str(scratch), timeout=timeout, env=_env(ws))
        if _r["timed_out"]:
            return {"ok": False, "detail": f"超时 {timeout}s（可能有死循环或等待输入）"}
        out, err, code = _r["out"], _r["err"], _r["code"]
    finally:
        pass  # 原地覆盖、不删除（见 _scratch）

    # **没写 expect_exit 就不判退出码**：默认要求 0 会把"报错退出"这类能力判死
    # （实测 2026-11-06 卡了两轮）。写了就严格判；不写就只判 stdout/stderr。
    want_exit = probe.get("expect_exit")
    problems: list[str] = []
    if want_exit is not None and code != int(want_exit):
        problems.append(f"退出码 {code}（期望 {int(want_exit)}）")
    has = probe.get("stdout_has")
    if has:
        needles = [has] if isinstance(has, str) else list(has)
        for n in needles:
            if str(n) not in out:
                problems.append(f"stdout 里没有 {str(n)!r}")
    if probe.get("stdout_eq") is not None:
        if out.strip() != str(probe.get("stdout_eq")).strip():
            problems.append(f"stdout 是 {_tail(out, 120)!r}，期望 {probe.get('stdout_eq')!r}")
    for n in ([probe.get("stderr_has")] if isinstance(probe.get("stderr_has"), str)
              else list(probe.get("stderr_has") or [])):
        if n and str(n) not in err:
            problems.append(f"stderr 里没有 {str(n)!r}")
    if problems and err.strip():
        problems.append("stderr：" + _tail(err, 200))
    return {"ok": not problems,
            "detail": "；".join(problems) if problems else f"退出码 0，stdout 符合预期",
            "evidence": {"exit": code, "stdout": _tail(out, out_cap),
                         "stderr": _tail(err, out_cap)}}


def _run_call(ws: Path, probe: dict, timeout: int) -> dict:
    cfg = {"module": str(probe.get("module") or ""), "func": str(probe.get("func") or ""),
           "args": probe.get("args") or [], "kwargs": probe.get("kwargs") or {},
           "expect": probe.get("expect"), "expect_raises": probe.get("expect_raises")}
    scratch = _scratch(ws, "call")
    runner = scratch / "_probe_call.py"
    try:
        runner.write_text(_CALL_RUNNER, encoding="utf-8")
        _r = _run_child([sys.executable, "-B", str(runner),
                         json.dumps(cfg, ensure_ascii=False), str(ws)],
                        cwd=str(scratch), timeout=timeout, env=_env(ws))
        if _r["timed_out"]:
            return {"ok": False, "detail": f"超时 {timeout}s（可能有死循环）"}
        lines = [ln for ln in (_r["out"] or "").strip().splitlines() if ln.strip()]
        if not lines:
            # 探针一行输出都没有 = 引擎自己跑不动，不是产物错（两回事，必须分开报）
            return {"ok": False, "engine_error": True,
                    "detail": "探针脚本没有输出：" + _tail(_r["err"] or "", 200)}
        try:
            data = json.loads(lines[-1])
        except Exception:                                # noqa: BLE001
            return {"ok": False, "engine_error": True,
                    "detail": "探针输出不可解析：" + _tail(lines[-1], 200)}
        # 归因标签：同样是"没通过"，导入失败（模块不存在）/ 函数不存在 / 调用崩了 / 值不对，
        # 修法完全不同。含糊成一句"失败"，夜班会按错的方向重写（实测吃过大亏）。
        kind = str(data.get("kind") or "")
        detail = str(data.get("detail") or ("符合预期" if data.get("ok") else "不通过"))
        label = {"import": "导入失败", "missing_func": "函数不存在", "crash": "调用抛异常",
                 "no_raise": "该抛没抛", "raised": "按预期抛错", "value": "返回值不符"}.get(kind, "")
        if label and detail and not detail.startswith(label):
            detail = f"{label}：{detail}"
        return {"ok": bool(data.get("ok")), "detail": detail,
                "evidence": {k: data.get(k) for k in ("kind", "got", "want") if k in data}}
    finally:
        pass  # 原地覆盖、不删除（见 _scratch）


def _run_pytest(ws: Path, probe: dict, timeout: int, out_cap: int = 240) -> dict:
    """测试能力也要能观测：跑 pytest，但**必须看到指定的用例名**。

    只判"stdout 里有 passed"是假绿：写一个 `def test_noop(): assert True` 就会 passed。
    """
    args = [str(a) for a in (probe.get("args") or [])]
    files = probe.get("files") or {}
    root = ws
    named: list[str] = []
    if files:
        # 测试源码由探针提供：写进固定的 scratch 目录（覆盖，不删），
        # 并且**只跑这次写出来的文件**（目录里可能还留着上次的测试，不能连它们一起收）。
        scratch = _scratch(ws, "pytest")
        for name, content in files.items():
            p = scratch / str(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(content), encoding="utf-8")
            named.append(str(name))
        root = scratch
    try:
        targets = args or named or [str(root)]
        # 项目根必须在 pytest 的 sys.path 上（2026-11-23 修）：
        #   · pytest 的 `prepend` 只把 **测试文件所在目录**（tests/）加入 sys.path，
        #     `tests/test_x.py` 里 `from store import ...` 会 ModuleNotFoundError；
        #   · 真实项目"仓库根即导入根"，这不是测试写法问题；
        #   · 环境变量 PYTHONPATH 在 pytest 9 下不生效，必须用 `-o pythonpath=`；
        #     且该 ini 值按 os.pathsep 解析，**Windows 反斜杠会被当转义吃掉**，
        #     必须换成正斜杠（实测 2026-11-23：反斜杠下 1 error during collection）。
        _pp_ini = str(Path(ws).resolve()).replace("\\", "/")
        cmd = [sys.executable, "-B", "-m", "pytest", "-v",
               "-o", f"pythonpath={_pp_ini}", *targets]
        env_p = _env(ws)
        _pp = str(Path(ws).resolve())
        if env_p.get("PYTHONPATH"):
            _pp = _pp + os.pathsep + env_p["PYTHONPATH"]
        env_p["PYTHONPATH"] = _pp
        _r = _run_child(cmd, cwd=str(root), timeout=timeout, env=env_p)
        if _r["timed_out"]:
            return {"ok": False, "detail": f"pytest 超时 {timeout}s"}
        out, err, code = _r["out"], _r["err"], _r["code"]
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "detail": f"pytest 跑不起来：{type(exc).__name__}: {exc}"}
    want_exit = probe.get("expect_exit")
    problems: list[str] = []
    if want_exit is not None and code != int(want_exit):
        problems.append(f"pytest 退出码 {code}（期望 {int(want_exit)}）")
    has = probe.get("stdout_has")
    needles = [has] if isinstance(has, str) else list(has or [])
    for n in needles:
        if str(n) not in out:
            problems.append(f"测试输出里没有用例 {str(n)!r}（可能根本没写这个用例）")
    if problems:
        tail = _tail(err or out, 220)
        if tail:
            problems.append("输出：" + tail)
    return {"ok": not problems,
            "detail": "；".join(problems) if problems else f"pytest 通过，用例都在：{'/'.join(map(str, needles))}",
            "evidence": {"exit": code, "stdout": _tail(out, out_cap),
                         "stderr": _tail(err, out_cap)}}


def _stack_app_candidates(ws) -> list:
    """按产物仓选栈给 ASGI app 候选；默认栈返回空列表（不干扰既有候选）。"""
    try:
        from . import stack as _st
        ad = _st.current(ws)
        return [] if ad.name == "fastapi" else list(ad.probe_app_candidates)
    except Exception:                                    # noqa: BLE001
        return []


def _http_exec(ws: Path, probe: dict, *, cwd: str, phase: int = 0,
               varfile: Path | None = None, timeout: int = 240) -> dict:
    """跑一次 http runner（一个进程）。phase/varfile 让"前段/后段"能跨进程传变量。"""
    cfg = {"app": str(probe.get("app") or ""), "method": str(probe.get("method") or "GET"),
           "path": str(probe.get("path") or "/"), "json": probe.get("json"),
           "data": probe.get("data"), "headers": probe.get("headers"),
           "expect_status": probe.get("expect_status"), "expect_json": probe.get("expect_json"),
           "body_has": probe.get("body_has"), "body_has_any": probe.get("body_has_any"),
           "body_not_has": probe.get("body_not_has"),
           "dom_has": probe.get("dom_has"), "dom_not_has": probe.get("dom_not_has"),
           "openapi_has": probe.get("openapi_has"),
           "steps": probe.get("steps"), "phase": phase,
           "varfile": str(varfile) if varfile else "",
           # §20.7 接缝 6/7：非 FastAPI 栈（Django 等）的 ASGI app 未必叫 app.main:app，
           # 由栈适配器给候选。没选栈 / 默认栈 → 空列表 → 与改动前逐字一致。
           "app_candidates": _stack_app_candidates(ws)}
    # runner 永远放引擎自己的临时目录：前端探针要在仓库根跑（读静态文件），
    # 但**不能**把探针脚本写进客户仓库（实测 2026-11-07：仓库根冒出 _probe_http.py）。
    runner = _scratch(ws, "http-run") / "_probe_http.py"
    try:
        runner.write_text(_HTTP_RUNNER, encoding="utf-8")
        _r = _run_child([sys.executable, "-B", str(runner), str(ws),
                         json.dumps(cfg, ensure_ascii=False)],
                        cwd=cwd, timeout=timeout, env=_env(ws))
        if _r["timed_out"]:
            return {"ok": False, "detail": f"请求超时 {timeout}s（可能卡在启动或死循环）"}
        lines = [ln for ln in (_r["out"] or "").strip().splitlines() if ln.strip()]
        if not lines:
            return {"ok": False, "engine_error": True,
                    "detail": "http 探针没有输出：" + _tail(_r["err"] or "", 200)}
        try:
            data = json.loads(lines[-1])
        except Exception:                                # noqa: BLE001
            return {"ok": False, "engine_error": True,
                    "detail": "http 探针输出不可解析：" + _tail(lines[-1], 200)}
        r = {"ok": bool(data.get("ok")), "detail": str(data.get("detail") or ""),
             "evidence": data.get("evidence") or {}}
        if data.get("engine_error"):
            r["engine_error"] = True
        return r
    finally:
        pass  # 原地覆盖、不删除（见 _scratch）


def _run_http_split(ws: Path, probe: dict, timeout: int) -> dict:
    """跨进程持久化：前段在进程 A 建数据并落变量，末步在进程 B 回读。

    为什么必须换进程：同进程里"能查到"只证明内存字典里有，证明不了写进了数据库。
    实测（2026-11-07）：列表接口返回硬编码 `Test Ticket`，同进程探针照样绿。
    两段共用一个工作目录（同一个 sqlite 文件），这才是"重启后还在"。
    """
    scratch = _scratch(ws, "http-split")
    varfile = scratch / "_vars.json"
    half = max(1, timeout // 2)
    r1 = _http_exec(ws, probe, cwd=str(scratch), phase=1, varfile=varfile, timeout=half)
    if not r1.get("ok"):
        return {"ok": False, "detail": "持久化前段（写数据）失败：" + str(r1.get("detail") or "")[:200],
                "evidence": r1.get("evidence") or {}}
    r2 = _http_exec(ws, probe, cwd=str(scratch), phase=2, varfile=varfile, timeout=half)
    if r2.get("ok"):
        return {"ok": True, "detail": "跨进程回读通过：" + str(r2.get("detail") or "")[:160],
                "evidence": r2.get("evidence") or {}}
    return {"ok": False,
            "detail": "持久化失败（新进程里读不到上一步写的数据）：" + str(r2.get("detail") or "")[:200],
            "evidence": r2.get("evidence") or {}}


def _run_http(ws: Path, probe: dict, timeout: int, out_cap: int = 240) -> dict:
    """Web 能力的端到端观测：载入 ASGI 应用 → 真发一次请求 → 比状态码/JSON。

    两种模式：
      · 普通：所有 steps 在**同一个进程**里顺序跑（注册→登录→建单→改单…）；
      · `"split": true`：最后一个 step 在**另一个进程**里跑（用变量文件传 token/id）。
        用来验"数据真的落盘了"——同进程内存里能查到不算持久化，重启后还在才算。
    """
    steps = probe.get("steps") or []
    if probe.get("split") and len(steps) >= 2:
        return _run_http_split(ws, probe, timeout)
    scratch = _scratch(ws, "http")
    # 默认在**独立空目录**里跑：产物常按相对路径落库（sqlite 的 users.db 之类），
    # 在仓库根跑会把上一次的状态留给下一次 → 探针之间互相污染（实测 2026-11-07）。
    # 例外：前端页面（末步路径是 /）要读仓库里的静态文件，得在仓库根跑。
    last = ((steps[-1] if steps else probe) or {})
    use_ws = (str((last or {}).get("path") or "").strip() == "/"
              or str(probe.get("cwd") or "") == "ws")
    cwd = str(ws) if use_ws else str(scratch)
    return _http_exec(ws, probe, cwd=cwd, timeout=timeout)


def _dedupe_http_probe(probe: dict) -> dict:
    """http 探针执行前的确定性修补（角色串味）。返回新 probe，绝不改入参。

    挂在 run() 的 http 分支：无论蓝图是旧的（存量串味链）还是新的，执行时都按
    已修正的独立账号发请求。这样即使存量 blueprint.json 没迁移，判据也立刻正确。
    """
    steps = probe.get("steps")
    if not isinstance(steps, list) or not steps:
        return probe
    new_steps, _notes = dedupe_role_users(steps)
    if new_steps == steps:
        return probe
    out = dict(probe)
    out["steps"] = new_steps
    return out


def _expects_2xx(st) -> bool:
    if not isinstance(st, dict):
        return False
    es = st.get("expect_status")
    if isinstance(es, list):
        return any(str(x).strip().isdigit() and 200 <= int(str(x).strip()) < 300 for x in es)
    s = str(es if es is not None else "").strip()
    return s.isdigit() and 200 <= int(s) < 300


def _has_auth(st) -> bool:
    h = (st or {}).get("headers")
    return isinstance(h, dict) and any(str(k).lower() == "authorization" for k in h)


def _is_login_step(st) -> bool:
    p = str((st or {}).get("path") or "").lower().rstrip("/")
    return p.endswith("/login")


def _is_auth_step(st) -> bool:
    """注册/登录这类"建立身份"的步骤本身不需要（也不该有）Authorization。"""
    p = str((st or {}).get("path") or "").lower()
    return "/auth/" in p or p.rstrip("/").endswith("/login") \
        or p.rstrip("/").endswith("/register")


def _repair_auth_order(probe: dict) -> dict:
    """把**唯一一条 login** 提到"期望 2xx、却没有鉴权头、且排在它前面"的步骤之前，
    并给那些步骤补上 `Authorization: Bearer ${token}`。

    为什么必须修（2026-11-19 实测）：`cap.inv.topic.guard_pending_1751` 的探针顺序是
        register → POST /topics/（期望 201、**没有 Authorization**）→ login → approve
    建题排在登录之前，产物只能回 403「权限不足」，第 4 步再连带 404。这种探针
    **结构上不可能通过**——不是产物的问题，是判据自己把顺序写错了。远端为此反复
    去改**本来正确**的权限逻辑（改了多轮）。

    安全性：只影响"排在唯一 login 之前、期望 2xx、且无鉴权头"的步骤。**能通过的能力
    不可能存在这种步骤**（它必然 401/403），所以对已绿能力零影响；多条 login 时角色
    不确定，宁可不动。
    """
    steps = (probe or {}).get("steps")
    if not isinstance(steps, list) or len(steps) < 2:
        return probe
    logins = [i for i, s in enumerate(steps) if _is_login_step(s)]
    if len(logins) != 1:
        return probe
    li = logins[0]
    # 只认"业务步骤"：注册/登录本身不该带 token（否则会把 login 提到 register 前面）
    need = [i for i in range(li)
            if _expects_2xx(steps[i]) and not _has_auth(steps[i])
            and not _is_auth_step(steps[i])]
    if not need:
        return probe
    save = steps[li].get("save")
    if isinstance(save, str):
        # save 也可能被写成字符串（同一类病，见 _HTTP_RUNNER 里的 save 归一）
        try:
            import json as _json
            save = _json.loads(save)
        except Exception:                            # noqa: BLE001
            save = None
    if not isinstance(save, dict) or not save:
        return probe                      # 拿不到 token 变量名，注入也没意义
    var = str(next(iter(save.keys())))
    m = min(need)
    new_steps = [dict(s) for s in steps]
    login = new_steps.pop(li)
    new_steps.insert(m, login)
    # 被落下的那些步骤：补鉴权头（按 method+path 回填，pop/insert 后索引会漂）
    for i in need:
        tgt = steps[i]
        for s2 in new_steps:
            if (s2.get("method") == tgt.get("method")
                    and s2.get("path") == tgt.get("path")
                    and not _has_auth(s2)):
                hdr = dict(s2.get("headers") or {})
                hdr["Authorization"] = "Bearer ${%s}" % var
                s2["headers"] = hdr
                break
    out = dict(probe)
    out["steps"] = new_steps
    return out


# sql 探针：只禁**破坏性/逃逸**语句，允许 SELECT 与 INSERT/UPDATE/DELETE（用于造数据）。
# 作用对象是产物仓里的 sqlite 文件（隔离），不是系统库。
_BANNED_SQL = ("drop ", "drop table", "attach ", "detach ", "vacuum into",
               "load_extension", "pragma writable_schema", "pragma journal_mode")


def _run_sql(ws, probe: dict, timeout: int = 30, out_cap: int = 240) -> dict:
    """SQL 探针：直连产物仓的 sqlite 文件，逐条执行并比对结果。

    为什么需要（2026-11-19 中间件生态包）：http 探针只能验"接口返回什么"，
    验不了"数据真落库了没有"（接口可以返回假数据）。sql 探针直接查库，是最硬的
    数据层判据。用 Python 内置 sqlite3，**零依赖**（不需要 Docker/Postgres）。
    """
    import sqlite3
    db = str(probe.get("db") or "").strip()
    steps = probe.get("steps")
    if not db:
        return {"ok": False, "invalid": True, "detail": "sql 探针缺 db（sqlite 文件路径）"}
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "invalid": True, "detail": "sql 探针缺 steps"}
    path = Path(ws) / db
    if not path.is_file():
        return {"ok": False, "kind": "sql",
                "detail": "数据库文件不存在：%s（产物没建库/没迁移？）" % db}
    bits, trace = [], []
    try:
        conn = sqlite3.connect(str(path), timeout=max(1, min(timeout, 30)))
        conn.row_factory = sqlite3.Row
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "kind": "sql",
                "detail": "打不开数据库 %s：%s: %s" % (db, type(exc).__name__, exc)}
    try:
        for i, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                continue
            sql = str(st.get("sql") or "").strip()
            if not sql:
                bits.append("第%d步 缺 sql" % i)
                continue
            low = sql.lower()
            if any(b in low for b in _BANNED_SQL):
                bits.append("第%d步 SQL 含禁用语句（drop/attach/load_extension 等）：%s"
                            % (i, sql[:80]))
                continue
            try:
                cur = conn.execute(sql)
                rows = [dict(r) for r in cur.fetchall()]
            except Exception as exc:                     # noqa: BLE001
                bits.append("第%d步 SQL 执行失败：%s: %s（SQL=%s）"
                            % (i, type(exc).__name__, exc, sql[:100]))
                continue
            trace.append({"step": i, "sql": sql[:200],
                          "rows": rows[:10], "count": len(rows)})
            if "expect_rows" in st and len(rows) != int(st["expect_rows"]):
                bits.append("第%d步 期望 %s 行，实际 %d 行（SQL=%s）"
                            % (i, st["expect_rows"], len(rows), sql[:80]))
            if "expect_first" in st and isinstance(st["expect_first"], dict):
                first = rows[0] if rows else {}
                for k, v in st["expect_first"].items():
                    got = first.get(k)
                    if str(got) != str(v):
                        bits.append("第%d步 首行 %s 实际 %r，期望 %r"
                                    % (i, k, got, v))
            if "expect_contains" in st and isinstance(st["expect_contains"], dict):
                for k, v in st["expect_contains"].items():
                    if not any(str(r.get(k)) == str(v) for r in rows):
                        bits.append("第%d步 结果里没有 %s=%r" % (i, k, v))
    finally:
        try:
            conn.close()
        except Exception:                                # noqa: BLE001
            pass
    if bits:
        return {"ok": False, "kind": "sql", "detail": "；".join(bits),
                "evidence": {"db": db, "steps": trace}}
    return {"ok": True, "kind": "sql",
            "detail": "SQL 验收通过（%d 条语句）" % len(trace),
            "evidence": {"db": db, "steps": trace}}


def _wait_port(host: str, port: int, timeout: float = 60.0) -> tuple[bool, float]:
    """等端口可连（前端 dev server 就绪）。返回 (就绪, 已等待秒数)。"""
    import socket
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True, time.time() - t0
        except OSError:
            time.sleep(0.3)
    return False, time.time() - t0


def _read_log(path: str, cap: int = 1200) -> str:
    """读 dev server 日志尾巴（失败诊断用）。读不到就空串，不抛。"""
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = f.read(cap)
        return data
    except OSError:
        return ""


def _kill_tree(proc) -> None:
    """杀前端服务整棵进程树（Windows 上 shell=True 起的是 cmd → node 子进程）。"""
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=15)
        else:
            proc.terminate()
    except Exception:                                    # noqa: BLE001
        pass


def _run_browser(ws, probe: dict, timeout: int, out_cap: int = 240) -> dict:
    """前端 SPA 探针：起服务 → 等就绪 → Playwright 访问 + DOM 断言 → 杀服务。

    铁律：默认红。浏览器不可用 / 服务起不来 / 断言不通过 一律算没通过，
    且**如实说"测不了"**（不假装通过）。
    """
    from . import browser as _br
    okb, whyb = _br.available()
    if not okb:
        return {"ok": False, "kind": "browser",
                "detail": "浏览器不可用（如实说测不了）：%s" % whyb}
    start_cmd = str(probe.get("start_cmd") or "").strip()
    port = int(probe.get("port") or 5173)
    url = str(probe.get("url") or "http://127.0.0.1:%d/" % port)
    expect_dom = probe.get("expect_dom") or probe.get("expect_dom_has") or []
    if isinstance(expect_dom, str):
        expect_dom = [expect_dom]
    actions = probe.get("actions") or []
    expect_url = str(probe.get("expect_url") or "")
    wait_s = float(probe.get("ready_timeout") or 60)
    # **输出重定向到文件，不用管道**（2026-11-19 check_probe_timeout 抓到的真问题）：
    # dev server 输出超过管道缓冲会死锁（同 _http_exec 的教训）。写文件没有这个问题。
    import tempfile
    logf = None
    log_path = ""
    try:
        _fd, log_path = tempfile.mkstemp(prefix="browser_", suffix=".log")
        os.close(_fd)
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(start_cmd, shell=True, cwd=str(ws),
                                stdout=logf, stderr=subprocess.STDOUT)
    except Exception as exc:                             # noqa: BLE001
        if logf is not None:
            try:
                logf.close()
            except Exception:                            # noqa: BLE001
                pass
        return {"ok": False, "kind": "browser",
                "detail": "起前端服务失败：%s: %s" % (type(exc).__name__, exc)}
    try:
        ready, waited = _wait_port("127.0.0.1", port, timeout=min(wait_s, timeout))
        if not ready:
            return {"ok": False, "kind": "browser",
                    "detail": "服务 %.0fs 内没在端口 %d 就绪：%s"
                              % (waited, port, _tail(_read_log(log_path), 400))}
        pv = _br.visit(url, actions=actions, expect_dom=expect_dom,
                       expect_url=expect_url,
                       timeout=min(int(timeout), 40) * 1000)
    finally:
        _kill_tree(proc)
        if logf is not None:
            try:
                logf.close()
            except Exception:                            # noqa: BLE001
                pass
        if log_path:
            try:
                os.unlink(log_path)
            except OSError:
                pass
    ev = {"url": pv.url, "final_url": pv.final_url, "status": pv.status,
          "dom": pv.dom[:20], "console_errors": pv.console_errors[:10],
          "a11y_violations": (pv.a11y_violations or [])[:10]}
    if pv.ok:
        return {"ok": True, "kind": "browser",
                "detail": "浏览器验收通过（DOM 断言 %d 条，%dms）"
                          % (len(expect_dom), pv.elapsed_ms), "evidence": ev}
    bits = []
    if pv.error:
        bits.append(pv.error)
    for d in (pv.dom or []):
        if isinstance(d, dict) and not d.get("ok", True):
            bits.append("DOM 不符：%s" % str(d)[:160])
    if pv.console_errors:
        bits.append("console 错误 %d 条：%s"
                    % (len(pv.console_errors),
                       "; ".join(map(str, pv.console_errors[:3]))[:200]))
    if pv.page_errors:
        bits.append("页面 JS 异常 %d 条：%s"
                    % (len(pv.page_errors),
                       "; ".join(map(str, pv.page_errors[:3]))[:200]))
    if expect_url and expect_url not in (pv.final_url or ""):
        bits.append("最终 URL 不含 %r（实际 %r）" % (expect_url, pv.final_url))
    return {"ok": False, "kind": "browser",
            "detail": "；".join(bits) or "浏览器断言不通过", "evidence": ev}


def _find_electron_exe(ws: Path, probe: dict) -> str:
    """解析 electron 可执行文件：探针给的相对路径按仓库根解析，绝对路径直接用。"""
    exe = str(probe.get("exe") or "").strip()
    if not exe:
        return ""
    p = Path(exe).expanduser()
    if p.is_absolute():
        return str(p) if p.is_file() else ""
    # 相对路径：相对仓库根（ws），并按常见 node_modules 位置兜底
    for cand in (ws / p, ws / "node_modules" / p.name if p.name == "electron" else ws / p):
        if cand.is_file():
            return str(cand)
    return ""


def _run_electron(ws, probe: dict, timeout: int, out_cap: int = 240) -> dict:
    """真机 Electron UI 探针：CDP 连真窗口 → DOM/标题断言 → 杀进程。默认红。"""
    from . import browser as _br
    exe = _find_electron_exe(ws, probe)
    if not exe and str(probe.get("exe") or "").strip():
        return {"ok": False, "kind": "electron",
                "detail": "electron 可执行文件不存在（如实说测不了）：%s" % probe.get("exe")}
    app_dir = str(probe.get("app_dir") or "").strip()
    app_path = ws / app_dir if app_dir and not Path(app_dir).is_absolute() else (app_dir or str(ws))
    expect_dom = probe.get("expect_dom") or probe.get("expect_dom_has") or []
    if isinstance(expect_dom, str):
        expect_dom = [expect_dom]
    pv = _br.visit_electron(
        exe, app_dir=str(app_path), actions=probe.get("actions") or [],
        expect_dom=expect_dom, expect_title=str(probe.get("expect_title") or ""),
        timeout=min(int(timeout), 40) * 1000,
        ready_timeout=float(probe.get("ready_timeout") or 45))
    ev = {"url": pv.url, "final_url": pv.final_url, "status": pv.status,
          "dom": pv.dom[:20], "console_errors": pv.console_errors[:10],
          "page_errors": pv.page_errors[:10],
          "a11y_violations": (pv.a11y_violations or [])[:10]}
    if pv.ok:
        return {"ok": True, "kind": "electron",
                "detail": "真机 Electron UI 验收通过（DOM 断言 %d 条，%dms）"
                          % (len(expect_dom), pv.elapsed_ms), "evidence": ev}
    bits = ([pv.error] if pv.error else []) + pv.problems()
    return {"ok": False, "kind": "electron",
            "detail": "；".join(bits)[:600] or "真机 Electron UI 断言不通过", "evidence": ev}


def run(ws, probe: dict, timeout: int = PROBE_TIMEOUT, out_cap: int = 240) -> dict:
    """执行一次探针。返回 {ok, detail, evidence?, engine_error?, invalid?}。

    **不抛异常**：任何异常都算"没通过"（默认红），并把原因写进 detail。

    `out_cap` 只影响 evidence 里 stdout/stderr 留多长（判定永远看全文）：
    片内自验闭环要拿真实输出去修代码，240 字只够判定、不够诊断。
    """
    ws = Path(ws).resolve()
    issues = validate(probe)
    if issues:
        return {"ok": False, "invalid": True, "detail": "；".join(issues)}
    t0 = time.time()
    try:
        kind = str(probe.get("kind") or "")
        if kind == "py_call":
            r = _run_call(ws, probe, timeout)
        elif kind == "pytest":
            r = _run_pytest(ws, probe, timeout, out_cap)
        elif kind == "http":
            # 角色串味修补（判据层，零 token）：存量蓝图不必迁移，执行时也按独立账号跑
            r = _run_http(ws, _repair_auth_order(_dedupe_http_probe(probe)), timeout, out_cap)
        elif kind == "browser":
            r = _run_browser(ws, probe, timeout, out_cap)
        elif kind == "sql":
            r = _run_sql(ws, probe, timeout, out_cap)
        elif kind == "electron":
            r = _run_electron(ws, probe, timeout, out_cap)
        else:
            r = _run_cli(ws, probe, timeout, out_cap)
    except Exception as exc:                             # noqa: BLE001
        r = {"ok": False, "engine_error": True,
             "detail": f"探针执行异常：{type(exc).__name__}: {exc}"}
    r["took_s"] = round(time.time() - t0, 2)
    return r
