"""规模化四件套 + 断点续跑 + 回归硬门禁的自检。

全部来自真跑数据：15 模块 44 函数的毕设双选系统上量出来的（estimate 估 ¥0.44/22 次，
实测约 ¥0.5；progress 15 个模块全部有记录；基线 42/44；出现过 store.py 2→0 退化）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import contract as cm, project  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []
REQ = '''代码规模：S

1) models.py：
   - make(x: str) -> dict：返回 {"id": x}
2) app.py：
   - run(db: dict) -> dict：返回 {"ok": True}

示例（必须满足）：
make("a") == {"id": "a"}
'''


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("规模化机制自检")
    cs = cm.parse_requirement(REQ)

    print("[1] 预算↔行数换算")
    e = project.estimate(cs)
    check("算出模块/函数/行数", e["modules"] == 2 and e["funcs"] == 2, str(e))
    check("算出调用数与花费", e["est_calls"] >= 2 and e["est_cny"] > 0, str(e))
    fit = project.budget_fits(cs, 0.01)
    check("预算不够时如实说并给分批建议", (not fit["ok"]) and "分批" in fit["note"], fit["note"])

    print("[2] 分批调度（按依赖序）")
    order = project.order_modules(cs)
    check("models 排在 app 前面", order.index("models.py") < order.index("app.py"), str(order))
    check("按 1 个一批切成 2 批", len(project.batch_plan(order, 1)) == 2, str(project.batch_plan(order, 1)))

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        print("[3] 断点续跑")
        project.save_progress(ws, "models.py", {"verified": ["make"], "attempts": 2})
        plan = project.resume_plan(ws, cs, {"models.py": {"verified": 1, "funcs": 1},
                                           "app.py": {"verified": 0, "funcs": 1}})
        check("满分模块进 done", "models.py" in plan["done"], str(plan["done"]))
        check("没动的模块进 fresh", "app.py" in plan["fresh"], str(plan["fresh"]))
        check("给出下一个该写的模块", plan["next"] in ("app.py", "models.py"), plan["next"])

        print("[4] 跨批回归基线 + 硬门禁")
        project.save_baseline(ws, {"models.py": {"verified": 1, "funcs": 1},
                                   "app.py": {"verified": 1, "funcs": 1}}, tag="b1")
        dif = project.diff_baseline(ws, {"models.py": {"verified": 1, "funcs": 1},
                                         "app.py": {"verified": 0, "funcs": 1}})
        check("实测到退化", any("app.py" in x for x in dif["regressed"]), str(dif))
        guard = project.regression_guard(ws, {"app.py": {"verified": 0, "funcs": 1}})
        check("硬门禁把退化模块列出来强写", "app.py" in guard["blocked"], str(guard))
        dif2 = project.diff_baseline(ws, {"models.py": {"verified": 1, "funcs": 1},
                                          "app.py": {"verified": 1, "funcs": 1}})
        check("不退化时明确写不退化", not dif2["regressed"], str(dif2))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          f"（估算 / 分批 / 断点续跑 / 回归硬门禁）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
