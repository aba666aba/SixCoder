# AGENTS.md —— 远端必读

> **任何远端 / AI 协作者进来后，先读这份文档再动手。**
>
> 它不是使用手册，是**实现地图 + 踩坑记录**。这里的每一条
> 要么是实测验证过的，要么是踩过坑之后写下来的。
>
> 如果你打算「新增一种能力」，先跳到第 5 节「改动前必做的盘点」。

---

## 0. 一句话说清这个项目在做什么

SixCoder 是一个**能自己写代码、也能自己证明写得对**的引擎。

它不是一个「让 LLM 写代码」的套壳。它最值钱的地方是**判据（judge）体系**：
每一份产出都要经过机械验收才能算完成 —— 不是模型说「我做完了」就算。

```
需求 → 澄清（问清技术栈/交互/边界）
     → 蓝图（拆成能力 cap，每条带探针 probe）
     → 生成（按栈的 LayerPlan 写骨架 + 远端写业务）
     → 闸门（gate：判据按栈分发，跑不过就阻断）
     → 审计（自认可依据：通过证据、变异杀伤、闸门失败数）
```

**核心设计哲学：宁可报错也不静默通过。** 后面的坑基本都是这条原则被违反导致的。

---

## 1. 目录地图（改了名，别再找 data/ecosystem）

```
prototype/
├── core/            引擎本体（166 个模块）
│   ├── stack.py           栈适配器注册表 ★
│   ├── layerplan.py       生成骨架计划 ★
│   ├── judges.py          判据注册表 / 分发 ★
│   ├── judges_cli.py      CLI 判据包
│   ├── judges_lang.py     多语言判据包
│   ├── judges_probe.py    通用探针判据包（7 种探针）★
│   ├── probe.py           探针执行器（7 种）★
│   ├── fullgen.py         全量生成（分层落点）
│   ├── auto.py            装配入口（write_web_entry 等）
│   └── clarify.py         远端提问（第一项强制技术栈）★
├── stacks/          ★ 栈插件（2026-11-24 从 data/ecosystem 改名）
│   ├── cli_tool/          manifest + adapter.py + templates/
│   ├── electron/          同上（8 个骨架文件）
│   ├── frontend_spa/      同上（6 个骨架文件）
│   ├── go_service/        同上（go.mod + main.go）
│   ├── microservice/      同上（celery_app.py + app/ 分层）
│   ├── node_express/      同上（server.js + routes/）
│   ├── py_lib/            同上（lib/__init__.py 懒加载导出）
│   └── spring_boot/       同上（pom.xml + Application.java）
├── checks/          232 个自检脚本（check_*.py）
├── tools/           辅助工具（gate_snapshot.py 等）
└── archive/         归档的旧代码（.removed_* 移进来）

tests/               92 个 pytest 用例
web/                 Vue 3 前端
docs/                文档
```

**为什么 stacks/ 要改名**：它原来在 `prototype/data/ecosystem/`。
`data/` 这个名字让人（和 AI）把它当数据目录跳过，实际上里面是
完整的插件体系。改名后一眼能看出这是栈包。
`core/ecosystem.py` 的 `ECO_ROOT` 有旧路径兼容回退。

---

## 2. 三个注册表（最容易搞混的地方）

系统里有**三套**按栈注册的东西。它们必须一致，否则会出现
「用户选了 A 栈，引擎按 B 栈生成」这类最隐蔽的错。

| 注册表 | 文件 | 管什么 | 当前内容 |
|---|---|---|---|
| `stack._ADAPTERS` | `core/stack.py` | 栈适配器（识别 + 探针约定） | 10 个（含 8 个生态包） |
| `layerplan` | `core/layerplan.py` | 生成骨架的分层与落点 | 5 个 |
| `judges` | `core/judges.py` | 验收判据按栈分发 | 26 条判据 |

### 2.1 当前注册情况

