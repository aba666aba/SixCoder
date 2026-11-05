"""微分支隔离：每次写代码在隔离层里做，成功才并回，失败整层丢弃。

为什么需要（这是"越改越坏"的地基）：
    现在的回滚是**单文件字节快照** —— 只还原本轮要改的那个模块。
    但一次尝试经常顺带动到别的东西（新增 helper 文件、改测试、写临时脚本），
    失败时这些残留会污染下一轮，于是"改坏了又改回去"最后没人知道工作区是什么状态。

两套后端（自动挑，可用环境变量覆盖）：
  · git 后端：工作区**自己是**一个干净的 git 仓库 → 每次尝试开一个一次性分支，
    成功 fast-forward 并回原分支并删掉临时分支；失败切回原分支 + 删分支。
  · 快照后端：不是 git / 不是自己的仓库 / 有未提交改动 → 记录全部 .py 的字节，
    失败时整体还原（并把本轮新建的文件删掉）。

为什么 git 后端要求"干净"：脏树上做 checkout/merge 会动到用户未提交的改动，
那是不可接受的风险 —— 这时宁可用慢一点但绝对安全的快照后端，并如实标注。

用法:
    sand = isolate.open_attempt(ws, "clean.py", 3)
    try:
        ...写代码、跑门禁...
        sand.commit() if ok else sand.rollback()
    finally:
        sand.close()
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".tmp"}


def _git(ws: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception:                                    # noqa: BLE001
        return 127, ""


def _own_clean_repo(ws: Path) -> tuple[bool, str]:
    """判断能不能用 git 后端：必须是**它自己的**仓库，且工作区干净。"""
    rc, inside = _git(ws, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or inside != "true":
        return False, "不是 git 工作树"
    rc, top = _git(ws, "rev-parse", "--show-toplevel")
    if rc != 0 or not top:
        return False, "取不到仓库根"
    if Path(top).resolve() != Path(ws).resolve():
        return False, "目录在外层仓库里（不是它自己的仓库），不做 git 隔离"
    rc, st = _git(ws, "status", "--porcelain")
    if rc != 0:
        return False, "git status 失败"
    if st.strip():
        return False, f"工作区有 {len(st.splitlines())} 处未提交改动，不用 git 后端（怕动到你的改动）"
    return True, "自己的干净仓库"


def _py_files(ws: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in Path(ws).rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(ws)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        try:
            out[str(rel).replace("\\", "/")] = p.read_bytes()
        except OSError:
            continue
    return out


class Sandbox:
    """一次写代码尝试的隔离层。用完必须 close()（幂等）。"""

    def __init__(self, ws: Path, label: str, round_no: int, force: str = ""):
        self.ws = Path(ws)
        self.label = label
        self.round_no = round_no
        self.branch = ""
        self.original_branch = ""
        self.snapshot: dict[str, bytes] = {}
        self.created: list[str] = []
        self.closed = False
        self.decided = ""                     # commit / rollback
        self.backend = ""
        self.notes: list[str] = []
        want = (force or os.environ.get("SIXCODER_ISOLATE") or "").strip().lower()
        if want in ("git", "snapshot"):
            self.backend = want
            self.notes.append(f"后端由 {want} 指定")
        else:
            ok, why = _own_clean_repo(self.ws)
            self.backend = "git" if ok else "snapshot"
            if not ok:
                self.notes.append(why)
        if self.backend == "git":
            self._begin_git()
        else:
            self._begin_snapshot()

    # ---------------- git 后端 ----------------
    def _begin_git(self) -> None:
        rc, cur = _git(self.ws, "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0 or not cur:
            self.backend = "snapshot"
            self.notes.append("git 取不到当前分支 → 退化到快照后端")
            self._begin_snapshot()
            return
        self.original_branch = cur
        name = f"sixcoder/try-{self.round_no}-{int(time.time()) % 100000}"
        rc, out = _git(self.ws, "checkout", "-q", "-b", name)
        if rc != 0:
            self.backend = "snapshot"
            self.notes.append(f"建分支失败（{out[:60]}）→ 退化到快照后端")
            self._begin_snapshot()
            return
        self.branch = name

    def _git_commit(self) -> bool:
        _git(self.ws, "add", "-A")
        rc, out = _git(self.ws, "commit", "-q", "-m",
                       f"sixcoder: attempt {self.label} r{self.round_no}")
        if rc != 0 and "nothing to commit" not in out and "无文件要提交" not in out:
            self.notes.append(f"commit 失败：{out[:80]}")
            return False
        rc, _ = _git(self.ws, "checkout", "-q", self.original_branch)
        if rc != 0:
            self.notes.append("切回原分支失败（改动仍留在临时分支上）")
            return False
        rc, out = _git(self.ws, "merge", "-q", "--ff-only", self.branch)
        if rc != 0:
            self.notes.append(f"fast-forward 并回失败：{out[:80]}")
            return False
        _git(self.ws, "branch", "-q", "-D", self.branch)
        return True

    def _git_rollback(self) -> bool:
        rc, _ = _git(self.ws, "checkout", "-q", "-f", self.original_branch)
        # `checkout -f` 只还原**已跟踪**文件；本轮新建的文件仍是未跟踪状态，必须清掉。
        # 开跑时树是干净的（_own_clean_repo 要求），所以此刻任何未跟踪文件都是本轮产生的。
        # 用 -fd（不加 -x）：不碰 .gitignore 里的东西（缓存/临时目录由别处管）。
        _git(self.ws, "clean", "-fdq")
        _git(self.ws, "branch", "-q", "-D", self.branch)
        return rc == 0

    # ---------------- 快照后端 ----------------
    def _begin_snapshot(self) -> None:
        self.snapshot = _py_files(self.ws)

    def _snapshot_rollback(self) -> bool:
        now = _py_files(self.ws)
        restored = 0
        for rel, data in self.snapshot.items():
            p = self.ws / rel
            try:
                if not p.exists() or p.read_bytes() != data:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(data)
                    restored += 1
            except OSError:
                pass
        removed = 0
        for rel in now:
            if rel not in self.snapshot:
                try:
                    (self.ws / rel).unlink()
                    removed += 1
                except OSError:
                    pass
        self.notes.append(f"快照还原：改回 {restored} 个、删掉本轮新建 {removed} 个")
        return True

    # ---------------- 对外 ----------------
    def commit(self) -> bool:
        if self.closed:
            return True
        self.decided = "commit"
        ok = self._git_commit() if self.backend == "git" else True
        self.close()
        return ok

    def rollback(self) -> bool:
        if self.closed:
            return True
        self.decided = "rollback"
        ok = self._git_rollback() if self.backend == "git" else self._snapshot_rollback()
        self.close()
        return ok

    def close(self) -> None:
        """兜底：没做决定就还原（宁可不改，也不要留半成品状态）。"""
        if self.closed:
            return
        if not self.decided:
            self.decided = "rollback(implicit)"
            if self.backend == "git":
                self._git_rollback()
            else:
                self._snapshot_rollback()
        self.closed = True

    def to_dict(self) -> dict:
        return {"backend": self.backend, "label": self.label, "round": self.round_no,
                "branch": self.branch, "original_branch": self.original_branch,
                "decided": self.decided, "notes": self.notes,
                "snapshot_files": len(self.snapshot)}

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_attempt(ws: Path, label: str, round_no: int = 0, force: str = "") -> Sandbox:
    """开一个隔离层。调用方负责 commit()/rollback()（忘了也有 close() 兜底还原）。"""
    return Sandbox(ws, label, round_no, force=force)
