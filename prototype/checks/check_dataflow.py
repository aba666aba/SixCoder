"""数据流/来源链自检：能核到代码事实的才叫证据，核不到就必须说"没有证据"。

这条钉的是"能力上限"里的数据流那一格（也钉住它**不是**污点追踪）：
    · 有调用点 → 必须报出文件、行号、实参表达式（这是可核的代码事实）；
    · 没有调用点 → evidence=no_evidence，报告必须写"没有证据"，不许猜"流到哪儿了"；
    · getattr 动态调用 → 记 dynamic_only，**不算**来源证据；
    · 参数个数按签名截断（多传的实参不硬塞给某个参数）；
    · 报告行必须写明"只做来源链，不是污点追踪"。

跑法: python checks/check_dataflow.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import dataflow  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

CLEAN = '''def clean_row(raw, unused):
    """清理一行：参数被改了两次。"""
    raw = dict(raw)
    raw["name"] = str(raw.get("name", "")).strip()
    return raw
'''

CALLER = '''from clean import clean_row


def pipeline(rows):
    out = []
    for r in rows:
        out.append(clean_row(r, "x"))
    return out
'''

DYNAMIC = '''import importlib

from clean import clean_row


def by_name(payload):
    fn = getattr(importlib.import_module("clean"), "clean_row")
    return fn(payload, "x")
'''

DYNAMIC_UNKNOWN = '''import importlib

from clean import clean_row


def by_var(fn_name, payload):
    mod = importlib.import_module("clean")
    fn = getattr(mod, fn_name)
    return fn(payload, "x")
'''

UNUSED = '''def lonely(value):
    """没人调用它。"""
    return value
'''


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws(files: dict) -> Path:
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix="dataflow_"))
    for name, src in files.items():
        (ws / name).write_text(src, encoding="utf-8")
    return ws


def main() -> int:
    print("数据流/来源链自检")

    print("[1] 有调用点：必须给出文件、行号、实参表达式")
    ws = _ws({"clean.py": CLEAN, "pipeline.py": CALLER})
    r = dataflow.trace(ws, "clean.py", "clean_row")
    check("trace 判绿（有来源证据）", bool(r.get("ok")), str(r.get("notes")))
    params = {p["name"]: p for p in r.get("params") or []}
    check("签名里的两个参数都列出来", set(params) == {"raw", "unused"}, str(sorted(params)))
    raw = params.get("raw") or {}
    check("拿到调用点文件", any("pipeline.py" in c["file"] for c in raw.get("callers") or []),
          str(raw.get("callers")))
    check("拿到实参表达式（第 1 个实参是 r）",
          (raw.get("callers") or [{}])[0].get("expr") == "r", str(raw.get("callers")))
    check("拿到行号", int((raw.get("callers") or [{}])[0].get("line") or 0) > 0,
          str(raw.get("callers")))
    check("参数 2 拿到第 2 个实参", (params.get("unused", {}).get("callers") or [{}])[0].get("expr")
          == "'x'", str(params.get("unused")))
    check("变换次数按函数体里的重新赋值统计", int(params.get("raw", {}).get("transforms") or 0) >= 1,
          str(params.get("raw", {}).get("transforms")))
    check("报告行给出能核到调用点的参数个数", "能核到调用点" in dataflow.report_lines(r)[0],
          dataflow.report_lines(r)[0])
    check("报告行明说不是污点追踪", "不是污点追踪" in dataflow.report_lines(r)[0],
          dataflow.report_lines(r)[0])

    print("[2] 没有调用点：必须写'没有证据'，不许猜")
    ws2 = _ws({"lonely.py": UNUSED})
    r2 = dataflow.trace(ws2, "lonely.py", "lonely")
    check("trace 判红（没有来源证据）", r2.get("ok") is False, str(r2))
    check("evidence 标成 no_evidence",
          all(p["evidence"] == "no_evidence" for p in r2.get("params") or []),
          str([p["evidence"] for p in r2.get("params") or []]))
    check("notes 里写明找不到调用点", any("找不到调用点" in n for n in r2.get("notes") or []),
          str(r2.get("notes")))

    print("[3] getattr 动态调用：分清'确认是它'与'可能命中'，两者都不算来源证据")
    ws3 = _ws({"clean.py": CLEAN, "dyn.py": DYNAMIC})
    r3 = dataflow.trace(ws3, "clean.py", "clean_row")
    check("字符串名动态调用不算有证据", r3.get("ok") is False, str(r3.get("ok")))
    check("evidence 标成 dynamic_only",
          all(p["evidence"] == "dynamic_only" for p in r3.get("params") or []),
          str([p["evidence"] for p in r3.get("params") or []]))
    check("notes 里点明 getattr 不含实参证据",
          any("getattr" in n for n in r3.get("notes") or []), str(r3.get("notes")))

    ws3b = _ws({"clean.py": CLEAN, "dynv.py": DYNAMIC_UNKNOWN})
    r3b = dataflow.trace(ws3b, "clean.py", "clean_row")
    check("变量名动态调用不算有证据", r3b.get("ok") is False, str(r3b.get("ok")))
    check("变量名动态调用标成 dynamic_maybe（不许当成'确认命中'）",
          all(p["evidence"] == "dynamic_maybe" for p in r3b.get("params") or []),
          str([p["evidence"] for p in r3b.get("params") or []]))
    check("notes 里写明'静态无法确认'",
          any("无法确认" in n for n in r3b.get("notes") or []), str(r3b.get("notes")))

    print("[4] 调用图统计：找不到定义的调用要计入 unresolved，不许当成结论")
    g = dataflow.graph(ws)
    check("统计到调用点", int(g.get("calls") or 0) >= 1, str(g))
    check("统计到定义", int(g.get("defined") or 0) >= 1, str(g))
    check("unresolved 有独立计数", "unresolved" in g, str(sorted(g)))
    check("报告行声明 unresolved 属无证据", "无证据" in dataflow.report_lines(g)[0],
          dataflow.report_lines(g)[0])
    check("不存在的模块 → 判红且写明原因",
          dataflow.trace(ws, "nope.py", "x").get("ok") is False, "居然通过了")

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（能核的核到事实 / 核不到的写没证据 / 动态调用不算证据 / 不是污点追踪）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
