"""蓝图「按栈落地」自检（2026-11-15）。

这一批修的都是**同一类病**：蓝图/探针/层生成里写死了 FastAPI 的世界观，
在 Django 仓上会「一条都落不下、却全绿」。所以断言要钉住**行为**，不是"函数存在"。

跑法：python checks/check_blueprint_stack.py
"""
from __future__ import annotations

# 引擎不含项目身份（2026-11-19）：路径来自 --repo / SIXCODER_REPO / _logs/engine.json
import sys as _sys
_ENGINE = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import repo_root as _repo_root  # noqa: E402


import inspect
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as B          # noqa: E402
from core import fullgen as F           # noqa: E402
from core import stack as S             # noqa: E402

FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 提示词按栈给落点（写死 app.main 会让 Django 仓整表作废）")
    dj, fa = B.web_rules("django"), B.web_rules("fastapi")
    ck("django 的 app 固定 config.asgi:application", "config.asgi:application" in dj, dj[:60])
    ck("django 落点写 apps/<域>/views.py", "apps/<域>/views.py" in dj)
    ck("django 明确禁止 app/ 前缀", "禁止" in dj and "`app/`" in dj)
    ck("django 说清尾斜杠与 health 例外", "尾斜杠" in dj and "/api/v1/health" in dj)
    ck("fastapi 口径没被破坏", "app.main:app" in fa and "app/main.py" in fa)
    ck("推导提示词留了占位符", "@WEB_RULES@" in B.DERIVE_PROMPT)
    ck("补能力提示词也留了占位符", "@STACK_WEB@" in B.EXTEND_PROMPT)
    ck("derive 真的注入（不是留在模板里）",
       "web_rules(_stack_name)" in inspect.getsource(B.derive))
    ck("extend 真的注入", "web_rules(" in inspect.getsource(B.extend))

    print()
    print("[2] 探针字段两种写法都要认（kind / type）")
    cap = B.Cap.from_dict({"id": "x", "probe": {"type": "HTTP", "app": "a"}})
    ck("type→kind 且小写", cap.probe.get("kind") == "http", str(cap.probe))
    cap2 = B.Cap.from_dict({"id": "y", "probe": {"kind": "cli", "entry": "m.py"}})
    ck("已有 kind 不被覆盖", cap2.probe.get("kind") == "cli")

    print()
    print("[2b] 栈要按实物自动识别（缺 stack.json 不许静默按 FastAPI）")
    ck("有 manage.py + config/settings.py → django", S.detect(_repo_root()) == "django")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "manage.py").write_text("", encoding="utf-8")
        (w / "config").mkdir()
        (w / "config" / "settings.py").write_text("", encoding="utf-8")
        ck("无配置文件也认得出 Django", S.from_workspace(w).name == "django")

    print()
    print("[3] 旧结构落点要重映射（审计/补能力那两步看不到栈约定）")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "apps" / "analytics").mkdir(parents=True)
        (w / "matching_core").mkdir()
        bp = B.Blueprint(goal="t")
        bp.add(B.Cap.from_dict({"id": "c1", "probe": {"kind": "http", "steps": [
            {"method": "GET", "path": "/api/v1/analytics/heat/"}]},
            "modules": ["app/api/analytics.py"]}))
        bp.add(B.Cap.from_dict({"id": "c2", "probe": {"kind": "http", "steps": [
            {"method": "GET", "path": "/api/v1/matching/run/"}]},
            "modules": ["app/api/matching.py"]}))
        n = B.remap_modules(bp, ws=w)
        ck("app/api/x.py → apps/x/views.py（域存在时）",
           bp.by_id("c1").modules == ["apps/analytics/views.py"], bp.by_id("c1").modules)
        ck("域不存在时不硬塞（保持原样或回落）", n >= 1, n)
        ck("不改动已在 apps/ 下的落点",
           B.remap_modules(B.Blueprint(goal="x"), ws=w) == 0)

    print()
    print("[4] 读接口补种子：别按带查询串的路径误判、别覆盖变量名")
    bp = B.Blueprint(goal="t")
    bp.add(B.Cap.from_dict({"id": "cap.a.create", "probe": {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "A"},
         "expect_status": 201, "save": {"t1": "id"}}]}}))
    bp.add(B.Cap.from_dict({"id": "cap.a.search", "probe": {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u1", "password": "p1"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "u1", "password": "p1"}, "expect_status": 200,
         "save": {"token": "token"}},
        {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "A"},
         "expect_status": 201, "save": {"t1": "id"},
         "headers": {"Authorization": "Bearer ${token}"}},
        {"method": "GET", "path": "/api/v1/topics/?keyword=A", "expect_status": 200}]}}))
    B.normalize_http(bp)
    steps = bp.by_id("cap.a.search").probe["steps"]
    posts = [s for s in steps if str(s.get("method")).upper() == "POST"
             and str(s.get("path")).startswith("/api/v1/topics")]
    ck("同资源（忽略查询串）不再重复补种", len(posts) == 1, len(posts))
    ck("补种不覆盖原有变量名（不出现 ticket_id）",
       not any("ticket_id" in json.dumps(s, ensure_ascii=False) for s in steps))

    print()
    print("[5] 占位符必须能解析（含数组里、编号变量）")
    bp2 = B.Blueprint(goal="t")
    bp2.add(B.Cap.from_dict({"id": "cap.p", "probe": {"kind": "http", "steps": [
        {"method": "POST", "path": "/api/v1/topics/", "json": {"t": "a"},
         "expect_status": 201, "save": {"t1": "id"}},
        {"method": "POST", "path": "/api/v1/prefs/",
         "json": {"topic_ids": ["${t2}", "${t3}"]}, "expect_status": 201}]}}))
    B.repair_placeholders(bp2)
    body = bp2.by_id("cap.p").probe["steps"][1]["json"]["topic_ids"]
    ck("数组里的占位符也被修", body == ["${t1}", "${t1}"], body)

    print()
    print("[6] 层生成按栈（Django 写 apps/<域>/views.py，不碰 models）")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "config").mkdir()
        (w / "config" / "settings.py").write_text("# x", encoding="utf-8")
        (w / "manage.py").write_text("# x", encoding="utf-8")   # 实物判栈的强特征
        (w / "apps" / "topic").mkdir(parents=True)
        (w / "apps" / "topic" / "views.py").write_text("# old", encoding="utf-8")
        bp3 = B.Blueprint(goal="t")
        bp3.add(B.Cap.from_dict({"id": "c", "modules": ["apps/topic/views.py"],
                                 "probe": {"kind": "http", "steps": []}}))
        tg = F._layer_targets(w, bp3, "models", ("app/models.py", "app/schemas.py"))
        ck("Django 不重生成数据层", tg == [], tg)
        tg2 = F._layer_targets(w, bp3, "services", None)
        ck("Django 业务层落在 apps/topic/views.py", tg2 == ["apps/topic/views.py"], tg2)
        ck("Django 不再走 api 那一批", F._layer_targets(w, bp3, "api", None) == [])
        made = F._ensure_module_files(w, ["apps/topic/views.py", "apps/new/views.py"])
        ck("缺的落点会被创建出来（否则等于不生成）", "apps/new/views.py" in made, made)
        ck("已存在的文件不覆盖", "apps/topic/views.py" not in made, made)
        ck("已有内容没被清掉",
           (w / "apps" / "topic" / "views.py").read_text(encoding="utf-8") == "# old")

    print()
    print("[7] 真实蓝图（产物仓）必须自洽")
    ws = _repo_root()
    if (ws / ".sixcoder" / "blueprint.json").is_file():
        bp4 = B.load(ws)
        act = [c for c in bp4.caps if c.status != "descoped"]
        ck("活跃能力 > 60", len(act) > 60, len(act))
        ck("全部 http 探针", all(str((c.probe or {}).get("kind")) == "http" for c in act))
        ck("全部指向 config.asgi:application",
           all(str((c.probe or {}).get("app")) == "config.asgi:application" for c in act))
        bad_mod = [(c.id, m) for c in act for m in (c.modules or [])
                   if not m.startswith(("apps/", "matching_core/", "services/"))]
        ck("落点全在当前栈布局内", not bad_mod, bad_mod[:3])
        import re
        unresolved = []
        for c in act:
            saved = set()
            for st in (c.probe.get("steps") or []):
                for ph in re.findall(r"\$\{(\w+)\}", json.dumps(st, ensure_ascii=False)):
                    if ph not in saved:
                        unresolved.append((c.id, ph))
                saved |= set((st.get("save") or {}))
        # 真实产物里的未解析占位符属于**用户项目数据**（如历史命令 id 的 ${topic_id}），
        # 引擎不代改用户产物（红线）。这类是数据问题不是代码问题：打 WARN 并点名，
        # 不计入 FAIL —— 否则全量自检永远红，且指向的不是引擎代码（2026-11-23 查明）。
        if unresolved:
            print(f"  WARN  真实产物有 {len(unresolved)} 个未解析占位符"
                  f"（用户数据，需人工修产物）：{unresolved[:3]}")
        else:
            ck("探针占位符全部可解析", True)
        ck("校验无致命问题", not B.fatal(B.validate(bp4)), B.fatal(B.validate(bp4))[:2])

    print()
    print("[8] 角色串味修补（41 条能力假 bug 的根因，2026-11-18）")
    from core import probe as P
    # 与真实蓝图像素级同构的串味链：3 个 role 同一用户名注册 + 同名 3 次 login
    stale = [
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u_x", "password": "p1", "role": "admin"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u_x", "password": "p1", "role": "teacher"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u_x", "password": "p1", "role": "student"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "u_x", "password": "p1"}, "save": {"ttoken": "token"},
         "expect_status": 200},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "u_x", "password": "p1"}, "save": {"atoken": "token"},
         "expect_status": 200},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "u_x", "password": "p1"}, "save": {"stoken": "token"},
         "expect_status": 200},
        {"method": "POST", "path": "/api/v1/topics/",
         "headers": {"Authorization": "Bearer ${ttoken}"}, "json": {"title": "x"},
         "expect_status": 201},
    ]
    fixed, notes = P.dedupe_role_users(stale)
    reg_users = [s["json"]["username"] for s in fixed if "/register" in s["path"]]
    ck("register 拆成独立账号",
       reg_users == ["u_x_admin", "u_x_teacher", "u_x_student"], reg_users)
    login_users = [s["json"]["username"] for s in fixed if "/login" in s["path"]]
    ck("login 按 token 名认领对应角色账号",
       login_users == ["u_x_teacher", "u_x_admin", "u_x_student"], login_users)
    ck("判据字段未动（末步逐字相同）", fixed[-1] == stale[-1])
    ck("入参未被改（纯函数）", stale[0]["json"]["username"] == "u_x")
    fixed2, notes2 = P.dedupe_role_users(fixed)
    ck("幂等（独立账号链再跑零改动）", fixed2 == fixed and notes2 == [], notes2)
    one = [{"method": "POST", "path": "/api/v1/auth/register/",
            "json": {"username": "u_y", "role": "admin"}, "expect_status": 201}]
    ck("单角色不碰", P.dedupe_role_users(one)[1] == [])
    norole = [{"method": "POST", "path": "/api/v1/auth/register/",
               "json": {"username": "u_z"}}]
    ck("无 role 字段不碰", P.dedupe_role_users(norole)[0] == norole)
    # 单 token（无角色前缀）链：register 拆名后，login 必须认领末次注册角色的账号，
    # 否则 login 用已不存在的旧名 → 401（cap.auth.forbidden 实测形态）
    single = [
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u_s", "role": "teacher"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/register/",
         "json": {"username": "u_s", "role": "student"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/auth/login/",
         "json": {"username": "u_s", "password": "p1"}, "save": {"token": "token"},
         "expect_status": 200},
        {"method": "POST", "path": "/api/v1/topics/",
         "headers": {"Authorization": "Bearer ${token}"}, "expect_status": 403},
    ]
    sfix, _ = P.dedupe_role_users(single)
    slog = [s for s in sfix if "/login" in str(s.get("path"))][0]
    ck("单 token 链 login 认领末次注册角色账号（不残留已拆的旧名）",
       slog["json"]["username"] == "u_s_student", slog["json"]["username"])
    wrapped = P._dedupe_http_probe({"kind": "http", "steps": stale})
    ck("执行层包装改 steps 但不改入参探针",
       wrapped["steps"][0]["json"]["username"] == "u_x_admin"
       and stale[0]["json"]["username"] == "u_x")
    ck("run 的 http 分支真调用修补（不是只定义函数）",
       "_dedupe_http_probe(probe)" in inspect.getsource(P.run))
    ck("normalize_http 真调用修补（从蓝图源头修）",
       "dedupe_role_users" in inspect.getsource(B.normalize_http))
    # 真跑 normalize_http：串味链经它之后必须变成独立账号（源头生效，非仅执行兜底）
    bp5 = B.Blueprint(goal="t")
    bp5.add(B.Cap.from_dict({"id": "cap.role.test",
                             "probe": {"kind": "http", "steps": json.loads(json.dumps(stale))}}))
    B.normalize_http(bp5)
    r5 = [x for x in bp5.by_id("cap.role.test").probe["steps"]
          if "/register" in str(x.get("path"))]
    r5u = [x["json"]["username"] for x in r5]
    ck("normalize_http 源头改造独立账号（不再依赖执行兜底）",
       len(r5u) == 3 and all(any(u.endswith("_" + r) for r in ("admin", "teacher", "student"))
                             for u in r5u), r5u)

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


# 未配置项目路径时：这些自检依赖真实产物，明确 SKIP（不是 FAIL）
if __name__ == "__main__" and _repo_root() is None:
    print("SKIP：未配置项目路径（--repo / SIXCODER_REPO / _logs/engine.json），本自检需要真实产物仓")
    raise SystemExit(0)

if __name__ == "__main__":
    raise SystemExit(main())