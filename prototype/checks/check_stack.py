"""D 档（§20.7）栈适配层自检 + C 档语言判据接线自检。

钉死三件事（都是真会骗人的地方）：
  1. **FastAPI 逐字不变**：适配器生成的入口 / 路由段必须与改动前的实现
     一个字节都不差。做不到就是"接了个新层，把老行为悄悄改了"。
  2. **适配层真的可插拔**：Django 适配器七接缝齐全，且**真机起 ASGI 应用
     真发 HTTP 请求**（Django 装在 prototype/.tmp_djlibs，缺了就如实 SKIP，
     不许用"字符串长得像"冒充跑通）。
  3. **选栈是配置不是猜**：没有 stack.json → fastapi；写错名字 → 回退 fastapi。

2026-11-11 血泪：`StackAdapter` 用 `@dataclass` 时，子类 `name = "django"`
只是类属性，被生成的 `__init__` 默认值盖成 "fastapi" → 后注册的 Django
把 FastAPI 顶掉 → 默认栈静默变成 Django，三条互不相干的自检一起红。
本自检第 1 组断言就是这条的反向钉子。
"""
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import stack as ST                                      # noqa: E402
from core import auto as AU                                       # noqa: E402
from core import props as PR                                      # noqa: E402
from core import probe as PB                                      # noqa: E402
from core import deps as DP                                       # noqa: E402

FAILS = []
N = 0


def check(name, cond, detail=""):
    global N
    N += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


def tmpws():
    return Path(tempfile.mkdtemp(prefix="sixcoder-stack-"))


print("[1] 注册表与默认栈（dataclass 覆盖坑的反向钉子）")

st = ST.current(None)
check("默认栈是 fastapi", st.name == "fastapi", st.name)
check("两个适配器都注册了", set(ST.names()) == {"fastapi", "django"},
      str(ST.names()))
dj = ST.get("django")
check("Django 适配器的 name 真是 django（不是被基类默认值盖掉）",
      dj.name == "django", dj.name)
check("FastAPI 适配器类型正确", type(st).__name__ == "FastAPIAdapter",
      type(st).__name__)
check("未知栈不猜、回退默认", ST.get("no-such-stack").name == "fastapi")

print("[2] 接缝 1：入口模板逐字不变（回归铁律）")

mods = ["app.api.datasets", "app.auth"]
legacy = AU.WEB_ENTRY.replace("{modules!r}", repr(sorted(mods)))
check("FastAPI 适配器入口 == 旧 WEB_ENTRY 渲染结果",
      st.render_entry(mods) == legacy)
check("入口仍挂 /static（B 档产物没被冲掉）", "/static" in legacy)
check("入口仍是标准 ASGI app 对象", "app = FastAPI(" in legacy)
check("没有 stack.json 时 workspace 也走 fastapi",
      ST.current(tmpws()).name == "fastapi")

print("[3] 选栈是配置，不是猜")

ws = tmpws()
r = ST.write_choice(ws, "django")
check("写 stack.json 成功", r.get("ok") is True, str(r))
check("按配置选中 django", ST.current(ws).name == "django")
check("stack.json 内容可解析", (json.loads((ws / ST.STACK_FILE).read_text(
    encoding="utf-8")) or {}).get("stack") == "django")
bad = ST.write_choice(ws, "ruby-on-rails")
check("未知栈拒绝写入（不静默收下）", bad.get("ok") is False, str(bad))
check("拒绝后原配置不变", ST.current(ws).name == "django")
ws2 = tmpws()
(ws2 / ".sixcoder").mkdir(parents=True, exist_ok=True)
(ws2 / ST.STACK_FILE).write_text("{bad json", encoding="utf-8")
check("配置读坏了回退 fastapi（不炸夜班）", ST.current(ws2).name == "fastapi")

print("[4] 七接缝齐全")

for field in ("entry_path", "entry_import_hint", "route_hint", "module_dirs",
              "module_file_fmt", "route_decorator_re", "auto_import_table",
              "template_dirs", "start_cmd", "app_ref", "deps", "scaffold",
              "probe_app_candidates"):
    check(f"Django 适配器有接缝 {field}", hasattr(dj, field))
check("Django 入口是标准 ASGI（httpx 能喂）",
      "get_asgi_application()" in dj.render_entry([]))
check("Django 补 import 表含 django.http",
      "JsonResponse" in dj.auto_import_table())
check("Django 补 import 表不含 fastapi（栈隔离）",
      not any("fastapi" in v for v in dj.auto_import_table().values()))
