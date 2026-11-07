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

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROBE_KINDS = ("cli", "py_call", "pytest", "http")
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
import json, sys, importlib

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

cands = [c for c in ([spec.get("app") or ""] + ["main:app", "app.main:app", "app:app",
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
              "expect_json": spec.get("expect_json"), "body_has": spec.get("body_has")}]

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
    for name, src in (st.get("save") or {}).items():
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
            val = 1                       # 自建种子数据的自增 id 通常就是 1，给个确定值比 None 强
        VAR[str(name)] = val
    want = st.get("expect_status")
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
            bits.append("第%d步 %s %s → 状态码 %s（期望 %s）"
                        % (i, method, path, resp.status_code, want if want is not None else "2xx"))
    ej = st.get("expect_json")
    if ej is not None:
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
                    bits.append("第%d步 JSON 字段不符：%s（实际 %s）"
                                % (i, ",".join(bad), preview({k: got.get(k) for k in bad})))
            elif got != ej:
                bits.append("第%d步 JSON 不等于期望：实际 %s" % (i, preview(got)))
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
out["evidence"] = {"steps": trace, "vars": {k: preview(v) for k, v in VAR.items()}}
out["ok"] = not bits
out["detail"] = "；".join(bits[:4]) or ("%d 步请求全部符合预期（末步 %s %s）"
                                        % (len(trace), trace[-1]["method"], trace[-1]["path"])
                                        if trace else "没有发出任何请求")
done()
'''


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
                      or (st or {}).get("expect_status") is not None]
            if not judged:
                out.append("http 探针每一步都没写判据：至少要有一 step 带 "
                           "expect_status / expect_json / body_has")
        else:
            path = str(probe.get("path") or "").strip()
            if not path.startswith("/"):
                out.append(f"http 探针的 path 必须以 / 开头（现在是 {path or '(空)'}）")
            if not (probe.get("expect_json") is not None or probe.get("body_has")
                    or probe.get("expect_status") is not None):
                out.append("http 探针只有状态码 2xx 一个判据太弱："
                           "必须给 expect_json / body_has / expect_status 之一")
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
        return f"{m} {p}{tail}"
    return f"（未支持的探针种类：{kind or '空'}）"


# ---------------------------------------------------------------- 执行

def _rmtree(p: Path) -> None:
    if not p.exists():
        return
    try:
        shutil.rmtree(p, ignore_errors=True)
    except Exception:                                    # noqa: BLE001
        pass


def _scratch(ws: Path, tag: str) -> Path:
    """探针的临时工作目录（**不许**把探针的垃圾留在客户仓库里）。

    用**固定名字 + 原地覆盖**，不再"每次新建一个再删掉"：
    一次夜班要跑几十上百个探针，每个探针删自己那几个文件，
    很快就会撞上外层"单轮删除 ≥50 个就拦下"的保护，把整次运行打断
    （实测 2026-11-07：夜班在第二片开头 exit 1，一行都没多写）。

    序号是**每次调用自增**：探针之间必须互相隔离。实测（2026-11-07 同一夜）：
    上一片留下的 `users.db` 表结构带 id 列，登录用 `SELECT *` 取到 id=5 当用户名 →
    响应校验炸在作者身上，看起来像它的 bug，其实是探针之间的状态污染。
    """
    global _SCRATCH_SEQ
    _SCRATCH_SEQ += 1
    d = ws / ".sixcoder" / "tmp" / f"probe-{tag}-{os.getpid()}-{_SCRATCH_SEQ}"
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    return env


def _tail(text: str, n: int = 240) -> str:
    t = (text or "").strip()
    return t[-n:] if len(t) > n else t


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
        try:
            proc = subprocess.run([sys.executable, "-B", str(target), *args],
                                  cwd=str(scratch), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  env=_env(ws))
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"超时 {timeout}s（可能有死循环或等待输入）"}
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
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
        try:
            proc = subprocess.run([sys.executable, "-B", str(runner),
                                   json.dumps(cfg, ensure_ascii=False), str(ws)],
                                  cwd=str(scratch), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  env=_env(ws))
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"超时 {timeout}s（可能有死循环）"}
        lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
        if not lines:
            # 探针一行输出都没有 = 引擎自己跑不动，不是产物错（两回事，必须分开报）
            return {"ok": False, "engine_error": True,
                    "detail": "探针脚本没有输出：" + _tail(proc.stderr or "", 200)}
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
        cmd = [sys.executable, "-B", "-m", "pytest", "-v", *targets]
        try:
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  env=_env(ws))
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"pytest 超时 {timeout}s"}
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
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


def _http_exec(ws: Path, probe: dict, *, cwd: str, phase: int = 0,
               varfile: Path | None = None, timeout: int = 240) -> dict:
    """跑一次 http runner（一个进程）。phase/varfile 让"前段/后段"能跨进程传变量。"""
    cfg = {"app": str(probe.get("app") or ""), "method": str(probe.get("method") or "GET"),
           "path": str(probe.get("path") or "/"), "json": probe.get("json"),
           "data": probe.get("data"), "headers": probe.get("headers"),
           "expect_status": probe.get("expect_status"), "expect_json": probe.get("expect_json"),
           "body_has": probe.get("body_has"), "body_has_any": probe.get("body_has_any"),
           "steps": probe.get("steps"), "phase": phase,
           "varfile": str(varfile) if varfile else ""}
    # runner 永远放引擎自己的临时目录：前端探针要在仓库根跑（读静态文件），
    # 但**不能**把探针脚本写进客户仓库（实测 2026-11-07：仓库根冒出 _probe_http.py）。
    runner = _scratch(ws, "http-run") / "_probe_http.py"
    try:
        runner.write_text(_HTTP_RUNNER, encoding="utf-8")
        try:
            proc = subprocess.run([sys.executable, "-B", str(runner), str(ws),
                                   json.dumps(cfg, ensure_ascii=False)],
                                  cwd=cwd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  env=_env(ws))
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"请求超时 {timeout}s（可能卡在启动或死循环）"}
        lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
        if not lines:
            return {"ok": False, "engine_error": True,
                    "detail": "http 探针没有输出：" + _tail(proc.stderr or "", 200)}
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
            r = _run_http(ws, probe, timeout, out_cap)
        else:
            r = _run_cli(ws, probe, timeout, out_cap)
    except Exception as exc:                             # noqa: BLE001
        r = {"ok": False, "engine_error": True,
             "detail": f"探针执行异常：{type(exc).__name__}: {exc}"}
    r["took_s"] = round(time.time() - t0, 2)
    return r
