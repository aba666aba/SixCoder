"""Git 能力：两个通道，权限不同（重要）。

- **模型通道**（core/tools.py: git）：只允许只读 + 快照提交，禁止 push/remote/config/reset --hard
- **人类通道**（本文件）：UI 按键与 CLI 调用，完整 Git 客户端能力（init/remote/fetch/pull/push/merge/...）

人类通道仍守三条红线：
1. 绝不写凭据（凭据来自 git 自身配置 / 环境变量 / SSH agent，本层不落盘）
2. `push --force` 必须显式 force=True，并在审计日志里单独标记
3. 历史改写类命令（filter-branch / reset --hard / gc --prune）不提供
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import tools
from .sandbox import SandboxError


def _ws(ws: Path) -> Path:
    return Path(ws).resolve()


def _audit(ws: Path, action: str, detail: str, ok: bool = True) -> None:
    """人类通道的操作留痕（写 .sixcoder/audit.jsonl，带 human 标记）。"""
    try:
        sb = tools.sandbox(ws)
        import json
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": f"human:{action}",
               "ok": ok, "detail": detail[:500]}
        with sb.audit_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 基础只读

def status(ws: Path) -> dict:
    ws = _ws(ws)
    rc_b, branch = tools.git_human(ws, "rev-parse", "--abbrev-ref", "HEAD")
    rc_s, porcelain = tools.git_human(ws, "status", "--porcelain")
    rc_h, head = tools.git_human(ws, "rev-parse", "--short", "HEAD")
    changes = []
    for line in (porcelain or "").splitlines():
        if not line.strip():
            continue
        # 注意：tools.git_human() 会 strip 整段输出，首行的前导空格已丢，不能用固定下标切
        m = re.match(r"^([ MADRCU?!]{1,2})\s+(.*)$", line)
        if m:
            changes.append({"code": m.group(1).strip() or "?", "path": m.group(2).strip()})
    upstream = ""
    rc_u, up = tools.git_human(ws, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if rc_u == 0 and up:
        upstream = up
    ahead = behind = 0
    if upstream:
        rc_c, cnt = tools.git_human(ws, "rev-list", "--left-right", "--count", "HEAD...@{upstream}")
        if rc_c == 0 and cnt:
            parts = cnt.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
    rc_r, remotes_raw = tools.git_human(ws, "remote", "-v")
    remotes = {}
    for line in (remotes_raw or "").splitlines():
        seg = line.split()
        if len(seg) >= 2:
            remotes.setdefault(seg[0], seg[1])
    return {"is_repo": rc_b == 0,
            "branch": branch if rc_b == 0 else "?",
            "head": head if rc_h == 0 else "(无提交)",
            "clean": not changes, "changes": changes,
            "upstream": upstream, "ahead": ahead, "behind": behind,
            "remotes": remotes}


def log(ws: Path, n: int = 12) -> dict:
    rc, out = tools.git_human(_ws(ws), "log", f"-{int(n)}", "--pretty=format:%h|%ad|%an|%s",
                        "--date=format:%m-%d %H:%M")
    commits = []
    for line in (out or "").splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({"hash": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
    return {"commits": commits}


def diff(ws: Path, rev: str = "", path: str = "", staged: bool = False) -> dict:
    ws = _ws(ws)
    args = ["diff"]
    if staged:
        args.append("--cached")
    if rev:
        args.append(rev)
    if path:
        try:
            tools.sandbox(ws).jail(path, write=False)   # 路径校验
        except SandboxError as exc:
            return {"diff": "", "error": str(exc)}
        args += ["--", path]
    rc, out = tools.git_human(ws, *args)
    return {"diff": out if rc == 0 else "", "error": "" if rc == 0 else out}


# ---------------------------------------------------------------- 仓库与分支

def init(ws: Path, branch: str = "main") -> dict:
    """在目标目录里初始化仓库。

    **不需要"仓库名"**：`git init` 是给当前目录建仓，仓库名就是目录名。
    唯一可配的是默认分支名（默认 main）。
    初始化后会做一次「首次快照」提交，否则所有文件都是未跟踪状态，
    「撤回」会变成删文件、diff 也没有基线。
    """
    ws = _ws(ws)
    created_dir = False
    if not ws.exists():
        ws.mkdir(parents=True, exist_ok=True)
        created_dir = True
    if (ws / ".git").exists():
        return {"ok": True, "detail": f"{ws.name} 已经是 Git 仓库（分支 {branch}）",
                "repo_name": ws.name, "created": False}

    branch = (branch or "main").strip() or "main"
    rc, out = tools.git_human(ws, "init", "-b", branch)
    if rc != 0:                       # 老版本 git 不支持 -b
        rc, out = tools.git_human(ws, "init")
        tools.git_human(ws, "checkout", "-b", branch)

    gi = ws / ".gitignore"
    if not gi.exists():
        gi.write_text("__pycache__/\n*.pyc\n.pytest_cache/\n.sixcoder/\n", encoding="utf-8")

    # 首次快照：给后续的 diff / 撤回 / 分支操作一个基线
    committed = None
    staged = [l for l in (tools.git_human(ws, "status", "--porcelain")[1] or "").splitlines() if l.strip()]
    if staged:
        tools.git_human(ws, "add", "-A")
        rc_c, out_c = tools.git_human(ws, "-c", "user.name=SixCoder",
                                      "-c", "user.email=coder@local",
                                      "commit", "-q", "-m", "chore: 初始化仓库（首次快照）")
        if rc_c == 0:
            rch, head = tools.git_human(ws, "rev-parse", "--short", "HEAD")
            committed = head if rch == 0 else None

    _audit(ws, "init", f"branch={branch} files={len(staged)} commit={committed}")
    detail = f"已初始化 {ws.name}（分支 {branch}）"
    if created_dir:
        detail += "，目录为新建"
    if committed:
        detail += f"，已做首次快照 {committed}（{len(staged)} 个文件纳入版本管理）"
    else:
        detail += "，目录为空，暂无快照"
    return {"ok": rc == 0, "detail": detail, "repo_name": ws.name,
            "branch": branch, "staged": len(staged), "commit": committed,
            "created": created_dir}


def branches(ws: Path) -> dict:
    rc, out = tools.git_human(_ws(ws), "branch", "--format=%(refname:short)")
    current_rc, current = tools.git_human(_ws(ws), "rev-parse", "--abbrev-ref", "HEAD")
    return {"branches": [b.strip() for b in (out or "").splitlines() if b.strip()],
            "current": current if current_rc == 0 else ""}


def new_branch(ws: Path, name: str, checkout: bool = True) -> dict:
    if not name or any(c in name for c in " ~^:?*[\\"):
        return {"ok": False, "detail": "分支名非法"}
    rc, out = tools.git_human(_ws(ws), "checkout", "-b", name) if checkout else tools.git_human(_ws(ws), "branch", name)
    _audit(ws, "new_branch", name, rc == 0)
    return {"ok": rc == 0, "detail": out}


def switch(ws: Path, name: str) -> dict:
    rc, out = tools.git_human(_ws(ws), "checkout", name)
    _audit(ws, "switch", name, rc == 0)
    return {"ok": rc == 0, "detail": out}


def merge(ws: Path, source: str, message: str = "") -> dict:
    """合并指定分支到当前分支（--no-ff 保留合并点，冲突时如实返回冲突文件）。"""
    ws = _ws(ws)
    if not source:
        return {"ok": False, "detail": "需要源分支名"}
    args = ["merge", "--no-ff", source]
    if message:
        args += ["-m", message]
    rc, out = tools.git_human(ws, *args)
    conflicts: list[str] = []
    if rc != 0:
        rc_s, por = tools.git_human(ws, "status", "--porcelain")
        conflicts = [l[3:].strip() for l in (por or "").splitlines()
                     if l[:2].strip() in ("UU", "AA", "DU", "UD")]
    _audit(ws, "merge", f"{source} -> {'ok' if rc == 0 else 'conflict'}", rc == 0)
    return {"ok": rc == 0, "detail": out[:1500], "conflicts": conflicts}


# ---------------------------------------------------------------- 暂存与提交

def toplevel(ws: Path) -> Path | None:
    """仓库根；不是 git 仓库就返回 None。

    为什么需要它：`git add -A` 在**仓库根**上执行 = "把整个仓库所有脏文件都暂存"。
    实测事故：agent 的 ws 落在 SixCoder 仓库根，节点完成时 add -A 把用户未提交的
    README/diff 一起打包，产出 39 个"消息写着 impl(calc.py)、内容却是别的文件"的混提交。
    """
    rc, out = tools.git_human(_ws(ws), "rev-parse", "--show-toplevel")
    if rc != 0 or not (out or "").strip():
        return None
    return Path(out.strip()).resolve()


def dirty_paths(ws: Path) -> list[str]:
    """当前脏文件清单（相对 ws，重命名取新路径）。"""
    rc, out = tools.git_human(_ws(ws), "status", "--porcelain")
    if rc != 0:
        return []
    paths: list[str] = []
    for line in (out or "").splitlines():
        if not line.strip():
            continue
        p = line[3:].strip()
        if " -> " in p:                                   # 重命名格式：旧 -> 新
            p = p.split(" -> ")[-1]
        paths.append(p.strip('"'))
    return paths


def stage(ws: Path, paths: list[str] | None = None) -> dict:
    """按"本次改动范围"暂存，而不是无条件 add -A。

    · 给了 paths：只暂存这些文件（`git add -A -- <paths>`），其它脏文件原封不动；
    · 没给 paths：沿用 add -A（人体通道"全部存快照"的语义），但**如实报告**本次到底
      暂存了哪些文件、以及是否在仓库根上这样做（那种情况无法区分谁动了什么）。
    """
    ws = _ws(ws)
    at_root = toplevel(ws) == ws
    if paths is None:                                    # "全部存快照"：保留 add -A 语义
        rc, out = tools.git_human(ws, "add", "-A")
        wanted: list[str] = []
    else:
        wanted = [p for p in dict.fromkeys(paths) if str(p).strip()]
        if not wanted:                                   # 明确"没有指定文件"：一个都不暂存
            return {"ok": True, "files": [], "swept": [], "at_root": at_root,
                    "detail": "没有指定文件：本次不暂存任何内容"}
        rc, out = tools.git_human(ws, "add", "-A", "--", *[str(p) for p in wanted])
    if rc != 0:
        return {"ok": False, "detail": out, "files": [], "swept": [], "at_root": at_root}
    rc2, staged = tools.git_human(ws, "diff", "--cached", "--name-only")
    files = [x.strip() for x in (staged or "").splitlines() if x.strip()] if rc2 == 0 else []
    keep = set(wanted)
    swept = [f for f in files if keep and f not in keep]
    res = {"ok": True, "files": files, "swept": swept, "at_root": at_root,
           "detail": (out or "").strip() or f"已暂存 {len(files)} 个文件"}
    if not keep and at_root:
        res["warning"] = (f"在仓库根上执行了 add -A：本次提交包含 {len(files)} 个文件，"
                          "无法区分哪些属于本次改动（建议传 paths 精确提交）")
    return res


def stage_all(ws: Path) -> dict:
    """人体通道的"暂存全部"：保留 add -A 语义，但把范围讲清楚。"""
    r = stage(ws, None)
    return {"ok": r["ok"], "detail": r.get("warning") or r["detail"],
            "files": r.get("files") or []}


def snapshot(ws: Path, message: str, author: str = "SixCoder",
             paths: list[str] | None = None) -> dict:
    """提交快照。**paths 给了就只提交这些文件**（推荐）；不给则 add -A 并如实报告范围。"""
    ws = _ws(ws)
    # 关键节点守卫：开发者模式下"创建提交（版本控制关键点）"要人点头；甲方模式自动放行
    try:
        from . import guards
        g = guards.gate_git_commit(ws, message or "snapshot")
        if not g.get("ok"):
            return {"ok": False, "detail": f"提交未获批准：{g.get('reason')}"}
    except Exception:                                    # noqa: BLE001
        pass
    st = stage(ws, paths)
    if not st["ok"]:
        return {"ok": False, "detail": st["detail"], "files": [], "swept": []}
    rc, out = tools.git_human(ws, "-c", f"user.name={author}", "-c", "user.email=coder@local",
                        "commit", "-m", message or "snapshot", "-q")
    if rc != 0 and "nothing to commit" in out:
        return {"ok": True, "detail": "无改动可提交", "files": [], "swept": []}
    rc2, head = tools.git_human(ws, "rev-parse", "--short", "HEAD")
    _audit(ws, "commit", f"{message} -> {head}", rc == 0)
    return {"ok": rc == 0, "head": head if rc2 == 0 else "", "detail": out[:400],
            "files": st["files"], "swept": st["swept"],
            **({"warning": st["warning"]} if st.get("warning") else {})}


# ---------------------------------------------------------------- 远端同步

def set_remote(ws: Path, name: str = "origin", url: str = "") -> dict:
    ws = _ws(ws)
    if not url:
        return {"ok": False, "detail": "需要远端地址（本地路径或 https/ssh）"}
    rc_v, cur = tools.git_human(ws, "remote", "get-url", name)
    if rc_v == 0:
        rc, out = tools.git_human(ws, "remote", "set-url", name, url)
    else:
        rc, out = tools.git_human(ws, "remote", "add", name, url)
    _audit(ws, "set_remote", f"{name}={url}", rc == 0)
    return {"ok": rc == 0, "detail": out or f"{name} -> {url}", "previous": cur if rc_v == 0 else None}


def fetch(ws: Path, remote: str = "origin", prune: bool = True) -> dict:
    args = ["fetch", remote]
    if prune:
        args.append("--prune")
    rc, out = tools.git_human(_ws(ws), *args, timeout=180)
    _audit(ws, "fetch", f"{remote} rc={rc}", rc == 0)
    return {"ok": rc == 0, "detail": out[-1500:] or f"已获取 {remote}"}


def pull(ws: Path, remote: str = "origin", branch: str = "", ff_only: bool = False) -> dict:
    ws = _ws(ws)
    args = ["pull"]
    args.append("--ff-only" if ff_only else "--no-rebase")
    args.append(remote)
    if branch:
        args.append(branch)
    rc, out = tools.git_human(ws, *args, timeout=240)
    conflicts: list[str] = []
    if rc != 0:
        _, por = tools.git_human(ws, "status", "--porcelain")
        conflicts = [l[3:].strip() for l in (por or "").splitlines()
                     if l[:2].strip() in ("UU", "AA", "DU", "UD")]
    _audit(ws, "pull", f"{remote}/{branch or '?'} rc={rc}", rc == 0)
    return {"ok": rc == 0, "detail": out[-2000:], "conflicts": conflicts}


def push(ws: Path, remote: str = "origin", branch: str = "", force: bool = False,
         set_upstream: bool = True) -> dict:
    ws = _ws(ws)
    if force:
        # 红线：force 必须显式声明，且单独记为高危操作
        _audit(ws, "PUSH_FORCE", f"{remote} {branch}", ok=False)
    # 关键节点守卫：开发者模式下"推送到远端"要人点头（含 force 的情况）
    try:
        from . import guards
        g = guards.gate_git_push(ws, "push", f"{remote}/{branch or '当前分支'}"
                                 f"{'（force-with-lease）' if force else ''}")
        if not g.get("ok"):
            return {"ok": False, "detail": f"推送未获批准：{g.get('reason')}"}
    except Exception:                                    # noqa: BLE001
        pass
    args = ["push"]
    if force:
        args.append("--force-with-lease")     # 用更安全的 force 变体
    if set_upstream:
        args.append("-u")
    args.append(remote)
    if branch:
        args.append(branch)
    rc, out = tools.git_human(ws, *args, timeout=240)
    _audit(ws, "push", f"{remote}/{branch or '?'} rc={rc} force={force}", rc == 0)
    return {"ok": rc == 0, "detail": out[-2000:] or "推送完成"}


def sync(ws: Path, remote: str = "origin") -> dict:
    """一键同步：fetch → pull(--no-rebase) → push。任一步失败即返回。"""
    steps = []
    f = fetch(ws, remote)
    steps.append({"step": "fetch", **f})
    if not f["ok"]:
        return {"ok": False, "steps": steps}
    p = pull(ws, remote)
    steps.append({"step": "pull", **p})
    if not p["ok"]:
        return {"ok": False, "steps": steps}
    u = push(ws, remote)
    steps.append({"step": "push", **u})
    return {"ok": u["ok"], "steps": steps}


# ---------------------------------------------------------------- 撤销

def undo_file(ws: Path, path: str) -> dict:
    """把单个文件恢复到改动前。

    - 已跟踪文件（M/A/D）：`git checkout -- <path>` 回到 HEAD 版本
    - 未跟踪的新文件（??）：删除它（删前先写沙箱备份，可再用「沙箱撤回」找回）
    - 不使用 reset --hard
    """
    ws = _ws(ws)
    sb = tools.sandbox(ws)
    try:
        target = sb.jail(path, write=True)
    except SandboxError as exc:
        return {"ok": False, "detail": str(exc)}

    rc_t, _ = tools.git_human(ws, "ls-files", "--error-unmatch", path)
    tracked = rc_t == 0
    if not tracked and target.exists():
        sb.backup(target, action="undo-delete")
        target.unlink()
        _audit(ws, "undo_file:delete-untracked", path, True)
        return {"ok": True, "action": "deleted-untracked",
                "detail": f"已删除未跟踪的新文件 {path}（删前已备份，可用「沙箱撤回」找回）"}

    rc, out = tools.git_human(ws, "checkout", "--", path)
    _audit(ws, "undo_file", path, rc == 0)
    return {"ok": rc == 0, "action": "checkout", "tracked": tracked,
            "detail": out or (f"已恢复 {path} 到 HEAD" if rc == 0 else "恢复失败")}


def sandbox_undo_file(ws: Path, path: str) -> dict:
    """按引擎写盘日志，把指定文件回滚到本次改动前（连未提交的也能撤）。"""
    import shutil
    ws = _ws(ws)
    sb = tools.sandbox(ws)
    want = path.replace("\\", "/")
    entries = [e for e in sb.journal() if e.get("rel") == want]
    if not entries:
        # 引擎没写过这个文件（手工改的）：退回 git 版本恢复，别让按钮点了没反应
        fallback = undo_file(ws, path)
        fallback["fallback"] = "checkout"
        fallback["detail"] = (f"写盘日志里没有 {path} 的记录（不是引擎写的），已改用 git 版本恢复："
                              f"{fallback.get('detail', '')}")
        return fallback
    e = entries[-1]
    src = sb.journal_dir / e["backup"]
    dst = sb.ws / e["rel"]
    if not src.exists():
        return {"ok": False, "detail": f"备份文件已丢失：{e['backup']}"}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    _audit(ws, "sandbox_undo_file", f"{path} <- {e['ts']}", True)
    return {"ok": True, "restored": e["rel"],
            "detail": f"已把 {path} 恢复到 {e['ts']} 的写盘前状态"}


def sandbox_undo(ws: Path, steps: int = 1) -> dict:
    """绕过 git 的沙箱级撤销：按写盘日志回滚（连未提交的也能撤）。"""
    try:
        done = tools.sandbox(ws).undo(steps)
        _audit(ws, "sandbox_undo", f"steps={steps} restored={done}", bool(done))
        return {"ok": bool(done), "restored": done}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}
