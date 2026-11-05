"""单模型 + 自动思考强度 自检（2026-11-05 用户口径）。

用户要的机制：
    · 远端**可以填多个模型**（每个都有自己的 base_url / api_key / model）；
    · 但**一次运行只用一个模型**——不再是"多模型 × 多思考强度"并排成八档；
    · 思考强度是这个单模型内部的旋钮，由系统**按需求/难度自动选**；
    · 失败时的升级 = 同一个模型内换更强的思考档（换到顶就如实报"这个模型到极限了"）。

这里把四件事钉成可执行断言：
    1. ladder() 里只有**一个** profile_id（单模型）；
    2. 换模型 = 换 active_model（或填平铺 model 字段），阶梯跟着换；
    3. effort.route() 的自动路由规则（规格最深 / 机械活不思考 / 代码按难度）；
    4. effort.pick() 在模型没配某档时**向上找**，到顶就用最高档（不偷偷退回 none）。

跑法: python check_single_model.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import company, effort, policy, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("单模型 + 自动思考强度 自检")
    backup = settings_store.models()
    old_active = settings_store.get_remote().get("active_model")
    old_flat = settings_store.get_remote().get("model")
    try:
        # ---------------------------------------------------------------- 1 单模型
        print("[1] 阶梯里只能有一个模型")
        steps = settings_store.ladder()
        ids = {s["profile_id"] for s in steps}
        check("阶梯非空", bool(steps))
        check("只有一个 profile_id（没有并排的多模型）", len(ids) == 1, str(ids))
        act = settings_store.active_model() or {}
        check("ladder 用的就是这个 active_model", act.get("id") in ids,
              f"{act.get('id')} vs {ids}")
        check("阶梯里的强度是规范的 none/low/high/max 序列",
              [effort.norm(s["effort"]) for s in steps]
              == [e for e in effort.EFFORTS if e in {effort.norm(s["effort"]) for s in steps}],
              str([s["effort"] for s in steps]))

        # ---------------------------------------------------------------- 2 换模型
        print("[2] 换模型 = 换 active_model（档案依旧可以存多条）")
        profs = settings_store.models()
        if not any(p["id"] == "pro" for p in profs):
            check("默认档案里有 pro（可切换的第二条）", False, str([p["id"] for p in profs]))
            return 1
        for p in profs:
            if p["id"] == "pro":
                p["enabled"] = True
        settings_store.save_models(profs)
        check("只打开 pro 不会让阶梯变两条",
              len({s["profile_id"] for s in settings_store.ladder()}) == 1)
        settings_store.update_remote({"active_model": "pro"})
        check("切到 pro → 阶梯全是 pro",
              {s["profile_id"] for s in settings_store.ladder()} == {"pro"},
              str({s["profile_id"] for s in settings_store.ladder()}))
        settings_store.update_remote({"active_model": ""})

        # 平铺字段（界面只填 model/base_url/api_key）也能选模型
        flat_model = next(p["model"] for p in settings_store.models() if p["id"] == "pro")
        settings_store.update_remote({"model": flat_model})
        got = {s["profile_id"] for s in settings_store.ladder()}
        check("填平铺 model=pro 的模型名 → 自动用 pro", got == {"pro"}, str(got))
        settings_store.update_remote({"model": ""})
    finally:
        # 一定要把用户的配置原样放回去：active_model 和界面平铺的 model 都不许被自检改掉
        settings_store.update_remote({"active_model": old_active or ""})
        settings_store.update_remote({"model": old_flat or ""})
        settings_store.save_models(backup)
    check("收尾：active_model 与平铺 model 都还原了",
          (settings_store.get_remote().get("active_model") or "") == (old_active or "")
          and (settings_store.get_remote().get("model") or "") == (old_flat or ""),
          f"{settings_store.get_remote().get('active_model')!r} / "
          f"{settings_store.get_remote().get('model')!r}")

    # ---------------------------------------------------------------- 3 自动路由
    print("[3] 思考强度自动路由（按需求/难度）")
    check("规格/方案 → 最深（max）", effort.route("spec") == "max")
    check("机械活（样板/校验/日志/适配）→ 不思考（none）",
          effort.route("mechanical") == "none" and effort.route("delegate") == "none")
    lvl = {0: "none", 1: "low", 3: "high", 5: "max", 99: "max"}
    for score, want in lvl.items():
        check(f"代码难度分 {score} → {want}", effort.route("code", score) == want,
              effort.route("code", score))
    d0 = effort.difficulty()
    d_hard = effort.difficulty(deps=3, funcs=7, examples=4, fails=2, stub=True, lines=400)
    check("难度分：空模块 = 0", d0 == 0, str(d0))
    check("难度分：又多依赖又多函数又反复失败 → 高分", d_hard >= 5, str(d_hard))
    check("难度分单调：失败次数越多分越高",
          effort.difficulty(fails=2) > effort.difficulty(fails=1) > effort.difficulty(fails=0))

    # ---------------------------------------------------------------- 4 落地挑档
    print("[4] 把'想要的档'落到模型真实支持的档上")
    four = [{"effort": e} for e in ("none", "low", "high", "max")]
    check("四档齐全时精确定位", [effort.pick(four, w) for w in ("none", "low", "high", "max")]
          == [0, 1, 2, 3])
    sparse = [{"effort": "none"}, {"effort": "high"}]
    check("模型没配 low/max → 向上找（want=max 用 high）", effort.pick(sparse, "max") == 1,
          str(effort.pick(sparse, "max")))
    check("want=low 时也向上找（用 high，不退回 none）", effort.pick(sparse, "low") == 1)
    only_one = [{"effort": "none"}]
    check("模型只有一档 → 用那一档", effort.pick(only_one, "max") == 0)
    check("空阶梯不炸", effort.pick([], "max") == 0)

    # ---------------------------------------------------------------- 5 编排真的用了
    print("[5] 编排层真的按难度路由，而且升级仍停在同一个模型内")
    src = inspect.getsource(company)
    check("按依赖/函数/失败算难度并挑档", "_auto_effort" in src and "effort.pick" in src)
    check("路由事件会落到事件流（可解释）", '"effort_route"' in src)
    check("甲方模式 effort_mode=auto", policy.for_mode("client").effort_mode == "auto")
    check("开发者模式 effort_mode=ladder（省钱）",
          policy.for_mode("developer").effort_mode == "ladder")
    ladder_ids = [s.get("profile_id") for s in settings_store.ladder()]
    check("升级只会动 effort，不动模型（阶梯内 profile 唯一）",
          len(set(ladder_ids)) == 1, str(set(ladder_ids)))
    ev = effort.explain("code", 5, "max", four, 3)
    check("explain() 说清为什么用这一档", ev["chosen"] == "max" and ev["why"], str(ev))

    # ---------------------------------------------------------------- 6 接口层
    print("[6] 接口层对得上（界面/CLI 能真的切换模型）")
    root = Path(__file__).resolve().parent.parent
    srv = (root / "server.py").read_text(encoding="utf-8", errors="replace")
    check("设置请求体接受 active_model", "active_model: Optional[str]" in srv)
    check("active_model 允许清空（空串 = 回到自动解析）",
          'k == "active_model"' in srv and 'patch["active_model"] = str(v or "").strip()' in srv)
    pub = (root / "core" / "settings_store.py").read_text(encoding="utf-8", errors="replace")
    check("对外视图带出当前模型（界面能显示『当前』）", '"active_model"' in pub)
    app_vue = (root / "web" / "src" / "App.vue").read_text(encoding="utf-8", errors="replace")
    check("界面有『用这个』= 切换当前模型", "useProfile" in app_vue and "用这个" in app_vue)
    check("界面有『当前模型』三合一填写（模型名/接口/key）",
          "saveCurrentModel" in app_vue and "保存当前模型" in app_vue)
    check("界面不再出现旧的美元价目表编辑器", "价目表（可编辑" not in app_vue
          and "美元 / 每百万 token" not in app_vue)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（单模型 / 换模型生效 / 难度自动路由 / 缺档向上找）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
