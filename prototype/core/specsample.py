"""验收样例（`--spec-sample`）：把"猜规格"换成"读样例"。

为什么需要（实测 2026-11-07，日志领域七跑不收敛）：
    那个领域里真正卡住人的不是"模型会不会写代码"，而是**规格本身模糊**：
    时间窗边界闭不闭、错误消息用哪个措辞、`--json` 要多紧凑、坏行报第几行……
    这些让模型猜，猜错的代价是整片重做（可自动修复占比掉到 48%）。
    人只要给 3~5 条**可执行**的验收样例（怎么跑、输入什么、该看到什么），
    判据就从"猜"变成"读"，而且这些样例天然是权威——比模型自造的探针可信。

支持两种写法（都解析成同一份结构）：

1) 文本（人手写最省事）
    ### 按 level 过滤
    $ python logscan.py --file d.log --level ERROR
    <<< d.log
    2026-11-06T10:00:00 INFO user=a action=x
    2026-11-06T10:01:00 ERROR user=b action=y
    >>>
    --- expect
    2026-11-06T10:01:00 ERROR user=b action=y

2) JSON 数组（机器生成/复制粘贴）
    [{"name": "按 level 过滤",
      "run": ["--file", "d.log", "--level", "ERROR"],
      "files": {"d.log": "..."},
      "stdout_has": ["2026-11-06T10:01:00 ERROR user=b action=y"],
      "exit": 0}]

语义（刻意保守，绝不猜）：
  · `stdout_has` 是"必须出现的原文"，多条都要出现；
  · `--- expect` 整段当作一条 needle（按原文比对，不做模糊匹配）；
  · 不写 `exit` 就不判退出码（错误路径与成功路径都可能存在）；
  · 每条样例 = 一条能力，id 是 `cap.sampleN`，**依赖为空**（互相独立）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path


def _parse_cmd(cmd: str) -> tuple[str, list[str]]:
    """`python main.py --file d.csv` → ("main.py", ["--file", "d.csv"])。"""
    parts = [p for p in re.split(r'\s+(?=(?:[^"]*"[^"]*")*[^"]*$)', (cmd or "").strip()) if p]
    if not parts:
        return "", []
    if parts[0].lower().endswith(("python", "python.exe", "python3")):
        parts = parts[1:]
    if not parts:
        return "", []
    entry = parts[0].strip('"\'')
    args = [p.strip('"\'') for p in parts[1:]]
    return entry, args


def parse(text: str) -> list[dict]:
    """把样例文本解析成 [{name, entry, args, files, stdout_has, expect_exit}]。"""
    raw = (text or "").strip()
    if not raw:
        return []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            data = json.loads(raw)
        except Exception:                                # noqa: BLE001
            data = None
        items = data if isinstance(data, list) else ((data or {}).get("samples") or [])
        out: list[dict] = []
        for i, it in enumerate(items, 1):
            if not isinstance(it, dict):
                continue
            run = it.get("run") or []
            if isinstance(run, str):
                entry, args = _parse_cmd(run)
            else:
                entry = str(run[0]) if run else ""
                args = [str(a) for a in run[1:]]
            has = it.get("stdout_has")
            out.append({
                "name": str(it.get("name") or f"样例{i}").strip(),
                "entry": entry,
                "args": args,
                "files": {str(k): str(v) for k, v in (it.get("files") or {}).items()},
                "stdout_has": [has] if isinstance(has, str) else [str(x) for x in (has or [])],
                "expect_exit": it.get("exit", it.get("expect_exit")),
            })
        return [s for s in out if s["entry"]]

    # 文本格式
    out = []
    cur: dict | None = None
    mode = ""
    needle: list[str] = []
    buf: list[str] = []
    fname = ""

    def _flush_needle() -> None:
        if cur is not None and needle:
            text_ = "\n".join(needle).strip("\n")
            if text_.strip():
                cur["stdout_has"].append(text_)
        needle.clear()

    for ln in raw.splitlines():
        s = ln.strip()
        if s.startswith("###"):
            _flush_needle()
            if cur:
                out.append(cur)
            cur = {"name": s.lstrip("#").strip() or f"样例{len(out) + 1}", "entry": "",
                   "args": [], "files": {}, "stdout_has": [], "expect_exit": None}
            mode = ""
            continue
        if cur is None:
            continue
        if s.startswith("$"):
            entry, args = _parse_cmd(s[1:])
            cur["entry"], cur["args"] = entry, args
            mode = ""
            continue
        if s.startswith("<<<"):
            fname = s[3:].strip()
            buf = []
            mode = "file"
            continue
        if s.startswith(">>>") and mode == "file":
            cur["files"][fname] = "\n".join(buf)
            mode = ""
            continue
        if s.startswith("---"):
            _flush_needle()
            tail = s[3:].strip().lower()
            mode = "expect"
            # 支持两种写法：`--- exit: 1` 和 `--- expect exit: 1`
            if "exit" in tail:
                m = re.search(r"exit[^\d]*(\d+)", tail)
                cur["expect_exit"] = int(m.group(1)) if m else 0
            continue
        if mode == "file":
            buf.append(ln)
        elif mode == "expect":
            needle.append(ln)
    _flush_needle()
    if cur:
        out.append(cur)
    return [s for s in out if s["entry"]]


def to_caps(samples: list[dict]) -> list:
    """样例 → 能力（id 稳定：cap.sample1、cap.sample2…）。"""
    from . import blueprint as _bp
    caps = []
    for i, s in enumerate(samples, 1):
        probe = {"kind": "cli", "entry": s["entry"], "args": list(s["args"])}
        if s.get("files"):
            probe["files"] = dict(s["files"])
        if s.get("stdout_has"):
            has = s["stdout_has"]
            probe["stdout_has"] = has[0] if len(has) == 1 else has
        if s.get("expect_exit") is not None:
            probe["expect_exit"] = int(s["expect_exit"])
        caps.append(_bp.Cap(
            id=f"cap.sample{i}",
            desc=f"（人给的验收样例）{s['name']}：python {s['entry']} "
                 + " ".join(str(a) for a in s["args"]),
            probe=probe, deps=[], funcs_est=3,
            modules=[s["entry"]] if s["entry"] else [],
            status="absent",
        ))
    return caps


def entries(samples: list[dict]) -> list[str]:
    """样例里出现的入口文件名（用来把契约/蓝图对齐到真人给的形状）。"""
    out: list[str] = []
    for s in samples:
        e = str(s.get("entry") or "")
        if e and e not in out:
            out.append(e)
    return out
