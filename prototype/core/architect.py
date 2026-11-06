"""前期架构问答：动手写代码之前，先让远端把"必须由人拍板"的事问清楚。

为什么要这一层（用户 2026-11-07 的要求 + 实测）：
    CSV/JSON 那条线能到 100%，日志那条线七跑不收敛——差别不在模型会不会写代码，
    而在**规格里有多少东西是靠猜的**。让模型自己拍板"时间窗闭不闭、错误消息怎么写"，
    猜错一次就是整片重做（可自动修复占比从 89% 掉到 48%）。
    所以把它拆成两段：
      阶段 1（贵的地方只花一次）：远端**只提问**，人给架构安排（几个字到几行）；
      阶段 2：拿着安排全自动生成，人不再介入。

三条纪律（否则这一层会变成又一轮瞎猜）：
  1. **每个问题必须能改变"架构安排"或"验收判据"**，口味类问题不许问；
  2. **每个问题必须带默认值**：人不回答也能开跑（默认写进架构安排，事后可追责）；
  3. 至少两条要人给**可执行验收样例**——样例是最硬的判据（见 core/specsample.py）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

QA_DIR = ".sixcoder"
QUESTIONS_FILE = f"{QA_DIR}/questions.json"
ANSWERS_FILE = f"{QA_DIR}/answers.json"
ARCH_FILE = f"{QA_DIR}/architecture.md"

MAX_QUESTIONS = 10
KINDS = ("choice", "text", "example")

ASK_PROMPT = """你是架构师。**先不要写任何代码**：在动手之前，把"必须由人拍板、而且会改变架构
或验收判据"的事情问清楚。只输出 JSON。

【只问这五类】
1. 边界：这件事明确**不做**什么？（防范围膨胀，也防做多了没法验收）
2. 入口与接口：程序怎么被调用？入口文件名、参数名怎么定？输入从哪来？
3. 数据形状：输入/输出是什么格式？举一个**具体的样子**（含边界：空值、引号、缺字段）
4. 错误策略：什么情况算失败？退出码怎么定？错误信息给谁看、要不要行号？
5. 非功能：规模/性能/依赖限制/落盘要求（只问会改变实现的）

【硬要求，不满足就算不合格】
- 最多 @MAX@ 个问题；**每个问题都必须能改变"架构安排"或"验收判据"**，否则不要问
- 每个问题给 `default`：人不回答就用它，保证仍然能开跑
- 至少 2 个问题 `kind` 为 `"example"`：要求人给一条"怎么跑 + 输入什么 + 该看到什么"的验收样例
- **不要问**"用什么框架/什么风格/你喜欢哪种"这类口味问题
- 问题要具体到能一句话回答，不要"请详细描述你的需求"

输出 JSON：
{"questions": [{"id": "Q1", "q": "一句话问题", "why": "为什么必须问（不答会怎样）",
                "kind": "choice|text|example", "options": ["A", "B"], "default": "默认安排"}]}

