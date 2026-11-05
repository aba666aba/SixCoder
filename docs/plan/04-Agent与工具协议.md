> ⚠️ **计划基线**（写于实现之前，保留作决策记录）。路径已按清理后的结构改写：
> `coder-core/` → `prototype/`｜`sixagent-web/` → `shell/`｜`sixagent-server/` **已删除**
> （设计契约抽到 `../15-多智能体架构映射.md`）｜`reference/` → `docs/legacy/`。
> 文中 `src/*` 包结构与 `python -m src.*` 命令**未采用**：实际入口是 `prototype/server.py`、
> `prototype/cli.py`。当前实况以 `../STRUCTURE.md` 与 `../15`–`../18` 为准。

# 04 Agent 与工具协议

> 本章是"3B 模型能用"的核心：**把模型的自由度压到它能可靠完成的范围内**。
> 三个约束：工具只做原子事、编辑只给补丁、上下文有硬预算。

---

## 1. 工具契约（继承 `src/tools/base.py`，不改接口）

```python
class BaseTool(ABC):
    name: str                      # 唯一名，模型只认这个
    description: str               # 一句话，进 Prompt
    parameters: dict               # JSON Schema 子集，用于渲染 + 调用前校验
    aliases: dict                  # 参数别名容错（小模型高频问题）
    def run(self, **kwargs) -> ToolResult: ...
```

铁律（`safe_run` 已实现，新增工具必须遵守）：

| 铁律 | 原因 |
|---|---|
| 失败返回 `ToolResult(ok=False, error=...)`，**永不抛异常** | 失败要变成一次"可读的 Observation"，让模型自己换路子；抛异常会中断整个 ReAct 循环 |
| 错误信息必须**可据以修正**（"文件不存在：`src/foo.py`，当前工作区为 `workspaces/t-01/`"） | 3B 看不懂堆栈，看得懂人话 |
| 返回内容截断（`MAX_OBSERVATION_CHARS = 1500`） | 一次工具输出吃掉整个上下文是最常见的死法 |
| `metadata` 必须是可 JSON 序列化的普通数据 | 前端轨迹树与指标层直接消费 |

---

## 2. 工具清单（最终态）

| 工具 | 关键参数 | 返回 | 硬限制 |
|---|---|---|---|
| `code_search` | `query, top_k=5, path_glob?` | 命中块 + `file:line` + 分数分路（rrf_vector/rrf_bm25） | 总字符 ≤ 1500 |
| `file_outline` | `path` | 类/函数/方法签名 + 行号 | 大文件只给前 200 个符号 |
| `read_file` | `path, start?, end?` | 带行号文本 | 单次 ≤ 400 行；路径必须在工作区内；二进制拒读 |
| `edit_file` | `path, search, replace` | 应用结果 + diff 摘要 | SEARCH 必须**唯一命中**；干跑失败即拒绝 |
| `bash` | `command, cwd?, timeout?` | stdout/stderr 尾部 + 退出码 | 命令白名单 + 工作目录约束 + 超时 ≤ 180 s |
| `git_tool` | `op(status/diff/commit/checkout), args` | 结果摘要 | `op` 枚举固定；**无 reset**；commit 前必须 `status` 干净 |
| `diff_view` | `a, b` | 新增/删除行分组 | 输出 ≤ 120 行 |
| `summarize` | `text, max_tokens` | 压缩文本 | 用于长日志/长文件 |
| `calculator` | `expression` | 数值 | `ast` 白名单求值，禁任意代码 |
| `current_time` | — | 时间 | — |
| `web_search` | `query` | 结果 | 离线默认禁用 |

**为什么没有 `write_file`（整文件写）**：见 §3。

### 2.1 `edit_file` 的实现要求（最容易翻车的地方）

