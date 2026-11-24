"""精修模式的"主动提问"通道（照 DSH 的 ask_user_question 语义做）。

用户要求：本地模型遇到关键问题时**主动提问找人解决，不自己胡写**。

三条硬口径（都按 DSH 的做法）：
1. **工具化**：提问是一个显式动作，有 id、有上下文、有选项，不是一个"卡住"；
2. **带选项**：问题最好给 2~3 个候选方案，人只需点一个（比让人写长文便宜）；
3. **答案即证据**：人的回答以 `human` 证据入账，写进审计与交付报告；
   同一个问题在同一工作区**只问一次**，之后直接复用上次答案。

与"审批"的区别：审批是"这个动作能不能做"（是/否）；提问是"我该怎么做"（要内容）。
所以这里单独一套存储，不和 approval 的工单混在一起。
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

DIR = ".sixcoder/questions"


def _dir(ws: Path) -> Path:
    d = Path(ws) / DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _key(question: str) -> str:
    return hashlib.sha1((question or "").strip().encode("utf-8")).hexdigest()[:12]


def _path(ws: Path, qid: str) -> Path:
    return _dir(ws) / f"{qid}.json"


def _audit(ws: Path, entry: dict) -> None:
    p = Path(ws) / ".sixcoder" / "audit.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": _now(), "kind": "question", **entry},
                           ensure_ascii=False) + "\n")


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ask(ws: Path, question: str, options: list[str] | None = None, why: str = "",
        node: str = "", task_id: str = "", emit=None, wait_s: float = 0,
        reuse: bool = True) -> dict:
    """提出问题。返回 {ok, answer, qid, cached, asked}。

    wait_s=0（默认）= **不阻塞**：登记成待答，立刻返回 ok=False；
    调用方（夜班/开发者流程）会停下来，等人回答后再继续。
    """
    q = (question or "").strip()
    if not q:
        return {"ok": False, "answer": "", "qid": "", "error": "空问题"}
    qid = _key(q)
    path = _path(ws, qid)
    if reuse and path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("answer"):
            return {"ok": True, "answer": data["answer"], "qid": qid, "cached": True,
                    "asked": data.get("asked_at", "")}
    data = {"qid": qid, "question": q, "options": list(options or [])[:4], "why": why[:300],
            "node": node, "task_id": task_id, "asked_at": _now(), "answer": "",
            "answered_by": "", "answered_at": ""}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _audit(ws, {"qid": qid, "action": "ask", "question": q[:200], "options": data["options"]})
    if emit:
        emit("question", {"qid": qid, "question": q, "options": data["options"],
                          "why": data["why"], "node": node})
    if wait_s and wait_s > 0:                          # 可选阻塞（CLI 交互用）
        deadline = time.time() + float(wait_s)
        while time.time() < deadline:
            got = json.loads(path.read_text(encoding="utf-8"))
            if got.get("answer"):
                return {"ok": True, "answer": got["answer"], "qid": qid, "cached": False,
                        "asked": data["asked_at"]}
            time.sleep(1.0)
    return {"ok": False, "answer": "", "qid": qid, "cached": False, "asked": data["asked_at"],
            "question": q, "options": data["options"], "why": why}


def answer(ws: Path, qid: str, text: str, by: str = "human") -> dict:
    """回答问题。答案作为 human 证据入账。"""
    path = _path(ws, qid)
    if not path.exists():
        return {"ok": False, "error": f"没有这个提问：{qid}"}
    txt = (text or "").strip()
    if not txt:
        return {"ok": False, "error": "答案不能为空"}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update({"answer": txt, "answered_by": by, "answered_at": _now()})
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _audit(ws, {"qid": qid, "action": "answer", "answer": txt[:300], "by": by})
    # 答案也进历史库：同一个问题以后不再问
    hist = Path(ws) / ".sixcoder" / "answers.jsonl"
    with hist.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"qid": qid, "question": data.get("question"),
                            "answer": txt, "by": by, "ts": data["answered_at"]},
                           ensure_ascii=False) + "\n")
    return {"ok": True, "qid": qid, "answer": txt}


def pending(ws: Path) -> list[dict]:
    out = []
    for p in sorted(_dir(ws).glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        if not d.get("answer"):
            out.append(d)
    return out


def answered(ws: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in _dir(ws).glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            continue
        if d.get("answer"):
            out[d.get("qid") or p.stem] = d["answer"]
    return out


def review_plan(ws: Path, plan: str, emit=None, mode: str = "developer") -> dict:
    """plan-mode 的流程（照 DSH）：出计划 → 人审 → 再执行。

    造物模式（mode="client"）：**自动通过**（无人值守，上限是远端能力）；
    精修模式：**必须人审** —— 人会看到计划并选择"通过"或"修改"，
    选择以 human 证据入账；不通过就把修改意见作为答案带回去。
    """
    text = (plan or "").strip()
    if not text:
        return {"ok": True, "approved": True, "answer": "", "auto": True}
    if mode == "client":
        if emit:
            emit("plan_review", {"auto": True, "chars": len(text)})
        return {"ok": True, "approved": True, "answer": "", "auto": True}
    r = ask(ws, f"是否按这个实现方案执行？\n{text[:1200]}", options=["通过", "修改"],
            why="精修模式下架构决策归人", node="plan", emit=emit)
    if r.get("ok"):
        ans = (r.get("answer") or "").strip()
        return {"ok": True, "approved": ans.startswith(("通过", "可以", "ok", "OK", "同意")),
                "answer": ans, "auto": False}
    return {"ok": False, "approved": False, "answer": "", "auto": False,
            "question": r.get("question"), "qid": r.get("qid")}
