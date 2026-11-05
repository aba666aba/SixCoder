> ⚠️ **计划基线**（写于实现之前，保留作决策记录）。路径已按清理后的结构改写：
> `coder-core/` → `prototype/`｜`sixagent-web/` → `shell/`｜`sixagent-server/` **已删除**
> （设计契约抽到 `../15-多智能体架构映射.md`）｜`reference/` → `docs/legacy/`。
> 文中 `src/*` 包结构与 `python -m src.*` 命令**未采用**：实际入口是 `prototype/server.py`、
> `prototype/cli.py`。当前实况以 `../STRUCTURE.md` 与 `../15`–`../18` 为准。

# 07 Profile 与交付门禁

> "通用性"不是模型的能力，是 **Profile 的数量**。
> "可交付级"不是形容词，是 **8 条机器可判定的检查**。

---

## 1. Profile 是什么

一个 profile = 一种技术栈的**全部确定性知识**，纯数据 + 模板，不含业务逻辑：

```
src/profiles/spring-vue/
├── profile.yaml            # 声明式：命令、能力、目录约定、校验器
├── templates/
│   ├── entity.java.tpl
│   ├── mapper.xml.tpl
│   ├── service.java.tpl
│   ├── controller.java.tpl
│   ├── vue-view.vue.tpl
│   ├── migration.sql.tpl
│   ├── test.java.tpl
│   ├── Dockerfile.tpl
│   ├── docker-compose.yml.tpl
│   └── ci.yml.tpl
├── conventions.md          # 生成 CONVENTIONS.md 的种子（命名/分层/异常/日志/事务）
└── verify.ps1              # 独立自检：模板能否渲染 + 骨架能否编译 + 测试能否跑
```

### 1.1 `profile.yaml` schema（`src/profiles/schema.json` 校验）

```yaml
id: spring-vue
name: Spring Boot 3 + Vue 3
version: 1.0.0

layout:                       # 目录约定（确定性，不靠模型决定）
  java_root: src/main/java
  web_root:  src/main/webapp
  migrations: src/main/resources/db/migration

commands:                     # 闸门命令（G2/G3/G4/G5/G6）
  build:  "mvn -q -o -DskipTests package"
  types:  "mvn -q -o compile"
  lint:   "mvn -q -o spotless:apply"
  test:   "mvn -q -o test -Dtest={test_class}"
  smoke:  "docker compose up -d --wait && curl -fsS http://localhost:{port}/actuator/health"

model:
  main:   qwen2.5-coder:3b
  strong: qwen2.5-coder:7b

node_kinds:                   # 本 profile 支持哪些节点类型（决定展开规则）
  - schema
  - iface
  - impl
  - test
  - config
  - assembly

capabilities:                 # ★ 能力清单 = 对用户的承诺边界
  crud-api:      {templates: [entity, mapper, service, controller, migration, test]}
  jwt-auth:      {templates: [security_config, login_controller, jwt_util, test]}
  file-upload:   {templates: [upload_controller, storage_service, test]}
  pdf-export:    {templates: [pdf_service, controller_patch, test]}
  scheduled-job: {templates: [job_service, scheduler_config, test]}

unsupported:                  # ★ 显式不支持，开工前就告知
  - payment-gateway
  - realtime-collab
  - 第三方 OAuth 对接

limits:                       # 硬边界，越界直接拒绝而不是硬做
  max_entities: 8
  max_nodes: 150
  no_cross_service_call: true
```

---

## 2. 能力求交：在开工前拦掉"做不了的需求"

> 求交之前还有一步：**技术栈自动选型**（需求 → 打分 → 推荐，见 [11 §1](./11-技术栈选型与工具链配置.md)）。
> 选型的环境可用性是硬约束——**不推荐本机跑不起来的栈**。
> 求交则判断"所选栈能不能做这件事"。两者一前一后，共同构成"开工前的承诺边界"。

`src/profiles/match.py`（**确定性代码，不用模型**）：

```
需求文本
  │  (a) 关键动词/名词抽取：用 keyword_extract + 规则词典（不花模型 token）
  │  (b) 与 profile.capabilities.keys() 求交
  ▼
命中集合 H  /  未命中集合 M
  │
  ├── H 非空 且 M 为空      → 正常进入规格化
  ├── H 非空 且 M 非空      → 生成"部分支持"清单，**要求用户确认或删减需求**
  └── H 为空                → 立即返回"当前 profile 不支持"，不进入开发
```

**这一步是"可交付"和"画大饼"的分界线。** 没有它，3B 会对着一个它做不了的需求狂写 40 个节点，最后交付一堆跑不起来的东西；有了它，系统在需求阶段就守住了承诺。

