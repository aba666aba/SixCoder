"""自适应算力预算：把 best-of-N 花在难节点上（docs/12 §3 的最小落地）。

之前是固定 3 次尝试、固定温度表；现在按"节点难度"决定采样次数与温度，
简单节点少花算力，难节点多给几次机会。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Budget:
    attempts: int
    temps: list[float]
    use_strong: bool = False      # 是否值得升级到更强的模型（本机是 7B）
    why: str = ""


def difficulty(node: dict, contract: str = "", existing_failures: int = 0) -> float:
    """0..1 的难度先验，全部来自确定性信号。"""
    d = 0.0
    if node.get("kind") == "impl":
        d += 0.25
    if node.get("mode") == "whole":
        d += 0.15                      # 整文件新建比 patch 难
    body = (contract or node.get("contract") or "")
    if len(body) > 80:
        d += 0.10
    if any(k in body for k in ("上限", "下限", "校验", "异常", "抛", "非法", "边界")):
        d += 0.15
    if node.get("module") and node.get("path") != node.get("module"):
        d += 0.05
    d += min(0.25, 0.08 * existing_failures)
    return min(1.0, d)


def plan(node: dict, contract: str = "", existing_failures: int = 0) -> Budget:
    d = difficulty(node, contract, existing_failures)
    if d < 0.35:
        return Budget(2, [0.15, 0.25], False, f"难度 {d:.2f}：简单，2 次采样")
    if d < 0.65:
        return Budget(3, [0.15, 0.3, 0.45], False, f"难度 {d:.2f}：常规，3 次采样")
    # 实测：4-5 次采样只把墙钟从 183s 拉到 276s，通过数没有增加 → 上限收敛到 3
    return Budget(3, [0.1, 0.25, 0.4], True, f"难度 {d:.2f}：难，3 次采样（含高温重采样）")
