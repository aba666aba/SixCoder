"""A 档（§20.4 落点 1–4）产物一等公民 + B 档（§20.5）判据自检。

为什么必须钉死（实测背景）：
    · 需求里的前端要求是散文，contractgen 解析不出可判功能点 → app/web.py /
      app/dashboard.py 在 contract.txt 里 0 次出现 → **永远不会被派单**（§20.1 证据 2）。
    · cap.page.home 标 done，而需求点名的 app/templates/index.html 根本不存在
      —— 探针只测行为，不测"需求点名的产物是否真的产出"（§20.1 证据 3，done 骗人）。

本检查钉住的行为：
    A1 解析     【必须产出的文件】段 → ArtifactFile（归属/要点/全局段/零影响/幂等注入）
    A2 判据     artifact_exists：缺文件=硬失败；缺要点=点名；全产物判据不 import 模块
    A3 作者     产物块解析（三种格式）+ 落盘（只写声明过的）+ 提示词口径
    A4 闭环     inspect.artifacts_missing → needs_work → 收工理由点名产物缺口
    B  判据     dom_has / dom_not_has / openapi_has（真起 FastAPI 真发请求）+ 前端构建钩子
"""
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import contract as C                                    # noqa: E402
from core import props                                            # noqa: E402
from core import author as A                                      # noqa: E402
from core import iterate                                          # noqa: E402
from core import company as CO                                    # noqa: E402
from core import probe as PB                                      # noqa: E402
from core import auto as AU                                       # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


REQ = textwrap.dedent("""
    1) app/api/pages.py：show_home() -> str
    首页展示毕业设计选题与登录入口。

    【必须产出的文件】
    - app/templates/index.html   要点: 毕业设计选题, 登录
    - app/static/dashboard.js   要点: echarts

    2) app/services/topics.py：create_topic(title) -> dict
    创建题目。
""")


