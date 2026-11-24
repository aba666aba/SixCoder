"""路由接线不许自伤自检（2026-11-10）。

背景（毕设项目实测，覆盖卡在 17/84、每轮烧钱 ≈¥3）：
  ① 接线"无可接路由"时把该模块**整块路由清空** → 那个模块的所有端点当场 404
     → 观测全红 → 引擎当成"功能没写"→ 把工作单派回远端 → 下一轮又被清空。
     日志原文：「app/api/rules.py（无可接路由，清掉旧引擎段；跳过 1 条）」
  ② 入口 include 列表按"接线表 + 能力表"生成 → **漏掉远端自己注册的路由模块**
     （实测 app/api/datasets.py 有 router、注册了 POST /api/v1/datasets/seed，
      却不在 include 列表里）→ 8 条依赖 seed 造数据的能力一起 404
  ③ 入口 import 某个模块抛异常（NameError: name 'router'）→ **整个 app 起不来**
     → 84 条能力一起红

本自检钉住这三条。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import auto  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    import shutil
    import tempfile

    print("[1] ① 旧引擎段仍有效时不许清空（清空 = 凭空制造 404）")
    tmp = Path(tempfile.mkdtemp(prefix="six-routing-"))
    try:
        (tmp / "app").mkdir(parents=True, exist_ok=True)
        (tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
        mod = "app/api/rules.py"
        (tmp / mod).parent.mkdir(parents=True, exist_ok=True)
        (tmp / mod).write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "\n"
            "def create_prereq_rule(**kw):\n"
            "    return kw\n"
            "\n"
            "\n"
            + auto.WEB_ROUTE_BEGIN + "\n"
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "@router.post(\"/api/v1/rules\", response_model=None, status_code=201)\n"
            "def _engine_post_api_v1_rules(body: dict = Body(default=None)):\n"
            "    return create_prereq_rule()\n"
            + auto.WEB_ROUTE_END + "\n",
            encoding="utf-8")
        # 本轮**接不上**任何路由（异名场景：端点是 /rules/preview，函数叫 preview_rules）
        res = auto.ensure_web_router(tmp, mod, [], {}, full=True)
        src = (tmp / mod).read_text(encoding="utf-8")
        check("旧路由被保留（没被清空）",
              "@router.post(\"/api/v1/rules\"" in src, res.get("why", ""))
        check("保留时说明写明是'旧路由仍有效'",
              "仍有效" in str(res.get("why") or ""), str(res.get("why")))

        print("[1b] 旧段指向的函数被作者删了 → 才允许清掉（否则请求时 NameError）")
        (tmp / mod).write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "\n"
            + auto.WEB_ROUTE_BEGIN + "\n"
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "@router.post(\"/api/v1/rules\", response_model=None, status_code=201)\n"
            "def _engine_post_api_v1_rules(body: dict = Body(default=None)):\n"
            "    return create_prereq_rule()\n"
            + auto.WEB_ROUTE_END + "\n",
            encoding="utf-8")
        res2 = auto.ensure_web_router(tmp, mod, [], {}, full=True)
        src2 = (tmp / mod).read_text(encoding="utf-8")
        check("失效的旧段被清掉",
              "@router.post(\"/api/v1/rules\"" not in src2, res2.get("why", ""))

        print("[2] ② 远端自己注册路由的模块必须被 include")
        (tmp / "app" / "api").mkdir(parents=True, exist_ok=True)
        (tmp / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
        (tmp / "app" / "api" / "datasets.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "\n"
            "@router.post('/api/v1/datasets/seed')\n"
            "def seed():\n"
            "    return {'ok': True}\n",
            encoding="utf-8")
        (tmp / "app" / "services").mkdir(parents=True, exist_ok=True)
        (tmp / "app" / "services" / "plain.py").write_text(
            "def helper():\n    return 1\n", encoding="utf-8")

        class _Cap:
            def __init__(self):
                self.probe = {"kind": "http"}
                self.modules = []          # 能力表**没点名** datasets

        class _BP:
            caps = [_Cap()]

        r = auto.write_web_entry(tmp, _BP())
        mods = r.get("modules") or []
        check("app.api.datasets 被 include", "app.api.datasets" in mods, str(mods))
        check("没有 router 的业务模块不被 include",
              "app.services.plain" not in mods, str(mods))
        entry = (tmp / "app" / "main.py").read_text(encoding="utf-8")
        check("入口里确实写了 app.api.datasets", "app.api.datasets" in entry, "")

        print("[3] ③ 单个模块 import 失败不许炸掉整个 app")
        (tmp / "app" / "broken.py").write_text(
            "router = undefined_name\n", encoding="utf-8")
        entry2 = auto.WEB_ENTRY.format(
            modules=["app.broken", "app.api.datasets"])
        compile(entry2, "<entry>", "exec")
        check("入口模板对 import 做了兜底",
              "except Exception" in entry2 and "import_module" in entry2, "")
        # 真跑一遍：坏模块存在时，good 模块的路由仍要能注册
        (tmp / "app" / "main.py").write_text(entry2, encoding="utf-8")
        import subprocess
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import app.main as m\n"
            # FastAPI 新版 include_router 产生的是 _IncludedRouter，
            # 真正注册的路径要看 openapi 的 paths（app.routes 里枚举不到）。
            "paths = list(m.app.openapi().get('paths', {}).keys())\n"
            "print('SEED_ROUTE_OK' if '/api/v1/datasets/seed' in paths else 'MISSING:'+str(paths))\n"
            % str(tmp))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, encoding="utf-8", errors="replace")
        check("坏模块不拖垮 app（好模块的路由照样注册）",
              "SEED_ROUTE_OK" in (out.stdout or ""),
              (out.stdout or "") + (out.stderr or "")[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
