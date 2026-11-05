"""SQLite 持久化：任务 / 节点 / 闸门记录（含 evidence_kind）/ 事件流。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "proto.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id          TEXT PRIMARY KEY,
  repo        TEXT NOT NULL,
  workspace   TEXT NOT NULL,
  requirement TEXT NOT NULL,
  target      TEXT,
  state       TEXT NOT NULL,
  model       TEXT NOT NULL,
  audit_json  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  id          TEXT PRIMARY KEY,
  task_id     TEXT NOT NULL,
  seq         INTEGER NOT NULL,
  kind        TEXT NOT NULL,
  path        TEXT NOT NULL,
  contract    TEXT NOT NULL,
  status      TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  patch       TEXT,
  created_at  TEXT NOT NULL
);

-- 判定权分离的核心：每个通过结论都必须带确定性证据
CREATE TABLE IF NOT EXISTS gate_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id       TEXT NOT NULL,
  node_id       TEXT NOT NULL,
  attempt       INTEGER NOT NULL,
  gate          TEXT NOT NULL,
  ok            INTEGER NOT NULL,
  evidence_kind TEXT NOT NULL,
  evidence_ref  TEXT,
  detail        TEXT,
  elapsed_ms    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT NOT NULL,
  CHECK (evidence_kind IN ('exit_code','assertion','mutation','static_rule','human','model_text'))
);

CREATE TABLE IF NOT EXISTS events (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts      TEXT NOT NULL,
  kind    TEXT NOT NULL,
  payload TEXT
);

-- 测试规约（供计算校验与报告回溯：期望值到底写了什么）
CREATE TABLE IF NOT EXISTS specs (
  task_id   TEXT NOT NULL,
  node_id   TEXT NOT NULL,
  spec_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (task_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_task ON nodes(task_id);
CREATE INDEX IF NOT EXISTS idx_gate_task  ON gate_runs(task_id);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
"""


def save_spec(task_id: str, node_id: str, spec: dict) -> None:
    # 记下生成它的引擎版本：引擎一改（判据/闸门修了 bug），旧规格就不能再复用
    try:
        from . import release_version
        spec = {**spec, "_engine": release_version()}
    except Exception:                                    # noqa: BLE001
        pass
    execute("INSERT INTO specs(task_id,node_id,spec_json,created_at) VALUES(?,?,?,?)"
            " ON CONFLICT(task_id,node_id) DO UPDATE SET spec_json=excluded.spec_json, created_at=excluded.created_at",
            (task_id, node_id, json.dumps(spec, ensure_ascii=False), now()))


def get_spec(task_id: str) -> dict | None:
    """取最近一次测试规格。

    若规格是**旧引擎**生成的（引擎版本变了），当作没有规格，让它按新判据重新生成。
    教训：判据修好之后旧规格还留在库里，于是"同样的需求反复失败"，看着像没修。
    """
    rows = query("SELECT spec_json FROM specs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
                 (task_id,))
    if not rows:
        return None
    try:
        spec = json.loads(rows[0]["spec_json"])
    except Exception:
        return None
    try:
        from . import release_version
        cur = release_version()
        old = spec.get("_engine")
        # 没有 _engine 标记的也算过期（来历不明 → 重新生成），宁可重算也不要拿旧判据跑
        if cur and old != cur:
            return None
    except Exception:                                    # noqa: BLE001
        pass
    return spec


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _lock, connect() as conn:
        conn.executescript(SCHEMA)


def execute(sql: str, params: tuple = ()) -> None:
    with _lock, connect() as conn:
        conn.execute(sql, params)


def query(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock, connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def add_event(task_id: str, kind: str, payload: dict | None = None) -> None:
    execute(
        "INSERT INTO events(task_id, ts, kind, payload) VALUES(?,?,?,?)",
        (task_id, now(), kind, json.dumps(payload or {}, ensure_ascii=False)),
    )


def record_gate(task_id: str, node_id: str, attempt: int, gate: str, ok: bool,
                evidence_kind: str, evidence_ref: str = "", detail: str = "",
                elapsed_ms: int = 0) -> None:
    """唯一允许写入"通过/失败"结论的入口。"""
    assert evidence_kind in ("exit_code", "assertion", "mutation", "static_rule", "human", "model_text")
    execute(
        "INSERT INTO gate_runs(task_id,node_id,attempt,gate,ok,evidence_kind,evidence_ref,detail,elapsed_ms,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (task_id, node_id, attempt, gate, 1 if ok else 0, evidence_kind,
         evidence_ref, detail[:2000], elapsed_ms, now()),
    )