```python
def run(self, path, search, replace):
    text = read_workspace_file(path)              # 越界拦截 + 大小上限
    hits = [m.start() for m in re.finditer(re.escape(search), text)]
    if len(hits) == 0:
        return ToolResult(ok=False, error=f"SEARCH 片段未命中。文件 {path} 现有内容前 40 行：\n{head}")
    if len(hits) > 1:
        return ToolResult(ok=False, error=f"SEARCH 片段命中 {len(hits)} 处，必须唯一。请包含更多上下文行。")
    new = text[:hits[0]] + replace + text[hits[0] + len(search):]
    dry = syntax_check(path, new)                # 干跑：语法不过就不落盘
    if not dry.ok:
        return ToolResult(ok=False, error=f"应用后语法错误：{dry.error}")
    backup(path)                                 # 备份，供节点级回滚
    write_workspace_file(path, new)
    return ToolResult(ok=True, content=diff_summary(text, new), metadata={"lines_added": ...})
```

三个细节决定成败：

1. **未命中时回传文件头部内容**——3B 第二枪几乎必中，因为它看到了真实文本。
2. **多处命中拒绝而不是"取第一个"**——小模型常在文件里出现两次的 `}` 附近乱改。
3. **落盘前干跑语法**——把最廉价的一道闸门前移到工具内部，省一整轮 ReAct。

---

## 3. 编辑协议：SEARCH/REPLACE，不用整文件重写

| 方案 | 3B 可靠性 | 可审查 | 可回滚 | 结论 |
|---|---|---|---|---|
| 整文件重写 | 低（300 行必丢 import / 改掉不该改的分支） | 差（看不出意图） | 难 | ❌ |
| 统一 diff | 中（行号错就全崩） | 好 | 好 | ❌ |
| **SEARCH/REPLACE 块** | **高**（只需复现 5–10 行唯一锚点） | 好 | 好 | ✅ |

模型输出格式（严格）：

```
<<<<<<< SEARCH
    def _build(self):
        self._parts = []
=======
    def _build(self):
        self._parts = []
        self._frozen = False
>>>>>>> REPLACE
```

解析器要求（`src/codegen/patch.py`）：

- 支持一个响应内**多个块**（按文件分组，每文件 ≤ 3 块）
- 任一块失败 → 整响应作废，把**具体哪块、为什么**回传（不落盘）
- 缩进不匹配时先尝试"去公共缩进后重新对齐"，仍失败才拒绝

### 3.1 FIM（填空）用于插入型小改动

qwen2.5-coder 原生支持 FIM 特殊 token。场景：在已有函数中间插入一个校验、补一个 `if` 分支、补一个测试用例。

```
<|fim_prefix|>{前文 20 行}<|fim_suffix|>{后文 10 行}<|fim_middle|>
```

比 instruct 稳，且输出更短（通常 5–30 行），在 3B 上延迟明显更低。`src/codegen/fim.py` 负责构造；路由规则决定何时用 FIM（见 §5）。

---

## 4. 上下文预算（4GB 卡上的生死线）

**每步上下文的硬上限：3,500 token**，超出即裁剪。固定四件套：

| 组成 | 预算 | 内容 | 来源 |
|---|---|---|---|
| ① 任务契约 | ≤ 400 | 要什么签名、要过哪些测试（红） | 任务图节点 |
| ② 目标代码 | ≤ 1,200 | 目标符号 outline + 目标函数±30 行 | `file_outline` + `read_file` |
| ③ 1 跳邻居 | ≤ 800 | 调用者/被调用者的**签名**（非实现） | 符号索引 |
| ④ 项目规约 | ≤ 400 | `CONVENTIONS.md` 摘要（包结构/命名/异常/日志/事务） | 一次生成后滚动更新 |
| ⑤ 历史反馈 | ≤ 700 | 上一轮失败的**窄反馈**（一条错误） | 闸门 |

规则：

- **绝不整包注入**。SixAgent 的 `ProjectContextService` 把 `yml/properties/json/sql` 全文塞 Prompt —— 在 4GB 卡上等于把 prefill 送进 CPU，一步要 1–2 分钟。
- **只给签名不给实现**，除非该实现就是本步要改的目标。
- 检索用来**找位置**，符号索引用来**保一致性**，两者不互相替代。
- `CONVENTIONS.md` 在首次索引仓库时用 7B 生成一次（此时值得花 2 分钟），之后增量维护。

---

