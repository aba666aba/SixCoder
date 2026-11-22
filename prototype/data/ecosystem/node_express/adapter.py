"""Node.js Express 后端生态包（跨语言栈，2026-11-23）。

能力边界（诚实声明）：
  · **识别**：`server.js` 存在 → Node 后端；marker 不用 package.json
    （那会与 frontend_spa 抢判）。
  · **验收**：`cli.cmd` 探针跑 `npm test --silent`（或 `node check.js` 自检脚本），
    断言 stdout 含 `passing` / 明确输出；退出码 0。
  · **环境边界**：本机没装 Node 时如实报"命令不存在"，不假装通过。
  · **未支持**：Express 的路由注册是运行期对象，静态只能解析 `app.get("/x")`；
    webview/前端渲染不归这个包（那是 frontend_spa）。
"""
from core import stack


class NodeExpressAdapter(stack.StackAdapter):
    name = "node_express"
    label = "Node.js Express 后端"
    entry_path = "server.js"
    entry_import_hint = "require('express')"
    route_hint = "路由放 routes/，用 app.get('/x', handler) 或 router.get('/x')"
    router_decl = "express()"
    router_marker = "app.listen("
    module_dirs = ("routes", "controllers", "src")
    module_file_fmt = "routes/{seg}.js"
    module_globs = ("**/*.js", "**/*.mjs")
    module_prefix = ""
    route_decorator_re = r"(?:app|router)\.(?:get|post|put|delete)\(\s*[\"']([^\"']+)[\"']"
    route_verb_default = "GET"
    route_hosts = ("app", "router")
    template_dirs = ()
    static_dirs = ()
    static_mount = "/static"
    start_cmd = "npm test --silent"
    app_ref = ""
    probe_app_candidates = ()
    deps = ()
    app_marker = "express"

    def asgi_app(self) -> bool:
        """Express 不是 ASGI，http 探针不适用；用 cli.cmd 跑测试/自检。"""
        return False

    def web_rules(self) -> str:
        """Express 的验收约定：npm test 或 node 自检脚本。"""
        return """1. **cli.cmd 探针**（二选一）：
   {"kind":"cli","cmd":["npm","test","--silent"],"stdout_has":"passing"}
   {"kind":"cli","cmd":["node","check.js"],"stdout_has":"ok"}
   可执行文件须在白名单（npm / npx / node / pnpm / yarn）。
2. **注意 `npm init` 的默认 test 是 `echo \"no test specified\" && exit 1`**——
   它会让你"看起来有 test 但永远失败"。必须真的写测试或自检脚本。
3. Express 路由 `app.get('/x')` 可静态解析；**行为**以测试/自检输出为准。
4. 本机没装 Node → 探针如实报"命令不存在"，不要写假断言。"""
