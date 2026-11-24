"""钉住「契约点名 → 接线同源」这条链（2026-11-11，§11.4g）。

背景：覆盖停滞的**最后一道结构性原因** ——
「端点 → 处理函数 → 签名」在系统里有 3 份独立副本（能力表探针 / interfaces.json /
模块契约 / 切片契约），没有一处是权威，于是：

- 契约点名 `teachers_me`，产物写 `teacher_me` → 契约检查永远判"缺函数"；
- `GET /api/v1/people/teachers/me` 被**猜**接到 `app/analytics.py` 的负载建议函数
  → 返回 `[{'teacher':'','quota':0,'assigned':0,'load_ratio':0.0}]` → 端到端永远红；
- 同一个路径在两个模块各注册一份 → FastAPI 里先 include 的抢先注册
  → 探针打到猜错的那份。

本文件钉住四件事：**点名表能解析 / 接线按点名选落点 / 重复注册被摘 /
参数判据只有一套实现**。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import auto, contract, routefuncs, slicer  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 契约点名表：能从契约文本解析出「端点 → 函数」")
    txt = ("1) app/services/people.py：教师档案服务层\n"
           "   - get_teacher_me(authorization: str) -> dict："
           "GET /api/v1/people/teachers/me 服务层：返回教师档案\n"
           "   - create_topic(body: dict, authorization: str) -> dict："
           "POST /api/v1/topics 服务层入口：创建草稿\n")
    rf = routefuncs.parse(txt)
    check("解析出 teachers/me → get_teacher_me",
          rf.get("/api/v1/people/teachers/me") == "get_teacher_me", str(rf))
    check("解析出 /api/v1/topics → create_topic",
          rf.get("/api/v1/topics") == "create_topic", str(rf))

    print("[2] 路径归一：契约的 {topic_id} 与探针的 999999 落到同一个键")
    check("数字段归一",
          routefuncs.normalize("POST /api/v1/topics/999999")
          == routefuncs.normalize("/api/v1/topics/{topic_id}"),
          routefuncs.normalize("POST /api/v1/topics/999999"))
    check("方法前缀与尾斜杠被去掉",
          routefuncs.normalize("post /api/v1/topics/") == "/api/v1/topics",
          routefuncs.normalize("post /api/v1/topics/"))

    print("[3] 函数名归一：同一函数的两种写法能对上")
    for a, b in (("teachers_me", "teacher_me"),
                 ("get_teachers_me", "teachers_me"),
                 ("create_topic_route", "create_topic")):
        check("%s ≈ %s" % (a, b),
              routefuncs._norm_name(a) == routefuncs._norm_name(b),
              "%s vs %s" % (routefuncs._norm_name(a), routefuncs._norm_name(b)))

    print("[4] 契约对象自带路由信息（写码/体检/接线同源）")
    check("FuncSig 有 route 字段",
          hasattr(contract.FuncSig, "route") or "route" in contract.FuncSig.__dataclass_fields__,
          str(getattr(contract.FuncSig, "__dataclass_fields__", {}).keys()))
    cs = contract.parse_requirement(txt)
    got = [(c.path, f.name, f.route) for c in cs.values() for f in c.funcs if f.route]
    check("解析后契约里的函数带上了端点", bool(got), str(cs.keys()))

    print("[5] 参数判据只有一套实现（_wire_call）")
    a_src = inspect.getsource(auto)
    check("ensure_web_router 调 _wire_call", "_wire_call(" in a_src)
    check("_wire_call 是模块级函数（可被 _locate_route 复用）",
          hasattr(auto, "_wire_call"))
    check("_locate_route 按参数可接筛选候选",
          "_wire_call" in inspect.getsource(auto._locate_route),
          "选落点与渲染用两套判据 → 会挑中参数接不上的函数 → 整条路由 404")

    print("[6] 接线：点名优先 + 重复注册被摘")
    check("_locate_route 有 prefer / prefer_mod 参数",
          "prefer_mod" in inspect.signature(auto._locate_route).parameters,
          str(inspect.signature(auto._locate_route)))
    check("apply_web_wiring 有 route_funcs 参数",
          "route_funcs" in inspect.signature(auto.apply_web_wiring).parameters)
    check("有 _authored_routes（AST 全扫，不漏 add_api_route 之外的写法）",
          hasattr(auto, "_authored_routes"))
    check("有 _strip_engine_routes（引擎段里的重复注册也要摘）",
          hasattr(auto, "_strip_engine_routes"))

    print("[7] FastAPI 声明式参数：Body/Form 必须传（2026-11-11 的 422 根因）")
    head = ("def login_account(body: dict = Body(default=None),"
            " authorization: str = Header(default=\"\")):\n"
            "    pass\n")
    fb, fo = auto._fastapi_decl_params(head, "login_account")
    check("body 被识别为声明式请求体", "body" in fb, str(fb))
    check("authorization 被识别为声明式头", "authorization" in fo, str(fo))
    fns = {"login_account": ["body", "authorization"]}
    a, bad = auto._wire_call(head, "login_account", "/api/v1/auth/login",
                             {"username", "password"}, fns, "POST")
    check("引擎把 body 传给业务函数（不是丢掉）",
          any("body=_b" in x for x in a), str(a))
    check("声明式 body 不算业务默认值（不省略）",
          not bad, str(bad))
    # 业务默认值（字面量）仍然可以省略（11-10 的口径不许改）
    head2 = "def f(x: int = 3):\n    pass\n"
    a2, bad2 = auto._wire_call(head2, "f", "/api/v1/zzz", set(),
                               {"f": ["x"]}, "GET")
    check("业务默认值仍可省略（不塞假值）", not bad2 and not a2, str((a2, bad2)))

    print("[8] 契约文本必须带**通用约束段**（2026-11-15 改：只讲领域边界，不讲框架形态）")
    check("有 _wire_constraints", hasattr(auto, "_wire_constraints"))
    try:
        c1 = auto._wire_constraints("1) app/x.py：\n   - f(a: int) -> int：说明\n")
        check("约束段写进契约文本", "契约约束（引擎要求" in c1, c1[-160:])
        check("要求凭据参数叫 authorization", "authorization" in c1)
        check("说清错误口径（ValueError，不返回状态码）",
              "ValueError" in c1 and "HTTPException" in c1)
        check("说清边界（不 import Web 框架 / 只写业务）",
              "import 任何 Web 框架" in c1 and "只写**业务实现**" in c1)
        c2 = auto._wire_constraints(c1)
        check("幂等（不重复追加）", c2.count("契约约束（引擎要求") == 1,
              str(c2.count("契约约束（引擎要求")))
    except Exception as e:
        check("_wire_constraints 可调用", False, "%s: %s" % (type(e).__name__, e))
    _sl = inspect.getsource(slicer)
    check("简报里也说了凭据参数形态（契约合成时就别写错）",
          "authorization" in _sl and "凭据参数" in _sl,
          "只在写码时说 → 契约已经合成错了签名")

    print("[9] 契约解析要认两种格式 + 候选集要含业务层子目录（2026-11-11 19:20 最后一环）")
    # 格式②：路径在模块段、签名行只管签名
    fmt2 = ("1) app/services/topics.py：POST /api/v1/topics 的服务层入口：创建草稿\n"
            "   - create_topics(title: str, quota: int, authorization: str) -> dict："
            "创建并回显：status 固定 draft\n")
    m2 = routefuncs.parse(fmt2)
    check("新格式（路径在模块段）能解析",
          m2.get("/api/v1/topics") == "create_topics", str(m2))
    fmt1 = ("1) app/x.py：\n"
            "   - create_topic(body: dict, authorization: str) -> dict："
            "POST /api/v1/topics 服务层入口：…\n")
    m1 = routefuncs.parse(fmt1)
    check("旧格式（签名行自带路径）仍能解析",
          m1.get("/api/v1/topics") == "create_topic", str(m1))
    check("候选集递归包含子目录（app/services/*）",
          "rglob" in inspect.getsource(auto._candidate_modules),
          "只扫一层 → 业务层 app/services/*.py 不在候选里 → 点名函数永远接不上")
    check("_locate_route 优先用契约点名的函数",
          inspect.getsource(auto._locate_route).find("if prefer:") <
          inspect.getsource(auto._locate_route).find("if prefer_mod:"),
          "先按名字挑 → 旧的短名函数抢到路由（实测 source 永远 None）")

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
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
