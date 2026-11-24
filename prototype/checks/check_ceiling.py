"""能力上限台账自检：状态必须由探针现算，测不了必须说测不了。

这条钉的是"能力上限"这一页本身的可信度：
    · 8 格必须齐全，顺序稳定（交付报告按它排）；
    · 每一格都必须给 measured；不是 done 就必须给"差在哪 / 下一步"——
      只写"部分"不给数字、不给原因，就是新的表演；
    · 台账不许手写：写在 ledger 里的证据要能**改变**探针结论
      （记一条规模实证 → scale 从 partial 变 done；记一条自主实证 → autonomy 从 absent 变 done）；
    · 诚实：人工介入数未知（None）的记录**不算**自主实证；
    · 稳：任何一格探针炸了只让那一格 blocked，整页不许崩。

跑法: python checks/check_ceiling.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import ceiling  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

IDS = ["business", "scale", "parallel", "coverage", "http", "dataflow", "blocking", "autonomy"]

SAMPLE = '''def add(a: int, b: int) -> int:
    """两数相加。"""
    return a + b
'''


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws(files: dict | None = None) -> Path:
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix="ceiling_"))
    for name, src in (files or {"calc.py": SAMPLE}).items():
        (ws / name).write_text(src, encoding="utf-8")
    return ws


def _row(res: dict, cid: str) -> dict:
    return next((r for r in res.get("rows") or [] if r.get("id") == cid), {})


def main() -> int:
    print("能力上限台账自检")

    print("[1] 8 格齐全、顺序稳定，且每格都说得清")
    ws = _ws()
    res = ceiling.probe_all(ws, contracts=None, level="S")
    rows = res.get("rows") or []
    check("格子数量是 8", len(rows) == 8, str(len(rows)))
    check("顺序稳定（交付报告按它排）", [r.get("id") for r in rows] == IDS,
          str([r.get("id") for r in rows]))
    check("汇总计数之和等于 8", sum((res.get("counts") or {}).values()) == 8,
          str(res.get("counts")))
    bad_measured = [r["id"] for r in rows if not str(r.get("measured") or "").strip()]
    check("每格都有 measured（不许只写状态）", not bad_measured, str(bad_measured))
    bad_why = [r["id"] for r in rows
               if r["status"] != ceiling.DONE
               and (not str(r.get("blocker") or "").strip() or not str(r.get("next") or "").strip())]
    check("不是 done 的格子都有'差在哪 + 下一步'", not bad_why, str(bad_why))
    check("没有契约时 business 判 blocked（不是通过）",
          _row(res, "business").get("status") == ceiling.BLOCKED,
          str(_row(res, "business")))
    check("没有 Web 层时 http 判 blocked（不是通过）",
          _row(res, "http").get("status") == ceiling.BLOCKED, str(_row(res, "http")))
    check("没有自主实证时 autonomy 判 absent",
          _row(res, "autonomy").get("status") == ceiling.ABSENT, str(_row(res, "autonomy")))
    check("并行默认串行 → parallel 只能是 partial（不许写 done）",
          _row(res, "parallel").get("status") == ceiling.PARTIAL, str(_row(res, "parallel")))

    print("[2] 台账不许手写：写证据必须能改变结论")
    check("写之前 scale 是 partial", _row(res, "scale").get("status") == ceiling.PARTIAL,
          str(_row(res, "scale")))
    ceiling.record_scale_evidence(ws, "S", verified=1, total=1, cost_cny=0.0, reason="自检样本")
    res2 = ceiling.probe_all(ws, contracts=None, level="S")
    check("写一条'全部验收通过'的规模实证 → scale 变 done",
          _row(res2, "scale").get("status") == ceiling.DONE, str(_row(res2, "scale")))
    check("规模实证里带真实行数", int((res2.get("lines") or 0)) >= 1, str(res2.get("lines")))

    ceiling.record_autonomy(ws, "S", verified=1, total=1, mode="甲方", human_interventions=None,
                            did_work=True)
    res3 = ceiling.probe_all(ws, contracts=None, level="S")
    check("人工介入数未知(None)的记录**不算**自主实证",
          _row(res3, "autonomy").get("status") == ceiling.ABSENT, str(_row(res3, "autonomy")))
    ceiling.record_autonomy(ws, "S", verified=1, total=1, mode="甲方", human_interventions=0,
                            did_work=False)
    res3b = ceiling.probe_all(ws, contracts=None, level="S")
    check("**没真干活**（did_work=False）的记录不算自主实证 —— 空跑不许自我表扬",
          _row(res3b, "autonomy").get("status") == ceiling.ABSENT, str(_row(res3b, "autonomy")))
    ceiling.record_autonomy(ws, "S", verified=1, total=1, mode="甲方", human_interventions=0,
                            did_work=True)
    res4 = ceiling.probe_all(ws, contracts=None, level="S")
    check("人工介入=0 且真干活且全绿 → autonomy 变 done",
          _row(res4, "autonomy").get("status") == ceiling.DONE, str(_row(res4, "autonomy")))
    ceiling.record_autonomy(ws, "S", verified=0, total=3, mode="甲方", human_interventions=0,
                            did_work=True)
    res5 = ceiling.probe_all(ws, contracts=None, level="S")
    check("验收没全绿的那条不算（不能被上一条带绿）",
          _row(res5, "autonomy").get("status") == ceiling.DONE,  # 还有前一条合格的
          str(_row(res5, "autonomy")))

    print("[3] 一格炸了不许整页崩")
    saved = ceiling.PROBES
    def _boom(ws_, c_, lv_):
        raise RuntimeError("故意炸")
    try:
        ceiling.PROBES = tuple((cid, nm, (_boom if cid == "dataflow" else fn))
                               for cid, nm, fn in saved)
        res6 = ceiling.probe_all(ws, contracts=None, level="S")
        check("炸掉的那格变 blocked", _row(res6, "dataflow").get("status") == ceiling.BLOCKED,
              str(_row(res6, "dataflow")))
        check("其余格照常出结论", len(res6.get("rows") or []) == 8, str(len(res6.get("rows") or [])))
        check("blocked 里写明了异常类型", "RuntimeError" in str(_row(res6, "dataflow").get("blocker")),
              str(_row(res6, "dataflow")))
    finally:
        ceiling.PROBES = saved

    print("[4] 交付报告那一页要人话、且标明'测不了≠通过'")
    lines = ceiling.report_lines(res4)
    text = "\n".join(lines)
    check("有汇总行", "汇总" in text, text[:160])
    for cid in IDS:
        name = next(n for i, n, _ in ceiling.PROBES if i == cid)
        check(f"报告里有『{name}』这一格", name in text, "")
    check("写明'测不了不等于通过'", "不等于通过" in text, "")
    check("没有台账时如实写'没计算'",
          "没有计算" in ceiling.report_lines({})[0], ceiling.report_lines({})[0])

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（8 格齐全 / 状态由证据驱动 / 测不了如实说 / 一格炸不崩全页）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
