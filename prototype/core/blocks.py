"""分区块计价：说清"钱花在哪一块"（系统/协议/契约/源码/证据）。

为什么需要（照 DSH 的 token-meter 分区计价思路）：
    过去只有一个"总 token / 总花费"，看不出是协议写太长、还是证据重复太多。
    分区计价之后，"把协议压缩 20%" 这种优化才有可量化的依据。

口径：
    · token 数用官方 tokenizer **精确算**（不是 4 字符/token 的启发式，
      中文那套会低估 3 倍）；
    · 价格用当前模型档案的峰谷价（输入按"是否命中"分开：这里给"未命中全价"和
      "命中价"两列，便于看缓存到底省了多少）；
    · 一块内容的价格 = tokens / 1e6 × 单价。
"""
from __future__ import annotations

from . import layout
from . import tokens as _tokens


def _price_of(step: dict | None) -> dict:
    from . import settings_store
    if step and step.get("pricing"):
        return step["pricing"]["offpeak" if True else "peak"]
    models = settings_store.models()
    if models and models[0].get("pricing"):
        return models[0]["pricing"]["offpeak"]
    return {"in_hit": 0.0, "in_miss": 0.0, "out": 0.0}


def price(chunks: list[layout.Chunk], step: dict | None = None) -> dict:
    """按块计价。返回 {rows, total_tokens, total_miss_cny, total_hit_cny}。"""
    p = _price_of(step)
    rows = []
    tot_tok = 0
    for c in layout.plan(chunks):
        n = _tokens.estimate_tokens(c.text)          # 精确（有 tokenizer 时）
        tot_tok += n
        rows.append({
            "id": c.id,
            "stable": c.stable,
            "chars": len(c.text),
            "tokens": n,
            "cny_miss": round(n / 1e6 * float(p.get("in_miss") or 0), 6),
            "cny_hit": round(n / 1e6 * float(p.get("in_hit") or 0), 6),
        })
    total_miss = round(sum(r["cny_miss"] for r in rows), 6)
    total_hit = round(sum(r["cny_hit"] for r in rows), 6)
    return {"rows": rows, "total_tokens": tot_tok, "total_miss_cny": total_miss,
            "total_hit_cny": total_hit,
            "saved_cny": round(total_miss - total_hit, 6),
            "share": {r["id"]: round(r["tokens"] / max(1, tot_tok), 3) for r in rows}}


def as_lines(chunks: list[layout.Chunk], step: dict | None = None,
             limit: int = 8) -> list[str]:
    """渲染成报告里的几行（按 token 降序，只说花得最多的几块）。"""
    q = price(chunks, step)
    rows = sorted(q["rows"], key=lambda r: -r["tokens"])[:limit]
    out = [f"- 合计 {q['total_tokens']} tokens｜未命中全价 ¥{q['total_miss_cny']}"
           f"｜命中价 ¥{q['total_hit_cny']}（缓存能省 ¥{q['saved_cny']}）"]
    for r in rows:
        tag = "稳定" if r["stable"] else "易变"
        out.append(f"- {r['id']}（{tag}）：{r['tokens']} tokens，占 {q['share'][r['id']] * 100:.0f}%"
                   f"，¥{r['cny_miss']}")
    return out
