"""微分支隔离自检：能不能真的"失败整层丢弃、成功才并回"。

关键验收：
  ① 快照后端：失败 → 全线还原（包括本轮新建的文件被删掉）；成功 → 改动保留；
  ② git 后端：失败 → 切回原分支且临时分支被删；成功 → 改动并回原分支、临时分支删除；
  ③ 脏树 / 不是自己的仓库 → **不许用 git 后端**（会动到用户未提交的改动）；
  ④ 忘了做决定 → close() 兜底还原（不许留半成品状态）。

跑法: python check_isolate.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import isolate  # noqa: E402

FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _git(ws: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip()


def _init_repo(ws: Path) -> bool:
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "t@t")
    _git(ws, "config", "user.name", "t")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "-m", "init")
    return _git(ws, "rev-parse", "HEAD")[0] == 0


def main() -> int:
    print("微分支隔离自检")

    print("[1] 快照后端：失败 → 全线还原（含删掉本轮新建的文件）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
        (ws / "b.py").write_text("B = 1\n", encoding="utf-8")
        sand = isolate.open_attempt(ws, "a.py", 1, force="snapshot")
        check("后端是快照", sand.backend == "snapshot", sand.backend)
        (ws / "a.py").write_text("A = 999\n", encoding="utf-8")
        (ws / "b.py").write_text("B = 999\n", encoding="utf-8")
        (ws / "new_helper.py").write_text("H = 1\n", encoding="utf-8")
        sand.rollback()
        check("改过的 a.py 被还原", (ws / "a.py").read_text(encoding="utf-8") == "A = 1\n")
        check("**顺带**改到的 b.py 也被还原（这正是单文件回滚做不到的）",
              (ws / "b.py").read_text(encoding="utf-8") == "B = 1\n")
        check("本轮新建的 helper 被删掉", not (ws / "new_helper.py").exists())
        check("rollback 记进 decided", sand.decided == "rollback", sand.decided)

    print("[2] 快照后端：成功 → 改动保留")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
        sand = isolate.open_attempt(ws, "a.py", 2, force="snapshot")
        (ws / "a.py").write_text("A = 2\n", encoding="utf-8")
        sand.commit()
        check("改动保留", (ws / "a.py").read_text(encoding="utf-8") == "A = 2\n")
        check("commit 记进 decided", sand.decided == "commit", sand.decided)

    print("[3] 忘了做决定 → close() 兜底还原")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
        sand = isolate.open_attempt(ws, "a.py", 3, force="snapshot")
        (ws / "a.py").write_text("A = 777\n", encoding="utf-8")
        sand.close()
        check("没决定就还原（不留半成品）",
              (ws / "a.py").read_text(encoding="utf-8") == "A = 1\n")
        check("decided 标成隐式回滚", "implicit" in sand.decided, sand.decided)

    print("[4] git 后端：失败 → 切回原分支 + 临时分支被删")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
        if not _init_repo(ws):
            check("git 后端可用（初始化成功）", False, "git init 失败，跳过本组")
        else:
            sand = isolate.open_attempt(ws, "a.py", 4)
            check("干净的自有仓库 → 用 git 后端", sand.backend == "git", sand.backend)
            check("开了临时分支", sand.branch.startswith("sixcoder/try-"), sand.branch)
            (ws / "a.py").write_text("A = 555\n", encoding="utf-8")
            (ws / "c.py").write_text("C = 1\n", encoding="utf-8")
            sand.rollback()
            check("回到原分支", _git(ws, "rev-parse", "--abbrev-ref", "HEAD")[1]
                  == sand.original_branch, _git(ws, "rev-parse", "--abbrev-ref", "HEAD")[1])
            check("临时分支已删", sand.branch not in _git(ws, "branch")[1])
            check("文件回到原始内容", (ws / "a.py").read_text(encoding="utf-8") == "A = 1\n")
            check("本轮新建的文件也消失", not (ws / "c.py").exists())

    print("[5] git 后端：成功 → 改动并回原分支")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
        if _init_repo(ws):
            sand = isolate.open_attempt(ws, "a.py", 5)
            (ws / "a.py").write_text("A = 42\n", encoding="utf-8")
            ok = sand.commit()
            check("commit 成功", ok, str(sand.notes))
            check("在当前分支上就有改动（已 fast-forward 并回）",
                  (ws / "a.py").read_text(encoding="utf-8") == "A = 42\n")
            check("临时分支已删", sand.branch not in _git(ws, "branch")[1])
            check("工作区干净（并回后无残留）",
                  _git(ws, "status", "--porcelain")[1].strip() == "",
                  _git(ws, "status", "--porcelain")[1][:80])

    print("[6] 脏树 → 不许用 git 后端（怕动到未提交改动）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
        if _init_repo(ws):
            (ws / "a.py").write_text("A = 2\n", encoding="utf-8")     # 故意不提交
            sand = isolate.open_attempt(ws, "a.py", 6)
            check("脏树 → 退化到快照后端", sand.backend == "snapshot", sand.backend)
            check("说明里写清原因", any("未提交" in n for n in sand.notes), str(sand.notes))
            sand.rollback()
            check("你的未提交改动没被动（还是 A = 2）",
                  (ws / "a.py").read_text(encoding="utf-8") == "A = 2\n")

    print("[7] 不是自己的仓库 → 快照后端（不当外层仓库的家）")
    with _sandbox_temp() as td:
        outer = Path(td)
        (outer / "a.py").write_text("A = 1\n", encoding="utf-8")
        if _init_repo(outer):
            inner = outer / "sub"
            inner.mkdir()
            (inner / "b.py").write_text("B = 1\n", encoding="utf-8")
            sand = isolate.open_attempt(inner, "b.py", 7)
            check("子目录 → 快照后端", sand.backend == "snapshot", sand.backend)
            check("说明里点出原因", any("外层" in n for n in sand.notes), str(sand.notes))
            sand.rollback()

    print("[8] 已接进夜班")
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("写代码前开隔离层", "open_attempt" in comp and "isolate_begin" in comp)
    check("失败回滚整层", "sand.rollback()" in comp and "isolate_rollback" in comp)
    check("成功并回", "sand.commit()" in comp and "isolate_commit" in comp)
    check("隔离层失败不阻断夜班（降级为旧回滚）", "隔离层开启失败" in comp)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（快照/git 双后端 / 失败整层丢弃 / 成功并回 / 脏树不碰 / 未决定即还原）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
