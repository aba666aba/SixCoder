# SixCoder 结构说明（清理后）

> 一句话：**`prototype/` 是唯一的产品代码；一键启动是根目录的 `start.ps1` / `start.bat`；`docs/` 是设计与实测；其余都是可再生产物或归档。**

## 目录

```
SixCoder/
├─ start.bat               ★ 一键启动（双击即可）：后端 + 桌面窗口
├─ start.ps1               启动器本体（-Web / -NoShell / -Port / -Rebuild）
├─ stop.ps1                停后端 + 关窗口
├─ prototype/              ★ 产品本体（FastAPI 后端 + Vue 前端）
│  ├─ server.py            HTTP 入口（含 /v1/settings/remote 等）
│  ├─ cli.py               命令行入口（run / handoff / remote / clean / regressions ...）
│  ├─ iterate_cli.py       迭代驱动器（工作单模式，工程式推进）
│  ├─ core/                引擎（唯一实现处）
│  ├─ checks/              自检脚本（python checks/run_all.py 一次全跑，99 个）
│  ├─ tools/               fixtures / legacy（历史脚本）/ night_* · trace（运维工具）
│  ├─ web/                 前端（Vue3 + Vite，构建产物在 web/dist）
│  ├─ data/                运行期数据（settings.json / api_index.db / proto.db，均不入库）
│  └─ workspaces/          任务工作区（可随时删，已 gitignore）
├─ shell/                  Electron 桌面壳（只依赖 prototype；缺 Electron 会自动装或退化为浏览器）
├─ docs/                   当前实况（15–27 + 模型门槛）；`plan/` 计划基线 01–13；`legacy/` 旧产品 README
├─ projects/               真实夜班产物（含 DELIVERY.md / TRACE.md），见 projects/README.md
├─ tools/git-hooks/        pre-push 守卫：拦住"把密钥/运行期文件推上远端"
├─ _logs/                  运行日志（不入库）
├─ README.md               顶层导航（含一键启动与实测状态）
└─ STRUCTURE.md            本文件
```

> 已删除并只保留知识的目录：
> `reference/`（原始 Spring/Vue/RAG 代码）→ 设计契约抽到 `docs/15-多智能体架构映射.md`；
> `graft_demo/`（符号图研究分支）→ 实测数字与结论抽到 `docs/18-仓库规模与符号图实测.md`。

## core/ 模块职责（一眼看懂）

| 模块 | 职责 |
|---|---|
| `contract.py` | **结构化契约**（函数/参数/类型/返回/异常/示例/键），所有闸门与生成器读它 |
| `model_policy.py` | **模型门槛（硬）**：本地模型必须 ≥7B；`verdict/require/enforce` 是唯一判定点，后端/CLI/夜班/界面都读它 |
| `permissions.py` | **三种模式（甲方/开发者/只读）**：能力矩阵、auto_approve、关键节点 |
| `approval.py` | 人工审核门（needhuman）：单据落盘 `_logs/approvals/`，跨进程 HTTP 审批，超时按拒绝 |
| `guards.py` | 五个关键节点守卫：写文件 / 提交 / 推送 / 上报远端 / 收尾交付 |
| `planner.py` | 需求 → 模块切分（`_module_blocks`）→ 依赖排序 |
| `agent.py` | 单任务编排：节点/两段式拆分/闸门串联/重试与预算 |
| `iterate.py` | **迭代驱动**：体检（无机械判据不给信用）→ 工作单 → 执行 → 机械验收 → 记账；`add_engine_tests` 用契约渲染 pytest |
| `workorder.py` | 工作单数据模型 + 机械分诊（缺失/桩/判据没过）+ 架构师提示词与解析 |
| `gates.py` | 闸门族：语法/类型/签名/桩/API/完整性/SQL 行形态/**安全**/**依赖**/反逃避/断言/测试/变异；`gate_field_map` 报字段歧义（不阻塞） |
| `props.py` | 引擎判据：示例 / 异常 / 往返性质 / 类型冒烟 / 返回键 / 数据库写→读端到端（全部来自契约） |
| `e2e.py` | **跨模块业务流判据**：从契约类型认出"清洗→入库→汇总"，真 sqlite 跑 5 条跨模块不变式 |
| `trace.py` | **追溯矩阵 TRACE.md**：需求条目 → 契约 → 判据 → 证据 → 产物；含缺口与待确认歧义 |
| `regression.py` | 回归库：成功过的判据永久生效 |
| `symedit.py` | 保真渲染（libcst）：只替换目标节点，其余字节不变 |
| `autofix.py` | 确定性修复：按契约重写参数、补缺失 import |
| `apikb.py` | API 知识库：真实签名回填 + 幻觉 API 检测 |
| `remote.py` | 远端（DeepSeek）：前缀缓存友好的包体、思考关闭、用量与闲时/忙时计费 |
| `handoff.py` | 远端接力：打包 → 远端符号编辑 → 本地渲染 + 闸门 |
| `settings_store.py` | 设置存储（api key 明文存本地、永不回显、只写变化项） |
| `probe.py` | **验收探针**：`cli` / `py_call` / `pytest` / `http`（多步链、`split` 跨进程、`parallel` 并发、`expect_count` 条数） |
| `shipgate.py` | **Web 交付机制**：机制能力注入、`.env` 生成、引擎生成 pytest、上线体检（导入/`/docs`/uvicorn/README/pytest） |
| `deps.py` | **依赖自举**：扫产物 import → 补装缺失包 → 写 `requirements.txt`（检测 FastAPI 自动带 uvicorn） |
| `context.py` / `budget.py` / `skeleton.py` / `oracle.py` / `style.py` / `tools.py` / `db.py` | 上下文预算 / 尝试预算 / 骨架 / 计算校验 / 风格 / 工具与沙箱 / 存储 |

## 常用命令

```powershell
cd SixCoder\prototype
python checks\run_all.py                       # 全部自检（99 项）
python cli.py run --repo <dir> --requirement-file <f> --model qwen2.5:7b
python iterate_cli.py --repo <dir> --requirement-file <f> --model qwen2.5:7b --rounds 4
python cli.py handoff <task_id> --node <n> --remote --apply
python cli.py remote                            # 远端用量与成本
python cli.py clean --benchmarks --yes          # 清理基准任务与工作区
python server.py --port 8760                    # 起后端（或用 shell/ 的桌面壳）
```

## 清理原则（本次做了什么）

- **代码只留产品需要的**：`reference/`（旧 Java/Vue/RAG）与 `graft_demo/`（符号图研究分支）已删除，
  可复用的部分**只保留知识与设计契约**（`docs/15`、`docs/18`）。
- **一键启动在根目录**：`start.bat` / `start.ps1` / `stop.ps1`，路径与依赖都由脚本自己找。
- 自检脚本集中到 `prototype/checks/`，统一入口 `run_all.py`（99 个，可任意 cwd 运行）。
- 独立验收脚本提到 `prototype/tools/hq_verify.py`（与引擎不共享代码，是最终裁判）。
- 运行期产物（`workspaces/`、`*.db`、`_logs/`、`node_modules/`、`dist/`）全部 gitignore，
  并把 `settings.json`（含明文 Key）从索引移除；`tools/git-hooks/pre-push` 负责拦住复发。
- 顶层只保留：`prototype/ shell/ docs/ projects/ tools/ _logs/` + `start.*` / `stop.ps1` + 四个元文件。
