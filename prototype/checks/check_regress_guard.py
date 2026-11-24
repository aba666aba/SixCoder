"""三件修复的回归钉子（2026-11-10 晚，用户定稿）。

1. **回归保护**：承载"已 done 能力"的模块，写完立刻定向复验；坏了当场回滚，
   不等下一轮全量观测（中午 3.5 小时 26 次状态变化、净增只有 4 条，就是等出来的）。
2. **顺序不再倒转**：轮号旋转/红灯优先是**优先级**，不许把依赖序改成"下游先于上游"。
3. **报告口径**：抬头必须同时给"端到端能力"和"业务流"，不许只报 done。

跑法：python checks/check_regress_guard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import company as C                                  # noqa: E402
from core import blueprint as BPM                              # noqa: E402
from core import openhouse as OH                               # noqa: E402
from core import project as PJ                                 # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


class _Cap:
    def __init__(self, cid, status, modules):
        self.id, self.status, self.modules = cid, status, list(modules)


class _BP:
    def __init__(self, caps):
        self.caps = caps


def main() -> int:
    print("[1] 已 done 能力归属表：只认 done，absent 不进表")
    _bp = _BP([_Cap("cap.a", "done", ["app/api/x.py", "app/svc/x.py"]),
               _Cap("cap.b", "absent", ["app/api/x.py"]),
               _Cap("cap.c", "done", ["app/y.py"])])
    _orig_load = BPM.load
    BPM.load = lambda ws: _bp
    try:
        m = C._caps_done_by_file(Path("."))
        check("x.py 承载 1 条 done（absent 的不算）", m.get("app/api/x.py") == {"cap.a"}, str(m))
        check("svc/x.py 也承载同一条", m.get("app/svc/x.py") == {"cap.a"}, str(m))
        check("y.py 承载 cap.c", m.get("app/y.py") == {"cap.c"}, str(m))
        check("absent 能力没有自己的键", "cap.b" not in str(m))
    finally:
        BPM.load = _orig_load

    print("[2] 定向复验：坏了返回那几个 id；引擎跑不动返回 None（不拦）")
    _orig_obs = OH.observe
    _orig_load2 = BPM.load
    BPM.load = lambda ws: _BP([_Cap("cap.a", "done", ["m.py"]),
                               _Cap("cap.b", "done", ["m.py"])])
    try:
        OH.observe = lambda ws, bp, emit=None, only=None: {
            "caps": {c: {"status": "done"} for c in (only or [])}}
        got = C._probe_caps_done(Path("."), {"cap.a", "cap.b"})
        check("全部还在 → 返回全部", got == {"cap.a", "cap.b"}, str(got))

        OH.observe = lambda ws, bp, emit=None, only=None: {
            "caps": {"cap.a": {"status": "done"}, "cap.b": {"status": "absent"}}}
        got = C._probe_caps_done(Path("."), {"cap.a", "cap.b"})
        check("cap.b 被写坏 → 只返回 cap.a", got == {"cap.a"}, str(got))

        def _boom(*a, **k):
            raise RuntimeError("探针起不来")
        OH.observe = _boom
        got = C._probe_caps_done(Path("."), {"cap.a"})
        check("复验自己跑不动 → None（绝不据此回滚）", got is None, str(got))
        check("空集合直接返回空集合", C._probe_caps_done(Path("."), set()) == set())
    finally:
        OH.observe = _orig_obs
        BPM.load = _orig_load2

    print("[2b] 守护范围：横切文件全查，业务文件查依赖邻域（跨文件破坏要拦住）")
    _deps = {"app/api/topics.py": ["app/services/topics.py"],
             "app/services/topics.py": ["app/models.py"],
             "app/models.py": [],
             "app/api/auth.py": ["app/services/topics.py"],
             "app/services/quota.py": []}
    _dm = {"cap.topic.create_draft": {"app/api/topics.py"},
           "cap.auth.forbidden_role": {"app/api/auth.py"},
           "cap.service.quota": {"app/services/quota.py"},
           "cap.unrelated": {"app/unrelated.py"}}
    g_api = C._guard_ids_for("app/api/auth.py", _deps, _dm)
    check("改了 api 层文件 → 全部已 done 能力都要复验", g_api == set(_dm), str(g_api))
    g_mod = C._guard_ids_for("app/models.py", _deps, _dm)
    check("改 models.py → 全部复验（底层一动一片）", g_mod == set(_dm), str(g_mod))
    g_svc = C._guard_ids_for("app/services/quota.py", _deps, _dm)
    check("改普通业务文件 → 只查邻域，不把无关能力也拉进来",
          "cap.service.quota" in g_svc and "cap.unrelated" not in g_svc, str(g_svc))
    # 2026-11-10 23:15：邻域算出来是空集时"什么都不守"= 裸奔（当晚 28→27 那次就是这样）
    g_none = C._guard_ids_for("app/unknown_mod.py", {}, _dm)
    check("邻域为空 → 退回全部已 done（宁可多跑探针，绝不裸奔）",
          g_none == set(_dm), str(g_none))
    check("横切文件识别：api 层算", C._is_shared_file("app/api/topics.py") is True)
    check("横切文件识别：models/schemas/__init__ 算",
          C._is_shared_file("app/models.py") and C._is_shared_file("app/schemas.py")
          and C._is_shared_file("app/__init__.py"))
    check("横切文件识别：普通业务文件不算",
          C._is_shared_file("app/services/quota.py") is False)

    print("[3] 顺序不许倒转：下游不能排在上游前面")
    _orig_map = PJ.build_map
    PJ.build_map = lambda contracts: {
        "deps": {"app/api.py": ["app/models.py"],
                 "app/models.py": [],
                 "app/ui.py": ["app/api.py"]},
        "modules": {}}
    try:
        out = C._dep_respecting_order(["app/ui.py", "app/api.py", "app/models.py"], {})
        check("ui→api→models 被修正成 models→api→ui", out == ["app/models.py", "app/api.py", "app/ui.py"],
              str(out))
        # 同级（互不依赖）保持输入顺序 → 红灯优先/旋转公平性不被破坏
        PJ.build_map = lambda contracts: {"deps": {"a.py": [], "b.py": [], "c.py": []}, "modules": {}}
        out2 = C._dep_respecting_order(["c.py", "a.py", "b.py"], {})
        check("无依赖时完全保持原优先级顺序", out2 == ["c.py", "a.py", "b.py"], str(out2))
        # 有环不死循环、不丢模块
        PJ.build_map = lambda contracts: {"deps": {"x.py": ["y.py"], "y.py": ["x.py"]}, "modules": {}}
        out3 = C._dep_respecting_order(["x.py", "y.py"], {})
        check("有环也不会卡死或丢模块", sorted(out3) == ["x.py", "y.py"], str(out3))
        check("空输入返回空", C._dep_respecting_order([], {}) == [])
    finally:
        PJ.build_map = _orig_map

    print("[4] 源码层钉子：三处改动必须在位")
    src = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    # ① 提交前必须复验：从"定向复验"那次调用**往后**找到的 `sand.commit()` 必须在它之后
    #（按能力派单那条路径里也有 sand.commit()，用全局 find 会被它抢先命中）
    i_guard = src.find("_probe_caps_done(ws, _guard_base)")
    i_commit = src.find("sand.commit()", i_guard) if i_guard > 0 else -1
    check("提交前有定向复验", i_guard > 0 and i_commit > i_guard, f"guard={i_guard} commit={i_commit}")
    check("回归拦截会回滚", "regression_blocked" in src and "sand.rollback()" in src)
    # ② 顺序修正在旋转之后
    i_rot = src.find("% len(_ordered)")
    i_fix = src.find("_dep_respecting_order(_ordered, contracts)")
    check("顺序修正在轮号旋转之后", 0 < i_rot < i_fix, f"rot={i_rot} fix={i_fix}")
    # ③ 报告口径：抬头必须并列能力与业务流
    check("报告抬头并列「端到端能力」", "端到端能力：" in src)
    check("报告抬头并列「业务流」", "｜**业务流：" in src)
    check("只报 done 会被口径提醒", "不能当作" in src and "系统可用" in src)
    # ④ 承载已 done 能力的文件：不许整文件重写（21:10 定稿，负循环的止血）
    check("承载 done 能力 + 不缺函数 → 强制增量补丁",
          "_carries_done and not _missing_now" in src)
    # 2026-11-11 **口径变更**（期望过期，不是代码错）：
    # 旧断言要求"承载 done 能力的横切文件 + 缺函数 → 直接不开工"。
    # 实测那个口径本身是死锁：缺函数恰恰说明该文件要**加**新函数，
    # 正确做法是"只回函数体"（bodies）补上，整文件重写才是危险的那个动作。
    # 旧口径把"整文件重写有风险"读成了"这个文件不能碰" → 跳过 → 远端没写 →
    # 红灯一直在 → 每轮空转（实测毕设 s10-9712636b：「修好 0 条，仍红 4 条」）。
    # 新口径：**缺函数 → 也强制 bodies，不许跳过**。详见 checks/check_shared_guard.py。
    check("承载 done 能力 + 缺函数 → 强制补函数体（不许跳过，2026-11-11 口径变更）",
          "_carries_done and _missing_now" in src
          and "禁止整文件重写" not in src
          and "不做整文件重写（跳过）" not in src,
          "缺函数时又变回'跳过'了 → 死锁复发")
    # 顺序：增量/保护判定必须在"要注册端点就整文件"之前
    i_body = src.find("_carries_done and not _missing_now")
    i_ep = src.find('emit("granular_forced", {"path": rel, "to": "whole_file"')
    check("顺序正确：先保护已通过能力，再考虑端点需要", 0 < i_body < i_ep, f"{i_body}/{i_ep}")

    # ── 可观测性：提交时必须打出"守没守、基线几条、拦了几条" ────────────────
    # 2026-11-10 23:00 实测代价：日志里 isolate_commit 只剩 `why=`，
    # 漏拦时无法区分"基线为空（等于没守）"和"破坏来自邻域外"，只能靠猜。
    print("[N] 可观测性：isolate_commit 必须带 guarded_n / base_n")
    check("company.py 提交事件带 guarded_n", '"guarded_n"' in src)
    check("company.py 提交事件带 base_n", '"base_n"' in src)
    cli_src = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8")
    check("cli 事件白名单放行 guarded_n / base_n",
          '"guarded_n"' in cli_src and '"base_n"' in cli_src)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
