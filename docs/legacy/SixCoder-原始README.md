# SixAgent Coder（旧产品 README · 归档）

> ⚠️ **这是改造前那套 Java/Spring 六 Agent 产品的 README**，其代码已删除，仅作历史留存。
> 当前项目说明见仓库根 [README.md](../../README.md)，结构见 [STRUCTURE.md](../../STRUCTURE.md)。
> 下文里的链接已按归档位置修正，但**内容与目录名均属旧产品**，不要照着操作。

> 结构导航见 [STRUCTURE.md](../../STRUCTURE.md)｜能力边界与模型门槛见 [模型门槛与能力边界.md](../模型门槛与能力边界.md)

- **产品代码**：prototype/（后端 + 引擎）、shell/（桌面壳）、prototype/web/（前端）
- **设计与实测**：docs/
- **研究分支**：graft_demo/（符号图 / 万级仓库实测，已删除，结论见 docs/18）
- **只读参考**：
reference/（原始 Spring/Vue/RAG 代码，已删除，结论见 docs/15）
- **自检**：cd prototype && python checks\run_all.py

# SixAgent 使用说明

> ## ⚠️ 本仓库正在改造为「本地 AI 编程助手」（进行中）
>
> 当前仓库承载**两个阶段**的内容，请先看 [docs/README.md](../README.md) 再读下文：
>
> | 目录 | 定位 |
> |---|---|
> | `coder-core/` | **智能内核**（Python 3.12）—— 源自 `ai文献助手` 的 RAG + ReAct Agent，承载全部 Agent/检索/闸门能力 |
> | `sixagent-web/` | **交付外壳**（Electron + Vue3）—— 沿用本项目的打包与界面 |
> | `sixagent-server/` | **参考实现**（Spring Boot + MySQL）—— 不再进主链路，**默认不参与构建** |
> | `docs/` | 开发文档（14 篇）：总体设计 / 目录映射 / 继承取舍 / 工具协议 / 闸门 / 任务图 / Profile / 里程碑 / 环境 / **仓库形态识别** / **技术栈选型与工具链** / **质量最大化** / **判定权分离与自认可防护** / 索引 |
>
> 下文的"六个 Agent 自动完成全流程"描述的是**改造前**的 SixAgent。新系统的定位与承诺边界见 `docs/01-总体设计.md` §1.2，它明确**不做**全自动无人值守交付。
>
> 改造完成前，`sixagent-server` 与下文的安装/配置说明仍然有效，可照旧运行。

SixAgent 是一个多智能体研发协同平台，六个 AI Agent 自动完成从需求分析到部署上线的全流程。

---

## 免责声明

1. **调用外部大模型会计费**：系统所有 Agent 均调用外部 LLM API（默认 `https://api.deepseek.com`）。每次运行至少产生 5 次长上下文调用（单项目上下文预算 100K 字符），**费用由使用者自行承担**。
2. **工程源码会发送到第三方**：整包上下文被注入 Prompt，其中 `yml` / `properties` / `json` / `sql` 类文件走全文注入 → **你项目里的配置与密钥可能被上传到第三方模型服务**。请勿在受保密约束的代码库上直接使用。
3. **AI 结论不等于质量保证**：评审的 P0/P1/P2 分级与"编译通过"是由 LLM 文本解析生成的；测试结论也来自文本解析（仅有 `javac` 一步是真实执行）。不要把它的结论当作权威评审。
4. **会对工程目录执行破坏性 Git 操作**：每步自动 `git add -A` + `git commit`；失败重试会执行 `git reset --hard <hash>`。**请务必在专用副本/分支上运行，不要在重要仓库的工作区直接跑。**
5. **无鉴权 + 通配 CORS**：后端默认监听 8080，未引入 Spring Security，`CorsConfig` 允许任意来源且 `allowCredentials(true)`。**若部署在公网，任何网页都可调用你的 LLM 密钥跑任务、写文件、执行 git/javac。请勿将 8080 暴露到公网。**
6. **密钥与日志**：`llm-config.yml` 含明文 API Key（已在 `.gitignore` 中）；执行日志会把完整 System Prompt / User Message / LLM Response 写入 `output/logs-{taskId}/` 并入库。历史安装包中曾固化真实密钥，**分发前请先清除配置**。
7. **不作任何担保**：本项目为实验性工具，自动生成的代码与部署方案需人工审核后才能用于生产。