check("FastAPI 补 import 表就是旧 _AUTO_IMPORTS",
      st.auto_import_table() == dict(AU._AUTO_IMPORTS))
check("FastAPI 不需要骨架（老行为零变化）",
      st.scaffold(tmpws()).get("wrote") is False)

print("[5] 接缝 4：端点反查按栈")

fast_src = '@router.get("/api/v1/x")\n@router.post("/api/v1/y")\n'
check("FastAPI 反查 @router.*", ("GET", "/api/v1/x") in st.parse_routes(fast_src))
check("FastAPI 反查 ${id} → {id}",
      ("GET", "/x/{id}") in st.parse_routes('@router.get("/x/${id}")'))
dj_src = 'urlpatterns = [path("api/v1/x/", views.x)]\n'
check("Django 反查 path()", ("GET", "/api/v1/x/") in dj.parse_routes(dj_src),
      str(dj.parse_routes(dj_src)))
check("Django 路径参数转换 {id} → <int:id>",
      dj.route_path("/api/v1/x/{teacher_id}") == "/api/v1/x/<int:teacher_id>",
      dj.route_path("/api/v1/x/{teacher_id}"))
check("Django 字符串参数用 <str:>",
      dj.route_path("/u/{name}") == "/u/<str:name>", dj.route_path("/u/{name}"))

print("[6] 接缝 2：RouteSpec 渲染逐字对齐旧实现")

spec = ST.RouteSpec(method="post", path="/api/v1/tickets", func="create_ticket",
                    args=[("ticket_id", "_to_int(ticket_id)"), ("title", '_b.get("title")')],
                    body_keys=["title"], body_types={"title": "str"},
                    body_required=["title"], body_sample={"title": "t"},
                    status=201, path_params=["ticket_id"],
                    slug="post_api_v1_tickets")
lines = st.render_route(spec)
joined = "\n".join(lines)
check("FastAPI 路由仍是 @router.<method>", '@router.post("/api/v1/tickets"' in joined)
check("FastAPI 路由保留 status_code", "status_code=201" in joined)
check("FastAPI 路由保留 Pydantic 模型", "class _Body_post_api_v1_tickets(BaseModel):" in joined)
check("FastAPI 路由转调作者函数", "return create_ticket(" in joined)
check("FastAPI 路由保留 422 语义", "HTTPException(status_code=422" in joined)
pre = st.route_prelude([spec], set())
check("prelude 补 pydantic / fastapi import",
      any("BaseModel" in x for x in pre) and any("from fastapi import" in x for x in pre))
check("prelude 补 router 声明", "router = APIRouter()" in pre)
check("prelude 补 _to_int", any("_to_int" in x for x in pre))

print("[7] 接线断言（源码级：适配层必须真的被调用）")

auto_src = (Path(__file__).resolve().parents[1] / "core" / "auto.py").read_text(
    encoding="utf-8")
check("auto.write_web_entry 用 st.render_entry", "body = st.render_entry(mods)" in auto_src)
check("auto._ensure_imports 用栈补 import 表", "auto_import_table()" in auto_src)
check("auto.ensure_web_router 用栈渲染路由", "_ad.render_route(spec)" in auto_src)
check("auto.ensure_web_router 用栈 prelude", "_ad.route_prelude(specs, bound)" in auto_src)
probe_src = (Path(__file__).resolve().parents[1] / "core" / "probe.py").read_text(
    encoding="utf-8")
check("probe 按栈取 app 候选", "_stack_app_candidates" in probe_src)
deps_src = (Path(__file__).resolve().parents[1] / "core" / "deps.py").read_text(
    encoding="utf-8")
check("deps._is_asgi 按栈判定", "ad.is_app_source(t)" in deps_src)
check("deps 按栈补依赖", "_st2.current(ws).deps" in deps_src)
oh_src = (Path(__file__).resolve().parents[1] / "core" / "openhouse.py").read_text(
    encoding="utf-8")
check("openhouse 接线前跑栈骨架", "_st.current(ws).scaffold(ws)" in oh_src)

print("[8] C 档语言判据在 props 执行器里真跑")

wsl = tmpws()
ok_chk = {"kind": "lang_test", "lang": "python",
          "cmd": [sys.executable, "-c", "print(1+1)"], "desc": "冒烟", "hard": True}
r1 = PR.run(wsl, "x.py", [ok_chk])
check("lang_test 真跑且通过", r1.get("ok") is True and r1.get("passed") == 1, str(r1)[:120])
bad_chk = {"kind": "lang_test", "lang": "python",
           "cmd": [sys.executable, "-c", "import sys;sys.exit(3)"],
           "desc": "必败", "hard": True}
