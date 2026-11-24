"""能力上限台账：每一项上限的"当前值"由**真实探针**现算，不许手写。

为什么非要有这一页：
    文档里的能力边界一定会过期。实测证据：`docs/25` 写"本机没装 coverage → 永远判定不了"，
    其实装了 7.17.1，真跑出来是 50%；同一页把 ¥硬上限、A→B→A 振荡、Plan Mode 硬门禁
    列在"待实现/部分"，代码里全都实现了。所以上限必须**每次现算**：
        · 探针能测 → 给数字（覆盖率、并行工人数、业务不变量条数……）
        · 探针测不了 → 如实写"测不了 + 缺什么"，**绝不当成通过**

状态词汇只有四个：
    done    机制在、这次探针也跑通了
    partial 机制在，但这次没达标 / 只做到一部分（如实给数字）
    absent  机制还没有实证（写出"要什么才算有"）
    blocked 这次测不了（写出缺什么才能测）

不用 `pytest`、不联网、不花钱：所有探针都是本地机械检查（有个别会起子进程真跑）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

DONE, PARTIAL, ABSENT, BLOCKED = "done", "partial", "absent", "blocked"

# 各档的关键门禁：用于"阻断式门禁"这一格
KEY_GATES = ("coverage", "mutation", "diff_size", "types", "lint")


def _sixcoder(ws: Path) -> Path:
    d = Path(ws) / ".sixcoder"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lines(ws: Path) -> tuple[int, int]:
    """(产物行数, 测试行数)。测试代码不算"产物规模"，但要一起报出来。

    旧实现只 `ws.glob("*.py")` —— **只看仓库根目录**，于是 `cal/core/numeric.py`
    这类包内模块一行都不算。实测（2026-11-05 自生成 round-01a 全部 20/20 通过）：
    台账记成 `lines: 0`，交付报告写着"本项目 0 行产物"，等于把真实产出抹掉了。
    现在直接复用 `scale.count_lines()`（那里已经按"一个非 __init__ 文件 = 一个模块"算对）。
    """
    try:
        from . import scale as _scale
        m = _scale.count_lines(Path(ws))
        return int(m.get("prod_lines") or 0), int(m.get("test_lines") or 0)
    except Exception:                                    # noqa: BLE001
        return 0, 0


def _read_jsonl(path: Path) -> list[dict]:
    out: list[dict] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def record_scale_evidence(ws: Path, level: str, verified: int, total: int,
                          cost_cny: float = 0.0, reason: str = "") -> dict:
    """夜班收工时记一条规模实证（同一个 task 只记一条，重复调用不叠加）。"""
    ws = Path(ws)
    prod, test = _lines(ws)
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "level": (level or "S").upper(),
           "lines": prod, "test_lines": test, "verified": int(verified), "total": int(total),
           "cost_cny": round(float(cost_cny or 0.0), 4), "reason": (reason or "")[:160]}
    path = _sixcoder(ws) / "scale_evidence.jsonl"
    rows = [r for r in _read_jsonl(path)
            if (r.get("level"), r.get("lines"), r.get("total"), r.get("verified"))
            != (rec["level"], rec["lines"], rec["total"], rec["verified"])]
    rows.append(rec)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return rec


def record_autonomy(ws: Path, level: str, verified: int, total: int, mode: str = "",
                    human_interventions: int | None = None, cost_cny: float = 0.0,
                    did_work: bool = False) -> dict:
    """记一条"自主完成"实证。

    完成口径（缺一条就不算）：造物模式 + 无人工介入 + 全部验收通过 + **真干了活**。
    为什么要有 did_work：一次"什么活都不用干"的运行（所有函数本来就已验收）证明不了
    系统能自主建成东西，把它记成自主实证就是自我表扬。
    human_interventions=None 表示"这一轮没统计到人工介入数"，同样不算。
    """
    ws = Path(ws)
    prod, _ = _lines(ws)
    rec = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "level": (level or "S").upper(),
           "mode": mode, "lines": prod, "verified": int(verified), "total": int(total),
           "human_interventions": human_interventions, "did_work": bool(did_work),
           "cost_cny": round(float(cost_cny or 0.0), 4)}
    path = _sixcoder(ws) / "autonomy.jsonl"
    rows = _read_jsonl(path)
    rows.append(rec)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                    encoding="utf-8")
    return rec


# ------------------------------------------------------------------ 探针

def _probe_business(ws: Path, contracts: dict | None, level: str) -> dict:
    if not contracts:
        return {"status": BLOCKED, "measured": "本轮没有契约，无法构造业务链",
                "blocker": "需要一个能解析出业务函数的契约文件", "next": "跑一次带契约的夜班"}
    try:
        from . import flow
        r = flow.business_flow(ws, contracts)
    except Exception as exc:                             # noqa: BLE001
        return {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                "blocker": str(exc)[:120], "next": "修业务流探针"}
    if r.get("applicable") is False:
        return {"status": BLOCKED, "measured": "这个项目没有可识别的业务链（不是失败）",
                "blocker": r.get("reason") or "契约里缺业务函数",
                "next": "给别的系统形态补业务流配方（现在只有双选系统这一条）"}
    if r.get("ok"):
        return {"status": DONE,
                "measured": f"{r.get('passed')}/{r.get('total')} 条业务不变量通过（2 套场景）",
                "blocker": "", "next": "给其它系统形态各写一条业务流"}
    return {"status": PARTIAL, "measured": f"业务不变量 {r.get('passed')}/{r.get('total')}",
            "blocker": (r.get("fails") or ["未知"])[0][:140],
            "next": "修产物或修判据（两者都不许糊弄）"}


def _probe_scale(ws: Path, contracts: dict | None, level: str) -> dict:
    prod, test = _lines(ws)
    rows = _read_jsonl(_sixcoder(ws) / "scale_evidence.jsonl")
    tiers = sorted({r.get("level") for r in rows if r.get("total")
                    and int(r.get("verified") or 0) >= int(r.get("total") or 1)})
    measured = (f"本项目 {prod} 行产物（+{test} 行测试）≈ {level} 档；"
                f"有'全部验收通过'实证的档位：{('、'.join(tiers) or '无')}")
    if level.upper() in tiers:
        return {"status": DONE, "measured": measured, "blocker": "",
                "next": "往上一个档位补一次真跑"}
    return {"status": PARTIAL, "measured": measured,
            "blocker": "当前档位还没有'全部验收通过'的实证记录",
            "next": "跑一次该档位的真项目并在收工时写入 scale_evidence.jsonl"}


def _probe_parallel(ws: Path, contracts: dict | None, level: str) -> dict:
    try:
        from . import parallel
        st = parallel.status()
    except Exception as exc:                             # noqa: BLE001
        return {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                "blocker": str(exc)[:120], "next": "修并行框架"}
    measured = (f"backend={st['backend']}，workers={st['workers']}（上限 {st['max_workers']}）；"
                f"进程池：{st['process_pool']}")
    if st["enabled"]:
        return {"status": PARTIAL, "measured": measured, "blocker": "并行已开但没有真项目实验证",
                "next": "用并行后端跑一次多模块项目并对比串行耗时/结果"}
    return {"status": PARTIAL, "measured": measured,
            "blocker": "默认串行：并行写有 429 风险、互相 import 会读到回归基线中间态，"
                       "而且夜班写模块路径还没接并行",
            "next": "把 map_calls 接进夜班的写模块路径，再做一次串行/并行对照实验"}


def _probe_coverage(ws: Path, contracts: dict | None, level: str) -> dict:
    """覆盖率这一格按 **M 档**（真正设地板的档位）报数字 —— 这才是"能力上限"。

    S 档本身不设地板（小程序硬压覆盖率会诱导写垃圾测试），按 S 跑只能得到
    "不设地板"，对"上限在哪"没有信息量，所以参考档固定 M，并注明本项目档位。
    """
    try:
        from . import arch
        r = arch.gate_coverage(ws, level="M")
    except Exception as exc:                             # noqa: BLE001
        return {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                "blocker": f"{type(exc).__name__}: {str(exc)[:100]}",
                "next": "装 coverage 或修 gate_coverage"}
    detail = str(getattr(r, "detail", "") or "")
    ref = str(getattr(r, "evidence_ref", "") or "")
    measured = f"按 M 档地板实测：{detail[:120]}（本项目 {level.upper()} 档）"
    if getattr(r, "ok", False):
        return {"status": DONE, "measured": measured, "blocker": "", "next": ""}
    if "测不了" in detail or "未安装" in detail or not ref:
        return {"status": BLOCKED, "measured": measured,
                "blocker": "覆盖率工具跑不起来",
                "next": "装 coverage 到本机 Python（pip install coverage）"}
    return {"status": PARTIAL, "measured": measured,
            "blocker": "实测覆盖率低于 M 档 80% 地板",
            "next": "补产物测试到 80%；达不到就别声明 M 档能力"}


def _probe_http(ws: Path, contracts: dict | None, level: str) -> dict:
    if not contracts:
        return {"status": BLOCKED, "measured": "本轮没有契约", "blocker": "需要契约",
                "next": "跑一次带契约的夜班"}
    try:
        from . import httpflow
        av = httpflow.available(contracts)
        if not av.get("ok"):
            return {"status": BLOCKED, "measured": "这个项目没有 Web 层（不是失败）",
                    "blocker": av.get("reason") or "", "next": ""}
        r = httpflow.e2e(ws, contracts)
    except Exception as exc:                             # noqa: BLE001
        return {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                "blocker": str(exc)[:120], "next": "修 HTTP 端到端探针"}
    if r.get("ok"):
        return {"status": DONE,
                "measured": f"真起服务真发请求 {r.get('passed')}/{r.get('total')} 项通过",
                "blocker": "", "next": ""}
    return {"status": PARTIAL, "measured": f"{r.get('passed')}/{r.get('total')} 项通过",
            "blocker": (r.get("fails") or ["未知"])[0][:140],
            "next": "修 Web 层或修判据"}


def _probe_dataflow(ws: Path, contracts: dict | None, level: str) -> dict:
    try:
        from . import dataflow
        g = dataflow.graph(ws)
    except Exception as exc:                             # noqa: BLE001
        return {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                "blocker": str(exc)[:120], "next": "修数据流探针"}
    return {"status": PARTIAL,
            "measured": (f"来源链可用：{g.get('files')} 文件 / {g.get('defined')} 定义 / "
                         f"{g.get('calls')} 调用点，{g.get('unresolved')} 个无定义（无证据）"),
            "blocker": "只做参数来源链，**没有污点追踪**（不跟踪传播与净化点）",
            "next": "要污点追踪得先有 IR + 传播规则，否则只会产出看似专业的假结论"}


def _probe_blocking(ws: Path, contracts: dict | None, level: str) -> dict:
    try:
        from . import scale
        modes = {g: scale.gate_mode(g, level) for g in KEY_GATES}
    except Exception as exc:                             # noqa: BLE001
        return {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                "blocker": str(exc)[:120], "next": "修 scale.gate_mode"}
    advisory = sorted(g for g, m in modes.items() if m != "block")
    measured = "；".join(f"{g}={m}" for g, m in modes.items())
    if not advisory:
        return {"status": DONE, "measured": measured, "blocker": "", "next": ""}
    return {"status": PARTIAL, "measured": measured,
            "blocker": "本档仍为 advisory（只报告不阻断）：" + "、".join(advisory),
            "next": "抬到更高档位时把这些门禁改成 block，并用自检钉住'不达标必须红'"}


def _probe_autonomy(ws: Path, contracts: dict | None, level: str) -> dict:
    rows = _read_jsonl(_sixcoder(ws) / "autonomy.jsonl")
    # 严口径：人工介入数必须是真实的 0、必须真干过活、必须全部验收通过。
    # 老记录没有 did_work 字段 → 不算（宁可承认没有，也不认领一次空跑）。
    good = [r for r in rows if r.get("human_interventions") == 0
            and r.get("did_work") is True
            and int(r.get("total") or 0) > 0
            and int(r.get("verified") or 0) >= int(r.get("total") or 1)]
    if good:
        last = good[-1]
        return {"status": DONE,
                "measured": (f"有 {len(good)} 条自主完成实证，最近一条 "
                             f"{last.get('verified')}/{last.get('total')}、"
                             f"{last.get('lines')} 行、¥{last.get('cost_cny')}"),
                "blocker": "", "next": "补更高档位/更多形态的自主实证"}
    return {"status": ABSENT,
            "measured": (f"自主完成实证 {len(good)} 条（口径：人工介入=0 + 真干过活 + 全部验收通过，"
                         f"当前记录 {len(rows)} 条）"),
            "blocker": "没有'完全自主'的实证：需要一次造物模式、人工介入为 0、真正写了/改了模块、"
                       "全部验收通过、花费在预算内的完整运行",
            "next": "跑一次这样的夜班并写入 autonomy.jsonl（三个条件都必须是真的）"}


PROBES = (
    ("business", "业务正确性（业务流不变量）", _probe_business),
    ("scale", "规模档位实证", _probe_scale),
    ("parallel", "并行（多核/多进程）", _probe_parallel),
    ("coverage", "测试覆盖率地板", _probe_coverage),
    ("http", "真实 HTTP 端到端", _probe_http),
    ("dataflow", "数据流/污点追踪", _probe_dataflow),
    ("blocking", "门禁是阻断还是只报告", _probe_blocking),
    ("autonomy", "完全自主的实证", _probe_autonomy),
)


def probe_all(ws: Path, contracts: dict | None = None, level: str = "S") -> dict:
    """把 8 项上限全跑一遍。任何探针异常都退化成本项的 blocked，不许整页崩掉。"""
    ws = Path(ws)
    rows: list[dict] = []
    for cid, name, fn in PROBES:
        try:
            r = fn(ws, contracts, level)
        except Exception as exc:                         # noqa: BLE001
            r = {"status": BLOCKED, "measured": f"探针异常：{type(exc).__name__}",
                 "blocker": (f"探针自身异常（不是产物问题）：{type(exc).__name__}: "
                             f"{str(exc)[:120]}"),
                 "next": "修这一格的探针"}
        rows.append({"id": cid, "name": name, **r})
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in (DONE, PARTIAL, ABSENT, BLOCKED)}
    prod, test = _lines(ws)
    return {"ok": counts[DONE] == len(rows), "level": level.upper(), "rows": rows,
            "counts": counts, "lines": prod, "test_lines": test,
            "at": time.strftime("%Y-%m-%d %H:%M:%S")}


_STATUS_LABEL = {DONE: "已达上限", PARTIAL: "部分", ABSENT: "还没有", BLOCKED: "测不了"}


def report_lines(res: dict) -> list[str]:
    """交付报告用的人话版：每一格都写清'多少'与'差什么'。"""
    if not res or not res.get("rows"):
        return ["- 能力上限台账：本轮没有计算（不是通过，也不是失败）"]
    c = res.get("counts") or {}
    out = ["", f"### 能力上限台账（{res.get('at')} 现算，{res.get('lines')} 行产物）", "",
           f"- 汇总：已达上限 {c.get(DONE, 0)}｜部分 {c.get(PARTIAL, 0)}｜"
           f"还没有 {c.get(ABSENT, 0)}｜测不了 {c.get(BLOCKED, 0)}", ""]
    for r in res["rows"]:
        out.append(f"- **{r['name']}**：{_STATUS_LABEL.get(r['status'], r['status'])} —— "
                   f"{r.get('measured') or ''}")
        if r.get("blocker"):
            out.append(f"  - 差在哪：{r['blocker']}")
        if r.get("next"):
            out.append(f"  - 下一步：{r['next']}")
    out.append("")
    out.append("> 这一页每次运行由探针现算；'测不了'不等于通过，'部分'必须带数字。")
    return out
