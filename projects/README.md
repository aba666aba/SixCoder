# projects/ — 只保留五个有证据价值的产物

这里的目录是夜班**真实跑出来**的工作区，不是样例代码。保留的是"能说明机制真的有用（或真的没用）"的五份；
其余历史实验目录（`ab_*`、`bench-*`、`calc*`、`night-v2*`、`night-smoke`、`hq_*`、`_repro_*` 等 **47 个**，
2026-11-23 已清理）都可以用下面的命令重新生成。

| 目录 | 它证明了什么 | 独立验收 |
|---|---|---|
| `night2/` | **修复回归**：同一份产物从 13/15 → 16/17 → 19/19，中间那条 `store 端到端 TypeError` 是被判据抓住、再由 agent 自己改对的 | `19/19 PASS` |
| `night3/` | **从零起跑**：清空工作区后 10 轮 / 约 8 分钟跑完，引擎自报 8/8 | **`22/25 FAIL`**（见下方"反面证据"） |
| `night3b/` | **3B 的天花板**：同一条需求、同一套机制、关掉远端，10 轮 5.3 分钟只有机械验收 2/8，绝大多数失败是「生成代码无法解析」 | `2/16 FAIL` |
| `night4/` | **机制自身引入的回归**：e2e 失败即撤销信用 → 引擎 3/8、原地打转；产物本身证明 store 没问题（`docs/17` §二） | — |
| `night6/` | **修完三条之后的对照**：6/8，不再自毁；`clean_row` 吞掉 id/name 被 e2e 与独立验收同时抓到 | `15/17` |

每个目录含 `DELIVERY.md`（早上交付的报告）与 `TRACE.md`（需求→判据→证据→ 追溯矩阵；`night3b` 只有前者）。

## 反面证据：`night3` 为什么留着

`night3` 的 `DELIVERY.md` 写「验收通过函数：8/8」，`TRACE.md` 写「证据全部为 engine_assertion」——
但用**同一仓库自带**的独立验收跑同一目录，结果是 **22/25 FAIL**：

```
FAIL clean_row(age 为 None) 不崩   AttributeError: 'NoneType' object has no attribute 'strip'
FAIL summarize([]) 不崩           ZeroDivisionError: division by zero
FAIL summarize([{age:None}]) 不崩  TypeError: unsupported operand type(s) for +: 'int' and 'NoneType'
```

同时 `test_clean.py` 全文只有一行 docstring（**0 个测试函数**），`test_report.py` 只断言"返回的 dict 里有
三个键"——`pytest` 却显示 `1 passed`。

这不是要卸磨杀驴：报告里的 8 条判据**确实**都过了，问题在于报告**没说自己没测什么**。
2026-11-23 已修（三处）：

1. `core/testweight.py` + `gates.gate_assertions` + `shipgate` 上线体检：把「0 个测试函数 / `assert True` /
   函数体只有 pass」判成空壳，"测试通过"不再是一句无信息量的话；
2. `company.write_delivery`：交付报告新增「这份报告能证明什么、不能证明什么」一节，**并现算一次独立验收**
   与报告并排印出；两边不一致时明示「以独立验收为准」；
3. 本 README 与 `DELIVERY.md` 的口径：**任何一份 `DELIVERY.md` 都只是"契约示例级"证据**。

## 复查命令（在 `SixCoder\prototype` 下）

```powershell
# 独立验收：与夜班本体不共享任何代码，自己连库、自己断言
python tools\hq_verify.py ..\projects\night3
# 只重算交付报告（判据改过之后刷新，不必重跑一整夜）
python tools\night_report.py night3
# 从零再跑一夜（会先清空同名目录）
python tools\night_fresh.py night4 --hours 0.5
```

> 中文 Windows 下如果看到 `UnicodeEncodeError`，说明用的是 2026-11-23 之前的版本：
> 那批脚本的入口没有切 UTF-8，`print("✓")` 会直接崩。新版本已在每个入口调用
> `core/console.encode_stdout()`，不再需要手动设置 `PYTHONUTF8=1`。

夜班的运行日志在仓库根的 `_logs/`；工作单与进度曲线在各自的 `.sixcoder/`（SQLite）。

## 提醒

**单次运行方差很大**（同一套机制 4/8～8/8 都出现过）。判断系统能力时要看多次运行的中位数，
不要拿一份 `DELIVERY.md` 当结论；能说明"机制真的有用"的是 `prototype/checks/` 里的反例测试。
