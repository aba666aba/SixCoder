"""前端 SPA 生态包：React/Vue + Vite 的栈适配器。

能力边界（诚实声明）：
  · **识别**：从 package.json / vite.config / src/ 判断是不是前端 SPA；
  · **路由解析**：从 JSX `<Route path="/x">` 或 vue-router `path: '/x'` 读路由；
  · **片段生成**：按契约给页面组件与路由项（返回文件内容，**不直接写盘**——
    写盘由引擎做，adapter 保持无副作用，这是生态包的安全模型）；
  · **骨架**：由引擎按本包 `templates/` 复制（adapter 只声明，不落盘）。
不做的（明确说清，不假装）：不做真机 UI 自动化（只做浏览器 DOM 断言）、
不做 SSR、不做原生 App。
"""
from core import stack


class FrontendSpaAdapter(stack.StackAdapter):
    """Vite + React（Vue 共用同一套接缝，仅模板不同）。"""

    name = "frontend_spa"
    label = "前端 SPA（React/Vue + Vite）"
    entry_path = "src/main.jsx"
    entry_import_hint = "import React from 'react'"
    route_hint = ('用 react-router 的 <Route path="/x" element={<Page/>}/>；'
                  'Vue 用 vue-router 的 routes 数组 { path, component }')
    router_decl = "const routes = ["
    router_marker = "<Route"
    module_dirs = ("src/pages", "src/views", "src/components", "src")
    module_file_fmt = "src/pages/{seg}.jsx"
    module_globs = ("src/**/*.jsx", "src/**/*.tsx", "src/**/*.vue",
                    "src/**/*.js", "src/**/*.ts")
    module_prefix = ""
    route_decorator_re = r'<Route[^>]*path=["\']([^"\']+)["\']'
    route_verb_default = "GET"
    route_hosts = ("routes", "Router")
    template_dirs = ("public",)
    static_dirs = ("public", "dist")
    static_mount = "/"
    start_cmd = "npm run dev"
    app_ref = ""
    probe_app_candidates = ()
    deps = ("react", "react-dom", "react-router-dom", "vite")
    app_marker = 'id="root"'

    def asgi_app(self) -> bool:
        """前端 SPA 不是 ASGI 应用（http 探针不能用，得走 browser 探针）。"""
        return False

    def web_rules(self) -> str:
        """喂给远端推导蓝图的**前端探针约定**（2026-11-19）。

        关键：前端 SPA **不是 ASGI**，`app.main:app` / uvicorn 那套完全不适用。
        远端必须用 `browser` 探针：起 `npm run dev`、真用浏览器访问、断 DOM。
        """
        return """1. **前端 SPA 不用 http 探针**（它不是 ASGI，`app.main:app` 那条路不存在）。
   一律用 `browser` 探针：真起 dev server、真用浏览器访问、按 DOM 断言判。
   示例：
   {"kind":"browser","start_cmd":"npm run dev","port":5173,
    "url":"http://127.0.0.1:5173/",
    "expect_dom":[{"selector":"#root"},{"selector":"h1","text":"首页"},
                  {"selector":"nav a","count":3}]}
2. `expect_dom` 每条断言格式：{"selector": <CSS 选择器>, 再带以下之一}
   · text：元素文本**包含**该串；· count：元素数量等于；· visible：是否可见
   **不许只写 url 不给断言**（空页面也能"能访问"，等于没判）。
3. 路由要走前端路由（react-router 的 `<Route path="/x">` 或 vue-router 的
   `routes:[{path:"/x"}]`），不要写后端 `/api/` 端点。
4. 落点（modules）只能在这些文件：
   · `src/main.jsx` —— 入口（挂载 + 路由容器），**已存在，不要重写**
   · `src/App.jsx` —— 根组件与路由表（由引擎渲染，能力里**不要**要求"实现路由"）
   · `src/pages/<页面>.jsx` —— 页面组件（一条能力 1~3 个组件）
   · `src/components/<组件>.jsx` —— 复用组件
   **禁止**出现 `app/`、`.py`、`config.asgi` 这类后端落点。
5. 需要交互的能力（点击/填写）用 `actions`：`[{"do":"click","selector":"button"},
   {"do":"fill","selector":"#q","value":"x"}]`，再配 `expect_dom` 断言结果。"""

    def module_for(self, seg: str) -> str:
        return "src/pages/%s.jsx" % seg

    def scaffold(self, ws) -> dict:
        """骨架由引擎按本包 templates/ 复制（adapter 不直接写盘）。"""
        return {"ok": True, "wrote": False,
                "why": "前端骨架由引擎复制包内 templates/（Vue/React 模板）"}

    def render_route(self, spec) -> list:
        """按一个路由规格生成页面组件 + 路由项（返回 [(相对路径, 内容)]）。"""
        path = str(getattr(spec, "path", "") or "/")
        seg = path.strip("/").replace("/", "_") or "home"
        comp = "".join(w[:1].upper() + w[1:] for w in seg.split("_")) or "Home"
        page = (
            "export default function %s() {\n"
            "  return <main className=\"page-%s\">%s</main>;\n"
            "}\n" % (comp, seg.replace("_", "-"), comp)
        )
        return [("src/pages/%s.jsx" % comp, page)]

    def parse_routes(self, src: str) -> list:
        """从 JSX / vue-router 源码里读路由路径。"""
        import re
        out = []
        for m in re.finditer(r'<Route[^>]*path=["\']([^"\']+)["\']', src):
            out.append(m.group(1))
        for m in re.finditer(r'path:\s*["\']([^"\']+)["\']', src):
            out.append(m.group(1))
        # 去重保序
        seen, uniq = set(), []
        for p in out:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return uniq

    def is_app_source(self, text: str) -> str:
        """判断一段源码是不是前端应用代码（识别用）。"""
        t = text or ""
        if "export default" in t or "defineComponent" in t or "<template>" in t:
            return "spa"
        if "createApp(" in t or "createRoot(" in t:
            return "spa"
        return ""
