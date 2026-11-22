"""探针期望值自洽自检（2026-11-10）。

背景（毕设项目实测）：端到端覆盖卡在 17/84，每轮还在烧钱。
真凶不是产品写错，而是**判据自相矛盾**：
  · cap.auth.login_token 链上注册 `u_cap_auth_login_token`，
    却断言 login / me 返回 `{"username":"stu1"}`
  · cap.org.direction_create POST `{"name":"人工智能","code":"AI"}`，
    却断言返回 `{"name":"计算机学院","code":"CS"}`（隔壁 colleges 步骤的值）

这类探针**永远不可能通过**。引擎把"修产品"的工作单一遍遍派给远端，
远端怎么改都过不了 —— 覆盖停滞，钱照烧。

本自检钉住 `align_probe_expectations` 的三条规则：
  1. 步骤自己的请求体里出现过的同名字段 → 断言以它为准
  2. 同路径创建步骤里的同名字段 → 断言以它为准
  3. 账号身份字段（username/email）在链上无本步来源时 → 取链上注册值
  以及一条"不许乱改"的负向规则：业务常量断言（如 {"code":"unauthorized"}）不许动。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as bp  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def cap(cid, steps):
    return bp.Cap(id=cid, desc="x", funcs_est=1,
                  probe={"kind": "http", "steps": steps})


def main() -> int:
    print("[1] 实测事故复现：注册 u_xxx 却断言 stu1")
    c = cap("cap.auth.login_token", [
        {"method": "POST", "path": "/api/v1/auth/register",
         "json": {"username": "u_cap_auth_login_token", "password": "p1",
                  "role": "student"},
         "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/login",
         "json": {"username": "u_cap_auth_login_token", "password": "p1"},
         "expect_status": 200,
         "expect_json": {"username": "stu1", "role": "student"}},
        {"method": "GET", "path": "/api/v1/auth/me",
         "expect_status": 200,
         "expect_json": {"username": "stu1", "role": "student"}},
    ])
    b = bp.Blueprint(goal="t", caps=[c])
    n = bp.align_probe_expectations(b)
    ej2 = c.probe["steps"][1]["expect_json"]
    ej3 = c.probe["steps"][2]["expect_json"]
    check("登录步骤断言被修正", n >= 1, f"n={n}")
    check("login 断言 username 对齐注册值",
          ej2.get("username") == "u_cap_auth_login_token", str(ej2))
    check("me 断言 username 对齐注册值（跨步骤身份字段）",
          ej3.get("username") == "u_cap_auth_login_token", str(ej3))
    check("role 不被动（它在本步请求体里是对的值）",
          ej2.get("role") == "student", str(ej2))

    print("[2] 实测事故复现：POST 人工智能 却断言 计算机学院")
    c2 = cap("cap.org.direction_create", [
        {"method": "POST", "path": "/api/v1/org/colleges",
         "json": {"name": "计算机学院", "code": "CS"},
         "expect_status": 201, "expect_json": {"code": "CS"}},
        {"method": "POST", "path": "/api/v1/org/directions",
         "json": {"name": "人工智能", "code": "AI"},
         "expect_status": 201,
         "expect_json": {"name": "计算机学院", "code": "CS"}},
    ])
    b2 = bp.Blueprint(goal="t", caps=[c2])
    bp.align_probe_expectations(b2)
    ej = c2.probe["steps"][1]["expect_json"]
    check("方向创建的断言以自己的请求体为准",
          ej.get("name") == "人工智能" and ej.get("code") == "AI", str(ej))
    check("上一条 colleges 步骤的断言不被误改",
          c2.probe["steps"][0]["expect_json"].get("code") == "CS",
          str(c2.probe["steps"][0]["expect_json"]))

    print("[3] 负向：业务常量断言不许乱改")
    c3 = cap("cap.auth.unauthorized", [
        {"method": "GET", "path": "/api/v1/auth/me",
         "expect_status": 401, "expect_json": {"code": "unauthorized"}},
    ])
    b3 = bp.Blueprint(goal="t", caps=[c3])
    n3 = bp.align_probe_expectations(b3)
    check("常量断言零改动", n3 == 0 and
          c3.probe["steps"][0]["expect_json"].get("code") == "unauthorized",
          f"n={n3}")

    print("[4] 幂等：重复调用不产生新改动")
    c4 = cap("cap.auth.login_token", [
        {"method": "POST", "path": "/api/v1/auth/register",
         "json": {"username": "u_x", "password": "p1", "role": "student"},
         "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/login",
         "json": {"username": "u_x", "password": "p1"},
         "expect_status": 200, "expect_json": {"username": "zzz"}},
    ])
    b4 = bp.Blueprint(goal="t", caps=[c4])
    first = bp.align_probe_expectations(b4)
    second = bp.align_probe_expectations(b4)
    check("第一次有改动、第二次为零", first >= 1 and second == 0,
          f"first={first} second={second}")

    print("[5] 数字/布尔不许动（数字错是真 bug）")
    c5 = cap("cap.x", [
        {"method": "POST", "path": "/api/v1/xx",
         "json": {"name": "a", "count": 3},
         "expect_status": 201, "expect_json": {"count": 99}},
    ])
    b5 = bp.Blueprint(goal="t", caps=[c5])
    n5 = bp.align_probe_expectations(b5)
    check("数字断言保持原值",
          n5 == 0 and c5.probe["steps"][0]["expect_json"].get("count") == 99,
          f"n={n5}")

    print("[6] 自洽化必须挂在 coverage.run 这一层（全自主线才覆盖得到）")
    # 实测 2026-11-11：自洽化原本只挂在 `openhouse.observe` 上，
    # 而**全自主模式**（auto.night_auto，看门狗固定用 --goal-file）
    # 直接调 `coverage.run`、**绕过 openhouse** → 那个修复在全自主线根本不生效。
    # 看门狗跑的就是全自主线 → 修了个寂寞。
    import inspect as _inspect
    from core import coverage as _cov
    _src = _inspect.getsource(_cov.run)
    check("coverage.run 里调了 align_probe_expectations",
          "align_probe_expectations" in _src,
          "全自主模式绕过 openhouse 时就白改了")
    from core import openhouse as _oh
    _src2 = _inspect.getsource(_oh.observe)
    check("openhouse.observe 里也调了（甲方模式那一路）",
          "align_probe_expectations" in _src2, "")

    print("[7] json 写成 JSON 字符串的步骤必须归一化（2026-11-19 血泪）")
    # 实测：部分 register/login 的 json 是 `"{\"username\":...}"`（字符串）而非 dict，
    # probe.fill 原样发出 → 422「用户名不能为空」→ 后续 403/404 连锁假 bug。
    c7 = cap("cap.inv.candidate.guard_x", [
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": '{"username": "u_guard_x", "password": "p1", "role": "teacher"}',
         "expect_status": 201},
    ])
    b7 = bp.Blueprint(goal="t", caps=[c7])
    bp.normalize_http(b7)
    body7 = c7.probe["steps"][0]["json"]
    check("蓝图加载时字符串 json 被解析为对象",
          isinstance(body7, dict) and body7.get("username") == "u_guard_x",
          str(body7))
    # 运行时兜底：不经过 normalize_http 的存量探针，runner 里的 fill 也要能解析。
    # fill 定义在 _HTTP_RUNNER（子进程脚本文本）里，不是模块级函数 —— 从这里抽出来真执行。
    from core import probe as _probe
    _ns: dict = {"json": json, "VAR": {}}
    _runner_src = _probe._HTTP_RUNNER
    _fill_start = _runner_src.index("def fill(v):")
    _fill_end = _runner_src.index("def pick(obj, path):")
    exec(_runner_src[_fill_start:_fill_end], _ns)         # noqa: S102 —— 执行引擎自己的 runner 片段
    _fill = _ns["fill"]
    filled7 = _fill('{"username": "u_x", "password": "p1"}')
    check("运行时 fill 把字符串 json 解析为对象",
          isinstance(filled7, dict) and filled7.get("username") == "u_x",
          str(filled7))
    check("纯文本串不被误当 json 解析",
          _fill("just a plain string") == "just a plain string")
    # 占位符在解析后的对象里仍能被替换
    _ns["VAR"] = {"uid": "u_sub"}
    check("字符串 json 解析后占位符仍能替换",
          _fill('{"username": "${uid}"}').get("username") == "u_sub",
          str(_fill('{"username": "${uid}"}')))
    # 账号统一能覆盖归一化后的步骤（字符串时代会被整段跳过）
    c7b = cap("cap.inv.candidate.guard_y", [
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": '{"username": "u_old", "password": "p1"}', "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": '{"username": "u_old", "password": "p1"}', "expect_status": 200},
    ])
    b7b = bp.Blueprint(goal="t", caps=[c7b])
    bp.normalize_http(b7b)
    users7b = {s["json"].get("username") for s in c7b.probe["steps"][:2]
               if isinstance(s.get("json"), dict)}
    check("归一化后账号统一能作用到（串味被消除）",
          len(users7b) == 1, str(users7b))

    print("[8] 鉴权顺序修复：建资源排在 login 之前是**判据错**，不是产物错（2026-11-19）")
    # 实测 cap.inv.topic.guard_pending_1751：register → POST /topics/（期望 201、无鉴权头）
    # → login → approve。建题排在登录前，产物只能回 403，后续连带 404——这种探针
    # **结构上不可能通过**，远端却反复去改本来正确的权限逻辑。
    from core import probe as _pb
    _bad = {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u_g", "password": "p1", "role": "admin"}, "expect_status": [201]},
        {"method": "POST", "path": "/api/v1/topics/",
         "json": {"title": "T", "capacity": 2}, "expect_status": [200, 201]},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": '{"username": "u_g", "password": "p1"}',
         "expect_status": [200], "save": '{"token": "token"}'},
        {"method": "POST", "path": "/api/v1/topics/${topic_id}/approve/",
         "json": {}, "expect_status": [400, 403, 409, 422]},
    ]}
    _fixed = _pb._repair_auth_order(_bad)
    _paths = [s.get("path") for s in _fixed["steps"]]
    check("login 被提到建题之前",
          _paths[0].endswith("/register/") and _paths[1].endswith("/login/")
          and _paths[2] == "/api/v1/topics/", str(_paths))
    _tk = (_fixed["steps"][2].get("headers") or {}).get("Authorization")
    check("建题步骤补上了 Bearer ${token}（save 是字符串也能认）",
          _tk == "Bearer ${token}", str(_tk))
    check("register 不被硬塞 token（认证步骤本身不带鉴权）",
          not ((_fixed["steps"][0].get("headers") or {}).get("Authorization")))
    # 已通过的能力不能被改（返回同一对象）
    _ok_probe = {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "u", "password": "p"}, "expect_status": [200],
         "save": {"token": "token"}},
        {"method": "POST", "path": "/api/v1/topics/",
         "json": {"title": "T"}, "headers": {"Authorization": "Bearer ${token}"},
         "expect_status": [201]},
    ]}
    check("本来就正确的探针原样返回（对已绿能力零影响）",
          _pb._repair_auth_order(_ok_probe) is _ok_probe)
    # 多条 login 时角色不确定 → 宁可不动
    _two = {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "T"},
         "expect_status": [201]},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "a", "password": "p"}, "save": {"ta": "token"}},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "b", "password": "p"}, "save": {"tb": "token"}},
    ]}
    check("多条 login 时不猜角色，原样返回", _pb._repair_auth_order(_two) is _two)
    # 接线钉子：函数存在 ≠ 真实调用路径会走到（本项目反复踩过的病）
    _src_pb = (Path(__file__).resolve().parents[1] / "core" / "probe.py") \
        .read_text(encoding="utf-8")
    check("run() 的 http 分支真调用它（不是死函数）",
          "_repair_auth_order(_dedupe_http_probe(probe))" in _src_pb)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