r2 = PR.run(wsl, "x.py", [bad_chk])
check("lang_test 失败要判红且带退出码", r2.get("ok") is False
      and "退出码 3" in str(r2.get("fails")), str(r2)[:160])
r3 = PR.run(wsl, "x.py", [{"kind": "lang_test", "desc": "空命令", "hard": True}])
check("lang_test 没带命令要判红（不许静默跳过）",
      r3.get("ok") is False and "没带命令" in str(r3.get("fails")))
check("语言判据不 import 被测模块（Go/Java 项目不炸）",
      "_NO_MODULE_KINDS" in (Path(__file__).resolve().parents[1] / "core" /
                             "props.py").read_text(encoding="utf-8"))

print("[9] Django 真机：起 ASGI 应用 + 真发 HTTP 请求")

DJLIB = Path(__file__).resolve().parents[1] / ".tmp_djlibs"
if not DJLIB.is_dir():
    print("  SKIP  .tmp_djlibs 不存在（Django 未装到项目临时目录），如实跳过真机验证")
else:
    wsd = tmpws()
    ST.write_choice(wsd, "django")
    # 真实形态是 config/ 布局（工程配置与业务域分开）——自检也照这个形态跑，
    # 否则「入口 settings 包名写死成 app」这种事故在自检里永远看不见
    # （2026-11-15：产品仓 config/asgi.py 是 app.settings → 覆盖恒 0/76）。
    (wsd / "config").mkdir(parents=True, exist_ok=True)
    sc = dj.scaffold(wsd)
    check("Django 骨架落盘（config/ 布局）", sc.get("wrote") is True and
          (wsd / "config" / "settings.py").is_file(), str(sc)[:120])
    entry = dj.render_entry([])
    check("入口 settings 包名跟着 entry_path（不再写死 app）",
          '"config.settings"' in entry and "app.settings" not in entry,
          entry.split("setdefault")[-1].strip()[:80])
    check("入口暴露名是 application（app_ref 就是 config.asgi:application）",
          "application = get_asgi_application()" in entry)
    ep = wsd / dj.entry_path
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(entry, encoding="utf-8")
    script = textwrap.dedent("""
        import asyncio, json, sys
        sys.path.insert(0, sys.argv[1])
        sys.path.insert(0, sys.argv[2])
        import httpx
        from config.asgi import application as app
        async def main():
            tr = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=tr, base_url="http://test") as c:
                r = await c.get("/__sixcoder_probe__")
                print(json.dumps({"status": r.status_code,
                                  "asgi": str(type(app).__name__)}))
        asyncio.run(main())
        """)
    sp = wsd / "_asgi_probe.py"
    sp.write_text(script, encoding="utf-8")
    try:
        p = subprocess.run([sys.executable, "-B", str(sp), str(wsd), str(DJLIB)],
                           capture_output=True, text=True, timeout=90,
                           encoding="utf-8", errors="replace", cwd=str(wsd))
        tail = [x for x in (p.stdout or "").strip().splitlines() if x.strip()]
        if tail and tail[-1].startswith("{"):
            data = json.loads(tail[-1])
            check("Django ASGI 应用能被 httpx 真发请求（404 也算真跑通）",
                  int(data.get("status")) in (200, 404), str(data))
        else:
            check("Django ASGI 应用能被 httpx 真发请求", False,
                  (p.stderr or "")[-300:])
    except subprocess.TimeoutExpired:
        check("Django ASGI 应用能被 httpx 真发请求", False, "超时 90s")
    except Exception as exc:                              # noqa: BLE001
        check("Django ASGI 应用能被 httpx 真发请求", False,
              f"{type(exc).__name__}: {exc}")

print("[10] 回归：既有自检必须仍绿")

for name in ("check_webwire.py", "check_routing_safety.py", "check_wiring_fidelity.py",
             "check_artifact_files.py", "check_enterprise_cd.py", "check_lang_tier.py"):
    p = subprocess.run([sys.executable, str(Path(__file__).resolve().parent / name)],
                       capture_output=True, text=True, timeout=180,
                       encoding="utf-8", errors="replace")
    check(f"回归 {name}", p.returncode == 0, (p.stdout or p.stderr or "")[-160:])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
    raise SystemExit(1)
print(f"全部通过（{N} 条断言：栈适配层 / 逐字回归 / 七接缝 / 真机 ASGI / 语言判据）")
