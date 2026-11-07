# 27 · Web 项目的验收与交付机制

> **从属声明**：本文是 `开发文档.md` §18（判定层）在 **Web 目标**下的展开说明。
> 目标/流程/判据以 `开发文档.md` 为准；本文只记"为 Web 项目补了什么、为什么、证据在哪"。
> 所有条目都能用 `prototype/checks/` 或文末的复现命令当场验证。

更新时间：2026-11-07（首次真实夜班把 9 条 FastAPI 能力跑到 6 条通过后整理）

---

## 一、问题：验收词汇里没有 Web

旧判定层只有三种探针：`cli` / `py_call` / `pytest`。
目标写的是"FastAPI + 前端 + JWT + SQLite"时，蓝图只能把每条能力规划成
`python main.py register` 这种 **CLI 探针**——从根上就观测不到真实产物。

后果不是"慢"，而是**判不了**：路由没挂、鉴权没做、返回硬编码，全都可能被算成"完成"。
第一次跑这类目标时系统报 9/9（100%），真起 uvicorn 复核后发现：

```
POST /auth/login            200  {"username":"smoke"}     ← 没有 token
POST /tickets （无 token）   201  照样建单                  ← 没有鉴权
GET  /tickets （垃圾 token） 200  [{"title":"Test Ticket"}] ← 硬编码假数据
```

所以这一层要补的不是"更严的判据"，而是**一套能表达 Web 行为的验收词汇**，
以及**把确定性接线从模型手里收回引擎**。

---

## 二、判定层：`http` 探针

`probe.py` 新增 `http` 执行器（`_run_http` / `_HTTP_RUNNER`），在子进程里用
`TestClient` 载入 ASGI 应用、真发请求。选 TestClient 而不是真起 uvicorn 的理由：
端口竞争、启动竞态、僵尸进程都会变成"假红"，而 TestClient 走的是**同一条**
路由/参数校验/序列化链，却可重复。

### 2.1 多步请求链 + 变量传递

```json
{"kind":"http","app":"app.main:app","steps":[
 {"method":"POST","path":"/auth/register","json":{"username":"u","password":"p"},
  "expect_status":[200,201,409]},
 {"method":"POST","path":"/auth/login","json":{"username":"u","password":"p"},
  "save":{"token":["token","access_token"]}},
 {"method":"POST","path":"/tickets","json":{"title":"T1"},
  "headers":{"Authorization":"Bearer ${token}"},"expect_status":201}]}
```

- `save` 支持**候选字段名**（`id`/`ticket_id`/`pk`），产物命名不统一时不会拿到 `None`；
- `${var}` 在路径 / 请求体 / 头里都会被替换；
- JWT 项目天生需要"注册 → 登录 → 带 token 访问"，链必须**自包含**。

### 2.2 跨进程持久化（`split`）

`"split": true` 时，链的前段在进程 A 跑、最后一步在**进程 B** 跑（变量经文件传递）。
同进程的内存字典能骗过普通探针，骗不过这一条。

### 2.3 并发（`parallel` + `expect_count`）

单步写 `"parallel": N`，runner 用 `httpx.ASGITransport` + `asyncio.gather` 并发发 N 次。
sync 端点会被 FastAPI 丢进线程池**真并行**，与 uvicorn 下一致，但不开端口。

配合 `expect_count` / `expect_count_min`（识别 `list` / `total` / `count` / `items` 多种形状）
判"并发写有没有全部落库"。

标定（`_sandbox/` 里的三份对照实现）：

| 实现 | 结果 |
|---|---|
| sqlite 每次请求新连接 + 锁 | 8 条全在，**通过** |
| JSON 文件读-改-写无锁 | **红：至少该有 8 条，实际只有 1** |
| 记录耗时 | 8 次请求 0.13 s（串行 ≈0.4 s）→ 确认真并发 |

### 2.4 幂等

`cap.api.idempotent`：同一份注册请求连发两次，第二次必须 4xx，且响应里不许出现
`Traceback` / `IntegrityError` / `UNIQUE constraint`。
真实产物实测：第二次 → **500 且泄露 `UNIQUE constraint`**，精确报红。

