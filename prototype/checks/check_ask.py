"""精修模式"主动提问" + 分区块计价 自检（照 DSH 的 ask_user_question / token-meter 思路）。

钉住四件事：
1. 提问是工具化的：有 id、有选项、能回答，答案作为 human 证据入账；
2. 同一个问题**只问一次**，之后复用答案（不重复骚扰人）；
3. plan-mode：造物模式自动通过、精修模式必须人审；
4. 分区块计价：能算出"每一块占多少 token、花多少钱、缓存能省多少"，
   且用**官方 tokenizer 精确算**（不是 4 字符/token 的启发式，中文会低估 3 倍）。

跑法: python check_ask.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import blocks, layout, questions, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("主动提问 + 分块计价 自检")
    ws = Path(tempfile.mkdtemp())
    q = "age 缺失时按 0 还是跳过？"

    # 1) 提问 → 待答 → 回答 → 证据入账
    print("[1] 提问通道")
    r1 = questions.ask(ws, q, options=["按 0", "跳过"], why="契约没写", node="clean_row")
    check("首次提问不阻塞且登记为待答", r1["ok"] is False and bool(r1["qid"]))
    check("选项被保留", (questions.pending(ws)[0]["options"]) == ["按 0", "跳过"])
    r2 = questions.answer(ws, r1["qid"], "跳过", by="human")
    check("回答成功", r2["ok"] and r2["answer"] == "跳过")
    check("待答列表清空", questions.pending(ws) == [])
    audit = (ws / ".sixcoder" / "audit.jsonl").read_text(encoding="utf-8")
    check("审计里有 ask 与 answer 两条", '"action": "ask"' in audit and '"action": "answer"' in audit)
    hist = (ws / ".sixcoder" / "answers.jsonl").read_text(encoding="utf-8")
    check("答案进历史库（human 证据）", "跳过" in hist and '"by": "human"' in hist)

    # 2) 同一问题只问一次
    print("[2] 不重复骚扰")
    r3 = questions.ask(ws, q)
    check("同问题二次提问直接复用答案", r3["ok"] and r3["cached"] and r3["answer"] == "跳过")
    check("没有新增待答", questions.pending(ws) == [])

    # 3) plan-mode：甲方自动通过 / 开发者必须人审
    print("[3] plan-mode 流程")
    c = questions.review_plan(ws, "先写 clean 再写 store", mode="client")
    check("造物模式自动通过", c["ok"] and c["approved"] and c["auto"])
    d = questions.review_plan(ws, "先写 clean 再写 store", mode="developer")
    check("精修模式要人答（不自动通过）", d["ok"] is False and d["auto"] is False)
    qid = d["qid"]
    questions.answer(ws, qid, "通过")
    d2 = questions.review_plan(ws, "先写 clean 再写 store", mode="developer")
    check("人回答「通过」后 approved=True", d2["ok"] and d2["approved"], str(d2))

    # 4) 分区块计价
    print("[4] 分区块计价")
    chunks = [
        layout.Chunk("a_rules", "铁律：只输出 JSON。" * 40, True),
        layout.Chunk("b_protocol", "本地工具清单：语法/签名/边界矩阵…" * 20, True),
        layout.Chunk("c_contract", "clean.py: normalize_age(x: str) -> int" * 25, True),
        layout.Chunk("z_evidence", "失败：summarize 空输入除零" * 15, False),
    ]
    q1 = blocks.price(chunks, settings_store.models()[0])
    check("每块都有 token 数", all(r["tokens"] > 0 for r in q1["rows"]), str(q1["rows"][:1]))
    check("占比加起来 ≈ 1", abs(sum(q1["share"].values()) - 1.0) < 0.02, str(q1["share"]))
    check("给出未命中价与命中价两列",
          q1["total_miss_cny"] >= q1["total_hit_cny"] > 0, str(q1)[:120])
    check("算出缓存能省多少", q1["saved_cny"] > 0, str(q1["saved_cny"]))
    lines = blocks.as_lines(chunks, settings_store.models()[0])
    check("报告行按 token 降序且注明稳定/易变",
          any("稳定" in x for x in lines) and any("易变" in x for x in lines), str(lines[:2]))

    # 5) 精确计数（不是 4 字符/token 的启发式）
    print("[5] 计价用的是精确 token")
    from core import tokens
    if tokens.mode() == "exact":
        n_cn = tokens.exact_count("写一个计算器，支持加减乘除")
        check("中文 13 字 = 9 token（4 字符/token 会算成 3，低估 3 倍）", n_cn == 9, str(n_cn))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（提问工具化 / 只问一次 / plan 人审 / 分块计价精确）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