## 安装与启动

### 桌面版（推荐）

下载 `SixAgent Setup 1.0.0.exe` 安装，启动后自动打开窗口。

**环境要求**：Java 17+、MySQL 8.0（运行中即可，数据库自动创建，无需手动建库）。

**首次配置**：安装目录下找到 `llm-config.template.yml`，改名为 `llm-config.yml`，填入数据库密码和各 Agent 的 API Key 后，双击桌面快捷方式启动。

### 命令行启动（开发模式）

```bash
cd sixagent-server && mvn spring-boot:run    # 后端，端口 8080
cd sixagent-web && npm install && npm run dev # 前端，端口 5173
```

---

## 配置 API 密钥

将安装目录下的 `llm-config.template.yml` 改名为 `llm-config.yml`，填入数据库密码和 LLM API 密钥：

```yaml
DB_URL: jdbc:mysql://localhost:3306/sixagent?createDatabaseIfNotExist=true&useUnicode=true&characterEncoding=UTF-8&serverTimezone=Asia/Shanghai
DB_USER: root
DB_PASS: 你的数据库密码

llm:
  max-tokens: 4096
  agents:
    orchestrator:
      provider: deepseek
      api-key: 你的API密钥
      base-url: https://api.deepseek.com
      model: deepseek-v4-pro
    architect:
      provider: deepseek
      api-key: 你的API密钥
      base-url: https://api.deepseek.com
      model: deepseek-v4-pro
    coder:
      provider: deepseek
      api-key: 你的API密钥
      base-url: https://api.deepseek.com
      model: deepseek-v4-pro
    reviewer:
      provider: deepseek
      api-key: 你的API密钥
      base-url: https://api.deepseek.com
      model: deepseek-v4-pro
    tester:
      provider: deepseek
      api-key: 你的API密钥
      base-url: https://api.deepseek.com
      model: deepseek-v4-pro
    deployer:
      provider: deepseek
      api-key: 你的API密钥
      base-url: https://api.deepseek.com
      model: deepseek-chat
  providers:
    deepseek:
      base-url: https://api.deepseek.com
      model: deepseek-chat
    qwen:
      base-url: https://dashscope.aliyuncs.com/compatible-mode
      model: qwen-plus
    openai:
      base-url: https://api.openai.com
      model: gpt-4o
```

6 个 Agent 可独立设置不同厂商、密钥、模型。支持 DeepSeek / 通义千问 / OpenAI 及兼容接口。

也可以启动后在界面顶栏点击 ⚙ 按钮，在弹窗中在线修改各 Agent 的 API 配置，保存即时生效。

---

## 开发进度与已知不足

### 已实现（代码可验证）

- Spring Boot 3.2 + Java 17 后端（55 个 Java 文件）；MySQL 8 真实连接（`createDatabaseIfNotExist=true`）
- **真实的 Flyway 数据库迁移**：6 个版本、156 行 SQL、8 张表
- **真实的 LLM HTTP 集成**（OpenAI 兼容协议），6 类 agentType 可独立配置 provider/key/model/超时/token，支持运行期热更新并回写 yml
- **5 个执行 Agent 均为真实 `@Service` Bean**，各有完整提示词与产物落库/落盘：architect / coder / reviewer / tester / deployer
- 事件驱动链式编排；评审 → 修复 → 重审循环（含轮次上限 `max-rounds: 3`）
- 失败自动重试 + Git 安全点回滚 + 跳过/中止策略；启动时恢复僵死任务（5 分钟阈值）
- **真实编译验证**（真跑 `javac -encoding UTF-8 -d _build`，读退出码），失败触发修复链路
- 真实 Git 集成（自动 `git init`、写 `.gitignore`、commit、diff、HEAD），提交记录入库
- 人在回路：自然语言指令注入 LLM 上下文（5 个 Agent 均调用）
- 前端 Vue 3 + Element Plus + Pinia 可运行（6 个标签页，3 秒轮询）；Electron 桌面封装 + NSIS 打包

### 未实现 / 与文档不符（如实列出）

