"""Python CLI 工具生态包。

能力边界（诚实声明）：
  · **识别**：有 `cli.py` → Python 命令行工具；
    marker 只用这一个独特文件（不与 py_lib 共用 pyproject.toml 抢判），
    与内置 django(manage.py) / fastapi(app/main.py)、
    spa(package.json)、electron(main.cjs)、microservice(celery) 都不重叠。
  · **验收**：`cli` 探针在临时目录真跑 `python -B cli.py <args>`，
    断言 stdout 含期望串 + 退出码；错误路径断言 expect_exit=1 + 错误文案。
    探针**不许只判退出码**（空壳 print(0) 也能过），必须有 stdout_has/stdout_eq。
  · **未支持**：交互式 TUI（依赖终端输入）、需联网/外部服务的 CLI —— 不假装能验。
"""
from core import stack


class CliToolAdapter(stack.StackAdapter):
    name = "cli_tool"
    label = "Python 命令行工具（CLI）"
    entry_path = "cli.py"
    entry_import_hint = "import argparse"
    route_hint = "用 argparse 定义子命令；每个子命令是纯函数，便于 py_call 单测"
    router_decl = "parser = argparse.ArgumentParser()"
    router_marker = "add_parser("
    module_dirs = ("cli", "commands", "src")
    module_file_fmt = "commands/{seg}.py"
    module_globs = ("**/*.py",)
    module_prefix = ""
    route_decorator_re = r"add_parser\(\s*[\"']([^\"']+)[\"']"
    route_verb_default = "GET"
    route_hosts = ("parser", "subparsers")
    template_dirs = ()
    static_dirs = ()
    static_mount = "/static"
    start_cmd = "python cli.py --help"
    app_ref = ""
    probe_app_candidates = ()
    deps = ()
    app_marker = "argparse"

    def asgi_app(self) -> bool:
        """CLI 不是 ASGI 服务，http 探针不适用。"""
        return False

    def web_rules(self) -> str:
        """CLI 工具的验收约定：真跑入口 + 断言 stdout 与退出码。"""
        return """1. **用 cli 探针真跑入口**，不要只判退出码（空壳 print(0) 也能过 exit=0）：
   {"kind":"cli","entry":"cli.py","args":["count","--file","d.csv"],
    "files":{"d.csv":"name,age\\n甲,20\\n乙,30\\n"},
    "stdout_has":"2","expect_exit":0}
   `entry` 必须是仓库内的 .py 脚本（不能写 python/pytest）；
   判据至少给 `stdout_has` 或 `stdout_eq` 之一。
2. **错误路径也要判**：
   {"kind":"cli","entry":"cli.py","args":["count","--file","nope.csv"],
    "stdout_has":"文件不存在","expect_exit":1}
   `files` 注入素材到临时目录，argv/cwd 由引擎控，不要写绝对路径。
3. 子命令实现成可独立调用的纯函数（`def count(path)` 返回结果），
   这样既能 cli 探针验命令行为，也能 pytest/py_call 验函数逻辑，判据不重复。
4. 不要写需要交互输入（input()/密码提示）或联网的 CLI —— 引擎的 cli 探针
   非交互、无外网；这类需求请改写成接受文件/参数的形式，否则无法机械验收。"""
