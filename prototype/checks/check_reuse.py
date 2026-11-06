"""自检：确定性步骤的「同证据复用」+ 蓝图审计缓存（0 花费、不发一次真远端）。

钉住的几件事，每条都对应一个真会烧钱的失败：
1. 同 scope + 同证据 + 同模型 → 第二次命中，0 token，记 cached_reply=1
2. 换证据 / 换 scope / **换模型**都不许命中（换模型复用旧答复会让对照实验测错对象）
3. 不给 reuse_scope → 两次都真调用（写代码的自愈 4 轮必须能拿到不同答案）
4. 空答复 / 失败答复不写缓存，不能变成下次的假命中
5. 蓝图指纹没变 → audit 复用，llm 调用次数不增；改一条能力才重新评审
6. 重规划无结构变更 → 直接跳过；有变更只把变更条目和涉及能力放进提示词
7. derive / replan 的 calls 不许把缓存命中算成「调用过」

跑法: python checks/check_reuse.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import blueprint as bp          # noqa: E402
from core import db, guards, remote, replan, settings_store, tokens  # noqa: E402

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    if cond:
        print("  OK  ", msg)
    else:
        FAILS.append(msg)
        print("  FAIL", msg)


TMP = Path(tempfile.mkdtemp(prefix="sixcoder_reuse_"))
WS = TMP / "ws"
WS.mkdir(parents=True, exist_ok=True)

_saved = {
    "db_path": db.DB_PATH, "schema_ready": db._SCHEMA_READY,
    "available": remote.available, "ladder": remote.ladder,
    "post": remote._post, "payload": remote._payload_for,
    "cost": remote.cost_cny, "get_remote": settings_store.get_remote,
    "guard": guards.gate_remote_call,
    "needs": tokens.needs_compression, "observe": tokens.observe,
}
_POST = {"n": 0, "answer": '{"caps": [], "issues": []}'}
_MODEL = {"name": "fake-model-a"}


def _fake_post(payload, base, key, timeout=300):
    _POST["n"] += 1
    data = {"choices": [{"message": {"content": _POST["answer"]},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                      "prompt_cache_hit_tokens": 0,
                      "completion_tokens_details": {"reasoning_tokens": 10}}}
    return data, "", 5


def _fake_ladder(profile=""):
    return [{"profile_id": "fake", "effort": "none", "model": _MODEL["name"],
             "max_output_tokens": 8000, "pricing": {}}]


try:
    # ── 隔离：临时 DB + 假远端，绝不碰真库、绝不花钱 ──────────────────
    db.DB_PATH = TMP / "test.db"
    db._SCHEMA_READY = False
    remote.available = lambda: (True, "")
    remote.ladder = _fake_ladder
    remote._post = _fake_post
    remote._payload_for = lambda step, r, messages, json_mode=False: (
        {"reasoning_effort": "none", "max_tokens": 8000}, "http://fake", "k")
    remote.cost_cny = lambda *a, **k: 0.0
    settings_store.get_remote = lambda: {"max_prompt_tokens": 950000,
                                         "max_prompt_chars": 4000000}
    guards.gate_remote_call = lambda *a, **k: {"ok": True}
    tokens.needs_compression = lambda t: (False, {})
    tokens.observe = lambda *a, **k: None

    print("[1] 同证据复用：确定性步骤")
    r1 = remote.complete("evidence-A", reuse_scope="blueprint-audit:plan")
    ok(bool(r1.get("ok")) and not r1.get("cached") and _POST["n"] == 1,
       "第一次真调用")
    r2 = remote.complete("evidence-A", reuse_scope="blueprint-audit:plan")
    ok(bool(r2.get("cached")) and _POST["n"] == 1,
       "第二次同证据命中缓存，远端调用次数不增")
    ok(r2.get("answer") == _POST["answer"] and
       (r2.get("usage") or {}).get("completion_tokens") == 0 and
       (r2.get("usage") or {}).get("cost_cny") == 0.0,
       "命中返回原答复，且 0 token / ¥0")
    rows = db.query("SELECT cached_reply, completion_tokens FROM remote_usage "
                    "WHERE cached_reply=1")
    ok(len(rows) == 1 and int(rows[0]["completion_tokens"] or 0) == 0,
       "用量库记一条 cached_reply=1、0 输出 token")

    print("[2] 换证据 / 换 scope / 换模型都不许命中")
    n = _POST["n"]
    remote.complete("evidence-B", reuse_scope="blueprint-audit:plan")
    ok(_POST["n"] == n + 1, "换证据重新调用")
    n = _POST["n"]
    remote.complete("evidence-A", reuse_scope="architect:architect")
    ok(_POST["n"] == n + 1, "换 scope 不串味")
    n = _POST["n"]
    _MODEL["name"] = "fake-model-b"
    r3 = remote.complete("evidence-A", reuse_scope="blueprint-audit:plan")
    ok(not r3.get("cached") and _POST["n"] == n + 1,
       "换模型不复用旧模型答复（对照实验不会测错对象）")
    _MODEL["name"] = "fake-model-a"
    n = _POST["n"]
    remote.complete("evidence-A", reuse_scope="blueprint-audit:plan")
    ok(_POST["n"] == n, "换回原模型仍能命中")

    print("[3] 创作路径保持不缓存")
    n = _POST["n"]
    a = remote.complete("same-author-prompt")
    b = remote.complete("same-author-prompt")
    ok(not a.get("cached") and not b.get("cached") and _POST["n"] == n + 2,
       "不给 reuse_scope 时两次都真调用（自愈 4 轮不被空转）")

    print("[4] 空答复不写缓存")
    _POST["answer"] = ""
    n = _POST["n"]
    remote.complete("evidence-empty", reuse_scope="blueprint:plan")
    e = remote.complete("evidence-empty", reuse_scope="blueprint:plan")
    ok(not e.get("cached") and _POST["n"] == n + 2,
       "空答复不落缓存，第二次不会假命中")
    _POST["answer"] = '{"caps": [], "issues": []}'

    # ── 审计缓存 ────────────────────────────────────────────────────
    print("[5] 蓝图指纹没变就复用评审")
    seen = {"n": 0, "prompts": []}

    def fake_llm(prompt, **kw):
        seen["n"] += 1
        seen["prompts"].append(prompt)
        return {"ok": True, "answer": '{"issues": []}'}

    cap = bp.Cap(id="cap.cli.count", desc="统计行数",
                 probe={"kind": "cli", "entry": "main.py", "args": ["count"],
                        "stdout_has": "2"},
                 deps=[], funcs_est=3)
    plan = bp.Blueprint(goal="做一个 CSV 统计工具", version=1, caps=[cap])
    out1 = bp.audit(plan, llm=fake_llm, ws=WS)
    ok(bool(out1.get("ok")) and seen["n"] == 1, "首次全量评审调用一次")
    out2 = bp.audit(plan, llm=fake_llm, ws=WS)
    ok(bool(out2.get("cached")) and seen["n"] == 1,
       "指纹未变直接复用，0 次远端调用")
    plan.version += 1
    plan.notes.append("只改了版本和备注")
    out3 = bp.audit(plan, llm=fake_llm, ws=WS)
    ok(bool(out3.get("cached")) and seen["n"] == 1,
       "只改 version / notes 不算结构变化，仍复用")
    plan.caps[0].desc = "统计行数（含空行）"
    out4 = bp.audit(plan, llm=fake_llm, ws=WS)
    ok(not out4.get("cached") and seen["n"] == 2,
       "改了一条能力 → 指纹变化 → 重新评审")

    print("[6] 重规划只审 diff")
    n = seen["n"]
    empty_diff = {"added": [], "removed": [], "rewired": {},
                  "descoped": [], "reopened": []}
    skipped = bp.audit(plan, llm=fake_llm, ws=WS, diff=empty_diff)
    ok(bool(skipped.get("skipped")) and seen["n"] == n,
       "无结构变更 → 整次复评跳过")
    changed = dict(empty_diff, added=["cap.cli.new"])
    out5 = bp.audit(plan, llm=fake_llm, ws=WS, diff=changed)
    prompt = seen["prompts"][-1]
    ok(bool(out5.get("ok")) and seen["n"] == n + 1,
       "有变更 → 走 diff 评审（不复用全量结论）")
    ok("cap.cli.new" in prompt and "cap.cli.count" not in prompt,
       "diff 提示词只带变更条目，不重列未变更能力")
    n = seen["n"]
    bp.audit(plan, llm=fake_llm, ws=WS, diff=changed)
    ok(bool(bp.audit(plan, llm=fake_llm, ws=WS, diff=changed).get("cached"))
       and seen["n"] == n,
       "同一份变更重复重规划 → 复用 diff 评审结论")

    print("[7] calls 口径：缓存命中不算调用过")
    WS2 = TMP / "ws_derive"        # 独立工作区：别让 [5] 写的评审缓存污染基线
    WS2.mkdir(parents=True, exist_ok=True)
    llm2 = {"n": 0}

    def derive_llm(prompt, **kw):
        llm2["n"] += 1
        return {"ok": True, "answer": json.dumps({
            "caps": [{"id": "cap.cli.count", "desc": "统计行数",
                      "probe": {"kind": "cli", "entry": "main.py",
                                "args": ["count"], "stdout_has": "2"},
                      "deps": [], "funcs_est": 3}],
            "issues": []}, ensure_ascii=False)}

    d1 = bp.derive("做一个 CSV 统计工具", llm=derive_llm, ws=WS2)
    d2 = bp.derive("做一个 CSV 统计工具", llm=derive_llm, ws=WS2)
    ok(int(d2.get("calls") or 0) == int(d1.get("calls") or 0) - 1,
       "derive 第二次少算一次审计调用")

    plan2 = bp.Blueprint(goal="做一个 CSV 统计工具", version=1,
                         caps=[bp.Cap(id="cap.cli.count", desc="统计行数",
                                      probe={"kind": "cli"}, deps=[], funcs_est=3)])
    rep = replan.replan(plan2, ws=WS2, llm=fake_llm, reason="无结构变化的停滞")
    ok(int(rep.get("calls") or 0) == 0,
       "replan 无结构变更时 calls=0（不把跳过算成调用）")

finally:
    db.DB_PATH = _saved["db_path"]
    db._SCHEMA_READY = _saved["schema_ready"]
    remote.available = _saved["available"]
    remote.ladder = _saved["ladder"]
    remote._post = _saved["post"]
    remote._payload_for = _saved["payload"]
    remote.cost_cny = _saved["cost"]
    settings_store.get_remote = _saved["get_remote"]
    guards.gate_remote_call = _saved["guard"]
    tokens.needs_compression = _saved["needs"]
    tokens.observe = _saved["observe"]
    shutil.rmtree(TMP, ignore_errors=True)

if FAILS:
    print("\n".join("FAIL " + m for m in FAILS))
    raise SystemExit(1)
print("全部通过（同证据复用 / 模型隔离 / 审计指纹缓存 / diff 评审）")
