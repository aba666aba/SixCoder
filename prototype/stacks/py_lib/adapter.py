"""纯 Python 库生态包（无 Web / 无 CLI 入口）。

能力边界（诚实声明）：
  · **识别**：有 `lib/__init__.py` → Python 库；
    marker 只用这一个独特文件（不与 cli_tool 共用 pyproject.toml 抢判），与 cli_tool(cli.py)、内置 django(manage.py) /
    fastapi(app/main.py)、spa(package.json)、electron(main.cjs)、microservice(celery)
    都不重叠。
  · **验收**：`py_call` 探针直接 import 模块、调用函数、断言返回值或异常类型；
    判据至少给 `expect` 或 `expect_raises` 之一，不许空判。
  · **未支持**：性能基准、并发/线程安全、依赖外部服务的库 —— 不假装能验。
"""
from core import stack


class PyLibAdapter(stack.StackAdapter):
    name = "py_lib"
    label = "纯 Python 库（函数逻辑验收）"
    entry_path = "lib/__init__.py"
    entry_import_hint = "from .core import ..."
    route_hint = "公开 API 在 lib/__init__.py re-export；纯函数放 lib/core.py 便于 py_call 直调"
    router_decl = "__all__ = [...]"
    router_marker = "__all__"
    module_dirs = ("lib", "src")
    module_file_fmt = "lib/{seg}.py"
    module_globs = ("lib/**/*.py",)
    module_prefix = "lib."
    route_decorator_re = r"def\s+(\w+)\s*\("
    route_verb_default = "GET"
    route_hosts = ()
    template_dirs = ()
    static_dirs = ()
    static_mount = "/static"
    start_cmd = "python -c 'import lib'"
    app_ref = ""
    probe_app_candidates = ()
    deps = ()
    app_marker = "__all__"

    def asgi_app(self) -> bool:
        """纯库不是 ASGI，http 探针不适用。"""
        return False

    def web_rules(self) -> str:
        """Python 库的验收约定：直接调函数，断言返回值与异常。"""
        return """1. **用 py_call 探针直调函数**，判据至少给 `expect` 或 `expect_raises` 之一：
   {"kind":"py_call","module":"lib.core","func":"normalize","args":["  HI  "],
    "expect":"hi"}
   `module` 必须是仓库内可 import 的模块，`func` 是其属性；
   断言支持 expect（返回值相等）、expect_raises（异常类型名）等。
2. **错误路径也要判**：
   {"kind":"py_call","module":"lib.core","func":"divide","args":[1,0],
    "expect_raises":"ValueError"}
   不要只测 happy path，异常契约同样是需求的一部分。
3. 公开 API 在 `lib/__init__.py` 里 re-export（`__all__` 列出），
   但探针优先直调实现模块（`lib.core`）而不是门面，避免 import 副作用掩盖问题。
4. 函数写成**纯函数**（同输入同输出、不依赖网络/时间/全局状态），
   否则无法机械复验；需要外部依赖的拆成参数注入，测试时传替身。
5. 不要写依赖真实数据库/网络/计时器的判据 —— py_call 不提供这些环境，
   这类需求请用 cli 探针配注入素材，或标注为人在环验收。"""
