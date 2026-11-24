# SixCoder 结构说明

> 一句话：**`prototype/` 是唯一的产品代码；用户入口只有一个 —— 安装包 `SixCoder-Setup-*.exe`（根目录不再有任何 bat/ps1 启动脚本）；`开发文档.md` 是权威开发文档（认它，别认本文件）。**
>
> 本文件只回答"东西在哪、哪份是准的"。**行为口径一律以 `开发文档.md` 为准。**

## 目录

```
SixCoder/
├─ (无启动脚本)               ★ 用户入口 = 安装包；所有开关都在界面里（复核过：根目录零 bat/ps1）
│     安装包 dist-installer/SixCoder-Setup-<版本>.exe
│       ├─ 装到 %LOCALAPPDATA%\Programs\SixCoder（per-user，不弹 UAC，可覆盖安装）
│       ├─ 自带便携 Python + 依赖（用户不需要装 Python/Node）
│       └─ 控制面板可卸载；卸载页可选「保留本地运行数据」（默认保留）
├─ packaging/                打包与验收（build_installer.py 打包 / test_uninstall.py 真装真卸）
├─ prototype/                ★ 产品本体（FastAPI 后端 + Vue 前端）
│  ├─ server.py              HTTP 入口（含 /v1/settings/remote、/v1/night/* 等）
│  ├─ cli.py                 命令行入口（night / run / remote / plan / clean ...）
│  ├─ iterate_cli.py         迭代驱动器（工作单模式，工程式推进）
│  ├─ core/                  引擎（100+ 模块，唯一实现处）
│  ├─ checks/                自检脚本（python checks/run_all.py 一次全跑，227 个）
│  ├─ tools/                 引擎工具 + 运维脚本（看门狗、巡检、诊断）
│  ├─ web/                   前端（Vue3 + Vite，构建产物在 web/dist）
│  ├─ data/                  运行期数据（settings.json / capability.json / proto.db，均不入库）
│  └─ workspaces/            任务工作区（可随时删，已 gitignore）
├─ shell/                    Electron 桌面壳（只依赖 prototype；缺 Electron 会自动装或退化为浏览器）
├─ docs/                     设计文档与机制台账；`plan/` 计划基线；`legacy/` 旧产品 README
├─ projects/                 真实夜班产物（含 DELIVERY.md / TRACE.md）
├─ tools/                    上传前隐私体检（preupload_scan.py）+ git pre-push 守卫
├─ packaging/ dist/ dist-installer/   打包与安装产物
├─ _logs/                    运行日志（不入库）
├─ 开发文档.md                ★ 权威开发文档（改动纪律 / 架构 / 铁律 / 变更记录）
├─ HANDOFF-2026xxxx.md       交接文档（按日期，认最新那份）
├─ README.md                 顶层导航（作品说明 + 快速开始）
└─ STRUCTURE.md              本文件
```

## core/ 模块职责（按功能归类，细节看 `开发文档.md`）

| 组 | 模块 | 职责 |
|---|---|---|
| 编排主循环 | `company.py` | **夜班主循环**（最大最危险）：能力→工作单→写码→验收→记账；`write_delivery` 出交付报告 |
| | `auto.py` | 全自主模式编排（切片循环、续跑、报告） |
| | `agent.py` / `delegate.py` / `protocol.py` | 单任务编排 / 分派 / 字符串协议两端 |
| 能力蓝图 | `blueprint.py` / `blueprint_tree.py` / `slicer.py` / `plan_scale.py` | 能力表、依赖图、拓扑序、切片、规模折算 |
| | `frontier.py` / `blocks.py` / `ceiling.py` | 前沿推进、分块、能力上限台账 |
| 契约与判据 | `contract.py` / `contract_reverse.py` / `contractcheck.py` / `contractgen.py` | 契约解析/反向合成/自查/合成 |
| | `props.py` / `oracle.py` / `e2e.py` / `httpflow.py` / `flow.py` | 机械判据、计算校验、跨模块业务流、HTTP 链、集成冒烟 |
| | `gates.py` / `archrules.py` / `lint.py` / `quality.py` | 静态闸门族、架构规则、lint、质量指标 |
| | `probe.py` / `specsample.py` | 验收探针（cli/py_call/pytest/http）、权威样例 |
| 写作 | `author.py` / `autofix.py` / `linkfix.py` / `symedit.py` / `patch.py` / `dictfill.py` | 远端写码、确定性修复、保真渲染（libcst）、补丁、字典填充 |
| | `tools.py` / `sandbox.py` | 本地工具面、沙箱 |
| 远端与档位 | `remote.py` / `remotetools.py` / `effort.py` / `capability.py` | 请求体与用量、**原生工具循环**、**思考档位路由**、**模型能力表与工具通路降级** |
| | `toolschema.py` / `llm_cache.py` / `tokens.py` / `model.py` / `remotemodel.py` | 工具 schema、响应缓存、token 口径、模型档案 |
| 决策与调度 | `decision.py` / `strategy.py` / `policy.py` / `oscillation.py` / `rot.py` | 失败分类与动作、策略阶梯、协作策略、振荡检测、模块轮转 |
| | `priority.py` | **写码优先级（核心优先 · 只算不接）**：按能力依赖图排模块 |
| 账本与预算 | `coverage.py` / `budget.py` / `budget_estimate.py` / `calibration.py` / `debt.py` / `known_gap.py` | 覆盖账本、预算闸门、**两步法估算**、档位校准、临时方案台账、**已知缺口台账（只记录不降权）** |
| | `project.py` / `regression.py` / `selfcheck_loop.py` / `workorder.py` | 项目地图与冻结、回归库、自检回路、工作单模型 |
| 架构与治理 | `arch.py` / `architecture` 系 / `governance.py` / `constitution.py` / `doctor.py` | 架构健康、依赖方向、治理文件、宪法、巡检 |
| | `dataflow.py` / `interfaces.py` / `openhouse.py` / `shipgate.py` | 数据流、接口表、端点↔契约映射、Web 交付门禁 |
| 语言与规模 | `lang.py` / `scale.py` / `layout.py` / `readplan.py` / `specsample.py` | 语言识别与工具链、分级门禁、稳定前缀布局、读计划 |
| 基础设施 | `db.py` / `settings_store.py` / `vcs.py` / `gitops.py` / `isolate.py` / `sandbox.py` | 存储、设置、版本控制（只 add+commit）、隔离层、沙箱 |
| 入口与杂项 | `entrypoint.py` / `demo.py` / `docs.py` / `recon.py` / `extpoints.py` / `questions.py` / `ask.py` | 统一入口、演示、文档生成、诊断、扩展点、提问 |

