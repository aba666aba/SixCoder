"""缓存友好布局 + 委托 自检（这两条是"省输出、省输入"的核心机制）。

钉住四件事：
1. 稳定前缀里**不许出现**时间戳/轮次/任务号（出现就剥掉或降级为易变段）；
2. 布局是**确定性**的：同样输入 → 同样顺序、同样指纹（否则缓存必挂）；
3. 委托清单：最多 3 项、非法项丢弃、本地模型写的不合格要**打回**；
4. 协议里必须有 delegate 输出类型和"最多 3 项"的约束。

跑法: python check_layout.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import delegate, layout, protocol  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("缓存友好布局自检")
    chunks = [
        layout.Chunk("d_evidence", "失败：summarize 空输入除零 2026-11-04 17:02:33", False),
        layout.Chunk("c_contract", "clean.py: normalize_age(x: str) -> int", True),
        layout.Chunk("b_signatures", "def summarize(rows: list[dict]) -> dict", True),
        layout.Chunk("a_rules", "铁律：只输出 JSON。第 3 轮", True),
        layout.Chunk("e_order", "工作单：修 upsert（night-20261104170233）", False),
    ]

    # 1) 识别易变内容
    print("[1] 易变内容识别")
    check("识别时间戳", "时间戳" in layout.unstable_reasons("2026-11-04 17:02:33"))
    check("识别轮次", "轮次计数" in layout.unstable_reasons("第 3 轮"))
    check("识别任务号", "任务号" in layout.unstable_reasons("night-20261104170233"))
    check("干净文本不报易变", layout.unstable_reasons("def f(x: int) -> int") == [])

    # 2) 布局：稳定在前、易变在后，且前缀干净
    print("[2] 布局顺序与稳定前缀")
    order = [c.id for c in layout.plan(chunks)]
    check("稳定块排在前面", order[:3] == ["a_rules", "b_signatures", "c_contract"], str(order))
    check("易变块排在后面", order[3:] == ["d_evidence", "e_order"], str(order))
    prefix = layout.stable_prefix(chunks)
    check("稳定前缀里没有时间戳/轮次", layout.unstable_reasons(prefix) == [],
          str(layout.unstable_reasons(prefix)))
    check("稳定前缀包含契约与签名", "normalize_age" in prefix and "summarize" in prefix)
    check("易变内容没有丢（证据仍在全文里）", "除零" in layout.render(chunks))

    # 3) 确定性：同样输入两次 → 同指纹
    print("[3] 确定性（缓存能不能命中靠它）")
    f1 = layout.fingerprint(chunks)
    f2 = layout.fingerprint(list(chunks))
    check("两次布局指纹一致", f1 == f2, f"{f1} vs {f2}")
    shuffled = [chunks[0], chunks[4], chunks[2], chunks[1], chunks[3]]
    check("输入顺序变了，指纹不变（按 id 排序）",
          layout.fingerprint(shuffled) == f1, f"{layout.fingerprint(shuffled)} vs {f1}")
    changed = [layout.Chunk(c.id, c.text, c.stable) for c in chunks]
    changed[1] = layout.Chunk("c_contract", "clean.py: 契约变了", True)
    check("契约变了，指纹就变（该失效时就失效）",
          layout.fingerprint(changed) != f1)

    # 4) 体检报告
    print("[4] 布局体检")
    a = layout.audit(chunks)
    check("体检含稳定/易变清单与指纹",
          a["stable_chunks"] and a["volatile_chunks"] and a["fingerprint"])
    check("稳定占比可读（0~1）", 0 < a["stable_ratio"] <= 1, str(a["stable_ratio"]))

    # 5) 委托：规范化 + 上限 3 项
    print("[5] 委托（省远端输出）")
    good = [{"kind": "validation", "target": "upsert", "spec": "补参数校验"},
            {"kind": "logging", "target": "fetch_all", "spec": "加日志"},
            {"kind": "adapter", "target": "clean_row", "spec": "加适配层"},
            {"kind": "boilerplate", "target": "x", "spec": "第四项应被砍掉"}]
    norm = delegate.normalize(good)
    check("最多 3 项", len(norm) == 3, str(len(norm)))
    check("缺 spec 的项被丢弃",
          delegate.normalize([{"kind": "validation", "target": "f"}]) == [])
    check("未知 kind 归为 boilerplate",
          delegate.normalize([{"kind": "胡写", "target": "f", "spec": "s"}])[0]["kind"]
          == "boilerplate")

    # 6) 协议里要有 delegate 类型与"最多 3 项"
    print("[6] 协议约束")
    one = protocol.output_format("delegate")
    check("协议含 delegate 结构", '"delegate"' in one)
    check("协议写明最多 3 项", "最多 3 项" in one)
    check("协议写明不过会打回", "打回" in one)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（易变识别 / 稳定前置 / 指纹确定 / 委托上限与打回）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
