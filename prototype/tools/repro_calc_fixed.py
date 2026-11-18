"""复现并验证「生成一个计算器」这条路：契约要由远端翻译时会不会崩。

这条路径的关键步骤（顺序）：
    需求解析不出模块契约 → 走 `contractgen.load_or_synthesize`（读写 .sixcoder/contract.txt）
    → 那里用到 Path 但以前没导入 → NameError → 整夜崩溃、交付报告只写"异常中断"。

本脚本用**假远端**跑通这条路，验证：
  ① 不再 NameError；
  ② 失败时交付报告的花费行**不许**出现全库累计（那是误导）。
"""
import io
import shutil
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import company, contractgen, remote  # noqa: E402

ws = ROOT.parent / "projects" / "_repro_calc_fixed"
if ws.exists():
    shutil.rmtree(ws, ignore_errors=True)
ws.mkdir(parents=True, exist_ok=True)

# 假远端：给出一个合法的契约 JSON（模拟"远端把需求翻译成契约"）
FAKE_CONTRACT = """```json
{"modules":[{"path":"calc.py","purpose":"四则运算","functions":[
 {"name":"add","params":[{"name":"a","type":"float"},{"name":"b","type":"float"}],
  "returns":"float","desc":"返回 a+b"},
 {"name":"div","params":[{"name":"a","type":"float"},{"name":"b","type":"float"}],
  "returns":"float","desc":"返回 a/b；b 为 0 时抛 ValueError"}],
 "examples":["add(1, 2) == 3","div(1, 0) 应当抛 ValueError"]}]}
```"""

calls = []


def fake_complete(prompt, **kw):
    calls.append(kw.get("node_id"))
    return {"ok": True, "answer": FAKE_CONTRACT, "by": "fake:contract", "step": 0,
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "cache_hit_tokens": 0,
                      "reasoning_tokens": 0, "cost_cny": 0.0}}


orig_complete = remote.complete
orig_avail = getattr(company, "_remote_available", None)
remote.complete = fake_complete                    # type: ignore[assignment]
if orig_avail is not None:
    company._remote_available = lambda: True       # type: ignore[assignment]

print(f"复现：{ws}（需求=生成一个计算器，假远端给契约）")
try:
    res = company.night(ws, "生成一个计算器", "qwen2.5-coder:7b",
                        use_remote=True, emit=lambda k, p: None)
    print(f"结果：{res.verified}/{res.total}｜{res.reason[:100]}")
    ok_no_crash = "NameError" not in res.reason
except Exception:
    ok_no_crash = False
    print("=== 仍然崩了 ===")
    traceback.print_exc()
finally:
    remote.complete = orig_complete                 # type: ignore[assignment]
    if orig_avail is not None:
        company._remote_available = orig_avail      # type: ignore[assignment]

# 直接验 contractgen 这条路本身
try:
    gen = contractgen.load_or_synthesize(ws, "生成一个计算器")
    print(f"contractgen 直接调用：ok={gen.get('ok')} modules={list((gen.get('contracts') or {}))}")
    ok_contractgen = True
except Exception as exc:
    ok_contractgen = False
    print(f"contractgen 仍然抛异常：{type(exc).__name__}: {exc}")

# 交付报告花费行检查
d = ws / "DELIVERY.md"
ok_cost = True
if d.exists():
    for line in d.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("- 花费"):
            print("交付报告花费行：", line.strip())
            if "941" in line or "6.2099" in line:
                ok_cost = False
else:
    print("（没有生成 DELIVERY.md）")

print()
print(f"① 不再 NameError：{'通过' if ok_no_crash else '失败'}")
print(f"② contractgen 可调用：{'通过' if ok_contractgen else '失败'}")
print(f"③ 花费行不显示累计：{'通过' if ok_cost else '失败'}")
sys.exit(0 if (ok_no_crash and ok_contractgen and ok_cost) else 1)