### 2.5 不在探针里造"假失败"

- 负向用例（`expect_status` 全为 4xx）只负责判，**不定义路由**——否则它会用自己的
  请求体和错误状态码覆盖正向路由（实测把 `POST /tickets` 冲成 200）。
- 每条探针跑在**独立工作目录**，产物按相对路径落库（sqlite）时不会互相污染。
  这条修之前，上一片留下的 `users.db` 让"登录"把 `id` 当用户名 → 作者被冤枉降级。
- `GET /`（前端）例外：要在仓库根跑，才能读到静态文件。

---

## 三、蓝图层：把"不可能通过的探针"机械修好

模型写出的 http 探针**看着合理，却永远过不了**：

- `/auth/login` 期望 `u1` 存在，但链里没有注册；
- 头里写 `Bearer ${token}`，但从没有步骤 `save` 过 token；
- 路径写 `/tickets/{ticket_id}`，字面占位符，没有创建它的前置步骤。

`blueprint.normalize_http` 做确定性修补：补注册/登录前置、把占位符换成 `save` 出来的
变量、读接口补一条种子数据（否则表都不存在）。修补后的链必须**自包含**。

`blueprint.fill_http_modules` 用**末步路径**（真正的能力端点）分配落点模块，
而不是第一步（往往都是 `/auth/register`）。

---

## 四、实现层：接线归引擎，作者只写函数

| 机制 | 不这么做会怎样（实测） |
|---|---|
| `write_web_entry` 生成 `app/main.py`（自动 include 各模块 `router`） | 每片重写入口，上一片路由被抹 |
| `apply_web_wiring` + `.sixcoder/web_routes.json` 注册表，整块重渲染 | 重复注册、`/tickets/{id}` 遮蔽 `/tickets/search`（405）、整文件重写后路由丢失 |
| 路由顺序"静态优先、长路径优先" | FastAPI 按注册顺序匹配，动态段吃掉静态段 |
| handler 用 **Pydantic 模型**接参（按请求体样本生成） | 裸 `dict` 接参时 FastAPI 不做校验，非法输入 500 而不是 422 |
| 参数可满足性校验（路径变量/请求体字段/字面 id/普通标量） | 把 `POST /tickets` 接到 `create_connection(db_file)` 上 → `TypeError` |
| `_strip_author_routes`：摘掉作者自挂的、引擎已接管的端点 | 作者写 `@router.post("/tickets", response_model=Dict[str,str])` → 重复注册 + `id` 被当 str 校验 → 500 |
| `_ensure_imports`：补"用了但没导入"的名字 | `raise HTTPException(...)` 没 import → 请求时 `NameError`，整模块全红 |
| 渲染前清 `_engine_*` / `_Body_*` 残留 | 作者把引擎 handler 抄进自己的代码（还丢了 import）→ 导入失败 |
| 函数被删时同步摘掉注册表条目 | 引擎段留着 `return create_ticket(...)`，但函数已不存在 → `NameError` |

作者侧另加两条"少写就少错"的机制（`author.py`）：

- **函数级增量** `_fragments_only`：围栏里只含**新名字**的函数时，合并进现有文件，
  旧函数一个不动（同名才走整份，且带"删函数就打回"的保全校验）。
- **补丁模式** `_parse_patches` / `_apply_patches`：`{find, replace}` 两级匹配
  （逐字 → 去缩进逐行），匹配不上整份作废，不猜着改。

---

## 五、交付层：`shipgate.py` + `deps.py`

"能力全绿"不等于"能上线"。`shipgate.ship_check` 单独回答交付问题，并**参与 `ok` 判定**：

| 检查 | 判什么 |
|---|---|
| 应用可导入 | `from app.main import app` 成功 |
| `/docs` 200 | OpenAPI 能出（目标里明确要求） |
| **uvicorn 真启动** | 真起进程轮询 `/docs`，不只 TestClient |
| README / requirements / .gitignore | 发布件齐全；`.env` 不进版本库 |
| pytest 通过 | 测试**由引擎从蓝图反生成**（`tests/test_api.py`），作者不能自己判自己 |

