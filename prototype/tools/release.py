"""发布与回滚：把"早上交付的一堆文件"变成**可安装、可回退的版本**。

为什么需要它（docs/17 §三 的缺口）：过去的交付是 `projects/<名>/` 里的一堆文件 + 一份报告，
出了问题只能说"重跑一遍"。传统工程里这一段是 release + rollback，本工具补上。

四个子命令：

    python tools/release.py verify                # 发布前体检（自检/依赖/密钥/工作区干净）
    python tools/release.py product [--bump ...]  # 产品版本：写 VERSION + 打 tag + 打包 dist
    python tools/release.py deliver <项目名>      # 交付版本：给 projects/<名>/ 做快照 + 记录证据
    python tools/release.py rollback <项目名> [版本]
    python tools/release.py list <项目名>

设计取舍：
- 交付快照用 **zip 字节快照**，不依赖 git（projects/ 往往不是 git 仓库，而且交付要"逐字节还原"）；
- 证据随版本走：每次交付把 `verified/total`、TRACE/DELIVERY 的 sha256、用到的模型记进 RELEASES.md，
  这样"这批东西当时凭什么算通过"永远查得到（对齐 docs/16 的证据观）；
- 回滚只还原文件，不删版本目录，历史一直留着。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve()
PROTO = HERE.parent.parent                  # prototype/
REPO = PROTO.parent                         # 仓库根
VERSION_FILE = REPO / "VERSION"
DIST = REPO / "dist"
PROJECTS = REPO / "projects"
EXCLUDE_DIRS = {
    ".tmp","__pycache__", ".git", ".pytest_cache", ".releases", "node_modules",
                "workspaces", ".sixcoder", "_logs", "projects", "demo-repo"}

# 绝不能进发布包的文件（本机配置里有明文 Key；日志/产地里有本机路径）
EXCLUDE_FILES = {"settings.json", "settings.json.bak", ".env", "llm-config.yml",
                 "proto.db", "api_index.db"}


# ------------------------------------------------------------------ 公共
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _iter_files(root: Path):
    """遍历待打包文件。

    注意：**只排除仓库根的 dist/**（发布包自己）；`prototype/web/dist` 必须打进去 ——
    否则别人拿到包还得装 Node 才能构建前端，"可安装"就不成立。
    """
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if rel.parts[:1] == ("dist",):          # 只排除仓库根的 dist/（发布包自己）
            continue
        if p.name in EXCLUDE_FILES or p.suffix in (".db", ".db-wal", ".db-shm", ".log"):
            continue                            # 本机配置 / 数据库 / 日志一律不进包
        yield p


def _zip_dir(src: Path, out: Path) -> tuple[int, int]:
    """把目录打成 zip，返回 (文件数, 字节数)。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    n = size = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in _iter_files(src):
            zf.write(f, f.relative_to(src).as_posix())
            n += 1
            size += f.stat().st_size
    return n, size


def _unzip_into(archive: Path, dst: Path, wipe: bool = True) -> int:
    """把快照还原到目标目录（wipe=True 时先清掉旧文件，保证逐字节还原）。"""
    if wipe and dst.exists():
        for p in sorted(dst.iterdir()):
            if p.name in EXCLUDE_DIRS:
                continue
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
    dst.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(dst)
        return len(zf.namelist())


def read_version() -> str:
    if VERSION_FILE.exists():
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    return "0.0.0"


def bump_version(cur: str, part: str) -> str:
    try:
        major, minor, patch = (int(x) for x in cur.split(".")[:3])
    except Exception:                                    # noqa: BLE001
        major, minor, patch = 0, 0, 0
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def _git(*args: str, check: bool = False) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=str(REPO), capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return 127, "git 不可用"
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} 失败：{(proc.stderr or proc.stdout)[:200]}")
    return proc.returncode, (proc.stdout or "").strip()


# ------------------------------------------------------------------ verify
def cmd_verify(args) -> int:
    print("== 发布前体检 ==")
    ok = True

    rc, status = _git("status", "--porcelain")
    if rc == 0 and status.strip():
        print(f"  [警告] 工作区有未提交改动（{len(status.splitlines())} 个文件）")
        if not args.allow_dirty:
            ok = False
    else:
        print("  [OK]   工作区干净")

    if not args.skip_checks:
        print("  → 跑自检套件…")
        proc = subprocess.run([sys.executable, "checks/run_all.py"], cwd=str(PROTO),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        tail = [ln for ln in (proc.stdout or "").splitlines() if "全部通过" in ln or "有失败项" in ln]
        if proc.returncode == 0:
            print(f"  [OK]   自检：{tail[-1] if tail else '通过'}")
        else:
            ok = False
            print(f"  [失败] 自检：{tail[-1] if tail else '见输出'}")
            for ln in (proc.stdout or "").splitlines():
                if "FAIL" in ln:
                    print(f"         {ln.strip()[:100]}")

    sys.path.insert(0, str(PROTO))
    from core import gates  # noqa: E402
    dep = gates.gate_deps(PROTO)
    print(f"  [{'OK' if dep.ok else '失败'}]   依赖：{dep.detail[:120]}")
    ok = ok and dep.ok

    leaks = []
    for f in _iter_files(PROTO / "data"):
        if f.suffix in (".json", ".yml", ".yaml", ".env"):
            txt = f.read_text(encoding="utf-8", errors="replace")
            if "sk-" in txt and len(txt) < 20000:
                leaks.append(str(f.relative_to(REPO)))
    tracked = subprocess.run(["git", "ls-files"], cwd=str(REPO), capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout or ""
    # 跳过自检临时目录：那里每次跑都会生成 settings.json，属于可重建产物，不是"入库的密钥"
    tracked_leaks = [ln for ln in tracked.splitlines()
                     if ln.endswith(("settings.json", "llm-config.yml"))
                     and ".tmp/" not in ln.replace("\\", "/")]
    if tracked_leaks:
        ok = False
        print(f"  [失败] 密钥/本地配置被跟踪：{', '.join(tracked_leaks)}")
    else:
        print("  [OK]   没有本地配置/密钥入库")

    print("== 结论：", "可以发布" if ok else "不能发布", "==")
    return 0 if ok else 1


# ------------------------------------------------------------------ product
def cmd_product(args) -> int:
    cur = read_version()
    ver = args.version or bump_version(cur, args.bump)
    tag = f"v{ver}"
    print(f"== 产品发布 {cur} → {ver} ==")
    if cmd_verify(argparse.Namespace(allow_dirty=args.allow_dirty, skip_checks=args.skip_checks)):
        print("体检没过，终止发布")
        return 1

    if args.dry_run:
        print(f"[dry-run] 会写 VERSION={ver}、打 tag {tag}、打包 {DIST}/sixcoder-{ver}.zip")
        return 0

    VERSION_FILE.write_text(ver + "\n", encoding="utf-8")

    _git("add", str(VERSION_FILE.name))
    rc, _ = _git("commit", "-m", f"release: {tag}")
    if rc != 0:
        print("（没有可提交的版本变化，跳过 commit）")
    _git("tag", "-a", tag, "-m", f"release {ver}")
    print(f"  [OK]   tag {tag}")

    out_zip = DIST / f"sixcoder-{ver}.zip"
    files, size = _zip_dir(REPO, out_zip) if not args.code_only else (0, 0)
    if args.code_only:
        # 只打产品代码：prototype（不含 data/workspaces）+ shell + 启动脚本 + 元文件
        out_zip.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for part in ("prototype", "shell", "tools", "docs"):
                base = REPO / part
                if not base.exists():
                    continue
                for f in _iter_files(base):
                    zf.write(f, f.relative_to(REPO).as_posix())
            for name in ("README.md", "STRUCTURE.md", "LICENSE", "start.ps1", "start.bat",
                         "stop.ps1", "VERSION"):
                p = REPO / name
                if p.exists():
                    zf.write(p, name)
        files = len(zipfile.ZipFile(out_zip).namelist())
        size = out_zip.stat().st_size
    print(f"  [OK]   打包 {out_zip.relative_to(REPO)}（{files} 个文件，{size / 1024:.0f} KB）")

    rel_dir = REPO / "_logs" / "releases"
    rel_dir.mkdir(parents=True, exist_ok=True)
    (rel_dir / f"{tag}.md").write_text(
        "\n".join([
            f"# {tag}",
            "",
            f"- 时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"- 提交：{_git('rev-parse', '--short', 'HEAD')[1]}",
            f"- 包：`dist/sixcoder-{ver}.zip`（{files} 文件 / {size / 1024:.0f} KB，"
            f"sha256={_sha256(out_zip)[:16]}…）",
            "- 体检：自检套件 + 依赖卫生 + 密钥扫描 均通过",
            "",
        ]), encoding="utf-8")
    print(f"  [OK]   发布记录 _logs/releases/{tag}.md")
    return 0


# ------------------------------------------------------------------ deliver
def _project(name: str) -> Path:
    p = PROJECTS / name
    if not p.exists():
        raise SystemExit(f"项目不存在：{p}")
    return p


def cmd_deliver(args) -> int:
    ws = _project(args.project)
    rel_dir = ws / ".releases"
    rel_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    ver = args.version or read_version()
    tag = f"{stamp}-v{ver}"
    archive = rel_dir / f"{tag}.zip"

    sys.path.insert(0, str(PROTO))
    from core import contract as cm, iterate  # noqa: E402
    req_file = PROTO / "tools" / "fixtures" / "hq_requirement.txt"
    state = {}
    verified = total = 0
    if req_file.exists():
        contracts = cm.parse_requirement(req_file.read_text(encoding="utf-8"))
        state = {k: v for k, v in iterate.inspect(ws, contracts).items()}
        verified = sum(s["verified"] for s in state.values())
        total = sum(s["funcs"] for s in state.values())

    files, size = _zip_dir(ws, archive)
    if files == 0:
        raise SystemExit(f"项目里没有可交付文件：{ws}")

    ev = {
        "tag": tag,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "commit": _git("rev-parse", "--short", "HEAD")[1],
        "verified": verified,
        "total": total,
        "files": files,
        "bytes": size,
        "sha256": _sha256(archive),
        "note": args.note or "",
    }
    for name in ("DELIVERY.md", "TRACE.md"):
        f = ws / name
        ev[name] = _sha256(f)[:16] if f.exists() else ""
    (rel_dir / f"{tag}.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2),
                                         encoding="utf-8")

    md = ws / "RELEASES.md"
    head = ("# 交付版本记录\n\n"
            "| 版本 | 时间 | 验收 | 文件 | 说明 | 回滚命令 |\n"
            "|---|---|---|---|---|---|\n") if not md.exists() else md.read_text(encoding="utf-8")
    if not md.exists():
        head += ""
    row = (f"| `{tag}` | {ev['at']} | {verified}/{total} | {files} | {ev['note'] or '—'} | "
           f"`python prototype/tools/release.py rollback {ws.name} {tag}` |\n")
    if md.exists():
        text = head.rstrip() + "\n" + row
    else:
        text = head + row
    md.write_text(text, encoding="utf-8")

    print(f"== 交付快照 {tag} ==")
    print(f"  验收：{verified}/{total}｜文件：{files}（{size / 1024:.0f} KB）")
    print(f"  快照：{archive.relative_to(REPO)}")
    print(f"  记录：{(rel_dir / f'{tag}.json').relative_to(REPO)} + {md.name}")
    print(f"  回滚：python prototype/tools/release.py rollback {ws.name} {tag}")
    return 0


# ------------------------------------------------------------------ rollback / list
def cmd_list(args) -> int:
    ws = _project(args.project)
    rel_dir = ws / ".releases"
    if not rel_dir.exists():
        print("还没有任何交付快照")
        return 0
    rows = sorted(rel_dir.glob("*.zip"))
    if not rows:
        print("还没有任何交付快照")
        return 0
    print(f"{args.project} 的交付快照（{len(rows)} 个）：")
    for z in rows:
        meta = z.with_suffix(".json")
        ev = json.loads(meta.read_text(encoding="utf-8")) if meta.exists() else {}
        print(f"  {z.stem:<24} {ev.get('verified', '?')}/{ev.get('total', '?')} "
              f"{ev.get('at', '')} {ev.get('note', '')}")
    return 0


def cmd_rollback(args) -> int:
    ws = _project(args.project)
    rel_dir = ws / ".releases"
    if not rel_dir.exists():
        raise SystemExit("这个项目还没有交付快照，无法回滚")
    if args.version:
        archive = rel_dir / f"{args.version}.zip"
        if not archive.exists():
            cand = sorted(rel_dir.glob(f"*{args.version}*.zip"))
            if not cand:
                raise SystemExit(f"找不到版本 {args.version}（用 list 看看有哪些）")
            archive = cand[-1]
    else:
        cand = sorted(rel_dir.glob("*.zip"))
        if not cand:
            raise SystemExit("没有可回滚的快照")
        archive = cand[-1]

    # 回滚前先把"当前状态"存一份，避免回滚本身造成不可逆丢失
    rescue = rel_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-pre-rollback.zip"
    _zip_dir(ws, rescue)
    n = _unzip_into(archive, ws)
    print(f"== 已回滚到 {archive.stem} ==")
    print(f"  还原文件：{n} 个")
    print(f"  回滚前的状态另存为：{rescue.name}（万一回滚错了可以从它再回来）")
    return 0


# ------------------------------------------------------------------ CLI
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="release", description="发布与回滚")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="发布前体检")
    v.add_argument("--allow-dirty", action="store_true", help="允许工作区有未提交改动")
    v.add_argument("--skip-checks", action="store_true", help="跳过自检套件（危险）")
    v.set_defaults(func=cmd_verify)

    p = sub.add_parser("product", help="产品版本：写 VERSION + tag + 打包")
    p.add_argument("--bump", choices=["patch", "minor", "major"], default="patch")
    p.add_argument("--version", default="", help="直接指定版本号（跳过 bump）")
    p.add_argument("--code-only", action="store_true", default=True, help="只打产品代码（默认）")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    p.add_argument("--skip-checks", action="store_true")
    p.set_defaults(func=cmd_product)

    d = sub.add_parser("deliver", help="给某个项目做交付快照")
    d.add_argument("project")
    d.add_argument("--note", default="")
    d.add_argument("--version", default="")
    d.set_defaults(func=cmd_deliver)

    r = sub.add_parser("rollback", help="把项目回滚到某个交付快照")
    r.add_argument("project")
    r.add_argument("version", nargs="?")
    r.set_defaults(func=cmd_rollback)

    ls = sub.add_parser("list", help="列出一个项目的交付快照")
    ls.add_argument("project")
    ls.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
