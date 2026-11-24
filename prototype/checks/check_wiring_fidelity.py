"""接线不许"造坏数据"自检（2026-11-10）。

背景（毕设项目实测）：远端架构师在第 2 轮当场给出根因：
  「引擎生成的路由接线把业务函数入参写死成空串/示例常量
    （如 publish_topic(current_status="", students="", …））」

后果：路由"注册成功"、HTTP 也通，但**语义完全是另一件事** ——
当前状态永远是空串、学生列表永远是空 → 业务判断全错 → 能力永远红，
而引擎看不出真相，只能一轮轮派工、轮轮烧钱。

另外两条同源问题：
  · 作者用了 `@router.xxx` 却没定义 router → 模块 import 就炸
    `NameError: name 'router' is not defined` → 远端为一行样板返工三轮，
    整轮接线还回滚 32 个文件（实测 2026-11-10）
  · 接线把入参写死成空串 → 同一类"假绿"

本自检钉住：
  1. 函数参数有默认值 → 接线**不传**（让它用自己的默认），不许塞空串
  2. 函数参数没有默认值、body 里也没有 → 这条路由**不接**（宁可 404）
  3. 作者用了 @router 却没定义 router → 引擎补 `router = APIRouter()`
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

    tmp = Path(tempfile.mkdtemp(prefix="six-wiring-"))
    try:
        (tmp / "app").mkdir(parents=True, exist_ok=True)
        (tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
        mod = "app/topics.py"

        print("[1] 参数有默认值 → 不传，不许塞空串")
        (tmp / mod).write_text(
            "def publish_topic(title, quota, current_status=\"\", students=\"\"):\n"
            "    return {'title': title, 'quota': quota,\n"
            "            'status': current_status, 'students': students}\n",
            encoding="utf-8")
        routes = [{"method": "POST", "path": "/api/v1/topics",
                   "func": "publish_topic",
                   "body_keys": ["title", "quota"],
                   "body_required": ["title", "quota"],
                   "body_types": {"title": "str", "quota": "int"},
                   "body_sample": {"title": "t", "quota": 2}}]
        funcs = {"publish_topic": ["title", "quota", "current_status", "students"]}
        res = auto.ensure_web_router(tmp, mod, routes, funcs, full=True)
        src = (tmp / mod).read_text(encoding="utf-8")
        # 只看**引擎段**：作者区里 `current_status=""` 是函数自己的默认值定义，
        # 那是合法的，不该被当成"引擎塞的空串"。
        wire = src.split(auto.WEB_ROUTE_BEGIN, 1)[-1].split(auto.WEB_ROUTE_END, 1)[0]
        check("引擎段里没有把 current_status 写成空串",
              'current_status=""' not in wire, wire[-600:])
        check("引擎段里没有把 students 写成空串",
              'students=""' not in wire, wire[-600:])
        check("路由仍然接上了（不是靠砍路由蒙混）",
              '@router.post("/api/v1/topics"' in wire, res.get("why", ""))

        print("[2] 参数没默认值、body 里也没有 → 这条路由不接（宁可 404）")
        (tmp / mod).write_text(
            "def publish_topic(title, quota, mystery_field):\n"
            "    return {'title': title, 'quota': quota, 'm': mystery_field}\n",
            encoding="utf-8")
        funcs2 = {"publish_topic": ["title", "quota", "mystery_field"]}
        res2 = auto.ensure_web_router(tmp, mod, routes, funcs2, full=True)
        src2 = (tmp / mod).read_text(encoding="utf-8")
        check("接不上的路由没有被硬接进来",
              '@router.post("/api/v1/topics"' not in src2, src2[-400:])
        check("跳过原因写进结果里（可追溯）",
              "mystery_field" in str(res2.get("skipped") or []),
              str(res2.get("skipped")))

        print("[3] **kwargs 的函数 → 整个 body 交给它")
        (tmp / mod).write_text(
            "def publish_topic(**kw):\n"
            "    return kw\n",
            encoding="utf-8")
        funcs3 = {"publish_topic": ["title", "quota"]}
        res3 = auto.ensure_web_router(tmp, mod, routes, funcs3, full=True)
        src3 = (tmp / mod).read_text(encoding="utf-8")
        check("**kwargs 路线接上了", '@router.post("/api/v1/topics"' in src3,
              res3.get("why", ""))

        print("[4] 作者用了 @router 却没定义 router → 引擎补一行")
        broken = (
            "from fastapi import APIRouter\n"
            "\n"
            "\n"
            "@router.post('/api/v1/xxx')\n"
            "def xxx():\n"
            "    return {'ok': True}\n"
        )
        fixed, rfix = auto._ensure_router_decl(broken)
        check("检测出缺 router 声明", bool(rfix), str(rfix))
        check("补上了 router = APIRouter()",
              "router = APIRouter()" in fixed, fixed)
        check("补上了 from fastapi import APIRouter",
              "from fastapi import APIRouter" in fixed, fixed)
        try:
            compile(fixed, "<t>", "exec")
            ok = True
        except SyntaxError as exc:
            ok = False
            print("    语法错误：", exc)
        check("补完能编译", ok, "")

        print("[5] 已经定义过 router 的模块不许被重复插入")
        good = ("from fastapi import APIRouter\n"
                "router = APIRouter()\n"
                "\n"
                "@router.post('/api/v1/xxx')\n"
                "def xxx():\n"
                "    return {'ok': True}\n")
        fixed2, rfix2 = auto._ensure_router_decl(good)
        check("已定义则零改动", not rfix2 and fixed2 == good, str(rfix2))

        print("[6] 没用 router 的纯业务模块不许被塞 router")
        plain = "def helper():\n    return 1\n"
        fixed3, rfix3 = auto._ensure_router_decl(plain)
        check("纯业务模块零改动", not rfix3 and fixed3 == plain, str(rfix3))
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
