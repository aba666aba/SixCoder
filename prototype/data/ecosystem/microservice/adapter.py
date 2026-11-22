"""多服务/中间件系统生态包：API + worker + 队列 + 数据库。

能力边界（诚实声明）：
  · **识别**：有 celery 配置（`celery_app.py` / `app/celery.py`）→ 多服务系统；
    **刻意不用 `docker-compose.yml` 作 marker**（Django/FastAPI 部署也常有，
    会误判）。
  · **验收（当前支持）**：
    - API：http 探针（ASGI TestClient）
    - 数据层：**sql 探针**（直连产物仓 sqlite，查真实落库，不是接口返回的假数据）
    - worker 任务逻辑：py_call / pytest（不连真 MQ，测任务函数本身）
  · **验收（未支持）**：真实 Redis/RabbitMQ/Kafka 集成、多服务联调 —— 需要真服务
    （Docker），本机不可假定有；**不假装**能验。
"""
from core import stack


class MicroserviceAdapter(stack.StackAdapter):
    name = "microservice"
    label = "多服务/中间件系统（API + worker + 队列 + 数据库）"
    entry_path = "app/main.py"
    entry_import_hint = "from fastapi import FastAPI"
    route_hint = "API 用 FastAPI 的 APIRouter；worker 任务定义在 app/tasks.py"
    router_decl = "router = APIRouter()"
    router_marker = "APIRouter("
    module_dirs = ("app/api", "app/services", "app/tasks", "app")
    module_file_fmt = "app/services/{seg}.py"
    module_globs = ("app/**/*.py",)
    module_prefix = "app."
    route_decorator_re = r"@router\.(\w+)\(\s*[\"']([^\"']+)[\"']"
    route_verb_default = "GET"
    route_hosts = ("router", "app")
    template_dirs = ("app/templates", "templates")
    static_dirs = ("app/static", "static")
    static_mount = "/static"
    start_cmd = "uvicorn app.main:app"
    app_ref = "app.main:app"
    probe_app_candidates = ("main:app", "app.main:app", "app:app")
    deps = ("fastapi", "uvicorn", "sqlalchemy", "redis", "celery")
    app_marker = "FastAPI("

    def asgi_app(self) -> bool:
        """API 服务仍是标准 ASGI（http 探针用 TestClient）。"""
        return True

    def web_rules(self) -> str:
        """多服务系统的探针约定（2026-11-19）。

        重点：**接口返回 ≠ 数据落库**。必须用 sql 探针直接查库，否则假实现
        （接口返回硬编码数据）也能过 http 判据。
        """
        return """1. **分层验收，别只判接口**：
   · API 行为 → http 探针（`app.main:app`，steps 链）；
   · **数据真落库** → `sql` 探针（直连 sqlite 查表，见下）；
   · worker 任务逻辑 → py_call（调任务函数本身）或 pytest；
   三者各管一层，"接口返回 200"不能推出"数据写进去了"。
2. `sql` 探针写法（db 是产物仓里的 sqlite 文件，如 `app.db`）：
   {"kind":"sql","db":"app.db","steps":[
     {"sql":"SELECT COUNT(*) AS n FROM users","expect_first":{"n":1}},
     {"sql":"SELECT name FROM users","expect_contains":{"name":"张三"}}]}
   判据至少给 expect_rows / expect_first / expect_contains 之一。
   **禁** DROP/ATTACH/load_extension（会破坏库/逃逸）。
3. worker 任务**不要依赖真实 MQ**：把任务函数写成可独立调用（`run_job(payload)`），
   用 py_call 直接调；不要写"往 Redis 推一条再等消费"这种需要真服务的判据。
4. 落点：API 在 `app/api/`，业务在 `app/services/`，任务在 `app/tasks.py`，
   **禁止**再建 `app/main.py` 之外的第二套服务入口（引擎按单一 ASGI 应用验）。
5. 真实多服务联调（Docker 起 Redis/Postgres）**当前不验**，不要写这类判据。"""