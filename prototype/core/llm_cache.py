"""LLM 响应缓存：同一步骤重跑直接命中，省一次推理（docs/05 确定性后处理）。

键 = sha1(model + temperature + prompt)，值 = 模型输出，落 SQLite 一张小表。
只用于确定性用途（补丁生成 / 签名生成 / 规格生成）；plan 这类需要多样性的不缓存。
"""
from __future__ import annotations

import hashlib

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_cache (
  k TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  value TEXT NOT NULL,
  created_at TEXT NOT NULL,
  hits INTEGER NOT NULL DEFAULT 0
);
"""

_inited = {"ok": False}


def _ensure() -> None:
    if _inited["ok"]:
        return
    db.execute(SCHEMA)
    _inited["ok"] = True


def _key(model: str, temperature: float, prompt: str) -> str:
    raw = f"{model}|{temperature:.2f}|{prompt}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def get(model: str, temperature: float, prompt: str) -> str | None:
    _ensure()
    k = _key(model, temperature, prompt)
    rows = db.query("SELECT value FROM llm_cache WHERE k=?", (k,))
    if not rows:
        return None
    db.execute("UPDATE llm_cache SET hits=hits+1 WHERE k=?", (k,))
    return rows[0]["value"]


def put(model: str, temperature: float, prompt: str, value: str) -> None:
    if not value:
        return
    _ensure()
    db.execute("INSERT INTO llm_cache(k,model,value,created_at) VALUES(?,?,?,?)"
               " ON CONFLICT(k) DO UPDATE SET value=excluded.value",
               (_key(model, temperature, prompt), model, value, db.now()))


def stats() -> dict:
    _ensure()
    rows = db.query("SELECT COUNT(*) c, COALESCE(SUM(hits),0) h FROM llm_cache")
    return {"entries": rows[0]["c"] or 0, "hits": rows[0]["h"] or 0}
