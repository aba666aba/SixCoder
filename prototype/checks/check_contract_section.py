"""契约解析：示例必须按**所在小节**归属，不能按函数名乱认领（2026-11-09 夜班卡死）。

事故：夜班在 `contract_check` 直接 stop，reason = "契约里有需要甲方拍板的矛盾"：
    app/api/topics.py::create_topic_draft 同一组参数出现两个期望值：… vs …(多 status/source)

真相：契约**自洽**——api 版约定"不含 status/source"，services 版约定"含 status/source"，
两个同名函数在不同模块。旧实现按"第一个同名函数"认领全篇示例 →
api 拿到两条（一条来自 services 节）→ 契约自查误判矛盾 → **夜班拒绝开跑，0 产出**，
远端一次都没被调用。

跑法: python checks/check_contract_section.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contract as cm  # noqa: E402
from core import contractcheck  # noqa: E402

FAILS: list[str] = []

TEXT = """1) app/api/topics.py：对外接口；兼容旧调用，返回不含 status/source 的草稿视图。
   - create_topic_draft(title: str, quota: int) -> dict：兼容函数。

示例（必须满足）：
create_topic_draft("题", 2) == {"title": "题", "quota": 2}

2) app/services/topics.py：领域逻辑；返回含 status/source 的完整草稿视图。
   - create_topic_draft(title: str, quota: int) -> dict：领域版。

示例（必须满足）：
create_topic_draft("题", 2) == {"title": "题", "quota": 2, "status": "draft", "source": "manual"}

3) app/api/other.py：另一个模块，不含同名函数。
   - other(x: int) -> int：别的函数。

示例（必须满足）：
other(1) == 1
"""


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    contracts = cm.parse_requirement(TEXT)
    check("解析出 3 个模块", len(contracts) == 3, str(list(contracts)))

    api = contracts.get("app/api/topics.py")
    svc = contracts.get("app/services/topics.py")
    other = contracts.get("app/api/other.py")
    check("三个模块都在", api is not None and svc is not None and other is not None)

    api_ex = list(getattr(api.by_name("create_topic_draft"), "examples", []) or [])
    svc_ex = list(getattr(svc.by_name("create_topic_draft"), "examples", []) or [])
    check("api 版只有 1 条示例（没被 services 的示例污染）", len(api_ex) == 1, str(len(api_ex)))
    check("services 版拿到自己那 1 条示例（不再被抢走）", len(svc_ex) == 1, str(len(svc_ex)))
    if api_ex:
        check("api 版期望值不含 status", "status" not in str(api_ex[0].expect), str(api_ex[0].expect))
    if svc_ex:
        check("services 版期望值含 status", "status" in str(svc_ex[0].expect), str(svc_ex[0].expect))
    oth_ex = list(getattr(other.by_name("other"), "examples", []) or [])
    check("别的模块的示例没跑偏", len(oth_ex) == 1, str(len(oth_ex)))

    cc = contractcheck.check(contracts)
    check("契约自查不再误判矛盾", not cc["hard"], str(cc["hard"][:2]))
    check("整体 ok", bool(cc.get("ok", not cc["hard"])))

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（示例按小节归属 / 跨模块同名函数不互相污染）")
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
