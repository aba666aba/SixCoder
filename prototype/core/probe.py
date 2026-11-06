"""覆盖探针：把一条能力变成一次**真的执行**，看它到底在不在。

两条铁律（都在这一层执行，不靠调用方自律）：
1. **默认红**。没跑、跑不动、输出解析不了 → 一律算"没通过"。
   只有拿到确定的观测才算 done。沿用 props.run 的纪律：引擎自己出错（探针脚本跑不起来）
   要跟"产物有 bug"分开报，否则夜班会反复重写本来就正确的代码。
2. **空壳必须过不了**。只判退出码、只判"能调用"、期望值恒真 —— 这些在校验阶段就拒收。
   实测过的形态：模型交 4 个文件 16 行 `raise NotImplementedError`，退出码全 0。

支持两种探针：
  cli     —— 跑入口脚本，比对 stdout（用户真能用的那条路）
  py_call —— import 模块、调函数、比返回值（零件级，也是最小可观测单元）
其余种类（http 等）现在**没有执行器**，一律算 unsupported，永远不算 done（不假装支持）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROBE_KINDS = ("cli", "py_call", "pytest")
PROBE_TIMEOUT = 60

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
    """探针的临时工作目录（**不许**把探针的垃圾留在客户仓库里）。"""
    d = ws / ".sixcoder" / "tmp" / f"probe-{tag}-{os.getpid()}-{int(time.time() * 1000) % 100000}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env(ws: Path) -> dict:
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONUTF8": "1"}
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
        _rmtree(scratch)

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
        _rmtree(scratch)


def _run_pytest(ws: Path, probe: dict, timeout: int, out_cap: int = 240) -> dict:
    """测试能力也要能观测：跑 pytest，但**必须看到指定的用例名**。

    只判"stdout 里有 passed"是假绿：写一个 `def test_noop(): assert True` 就会 passed。
    """
    args = [str(a) for a in (probe.get("args") or [])]
    files = probe.get("files") or {}
    root = ws
    scratch = None
    if files:
        # 测试源码由探针提供时，放进临时目录跑（不污染客户仓库）
        scratch = _scratch(ws, "pytest")
        for name, content in files.items():
            p = scratch / str(name)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(content), encoding="utf-8")
        root = scratch
    try:
        cmd = [sys.executable, "-B", "-m", "pytest", "-v", *(args or [str(root)])]
        try:
            proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=timeout,
                                  env=_env(ws))
        except subprocess.TimeoutExpired:
            return {"ok": False, "detail": f"pytest 超时 {timeout}s"}
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
    finally:
        if scratch is not None:
            _rmtree(scratch)
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
        else:
            r = _run_cli(ws, probe, timeout, out_cap)
    except Exception as exc:                             # noqa: BLE001
        r = {"ok": False, "engine_error": True,
             "detail": f"探针执行异常：{type(exc).__name__}: {exc}"}
    r["took_s"] = round(time.time() - t0, 2)
    return r
