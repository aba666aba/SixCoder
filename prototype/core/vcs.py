"""git 记账：把每个有净收获的片变成一个可回滚的历史点。

红线（为什么这个模块只有两个动作）：引擎**只做** `add` + `commit`。
`reset --hard` / `rebase` / `checkout --` / 删 `.git` 一概不碰 —— 历史是
唯一的"时间机器"，改历史的工具在自动化里全是事故源（哪怕本意是修复）。
误删兜底靠 git 自带 reflog（默认 90 天）。
"""
import subprocess
from pathlib import Path


def _git(ws: Path, *args: str, timeout: int = 60):
    return subprocess.run(["git", *args], cwd=str(ws), capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def is_repo(ws) -> bool:
    ws = Path(ws)
    return (ws / ".git").is_dir() and _git(ws, "rev-parse", "--is-inside-work-tree").returncode == 0


def commit_slice(ws, *, sl_id: str, gained: list, emit=None) -> dict:
    """片结束后提交一次。没有变化就不提交（空提交只制造噪音）。"""
    ws = Path(ws)
    if not is_repo(ws):
        return {"ok": False, "why": "不是 git 仓库（快照后端在工作，但没有历史）"}
    st = _git(ws, "status", "--porcelain")
    if st.returncode != 0:
        return {"ok": False, "why": (st.stderr or "status 失败").strip()[:120]}
    files = [ln for ln in (st.stdout or "").splitlines() if ln.strip()]
    if not files:
        return {"ok": True, "committed": False, "why": "没有变化"}
    add = _git(ws, "add", "-A")
    if add.returncode != 0:
        return {"ok": False, "why": (add.stderr or "add 失败").strip()[:120]}
    msg = f"[sixcoder] {sl_id}: +{len(gained or [])} caps, {len(files)} files"
    cm = _git(ws, "commit", "-m", msg)
    if cm.returncode != 0:
        return {"ok": False, "why": (cm.stderr or "commit 失败").strip().splitlines()[-1][:120]}
    if emit:
        emit("git_commit", {"slice": sl_id, "gained": len(gained or []),
                            "files": len(files), "head": msg})
    return {"ok": True, "committed": True, "files": len(files), "msg": msg}
