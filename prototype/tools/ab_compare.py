"""A/B 对照实验：**我们这套系统** vs **纯 API 一次写完**。

同一份需求、同一个模型（flash:none）、同一个空仓库起点，只改"有没有 harness"：

    A 系统臂：空仓库 → 契约（远端翻译+自查）→ 整文件生成 → 机械判据 → 窄证据重试 → 升档
    B API 臂：空仓库 → **一次** API 调用让它把整个项目写出来（没有判据、没有重试、没有反馈）

判定用**同一套机械判据**（契约示例 / 边界矩阵 / 签名 / 编译 / pytest），
判据用的契约由本脚本单独合成一次，成本记在"裁判"名下，不算进任何一臂。

跑法：python tools/ab_compare.py            # 两个仓库都跑
      python tools/ab_compare.py --judge-only projects/ab_api   # 只判已有仓库
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import contract as cm            # noqa: E402
from core import contractcheck, contractgen, iterate, remote  # noqa: E402

PROJECTS = ROOT.parent / "projects"
TASK_FILE = ROOT / "tools" / "fixtures" / "hq_requirement.txt"

API_PROMPT = """你是资深工程师。按下面的需求，把**整个项目**写出来，只输出 JSON。

{contract}

需求：
{requirement}

只输出 JSON（不要解释、不要围栏外的文字）：
{{"files":[{{"path":"模块.py","content":"<完整文件内容>"}}]}}