def main() -> int:
    print("[A1] 解析：段归属 / 要点 / 零影响 / 幂等")
    arts = C.parse_artifacts(REQ)
    check("解析出 2 个产物文件", len(arts) == 2, str([a.path for a in arts]))
    check("归属 = 声明它的小节模块",
          all(a.owner == "app/api/pages.py" for a in arts),
          str([(a.path, a.owner) for a in arts]))
    check("要点按分隔符拆开",
          arts and arts[0].contains == ["毕业设计选题", "登录"], str(arts[0].contains))
    cs = C.parse_requirement(REQ)
    check("不产生假模块（.html/.js 条目不是模块标题）",
          set(cs) == {"app/api/pages.py", "app/services/topics.py"}, str(sorted(cs)))
    check("契约携带产物声明",
          len(getattr(cs.get("app/api/pages.py"), "artifacts", [])) == 2
          and not getattr(cs.get("app/services/topics.py"), "artifacts", []))
    req_plain = "1) app/services/topics.py：create_topic(title) -> dict\n创建题目。\n"
    cs2 = C.parse_requirement(req_plain)
    check("没写段 → artifacts 为空（零影响，不写就不生效）",
          all(not c.artifacts for c in cs2.values()))
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        miss = C.missing_artifacts(ws, arts)
        check("文件不存在 → missing 且带 exists=False",
              len(miss) == 2 and all(not x["exists"] for x in miss), str(miss))
        (ws / "app" / "templates").mkdir(parents=True)
        (ws / "app" / "templates" / "index.html").write_text(
            "<h1>毕业设计选题</h1><a>登录</a>", encoding="utf-8")
        miss2 = C.missing_artifacts(ws, arts)
        check("文件在但另一个缺 → 只报缺的那个", len(miss2) == 1
              and miss2[0]["path"] == "app/static/dashboard.js", str(miss2))
    t0 = "1) app/api/pages.py：show_home()\n首页。\n"
    t1 = C.inject_artifacts(t0, arts)
    check("注入后含段", "【必须产出的文件】" in t1)
    check("注入幂等（同 path 不重复）", C.inject_artifacts(t1, arts) == t1)

    print("[A2] 判据：artifact_exists（真探针子进程）")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app" / "templates").mkdir(parents=True)
        (ws / "app" / "templates" / "index.html").write_text(
            "<h1>毕业设计选题</h1><a>登录</a>", encoding="utf-8")
        ok_c = [{"kind": "artifact_exists", "path": "app/templates/index.html",
                 "contains": ["毕业设计选题", "登录"], "desc": "首页", "hard": True}]
        no_file = [{"kind": "artifact_exists", "path": "app/templates/absent.html",
                    "contains": [], "desc": "缺文件", "hard": True}]
        no_key = [{"kind": "artifact_exists", "path": "app/templates/index.html",
                   "contains": ["echarts"], "desc": "缺要点", "hard": True}]
        r = props.run(ws, "app/api/pages.py", ok_c)
        check("文件齐 + 要点齐 → 过", bool(r.get("ok")), str(r.get("fails"))[:120])
        r = props.run(ws, "app/api/pages.py", no_file)
        check("文件不存在 → 硬失败并点名路径",
              not r.get("ok") and "app/templates/absent.html" in
              str((r.get("fails") or [{}])[0].get("detail")), str(r.get("fails"))[:120])
        r = props.run(ws, "app/api/pages.py", no_key)
        check("缺要点 → 硬失败并点名缺哪个字",
              not r.get("ok") and "echarts" in
              str((r.get("fails") or [{}])[0].get("detail")), str(r.get("fails"))[:120])
        # 全部是产物判据 → **不 import 模块**（这个模块路径必然导入失败，但判据照样能判）
        r = props.run(ws, "app/definitely/not/a/module.py", ok_c)
        check("全产物判据不 import 被测模块（模板存在与否跟包导入无关）",
              bool(r.get("ok")), str(r.get("fails"))[:120])

    print("[A3] 作者：产物块解析 / 落盘 / 提示词")
    reply = textwrap.dedent("""
        先写代码

        ```file:app/templates/index.html
        <!doctype html><h1>毕业设计选题</h1>
        ```

        ```python
        def f():
            return 1
        ```

        <file path="app/static/dashboard.js">
        // echarts init
        </file>
    """)
    blocks = A.parse_artifact_blocks(reply)
    check("三种格式都认（file: 围栏 / <file> 块）",
          "app/templates/index.html" in blocks and "app/static/dashboard.js" in blocks,
          str(sorted(blocks)))
    check("python 围栏不被误吞", all("def f" not in v for v in blocks.values()))
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        two = [NS(path="app/templates/index.html", contains=[]),
               NS(path="app/static/dashboard.js", contains=[]),
               NS(path="app/other/never.css", contains=[])]
        r = A.write_artifacts(ws, reply, two, None, "test")
        check("只写声明过的、交齐的不缺",
              sorted(r["written"]) == ["app/static/dashboard.js", "app/templates/index.html"]
              and r["missing"] == ["app/other/never.css"], str(r))
        check("内容原样落盘",
              "<h1>毕业设计选题</h1>" in
              (ws / "app" / "templates" / "index.html").read_text(encoding="utf-8"))
    pr = C.artifacts_prompt(arts)
    check("提示词带交回格式（file: 围栏）", "```file:app/templates/index.html" in pr)
    check("html 产物 → 带页面口径（Jinja2 / static / ECharts CDN）",
          "Jinja2" in pr and "/static" in pr and "ECharts" in pr)

    print("[A4] 闭环：inspect → needs_work → 收工理由")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app").mkdir()
        (ws / "app" / "__init__.py").write_text("", encoding="utf-8")
        (ws / "app" / "api").mkdir()
        (ws / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
        (ws / "app" / "api" / "pages.py").write_text(
            "def show_home():\n    return '毕业设计选题'\n", encoding="utf-8")
        contracts = C.parse_requirement(REQ)
        st = iterate.inspect(ws, contracts)
        s1 = st.get("app/api/pages.py") or {}
        check("inspect 标出 artifacts_missing",
              "app/templates/index.html" in (s1.get("artifacts_missing") or []),
              str(s1.get("artifacts_missing")))
        check("needs_work = True（产物缺 = 模块欠活）",
              CO.needs_work_state(st, "app/api/pages.py"))
        (ws / "app" / "templates").mkdir(parents=True)
        (ws / "app" / "templates" / "index.html").write_text(
            "毕业设计选题 登录", encoding="utf-8")
        (ws / "app" / "static").mkdir(parents=True)
        (ws / "app" / "static" / "dashboard.js").write_text("// echarts", encoding="utf-8")
        st2 = iterate.inspect(ws, contracts)
        check("产物齐了 → artifacts_missing 清空",
              not ((st2.get("app/api/pages.py") or {}).get("artifacts_missing")),
              str((st2.get("app/api/pages.py") or {}).get("artifacts_missing")))
    src_co = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(
        encoding="utf-8", errors="replace")
    check("收工理由必须点名产物缺口（不许说做完）", "产物文件未产出" in src_co)
    check("needs_work 把 artifacts_missing 算进去", "artifacts_missing" in src_co)

    print("[B] 判据：dom_has / dom_not_has / openapi_has（真 FastAPI）+ 前端构建")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app").mkdir()
        (ws / "app" / "__init__.py").write_text("", encoding="utf-8")
        (ws / "app" / "main.py").write_text(textwrap.dedent('''
            from fastapi import FastAPI
            from fastapi.responses import HTMLResponse
            app = FastAPI(title="t")
            @app.get("/")
            def home():
                return HTMLResponse("<html><body><h1>毕业设计选题</h1>"
                                    "<canvas id=\\"chart\\"></canvas>"
                                    "<script src=\\"https://cdn.jsdelivr.net/npm/echarts\\"></script>"
                                    "</body></html>")
            @app.get("/api/v1/topics")
            def topics():
                return {"items": []}
        '''), encoding="utf-8")
        good = {"kind": "http", "app": "app.main:app", "path": "/",
                "dom_has": ["tag:canvas", "id:chart", "text:毕业设计选题", "text:echarts"],
                "openapi_has": ["/api/v1/topics"]}
        bad = {"kind": "http", "app": "app.main:app", "path": "/",
               "dom_has": ["tag:video"], "openapi_has": ["/api/v1/absent"]}
        steps_form = {"kind": "http", "app": "app.main:app", "steps": [
            {"method": "GET", "path": "/", "dom_has": ["text:毕业设计选题"],
             "dom_not_has": ["text:不该出现"]}]}
        r = PB.run(ws, good)
        check("DOM 全对 + OpenAPI 暴露 → 过", bool(r.get("ok")), str(r.get("detail"))[:140])
        r = PB.run(ws, bad)
        d = str(r.get("detail") or "")
        check("缺元素 → 红且点名", (not r.get("ok")) and "<video>" in d, d[:140])
        check("OpenAPI 没暴露契约接口 → 红且点名", "/api/v1/absent" in d, d[:140])
        r = PB.run(ws, steps_form)
        check("steps 形态的 dom_has / dom_not_has 都生效", bool(r.get("ok")),
              str(r.get("detail"))[:140])
        check("validate：只有 DOM/OpenAPI 判据的探针不被拒收",
              PB.validate(good) == [], str(PB.validate(good)))
        check("validate：空壳探针仍被拒收（不许退化）",
              bool(PB.validate({"kind": "http", "app": "app.main:app", "path": "/"})))
    src_auto = (Path(__file__).resolve().parents[1] / "core" / "auto.py").read_text(
        encoding="utf-8", errors="replace")
    check("WEB_ENTRY 挂载 /static（静态资源要真能被服务到）",
          "StaticFiles" in src_auto and '"/static"' in src_auto)
    check("切片契约注入产物段（contractgen 可能丢散文段 → 引擎确定性注入）",
          "inject_artifacts" in src_auto and "artifact_injected" in src_auto)
    wb = AU.ensure_web_build(Path(tempfile.gettempdir()) / "definitely-not-a-project")
    check("前端构建钩子：没有 package.json → 零影响直接跳过", wb.get("ran") is False,
          str(wb))
    check("前端构建钩子：install/build 两段都在（真工程时才跑）",
          "node_modules" in src_auto and '"build"' in src_auto)

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项：{FAILS}")
        return 1
    print("全部通过（A1 解析 / A2 判据 / A3 作者 / A4 闭环 / B DOM+OpenAPI+构建）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