`deps.ensure` 做依赖自举：扫产物的 `import` → 补装缺失包（`jwt→PyJWT` 这类名字映射）
→ 写 `requirements.txt`；检测到 `FastAPI(` 自动带 `uvicorn`。
不这么做的话，作者写了 `import jwt` 而环境没装 → 整个 app 导入失败 →
**9 条能力一起红，被记成"模型做不出来"**（实测：覆盖从 6/9 掉到 0/8）。

`shipgate.ensure_env` 扫 `os.environ[...]` 生成 `.env` / `.env.example`，
探针 / 测试 / uvicorn / 体检共用同一份（否则 `KeyError: JWT_SECRET_KEY` 也是"全红"）。

---

## 六、机制能力注入

`shipgate.inject_caps` 在规划前按蓝图事实补"上线必过"的能力（不消耗远端能力）：

| 能力 | 判据 |
|---|---|
| `cap.security.auth_required` | 不带凭证访问受保护接口必须 401/403 |
| `cap.security.no_password_leak` | 注册/登录响应里不得出现 `password` |
| `cap.ui.index` | `GET /` 返回可渲染 HTML（不是 404、不是纯 JSON） |
| `cap.ui.calls_api` | 页面里真的调接口（出现 `fetch(`/`XMLHttpRequest`/接口路径） |
| `cap.data.persist_*` | 换进程回读仍在（`split`） |
| `cap.api.validation` | 非法请求体必须 4xx（配合引擎生成的 Pydantic 模型，天然 422） |
| `cap.api.not_found` | 不存在的资源 404（只在真有 `GET /{id}` 时注入，否则 405 是探针自造） |
| `cap.api.idempotent` | 重复注册必须 4xx 且不抛栈 |
| `cap.data.concurrent` | 并发 N 次写全部成功且全部可见 |

并发/幂等这两条用 `deps` 做**级联隔离**：登录没通时它们停在 `blocked`，
不会把"登录没做"误算成"并发不行"。

---

## 七、实测口径（诚实版）

- 蓝图：9 条业务能力 + 8 条机制能力 = **17 条**
- 某轮真实夜班：**6/17** 通过（注册/登录/建单/列表/搜索/看板 都是真实 HTTP 验收）
- 剩余红的能力，归因全部在**模型侧**（登录不发 token、search/board 硬编码、无鉴权、无首页）
- 花费 ¥0（远端 glm-4-flash 免费档 + 本地作者）

**必须说清楚的两件事**：

1. **离 XL 还很远**。当前产物是"9 个 REST 端点的后端骨架"（约 220 行），目标要 ≥10000 行 +
   前端页面。规模扩产（`blueprint.extend` + `_target_lines`）已接进夜班，但还没跑到那个量级。
2. **远端能力是硬约束**。glm-4-flash 在"一次写对 4 个带 DB/鉴权的函数"上成功率很低；
   机制能做的是"不冤枉它、不浪费轮次、给出精确证据"，不能替它写对。

---

## 八、复现命令

```powershell
cd SixCoder\prototype

# 单条能力：真跑一次 http 探针
python -c "from core import blueprint, probe; b=blueprint.load(r'D:\proj'); \
print(probe.run(r'D:\proj', b.by_id('cap.ticket.create').probe))"

# 全量：17 条能力的真实状态
python -c "from core import blueprint, probe; from pathlib import Path; \
b=blueprint.load(Path(r'D:\proj')); \
print(sum(probe.run(r'D:\proj', c.probe).get('ok',0) for c in b.caps if c.probe.get('kind')=='http'))"

# 交付体检（导入 / /docs / uvicorn / README / pytest）
python -c "from core import blueprint, shipgate; from pathlib import Path; \
print(shipgate.ship_check(Path(r'D:\proj'), blueprint.load(Path(r'D:\proj'))))"
```

对照实现（证明判据不是假绿）放在 `_sandbox/`：

- `race/` 正确实现（sqlite 每请求连接 + 锁）→ 并发/幂等全过
- `racelost/` JSON 文件读-改-写无锁 → 并发判红
- `racebad/` 重复注册不处理 → 幂等判红
