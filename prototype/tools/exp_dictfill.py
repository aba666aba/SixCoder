"""实验：字段级填充对"返回 dict"的函数到底有没有用（A/B，同一契约、同一模型）。

问题：`summarize` 类函数在历史样本里只过 2/7。假设是"产出整段代码时口径写错/转义搞坏"，
所以改成"引擎出骨架 + 模型只填每个键的表达式"。这个假设必须被验证，不能靠感觉。

做法：
  1. 造一个 summarize 还是桩的工作区（其余模块都能导入）；
  2. 同一张单，分别 SIXCODER_DICTFILL=0 / 1 各跑 N 次；
  3. 对比：渲染是否成功 · 是否过契约判据（真 call_eq + keys）· 耗时。

用法：python tools/exp_dictfill.py [--runs 3]
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PROTO = HERE.parent.parent
sys.path.insert(0, str(PROTO))

from core import contract as cm, iterate, props  # noqa: E402

FIXTURE = PROTO / "tools" / "fixtures" / "hq_requirement.txt"

STUB = '''"""report 模块。"""

def summarize(rows: list[dict]) -> dict:
    """summarize(rows: list[dict]) -> dict：返回 {"count": 人数, "avg_age": 平均年龄, "domains": 域名计数}"""
    raise NotImplementedError
'''


def prepare() -> tuple[Path, dict]:
    ws = Path(tempfile.mkdtemp(prefix="df_"))
    (ws / "report.py").write_text(STUB, encoding="utf-8")
    text = FIXTURE.read_text(encoding="utf-8")
    return ws, cm.parse_requirement(text)


def run_once(on: bool, model: str) -> dict:
    os.environ["SIXCODER_DICTFILL"] = "1" if on else "0"
    ws, cs = prepare()
    c = cs["report.py"]
    f = next(x for x in c.funcs if x.name == "summarize")
    ev = props.as_feedback(props.run(ws, "report.py",
                                     props.from_contract(iterate._single_func_contract(c, f))))
    order = {"kind": "implement", "target": "report.py", "symbol": "summarize",
             "acceptance": ev, "detail": "按契约实现 summarize"}
    t0 = time.time()
    r = iterate.execute_local(ws, order, model, attempts=1, contract=c)
    ms = int((time.time() - t0) * 1000)
    after = props.run(ws, "report.py", props.from_contract(iterate._single_func_contract(c, f)))
    # 边界判据单独记：这是这次改动的靶心（avg_age=sum(...)/len(...) 在空表上除零）
    src = (ws / "report.py").read_text(encoding="utf-8", errors="replace")
    bchk = [x for x in props.boundary_checks(src, hard=True)
            if str(x.get("expr", "")).startswith("m.summarize(")]
    br = props.run(ws, "report.py", bchk) if bchk else {"ok": True, "fails": []}
    shutil.rmtree(ws, ignore_errors=True)
    return {"on": on, "ok": bool(r.get("ok")), "by": r.get("by") or "",
            "passes": bool(after.get("ok")), "robust": bool(br.get("ok")),
            "robust_detail": str((br.get("fails") or [{}])[0].get("detail", ""))[:80]
            if not br.get("ok") else "",
            "tries": r.get("attempt") or 1, "ms": ms,
            "reason": str(r.get("reason") or "")[:110]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default="qwen2.5:7b")
    args = ap.parse_args()
    rows = []
    for i in range(args.runs):
        for on in (False, True):
            r = run_once(on, args.model)
            rows.append(r)
            print(f"  #{i+1} dictfill={'on ' if on else 'off'} 落盘={r['ok']} "
                  f"过契约判据={r['passes']} 边界不崩={r['robust']} 尝试={r['tries']} "
                  f"by={r['by'] or '-'} {r['ms']}ms {r['reason'][:60] or r['robust_detail']}",
                  flush=True)
    print("\n=== 汇总 ===")
    for on in (False, True):
        sub = [x for x in rows if x["on"] == on]
        print(f"dictfill={'on ' if on else 'off'}: 落盘 {sum(x['ok'] for x in sub)}/{len(sub)}｜"
              f"过契约判据 {sum(x['passes'] for x in sub)}/{len(sub)}｜"
              f"**边界不崩 {sum(x['robust'] for x in sub)}/{len(sub)}**｜"
              f"走字段级填充 {sum(1 for x in sub if x['by'] == 'dictfill')}/{len(sub)}")
    os.environ.pop("SIXCODER_DICTFILL", None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
