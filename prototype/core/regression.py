"""回归用例库：远端解成功过一次的问题，沉淀成"以后每次都要过"的检查。

为什么需要：远端一次成功很贵（时间 + token），如果同一类错误下次重来，等于白花钱。
所以成功落盘后，把当时用的**引擎判据**（示例/异常/往返性质）连同需求指纹存下来；
下次同一需求（同 requirement 指纹）跑同一模块时，这些判据自动并入闸门。

作用域（避免误伤别的仓库）：
- 只按 `需求指纹 + 文件路径` 匹配；
- 只在目标文件存在时生效；
- 判据自身是纯函数调用/属性检查，跑在沙箱子进程里，超时即失败而非阻塞。
"""
from __future__ import annotations

import hashlib
import json

from . import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS regressions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  req_hash TEXT, module TEXT, task_id TEXT, node_id TEXT,
  checks_json TEXT, source TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS ix_reg_req ON regressions(req_hash, module);
"""


def _ensure() -> None:
    db.execute(SCHEMA.split(";")[0])
    try:
        db.execute("CREATE INDEX IF NOT EXISTS ix_reg_req ON regressions(req_hash, module)")
    except Exception:                                  # noqa: BLE001
        pass


def req_hash(requirement: str) -> str:
    norm = " ".join((requirement or "").split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def add(requirement: str, module: str, checks: list[dict], task_id: str = "",
        node_id: str = "", source: str = "remote") -> int:
    """登记一组判据；相同指纹+模块+判据内容不重复登记。返回新增条数。"""
    _ensure()
    if not checks:
        return 0
    h = req_hash(requirement)
    payload = json.dumps(checks, ensure_ascii=False, sort_keys=True)
    exists = db.query("SELECT checks_json FROM regressions WHERE req_hash=? AND module=?",
                      (h, module))
    for row in exists:
        if (row.get("checks_json") or "") == payload:
            return 0
    db.execute("INSERT INTO regressions(req_hash, module, task_id, node_id, checks_json,"
               " source, created_at) VALUES(?,?,?,?,?,?,?)",
               (h, module, task_id, node_id, payload, source, db.now()))
    return 1


def for_task(requirement: str, module: str = "") -> list[dict]:
    """取出与本次需求匹配的判据。

    先按 (需求指纹, 模块) 精确匹配；没有则按同一需求指纹跨模块回退——
    因为同一需求下文件可能被重命名（实测 int_to_roman.py ↔ int_roman.py），
    而需求指纹本身已经足够窄，跨模块回退不会误伤别的任务。
    """
    _ensure()
    h = req_hash(requirement)
    rows = db.query("SELECT checks_json FROM regressions WHERE req_hash=? AND module=?",
                    (h, module))
    if not rows:
        rows = db.query("SELECT checks_json FROM regressions WHERE req_hash=?", (h,))
    out, seen = [], set()
    for r in rows:
        try:
            checks = json.loads(r.get("checks_json") or "[]")
        except Exception:                              # noqa: BLE001
            continue
        for c in checks:
            key = json.dumps(c, ensure_ascii=False, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
    return out


def listing(limit: int = 50) -> list[dict]:
    _ensure()
    return db.query("SELECT id, module, task_id, node_id, source, created_at,"
                    " length(checks_json) AS bytes FROM regressions"
                    " ORDER BY id DESC LIMIT ?", (limit,))


def delete(rid: int) -> int:
    _ensure()
    rows = db.query("SELECT id FROM regressions WHERE id=?", (rid,))
    db.execute("DELETE FROM regressions WHERE id=?", (rid,))
    return len(rows)


def clear() -> int:
    _ensure()
    n = len(db.query("SELECT id FROM regressions"))
    db.execute("DELETE FROM regressions")
    return n
