"""治本：收工判定看业务事实，不看记账（2026-11-11）。

症状串（同一天三轮）：
  · 10:16「模块契约已全过，但端到端还有 56 项能力未通过：**没有可执行的工作**」
  · 10:58 同样一句话再次出现
  · 产物 notes 连续「片内自验：修好 0 条，仍红 4 条（空转）」

**根因不是"某一行写错了"，是结构问题**：
收工判定的依据是 `stats["modules"]`（**记账**），而记账是**实现细节** ——
`_write_one` 是个千行嵌套函数，有 7 个 `return` 出口，记账（append）与业务逻辑**交织**。
任何一条新增路径/异常分支漏了 append，引擎就会把
「还有 56 项端到端红灯、契约层还欠活」读成「没有活干」→ **收工**。

**治本（两层）**：
  ① **兜底记账**：派工结束后按"本轮实际派过的模块"核对一遍，谁没留下痕迹就补一条
     —— 保证"派过工"这件事**一定在账上**（不再依赖每个 return 分支都记得 append）。
  ② **收工判定换依据**：用**业务事实**（端到端红灯数 / 契约层欠活），
     不用记账。**有活干 → 永远不许因为"记账空"收工**；
     记账真丢了就发 `accounting_lost` 报警并换打法继续。

只补 append 是治标（下一个 return 分支照样漏）；只改判定是治标（账还是缺）；
**两个一起做**才治本：账不缺 + 判定不看账。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import company  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    import inspect
    import re

    src = inspect.getsource(company)

    print("[1] 兜底记账：派工后按实际派过的模块核对")
    check("派工后有核对逻辑（本轮派过但没记账的补一条）",
          "accounting_lost" in src,
          "没有兜底 → 下一个 return 分支照样漏记账")
    m = re.search(r"_par_res\s*=\s*_par_mod\.map_calls\(mods,\s*_write_one\)(.{0,3000}?)"
                  r"accounting_lost", src, re.S)
    check("兜底发生在 map_calls **之后**（拿到本轮派工结果才核）", bool(m),
          "兜底位置不对 → 核对不到本轮派了什么")
    check("兜底按 mods（实际派过的模块）核对",
          re.search(r"for _rel in \(mods or \[\]\)", src) is not None, "")

    print("[2] 收工判定看业务事实，不看记账")
    i_stop = src.find('if not stats["modules"]')
    assert i_stop > 0
    # 业务事实的判定块插在收工分支**之前**（先判"还有活干"→ continue），
    # 所以扫描窗口要往前扩。
    seg = src[max(0, i_stop - 3000):i_stop + 2600]
    check("收工分支里先判『还有没有活干』（端到端红灯 / 契约欠活）",
          ("_red_left" in seg) or ("cap_red" in seg),
          "收工判定没有业务事实依据")
    check("『有活干』时不收工（记账空 ≠ 没活干）",
          "accounting_lost" in seg and "continue" in seg,
          "还是会在有活干时收工")
    check("业务事实用契约层欠活（needs_work_state）也参与判断",
          "needs_work_state" in seg, "只看端到端红灯会漏掉契约层欠活的情况")

    print("[3] 断点续跑判据：端到端红的模块不算 done（2026-11-11 16:00 空转死循环）")
    from core import project as _pr
    check("resume_plan 有 red_modules 参数",
          "red_modules" in inspect.signature(_pr.resume_plan).parameters,
          str(inspect.signature(_pr.resume_plan)))
    try:
        _c = {"app/services/topics.py": {"funcs": 2, "verified": 2},
              "app/api/batches.py": {"funcs": 1, "verified": 1}}
        _s = {"app/services/topics.py": {"funcs": 2, "verified": 2},
              "app/api/batches.py": {"funcs": 1, "verified": 1}}
        _p0 = _pr.resume_plan(Path("."), _c, _s)
        _p1 = _pr.resume_plan(Path("."), _c, _s,
                               red_modules={"app/services/topics.py"})
        check("端到端红的模块不再算 done（必须回到待写队列）",
              "app/services/topics.py" not in (_p1["done"] or []),
              "不传=%s / 传=%s" % (_p0["note"], _p1["note"]))
        check("端到端红的模块出现在待写里（resume 或 fresh）",
              "app/services/topics.py" in _p1["next"]
              or any(x["path"] == "app/services/topics.py" for x in _p1["resume"])
              or "app/services/topics.py" in [_f for _f in _p1["fresh"]],
              str(_p1))
    except Exception as _e:                          # noqa: BLE001
        check("resume_plan 可调用", False, "%s: %s" % (type(_e).__name__, _e))
    check("company 调 resume_plan 时传了端到端红灯",
          "red_modules=cap_red" in inspect.getsource(company),
          "不传红灯 → 契约全过就说'全部完成' → 一轮都不写码 → 只空转")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
