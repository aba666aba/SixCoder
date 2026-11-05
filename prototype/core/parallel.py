"""并行框架（骨架）：先把"能并行"和"敢并行"分开说清楚，再谈打不打开它。

现状（诚实，每次现算，不写死）：
    · 夜班**写模块是串行的**，按依赖序一个一个写；
    · 这个模块提供并行执行能力，但**默认关闭**，因为并行写有两个已知风险：
        1) 远端 API 有并发/速率上限，盲目并发会 429；
        2) 同一批模块可能互相 import，并行写会让"回归基线"在写了一半时被读到；
    · 所以 status() 永远如实返回 {enabled, workers, backend, reason}；
      没有 SIXCODER_PARALLEL 时 tasks 一律串行，行为与老代码**完全一致**。

不做什么（免得被当成"已经支持多进程"）：
    · 没有进程池消费者。真正的 CPU 密集活（门禁、编译、pytest）本来就以子进程方式跑，
      再套一层 ProcessPool 只会放大开销、还要反复 pickle 工作区路径。
      所以 backend 只有 "serial" 与 "thread" 两种，不存在 process 后端。

用法（唯一入口，串行/并行行为一致）：
    from . import parallel
    res = parallel.map_calls(rels, lambda rel: author_module(...))
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

# 远端 API 并发上限：拍的值，超过这个并发 429 概率显著上升（见 docs/23 八档阶梯）
MAX_WORKERS = 8


def workers() -> int:
    """并发工人数（1 = 串行）。环境变量非法时按 1 处理，绝不悄悄放大。"""
    raw = (os.environ.get("SIXCODER_PARALLEL") or "").strip()
    if not raw:
        return 1
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, MAX_WORKERS))


def enabled() -> bool:
    return workers() > 1


def status() -> dict:
    """当前并行能力状态：调用方（交付报告、上限台账）只用这一个函数，不看环境变量。"""
    n = workers()
    return {"enabled": n > 1, "workers": n, "max_workers": MAX_WORKERS,
            "backend": "thread" if n > 1 else "serial",
            "process_pool": "未接（CPU 密集活本来就走子进程，再套进程池只是放大开销）",
            "reason": ("" if n > 1 else
                       "默认串行：并行写模块有 429 风险，且互相 import 的模块会让回归基线读到中间态；"
                       "要并行请显式设 SIXCODER_PARALLEL=N（N 会被夹到 1..%d）" % MAX_WORKERS)}


def map_calls(items, fn, workers_n: int | None = None) -> dict:
    """把一串**互相独立**的调用跑完，返回 {ok, results, order, backend, errors}。

    · 串行与并行返回结构完全相同（order 永远等于输入顺序）；
    · 某个 item 抛异常不会炸掉整批：进 errors，其余照常跑完（诚实记录，不吞）。
    """
    seq = list(items)
    n = workers_n if workers_n is not None else workers()
    n = max(1, min(int(n), MAX_WORKERS))
    results: dict = {}
    errors: dict = {}
    done_order: list = []

    def one(item):
        try:
            return item, fn(item), ""
        except Exception as exc:                              # noqa: BLE001
            return item, None, f"{type(exc).__name__}: {exc}"

    if n <= 1 or len(seq) <= 1:
        for item in seq:
            it, val, err = one(item)
            if err:
                errors[it] = err
            else:
                results[it] = val
            done_order.append(it)
        backend = "serial"
    else:
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=n) as pool:
            for it, val, err in pool.map(one, seq):
                if err:
                    errors[it] = err
                else:
                    results[it] = val
                with lock:
                    done_order.append(it)
        backend = "thread"
    return {"ok": not errors, "results": results, "errors": errors,
            "order": done_order, "backend": backend, "workers": n}


def report_lines(res: dict) -> list[str]:
    st = status()
    if not st["enabled"]:
        return ["- 并行：**未启用**（串行）；" + st["reason"]]
    return [f"- 并行：{res.get('backend')} × {res.get('workers')} 个工人跑 {len(res.get('order') or [])} 项"
            + ("" if res.get("ok") else f"；有 {len(res.get('errors') or {})} 项失败")]