| 栈 | stack | layerplan | 判据(总/专属) | 骨架模板 | 探针 |
|---|---|---|---|---|---|
| `fastapi` | Y | Y（3 层） | 23 / **4** | 内置 | 内置 |
| `django` | Y | Y（3 层） | 23 / **4** | 内置 | 内置 |
| `cli_tool` | Y | Y（1 层） | 22 / **3** | **Y** | cli |
| `electron` | Y | Y（1 层） | 19 / 0 | **Y** | — |
| `frontend_spa` | Y | Y（1 层） | 19 / 0 | **Y** | browser |
| `go_service` | Y | **Y**（1 层） | 19 / 0 | **Y** | cli |
| `microservice` | Y | **Y**（1 层） | 19 / 0 | **Y** | sql |
| `node_express` | Y | **Y**（1 层） | 19 / 0 | **Y** | cli |
| `py_lib` | Y | **Y**（1 层） | 19 / 0 | **Y** | py_call |
| `spring_boot` | Y | **Y**（1 层） | 19 / 0 | **Y** | cli |

**「专属」= 只对这一个栈生效的判据。** 「总」= 加上 19 条通用判据
（`probe.*` 7 条 + `lang.*` 2 条 + anti_cheat/assertions/completeness 等 10 条）。

**这张表说明了两个事实：**

1. 生态包的验收**主要靠通用探针判据**（`probe:cli` 等），
   不是靠专属判据 —— 这正是 `judges_probe.py` 存在的意义。
2. `electron` / `frontend_spa` 也没有专属判据，
   它们的验收靠 `probe.browser`（真起 dev server + 浏览器断 DOM）。

### 2.2 名字别名（重要）

`cli` 和 `cli_tool` 是同一个东西，`frontend` 和 `frontend_spa` 同理。
**以生态包名为准**（真适配器、真探针都在那里）。
`auto._persist_stack_choice` 里有别名表：

```python
_ALIASES = {"cli": "cli_tool", "frontend": "frontend_spa"}
```

历史代码/测试里还写着短名，所以不能直接删。

---

## 3. 一次生成从头到尾发生了什么（实现细节）

### 3.1 澄清阶段：技术栈由用户选，不靠默认值

`core/clarify.py` 的 `_SYSTEM` 提示词强制要求：
> **第一项必须是技术栈（key 固定为 "stack"）**

选项范围从 `layerplan` 动态读（不是硬编码菜单）：

```python
def available_stacks() -> list[tuple[str, str]]:
    plans = _lp.default_registry().all()
    return [(p.stack, p.description) for p in plans]
```

**加一个 LayerPlan，可选范围自动变宽。**

用户选完后 `auto._persist_stack_choice()` 把它写进
`.sixcoder/stack.json` → `{"stack": "cli_tool"}`。

这一步以前是缺的：clarify 选完只存 design_doc.json，
**没写 stack.json**，于是后面全按默认 fastapi 猜。
用户选了 CLI，生成侧仍按 Web 三层走。

### 3.2 生成阶段：LayerPlan 决定分层与落点

```python
class LayerPlan(Protocol):
    stack: str                          # 对哪个栈生效
    layers: tuple                       # 层名序列（依赖顺序）
    def targets(ctx, layer) -> list[str]  # 这层的落点文件
```

`fullgen._layer_targets()` 的调用顺序：

```python
# 1. 先走可插拔计划
_plan = layerplan.plan_for(ws, _ad)
if _plan is not None:
    return _plan.targets(ctx, name)
# 2. 取不到才回退旧 if 分支（保留作对照物）
is_django = ...; is_frontend = ...
```

**内置计划**（`core/layerplan.py`）：

| 计划 | 分层 | 落点规则 |
|---|---|---|
| `fastapi` | models → services → api | models 固定 `app/models.py`+`app/schemas.py`；services/api 扫目录 |
| `django` | models(空) → services → (空) | models 跳过（防冲掉已有迁移），业务层写 `apps/**/*.py` |
| `cli_tool` / `electron` / `frontend_spa` | modules（单层） | 生态包通用计划，见下 |

**生态包通用计划**（`ecosystem_plan()`）的落点规则 ——
这是被三处测试来回夹出来的，改动前务必看清：

```
骨架文件分两类：
  A. 入口/装配文件（main.jsx / App.jsx / cli.py / main.py /
     vite.config.js / package.json / index.html …）
     → 按【文件名】判断，永远不让远端写
  B. 起步占位页（src/pages/Home.jsx）
     → 蓝图显式声明了就放行，否则不动它
```

**组件类栈忽略层名**：`module_file_fmt` 是 `.jsx/.tsx/.vue` 的栈，
问任何层名都返回组件文件（前端没有 models/services/api 这套分层）。
其他生态包只认 `modules` 层。

