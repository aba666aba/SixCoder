"""Electron 桌面应用生态包：识别 + 生成 + 渲染进程构建验收 + 真机 UI 验收。

能力边界（诚实声明）：
  · **识别**：`electron/main.js` 存在 → Electron 项目；
  · **生成**：主进程（BrowserWindow）、preload、渲染进程 React 页面；
  · **验收（构建级）**：渲染进程 `vite build` 通过 + 主进程语法检查；
  · **验收（真机 UI）**：electron 以 `--remote-debugging-port` 启动 → playwright
    `connect_over_cdp` 连真窗口 → DOM/标题断言（2026-11-23 接上）。

真机 UI 的边界：需要本机有 electron 二进制（项目 `npm install` 自带，
或系统装了）。`electron` 探针找不到二进制 / CDP 起不来 → 如实报"测不了"
（默认红，不假装通过）；能起来就真驱动窗口、真断 DOM。构建级仍作为兜底。
"""
from core import stack


class ElectronAdapter(stack.StackAdapter):
    name = "electron"
    label = "Electron 桌面应用（主进程 + React 渲染进程）"
    entry_path = "electron/main.cjs"
    entry_import_hint = "const { app, BrowserWindow } = require('electron')"
    route_hint = ("渲染进程用 react-router 的 <Route path=\"/x\">；"
                  "主进程只负责开窗口与 IPC，不写业务路由")
    router_decl = "const routes = ["
    router_marker = "<Route"
    module_dirs = ("src/renderer/pages", "src/renderer/components",
                   "src/renderer", "electron")
    module_file_fmt = "src/renderer/pages/{seg}.jsx"
    module_globs = ("src/renderer/**/*.jsx", "src/renderer/**/*.tsx",
                    "electron/**/*.js")
    module_prefix = ""
    route_decorator_re = r'<Route[^>]*path=["\']([^"\']+)["\']'
    route_verb_default = "GET"
    route_hosts = ("routes", "Router")
    template_dirs = ("public",)
    static_dirs = ("dist", "dist-electron")
    static_mount = "/"
    start_cmd = "npm run start"
    app_ref = ""
    probe_app_candidates = ()
    deps = ("electron", "react", "react-dom", "vite")
    app_marker = "new BrowserWindow("

    def asgi_app(self) -> bool:
        return False

    def module_for(self, seg: str) -> str:
        return "src/renderer/pages/%s.jsx" % seg

    def scaffold(self, ws) -> dict:
        return {"ok": True, "wrote": False,
                "why": "Electron 骨架由引擎复制包内 templates/"}

    def render_route(self, spec) -> list:
        path = str(getattr(spec, "path", "") or "/")
        seg = path.strip("/").replace("/", "_") or "home"
        comp = "".join(w[:1].upper() + w[1:] for w in seg.split("_")) or "Home"
        page = (
            "export default function %s() {\n"
            "  return <main className=\"page-%s\">%s</main>;\n"
            "}\n" % (comp, seg.replace("_", "-"), comp)
        )
        return [("src/renderer/pages/%s.jsx" % comp, page)]

    def parse_routes(self, src: str) -> list:
        import re
        out = []
        for m in re.finditer(r'<Route[^>]*path=["\']([^"\']+)["\']', src):
            out.append(m.group(1))
        for m in re.finditer(r'path:\s*["\']([^"\']+)["\']', src):
            out.append(m.group(1))
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def is_app_source(self, text: str) -> str:
        t = text or ""
        if "new BrowserWindow(" in t or "app.whenReady()" in t:
            return "electron-main"
        if "export default" in t or "defineComponent" in t:
            return "electron-renderer"
        return ""

    def web_rules(self) -> str:
        """Electron 项目的探针约定（2026-11-23：真机 UI 已接）。

        渲染进程是前端：优先用 **electron 探针**做真机 UI 验收（CDP 连真窗口+
        DOM 断言）；本机没 electron 二进制时，退回构建级探针（cli）。两条都默认红。
        """
        return """1. **Electron 不是 ASGI，也不是普通 Web 服务**：不要写 `app.main:app` 或 http 探针。
2. 渲染进程（React 页面）落到 `src/renderer/pages/<页面>.jsx`；
   主进程只放 `electron/main.js`（开窗口/IPC），**能力里不要要求"实现窗口逻辑"**。
3. 判据写法（优先真机 UI，构建级兜底）：
   · **真机 UI（首选）**：{"kind":"electron","exe":"node_modules/electron/dist/electron.exe",
     "app_dir":".","expect_dom":[{"selector":"h1","text":"主页"}],"expect_title":"MyApp"}
     —— 引擎以调试端口起 electron、用 CDP 连真窗口断 DOM。必须有 expect_dom /
     expect_title / actions 之一（只判"能启动"太弱，空壳窗口也能过）。
     找不到 electron 二进制会如实报"测不了"（默认红，不假装通过）。
   · **构建级（兜底）**：{"kind":"cli","entry":"scripts/build_check.js",
     "stdout_has":"build ok"} —— 引擎先 `npm run build` 再跑它。
   **不许**写 browser 探针的 url（Electron 没有稳定的 http dev server 端口）。
4. 落点禁止 `.py`、`app/`、`config.asgi` 这类后端路径。"""