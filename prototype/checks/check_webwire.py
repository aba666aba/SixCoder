"""Web 路由接线自检（2026-11-07 真实夜班暴露的四个 bug）。

事故现场（某 Django/FastAPI 双向选题系统）：
\\WorkSpace\\biyeshejishuangxuan，Django/FastAPI 双向选题系统）：
夜班日志一片红，6 轮振荡后 `oscillation action=stop`，覆盖 0/76。逐条查下来，
**没有一条是"能力做不出来"**，全是引擎自己的接线错：

  1. `app/main.py`（ASGI 入口）被当成路由模块接线，尾部追加了 `router = APIRouter()`
     而入口没有、也不该有 APIRouter 的 import → `import app.main` 直接 NameError
     → 探针报"没有可用的 ASGI 应用"，所有 Web 能力一起红。
  2. 引擎段要不要补 `from fastapi import APIRouter`，原先用**正则搜全文**判断。作者
     习惯把 `router = APIRouter()` 写进模块 docstring 当说明 → 正则在 docstring 里命中
     → 引擎以为已经 import 了 → 生成的代码一 import 就 NameError。
  3. 一条路由都接不上时，仍然生成 `router = APIRouter()` + import + `_to_int` 的死代码。
  4. `_pick_func` 只按 create/add/update 这类动作词选函数，而契约里"路由背后的服务
     函数"叫 `*_endpoint`（`run_batch_endpoint`）→ 永远选不中 → 路由不接线 → 404。

另有两处同源问题在同一个夜里出现，一并在这里钉住：
  5. `POST /api/v1/batches`（带 4 个请求体字段）被硬接到 `run_batch_endpoint(batch_id=1)`
     —— 路径里根本没有 id，硬塞一个 1 只是让路由"注册成功"，跑的却是另一件事。
  6. `core/auto.py` 有一段整块重复定义（`write_web_entry` / `_load_routes` / `_save_routes`
     各两份），后一份静默覆盖前一份。

跑法: python checks/check_webwire.py
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import auto  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


API_SRC = '''"""批次路由：模块内建 router = APIRouter()，挂 POST /api/v1/batches/{batch_id}/run。"""
from fastapi import Body, Header


def run_batch_endpoint(batch_id: str) -> dict:
    return {"batch_id": batch_id}
'''

RUN_ROUTE = {"method": "POST", "path": "/api/v1/batches/{batch_id}/run",
             "func": "run_batch_endpoint", "body_keys": [], "status": 200}
CREATE_ROUTE = {"method": "POST", "path": "/api/v1/batches",
                "func": "run_batch_endpoint",
                "body_keys": ["algorithm", "dataset_version", "seed", "teacher_pref_source"],
                "body_sample": {"algorithm": "da", "dataset_version": "v1", "seed": 42,
                                "teacher_pref_source": "form"}}


def make_ws(api_src: str = API_SRC) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="wc_"))
    (ws / "app" / "api").mkdir(parents=True)
    (ws / "app" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "app" / "api" / "batches.py").write_text(api_src, encoding="utf-8")
    return ws


def wire(ws: Path, routes: list[dict], module: str = "app/api/batches.py") -> dict:
    p = ws / module
    funcs = auto._module_signatures(p.read_text(encoding="utf-8"))
    return auto.ensure_web_router(ws, module, routes, funcs)


def main() -> int:
    # 1) docstring 里提到 APIRouter ≠ 已经 import
    ws = make_ws()
    res = wire(ws, [RUN_ROUTE])
    src = (ws / "app/api/batches.py").read_text(encoding="utf-8")
    check("docstring 提到 APIRouter 不会免掉 import", "from fastapi import APIRouter" in src,
          "引擎段缺 import → 一 import 就 NameError")
    check("路由真的挂上了", "@router.post(\"/api/v1/batches/{batch_id}/run\"" in src)
    check("接线返回值报真接上的条数", res.get("routes") == 1, str(res.get("routes")))
    _defs = len(re.findall(r"^router\s*=\s*APIRouter\(", src, re.M))
    check("不重复定义 router", _defs == 1, f"模块级赋值 {_defs} 次")

    # 2) 接线后的模块必须能被解释器真正 import（文本里看着对不算数）
    (ws / "app/main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n",
                                    encoding="utf-8")
    proc = subprocess.run([sys.executable, "-c",
                           "import sys;sys.path.insert(0,'.');"
                           "import app.api.batches as m;print(len(m.router.routes))"],
                          cwd=str(ws), capture_output=True, text=True, timeout=120,
                          encoding="utf-8", errors="replace")
    check("接线后的模块能 import 且注册出路由", proc.returncode == 0 and proc.stdout.strip() == "1",
          (proc.stderr or proc.stdout or "")[-160:])

    # 3) 入口文件绝不接线
    entry = ws / "app/main.py"
    before = entry.read_text(encoding="utf-8")
    r2 = wire(ws, [RUN_ROUTE], module="app/main.py")
    check("app/main.py 不被当成路由模块", entry.read_text(encoding="utf-8") == before,
          str(r2))
    check("入口接线调用明确拒绝", r2.get("wrote") is False)

    # 4) 一条路由都接不上：只清旧段，不生成死代码
    ws2 = make_ws('"""x"""\n\n\ndef ping() -> dict:\n    return {}\n')
    (ws2 / "app/api/other.py").write_text('"""x"""\n\n\ndef ping() -> dict:\n    return {}\n',
                                          encoding="utf-8")
    r3 = auto.ensure_web_router(ws2, "app/api/other.py", [], {})
    txt = (ws2 / "app/api/other.py").read_text(encoding="utf-8")
    check("空路由不生成 router 死代码", "router = APIRouter(" not in txt)
    check("空路由不生成 import 死代码", "from fastapi import" not in txt)
    check("空路由保持 wrote=False（无事可做）", r3.get("wrote") is False, str(r3))

    # 5) 旧引擎段（指向已被删掉的函数）必须被剥干净
    stale = ('"""x"""\n\n\ndef ping() -> dict:\n    return {}\n\n'
             + auto.WEB_ROUTE_BEGIN + "\nrouter = APIRouter()\n"
             + '@router.get("/old")\n'
             + "def _engine_old():\n    return gone_func()\n"
             + auto.WEB_ROUTE_END + "\n")
    ws3 = make_ws('"""x"""\n\n\ndef ping() -> dict:\n    return {}\n')
    (ws3 / "app/api/other.py").write_text(stale, encoding="utf-8")
    auto.ensure_web_router(ws3, "app/api/other.py", [], {})
    txt3 = (ws3 / "app/api/other.py").read_text(encoding="utf-8")
    check("旧引擎段被剥干净", auto.WEB_ROUTE_BEGIN not in txt3 and "gone_func" not in txt3)

    # 6) *_endpoint 命名能被认出来（动作词组选不中时的兜底）
    funcs = auto._module_signatures(API_SRC)
    check("_pick_func 认得 run_batch_endpoint",
          auto._pick_func("/api/v1/batches/{batch_id}/run", funcs, "POST") == "run_batch_endpoint",
          auto._pick_func("/api/v1/batches/{batch_id}/run", funcs, "POST"))

    # 7) 路径里没有 id 就不许硬塞 1
    ws4 = make_ws()
    wire(ws4, [CREATE_ROUTE])
    src4 = (ws4 / "app/api/batches.py").read_text(encoding="utf-8")
    check("无 id 可接时不硬接路由", '@router.post("/api/v1/batches"' not in src4,
          "硬接会把创建路由绑到 run_batch_endpoint(batch_id=1) 上")

    # 8) auto.py 自身不许有整块重复定义（结构体检同款判据）
    tree = __import__("ast").parse((ROOT / "core" / "auto.py").read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (__import__("ast").FunctionDef, __import__("ast").ClassDef)):
            seen[node.name] = seen.get(node.name, 0) + 1
    dup = sorted(k for k, v in seen.items() if v > 1)
    check("auto.py 无重复定义", not dup, ",".join(dup))

    # 9) 收原始请求体的函数必须拿到整个 body（实测 2026-11-08 13:10：包装器把
    #    request 写死成 "" → recover_request_body("") 为假 → LLM 能力永远 absent）
    auto_src = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("包装器对 request/payload 类参数传 _b 而非空串",
          'args.append(f"{pn}=_b")' in auto_src, "生成器缺 request=_b 分支")

    # 9.5) 跨模块符号必须能确定性补 import（实测 2026-11-08：app/api/org.py 一个 import
    #      都没有却调用 app/models.py 的 _models_state() → 探针 500 → 能力永远 absent）
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app").mkdir(parents=True)
        (ws / "app" / "models.py").write_text(
            "def _models_state() -> dict:\n    return {}\n", encoding="utf-8")
        mod = ws / "app" / "org.py"
        mod.write_text("def insert_term(name: str) -> dict:\n"
                       "    return _models_state()\n", encoding="utf-8")
        src = mod.read_text(encoding="utf-8")
        out, adds = auto._ensure_local_imports(ws, src, "app/org.py")
        check("跨模块符号自动补 import",
              adds == ["from app.models import _models_state"], f"adds={adds}")
        check("补 import 插在模块级（不破坏缩进）",
              out.splitlines()[0].startswith("from app.models import")
              and __import__("ast").parse(out) is not None, "插入位置不对")

    # 9.6) 路径末段必须在函数名里出现，否则宁可不接（实测 2026-11-08：
    #      /auth/login 被接到 parse_authorization_header → 所有登录前置链全崩）
    check("末段不匹配就不接线",
          auto._pick_func("/api/v1/auth/login",
                          {"parse_authorization_header": ["header"]}, "POST") == "",
          "仍会误接")

    # 9.7) 短末段（me）不能因长度过滤退化成上一层资源段（实测 2026-11-08：
    #      GET /auth/me 接到 parse_authorization_header(header="") → 永远返回空串）
    check("短末段不被长度过滤吞掉",
          auto._pick_func("/api/v1/auth/me",
                          {"parse_authorization_header": ["header"]}, "GET") == "",
          "me 退化成 auth，被 parse_authorization_header 靠子串抢走")
    check("短末段能按词命中 get_me",
          auto._pick_func("/api/v1/auth/me",
                          {"get_me": [], "parse_authorization_header": ["h"]},
                          "GET") == "get_me",
          "词级匹配失效")

    # 9.8) 方法-动词冲突必须硬否决（实测 2026-11-08 路由表 7/22 条错的）：
    #      GET /analytics/directions→create_direction、GET /topics/{id}→submit_topic
    check("GET 不许接 create_*",
          auto._pick_func("/api/v1/analytics/directions",
                          {"create_direction": ["code"]}, "GET") == "",
          "GET 打到创建函数会改库状态")
    check("GET 不许接 submit_*",
          auto._pick_func("/api/v1/topics/{topic_id}",
                          {"submit_topic": ["topic_id"], "create_topic": []}, "GET") == "",
          "GET /topics/{id} 被 submit_topic 抢走")
    check("GET 详情端点优先读语义函数",
          auto._pick_func("/api/v1/topics/{topic_id}",
                          {"list_topics": [], "get_topic_detail": ["topic_id"]},
                          "GET") == "get_topic_detail",
          "详情端点没优先 get/detail")

    # 9.9) 根路径必须能接首页（实测 2026-11-08：`GET /` 在 toks 为空时无条件 return ""，
    #      作者写好的 home_page_route 永远接不上 → page.home / ui.index 连续 10 轮 404，
    #      而 apply_web_wiring 整块重建又把旧路由清掉，两条已 done 能力被拉红）
    check("根路径接到首页路由函数",
          auto._pick_func("/", {"home_page_html": [], "home_page_route": []},
                          "GET") == "home_page_route",
          "GET / 接不上首页")
    check("根路径不接 POST",
          auto._pick_func("/", {"home_page_route": []}, "POST") == "",
          "POST / 不该硬接首页")

    # 9.10) 构造器/解析器不许当端点（实测 2026-11-08：schemas.py 的
    #       build_direction_request 被 GET /analytics/directions 抓走）
    check("build_* 不当端点",
          auto._pick_func("/api/v1/analytics/directions",
                          {"build_direction_request": []}, "GET") == "",
          "构造器被当成聚合接口")
    check("me 认得 current_user 别名",
          auto._pick_func("/api/v1/auth/me",
                          {"get_current_user": ["authorization"]}, "GET") == "get_current_user",
          "REST 惯例的 current_user 没接上")
    check("PUT 不许接 create_*",
          auto._pick_func("/api/v1/llm/candidates/{candidate_id}",
                          {"create_candidates": ["request"]}, "PUT") == "",
          "PUT 接到 create_candidates 会多造候选")

    # 9.11) 通用末段（me）必须再命中资源段：实测 2026-11-08 路由表里
    #       /people/students/me、/people/teachers/me、/results/me 三条全被 deps.auth_me 抢走
    check("通用末段 me 不许跨资源抢",
          auto._pick_func("/api/v1/people/students/me",
                          {"auth_me": ["authorization"]}, "GET") == "",
          "students/me 落到 auth_me")
    check("通用末段 me 同资源仍可接",
          auto._pick_func("/api/v1/auth/me",
                          {"auth_me": ["authorization"]}, "GET") == "auth_me",
          "同资源的 me 端点被误杀")
    check("学生的 me 能接到档案函数",
          auto._pick_func("/api/v1/people/students/me",
                          {"auth_me": [], "get_student_profile": ["authorization"]},
                          "GET") == "get_student_profile",
          "get_student_profile 没被选中")
    # 9.12) 归一化工具不是端点（实测 2026-11-08：PUT /topics/{id}/quota → normalize_quota）
    check("normalize_* 不当端点",
          auto._pick_func("/api/v1/topics/{topic_id}/quota",
                          {"normalize_quota": ["quota"]}, "PUT") == "",
          "归一化工具被当成更新端点")

    # 10) 落点解析必须跳过 `api` / `v1` 这类版本前缀（实测 2026-11-08：
    #     `/api/v1/topics` 被分到 `app/api.py` → 文件不存在 → 整条能力被跳过 →
    #     路由永远 404，能力永远 absent）
    from core import blueprint as _bp
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app" / "api").mkdir(parents=True)
        (ws / "app" / "api" / "topics.py").write_text(
            "def create_topic() -> dict://n    return {}\n", encoding="utf-8")

        class _FakeCap:
            probe = {"kind": "http",
                     "steps": [{"method": "POST", "path": "/api/v1/topics"}]}

        got = auto._resolve_route_module(ws, _FakeCap())
        check("落点解析跳过版本前缀", got == "app/api/topics.py", f"got={got!r}")

        # 11) fill_http_modules 也不能把版本前缀当资源段
        cap = _bp.Cap(id="cap.t.x", desc="x", funcs_est=1,
                      probe={"kind": "http",
                             "steps": [{"method": "POST", "path": "/api/v1/topics"}]})

        class _Bp:
            caps = [cap]
        _bp.fill_http_modules(_Bp())
        # 判据是"资源段不能是 api/v1"，而不是某个固定路径：
        # 产物仓库所有路由都在 app/api/ 下（app/api/topics.py），扁平布局才在 app/topics.py。
        check("模块分配不取版本前缀",
              bool(cap.modules) and __import__("pathlib").Path(cap.modules[0]).stem == "topics",
              f"modules={cap.modules}")

    # 9.13) 外键/归属 id 参数可以接（否则 POST /topics 整条不接线 → 主干 404，
    #       实测 2026-11-09 一整夜 7 次"覆盖停滞"replan 就是这么来的）
    check("teacher_id 对 POST /topics 是外键",
          auto._foreign_id_param("teacher_id", "/api/v1/topics", "POST"),
          "归属 id 被误判成主键，路由接不上")
    check("batch_id 对 POST /batches 是主键（不许硬塞）",
          not auto._foreign_id_param("batch_id", "/api/v1/batches", "POST"),
          "主键被当成外键会操作不存在记录")
    check("带参数段取参数前那段资源",
          not auto._foreign_id_param("batch_id",
                                     "/api/v1/batches/{batch_id}/run", "POST"),
          "/batches/{id}/run 的资源应是 batches")
    check("复数 -ches 要去 es",
          not auto._foreign_id_param("batch_id", "/api/v1/batches", "POST"),
          "batches→batche 会误判")
    check("GET 不给默认 id",
          not auto._foreign_id_param("teacher_id", "/api/v1/topics", "GET"),
          "读操作不该塞归属 id")

    # 10) **末段命中**的单数化（_pick_func 里的 forms()）：
    #     'batches' 单数是 'batch'，只砍一个 's' 会得到 'batche' → 命中全灭 →
    #     路由判定"没有匹配函数" → 不接线 → 404 → 能力永远 absent
    #     （实测 2026-11-09 14:45：夜班在 cap.batch.create 上一片反复重跑，gained=[]）
    _sigs = {"create_batch": ["dataset_version", "algorithm"],
             "create_batch_endpoint": ["dataset_version", "algorithm"],
             "seed_endpoint": ["scenario", "seed"],
             "parse_batch_id": ["raw"]}
    check("POST /batches 能命中 create_batch*",
          auto._pick_func("/api/v1/batches", _sigs, "POST", src="").startswith("create_batch"),
          "batches→batche 导致末段命中全灭")
    check("POST /batches/{id}/run 能命中 run_batch_endpoint",
          auto._pick_func("/api/v1/batches/{batch_id}/run",
                          {"run_batch_endpoint": ["batch_id"]}, "POST", src="")
          == "run_batch_endpoint",
          "参数段前的资源段应参与命中")
    check("POST /datasets/seed 仍能命中 seed_endpoint",
          auto._pick_func("/api/v1/datasets/seed", _sigs, "POST", src="") == "seed_endpoint",
          "正常复数（-s）单数化不能被改坏")
    check("POST /preferences 命中 preference*",
          auto._pick_func("/api/v1/preferences",
                          {"submit_preference": ["student_id", "topic_id"]}, "POST", src="")
          == "submit_preference",
          "preferences→preference 应命中")

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（docstring 不误判 import / 入口不接线 / 空路由不产死代码 / "
          "*_endpoint 可识别 / 不硬塞 id / 自身无重复定义 / request 传 body）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