### 3.3 验收阶段：判据按栈分发

```python
class Judge(Protocol):
    key: str                            # 'cli.exit_code'
    applies_to: frozenset[str]          # {'cli_tool'} 或 {'*'}
    def check(ctx) -> GateResult
```

当前 26 条判据（19 通用 + 7 栈专属），按前缀：

```
probe.*        7   cli / py_call / pytest / http / browser / sql / electron  (ANY_STACK)
lang.*         2   typecheck / tests                                          (ANY_STACK)
api            1   HTTP 端点行为                                              (fastapi, django)
db_rows        1   数据库行数对得上                                            (fastapi, django)
field_map      1   字段映射正确                                                (fastapi, django)
security       1   越权/注入等安全检查                                          (fastapi, django)
cli.exit_code  1   程序能跑，退出码 0                                          (cli_tool)
cli.stdout     1   输出含约定文本                                              (cli_tool)
cli.artifact   1   产物文件存在且非空                                          (cli_tool)
其余 10 条      anti_cheat / assertions / completeness / cross_dupes /
               duplicates / import_ok / signature / stub / syntax / tests   (ANY_STACK)
```

**注意 `cli.*` 判据的 applies_to 同时含 `{"cli", "cli_tool"}`** ——
这是打通过程中统一命名的结果，只认一个会让另一半取不到判据。

### 3.4 通用探针判据（`judges_probe.py`，2026-11-24 新增）

**这是打通三个注册表的关键一环。**

`probe.py` 早就支持 7 种探针，但 judges 里一条都没有。
于是 `go_service` / `node_express` / `spring_boot`（声明 `probe:cli`）、
`microservice`（`probe:sql`）、`py_lib`（`probe:py_call`）这些包，
adapter 写好了、探针约定写好了，**验收能力却没接进判据体系**。

做法：判据不重写判断逻辑，**调用 `probe.run` 并翻译结果**。

```python
r = probe.run(ctx.ws, spec, timeout=180)
if not r.get("ok"):
    return GateResult("probe.cli", False, "probe", ...)
```

探针来源：`.sixcoder/probes.json`。

**没有这个文件 = 判红，不是判通过**（`evidence_ref = "no-probes"`）。

为什么不合成一条判据：分开能看出「哪类探针没过」——
cli 过了但 sql 没过，定位完全不同。

---

## 4. 铁律（违反这些会静默出错）

### 4.1 没配置 ≠ 通过

```python
# 正确
if not probes:
    return GateResult(..., False, "config", "no-probes", "没有 probes.json")

# 错误 —— 这是静默假绿
if not probes:
    return GateResult(..., True, ...)
```

同理：`props.py` 里未知判据类型必须 fail，不能 `else: pass`。

### 4.2 断言不能只看「旧名字不存在」

改名时踩过：

```python
# 危险：改名后旧字符串不存在了，这个断言**永远通过**
check("不被排除", "!data/ecosystem/**" not in flt)

# 正确：同时检查新旧两个路径
_bad = ("!data/ecosystem/**" in flt) or ("!stacks/**" in flt)
check("不被排除", not _bad)
```

### 4.3 跳过 ≠ 通过

跳过必须带原因（`evidence_ref` 说明为什么跳过），不能静默。

### 4.4 缺工具链 ≠ 通过

没有编译器/解释器时，判据返回 `ok=False, evidence_kind="toolchain"`，
不能因为「跑不了」就判绿。

### 4.5 坏模块不拖垮整体

Web 入口：单个 router import 失败就跳过。
CLI 入口：单个子命令坏了标 `(unavailable: 原因)`，**仍出现在 --help 里**，
不静默消失（否则用户以为没这个功能）。

### 4.6 入口文件永远不让远端写

`main.py` / `cli.py` / `App.jsx` / `vite.config.js` 这类装配文件由引擎生成，
远端改了会和骨架约定冲突（实测：远端改 cli.py 的分发，退出码约定就失守）。

---

## 5. 改动前必做的盘点（血泪教训）

### 5.1 先列 stacks/ 目录

