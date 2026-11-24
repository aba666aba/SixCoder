# tests/

SixCoder 的**回归安全网**。与 `prototype/checks/` 的分工：

| 目录 | 是什么 | 何时跑 |
|---|---|---|
| `prototype/checks/` | 227 个**自检脚本**：验"引擎的机制还成立吗" | 开发时手动跑，`run_all.py` |
| `tests/` | **回归测试**：冻结"重构前的行为"，防止改坏 | CI / 重构期间每次改动 |

## 为什么现在才建（2026-11-24）

路线 B 重构（把判据抽成可插拔）要动 30+ 个闸门。动手之前必须先能回答：

> 这次重构有没有改变任何现有行为？

`checks/` 回答不了 —— 它验的是"机制对不对"，不是"行为变没变"。
所以补这一层：**先记录现状，重构后要求逐字不变**。

## 内容

- `gate_snapshot.json` —— 闸门行为的基线快照（`tools/gate_snapshot.py --record` 生成）
- `test_gate_snapshot.py` —— 把快照比对接进 pytest
- `test_judge_contract.py` —— 阶段 1 的 Judge 协议契约测试

## 跑法

    cd prototype
    python -m pytest ../tests -q

或只跑快照：

    python tools/gate_snapshot.py --verify
