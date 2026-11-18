"""思考强度路由：单模型下由系统按"这次干什么、有多难"决定想多深。

为什么有这个模块（2026-11-05 用户口径）：
    远端机制从"**多模型 × 多思考强度**"改成"**单模型 + 按需求自动切思考强度**"。
    于是"这次用哪一档强度"从"阶梯第几档"变成一条**可解释、可复现**的路由规则 ——
    不能写死"总是 none"（省了钱但做不对），也不能写死"总是 max"（想得深但烧钱）。

口径（全部确定性，同输入同输出）：

    kind="spec"/"plan"   → max   规格/方案是全链最高杠杆的一步，想得越深，后面越省
    kind="mechanical"    → none  样板/校验/日志/适配：想也没用，交给便宜路径或本地模型
    kind="code"          → 按难度分 score 选 none / low / high / max

难度分只吃确定性事实（依赖数/函数数/示例数/历史失败/是否桩/文件行数），
因为这些事实本来就在契约与状态里，不需要再猜。
"""
from __future__ import annotations

EFFORTS = ("none", "low", "high", "max")
_ORDER = {e: i for i, e in enumerate(EFFORTS)}
_ALIAS = {"off": "none", "minimal": "low", "medium": "high", "xhigh": "high",
          "disabled": "none", "": "none", "-": "none"}


def norm(value) -> str:
    """把任意写法收敛成 none/low/high/max（非法值当 none，绝不抛错）。"""
    key = str(value if value is not None else "").strip().lower()
    if key in EFFORTS:
        return key
    return _ALIAS.get(key, "none")


def difficulty(*, deps: int = 0, funcs: int = 0, examples: int = 0, fails: int = 0,
               stub: bool = False, lines: int = 0) -> int:
    """难度分（0–9，越大越难）。全部来自已有事实，可复现、可解释、可测。"""
    score = 0
    if deps >= 1:
        score += 1
    if deps >= 3:
        score += 1
    if funcs >= 4:
        score += 1
    if funcs >= 7:
        score += 1
    if examples >= 4:
        score += 1
    if fails >= 1:
        score += 1
    if fails >= 2:
        score += 1
    if stub:
        score += 1
    if lines >= 300:
        score += 1
    return score


def route(kind: str = "code", score: int = 0) -> str:
    """这个 kind 的**封顶心愿档**（spec/plan=max；写码=max，即模型物理上限）。

    **2026-11-10 起口径演进（用户要求双向自适应）**：`route()` 不再直接决定起点，
    只表达"这个 kind 最高允许到哪"。写码起点走 `route_start()`（难度先验）
    + `proven_floor()`（模块实测档案），升档封顶仍是 max——
    11-09 铁律的**精髓**（失败永远能升到最强档救场）完整保留；
    变的只是**起点**：简单模块不再先烧一轮 max 学费。

    11-09 事故教训（为什么难度分一度被全禁，又为什么现在能恢复）：
    当年恶性闭环的根源是**校准器拿"各档成功率"反向压封顶**（全局、不可逆）：
    机械小任务拿到 none 且成功 → 校准器认定"none 成功率最高" → 把
    `code_effort_ceiling` 压到 none → 失败后永远升不了档。
    现在的三条保险丝让它不会再发生：
      ① 封顶恒 max，校准器只记录成功率不参与决策（calibration.py 已拆）；
      ② 降档只认"该模块该档**亲自成功过**"的证据（`proven_floor`），
         失败永远不作为降档证据——失败可能是引擎 bug，不是档位不够；
      ③ 起点错了的代价 = 一次低档调用 + `escalate_after=1` 秒升，
         远小于"每模块都从 max 起步"的固定学费（实测占夜班花费 74%）。
    `score` 参数保留是为了兼容调用方签名与事件流的可解释性。
    """
    k = str(kind or "code").strip().lower()
    if k in ("spec", "plan", "architect", "diagnose"):
        return "max"
    # 写码相关的所有 kind 封顶都是 max（完全相信远端的能力上限）。
    return "max"


