"""check_vcs —— git 记账的断言（只 add+commit，永不碰历史）。"""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import vcs  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)
    return ok


def _git(ws: Path, *args):
    return subprocess.run(["git", *args], cwd=str(ws), capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def git_ws() -> Path:
    ws = Path(tempfile.mkdtemp())
    _git(ws, "init")
    _git(ws, "config", "user.name", "T")
    _git(ws, "config", "user.email", "t@t")
    (ws / "app").mkdir()
    (ws / "app/m.py").write_text("x = 1\n", encoding="utf-8")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-m", "init")
    return ws


def main() -> int:
    ws = git_ws()

    print("[1] 有变化 → 提交成功，历史 +1")
    (ws / "app/m.py").write_text("x = 2\n", encoding="utf-8")
    n0 = len(_git(ws, "log", "--oneline").stdout.strip().splitlines())
    r = vcs.commit_slice(ws, sl_id="s1", gained=["cap.a"])
    check("提交 ok", r.get("ok") and r.get("committed"), str(r))
    n1 = len(_git(ws, "log", "--oneline").stdout.strip().splitlines())
    check("历史点 +1", n1 == n0 + 1, f"{n0}→{n1}")
    check("提交信息带片号", "s1" in _git(ws, "log", "-1", "--format=%s").stdout,
          _git(ws, "log", "-1", "--format=%s").stdout)

    print("[2] 没变化 → 不制造空提交")
    r2 = vcs.commit_slice(ws, sl_id="s2", gained=["cap.b"])
    check("跳过但 ok", r2.get("ok") and not r2.get("committed"), str(r2))
    n2 = len(_git(ws, "log", "--oneline").stdout.strip().splitlines())
    check("历史不变", n2 == n1, f"{n1}→{n2}")

    print("[3] 红线：vcs 模块里不许出现改历史的动作")
    src = Path(vcs.__file__).read_text(encoding="utf-8")
    banned = ["reset", "rebase", "checkout", "push", "clean", "filter-branch", "rmdir", "rmtree"]
    hits = [b for b in banned if f'"{b}"' in src or f"'{b}'" in src]
    check("没有任何改历史/删除动作", not hits, str(hits))

    print("[4] 不是 git 仓库 → 如实跳过（快照后端仍在工作）")
    ws2 = Path(tempfile.mkdtemp())
    (ws2 / "a.py").write_text("x = 1\n", encoding="utf-8")
    r3 = vcs.commit_slice(ws2, sl_id="s3", gained=[])
    check("非仓库跳过", not r3.get("ok") and "git" in str(r3.get("why")), str(r3))

    print()
    if FAILS:
        print(f"失败：{FAILS}")
        return 1
    print("全部通过（变化才提交 / 空变化不提交 / 只 add+commit / 非仓库如实跳过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
