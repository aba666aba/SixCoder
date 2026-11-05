"""并行框架自检：默认必须串行、打开后结果必须一致、且不许假装有进程池。

这条钉的是"能力上限"里的并行那一格：
    · 默认关闭 → status() 必须说清楚原因，report_lines 不许写成"并行已启用"；
    · 打开后 → 结果必须与串行**逐项一致**（并行只该省时间，不该改结果）；
    · 某项抛异常 → 进 errors，其余照常跑完，不许整批静默炸掉；
    · 环境变量非法/越界 → 夹到合法范围，绝不悄悄放大并发。

跑法: python checks/check_parallel.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import parallel  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _set(env: str | None) -> None:
    if env is None:
        os.environ.pop("SIXCODER_PARALLEL", None)
    else:
        os.environ["SIXCODER_PARALLEL"] = env


def main() -> int:
    print("并行框架自检")

    print("[1] 默认必须是串行，且把原因说清楚")
    _set(None)
    st = parallel.status()
    check("默认不启用", st["enabled"] is False, str(st))
    check("默认工人数为 1", st["workers"] == 1, str(st["workers"]))
    check("默认 backend 是 serial", st["backend"] == "serial", st["backend"])
    check("reason 写清了为什么默认关", "429" in st["reason"] or "串行" in st["reason"], st["reason"])
    check("report 行如实写'未启用'", "未启用" in parallel.report_lines({})[0],
          parallel.report_lines({})[0])
    check("如实声明没有进程池后端", "未接" in st["process_pool"], st["process_pool"])

    print("[2] 环境变量越界/非法 → 夹到合法范围")
    for raw, want in (("99", parallel.MAX_WORKERS), ("0", 1), ("-3", 1), ("abc", 1), ("", 1)):
        _set(raw)
        check(f"SIXCODER_PARALLEL={raw!r} → workers={want}",
              parallel.workers() == want, str(parallel.workers()))
    _set(None)

    print("[3] 串行与并行结果必须逐项一致")
    items = [f"m{i}" for i in range(5)]
    idents: dict[str, int] = {}

    def work(item: str) -> dict:
        time.sleep(0.05)
        idents[item] = threading.get_ident()
        return {"item": item, "len": len(item)}

    serial = parallel.map_calls(items, work, workers_n=1)
    serial_ids = dict(idents)
    idents.clear()
    para = parallel.map_calls(items, work, workers_n=4)
    para_ids = dict(idents)
    check("串行 backend=serial", serial["backend"] == "serial", serial["backend"])
    check("并行 backend=thread", para["backend"] == "thread", para["backend"])
    check("结果逐项一致", serial["results"] == para["results"],
          f"{serial['results']} vs {para['results']}")
    check("顺序都等于输入顺序", serial["order"] == items and para["order"] == items,
          f"{serial['order']} / {para['order']}")
    check("串行真的只用主线程干活", len(set(serial_ids.values())) == 1, str(set(serial_ids.values())))
    check("并行真的用了多个线程（否则并行是假的）",
          len(set(para_ids.values())) > 1, str(set(para_ids.values())))

    print("[4] 单项异常不许炸掉整批")
    def boom(item: str) -> str:
        if item == "m2":
            raise RuntimeError("故意失败")
        return item.upper()

    r = parallel.map_calls(items, boom, workers_n=3)
    check("整体标记失败", r["ok"] is False, str(r["ok"]))
    check("失败项进 errors 并带类型", "m2" in r["errors"] and "RuntimeError" in r["errors"]["m2"],
          str(r["errors"]))
    check("其余项照常跑完", sorted(r["results"]) == ["m0", "m1", "m3", "m4"], str(sorted(r["results"])))

    print("[5] 打开时报告行写清后端与并发数")
    _set("4")
    lines = parallel.report_lines({"backend": "thread", "workers": 4, "order": items, "ok": True})
    check("报告写明 thread × 4", "thread" in lines[0] and "4" in lines[0], lines[0])
    _set(None)

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（默认串行 / 夹取合法 / 结果一致 / 异常不炸批 / 报告如实）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
