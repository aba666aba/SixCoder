"""规模推导与层数规划自检（开发文档 §8.2 补充）。

要钉住三件事：
  1. 目标规模优先取**需求声明**（30000 行 / 3 万行），不是靠实测反推；
  2. 没声明时按**内容折算**给区间，且每条系数都能被人复核；
  3. **层数随规模变**：2 千以下两层、3 万四层、5 万以上五层——不能一律三层。

跑法: python checks/check_plan_scale.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import plan_scale  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


REQ_BIG = """# 双向选题系统

## 一、教师发题与配额
## 二、学生三志愿填报
## 三、学生提议 DA 匹配
## 四、调剂推荐
## 五、实验与分析
验收标准：一志愿命中率不低于 75%；blocking pair 为零。
接口：/api/v1/topics、/api/v1/prefs、/api/v1/batches

代码规模：30000 行
教师、学生、管理员三个角色。
"""

REQ_SMALL = """# 小工具

把 CSV 转成 JSON，一条命令跑完。
"""


def main() -> int:
    print("[1] 需求声明优先（30000 行 / 3 万行）")
    d1 = plan_scale.declared(REQ_BIG)
    check("抓到 30000 行", d1["lines"] == 30000, str(d1))
    d2 = plan_scale.declared("目标 3 万行，做一套教务系统")
    check("抓到 3 万行写法", d2["lines"] == 30000, str(d2))
    d3 = plan_scale.declared("代码规模：M")
    check("认得档位写法", d3["tier"] == "M", str(d3))

    p1 = plan_scale.plan(REQ_BIG)
    check("plan 以声明为准", p1["target"] == 30000 and p1["source"] == "declared", str(p1["source"]))

    print("[2] 没声明时按内容折算，且系数可复核")
    p2 = plan_scale.plan(REQ_SMALL)
    check("来源标成折算", p2["source"] == "estimated", p2["source"])
    check("有区间且低<中<高",
          p2["estimate"]["low"] <= p2["estimate"]["estimated"] <= p2["estimate"]["high"],
          str(p2["estimate"]))
    check("折算依据不空", len(p2["estimate"]["basis"]) >= 4, str(p2["estimate"]["basis"]))
    check("小工具不会被估成几万行", p2["target"] < 5000, str(p2["target"]))

    print("[3] 层数随规模变（这是本节的重点）")
    cases = [(1500, 2), (1999, 2), (2000, 3), (9999, 3), (10000, 4), (49999, 4), (50000, 5)]
    for t, want in cases:
        got = plan_scale.layers_for(t)["layers"]
        check(f"{t} 行 → {want} 层", got == want, str(got))
    lay = plan_scale.layers_for(30000)
    check("3 万行是四层且拆到接口", lay["layers"] == 4 and lay["freeze"] == "接口清单", str(lay))
    check("片上限随规模放大", plan_scale.layers_for(60000)["slice_files"] >
          plan_scale.layers_for(1500)["slice_files"], str(plan_scale.layers_for(60000)))

    print("[4] 声明与折算差太多要如实提示（不假装一致）")
    p4 = plan_scale.plan(REQ_SMALL + "\n代码规模：50000 行\n")
    check("冲突被标出来", p4["conflict"] is True, str(p4.get("note")))
    check("冲突时仍按声明规划", p4["target"] == 50000, str(p4["target"]))

    print("[5] 蓝图参与折算")
    e = plan_scale.estimate(REQ_SMALL, caps=20, funcs=6)
    check("蓝图折算被用上", e["by_blueprint"] == 2400, str(e["by_blueprint"]))
    check("basis 里写明蓝图口径", any("蓝图" in b for b in e["basis"]), str(e["basis"]))

    print("[6] 落盘 / 读回 / 摘要")
    ws = Path(tempfile.mkdtemp(prefix="ps_"))
    plan_scale.plan(REQ_BIG, ws=ws)
    got = plan_scale.load(ws)
    check("计划落盘可读回", got.get("target") == 30000, str(got.get("target")))
    lines = "\n".join(plan_scale.summary_lines(got))
    check("摘要写明来源与层数", "需求声明" in lines and "4 层" in lines, lines[:160])

    print("[N] 链中间没人负责的端点必须进契约（login 黑洞）")
    from core import blueprint as _bpm, slicer as _sl
    _b = _bpm.Blueprint(goal="g")
    # 真实形状：login_token 这条能力的**末步是 me**，所以 login 本身没人负责
    _b.add(_bpm.Cap(id="cap.auth.login", desc="x", funcs_est=1,
                    probe={"kind": "http", "steps": [
                        {"method": "POST", "path": "/api/v1/auth/register",
                         "json": {"username": "u", "password": "p"}, "expect_status": 201},
                        {"method": "POST", "path": "/api/v1/auth/login",
                         "json": {"username": "u", "password": "p"},
                         "expect_status": 200, "save": {"token": "token"}},
                        {"method": "GET", "path": "/api/v1/auth/me",
                         "headers": {"Authorization": "Bearer ${token}"},
                         "expect_status": 200}]}))
    _b.add(_bpm.Cap(id="cap.topic.create", desc="y", funcs_est=1,
                    probe={"kind": "http", "steps": [
                        {"method": "POST", "path": "/api/v1/auth/login",
                         "json": {"username": "u", "password": "p"},
                         "expect_status": 200, "save": {"token": "token"}},
                        {"method": "POST", "path": "/api/v1/topics",
                         "json": {"title": "t"}, "expect_status": 201}]}))
    _owned = _sl.owned_routes(_b)
    _spe = _sl.web_route_spec([_b.by_id("cap.topic.create").probe], _owned)
    _joined = " | ".join(_spe)
    check("链中间的 login 进了契约", "/auth/login" in _joined, _joined[:120])
    check("自己的末步也在契约里", "/api/v1/topics" in _joined, _joined[:120])
    # 已被当产出负责的端点不再重复要求（login 能力自己负责它）
    _spe2 = _sl.web_route_spec([_b.by_id("cap.auth.login").probe], _owned)
    check("login 只出现一次（不因多条链重复写）",
          _joined.count("/auth/login") == 1, str(_joined))
    check("owned=None 时保持旧行为（只取末步）",
          len(_sl.web_route_spec([_b.by_id("cap.topic.create").probe])) == 1)

    print("[N+1] 路由落点由路径决定（跨模块必须点名）")
    _sl_rh = _sl.route_home
    check("auth/login → auth.py", _sl_rh("/api/v1/auth/login") == "app/api/auth.py",
          _sl_rh("/api/v1/auth/login"))
    check("batches/{id}/run → batches.py（取资源段不取动作）",
          _sl_rh("/api/v1/batches/{id}/run") == "app/api/batches.py",
          _sl_rh("/api/v1/batches/{id}/run"))
    check("org/majors → org.py", _sl_rh("/api/v1/org/majors") == "app/api/org.py",
          _sl_rh("/api/v1/org/majors"))
    check("根路径不指定模块", _sl_rh("/") == "", _sl_rh("/"))
    _sp3 = _sl.web_route_spec([_b.by_id("cap.topic.create").probe], _owned,
                              home="app/api/topics.py")
    _j3 = " | ".join(_sp3)
    # 2026-11-11 **口径变更**（期望过期，不是代码错）：
    # 旧口径要求路由提示里**点名接线层文件**（"写在 app/api/auth.py"），
    # 让远端把处理函数写进**引擎拥有**的文件 ——
    # 那文件每次接线都整块重渲染（远端写的被冲掉），且通常承载已通过能力
    # → 触发「不许整文件重写」护栏 → 两道锁叠成死锁
    # （实测 56 条红灯里 52 条落在 app/api/*，表现为「修好 0 条，仍红 4 条（空转）」）。
    # 新口径（开发文档 §11.4e 文件所有权）：**不许点名接线层**，
    # 只说"处理函数写在上面列的落点模块里，函数名含路径末段"。
    check("跨模块路由**不再**点名接线层文件（所有权口径）",
          "app/api/" not in _j3 and "上面列的落点模块" in _j3, _j3[:140])
    # 本片模块就是 topics.py 时，topics 那条不该再啰嗦
    _sp4 = _sl.web_route_spec([_b.by_id("cap.topic.create").probe], _owned,
                              home="app/api/topics.py")
    _t_line = [x for x in _sp4 if x.startswith("POST /api/v1/topics")][0]
    check("同模块那条不啰嗦", "上面列的落点模块" not in _t_line, _t_line[:120])
    check("它模块那条说明写去哪个模块（不含接线层路径）",
          any("上面列的落点模块" in x and "app/api/" not in x for x in _sp4),
          str(_sp4)[:140])

    print("[N+2] 没人负责的端点必须点名可接线函数名（契约/接线规则对齐）")
    # 实测 2026-11-09：契约要求作者写 parse_login_body / build_login_response，
    # 而接线层的 _NON_HANDLER_PREFIX 正是要否决 parse_/build_ 开头的构造器 →
    # 作者写得再对，_pick_func 也返回空，POST /auth/login 永远 404。
    # 这条把「契约推荐的名字」直接喂给接线层，防止两条规则再次自相矛盾。
    from core import auto as _auto
    import re as _re2
    _sp5 = _sl.web_route_spec([_b.by_id("cap.topic.create").probe], _owned,
                              home="app/api/topics.py")
    _login_line = [x for x in _sp5 if x.startswith("POST /api/v1/auth/login")][0]
    check("点名了处理函数名", "处理函数" in _login_line, _login_line[:170])
    _m = _re2.search(r"处理函数 `([A-Za-z_]\w*)`", _login_line)
    _rec = _m.group(1) if _m else ""
    _pick = (_auto._pick_func("/api/v1/auth/login", {_rec: ["body"]}, "POST")
             if _rec else "")
    check("推荐名真能被接线层认出", bool(_rec) and _pick == _rec,
          f"recommended={_rec!r} pick={_pick!r}")
    check("不推荐构造器前缀（parse_/build_ 等）",
          bool(_rec) and not _rec.startswith(
              ("parse_", "build_", "make_", "coerce_", "format_", "render_")),
          _rec)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（声明优先 / 折算可复核 / 层数随规模 / 冲突如实提示 / 蓝图参与）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