> 完整清单以 `ls prototype/core/*.py` 为准（100+ 个，新增模块都会在 `开发文档.md` §9 的变更记录里留痕）。

## 常用命令

```powershell
cd SixCoder\prototype
python checks\run_all.py                       # 全部自检（227 项，PowerShell 通道跑）
python cli.py night --repo <dir> --goal "目标" --estimate   # 两步法第一步：只估算花费
python cli.py night --repo <dir> --goal-file <需求.md>      # 正式开工（续跑）
python cli.py plan show / plan slices          # 看蓝图 / 切片
python cli.py remote                           # 远端用量与成本
python cli.py clean --benchmarks --yes         # 清理基准任务与工作区
python server.py --port 8760                   # 起后端（开发态；用户态由安装包里的壳拉起）
```

夜班的起停都在界面：`POST /v1/night/detached` 起（独立进程 + 看门狗），`POST /v1/night/stop` 停；
检测 / 检测+修复 / 生成契约走 `POST /v1/ops/start`。**没有命令行脚本入口了。**

## 当前关键口径（细节看 `开发文档.md`）

- **预算**：唯一停机线 = 总花费 ≥ 系统总预算 × 1.05（写在**产物仓** `.sixcoder/budget.json` 的
  `system_budget_cny`，由 `core/budget_estimate.load_system_budget` 读取）。
  **不设上限的只有 `--max-cny` / `--hours` 这类一次性命令行参数**（已删除，防止跑一夜忘了设）；
  用户可以、也应该在 `.sixcoder/budget.json` 里给一个 `system_budget_cny` —— **不写这个文件 = 真的没有上限**
  （函数返回 0 = 不启停机线），这也是 README 让新手第一步就设预算的原因。
  口径：它是"系统给的总盘子"，引擎只能用它、不能自己改；CLI/界面没有任何参数能覆盖它。
  两步法：先 `--estimate` 估算 → 充值 → 再开工。
- **思考档位**：单模型 + 按难度自适应。起点按模块实测档案（`proven_floor`）+ 难度先验，
  失败 1 次即升档、成功即回最低档；**封顶恒为 max**（完全相信远端，不按成功率压封顶）。
- **工具通路**：按模型能力选 `native`（官方 function calling）/ `xml`（文本协议）/ `none`（无工具）；
  端点回"不支持 tools"会**自动降级并记住**，不当成能力不足。
- **写码优先级**：核心业务优先（按能力依赖图）—— 但**当前只算不接**，逻辑打磨好后才接进排程。
- **铁律**：每修一个真 bug 补一条自检断言；改引擎后必须重启夜班才生效；重启只挂看门狗。

## 清理原则

- **代码只留产品需要的**：`reference/`（旧 Java/Vue/RAG）与 `graft_demo/`（符号图研究分支）已删除，
  可复用的部分**只保留知识与设计契约**（`docs/15`、`docs/18`）。
- **用户入口只有一个 EXE**：`start.bat` / `start.ps1` / `stop.bat` / `stop.ps1` / `START_NIGHT.bat` / `STOP_NIGHT.bat`
  已全部删除（2026-11-20），功能搬进界面与 `server.py` 接口；打包脚本也从 ps1 收成 Python。
- 自检脚本集中到 `prototype/checks/`，统一入口 `run_all.py`（227 个，可任意 cwd 运行）。
- 运行期产物（`workspaces/`、`*.db`、`_logs/`、`node_modules/`、`dist/`）全部 gitignore，
  并把 `settings.json`（含明文 Key）与 `capability.json`（本地模型能力记忆）移出索引；
  `tools/git-hooks/pre-push` 负责拦住复发。
- 临时探针脚本一律放 `prototype/tools/_*.py` 并**当轮删掉**；本文件与 `开发文档.md` 都要跟着改。