**我在这上面连续犯过两次错**：先写了一套前端骨架模板，
跑通了 npm build，才发现 `stacks/frontend_spa/templates/` 早就有了；
隔一轮又给 CLI 写了一套，才发现 `stacks/cli_tool/` 也有。

做任何「新增一种 X」之前，先跑：

```bash
ls prototype/stacks/          # 8 个包，看有没有现成的
cat prototype/stacks/*/manifest.json   # 看 provides 声明了什么
```

然后回答三个问题：

```
我的需求 vs 已有包：
  □ 完全重复     → 直接用，别写
  □ 部分重叠     → 补缺口（例如给包加 templates/）
  □ 真空白       → 才写新包
```

### 5.2 三个注册表要一起改

只改一个会出现「计划按 A 选、判据按 B 注册」的错位。
实测：`CLIPlan` 用 `cli_tool`、`judges_cli` 用 `cli`，
**CLI 项目一条 cli.* 判据都跑不到**。

### 5.3 改完必须跑

**日常改动（快，1 分钟）**：

```bash
cd F:/WorkSpace/SixCoder
python -m pytest -q                    # 102 个用例，默认排除 slow
```

**改到架构/判据/生成（必跑）**：

```bash
cd prototype
python checks/check_ecosystem.py       # 生态包
python checks/check_stack.py           # 栈
python checks/check_fullgen.py         # 生成
python tools/gate_snapshot.py --verify # 7 个闸门快照 ← 最重要
```

**提交前全量（15-20 分钟）**：

```bash
pytest -m slow tests/test_all_checks.py -q   # 229 个自检逐个跑
```

2026-11-24 起，**229 个自检已全部纳入 pytest**（`tests/test_all_checks.py`
用 parametrize 逐个包装）。好处：

- 失败立刻可见（不再需要"记得手动跑 run_all.py"）
- 可以 `-k` 筛选：`pytest -m slow tests/test_all_checks.py -k ecosystem`
- 可以 `--lf` 只跑上次失败的
- 每个脚本独立用例，一个崩了不影响其他

**`gate_snapshot --verify` 是最重要的**：它比对 7 个闸门的行为
与基线是否一致。改了判据/生成逻辑，这个必须过。

---

## 6. 已知问题（截至 2026-11-24）

### 6.1 自检不全是绿的

全部 229 个自检，**用正确的 UTF-8 环境跑**：

```bash
$env:PYTHONIOENCODING="utf-8"; $env:PYTHONUTF8="1"
```

**不用 UTF-8 环境会有 7 个假失败** —— 它们是 `UnicodeEncodeError: 'gbk'`，
脚本本身是好的（`Get-ChildItem` 循环里跑就崩，单独跑就过）。

**2026-11-24 全部修完**，之前记录的 15 个失败逐条处理：

| 类别 | 数量 | 处理 |
|---|---|---|
| UTF-8 假失败（`UnicodeEncodeError: 'gbk'`） | 7 | 环境问题，设 `PYTHONIOENCODING` 即可，脚本本身没问题 |
| 新增文件未登记 | 2 | 补 `开发文档.md` 架构地图（6 个 core 文件）+ 修 `check_mechanism_wiring` 的回调识别 |
| `check_invariants` 误判 | 1 | 把提示词文字（如 `"不许把未实现当完成"`）当成"禁令"，已修检测器 + 补 2 条真实依据 |
| `check_remote_http` 真 bug | 1 | `clear_key` 只清平铺字段不清厂商卡片，与 UI 承诺相反，已修 |
| `check_devdoc` 计数 | 1 | 227 → 229 |
| 其余 | 3 | 详见下 |

**跑全量自检的注意事项**：

```bash
# 有些 check 需要后端在跑（check_remote_http 等）
python -m uvicorn server:app --host 127.0.0.1 --port 8798 &
$env:SIXCODER_BASE_URL = "http://127.0.0.1:8798"
```

全量跑一遍大约 15-20 分钟，**不要用在同步工具调用里等它**——
放后台跑（`run_in_background`），或者只跑相关的子集。

### 6.2 生态包骨架已全部补齐（2026-11-24）

8 个包现在都有 `templates/`：

