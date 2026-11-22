"""Java Spring Boot 生态包（跨语言栈，2026-11-23）。

能力边界（诚实声明）：
  · **识别**：`pom.xml` 存在 → Spring Boot / Maven 项目；marker 单一独特，
    与 Python 的 manage.py / app/main.py、Node 的 server.js、Go 的 go.mod 都不重叠。
  · **验收**：`cli.cmd` 探针真跑 `mvn -q test`，断言 stdout 含 `BUILD SUCCESS`
    （以及 `Tests run: N` 这类"测试真跑了"的证据），argv 列表传参、不经 shell。
  · **环境边界**：本机没装 JDK/Maven 时探针报"命令不存在"——如实报，不假装通过。
  · **未支持**：运行期装配（数据源、Bean 顺序）与真实第三方依赖仍以测试输出为准，
    静态只能解析控制器路由路径。
"""
from core import stack


class SpringBootAdapter(stack.StackAdapter):
    name = "spring_boot"
    label = "Java Spring Boot（Maven）"
    entry_path = "pom.xml"
    entry_import_hint = "<artifactId>"
    route_hint = "控制器放 src/main/java/<pkg>/controller/，用 @GetMapping(\"/x\") 声明路径"
    router_decl = "@RestController"
    router_marker = "@RequestMapping"
    module_dirs = ("src/main/java", "src/test/java")
    module_file_fmt = "src/main/java/{seg}.java"
    module_globs = ("**/*.java",)
    module_prefix = ""
    route_decorator_re = r"Mapping\(\s*[\"']([^\"']+)[\"']"
    route_verb_default = "GET"
    route_hosts = ("controller", "RestController")
    template_dirs = ()
    static_dirs = ()
    static_mount = "/static"
    start_cmd = "mvn -q test"
    app_ref = ""
    probe_app_candidates = ()
    deps = ()
    app_marker = "spring-boot"

    def asgi_app(self) -> bool:
        """Spring Boot 不是 ASGI，http 探针不适用；用 cli.cmd 跑构建/测试。"""
        return False

    def web_rules(self) -> str:
        """Spring Boot 的验收约定：用 cli.cmd 真跑 Maven 测试。"""
        return """1. **跨语言栈用 cli.cmd 探针**（不是 entry）：引擎直接执行该命令，argv 列表传参、不经 shell：
   {"kind":"cli","cmd":["mvn","-q","test"],"stdout_has":"BUILD SUCCESS"}
   可执行文件必须在白名单（mvn / mvnw / gradle / gradlew），不要写 shell 脚本。
2. **判据要抓住"测试真的跑了"**：用 `BUILD SUCCESS` 或 `Tests run: N, Failures: 0`；
   只判退出码 = 空项目也能过（Maven 没测试也 BUILD SUCCESS）。
3. 控制器路径可静态解析（`@GetMapping("/x")`），但**接口行为**仍以测试输出为准。
4. 本机没装 JDK/Maven 时探针会如实报"命令不存在"——这是环境边界，不是产物错；
   **不要**为了变绿去写假断言或改成 `echo`。"""
