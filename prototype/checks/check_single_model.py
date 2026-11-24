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
# 隔离：自检不许碰用户真配置（开发文档铁律：自检必须走独立 SIXCODER_SETTINGS）。
import os as _os
import tempfile as _tf
_os.environ["SIXCODER_SETTINGS"] = str(Path(_tf.mkdtemp(prefix="sixcoder_sm_")) / "settings.json")
from core import company, contractgen, dosing, effort, policy, settings_store  # noqa: E402

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
    # 2026-11-10 口径：route() 只表达"封顶心愿档"（全 kind=max）；
    # 写码起点 = proven_floor（模块实测档案）优先 + route_start（难度先验）兜底。
    print("[3] 思考强度路由（封顶恒 max；起点按模块档案/难度先验）")
    check("封顶语义：spec → max", effort.route("spec") == "max")
    check("封顶语义：mechanical/delegate/code → max（模型物理上限）",
          effort.route("mechanical") == "max" and effort.route("delegate") == "max"
          and effort.route("code") == "max")
    lvl = {0: "none", 2: "none", 3: "low", 5: "low", 6: "high", 99: "high"}
    for score, want in lvl.items():
        check(f"起点先验 route_start({score}) = {want}",
              effort.route_start(score) == want, effort.route_start(score))
    check("起点先验永不给 max（max 留给 spec/升档救场）",
          all(effort.route_start(s) != "max" for s in range(10)))
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
    check("造物模式 effort_mode=auto", policy.for_mode("client").effort_mode == "auto")
    check("精修模式 effort_mode=ladder（省钱）",
          policy.for_mode("developer").effort_mode == "ladder")
    ladder_ids = [s.get("profile_id") for s in settings_store.ladder()]
    check("升级只会动 effort，不动模型（阶梯内 profile 唯一）",
          len(set(ladder_ids)) == 1, str(set(ladder_ids)))
    ev = effort.explain("code", 5, "max", four, 3)
    check("explain() 说清为什么用这一档", ev["chosen"] == "max" and ev["why"], str(ev))

    # ---------------------------------------------------------------- 6 思考配额
    print("[6] 思考额度加在正文配额之上（不然越想越写不出来）")
    from core import remote as _remote
    _msgs = [{"role": "user", "content": "x"}]

    def _mt(effort, cap=8000):
        step = {"model": "deepseek-flash", "effort": effort, "max_output_tokens": cap}
        payload, _, _ = _remote._payload_for(step, {}, _msgs, True)
        return payload.get("max_tokens"), payload.get("thinking")

    mt_none, th_none = _mt("none")
    mt_max, th_max = _mt("max")
    # 生效上限是**校准值**（core/calibration），不是那个常量：常量只是地板。
    # 断言口径因此改成"等于当前生效值 + 不低于地板 + 小于官方 384K"，
    # 否则台账一变（真实跑几次就会变）这条自检就假红 —— 测的应该是策略，不是某天的数字。
    cap_now = _remote.safety_cap()
    check("不思考时也直接给到安全上限（上限不等于消费）", mt_none == cap_now, str(mt_none))
    check("max 档同样顶到安全上限，不再按需算小值", mt_max == cap_now, str(mt_max))
    check("安全上限不低于地板 128k、容得下 4 倍实测峰值、且小于官方 384K",
          _remote.SAFETY_MIN_CAP <= cap_now < 384000 and 4 * 26788 <= cap_now,
          str(cap_now))
    check("校准时上限绝不被压到地板以下", _remote.safety_cap() >= _remote.SAFETY_MIN_CAP,
          str(cap_now))
    check("开档位的 thinking 参数正确", th_max == {"type": "enabled"}
          and th_none == {"type": "disabled"}, f"{th_max} / {th_none}")
    mt_floor, _ = _mt("max", cap=0)
    check("没配正文配额也照样给上限", mt_floor == cap_now, str(mt_floor))
    check("正文配额（dosing）与上限解耦：capacity 仍是 12 000",
          dosing.capacity_tokens({"max_output_tokens": 12000, "effort": "max"}) == 12000)
    # 正文截断判定：必须扣掉思考再比，否则"思考用满"会被误判成"正文用满"
    check("思考吃满但正文没满 → 不算截断",
          _remote.content_truncated({"completion_tokens": 16000, "reasoning_tokens": 12815},
                                    8000) is False)
    check("正文用满 → 判为截断",
          _remote.content_truncated({"completion_tokens": 8000, "reasoning_tokens": 0},
                                    8000) is True)
    check("没有正文配额 → 不判截断（避免无脑重试）",
          _remote.content_truncated({"completion_tokens": 16000}, 0) is False)
    check("契约合成会把正文配额传给远端（否则只能吃档案里的 8000）",
          "max_out=out_budget" in inspect.getsource(contractgen.synthesize))
    check("契约合成能识别截断并加预算重试",
          "content_truncated" in inspect.getsource(contractgen.synthesize))
    # 唯一的算法入口：complete() 的 max_out 分支也必须带预留，否则会把 _payload_for 的结果覆盖掉
    check("max_tokens 只有一个算法入口 max_tokens_for",
          _remote.max_tokens_for(12000, "max") == cap_now
          and _remote.max_tokens_for(0, "none") == 0)
    _csrc = inspect.getsource(_remote.complete)
    check("complete() 的 max_out 分支带上了思考预留",
          "REASONING_RESERVE.get" in _csrc or "max_tokens_for" in _csrc, _csrc[:0])
    # 用户口径：一开始就给足，不够就归因配额、当场翻倍重试
    check("预留按实测给足（high ≥ 实测 15 765、max ≥ 实测 22 452）",
          _remote.REASONING_RESERVE["high"] >= 15765
          and _remote.REASONING_RESERVE["max"] >= 22452,
          str(_remote.REASONING_RESERVE))
    check("配额不够会当场翻倍重试（归因到配额，不归因到模型）",
          "budget_grown" in _csrc and "starved" in _csrc, _csrc[:0])
    check("正文下限给足（不让人再看到『思考吃满、正文 0』）",
          _remote.CONTENT_FLOOR >= 12000, str(_remote.CONTENT_FLOOR))
    check("正文部分被夹在官方 384k 以内（思考预留另加）",
          _remote.max_tokens_for(400000, "max")
          == 384000 + _remote.reasoning_reserve("max"),
          str(_remote.max_tokens_for(400000, "max")))

    # ---------------------------------------------------------------- 7 接口层
    print("[7] 接口层对得上（界面/CLI 能真的切换模型）")
    root = Path(__file__).resolve().parent.parent
    srv = (root / "server.py").read_text(encoding="utf-8", errors="replace")
    check("设置请求体接受 active_model", "active_model: Optional[str]" in srv)
    check("active_model 允许清空（空串 = 回到自动解析）",
          'k == "active_model"' in srv and 'patch["active_model"] = str(v or "").strip()' in srv)
    pub = (root / "core" / "settings_store.py").read_text(encoding="utf-8", errors="replace")
    check("对外视图带出当前模型（界面能显示『当前』）", '"active_model"' in pub)
    app_vue = (root / "web" / "src" / "App.vue").read_text(encoding="utf-8", errors="replace")
    check("界面有『设为当前』= 切换当前模型",
          "useProfile" in app_vue and "设为当前" in app_vue)
    # 2026-11-09 厂商一等公民重构：连接参数（请求网址 / API key）的编辑移到了
    # 厂商弹窗的「连接」区，保存动作是 saveProviderDraft。2026-11-19 按用户口径
    # 把按钮从「保存这个厂商」改成「保存这个 key」（更准确：这个弹窗存的是 key）。
    # 钉"有保存动作 + 两种措辞至少在其中"，避免文案微调又假红。
    check("界面点开厂商可改连接参数并保存（网址 / key）",
          "请求网址" in app_vue and "API key" in app_vue
          and "saveProviderDraft" in app_vue
          and ("保存这个 key" in app_vue or "保存这个厂商" in app_vue))
    check("界面不再出现旧的美元价目表编辑器", "价目表（可编辑" not in app_vue
          and "美元 / 每百万 token" not in app_vue)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（单模型 / 换模型生效 / 难度自动路由 / 缺档向上找）")
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