| 包 | 骨架文件 | 验证深度 |
|---|---|---|
| `cli_tool` | cli.py + commands/ | **真跑**：--help/子命令/退出码/坏模块 |
| `electron` | 8 个（main.cjs + preload + React 渲染进程） | 结构 |
| `frontend_spa` | 6 个（vite + React） | **真构建**：npm install + vite build + 浏览器渲染 |
| `microservice` | 7 个（celery_app.py + app/{api,services,tasks}） | **py_compile 全过** |
| `node_express` | 3 个（server.js + routes/ + package.json） | **node --check 通过** |
| `py_lib` | lib/__init__.py（懒加载导出） | **真 import**：空库可导入、加模块能取符号 |
| `spring_boot` | 3 个（pom.xml + Application.java + properties） | **mvn validate exit=0** |
| `go_service` | 2 个（go.mod + main.go） | **仅文本检查**（本机无 go） |

**唯一没有真跑验证的是 go_service**——本机没装 Go 工具链。
它的骨架是文本层面检查的（package main / func main / net/http）。
**如果你有 Go 环境，应该跑一次 `go build ./...` 确认。**

测试：`tests/test_ecosystem_skeletons.py`（9 条）钉住
"每个包有模板 + 复制后能被 detect 认出 + 能跑"。

### 6.3 仓库体积（2026-11-24 已清理）

```
清理前 prototype/: 199 MB
清理后 prototype/:  80 MB   实测
```

删掉的（**全部未纳入 git 跟踪**，纯本地产物）：

| 目录 | 大小 | 内容 |
|---|---|---|
| `_sandbox/` | 57 MB | 判据标定用的对照实现 |
| `_logs/spa_verify/` | 44.5 MB | 前端验证的 node_modules 副本 |
| `_logs/del_* 等` | ~1 MB | 调试遗留（runall_*.txt、截图）|
| `data/scratch/` | 16.4 MB | 临时工作区 |

**代码会自动重建这些目录**（`paths.py` 的 `logs_dir()` 等按需创建），
所以删了不影响运行。`.gitignore` 早就覆盖它们了。

剩余 80 MB 里，`web/node_modules` 占 36.7 MB（本地依赖，正常）。
真正入仓库的代码约 40 MB。

---

## 7. 各模块的关键实现点（速查）

### `core/stack.py`

- `_ADAPTERS` 是栈适配器字典；启动时 `_register_ecosystem_adapters()` 扫 `stacks/`
- **安全边界**：加载失败只跳过该包、同名不覆盖内置、没有目录时零影响
- `detect(ws)` 是**纯名称判定**，无适配器回退
- `from_workspace(ws).name` **会回退到 fastapi** —— 需要真实栈名时用 `detect()`

### `core/judges.py`

- `project_stack(ws)`：优先 `stack.json` → `cli.json` → **`stack.detect()`**
- 必须用 `detect()` 而不是 `from_workspace().name`（后者回退会抹掉 frontend_spa）
- `full_registry()` 必须缓存（重复注册同一个 key 会报错）

### `core/layerplan.py`

- `plan_for(ws, adapter)` 按适配器名查计划；取不到返回 `None`（调用方回退）
- `ecosystem_plan()` 给有 `templates/` 的包自动生成通用计划
- 组件类栈按 `module_file_fmt` 后缀判断，忽略层名

### `core/auto.py`

- `write_web_entry(ws, bp)`：生成 ASGI 入口，include 各模块 router
- `_persist_stack_choice(ws, doc)`：澄清结果 → `stack.json`，含别名表

### `core/fullgen.py`

- `_layer_targets(ws, bp, layer, fixed)`：先走 LayerPlan，再回退旧 if 分支
- 旧分支**保留作对照物**，删之前必须证明新路径完全等价

---

## 8. Windows 环境注意事项

- 中文输出：子进程必须设 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`，
  否则中文会乱码或空（实测踩过）
- `.cmd` 包装器（mvn/npm）：用 `shutil.which()` 解析，直接调会 `WinError 2`
- 行尾：用 Python 改文件时注意 `splitlines(keepends=True)`，
  否则会把多行合并成一行（实测踩过，导致函数被截断）

---

## 9. 提交规范

- 每个提交说明**为什么**，不只说明改了什么
- 实测过的行为要写进提交信息（带具体输出）
- 没验证的东西要明确说「未验证」
- 发现既有失败要标注「与本次无关，基线就失败」

---

*最后更新：2026-11-24。改动架构相关部分时请同步更新本文档。*
