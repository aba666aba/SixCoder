"""三个硬编码耦合点修复的断言（2026-11-08 专项重构）。

用户点名的三个耦合点：
  ① 入口路径写死 app/main.py（12+ 处）→ core/entrypoint.py 探测，判据端全走它；
  ② 分层"猜层"：治理层名与目录名的子串巧合，对不上就静默豁免 → arch._layer_map
     三级匹配（名字 → 目录共识 → import 锚定），定不了如实声明；
  ③ 路由→函数全靠语义猜（*_endpoint 命名兜底）→ _route_handler_from_src 先读
     产物装饰器注册的权威映射，猜只是兜底。

跑法:  python checks/check_coupling_fix.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import arch, auto, entrypoint, gates  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _mkws(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="coupling_"))
    for rel, src in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return ws


def check_entrypoint() -> None:
    print("[1] 入口探测")
    ws = _mkws({"app/main.py": "x = 1\n", "main.py": "m = 0\n", "other.py": "y = 2\n"})
    check("app/main.py 优先命中", entrypoint.entry_rel(ws) == "app/main.py")
    (ws / "app" / "main.py").unlink()
    check("回落 main.py", entrypoint.entry_rel(ws) == "main.py")
    (ws / "main.py").unlink()
    (ws / "src").mkdir(exist_ok=True)
    (ws / "src" / "main.py").write_text("z = 3\n", encoding="utf-8")
    check("回落 src/main.py", entrypoint.entry_rel(ws) == "src/main.py")
    check("is_entry 兼容写死口径", entrypoint.is_entry("app/main.py", ws))
    check("is_entry 认探测入口", entrypoint.is_entry("src/main.py", ws))
    check("is_entry 拒绝非入口", not entrypoint.is_entry("other.py", ws))
    shutil.rmtree(ws, ignore_errors=True)
    # gate_import_smoke 对非 app/main.py 形态也能给出真冒烟（而不是"不适用"）
    ws2 = _mkws({"main.py": "VALUE = 41 + 1\n"})
    res = gates.gate_import_smoke(ws2)
    check("非 app/main.py 形态冒烟真跑", res.ok and "import main" in (res.detail or ""),
          res.detail)
    shutil.rmtree(ws2, ignore_errors=True)


def check_layer_map() -> None:
    print("[2] 分层定层")
    layers = ["Models", "Repo", "Api"]
    ws = _mkws({
        ".sixcoder/governance/architecture.md": "Models → Repo → Api\n",
        # 名字匹配（快路径）：db/ 目录命中 Models（/models./ 或 stem）
        "db/models.py": "class Row: pass\n",
        "db/helper.py": "from db.models import Row\nimport repositories.x\n"
                        "def h():\n    return Row\n",
        # 目录名与治理名完全对不上（Repo vs repositories）——名字匹配必失败
        "repositories/x.py": "from db.models import Row\n\ndef get():\n    return Row\n",
        "api/routes.py": "from db.helper import h\n\ndef r():\n    return h\n",
    })
    files = sorted((ws / d).glob("*.py") for d in ("db", "repositories", "api")
                   for d2 in [d] if d2.exists()) if False else \
        [p for p in ws.rglob("*.py") if "__pycache__" not in str(p)]
    m = arch._layer_map(ws, layers, files)
    check("名字匹配命中 Models", m.get("db/models.py") == 0, str(m))
    # repositories/x.py：名字匹配失败 → import 锚定（deps min=0，users max=0）→ 判 0
    check("import 锚定补上猜不中的层", m.get("repositories/x.py") == 0, str(m))
    check("名字匹配命中 Api", m.get("api/routes.py") == 2, str(m))
    # gate_layers 集成：反向依赖要被抓（db/models import api → 低 import 高）
    (ws / "db" / "models.py").write_text("from api.routes import r\n", encoding="utf-8")
    res = arch.gate_layers(ws, "M")
    check("反向依赖仍被抓", not res.ok, res.detail)
    shutil.rmtree(ws, ignore_errors=True)
    # 全对不上时如实声明（不再静默豁免）
    ws2 = _mkws({".sixcoder/governance/architecture.md": "Models → Repo → Api\n",
                 "mystery/a.py": "def f():\n    return 1\n",
                 "mystery/b.py": "def g():\n    return 2\n"})
    res2 = arch.gate_layers(ws2, "M")
    check("全部定不了层时如实声明", ("定不了层" in (res2.detail or "")), res2.detail)
    shutil.rmtree(ws2, ignore_errors=True)


def check_route_handler() -> None:
    print("[3] 路由→函数实测")
    src = ('from fastapi import APIRouter\n'
           'router = APIRouter()\n'
           '@router.post("/batches/{id}/run")\n'
           'def run_batch(batch_id: int) -> dict:\n'
           '    return {"ran": batch_id}\n'
           '@router.get("/batches")\n'
           'def list_batches() -> dict:\n'
           '    return {"items": []}\n')
    # 前缀容忍 + 参数名归一：探针路径带 /api/v1、参数叫 batch_id
    got = auto._route_handler_from_src(src, "POST", "/api/v1/batches/{batch_id}/run")
    check("装饰器注册的权威函数名", got == "run_batch", got)
    got2 = auto._route_handler_from_src(src, "GET", "/api/v1/batches")
    check("GET 也实测命中", got2 == "list_batches", got2)
    check("方法不对不命中", auto._route_handler_from_src(src, "DELETE", "/batches") == "")
    # 无装饰器注册 → 落回语义猜（旧行为不回归）。funcs 形态 = {函数名: [参数名]}。
    got3 = auto._pick_func("/api/v1/tickets", {"create_ticket": ["payload"]}, "POST")
    check("无注册时语义猜兜底", got3 == "create_ticket", got3)
    # 有注册时实测压过语义猜（作者命名完全不带动词/后缀也不怕）
    src2 = ('router = APIRouter()\n'
            '@router.post("/org/terms")\n'
            'def add_term(payload: dict) -> dict:\n'
            '    return {"ok": True}\n')
    got4 = auto._pick_func("/api/v1/org/terms",
                           {"app/api/org.py": ["add_term", "normalize_term_name"]},
                           "POST", src=src2)
    check("实测压过语义猜（无后缀命名也接上）", got4 == "add_term", got4)


def check_mark_keeps_done() -> None:
    print("[4] 覆盖观测忠实写回（回归留痕 + 派单链路不断）")
    from core import coverage as covmod
    from core import blueprint as bpmod
    bp_dict = {
        "goal": "t", "version": 1,
        "caps": [
            {"id": "cap.a", "title": "a", "status": "done"},
            {"id": "cap.b", "title": "b", "status": "absent"},
            {"id": "cap.c", "title": "c", "status": "descoped"},
        ],
    }
    bp = bpmod.Blueprint.from_dict(bp_dict)
    # cap.a 探针挂（真回归或中间态）：status 忠实降 absent（派单能看见它），
    # why 留回归标记（人/巡检可见），descoped 保留。
    cov = {"caps": {"cap.a": {"status": "absent", "detail": "探针 500"},
                    "cap.b": {"status": "done", "detail": ""},
                    "cap.c": {"status": "absent", "detail": "x"}}}
    out = covmod.mark(bp, cov)
    ma = out.by_id("cap.a")
    mb = out.by_id("cap.b")
    mc = out.by_id("cap.c")
    check("status 忠实于观测（降 absent，进 pending 可派单）", ma.status == "absent",
          ma.status)
    check("回归留痕在 why", "探针回归待复核" in (ma.why or ""), ma.why)
    check("回归清单可读", getattr(out, "regressed_now", None) == ["cap.a"])
    check("absent 照常升 done", mb.status == "done")
    check("descoped 保留语义不变", mc.status == "descoped")
    # cap.a 探针恢复：升回 done，why 清空
    cov2 = {"caps": {"cap.a": {"status": "done", "detail": ""}}}
    out2 = covmod.mark(out, cov2)
    check("探针恢复后升回 done 且 why 清空",
          out2.by_id("cap.a").status == "done" and not (out2.by_id("cap.a").why or ""))


def check_scc_no_false_positive() -> None:
    print("[5] 环检测（SCC）：到达环的节点不算环成员")
    from core import arch as archmod
    # 构图：A→B→C→B（B、C 真环），A 只是能到达环 —— 旧 DFS 会把 A 误报成环成员
    graph = {"m.a": {"m.b"}, "m.b": {"m.c"}, "m.c": {"m.b"},
             "m.d": {"m.e"}, "m.e": set()}
    comps = archmod._sccs(graph)
    members = sorted(m for comp in comps for m in comp)
    check("真环成员=B/C", members == ["m.b", "m.c"], str(members))
    check("到达环的 A 不被误报", "m.a" not in members, str(members))
    # 自环也算环
    comps2 = archmod._sccs({"m.x": {"m.x"}})
    check("自环被识别", comps2 == [["m.x"]], str(comps2))


def check_mod_to_rel() -> None:
    print("[6] 修复片 mods 形态归一（点分/斜杠不再混存）")
    ws = _mkws({"app/models.py": "x = 1\n", "pkg/__init__.py": "",
                "pkg/mod.py": "y = 2\n", "plain.py": "z = 3\n"})
    check("点分转斜杠", auto._mod_to_rel(ws, "app.models") == "app/models.py")
    check("包名转 __init__", auto._mod_to_rel(ws, "pkg") == "pkg/__init__.py")
    check("子模块转斜杠", auto._mod_to_rel(ws, "pkg.mod") == "pkg/mod.py")
    check("已是斜杠路径原样", auto._mod_to_rel(ws, "app/models.py") == "app/models.py")
    check("落不到的原样返回（不瞎猜）", auto._mod_to_rel(ws, "nope.mod") == "nope.mod")
    shutil.rmtree(ws, ignore_errors=True)


def check_e2e_chain_and_cross_dupes() -> None:
    print("[6] 端到端链渲染 + 跨文件同名语义重复 + 404 报告保真")
    # ① _render_e2e_chain：save 依赖与 expect_json 值字面量必须可见
    probes = [{"steps": [
        {"method": "POST", "path": "/api/v1/auth/register", "expect_status": 201,
         "save": {"atoken": "token"}},
        {"method": "POST", "path": "/api/v1/datasets/seed", "expect_status": 201,
         "expect_json": {"dataset_version": "small_2x2-v1", "students": 2, "topics": 2},
         "save": {"batch_id": "batch_id"}},
        {"method": "POST", "path": "/api/v1/batches/${batch_id}/run", "expect_status": 200,
         "expect_json": {"status": "created", "matched": 2, "blocking_pairs": 0}},
    ]}]
    chain = "\n".join(auto._render_e2e_chain(probes))
    for key in ("batch_id", "响应里必须有字段", "status='created'", "blocking_pairs=0",
                "同一条链"):
        if key not in chain:
            print(f"  FAIL 端到端链渲染缺: {key}")
            FAILS.append(f"端到端链渲染缺 {key}")
            return
    print("  OK   save 依赖/值字面量/链路提示全部渲染")
    # ② gate_cross_dupes：跨文件同名不同体 → blocked；同体 → 提示不阻塞
    ws = _mkws({
        "app/services/batches.py": "def run_batch_endpoint(batch_id: str) -> dict:\n"
                                   "    return {'a': 1}\n",
        "matching_core/da.py": "def run_batch_endpoint(batch_id: str) -> dict:\n"
                               "    if '-' not in batch_id:\n"
                               "        raise ValueError('x')\n"
                               "    return {'b': 2}\n",
    })
    res = gates.gate_cross_dupes(ws, ["app/services/batches.py", "matching_core/da.py"])
    check("跨文件同名不同体被阻塞", not res.ok, res.detail)
    shutil.rmtree(ws, ignore_errors=True)
    ws2 = _mkws({
        "a.py": "def f(x):\n    return x + 1\n",
        "b.py": "def f(x):\n    return x + 1\n",
    })
    res2 = gates.gate_cross_dupes(ws2, ["a.py", "b.py"])
    check("跨文件同体复制不阻塞只提示", res2.ok and "复制" in (res2.detail or ""), res2.detail)
    shutil.rmtree(ws2, ignore_errors=True)
    # ③ 探针 404 报告保真（不真发请求，直接验证文案分支的语义：404 → 提"路由未命中"）
    from core import probe as probemod
    src = Path(probemod.__file__).read_text(encoding="utf-8")
    check("404 报告保真已实装", "404 路由未命中" in src and "_route_miss" in src)


def main() -> int:
    check_entrypoint()
    check_layer_map()
    check_route_handler()
    check_mark_keeps_done()
    check_scc_no_false_positive()
    check_mod_to_rel()
    check_e2e_chain_and_cross_dupes()
    if FAILS:
        print("有失败项: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
