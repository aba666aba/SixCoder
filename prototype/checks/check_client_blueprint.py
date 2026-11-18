"""甲方模式必须接上 blueprint 那条线（§三 端到端业务流 / §八 骨架先行）。

2026-11-10 的由来（真实事故，不是设计想象）：
甲方模式（`company.night`）长期**绕开 blueprint 这条线**，形成闭环：
  写出来的模块**路由接不上**（契约只验函数、不验路由 → 35/35 全过）
  → 端到端红灯**没人知道**（从不跑能力覆盖，账本停在 16:34）
  → 完成判据**只看模块契约**（齐了就收工）。
实测 84 项能力只过 13 项。

`core/openhouse.py` 是甲方模式与全自主模式**共用**的一份实现 ——
复制两份必然漂移，所以这里断言的是"共用"，不是"甲方模式自己写一遍"。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    if cond:
        print(f"  OK   {name}")
    else:
        print(f"  FAIL {name}  {extra}")
        FAILS.append(name)


def main() -> int:
    print("[1] openhouse 模块：语法 + 关键能力都在")
    src = (ROOT / "core" / "openhouse.py").read_text(encoding="utf-8")
    try:
        ast.parse(src)
        check("openhouse.py 语法可解析", True)
    except SyntaxError as exc:
        check("openhouse.py 语法可解析", False, str(exc))
        return 1
    for fn in ("def prepare(", "def wire(", "def observe(", "def red_modules("):
        check(f"openhouse 里有 {fn})", fn in src)

    print("[2] 甲方模式真的调用了这些能力（不是只写了模块没人用）")
    csrc = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("company 加载 blueprint", "blueprint" in csrc and "_bp_mod.load(ws)" in csrc)
    check("company 跑开工段（骨架树/接口锁定/扩展点）",
          "_oh.prepare(" in csrc)
    check("company 跑端到端观测", "_oh.observe(" in csrc)
    check("company 跑接线", "_oh.wire(" in csrc)
    check("company 用红灯模块映射", "_oh.red_modules(" in csrc)
    check("company 记能力账本", "record(ws, _cov" in csrc)

    print("[3] 红灯能力必须变成工作单（否则观测只是白跑）")
    check("needs_work 把红灯模块判成要干活",
          "if cap_red and path in cap_red:" in csrc)
    check("_author_round 收下 cap_red/cap_detail",
          "cap_red: set | None = None" in csrc and "cap_detail: dict | None = None" in csrc)
    check("红灯详情喂给远端（evidence）", "_cap_ev" in csrc and "evidence=_cap_ev" in csrc)
    # 这一条最关键：甲方模式的**主路径是 orders**（workorder.triage 分诊），
    # 不是 _author_round。triage 只看模块级判据 → 契约全过 → 无单 → 收工。
    # 所以红灯必须自己变成 orders，否则前面所有观测全是白跑。
    check("端到端红灯被转成工作单（orders）", "cap_orders" in csrc and
          '"kind": "implement"' in csrc)
    check("工作单带上端到端失败详情", "端到端验收不通过" in csrc)
    # 2026-11-17 反转：契约只管命名/缓存前缀，不再是派单白名单。
    # 旧规则 `if _m not in contracts` 把 19 条红灯滤到 1-2 条（契约只有 3 个模块），
    # 且漏过的恰是护栏命门 → 净增长恒 0。现在要求这行**不存在**。
    check("契约窄表不再当派单闸门（红灯照常派）", "if _m not in contracts" not in csrc)
    check("派单仍过 triage 分诊闸门（local_wiring/env 不派）",
          "if allow is not None and m not in allow:" in csrc)

    print("[4] 收工判据：契约全过但端到端还红 → 必须如实写出来")
    check("收工理由包含端到端红灯数",
          "端到端还有" in csrc and "cap_red" in csrc)

    print("[5] 两条模式共用一份实现（不许复制两份）")
    asrc = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("auto.py 里也有 openhouse 的引用", "openhouse" in asrc)

    print("[6] 行为级：red_modules 能把红灯能力映射成模块")
    from core import openhouse as _oh

    class _Cap:
        def __init__(self, cid, mods):
            self.id = cid
            self.modules = mods

    class _BP:
        def __init__(self, caps):
            self._caps = caps

        def by_id(self, cid):
            for c in self._caps:
                if c.id == cid:
                    return c
            return None

    _bp = _BP([_Cap("cap.a.x", ["app/a.py"]), _Cap("cap.b.y", ["app/b.py", "app/c.py"])])
    _cov = {"caps": {"cap.a.x": {"status": "absent", "detail": "404 路由未命中"},
                     "cap.b.y": {"status": "done", "detail": "通过"},
                     "cap.c.z": {"status": "absent", "detail": "缺 code 字段"}}}
    _mods, _detail = _oh.red_modules(_cov, _bp)
    check("红灯能力映射出模块 app/a.py", "app/a.py" in _mods, str(_mods))
    check("已 done 的能力不进模块集", "app/b.py" not in _mods and "app/c.py" not in _mods,
          str(_mods))
    check("失败详情按模块归档", _detail.get("app/a.py") and "404" in _detail["app/a.py"][0],
          str(_detail))
    _mods2, _d2 = _oh.red_modules(_cov, None)
    check("没有 blueprint 时也不崩（退化为空集）", isinstance(_mods2, set) and isinstance(_d2, dict))

    print("[7] 红灯映射必须能落到契约层（两套路径体系的桥）")
    import tempfile
    _ws = Path(tempfile.mkdtemp())
    (_ws / "app" / "api").mkdir(parents=True)
    (_ws / "matching_core").mkdir(parents=True)
    (_ws / "app" / "api" / "auth.py").write_text(
        "from app.authz import check\n", encoding="utf-8")
    (_ws / "app" / "authz.py").write_text("def check():\n    return 1\n", encoding="utf-8")
    (_ws / "app" / "analytics.py").write_text("def heat():\n    return 1\n", encoding="utf-8")
    (_ws / "matching_core" / "da.py").write_text("def solve():\n    return 1\n", encoding="utf-8")
    check("按端点路径反查得到业务模块",
          "app/authz.py" in _oh.module_candidates(_ws, "/api/v1/auth/me"),
          str(_oh.module_candidates(_ws, "/api/v1/auth/me")))
    check("路由层与业务层两个候选都给（远端只能改业务层）",
          "app/api/auth.py" in _oh.module_candidates(_ws, "/api/v1/auth/me"))
    check("单数/复数变体都认（adjustments → adjustment）",
          "app/analytics.py" in _oh.module_candidates(_ws, "/api/v1/analytics/heat")
          or "app/api/analytics.py" in _oh.module_candidates(_ws, "/api/v1/analytics/heat"),
          str(_oh.module_candidates(_ws, "/api/v1/analytics/heat")))
    check("能力探针路径能被取出来",
          _oh.cap_paths(type("C", (), {"probe": {"path": "/x", "steps": [{"path": "/y"}]}})()) == ["/x", "/y"])

    print("[8] 端点清单：语义映射交给远端（引擎靠字符串猜接不上）")
    import tempfile as _tf
    _ws2 = Path(_tf.mkdtemp())
    (_ws2 / "app" / "api").mkdir(parents=True)
    (_ws2 / "app").mkdir(parents=True, exist_ok=True)
    (_ws2 / "app" / "analytics.py").write_text("def quota_advice():\n    return {}\n", encoding="utf-8")

    class _Cap2:
        def __init__(self, cid, path, method="GET"):
            self.id = cid
            self.modules = []
            self.probe = {"kind": "http", "steps": [{"method": method, "path": path}]}

    class _BP2:
        def __init__(self, caps):
            self._caps = caps
            self.caps = caps

        def by_id(self, cid):
            for c in self._caps:
                if c.id == cid:
                    return c
            return None

    _bp2 = _BP2([_Cap2("cap.a", "/api/v1/analytics/quota-suggestions")])
    _eps = _oh.endpoints_for_module(_bp2, _ws2, "app/analytics.py")
    check("能算出模块该负责的端点", _eps == ["GET /api/v1/analytics/quota-suggestions"], str(_eps))
    check("提示词里要求用装饰器注册",
          "必须真的注册" in csrc and "APIRouter" in csrc)
    check("写码时把端点清单拼进 plan", "_mod_plan" in csrc and "cap_endpoints" in csrc)
    check("接线候选含 matching_core（算法端点落点在算法包里）",
          '"matching_core"' in (ROOT / "core" / "auto.py").read_text(encoding="utf-8"))
    check("路径匹配认连字符（quota-suggestions → quota + suggestion）",
          're.split(r"[-_]+"' in (ROOT / "core" / "auto.py").read_text(encoding="utf-8"))

    print("[9] 作者自己注册的端点是权威（引擎不许摘）")
    asrc2 = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("接线会先收集作者已注册的端点", "authored_by_mod" in asrc2)
    check("作者注册的端点不进引擎渲染段", 'if not r.get("authored")' in asrc2)
    check("作者注册的端点仍记进接线表（入口要 include）",
          '"authored": True' in asrc2)
    print("[10] 红灯工作单必须置顶 + 红灯模块优先（否则永远排不上）")
    check("红灯单插到 orders 最前面", "orders = _cap_new + orders" in csrc)
    check("红灯模块排在依赖序之前", "_cap_first" in csrc)
    check("**轮转**：上一轮处理过的排到后面（只排序不够）",
          "_cap_seen" in csrc and "cap_recent" in csrc)
    check("每轮回写处理过的模块", "cap_recent = {str(m.get(\"path\")" in csrc)
    check("按轮号旋转起点（保证均匀覆盖所有红灯模块）", "cap_round" in csrc)
    print("[11] 振荡指纹必须含端到端状态（契约全过时指纹恒定 = 假振荡）")
    osrc = (ROOT / "core" / "oscillation.py").read_text(encoding="utf-8")
    check("fingerprint 接受 cap_cov", "cap_cov" in osrc and "cap_red=" in osrc)
    check("company 把端到端观测传进指纹", "_osc.fingerprint(state, cap_cov)" in csrc)
    print("[12] 接线后必须冒烟（接线弄坏入口 = 整个工作区全灭）")
    osrc2 = (ROOT / "core" / "openhouse.py").read_text(encoding="utf-8")
    check("接线前拍快照", "_snapshot_files" in osrc2)
    check("接线后跑入口冒烟", "gate_import_smoke" in osrc2)
    check("冒烟失败恢复快照", "_restore_files" in osrc2)
    check("恢复后本轮不采用这次接线", "wire_reverted" in osrc2)
    print("[13] 要注册路由的模块必须走整文件路径（bodies 路径加不了装饰器）")
    check("有端点清单 → 强制 whole_file", "granular_forced" in csrc)
    check("端点清单非空才强制", "cap_endpoints[rel]" in csrc)
    check("提示词要求保留已验收函数", "已验收的函数必须原样保留" in csrc)

    print("[14] 派单闸门：契约窄表不滤红灯（真跑纯函数，钉调用路径）")
    from core import company as _co
    # 场景：19 条红灯全判 dispatch；旧契约只覆盖 3 个模块（实测产物仓状态）
    _mods19 = ["apps/%s.py" % n for n in
               ("accounts", "analytics", "topics", "prefs", "matching", "rules",
                "audit", "llm", "org", "people", "security", "experiment", "data",
                "topic2", "matching2", "rules2", "analytics2", "auth2", "misc")]
    _allow19 = set(_mods19)                      # triage 全判该派
    _ev = {m: ["字段不符：qty（实际 1；期望 2）"] for m in _mods19}
    _orders = _co._cap_orders_from_red(_mods19, set(), _allow19, _ev)
    check("19 条红灯全转成工作单（不受契约窄表限制）",
          len(_orders) == 19, "得到 %d 条" % len(_orders))
    check("工作单带失败证据（含期望值）",
          all("期望 2" in o["detail"] for o in _orders))
    # triage 分诊闸门仍在：local_wiring/env 不在 allow → 不派
    _partial = _co._cap_orders_from_red(_mods19, set(), set(_mods19[:5]), _ev)
    check("triage 判 local/env 的红灯仍被拦（分诊闸门没被拆）",
          len(_partial) == 5, "得到 %d 条" % len(_partial))
    # 去重：已在手里的不重复派
    _dedup = _co._cap_orders_from_red(_mods19, {_mods19[0]}, _allow19, _ev)
    check("已有工作单不重复派", len(_dedup) == 18 and
          all(o["target"] != _mods19[0] for o in _dedup), "得到 %d 条" % len(_dedup))
    # allow=None（无分诊结果）时全派
    _noallow = _co._cap_orders_from_red(_mods19, set(), None, _ev)
    check("无分诊结果时不误杀（allow=None 全派）", len(_noallow) == 19)
    # 主路径真的调用了纯函数（不是留个没人调的工具函数）
    check("night() 主路径真调用 _cap_orders_from_red",
          "_cap_new = _cap_orders_from_red(" in csrc and
          "if _m not in contracts" not in csrc)

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（甲方模式接上 blueprint / 接线 / 端到端观测 / 红灯进工作单）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