- **"六个 Agent"里第 6 个没有可运行实体**：`sys_agent` 表插入了 6 行，但**全项目无任何代码查询该表**；调度用硬编码 `AGENT_MAP`。实际只有 **5 个 Agent Bean**，第 6 个"调度 Agent"就是 `OrchestratorService` 本身
- **"部署上线"未实现**：`DeployerService` 全程只调用一次 LLM 生成部署文档并写库，**无打包、无启动、无健康检查**；文档中承诺的 `readme-update` Agent 也不在 6 类枚举内（仅在历史日志中出现过）
- **README 称"DAG（有向无环图）编排"，实际是有序 `List<String>`**：按 `createTime` 取首条执行，下游依赖靠 `taskId - 1` 猜测；无拓扑排序、无并行分支、无依赖校验
- **前端依赖声明了但未接线**：Monaco Editor 与 Mermaid 在 `src` 中**零 import**（代码只用 `<pre>` 展示文本）
- **"全局历史"标签页不存在**：`App.vue` 只有 6 个 tab；`HistoryView.vue` 未被任何文件 import；`vue-router` 从未 `app.use(router)` → 整个路由层是死代码
- **"重新执行(带反馈)"按钮永不渲染**（需 `agentProgress.status===2`，各视图均不传该 prop）
- 专题页只有"对话记录 + 任务表格"：评审问题清单 / 测试用例 / 部署记录 / 架构文档 / 代码内容五组 API **有后端无前端**
- `code_artifact.compile_status` 恒写 1（真实编译结论来自 Tester）
- **无任何单元或集成测试**：`src/test` 目录不存在；`pom.xml` 声明了 `spring-boot-starter-test` 但无测试类；打包命令显式 `-DskipTests`
- **确定性 bug**：`git config user.name SixAgent` 缺少 `--`，等价于 `--get` → 提交身份从未被设置，后续 `git commit` 必然失败 → Git 提交历史功能实际不可用
- **回滚/查看 API 与工作目录脱钩**：`TraceController` 不接收 `workingDir`，前端也不传 → 落到 `user.dir/output`，对用户真实仓库无效，而 `reset --hard` 具破坏性
- `retryCounts` 用内存 Map，重启即清零；`AsyncConfig` 空配置（默认 `SimpleAsyncTaskExecutor`，并发无上限）
- 配置默认值三处不一致（读超时 600s vs UI 上限 600 vs README 写 180s；max-tokens 16384 / 8192 / 4096 三处不同）
- **API Key 掩码回写会破坏真实密钥**：`GET /api/agent/config` 返回掩码，前端原样提交，后端非空即覆盖落盘 → 用户点一次保存真实 key 就被 `sk-a***z` 覆盖
- `/api/task/{id}/retry` 语义错误（实际调用 rollback）；大量 `catch (Exception ignored) {}` 静默吞错
- LLM 需求分析失败时静默回退到 `DEFAULT_CHAIN`，用户无感知
- 仓库内含旧构建产物（`static/` 下 20 个 assets，`index.html` 引用源码中不存在的 `katex@0.16.9`）
- 无 CI / Dockerfile / 部署脚本

## 使用流程

### 1. 选择工作目录

启动后在首页选择你的项目目录（即要开发的代码所在目录）。Agent 会自动扫描目录结构并注入到 LLM 上下文，确保生成的代码符合项目现有风格。

### 2. 提交需求

在首页输入框用自然语言描述你要开发的功能，点击提交。

### 3. 流水线自动执行

调度中心（Orchestrator）使用 LLM 分析需求，自动拆解为步骤链并生成 DAG（有向无环图），按序派发给各 Agent：

| 步骤 | Agent | 职责 | 产出 |
|------|-------|------|------|
| 架构设计 | Architect | ER 图、分层架构、API 契约设计 | 架构文档 |
| 代码生成 | Coder | 生成 Java 源码，自动写入磁盘并 Git 提交 | 源码文件 |
| 代码评审 | Reviewer | P0/P1/P2 分级扫描，P0 自动触发修复重试 | 问题清单 |
| 测试 | Tester | 用例生成与编译验证 | 测试用例 |
| 部署 | Deployer | 环境要求、数据库初始化、打包、健康检查 | 部署方案 |

### 4. 查看结果

顶部标签页实时展示各 Agent 的处理结果：

- **调度中心** — 进度统计、DAG 时间线、历史任务列表
- **架构设计** — 架构文档
- **代码生成** — 源码（Monaco 编辑器）
- **评估评审** — P0/P1/P2 分级问题清单
- **测试中心** — 用例与测试结果
- **运维部署** — 部署方案与步骤
- **全局历史** — 过往任务的完整详情与开发日志时间线

