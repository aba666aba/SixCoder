# projects/ — 只保留两份有证据价值的产物

这里的目录是夜班**真实跑出来**的工作区，不是样例代码。留两份就够，其余历史实验目录（`hq_*`、
`iter*`、`exp_*`、`night1` 等 25 个）已清理——它们都能用下面的命令重新生成。

| 目录 | 它证明了什么 | 独立验收 |
|---|---|---|
| `night2/` | **修复回归**：同一份产物从 13/15 → 16/17 → 19/19，中间那条 `store 端到端 TypeError` 是被判据抓住、再由 agent 自己改对的 | `19/19 PASS` |
| `night3/` | **从零起跑**：清空工作区后 10 轮 / 约 8 分钟跑完，引擎自报 8/8 且与独立验收一致 | `19/19 PASS` |
| `night3b/` | **3B 的天花板**：同一条需求、同一套机制、关掉远端，10 轮 5.3 分钟只有机械验收 2/8，绝大多数失败是「生成代码无法解析」 | `2/16 FAIL` |
| `night4/` | **机制自身引入的回归**：e2e 失败即撤销信用 → 引擎 3/8、原地打转；产物本身证明 store 没问题（`docs/17` §二） | — |
| `night6/` | **修完三条之后的对照**：6/8，不再自毁；`clean_row` 吞掉 id/name 被 e2e 与独立验收同时抓到 | `15/17` |

每个目录含 `DELIVERY.md`（早上交付的报告）与 `TRACE.md`（需求→判据→证据→产物 的追溯矩阵）。

## 复查命令（在 `SixCoder\` 下）

```powershell
# 独立验收：与夜班本体不共享任何代码，自己连库、自己断言
D:\Python\python.exe prototype\tools\hq_verify.py projects\night3

# 只重算交付报告（判据改过之后刷新，不必重跑一整夜）
cd prototype
D:\Python\python.exe tools\night_report.py night3

# 从零再跑一夜（会先清空同名目录）
D:\Python\python.exe tools\night_fresh.py night4 --hours 0.5
```

夜班的运行日志在仓库根的 `_logs/`；工作单与进度曲线在各自的 `.sixcoder/`（SQLite）。

## 提醒

**单次运行方差很大**（同一套机制 4/8～8/8 都出现过）。判断系统能力时要看多次运行的中位数，
不要拿一份 `DELIVERY.md` 当结论；能说明"机制真的有用"的是 `prototype/checks/` 里的反例测试。
