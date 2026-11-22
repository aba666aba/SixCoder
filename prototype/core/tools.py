"""工具层：read_file / edit_file / list_files / bash / git —— 全部经沙箱。

铁律（docs/04 §1）：
- 失败返回可读错误，永不抛异常打断循环
- 路径越界 → 沙箱拦截；敏感路径（.ssh/.env/keystore）→ 拒绝
- 写盘前自动备份（可一键撤销），落盘前干跑语法
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .sandbox import Sandbox, SandboxError

MAX_READ_LINES = 400

_SB: dict[str, Sandbox] = {}
_ENGINE_GIT = {"status", "diff", "log", "ls-files", "add", "commit", "checkout",
               "rev-parse", "show", "init", "branch", "symbolic-ref", "for-each-ref"}


class ToolError(Exception):
    pass


def sandbox(ws: Path, mode: str = "write") -> Sandbox:
    key = str(Path(ws).resolve())
    if key not in _SB:
        _SB[key] = Sandbox(Path(ws), mode=mode)
    return _SB[key]


def safe_path(ws: Path, rel: str) -> Path:
    """对外的路径校验（走沙箱牢笼，只读）。"""
    try:
        return sandbox(ws).jail(rel, write=False)
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc


def read_file(ws: Path, rel: str, start: int | None = None, end: int | None = None) -> str:
    p = safe_path(ws, rel)
    if not p.exists():
        raise ToolError(f"文件不存在：{rel}")
    if p.stat().st_size > 512 * 1024:
        raise ToolError(f"文件过大（{p.stat().st_size // 1024} KB），请指定行范围")
    lines = p.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    s = max(1, start or 1)
    e = min(len(lines), end or (s + MAX_READ_LINES - 1))
    body = "\n".join(f"{i:>4}| {lines[i-1]}" for i in range(s, e + 1))
    return f"{rel}  (共 {len(lines)} 行，显示 {s}-{e})\n{body}"


def list_files(ws: Path, sub: str = "", limit: int = 200) -> list[str]:
    ws_r = Path(ws).resolve()
    root = safe_path(ws, sub) if sub else ws_r
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if any(part in {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv",
                        ".sixcoder", ".mypy_cache", ".ruff_cache", "dist", "build"}
               for part in p.parts):
            continue
        if p.is_file():
            out.append(p.relative_to(ws_r).as_posix())
        if len(out) >= limit:
            break
    return out


PATCH_RE = re.compile(
    r"<{5,}\s*SEARCH\s*\n(.*?)\n={5,}\s*\n(.*?)\n>{5,}\s*REPLACE",
    re.DOTALL,
)


def parse_patches(text: str) -> list[dict]:
    return [{"search": m.group(1), "replace": m.group(2)} for m in PATCH_RE.finditer(text or "")]


def apply_patch_text(text: str, search: str, replace: str, rel: str = "") -> str:
    """在内存文本上应用一个 search/replace（唯一命中）。"""
    hits = [m.start() for m in re.finditer(re.escape(search), text)]
    if not hits:
        head = "\n".join(text.splitlines()[:30])
        raise ToolError(f"search 未命中。{rel} 前 30 行：\n{head}")
    if len(hits) > 1:
        raise ToolError(f"search 命中 {len(hits)} 处，必须唯一。请包含更多上下文行。")
    return text[:hits[0]] + replace + text[hits[0] + len(search):]


def apply_patch(ws: Path, rel: str, search: str, replace: str) -> str:
    """在内存里计算应用结果（不落盘）。"""
    p = safe_path(ws, rel)
    if not p.exists():
        raise ToolError(f"文件不存在：{rel}")
    return apply_patch_text(p.read_text(encoding="utf-8"), search, replace, rel)


def write_file(ws: Path, rel: str, text: str, *, backup: bool = True) -> None:
    sb = sandbox(ws)
    try:
        p = sb.jail(rel, write=True)
    except SandboxError as exc:
        raise ToolError(str(exc)) from exc
    p.parent.mkdir(parents=True, exist_ok=True)
    if backup:
        sb.backup(p, action="write")
    p.write_text(text, encoding="utf-8")
    sb.audit("write_file", rel)


def diff_text(a: str, b: str, name: str) -> str:
    import difflib
    d = difflib.unified_diff(a.splitlines(), b.splitlines(),
                             fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="")
    return "\n".join(d)


def run_bash(ws: Path, cmd: str, timeout: int = 180) -> tuple[int, str]:
    sb = sandbox(ws)
    try:
        sb.check_cmd(cmd)
    except SandboxError as exc:
        sb.audit("bash:denied", f"{cmd} :: {exc}", ok=False)
        raise ToolError(str(exc)) from exc
    sb.audit("bash", cmd)
    try:
        p = subprocess.run(cmd, cwd=str(sb.ws), shell=True, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=sb.env())
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"


def run_cmd(ws: Path, argv: list[str], timeout: int = 180) -> tuple[int, str]:
    """跑一个内部固定命令（列表形式，不经 shell 解析 PATH）。

    与 run_bash 的关键区别：run_bash 走 shell=True 字符串，`python` 由 PATH 解析——
    在 WorkBuddy 会话里 PATH 最前是自带的 3.13.x（无 pytest/mypy），会让
    gate_tests/gate_types 命中错误解释器。这里改用列表 argv + sys.executable，
    保证用项目真正的解释器，同时保留沙箱脱敏环境与审计。
    """
    sb = sandbox(ws)
    sb.audit("cmd", " ".join(argv))
    try:
        p = subprocess.run(argv, cwd=str(sb.ws), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=sb.env())
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"超时 {timeout}s"


HUMAN_GIT_BLOCK = {"filter-branch", "gc", "reflog", "replace"}


def _git_sub(args: tuple[str, ...]) -> str:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-c":
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        return a
    return ""


def _git_run(ws: Path, args: list[str], timeout: int) -> tuple[int, str]:
    """跑 git；没装 Git 时返回可读原因，别让 API 抛 500。"""
    try:
        p = subprocess.run(["git", *args], cwd=str(ws), capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError:
        return 127, "找不到 git：请安装 Git，或重新运行安装器勾选「安装 Git（MinGit）」"
    except subprocess.TimeoutExpired:
        return 124, f"git {args[0] if args else ''} 超时（{timeout}s）"


def git(ws: Path, *args: str, timeout: int = 60) -> tuple[int, str]:
    """【模型通道】引擎内部 git：只允许只读 + 快照提交。

    模型不得调用 push/remote/config/reset --hard —— 这些只存在于人类通道
    （gitops.git_human），见模块文档的两通道设计。
    """
    sub = _git_sub(args)
    if sub and sub not in _ENGINE_GIT:
        return 1, f"git 子命令 `{sub}` 未被引擎允许（模型通道只允许 {sorted(_ENGINE_GIT)}）"
    return _git_run(ws, list(args), timeout)


def git_human(ws: Path, *args: str, timeout: int = 240) -> tuple[int, str]:
    """【人类通道】UI 按键 / CLI 用：完整 Git 客户端能力。

    仍守红线：不提供历史改写类命令（filter-branch / gc / reflog / replace），
    `reset --hard` 也不提供（请用 undo_file 或沙箱撤销）。
    """
    sub = _git_sub(args)
    if sub in HUMAN_GIT_BLOCK:
        return 1, f"git 子命令 `{sub}` 属于历史改写类，本工具不提供"
    if sub == "reset" and any("--hard" in a for a in args):
        return 1, "`git reset --hard` 不可用：请用「撤销该文件」或「沙箱撤销」"
    return _git_run(ws, list(args), timeout)
