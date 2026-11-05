"""上传前体检：工作区 + 全量历史 扫密钥/隐私/不该入库的路径，输出打掩码。

对应 GITHUB_PREUPLOAD_SECURITY.md 第 4 节那份清单的自动化版本。
用法（仓库根执行）： python tools/preupload_scan.py
注意：只读，不写文件、不改历史、不联网；命中值一律打掩码，避免终端历史成为新的泄露点。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]      # 仓库根（不写死本机绝对路径）
ORIGIN = "origin/main"

PATTERNS = {
    "model_key(sk-)": r"\bsk-[A-Za-z0-9_\-]{16,}",
    "github_pat": r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",
    "aws_ak": r"AKIA[0-9A-Z]{16}",
    "aliyun_key": r"LTAI[A-Za-z0-9]{12,}",
    "private_key": r"BEGIN [A-Z ]*PRIVATE KEY",
    "phone_cn": r"(?<![\d.])1[3-9]\d{9}(?![\d.])",
    "id_card_cn": r"(?<!\d)\d{17}[\dXx](?!\d)",
    "db_url_with_pw": r"(?i)(mysql|mongodb|neo4j|redis|postgres)(\+[a-z]+)?://[^\s\"']*:[^\s\"'@]*@",
    "password_assign": r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']",
}
HISTORY_ONLY = ("model_key(sk-)", "github_pat", "aws_ak", "aliyun_key",
                "private_key", "db_url_with_pw")


def git(*args: str, text: bool = True):
    return subprocess.run(["git", *args], cwd=str(REPO), capture_output=True,
                          text=text, encoding="utf-8", errors="replace")


def mask(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return s[:2] + "***"
    return f"{s[:6]}…{s[-2:]}(len={len(s)})"


def files_in_scope() -> list[Path]:
    out = git("ls-files", "--cached", "--others", "--exclude-standard").stdout
    paths = []
    for rel in out.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        p = REPO / rel
        if p.is_file():
            paths.append(p)
    return paths


def scan_tree() -> list[tuple[str, str, str]]:
    hits = []
    for p in files_in_scope():
        try:
            if p.stat().st_size > 3_000_000:
                continue
            raw = p.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            continue
        text = raw.decode("utf-8", errors="replace")
        rel = p.relative_to(REPO).as_posix()
        for name, pat in PATTERNS.items():
            for m in re.finditer(pat, text):
                line_no = text[: m.start()].count("\n") + 1
                hits.append((name, f"{rel}:{line_no}", mask(m.group(0))))
    return hits


def history_commits(pattern: str) -> list[str]:
    out = git("log", "--all", "--format=%H", "-S", pattern.split("|")[0].replace("\\b", "")
              .replace("\\", "")).stdout
    return [x.strip() for x in out.splitlines() if x.strip()]


def in_range(sha: str, rng: str) -> bool:
    if not rng:
        return False
    return git("merge-base", "--is-ancestor", sha, rng).returncode == 0


def scan_history() -> tuple[list[tuple], int]:
    rows = []
    seen = set()
    for name, pat in PATTERNS.items():
        if name not in HISTORY_ONLY:
            continue
        needle = {"model_key(sk-)": "sk-", "github_pat": "ghp_", "aws_ak": "AKIA",
                  "aliyun_key": "LTAI", "private_key": "PRIVATE KEY",
                  "db_url_with_pw": "://"}[name]
        shas = set()
        # 同一个 needle 只问一次 git（以前按正则分支重复跑，等于把扫描放大 N 倍）
        out = git("log", "--all", "--format=%H", "-S", needle).stdout
        shas |= {x.strip() for x in out.splitlines() if x.strip()}
        if not shas:
            continue
        rx = re.compile(pat)
        for sha in sorted(shas):
            key = (name, sha)
            if key in seen:
                continue
            seen.add(key)
            diff = git("show", "--no-color", "--format=%ci", sha).stdout
            if not rx.search(diff):
                continue
            files = git("show", "--name-only", "--format=", sha).stdout.split()
            first = rx.search(diff)
            date = diff.splitlines()[0][:10] if diff.splitlines() else "?"
            rows.append((name, sha[:8], date, ",".join(files[:4]),
                         mask(first.group(0)) if first else ""))
    return rows, len(git("rev-list", "--all").stdout.split())


def main() -> int:
    print("=" * 70)
    print("1) 工作区（含未跟踪但未被忽略的文件）")
    tree = scan_tree()
    if not tree:
        print("   未命中")
    for name, where, val in tree[:60]:
        print(f"   [{name}] {where}  -> {val}")
    print(f"   合计 {len(tree)} 处")

    print("=" * 70)
    print("2) 历史（全部提交 / 全部引用）")
    rows, n_commits = scan_history()
    pushed = git("rev-parse", "--verify", ORIGIN).returncode == 0
    print(f"   仓库共 {n_commits} 个可达提交；远端 {ORIGIN} {'存在' if pushed else '不存在'}")
    head_sha = git("rev-parse", "main").stdout.strip()[:8]
    print(f"   本地 main = {head_sha}")
    if not rows:
        print("   未命中")
    for name, sha, date, files, val in rows[:40]:
        on_remote = in_range(sha + "^{commit}", ORIGIN) if pushed else False
        will_push = (not on_remote) if pushed else True
        print(f"   [{name}] {sha} {date} 文件={files} 值={val}")
        print(f"      已在远端历史? {'是' if on_remote else '否'}"
              f"｜本次会随 push 上去? {'是' if will_push else '否'}")
    print(f"   合计 {len(rows)} 处")

    print("=" * 70)
    print("3) 待推送范围")
    if pushed:
        rng = git("rev-list", "--count", f"{ORIGIN}..main").stdout.strip()
        files = git("diff", "--name-only", f"{ORIGIN}..main").stdout.splitlines()
        print(f"   {ORIGIN}..main = {rng} 个提交，涉及 {len(files)} 个文件")
    else:
        print("   没有远端引用，首次推送会把全部历史推上去")
    tracked = git("ls-files").stdout.splitlines()
    print(f"   当前索引跟踪 {len(tracked)} 个文件")
    bad_paths = [p for p in tracked if re.search(
        r"(settings\.json|\.db$|node_modules/|/\.sixcoder/|llm-config\.yml|\.env$|"
        r"_logs/|\.tmp/|dist/|__pycache__|\.pytest_cache)", p)]
    print(f"   其中不该入库的路径 {len(bad_paths)} 个")
    for p in bad_paths[:30]:
        print(f"      {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
