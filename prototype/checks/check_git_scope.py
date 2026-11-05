"""提交范围自检：快照只能提交"本次改动"，不能把别人的脏文件一起打包。

为什么要这条（真实事故，2026-11-05）：
    agent 的 ws 落在 SixCoder 仓库根，节点完成时 `_snapshot()` 无条件 `git add -A`，
    把用户未提交的 README / 未跟踪文件一起打包，产出 **39 个**"消息写着
    `impl(calc.py): 远端协助（本地闸门通过）`、内容却一个 calc.py 都没有"的混提交。
    问题不在 commit 消息，而在"提交范围不受约束"。

判定口径（正反两面都要）：
    · 该拦的拦住：给了 paths → 只提交这些文件，无关文件必须仍然是"未提交"状态；
    · 不该拦的别拦：不给 paths（人体通道"全部存快照"）→ 照样能提交，但必须
      **如实报告**提交范围（files / swept / 仓库根警告），不许假装只提交了本次改动。

跑法: python checks/check_git_scope.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import agent, gitops  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

NOTES = "用户的未提交草稿\n"
CALC_V1 = 'def add(a: float, b: float) -> float:\n    return a + b\n'
CALC_V2 = ('def add(a: float, b: float) -> float:\n    return a + b\n\n\n'
           'def sub(a: float, b: float) -> float:\n    return a - b\n')


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _git(ws: Path, *args: str) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(ws), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    # 只能用 rstrip：porcelain 的第一行形如 " M path"，strip() 会吃掉前导空格，
    # 把 ln[3:] 的解析整体错开一位（本自检第一版就踩了这个坑，得到 'otes.md'）。
    return p.returncode, ((p.stdout or "") + (p.stderr or "")).rstrip()


def _repo(tag: str) -> Path:
    """建一个起始状态干净的临时仓库：初始提交里有 calc.py 和 notes.md。"""
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix=f"scope_{tag}_"))
    _git(ws, "init", "-q")
    (ws / "calc.py").write_text(CALC_V1, encoding="utf-8")
    (ws / "notes.md").write_text(NOTES, encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "-c", "user.name=init", "-c", "user.email=init@local", "commit", "-q", "-m", "init")
    return ws


def _committed_files(ws: Path) -> list[str]:
    rc, out = _git(ws, "show", "--name-only", "--format=", "HEAD")
    return [x.strip() for x in out.splitlines() if x.strip()] if rc == 0 else []


def _dirty(ws: Path) -> list[str]:
    """用户眼里的脏文件：排除 .sixcoder/（那是本工具自己写的审计留痕，不是用户的活）。"""
    rc, out = _git(ws, "status", "--porcelain")
    rows = [ln[3:].strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    return [r for r in rows if not r.startswith(".sixcoder")]


def _dirty_and_write(ws: Path) -> None:
    """模拟"任务写了 calc.py，而用户手里还有没提交的 notes.md"。"""
    (ws / "notes.md").write_text(NOTES + "用户的第二段草稿\n", encoding="utf-8")
    (ws / "calc.py").write_text(CALC_V2, encoding="utf-8")


def main() -> int:
    print("提交范围自检（快照只提交本次改动）")

    # ---------------- 1) agent._snapshot 给了 paths：必须只提交 calc.py
    print("[1] agent 快照按 paths 精确提交")
    ws = _repo("agent")
    _dirty_and_write(ws)
    head = agent._snapshot(ws, "impl(calc.py): 精确提交", paths=["calc.py"])
    files = _committed_files(ws)
    check("产生了提交", bool(head), f"head={head!r} 提交文件={files}")
    check("只提交了 calc.py", files == ["calc.py"], f"实际提交 {files}")
    check("用户的 notes.md 仍未被提交", "notes.md" in " ".join(_dirty(ws)), f"脏文件 {_dirty(ws)}")
    check("notes.md 内容没被弄坏", (ws / "notes.md").read_text(encoding="utf-8").endswith("第二段草稿\n"))

    # ---------------- 2) 反例对照：不传 paths 就会把用户文件一起打包（旧行为）
    ws2 = _repo("legacy")
    _dirty_and_write(ws2)
    agent._snapshot(ws2, "impl(calc.py): 不传 paths")
    files2 = _committed_files(ws2)
    check("不传 paths 时确实会扫进无关文件（证明 bugs 场景存在）",
          "notes.md" in files2, f"实际提交 {files2}")

    # ---------------- 3) gitops.snapshot 给了 paths：只提交指定文件，且 swept 为空
    print("[2] gitops 快照按 paths 精确提交")
    ws3 = _repo("gitops")
    _dirty_and_write(ws3)
    r = gitops.snapshot(ws3, "手动快照: 只交 calc.py", paths=["calc.py"])
    files3 = _committed_files(ws3)
    check("提交成功", bool(r.get("ok")), str(r)[:200])
    check("只提交了 calc.py", files3 == ["calc.py"], f"实际提交 {files3}")
    check("files 如实列出提交范围", r.get("files") == ["calc.py"], str(r.get("files")))
    check("swept 为空（没有夹带）", r.get("swept") == [], str(r.get("swept")))
    check("用户的 notes.md 仍未提交", "notes.md" in " ".join(_dirty(ws3)), f"脏文件 {_dirty(ws3)}")

    # ---------------- 4) 不传 paths 的人体通道：允许提交，但必须如实报告范围
    print("[3] 不传 paths：照旧能提交，但如实报告范围")
    ws4 = _repo("all")
    _dirty_and_write(ws4)
    r4 = gitops.snapshot(ws4, "全部快照")
    check("提交成功", bool(r4.get("ok")), str(r4)[:200])
    check("files 里含 notes.md（承认扫进去了）",
          "notes.md" in (r4.get("files") or []), str(r4.get("files")))
    check("仓库根上 add -A 会给出警告，不假装只交了本次改动",
          bool(r4.get("warning")) and "add -A" in r4["warning"], str(r4.get("warning"))[:120])

    # ---------------- 5) paths=[]：明确"本次没有改动"，绝不能退化成 add -A
    print("[4] paths=[] 不许退化成 add -A")
    ws6 = _repo("empty")
    (ws6 / "notes.md").write_text(NOTES + "第三段草稿\n", encoding="utf-8")
    n_before = len([x for x in _git(ws6, "log", "--oneline")[1].splitlines() if x.strip()])
    head6 = agent._snapshot(ws6, "impl(calc.py): 本次没有改动", paths=[])
    n_after = len([x for x in _git(ws6, "log", "--oneline")[1].splitlines() if x.strip()])
    check("不产生提交", not head6 and n_after == n_before,
          f"head={head6!r} 提交数 {n_before} -> {n_after}")
    check("用户的 notes.md 仍未被提交", _dirty(ws6) == ["notes.md"], str(_dirty(ws6)))
    st6 = gitops.stage(ws6, [])
    check("stage([]) 什么都不暂存",
          st6.get("files") == [] and not _git(ws6, "diff", "--cached", "--name-only")[1].strip(),
          str(st6)[:160])

    # ---------------- 6) stage 只按 paths 暂存，顺手确认 scope 工具本身
    print("[5] stage / scope 工具本身")
    ws5 = _repo("stage")
    _dirty_and_write(ws5)
    st = gitops.stage(ws5, ["calc.py"])
    rc_c, cached = _git(ws5, "diff", "--cached", "--name-only")
    staged = [x.strip() for x in cached.splitlines() if x.strip()] if rc_c == 0 else []
    check("stage(paths) 只暂存 calc.py", staged == ["calc.py"], f"暂存 {staged}")
    check("stage 报出 at_root（仓库根上 add -A 才危险）", st.get("at_root") is True, str(st)[:160])
    check("toplevel 认得出仓库根", gitops.toplevel(ws5) == ws5.resolve(), str(gitops.toplevel(ws5)))
    check("dirty_paths 看得到两个脏文件", sorted(gitops.dirty_paths(ws5)) == ["calc.py", "notes.md"],
          str(gitops.dirty_paths(ws5)))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（精确提交 / 旧行为对照 / 如实报告范围 / stage 语义）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