求交之后还要做**规模校验**（`limits.max_entities` 等）：实体数、预估节点数超限 → 拒绝并建议拆分需求。

---

## 3. 契约先行（把"写对"变成编译期问题）

进入开发前，先生成一批**机器可校验**的产物，之后所有实现都只是"让这些变绿"：

| 产物 | 形态 | 校验 |
|---|---|---|
| 接口签名 | Java interface / TS type / Python Protocol | 编译通过 |
| 数据库 DDL + 迁移 | 可执行 SQL | 在空库上执行 + 重放一次 |
| 请求/响应 schema | JSON Schema | 实例校验 |
| **测试骨架** | 可运行的**失败**测试（红） | 必须红；绿了视为无效测试 |
| 需求→测试映射 | `requirement_id → test_id` | 每条需求至少 1 条 |

这条策略的收益是复利的：类型、迁移、API 契约、测试这些"交付级标配"由**机制产出**，不靠模型临场发挥，因此不会漂移；模型的自由度从"整个文件"缩到"一个函数体"。

---

## 4. 八条交付门禁（`src/delivery/checklist.py`）

逐条实现，全绿才允许进入人工签核。任一不过，流程**停在 delivery 阶段**并输出可读修复建议。

| # | 门禁 | 判定方式 | 常见失败原因 |
|---|---|---|---|
| D1 | 干净构建 | 空目录 `git clone` → 一条命令 → 退出码 0 | 缺依赖声明、路径硬编码 |
| D2 | 测试全绿 | 全量测试退出码 0，且覆盖率报告存在 | 测试依赖外部服务 |
| D3 | CI 可本地等价执行 | 解析 CI 配置里的命令并逐条真跑 | CI 写了没跑过的命令 |
| D4 | `docker compose up` 可起 | 起容器 + 健康检查 HTTP 200 + `down` 清理 | 端口冲突、缺 healthcheck |
| D5 | 迁移可重放 | 空库执行 → 清库 → 再执行 → 结果一致 | 非幂等 DDL |
| D6 | 配置齐全无密钥 | `.env.example` 存在 + `scan_secrets.py` 无命中 | 明文 key、硬编码密码 |
| D7 | README 命令真跑 | 提取 README 中所有代码块命令 → 逐条执行 | README 与实际不符（**最高频**） |
| D8 | 需求→测试映射完整 | 每条需求至少 1 条测试且该测试通过 | 需求漏实现 |

补充的**软告警**（不阻断，但写进报告）：

- 无 `TODO`/`FIXME`/`raise NotImplementedError` 残留（有则必须是 `NEEDS_HUMAN` 标记过的）
- 无调试输出（`print` / `console.log` / `System.out`）
- 许可证头齐全
- 每个公开函数有 docstring/注释

> D7 是最容易被忽视又最能体现"可交付"的一条：绝大多数 AI 生成仓库的 README 里都写着跑不通的命令。用脚本真跑一遍，这个陋习就没了。

---

## 5. 交付物形态

```
workspaces/<task_id>/
├── （生成的仓库本身：源码 / 迁移 / 测试 / Dockerfile / compose / CI / README）
└── .sixcoder/
    ├── report.md             # 交付报告（摘要/映射/闸门统计/NEEDS_HUMAN/复现命令）
    ├── report.json
    ├── spec.json             # 规格（可再审）
    ├── graph.json            # 任务图快照（可重放）
    ├── traces/               # Agent 轨迹 JSONL（可回溯）
    └── gate_runs.jsonl       # 每次闸门原始记录
```

`.sixcoder/` **不进交付仓库**（写进 `.gitignore`），但随交付一起交给人——审阅者可以据此判断哪些地方是模型写的、哪些是模板生成的、哪些需要人补。

---

## 6. 通用性的扩张路线（重要：不要一上来做三个 profile）

| 阶段 | 范围 | 目标 |
|---|---|---|
| 现在 | **只做 `spring-vue`** | 端到端通过率 ≥ 80%，D1–D8 全绿率 ≥ 60% |
| +1 周 | 补 `spring-vue` 的能力（jwt-auth、file-upload） | 覆盖更多需求而不改引擎 |
| +2 周 | 第二个 profile：`python-fastapi` | 验证"引擎与 profile 解耦"这个假设 |
| 以后 | `node-cli` / 你们真实用到的栈 | 每个新 profile 只写数据与模板 |

**判据**：新加一个 profile 时，如果必须改 `src/codegen/ src/gates/ src/tasks/` 里的代码，说明抽象漏了——先修抽象，再加 profile。**宁可一个 profile 做到 95%，不要三个各 60%。**