【目标】
@GOAL@
"""


def _default_llm(prompt: str, *, json_mode: bool = True, task_id: str = "architect",
                 node_id: str = "architect", max_out: int | None = None) -> dict:
    from . import remote
    from . import settings_store as _st
    idx = None
    try:
        idx = _st.strongest_step_index()
    except Exception:                                    # noqa: BLE001
        idx = None
    r = remote.complete(prompt, task_id=task_id, node_id=node_id, json_mode=json_mode,
                        step_index=idx, thinking="on", max_out=max_out)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or "远端调用失败", "answer": ""}
    return {"ok": True, "answer": r.get("answer") or "", "by": r.get("by") or "",
            "usage": r.get("usage") or {}}


def _json_of(text: str) -> dict | None:
    import re
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            d = json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(d, dict):
            return d
    return None


def _next_id(used: list[dict]) -> str:
    """下一个不撞号的 id（模型自己给的 id 可能跳号/重号，不能按 len 推算）。"""
    n = 0
    for q in used:
        digits = "".join(ch for ch in str(q.get("id") or "") if ch.isdigit())
        if digits:
            n = max(n, int(digits))
    nxt = f"Q{n + 1}"
    ids = {str(q.get("id") or "") for q in used}
    while nxt in ids:
        n += 1
        nxt = f"Q{n + 1}"
    return nxt


def normalize(raw: list) -> list[dict]:
    """校验 + 归一：只留"能改变架构/判据"的问题，且每个都带默认值。"""
    out: list[dict] = []
    for i, q in enumerate(raw or [], 1):
        if not isinstance(q, dict):
            continue
        text = str(q.get("q") or q.get("question") or "").strip()
        if not text:
            continue
        kind = str(q.get("kind") or "text").strip().lower()
        if kind not in KINDS:
            kind = "text"
        d = str(q.get("default") or "").strip()
        opts = [str(o).strip() for o in (q.get("options") or []) if str(o).strip()]
        if kind == "choice" and not opts:
            kind = "text"
        if kind == "choice" and not d and opts:
            d = opts[0]
        out.append({"id": str(q.get("id") or f"Q{i}").strip() or f"Q{i}",
                    "q": text, "why": str(q.get("why") or "").strip(),
                    "kind": kind, "options": opts, "default": d})
    # 至少两条"验收样例"问题（缺就补最通用的两条）
    need = max(0, 2 - sum(1 for x in out if x["kind"] == "example"))
    if need:
        extra = [("给一条**成功路径**的验收样例：命令怎么跑、输入文件是什么内容、该看到什么输出？",
                  "样例是最硬的判据；没有它，输出格式只能靠猜"),
                 ("给一条**失败路径**的验收样例：什么输入算错、该报什么原因、退出码是多少？",
                  "错误路径最容易各写各的（措辞/退出码）")][:need]
        for text, why in extra:
            out.append({"id": _next_id(out), "q": text, "why": why,
                        "kind": "example", "options": [], "default": ""})
    # 截断：验收样例类优先留下（它们是最贵的判据），其余按原顺序补满
    if len(out) > MAX_QUESTIONS:
        ex = [q for q in out if q["kind"] == "example"]
        rest = [q for q in out if q["kind"] != "example"]
        out = (ex + rest)[:MAX_QUESTIONS]
    return out


def ask(goal: str, *, llm=None, emit=None) -> dict:
    """远端就架构安排提问。返回 {ok, questions, calls}。"""
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "questions": [], "calls": 0, "problems": ["目标是空的"]}
    llm = llm or _default_llm
    prompt = ASK_PROMPT.replace("@MAX@", str(MAX_QUESTIONS)).replace("@GOAL@", goal[:6000])
    r = llm(prompt, json_mode=True, task_id="architect", node_id="architect", max_out=12000)
    if not r.get("ok"):
        return {"ok": False, "questions": [], "calls": 1,
                "problems": [f"提问失败：{r.get('error')}"]}
    data = _json_of(r.get("answer") or "") or {}
    qs = normalize(data.get("questions") or [])
    if emit:
        emit("architect_questions", {"n": len(qs),
                                     "ids": [q["id"] for q in qs]})
    return {"ok": bool(qs), "questions": qs, "calls": 1}


def render_qa(questions: list[dict], answers: dict | None = None) -> str:
    """把问答渲染成"架构安排"（权威文本，会进目标与契约语境）。"""
    answers = answers or {}
    lines = ["## 架构安排（人给的，权威；与目标冲突时以此为准）"]
    for q in questions:
        a = str(answers.get(q["id"]) or "").strip() or q.get("default") or ""
        src = "人答" if str(answers.get(q["id"]) or "").strip() else "默认"
        lines.append(f"- [{q['id']}·{src}] {q['q']}")
        lines.append(f"  → {a or '（未定，按最保守做法）'}")
    return "\n".join(lines)


def samples_from(questions: list[dict], answers: dict) -> str:
    """把 `kind=example` 的回答转成 specsample 文本（人给的验收样例）。"""
    blocks: list[str] = []
    for q in questions:
        if q.get("kind") != "example":
            continue
        a = str((answers or {}).get(q["id"]) or "").strip()
        if not a:
            continue
        name = (q["q"][:30] or q["id"]).replace("\n", " ")
        blocks.append(f"### {name}\n{a}")
    return "\n\n".join(blocks)


def save(ws, questions: list[dict], answers: dict | None, goal: str = "") -> list[str]:
    """落盘三件：问题、答案、架构安排（都可溯源）。返回写过的路径。"""
    d = Path(ws) / QA_DIR
    d.mkdir(parents=True, exist_ok=True)
    wrote = []
    try:
        (Path(ws) / QUESTIONS_FILE).write_text(
            json.dumps({"goal": goal, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "questions": questions}, ensure_ascii=False, indent=1), encoding="utf-8")
        wrote.append(QUESTIONS_FILE)
        if answers is not None:
            (Path(ws) / ANSWERS_FILE).write_text(
                json.dumps(answers, ensure_ascii=False, indent=1), encoding="utf-8")
            wrote.append(ANSWERS_FILE)
        (Path(ws) / ARCH_FILE).write_text(render_qa(questions, answers or {}), encoding="utf-8")
        wrote.append(ARCH_FILE)
    except OSError:
        pass
    return wrote


def load_questions(ws) -> list[dict]:
    p = Path(ws) / QUESTIONS_FILE
    try:
        return list((json.loads(p.read_text(encoding="utf-8")) or {}).get("questions") or [])
    except Exception:                                    # noqa: BLE001
        return []


def load_answers(ws) -> dict:
    p = Path(ws) / ANSWERS_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def parse_answers(text: str) -> dict:
    """答案文件两种写法都收：JSON 字典，或 `Q1: 答案` 逐行（可多行续写）。

    人写答案不该被格式卡住——卡住一次，这一层就白做了。
    """
    import re
    raw = text or ""
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {str(k).strip(): str(v) for k, v in d.items()}
    except Exception:                                    # noqa: BLE001
        pass
    out: dict[str, str] = {}
    cur = ""
    cont = False          # 当前答案是否"还没写完"（验收样例那种多行块）
    for line in raw.splitlines():
        m = re.match(r"^\s*(Q\d+)\s*[:：]\s*(.*)$", line)
        if m:
            cur = m.group(1)
            out[cur] = m.group(2).strip()
            cont = out[cur].startswith(("$", "```", "---", "<<<"))
            continue
        if not cur or not line.strip():
            continue
        # 缩进行一定算续写；非缩进行只有在"样例块已开始"时才续，
        # 免得一段随手写的说明被吸进上一个答案里（答案错了比没答案更糟）
        if line[:1] in (" ", "\t") or cont:
            out[cur] = (out[cur] + "\n" + line.rstrip()).strip()
    return out


def merge_goal(goal: str, questions: list[dict], answers: dict | None) -> str:
    """目标 + 架构安排 → 新的目标文本（后续 derive/契约都吃这一份）。"""
    arch = render_qa(questions, answers or {})
    return f"{goal.strip()}\n\n{arch}" if (goal or "").strip() else arch
