"""临时方案台账：旧的临时方案必须清除，不许层层打补丁。

为什么需要（分级表里 M 级就要求"熵管理"，L 级要求"后台扫描 + 自动合并"）：
    夜班为了当下过关，很容易写出"临时方案"——`TODO: 先这样`、`HACK`、`# 临时绕过`。
    这些不会让判据变红，但它们会**复利**：下一轮在临时方案上再打补丁，
    最后没人知道哪段代码是正解、哪段是将就。

做法（确定性、可追溯）：
  1. 扫工作区的标记（TODO/FIXME/XXX/HACK/临时/先这样/workaround/暂时/兜底写法）；
  2. 台账 `.sixcoder/debt.jsonl`：每条记 first_seen_round + 到期轮次，**不清空历史**；
  3. 到期的临时方案：S 级只提示、M 级只报告、L 级以上**判红**（必须清掉或写清理由）；
  4. 被清掉/修好的条目自动标记 resolved（不是删掉，保留"曾经有过"的记录）。

分级依据：S 级项目测试覆盖不足，硬逼清理反而危险（分级表原文）；到 L 级才硬。
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

LEDGER = ".sixcoder/debt.jsonl"
MARKERS = re.compile(
    r"(TODO|FIXME|XXX|HACK|TEMP|WORKAROUND|"
    r"临时方案|临时的|临时绕过|先这样|暂时这样|暂时先|权宜|兜底写法)", re.I)
_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache"}

# 每级允许一个临时方案活多少轮（S 级不硬性清理，给很大的数）
LIFETIME = {"S": 999, "M": 5, "L": 3, "XL": 2}


def scan(ws: Path, limit: int = 200) -> list[dict]:
    """扫出所有临时方案标记（只扫源码与测试，跳过依赖与缓存）。"""
    ws = Path(ws)
    out: list[dict] = []
    for p in ws.rglob("*.py"):
        rel = p.relative_to(ws)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            m = MARKERS.search(line)
            if not m:
                continue
            out.append({"file": str(rel).replace("\\", "/"), "line": i,
                        "marker": m.group(1).upper(),
                        "text": re.sub(r"\s+", " ", line.strip())[:160]})
            if len(out) >= limit:
                return out
    return out


def _key(rec: dict) -> str:
    """条目身份：文件 + 标记种类（行号会漂，不参与身份；同一文件同类只算一条台账）。"""
    return f"{rec['file']}|{rec['marker']}"


def load(ws: Path, fold: bool = True) -> list[dict]:
    """读台账。

    JSONL 是**只追加**的审计流（谁在什么时候改了什么都在），所以同一个 key 会有多条；
    `fold=True` 时按 key 折叠成"最新状态"（后写的覆盖先写的）——
    这样"行号跟着代码走""修好后标记 resolved"都能表达，而历史一条不丢。
    """
    f = Path(ws) / LEDGER
    if not f.exists():
        return []
    rows: list[dict] = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:                                # noqa: BLE001
            continue
    if not fold:
        return rows
    latest: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        k = r.get("key") or _key(r)
        if k not in latest:
            order.append(k)
        latest[k] = {**latest.get(k, {}), **r}            # 后写覆盖先写
    return [latest[k] for k in order]


def _append(ws: Path, entries: list[dict]) -> None:
    f = Path(ws) / LEDGER
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def sync(ws: Path, level: str = "S", round_no: int = 0) -> dict:
    """把当前扫到的临时方案记进台账；已消失的标记 resolved（保留历史）。"""
    ws = Path(ws)
    found = scan(ws)
    existing = load(ws)
    alive = {_key(r): r for r in existing if not r.get("resolved")}
    life = LIFETIME.get(str(level).upper(), 999)
    new_entries: list[dict] = []
    moved: list[dict] = []
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for rec in found:
        k = _key(rec)
        if k in alive:
            old = alive[k]
            if old.get("line") != rec["line"] or old.get("text") != rec["text"]:
                # 行号/文本变了：追加一条新状态（审计流只追加，load 时折叠成最新）
                moved.append({**old, "line": rec["line"], "text": rec["text"],
                              "moved_at": now})
            continue
        new_entries.append({**rec, "key": k, "first_seen_round": round_no,
                            "expire_round": round_no + life, "seen_at": now,
                            "resolved": False})
    if new_entries:
        _append(ws, new_entries)
    if moved:
        _append(ws, moved)
    current_keys = {_key(r) for r in found}
    resolved = [r for r in existing
                if not r.get("resolved") and r.get("key") not in current_keys]
    if resolved:
        _append(ws, [{**r, "resolved": True,
                      "resolved_at": now} for r in resolved])
    return {"found": found, "new": new_entries, "resolved": resolved,
            "open": [r for r in load(ws) if not r.get("resolved")]}


def gate(ws: Path, level: str = "M", round_no: int = 0) -> dict:
    """到期的临时方案怎么办：S 提示 / M 报告 / L+ 判红。"""
    lv = str(level).upper()
    try:
        from . import scale as _scale
        mode = "block" if lv in ("L", "XL") else ("advisory" if lv == "M" else "skip")
    except Exception:                                    # noqa: BLE001
        mode = "advisory"
    open_items = [r for r in load(ws) if not r.get("resolved")]
    expired = [r for r in open_items if int(r.get("expire_round") or 0) <= round_no]
    if mode == "skip":
        return {"ok": True, "mode": mode, "open": len(open_items), "expired": len(expired),
                "detail": f"{lv} 级不硬性清理临时方案（测试覆盖不足时硬清更危险）；"
                          f"当前 {len(open_items)} 条，到期 {len(expired)} 条（只记录）"}
    if not expired:
        return {"ok": True, "mode": mode, "open": len(open_items), "expired": 0,
                "detail": f"临时方案 {len(open_items)} 条，无到期"}
    names = "；".join(f"{r['file']}:{r['line']} {r['marker']}" for r in expired[:4])
    if mode == "block":
        return {"ok": False, "mode": mode, "open": len(open_items), "expired": len(expired),
                "detail": (f"到期未清的临时方案 {len(expired)} 条（{lv} 级要求清除）：{names}"
                           f" —— 要么按最新导轨重写这段，要么在台账里写清为什么必须保留")}
    return {"ok": True, "mode": mode, "open": len(open_items), "expired": len(expired),
            "detail": f"到期未清 {len(expired)} 条（{lv} 级只报告，不阻断）：{names}"}


def report_lines(ws: Path, level: str = "M", round_no: int = 0) -> list[str]:
    g = gate(ws, level, round_no)
    open_items = [r for r in load(ws) if not r.get("resolved")]
    lines = ["", "### 临时方案台账（旧方案必须清除）", "",
             f"- {'**未通过**' if not g['ok'] else '通过'}：{g['detail']}",
             f"- 台账文件：`{LEDGER}`（记录 first_seen/到期轮次，已清的标记 resolved 而不是删除）"]
    for r in open_items[:8]:
        lines.append(f"- `{r['file']}:{r['line']}` [{r['marker']}] {str(r.get('text'))[:100]}")
    if not open_items:
        lines.append("- 没有未清的临时方案")
    return lines
