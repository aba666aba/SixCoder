"""缺失函数假绿自检：契约函数被删/改名时，夜班必须继续干活，而不是宣布"全部通过"。

这条是一个真 bug 的回归测试（冒烟 + 探针发现）：
    模块里只剩 `def placeholder()`，契约函数全没了 →
    旧逻辑 needs_work=False → 收工理由是"契约内函数已全部通过机械验收"，而验收是 0/8。

跑法: python check_missing.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import company, contract as cm, iterate  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT.parent / "projects" / "ab_contract.txt"
FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("缺失函数假绿自检")
    contracts = cm.parse_requirement(CONTRACT.read_text(encoding="utf-8"))

    print("[1] 契约函数整段消失 → 仍要干活")
    with _sandbox_temp() as td:
        ws = Path(td)
        for name in ("clean.py", "store.py", "report.py"):
            (ws / name).write_text("def placeholder():\n    return 1\n", encoding="utf-8")
        state = iterate.inspect(ws, contracts)
        check("inspect 记下 missing", all(state[p].get("missing") for p in contracts),
              str({p: state[p].get("missing") for p in contracts}))
        check("verified 是 0", sum(state[p]["verified"] for p in contracts) == 0)
        pending = [p for p in contracts if company.needs_work_state(state, p)]
        check("**三个模块都判为还要干活**（这就是修掉的那条假绿）",
              len(pending) == len(contracts), str(pending))
        check("fails 里写清缺失了哪些函数",
              any("契约函数缺失" in str(x) for p in contracts for x in state[p]["fails_all"]),
              str([state[p]["fails_all"] for p in list(contracts)[:1]]))

    print("[2] 文件不存在 → 要干活")
    with _sandbox_temp() as td:
        ws = Path(td)
        state = iterate.inspect(ws, contracts)
        check("不存在的模块也算要干活",
              all(company.needs_work_state(state, p) for p in contracts))

    print("[3] 有桩 → 要干活；已验收满 → 不用干活")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "report.py").write_text(
            "def summarize(rows):\n    raise NotImplementedError\n", encoding="utf-8")
        st = iterate.inspect(ws, {"report.py": contracts["report.py"]})
        check("桩函数 → 要干活", company.needs_work_state(st, "report.py"))
        fake = {"m.py": {"exists": True, "funcs": 3, "verified": 3, "symbols": [],
                         "stubs": set(), "hard_failed": [], "untested": [], "missing": []}}
        check("满验收 → 不用干活", not company.needs_work_state(fake, "m.py"))
        fake2 = {"m.py": {"exists": True, "funcs": 3, "verified": 2, "symbols": [],
                          "stubs": set(), "hard_failed": [], "untested": [], "missing": []}}
        check("验收不满 → 仍要干活（兜底）", company.needs_work_state(fake2, "m.py"))

    print("[4] 收工理由不许自相矛盾")
    comp = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("收工理由按真实验收数说话", "验收只有 {_v}/{_t}" in comp, "没找到兜底文案")
    check("满分时才说全部通过", "契约内函数已全部通过机械验收（{_v}/{_t}" in comp)
    check("判定点是模块级函数（可被自检/探针复用）", "def needs_work_state" in comp)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（缺失/不存在/桩/不满分 都要干活；满验收才收工；理由不自相矛盾）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