---

## 人在回路

流水线执行过程中，你可以在任意 Agent 标签页底部的交互面板发送自然语言指令（如"使用 JDK 21 语法"、"给 Controller 加参数校验"），系统会将该指令注入到对应 Agent 的下一次 LLM 调用中。支持查看指令历史和取消未执行的指令。

也可以在历史任务中点击"重新执行"按钮，带上反馈意见让 Agent 重新生成。

---

## 执行日志

每个 Agent 的每次 LLM 调用都会被完整记录，可在交互面板中展开查看：

- **System Prompt** — 注入给 LLM 的系统指令
- **User Message** — 发送给 LLM 的用户消息（含项目上下文）
- **LLM Response** — LLM 的原始返回内容

日志同时存储在数据库和文件系统，方便调试和审计。开发日志（Pipeline Log）记录流水线完整过程：启动、步骤开始、LLM 调用、步骤完成、用户指令、错误、结束。

---

## 失败自动恢复

任一 Agent 执行失败时，系统会自动处理：

1. Git 回滚到该任务执行前的安全点
2. 等待 2 秒后自动重试（最多 2 次）
3. 重试耗尽后跳过该步骤继续执行，或中止整条流水线

评审-修复循环：Reviewer 发现 P0 问题时自动触发 Coder 修复 → 重新评审，最多循环 3 轮。

启动恢复：服务重启时自动扫描超过 5 分钟未完成的僵死任务，标记失败并触发重试。

可通过 `application.yml` 调整策略：

```yaml
sixagent:
  retry:
    max-agent-retries: 2       # Agent 最大重试次数
    max-rounds: 3              # 评审-修复最大循环轮数
    skip-on-exhausted: true    # 重试耗尽后跳过（false=中止）
```

---

## Git 版本控制

每个 Agent 完成后自动 `git commit`，保留完整的历史记录。前端支持查看 Git 提交历史、查看 diff，以及一键回滚到任意历史版本。

Agent 执行前自动记录 HEAD 作为安全点，失败时回滚到此点确保重试从干净状态开始。

---

## 技术架构

| 维度 | 选型 |
|------|------|
| 后端框架 | Spring Boot 3.2 + MyBatis-Plus |
| 数据库 | MySQL 8.0 + Flyway 自动迁移 |
| 前端框架 | Vue 3 + Element Plus + Pinia |
| 代码编辑器 | Monaco Editor |
| 图表渲染 | Mermaid.js（DAG/ER 图） |
| 桌面打包 | Electron + NSIS Windows 安装包 |
| AI 通信 | HTTP API（OpenAI 兼容格式） |
| 任务调度 | Spring @Async + 事件驱动 |

---

## 常见问题

**Q: 启动报 "Access denied for user"？**

检查 `llm-config.yml` 中的 `DB_PASS` 是否正确。数据库无需手动建库，`createDatabaseIfNotExist=true` 参数会自动创建。

**Q: Agent 执行超时？**

在顶栏 ⚙ 设置中调大对应 Agent 的超时时间（默认读取 180 秒）。

**Q: 如何更换 LLM 厂商？**

在 `llm-config.yml` 或界面 ⚙ 设置中修改 `provider` 字段（deepseek / qwen / openai），并填写对应的 `api-key` 和 `base-url`。支持任意 OpenAI 兼容接口。

**Q: 服务重启后未完成的任务会怎样？**

启动时自动扫描超过 5 分钟未完成的任务，标记失败并触发自动重试。

**Q: 安装后找不到配置模板？**

安装包自带 `llm-config.template.yml`，位于安装目录下（如 `C:\Program Files\SixAgent\`）。将其改名为 `llm-config.yml` 并填入配置即可。


## 📄 许可证

[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

本项目采用 **GNU Affero 通用公共许可证第 3 版（AGPL-3.0）** - 查看 [LICENSE](LICENSE) 文件了解详情。

AGPL-3.0 是强著佐权（copyleft）许可证：你可以自由使用、修改和分发本项目，但**分发修改后的版本（含以网络服务形式提供）时必须同样以 AGPL-3.0 开源完整源代码**（见许可证第 13 条"远程网络交互"）。

```
Copyright (C) 2026  aba666aba

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.
```