## 5. 路由与短路（省 Token 也省时间）

继承 `src/agent/router.py` 的规则表 + `routing_shortcut_confidence=0.8` 短路机制，规则表换成代码意图：

| 意图特征 | 直接动作 | 是否进模型 |
|---|---|---|
| "第几行/在哪定义/谁调用了 X" | `code_search` + `file_outline` 结果直接作答 | ❌ 0 token |
| "这个文件多大/有哪些函数" | `file_outline` | ❌ |
| "时间/算术" | `current_time` / `calculator` | ❌ |
| "把 X 改成 Y"（局部、单文件） | **FIM** | ✅ 但短 |
| "加一个接口/字段，贯穿多层" | 任务图多节点 + SEARCH/REPLACE | ✅ |
| "为什么要这样设计/怎么修这个架构问题" | 升级到 7B（plan-first） | ✅ 大模型 |

短路机制在 3B 上的价值被低估：一句"`handle_input` 在哪定义"如果走完整 ReAct，要 2–3 轮共 ~1,800 token、约 40 s；短路后 0.3 s。

---

## 6. 三套 Prompt（`src/generation/prompts/templates.py`）

### 6.1 codegen（主力，要求极短的输出）

```
你是代码补丁生成器。只输出 SEARCH/REPLACE 块，不解释。

规则：
1. SEARCH 必须是目标文件中【已存在】的原文，逐字符一致，含缩进，通常 5-10 行。
2. SEARCH 必须在文件中唯一出现；若可能重复，请把范围扩大到包含函数签名。
3. 不要输出完整文件。不要输出解释、不要 Markdown 代码围栏。
4. 不得改动与任务无关的行。
5. 只使用上下文中已导入的符号；需要新导入时，用单独的块在文件头部插入。

【任务契约】
{contract}

【目标文件大纲】
{outline}

【当前代码】
{code_with_line_numbers}

【规约】
{conventions}
```

### 6.2 repair（拿到窄反馈后修复）

```
上一步补丁未通过验证。只修这一个问题，不要顺手改别的。

【失败原因】
{one_error_with_file_line}

【相关代码】
{excerpt}

【上次的补丁】
{last_patch}

输出一个 SEARCH/REPLACE 块修复它。
```

### 6.3 plan（仅在升级到 7B 时）

要求输出**严格 JSON**（用 Ollama `format: json` + 我们自己的 schema 校验）：

```json
{
  "steps": [
    {"kind": "edit|create|test|config", "path": "src/x.py",
     "contract": "给 Foo.bar 增加 timeout 参数，默认 30", "why": "..."}
  ],
  "risks": ["..."],
  "need_human": false
}
```

---

## 7. 采样与输出约束（统一在 `src/api/queue.py` 里强制）

| 参数 | 值 | 理由 |
|---|---|---|
| `temperature` | 0.15（codegen）/ 0.3（plan） | 代码要确定性；方案要一点多样性 |
| `top_p` | 0.9 | — |
| `num_predict` | ≤ 1200（codegen）/ ≤ 800（FIM） | 单步就该短；超长输出必然发散 |
| `num_ctx` | 8192（3B 常用 4096） | 4GB 卡留余量给 KV |
| `stop` | `>>>>>>> REPLACE`、```` ``` ```` | 防止模型自问自答续写 |
| `format` | `json`（仅 plan 与规格化步骤） | 结构化输出比"求你别写解释"靠谱 |
| best-of-N | N=3（3B），全绿即停 | 见 05 |

---

## 8. 反模式清单（写代码时贴在显示器上）

1. ❌ 让模型输出完整文件 → 静默丢逻辑
2. ❌ 把编译日志整段喂回 → 上下文溢出且无信息
3. ❌ 让模型自己判断"改好了没" → 幻觉式完成
4. ❌ 一步改 3 个文件 → 无法定位失败
5. ❌ 单步上下文 > 4K → prefill 走 CPU，一步 2 分钟
6. ❌ 失败后原样重试同一提示词 → 必然再失败；必须升级阶梯
7. ❌ 在用户工作区直接改 → 必须独立 workspace + git 快照