def delete_task(task_id: str) -> dict:
    """删除一个任务的全部记录（任务/节点/闸门证据/事件/测试规约）。"""
    counts = {}
    for table in ("nodes", "gate_runs", "events", "specs"):
        rows = query(f"SELECT COUNT(*) AS n FROM {table} WHERE task_id=?", (task_id,))
        counts[table] = rows[0]["n"] if rows else 0
        execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
    rows = query("SELECT COUNT(*) AS n FROM tasks WHERE id=?", (task_id,))
    counts["tasks"] = rows[0]["n"] if rows else 0
    execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return counts



def delete_all_tasks(with_finished_only: bool = True) -> dict:
    """一键清空历史任务（默认不动正在运行的）。"""
    rows = query("SELECT id,state,workspace FROM tasks")
    targets = [r for r in rows if (r["state"] != "running" if with_finished_only else True)]
    counts = {"nodes": 0, "gate_runs": 0, "events": 0, "specs": 0, "tasks": 0}
    for r in targets:
        c = delete_task(r["id"])
        for k in counts:
            counts[k] += c.get(k, 0)
    return {"counts": counts, "deleted": [r["id"] for r in targets],
            "kept_running": [r["id"] for r in rows if r["id"] not in {t["id"] for t in targets}]}


def set_node_status(task_id: str, node_id: str, status: str, gate: str,
                    ok: bool, detail: str) -> None:
    execute("UPDATE nodes SET status=? WHERE task_id=? AND id=?", (status, task_id, node_id))
    record_gate(task_id, node_id, 99, gate, ok, "human", "manual", detail)


def recompute_state(task_id: str) -> str:
    """人工处置后重算任务状态：全部 done/accepted → done；否则 stuck（有节点没通过）。

    历史记录里可能存着旧的 "needs_human"，读取时按 stuck 处理（兼容不迁移数据）。
    """
    rows = query("SELECT status FROM nodes WHERE task_id=?", (task_id,))
    states = [r["status"] for r in rows]
    if not states:
        return "failed"
    if all(s in ("done", "accepted") for s in states):
        new = "done"
    elif any(s in ("stuck", "needs_human", "pending", "running") for s in states):
        new = "stuck"
    else:
        new = "stuck"
    execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", (new, now(), task_id))
    add_event(task_id, "human_resolve", {"state": new, "statuses": states})
    return new


def recover_stale() -> int:
    """启动时把上次进程中断留下的 running 任务标记为 failed（否则永远转圈）。"""
    rows = query("SELECT id FROM tasks WHERE state='running'")
    for r in rows:
        add_event(r["id"], "notice", {"message": "进程重启，任务中断；已标记为 failed（可用 --resume 重跑）"})
        execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?", ("failed", now(), r["id"]))
    return len(rows)


def audit(task_id: str) -> dict:
    """三项自认可指标（docs/13 §6）。全部确定性计算。"""
    rows = query("SELECT gate, ok, evidence_kind FROM gate_runs WHERE task_id=?", (task_id,))
    passed = [r for r in rows if r["ok"] == 1]
    model_backed = [r for r in passed if r["evidence_kind"] == "model_text"]
    mutation = [r for r in rows if r["gate"] == "mutation"]
    mutation_ok = [r for r in mutation if r["ok"] == 1]
    by_kind: dict[str, int] = {}
    for r in rows:
        by_kind[r["evidence_kind"]] = by_kind.get(r["evidence_kind"], 0) + 1
    return {
        "passed": len(passed),
        "model_assertion_rate": round(len(model_backed) / len(passed), 4) if passed else 0.0,
        "mutation_runs": len(mutation),
        "mutation_kill_rate": round(len(mutation_ok) / len(mutation), 4) if mutation else None,
        "evidence_by_kind": by_kind,
        "gates_total": len(rows),
        "gates_failed": len(rows) - len(passed),
    }