def route_start(score: int) -> str:
    """写码**起点档**先验（新模块没有实测档案时的兜底，2026-11-10）。

    只吃难度分（0–9），分段保守：
      0–2 简单（少依赖/少函数/非桩）  → none
      3–5 中等                        → low
      6–9 复杂（多依赖/多函数/历史失败/大文件）→ high
    **max 永不当起点**：留给 spec/plan 和升档救场。台账实证（2026-11-10）：
    简单模块 none 档成功率 100%（quota 7/7、schemas 11/11、llm 11/11、
    audit 6/6、authz 4/4……），它们从 max 起步纯属烧钱。
    """
    s = int(score or 0)
    if s >= 6:
        return "high"
    if s >= 3:
        return "low"
    return "none"


def proven_floor(ws, rel: str) -> str:
    """该模块历史上**亲自成功过**的最低档（产物仓写作台账 author_attempts.jsonl）。

    这是"自适应代码库"的核心：代码库在长，每个模块的实测档案跟着积累——
    下次同模块再来，起点直接用"它被证明能写对的最低档"，不再靠猜。
    例（2026-11-10 台账实测）：metrics.py 在 none 档 11/11 成功 → 下次从 none 起；
    api/batches.py 只有 max 成功过 1 次 → 从 max 起。

    只认成功、不认失败（2026-11-09 血泪）：失败分不清是"模型能力不够"还是
    "引擎自己造坏数据"（多行签名截断/丢函数/路由 404——当时一批 bug），
    拿失败当降档证据 = 把机制问题记成能力问题 = 恶性闭环；
    成功没有歧义：这一档真的把这个模块写对过。
    查不到任何成功记录返回 ""（调用方退回 route_start 先验）。绝不抛错。
    """
    if not ws or not rel:
        return ""
    try:
        import json as _json
        from pathlib import Path as _P
        p = _P(ws) / ".sixcoder" / "author_attempts.jsonl"
        if not p.exists():
            return ""
        floor_i: int | None = None
        # 只看最近 2000 条（防长项目无限膨胀）；同一模块旧证据足够代表
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines()[-2000:]:
            try:
                r = _json.loads(line)
            except Exception:                            # noqa: BLE001
                continue
            if not isinstance(r, dict) or not r.get("ok"):
                continue
            # **工作单级**记录（kind="order"）不算模块级证据：
            # 它只说明"这个函数在 X 档能写对"，推不出"整个模块在 X 档能写对"。
            # 混进来会让 proven_floor 误报一个偏低的档（2026-11-10 补记账时钉住）。
            if str(r.get("kind") or "module") == "order":
                continue
            if str(r.get("path") or "") != str(rel):
                continue
            i = _ORDER.get(norm(r.get("effort")))
            if i is None:
                # 台账里认不出的档位 → 不当证据（原来用 99 当"最差"哨兵，
                # 等于给这个数编了个没依据的含义，check_invariants 会当场抓住）。
                continue
            if floor_i is None or i < floor_i:
                floor_i = i
        return EFFORTS[floor_i] if floor_i is not None else ""
    except Exception:                                    # noqa: BLE001
        return ""


def pick(steps: list[dict], want: str) -> int:
    """在一段阶梯（同一个模型的不同强度）里挑下标。

    规则：**取不小于 want 的最低一档**；该模型没配这一档就往上找；到顶就用最高档。
    为什么不是"精确等于"：某个模型可能只开了 none/high（没钱买 max），
    want="max" 时用 high 也比退回 none 更接近意图。
    """
    if not steps:
        return 0
    if len(steps) == 1:
        return 0                # 单档直通：没有可挑的档，也不按难度打分
    w = norm(want)
    for i, s in enumerate(steps):
        if norm(s.get("effort")) == w:
            return i
    want_i = _ORDER[w]
    for i, s in enumerate(steps):
        if _ORDER[norm(s.get("effort"))] >= want_i:
            return i
    return len(steps) - 1


def explain(kind: str, score: int, want: str, steps: list[dict], idx: int) -> dict:
    """给事件流/报告用的一句话解释（"为什么这次想这么深"）。"""
    chosen = norm((steps[idx] if 0 <= idx < len(steps) else {}).get("effort"))
    return {"kind": kind, "difficulty": int(score or 0), "want": norm(want),
            "chosen": chosen, "index": int(idx),
            "why": ("规格/方案 → 最深" if norm(want) == "max" and kind in ("spec", "plan")
                    else "机械活 → 不思考" if norm(want) == "none"
                    else f"难度分 {int(score or 0)} → {norm(want)}")}
