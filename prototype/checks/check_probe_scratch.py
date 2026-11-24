"""探针临时目录方案的回归自检（2026-11-15）。

背景：旧方案是 `.sixcoder/tmp/run-<pid>/probe-<tag>-<seq>/`，靠 atexit 删自己那棵树。
夜班被强杀 3 次后残留 **111,793 个目录 / 903MB**；宿主的删除保护（每次 rmtree 起一个
node 子进程做批量保护检查）让删除速度掉到 **4 个/秒**，并在 14:00–14:06 触发
Windows 资源耗尽检测器连启三次、Edge 挂起。

新方案（用户 2026-11-15 定的）：**固定一个 `tmp/work/`，开跑前整棵删掉重建**，
进程内每个探针仍用独立子目录（隔离），超上限删最早。

跑法：python checks/check_probe_scratch.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import probe as P       # noqa: E402

FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _fresh_ws(td: str) -> Path:
    ws = Path(td)
    (ws / ".sixcoder" / "tmp").mkdir(parents=True, exist_ok=True)
    P._RUN_DIRS.clear()
    P._SCRATCH_SEQ = 0
    return ws


def main() -> int:
    print("[1] 固定父目录 + 探针之间仍隔离")
    with tempfile.TemporaryDirectory() as td:
        ws = _fresh_ws(td)
        a = P._scratch(ws, "http")
        b = P._scratch(ws, "http")
        ck("父目录是 tmp/work", a.parent == ws / ".sixcoder" / "tmp" / "work",
           a.parent)
        ck("两次探针不同子目录（防状态污染）", a != b, (a.name, b.name))
        ck("子目录都在同一个 work 下", a.parent == b.parent == P._work_root(ws))
        ck("owner 标记写了当前 pid",
           (a.parent / ".owner").read_text(encoding="utf-8").strip() == str(os.getpid()))

    print()
    print("[2] 开跑前整棵删掉重建（上次怎么死都不残留）")
    with tempfile.TemporaryDirectory() as td:
        ws = _fresh_ws(td)
        junk = P._work_root(ws)
        junk.mkdir(parents=True, exist_ok=True)
        for i in range(5):
            d = junk / ("old%d" % i)
            d.mkdir()
            (d / "left.txt").write_text("x" * 100, encoding="utf-8")
        ck("造好残留", len(list(junk.iterdir())) >= 5, list(junk.iterdir()))
        got = P._run_dir(ws)                       # 第一次使用 → 删旧建新
        ck("旧内容被整棵删掉", not list(got.glob("old*")), list(got.iterdir()))
        ck("新目录里只有 owner", [p.name for p in got.iterdir()] == [".owner"],
           [p.name for p in got.iterdir()])

    print()
    print("[3] 进程内限流（不随探针数线性膨胀）")
    with tempfile.TemporaryDirectory() as td:
        ws = _fresh_ws(td)
        for _ in range(P.WORK_MAX_SUBDIRS + 200):
            P._scratch(ws, "http")
        subs = [p for p in P._work_root(ws).iterdir() if p.is_dir()]
        ck("子目录数不超过上限（+ 一轮余量）",
           len(subs) <= P.WORK_MAX_SUBDIRS + 40, len(subs))

    print()
    print("[4] 退出清理 + 回收命名空间")
    with tempfile.TemporaryDirectory() as td:
        ws = _fresh_ws(td)
        P._scratch(ws, "http")
        P._cleanup_run_dir()
        ck("退出后 work 被删", not P._work_root(ws).exists())
        ck("登记表已清空（否则会在已删路径上重建）", P._RUN_DIRS == [], P._RUN_DIRS)
    ck("垃圾回收认得 work/work- 前缀",
       {"work", "work-"} <= set(P._SCRATCH_PREFIXES), P._SCRATCH_PREFIXES)
    ck("回收仍认得旧的 run-/probe-", {"run-", "probe-"} <= set(P._SCRATCH_PREFIXES))

    print()
    print("[5] 别的活进程正持有 work 时不许互删")
    with tempfile.TemporaryDirectory() as td:
        ws = _fresh_ws(td)
        root = P._work_root(ws)
        root.mkdir(parents=True, exist_ok=True)
        (root / "keep.txt").write_text("别人的东西", encoding="utf-8")
        other = os.getppid()                       # 活着的另一个进程
        (root / ".owner").write_text(str(other), encoding="utf-8")
        mine = P._run_dir(ws)
        ck("退让到 work-<pid>", mine.name == "work-%d" % os.getpid(), mine)
        ck("别人的工作目录没被删", (root / "keep.txt").is_file())
        P._cleanup_run_dir()

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
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
