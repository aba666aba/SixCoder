"""自检：夜班只有**一条**主路径（新：判型→供料→整仓→验证）。

为什么需要这条自检（2026-11-15 用户定稿"砍掉与现有思路不同的"）：
引擎里曾经并存两条夜班路径 —— `auto.night_auto`（切片式，1100 行）和
`company.night`（轮式）。两者都能跑，于是"谁决定改哪里"有两套答案，
实测出现"订单算了六层筛选、施工时又按另一套挑文件"。
现在主路径收敛成 `core/night.py` 一条，本自检把这条口径钉死：
  · `cli night` 默认必须调 `night.run`
  · 只有架构问答类参数才允许回落 `night_auto`，且必须打印 NOTE
  · `night.run` 不许碰切片（不 import slicer）
  · `night.run` 必须把**锁定的全量契约**交给 company.night
  · 整仓会话成功后必须「重新接线 + 全量重测」（否则改坏别的模块看不见）

跑法: python checks/check_night_path.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE.parent
sys.path.insert(0, str(PROTO))

from core import company, fullgen, night                            # noqa: E402

CLI = (PROTO / "cli.py").read_text(encoding="utf-8-sig", errors="replace")
NIGHT_SRC = inspect.getsource(night)
COMPANY_SRC = (PROTO / "core" / "company.py").read_text(encoding="utf-8-sig")

FAILS: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  PASS ", name)
    else:
        print("  FAIL ", name, "|", str(detail)[:300])
        FAILS.append(name)


def main() -> int:
    print("[1] 主路径唯一：cli night 默认走 night.run")
    ck("cli 里出现了 night.run 调用", "_night_mod.run(" in CLI or "night.run(" in CLI)
    ck("night_auto 仍保留（只做回落）", "night_auto(" in CLI)
    ck("回落前打印 NOTE（不许静默走旧路）", "_need_legacy" in CLI and "NOTE" in CLI)
    ck("回落条件是架构问答类参数",
       all(k in CLI for k in ("clarify", "interview", "spec_sample")), "")

    print()
    print("[2] 新路径不含切片（切片会把整仓视野切碎）")
    ck("night 模块不 import slicer", "slicer" not in NIGHT_SRC)
    ck("night 模块不调用 contract_for_slice", "contract_for_slice" not in NIGHT_SRC)
    ck("night 只有四个阶段函数调用",
       all(k in NIGHT_SRC for k in ("decide_mode", "generate_contract_by_domain",
                                    "_lock_contract", "_co.night")))

    print()
    print("[3] 供料：把**锁定的全量契约**交给公司循环")
    ck("调用 company.night 时传 contract_file", "contract_file=" in NIGHT_SRC)
    ck("用的是 fullgen 的锁定文件名", "CONTRACT_LOCK" in NIGHT_SRC)
    ck("契约锁定失败不炸（如实记录）", "契约锁定跳过" in NIGHT_SRC)

    print()
    print("[4] 整仓会话后的两条善后（改坏了要能看见）")
    ck("成功后清空已观测集合（强制全量重测）", "cap_done.clear()" in COMPANY_SRC)
    ck("成功后重新接线", "_oh_w.wire(" in COMPANY_SRC)
    ck("整仓开关可关（应急）", 'SIXCODER_WHOLE' in COMPANY_SRC)

    print()
    print("[5] 归因不重复花钱")
    ck("整仓模式下跳过独立架构师调用",
       "跳过独立架构师调用" in COMPANY_SRC)

    print()
    print("[6] 旧路径已冻结（有明确标记，别再往里加功能）")
    auto_src = (PROTO / "core" / "auto.py").read_text(encoding="utf-8-sig")
    ck("night_auto 文档里标了已冻结", "已冻结（2026-11-15）" in auto_src)
    ck("说明只做回落用途", "只在 `cli night` 收到架构问答类参数" in auto_src)

    print()
    print("[7] 判型仍可用（空项目 / 已有项目）")
    ck("decide_mode 存在且被 night 调用", callable(fullgen.decide_mode))
    # 2026-11-24 修：原来拿 PROTO/_sandbox/whole_try 当"多模块"样本，
    # 但 _sandbox/ 是 .gitignore 里的本地副产物（判据标定用的对照实现），
    # 清理仓库时被删掉 -> 这个断言永远失败（实测：单独跑 3 次全 exit=1）。
    #
    # 自检不该依赖"本地正好存在的目录"。这里现造一个临时目录：
    # 空目录判 scratch、3 个模块判 repair（阈值是 SCRATCH_MIN_MODULES=3）。
    import tempfile as _tf
    with _tf.TemporaryDirectory(prefix="np_") as _td:
        _empty = Path(_td) / "empty"
        _empty.mkdir()
        ck("空目录 → scratch", fullgen.decide_mode(_empty) == "scratch")

        _multi = Path(_td) / "multi"
        for _i in range(fullgen.SCRATCH_MIN_MODULES):
            _f = _multi / "app" / ("mod%d.py" % _i)
            _f.parent.mkdir(parents=True, exist_ok=True)
            _f.write_text("VALUE = %d\n" % _i, encoding="utf-8")
        _got = fullgen.decide_mode(_multi)
        ck("多模块 → repair", _got == "repair", "actual=" + _got)

    print()
    print("[8] 公司循环仍能写交付报告（异常也不许没有交付物）")
    src = inspect.getsource(company.night)
    ck("night 包了 try/except 兜底", "except Exception" in src)
    ck("NightResult 仍是返回类型", "NightResult" in src)

    print()
    print("[9] 收敛叫人必须真停（不许把 rc=0 当成跑完再来一遍）")
    ck("NightResult 带 need_human 字段（默认空）",
       hasattr(company.NightResult(True), "need_human")
       and company.NightResult(True).need_human == "")
    ck("振荡/停滞/预算/配置四处收工都写叫人标记",
       COMPANY_SRC.count("need_human = reason") >= 4
       and "检测到振荡" in COMPANY_SRC)
    ck("收工把标记带进 NightResult", "need_human=need_human" in COMPANY_SRC)
    ck("night_end 事件带叫人原因（日志可查）", "need_human" in NIGHT_SRC)
    ck("cli 叫人退出码 42，两条 night 分支都映射",
       "HUMAN_STOP_RC = 42" in CLI and CLI.count("sys.exit(HUMAN_STOP_RC)") == 2)

    print()
    print("[10] 已绿能力契约供料接进整仓报告（调用路径钉子）")
    _i = COMPANY_SRC.find("_whole.report_from_state(")
    _call = COMPANY_SRC[_i:_i + 340] if _i >= 0 else ""
    ck("整仓报告传 ws 进去（否则契约供料恒不触发）",
       "ws=ws" in _call and "passed=_passed" in _call, _call[:160])
    ck("报告里给已绿能力列行为断言与命门文件",
       "cap_contract_hint" in (PROTO / "core" / "wholerepo.py").read_text(
           encoding="utf-8") and "命门文件" in
       (PROTO / "core" / "wholerepo.py").read_text(encoding="utf-8"))

    print()
    print("[11] stall 判定看业务事实：端到端 done 涨就不算停滞（2026-11-18 血泪）")
    # 坑：verified 恒 6 不动、端到端 done 43→55 一路涨，却因"连续3轮无增长"收工。
    ck("done_count_of 从 cov['done'] 取数",
       company.done_count_of({"done": ["a", "b", "c"]}) == 3)
    ck("无观测/空 cov → None（不是 0）",
       company.done_count_of(None) is None and company.done_count_of({}) is None)
    ck("观测跳过（无 done 字段）→ None，绝不退化成 0",
       company.done_count_of({"caps": {}}) is None)
    ck("verified 涨 → 算增长", company.progress_grew(7, 6, None, None) is True)
    ck("**done 涨但 verified 不动 → 也算增长（本轮误判的根因）**",
       company.progress_grew(6, 6, 55, 43) is True)
    ck("两条都不涨 → 不算增长", company.progress_grew(6, 6, 55, 55) is False)
    ck("done 未知时退回只看 verified（不把没测到当停滞）",
       company.progress_grew(6, 6, None, 43) is False
       and company.progress_grew(7, 6, None, 43) is True)
    ck("done 回退（回归）不算增长", company.progress_grew(6, 6, 50, 55) is False)
    _inner = inspect.getsource(company._night_inner)
    ck("_night_inner 真调用 progress_grew（不是函数存在没人用）",
       "progress_grew(" in _inner)
    ck("stall 判定处同时取 done_count_of（接上业务事实）",
       "done_count_of(cap_cov)" in _inner and "prev_done" in _inner)

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过（主路径唯一 / 无切片 / 契约锁定 / 两条善后 / 旧路径冻结）")
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
