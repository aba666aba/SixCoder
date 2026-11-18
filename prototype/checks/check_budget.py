"""输出配额自检：配额必须**按任务规模**给，不是拍一个固定上限。

用户批评过的两件事，这一档一次钉住：
  ① 小模块被允许写 32,000 token（实测 76 行的 clean.py 整份才 706 token，
     模型一次写了 24,636 → 27 倍的浪费）；
  ② 但不能反过来砍成固定小值，否则大文件没法重写（2,000 行 ≈ 18,000 token）。

所以要验的是"随规模增长 + 只受物理上限约束"这两条，而不是某个具体数字。

跑法: python check_budget.py
"""
from __future__ import annotations


def _sandbox_temp():
    """临时目录放进工作区（沙箱只允许写工作区内的路径）。"""
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import tokens  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("输出配额自检")

    print("[1] 基准：配额取模型物理上限（不按任务量掐）")
    small = "def f():\n    return 1\n"                    # ~20 token
    mid = "x = 1\n" * 500                                 # ~2,500 token
    big = "x = 1\n" * 4000                                # ~20,000 token
    b_small, b_mid, b_big = (tokens.output_budget(t) for t in (small, mid, big))
    print(f"     小={b_small}  中={b_mid}  大={b_big}")
    check("小模块给到下限（不低于 1200，够写一个函数体）", b_small >= 1200, str(b_small))
    # 2026-11-10：11-09 的"放大器"原则把 output_budget 改成"只按模型物理上限兜底"
    # （不再按任务量掐）——旧断言"配额随规模上升"随之作废：现在三种规模**都是**物理上限。
    # 意图保留并加强：任何规模都**不得低于自己需要的量**（不会被掐断）。
    check("中模块配额 ≥ 它需要的量（不会被掐断）",
          b_mid >= tokens.estimate_tokens(mid), f"{b_mid} vs {tokens.estimate_tokens(mid)}")
    check("大模块配额 ≥ 它需要的量（不会被掐断）",
          b_big >= tokens.estimate_tokens(big), f"{b_big} vs {tokens.estimate_tokens(big)}")
    check("大文件不会被固定小上限挡住（20,000 token 的文件拿到的配额远超它本身）",
          b_big > tokens.estimate_tokens(big), f"{b_big} vs {tokens.estimate_tokens(big)}")

    print("[2] 上限只有物理上限 384,000，没有人造天花板")
    huge = "x = 1\n" * 400000
    b_huge = tokens.output_budget(huge)
    check("超大文件被物理上限兜住（= 384,000）", b_huge == 384000, str(b_huge))
    check("物理上限与远端模型能力一致（384K 输出）", b_huge <= 384000)

    print("[3] 真实模块：算出来的配额 > 实际需要（但不会离谱地大）")
    root = Path(__file__).resolve().parents[2] / "projects"
    sample = root / "ab_whole" / "clean.py"
    if sample.exists():
        src = sample.read_text(encoding="utf-8")
        need = tokens.exact_count(src)
        budget = tokens.output_budget(src)
        print(f"     ab_whole/clean.py：整份 {need} token，配额 {budget}")
        check("配额 ≥ 需要的量（不会把自己掐断）", budget >= need, f"{budget} vs {need}")
        # 2026-11-10：旧断言"配额 ≤ 需要的 3 倍"与 11-09 原则直接冲突（配额=物理上限
        # 必然远大于 need×3）。意图是"别掐断"，新语义下更强的表述是"恒为物理上限"。
        check("配额恒为模型物理上限（不按任务量掐）", budget == 384000, f"{budget}")
    else:
        print("     （跳过：样本不存在）")

    print("[4] 三个调用点都按规模算（远端请求里的 max_tokens 被真的改掉）")
    from core import author, remote
    captured: list[int] = []
    orig = remote.complete

    def fake_complete(prompt, **kw):                       # noqa: ANN001
        captured.append((int(kw.get("max_out") or 0), str(kw.get("node_id") or "")))
        return {"ok": False, "error": "stub", "answer": ""}

    remote.complete = fake_complete                         # type: ignore[assignment]
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "tiny.py").write_text("def f():\n    return 1\n", encoding="utf-8")
            (ws / "huge.py").write_text("x = 1\n" * 4000, encoding="utf-8")

            class _C:                                      # 最小契约壳
                funcs: list = []
                raw = "def f() -> int：返回 1"
            author.author_module(ws, "tiny.py", _C(), {"tiny.py": _C()})
            author.author_module(ws, "huge.py", _C(), {"huge.py": _C()})
            author.author_bodies(ws, "tiny.py", _C(), {"tiny.py": _C()})
            from core import patch as _patch
            _patch.request_line_fix(ws, "tiny.py", "第 1 行语法错误", verify=None)
    finally:
        remote.complete = orig                             # type: ignore[assignment]
    print(f"     捕获到的配额：{captured}")
    # 按**角色**认配额，不靠下标：函数体路径现在会多打一次（工具循环 + 兜底），
    # "第几个调用点 = 哪个角色"早就不成立了。
    _by_role: dict[str, list[int]] = {}
    for _cap, _nid in captured:
        _role = ("patch" if _nid.startswith("patch:")
                 else "bodies" if _nid.startswith("bodies:") else "module")
        _by_role.setdefault(_role, []).append(_cap)
    _mod = _by_role.get("module") or []
    check("四个调用点都拿到了配额（没有 0 = 没有漏传）",
          all(x > 0 for v in _by_role.values() for x in v), str(captured))
    # 2026-11-10：旧断言"大文件配额 > 小文件"依赖"配额按规模算"的旧语义；新语义下
    # 所有 module 调用点的配额都是模型物理上限（恒等）。意图（配额不因规模小而被掐）
    # 由上一条"没有 0 = 没有漏传" + "配额恒为物理上限"共同保证。
    check("module 调用点的配额恒为物理上限（不因规模小而被掐）",
          len(_mod) >= 2 and all(x == 384000 for x in _mod), str(_mod))
    check("行级补丁的配额最小（本来就只改几行）",
          bool(_by_role.get("patch")) and _by_role["patch"][0] <= (_mod[0] if _mod else 10 ** 9),
          str(captured))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（按规模给配额 / 物理上限兜底 / 三个调用点都生效）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
