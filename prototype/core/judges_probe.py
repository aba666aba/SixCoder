"""通用探针判据（2026-11-24）—— 把 probe.py 已有的 7 种探针接进判据体系。

## 解决什么

核对三处注册表时发现的关键断层：

    probe.py  PROBE_KINDS = (cli, py_call, pytest, http, browser, sql, electron)
    judges     只认识 cli_tool / django / fastapi 的判据

**验收能力早就存在**，只是没和"按栈分发判据"这套接上。
于是 go_service / node_express / spring_boot（声明了 probe:cli）、
microservice（probe:sql）、py_lib（probe:py_call）这些包，
adapter 写好了、探针约定写好了，但 judges.for_stack() 里没有对应判据。

## 做法：判据就是"跑这个项目声明的探针"

不在 judges 里重写一遍 cli/browser/sql 的判断逻辑（那是重复），
而是让判据调用 probe.run，把它的结果翻译成 GateResult。

探针从哪来：.sixcoder/probes.json。没有它 = 没有可验收的东西，
**明确说"没配置"，不判通过**。

## 为什么用 ANY_STACK

用哪种探针由**项目实际声明的探针**决定，不由外层栈名决定。
一个 fastapi 项目也可能带 browser 探针（前端页面）。
所以判据对所有栈开放，靠"有没有 probes.json"兜底。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .gates import GateResult
from .judges import ANY_STACK, CtxJudge, JudgeContext, JudgeRegistry

PROBES_FILE = ".sixcoder/probes.json"

PROBE_KINDS = ("cli", "py_call", "pytest", "http", "browser", "sql", "electron")


def load_probes(ws: Path) -> dict:
    """读 .sixcoder/probes.json。失败返回空字典（调用方据此判"没配置"）。"""
    p = Path(ws) / PROBES_FILE
    if not p.is_file():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return d if isinstance(d, dict) else {}


def _items(probes: dict) -> list:
    """把 probes.json 归一到 [(名字, spec)]，兼容 dict 与 list 两种写法。"""
    out = []
    if isinstance(probes, dict):
        for name, spec in probes.items():
            if isinstance(spec, dict):
                out.append((str(name), spec))
    return out


def _run_kind(ctx: JudgeContext, kind: str) -> GateResult:
    """跑这一类探针。任一条不过 = 判据红，并列出失败的探针名。"""
    t0 = time.time()
    probes = load_probes(ctx.ws)
    if not probes:
        return GateResult("probe." + kind, False, "config", "no-probes",
                          "没有 " + PROBES_FILE + "：该栈没有可验收的探针",
                          int((time.time() - t0) * 1000))

    items = _items(probes)
    mine = [(n, s) for n, s in items if str(s.get("kind") or "") == kind]
    if not mine:
        kinds = sorted({str(s.get("kind") or "") for _, s in items})
        return GateResult("probe." + kind, False, "config", "no-probe-of-kind",
                          "probes.json 里没有 kind=" + kind
                          + "（有的种类：" + ", ".join(kinds) + "）",
                          int((time.time() - t0) * 1000))

    from . import probe as _probe
    fails = []
    for name, spec in mine:
        try:
            r = _probe.run(ctx.ws, spec,
                           timeout=int((ctx.extras or {}).get("timeout") or 180))
        except Exception as exc:                      # noqa: BLE001
            fails.append(name + ": 执行异常 " + type(exc).__name__ + ": " + str(exc))
            continue
        if not r.get("ok"):
            fails.append(name + ": " + str(r.get("detail") or "")[:300])

    ms = int((time.time() - t0) * 1000)
    if not fails:
        return GateResult("probe." + kind, True, "probe",
                          str(len(mine)) + "/" + str(len(mine)),
                          str(len(mine)) + " 条 " + kind + " 探针全部通过", ms)
    return GateResult("probe." + kind, False, "probe",
                      str(len(fails)) + " 条失败", "；".join(fails[:4]), ms)


def _make(kind: str):
    def judge(ctx: JudgeContext) -> GateResult:
        return _run_kind(ctx, kind)
    return judge


PROBE_JUDGES = tuple("probe." + k for k in PROBE_KINDS)


def register_probes(reg: JudgeRegistry) -> None:
    """注册 7 条探针判据（每种探针一条）。

    为什么不合成一条：分开能看出"哪类探针没过"
    （cli 过了但 sql 没过，定位完全不同）。
    """
    for kind in PROBE_KINDS:
        reg.register(CtxJudge("probe." + kind, _make(kind),
                              frozenset({ANY_STACK}),
                              "跑 " + kind + " 探针（复用 core/probe.py）"))
