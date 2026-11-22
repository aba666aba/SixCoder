"""Go 服务 / 命令行生态包（跨语言栈，2026-11-23）。

能力边界（诚实声明）：
  · **识别**：`go.mod` 存在 → Go 模块；marker 单一独特。
  · **验收**：`cli.cmd` 探针真跑 `go test ./...`，断言 stdout 含 `ok`（有测试的包）
    或 `PASS`；同时要求退出码 0。
  · **环境边界**：本机没装 Go 时如实报"命令不存在"，不假装通过。
  · **未支持**：`go test ./...` 对"没有测试文件的包"会输出 `no test files`，
    这**不算通过**——判据必须指向至少一个有真实断言的包。
"""
from core import stack


class GoServiceAdapter(stack.StackAdapter):
    name = "go_service"
    label = "Go 服务 / 命令行（go.mod）"
    entry_path = "go.mod"
    entry_import_hint = "module "
    route_hint = "包放 internal/<域>/，HTTP 路由用 gin/echo 的 r.GET(\"/x\") 声明"
    router_decl = "func main()"
    router_marker = "http.HandleFunc"
    module_dirs = ("internal", "cmd", "pkg")
    module_file_fmt = "internal/{seg}/{seg}.go"
    module_globs = ("**/*.go",)
    module_prefix = ""
    route_decorator_re = r"\.(?:GET|POST|PUT|DELETE)\(\s*[\"']([^\"']+)[\"']"
    route_verb_default = "GET"
    route_hosts = ("router", "engine", "mux")
    template_dirs = ()
    static_dirs = ()
    static_mount = "/static"
    start_cmd = "go test ./..."
    app_ref = ""
    probe_app_candidates = ()
    deps = ()
    app_marker = "module "

    def asgi_app(self) -> bool:
        """Go 服务不是 ASGI，http 探针不适用；用 cli.cmd 跑 go test。"""
        return False

    def web_rules(self) -> str:
        """Go 的验收约定：go test 必须有真实断言。"""
        return """1. **cli.cmd 探针**（真跑 go test ./...）：
   {"kind":"cli","cmd":["go","test","./..."],"stdout_has":"ok"}
   可执行文件须在白名单（go）；列表传参，不经 shell。
2. **判据要抓住"测试真的跑了"**：`ok  <pkg>` 或 `PASS`；
   `no test files` 不算通过——至少一个包要有真实断言（t.Fatalf / 比较返回值）。
3. 路由用 gin/echo 的 `r.GET("/x")`，路径可静态解析；**行为**仍以测试输出为准。
4. 本机没装 Go → 探针如实报"命令不存在"，不要写假断言。"""