要求：完整可运行的文件（含 import）；函数要有类型注解；每个模块都要有对应功能。
"""


def _usage() -> dict:
    return remote.usage_summary("")


def _delta(a: dict, b: dict) -> dict:
    return {"calls": b["calls"] - a["calls"],
            "prompt_tokens": b["prompt_tokens"] - a["prompt_tokens"],
            "completion_tokens": b["completion_tokens"] - a["completion_tokens"],
            "cost_cny": round(b["cost_cny"] - a["cost_cny"], 6)}


def _judge(ws: Path, contracts: dict) -> dict:
    """用同一套机械判据判一个仓库。"""
    state = iterate.inspect(ws, contracts)
    verified = sum(s.get("verified", 0) for s in state.values())
    total = sum(s.get("funcs", 0) for s in state.values())
    hard = []
    for path, st in state.items():
        for f in (st.get("hard_failed") or []):
            hard.append(f"{path}::{f}")
        for msg in (st.get("fails") or [])[:2]:
            hard.append(str(msg)[:160])
    files = sorted(p.name for p in ws.glob("*.py"))
    tests = sorted(p.name for p in ws.glob("test_*.py"))
    cc = contractcheck.check(contracts)
    return {"verified": verified, "total": total, "defects": hard[:8],
            "defect_count": len(hard), "files": files, "tests": tests,
            "contract_ok": cc["ok"], "modules": {k: {"verified": v.get("verified"),
                                                     "hard_failed": v.get("hard_failed")}
                                                 for k, v in state.items()}}


def run_api_arm(ws: Path, requirement: str, contract_text: str = "") -> dict:
    """纯 API 臂：一次调用，写完就完事。"""
    ws.mkdir(parents=True, exist_ok=True)
    before = _usage()
    t0 = time.time()
    block = ("【契约（必须逐条满足）】\n" + contract_text.strip()) if contract_text.strip() else ""
    r = remote.complete(API_PROMPT.format(requirement=requirement, contract=block),
                        node_id="api-arm",
                        json_mode=True, step_index=0, max_out=32000)
    ms = int((time.time() - t0) * 1000)
    after = _usage()
    files = []
    if r.get("ok"):
        from core import author
        got, _issues, _deleg = author._parse_reply(r.get("answer") or "")
        for f in got:
            p = ws / f["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(author._strip_fence(f["content"]), encoding="utf-8")
            files.append(f["path"])
    return {"ok": bool(r.get("ok")) and bool(files), "files": files, "ms": ms,
            "usage": _delta(before, after), "error": str(r.get("error") or "")[:200]}


def run_system_arm(ws: Path, requirement: str, hours: float,
                   contract_file: str = "") -> dict:
    """系统臂：走夜班（契约→整文件→判据→反馈→升档）。"""
    from core import company
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    before = _usage()
    t0 = time.time()
    res = company.night(ws, requirement, "qwen2.5-coder:7b", use_remote=True,
                        contract_file=contract_file)
    ms = int((time.time() - t0) * 1000)
    after = _usage()
    return {"ok": bool(res.ok), "verified": res.verified, "total": res.total,
            "reason": res.reason, "ms": ms, "usage": _delta(before, after),
            "delivery": res.delivery_path}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0.2)
    ap.add_argument("--system-dir", default="ab_system")
    ap.add_argument("--api-dir", default="ab_api")
    ap.add_argument("--judge-only", default="")
    ap.add_argument("--skip-system", action="store_true")
    ap.add_argument("--skip-api", action="store_true")
    a = ap.parse_args()
    requirement = TASK_FILE.read_text(encoding="utf-8")

    # 裁判契约：**合成一次并锁存**，两臂与裁判都用同一份（消除契约漂移）
    locked_path = PROJECTS / "ab_contract.txt"
    jb = _usage()
    if locked_path.exists():
        text = locked_path.read_text(encoding="utf-8")
        gen = {"ok": True, "text": text, "contracts": cm.parse_requirement(text),
               "by": "locked", "problems": []}
    else:
        gen = contractgen.synthesize(requirement, task_id="judge")
        if gen.get("ok"):
            locked_path.write_text(gen["text"], encoding="utf-8")
    ja = _usage()
    if not gen.get("ok"):
        print("裁判契约合成失败：", gen.get("problems"))
        return 2
    contracts = gen["contracts"]
    cc = contractcheck.check(contracts)
    print(f"裁判契约：{sorted(contracts)}｜来源 {gen.get('by')}"
          f"｜合成花费 ¥{_delta(jb, ja)['cost_cny']}｜自查 {'通过' if cc['ok'] else '有矛盾'}")
    if cc["hard"]:
        print("  契约矛盾：", cc["hard"][:3])
    print()

    if a.judge_only:
        ws = Path(a.judge_only).resolve()
        print(json.dumps(_judge(ws, contracts), ensure_ascii=False, indent=2))
        return 0

    out = {}
    if not a.skip_api:
        ws = PROJECTS / a.api_dir
        if ws.exists():
            shutil.rmtree(ws)
        print("=== B 臂：纯 API 一次写完 ===")
        out["api"] = run_api_arm(ws, requirement, gen["text"])
        print("  写入文件：", out["api"]["files"], "｜用时", out["api"]["ms"], "ms｜",
              out["api"]["usage"])
        out["api"]["judge"] = _judge(ws, contracts)
        print("  判据：", out["api"]["judge"]["verified"], "/", out["api"]["judge"]["total"],
              "｜缺陷", out["api"]["judge"]["defect_count"])

    if not a.skip_system:
        ws = PROJECTS / a.system_dir
        print("\n=== A 臂：我们的系统（夜班）===")
        out["system"] = run_system_arm(ws, requirement, a.hours, str(locked_path))
        print("  ", out["system"]["verified"], "/", out["system"]["total"],
              "｜用时", out["system"]["ms"], "ms｜", out["system"]["usage"],
              "｜", out["system"]["reason"][:60])
        out["system"]["judge"] = _judge(ws, contracts)
        print("  判据复核：", out["system"]["judge"]["verified"], "/",
              out["system"]["judge"]["total"], "｜缺陷", out["system"]["judge"]["defect_count"])

    print("\n=== 对照 ===")
    rows = [("纯 API 一次写完", "api"), ("我们的系统", "system")]
    for label, key in rows:
        if key not in out:
            continue
        d = out[key]
        j = d["judge"]
        u = d.get("usage") or {}
        print(f"{label}：调用 {u.get('calls')} 次｜输入 {u.get('prompt_tokens')}"
              f"｜输出 {u.get('completion_tokens')}｜花费 ¥{u.get('cost_cny')}"
              f"｜{d['ms']}ms｜判据 {j['verified']}/{j['total']}｜缺陷 {j['defect_count']}")
    print("\n仓库：")
    if "api" in out:
        print("  纯 API：", (PROJECTS / a.api_dir))
    if "system" in out:
        print("  系统  ：", (PROJECTS / a.system_dir))
    (PROJECTS / "ab_report.json").write_text(
        json.dumps({"judge_contract_cost": _delta(jb, ja), "result": out},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")
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
