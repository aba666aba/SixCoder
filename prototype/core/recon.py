"""仓库形态识别（docs/10）与工具链探测（docs/11 §2）——全部确定性命令 + 解析。

2026-11-07 从 core/probe.py 拆出：probe.py 已重构为"验收探针"（cli/py_call/pytest/http），
仓库/环境探测的职责移到本模块。调用方：cli.py 的 doctor（工具链）/ probe（仓库形态）
命令，与 agent.py 主循环的探测步骤。拆出原因：586294b 重构 probe.py 时删除了这两个函数
却未同步调用点，导致 `cli.py doctor` / `cli.py probe` / agent 探测步直接 AttributeError。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

GREENFIELD = "GREENFIELD"
SCAFFOLDED = "SCAFFOLDED"
BROKEN_BASELINE = "BROKEN_BASELINE"
YOUNG = "YOUNG"
GROWING = "GROWING"
MATURE = "MATURE"

STATE_DOC = {
    GREENFIELD: "空目录 / 无提交：契约先行 + 模板全量生成",
    SCAFFOLDED: "有提交但几乎无源码：复用骨架，只插节点",
    BROKEN_BASELINE: "构建或测试不绿：先建立可验证基线，功能需求后置",
    YOUNG: "有源码无测试：先补测试地基，再开发",
    GROWING: "源码 + 测试：正常增量，最小补丁",
    MATURE: "源码 + 测试 + CI + 迁移 + 多人：强约束增量",
}

SRC_DIRS = {"src", "app", "lib", "pkg", "core"}
CODE_EXT = {".py", ".java", ".js", ".ts", ".vue", ".go", ".rs", ".kt", ".cs", ".php", ".rb"}
IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
               "target", ".idea", ".vscode", ".pytest_cache", "workspaces"}


def _run(cmd: list[str], cwd: Path, timeout: int = 120, env: dict | None = None):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, env=env, shell=False)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "command not found"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"


def _walk(repo: Path, limit: int = 4000) -> list[Path]:
    out: list[Path] = []
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for f in files:
            out.append(Path(root) / f)
            if len(out) >= limit:
                return out
    return out


FULL_RUN_LIMIT = 120        # 用例数 <= 此值才在探测阶段跑全量


def classify(repo: Path, deep: bool = False) -> dict:
    """输出信号 + 判定状态（docs/10 §1、§2）。"""
    repo = Path(repo)
    signals: dict = {"path": str(repo)}
    if not repo.exists():
        return {"state": GREENFIELD, "signals": signals, "note": "路径不存在",
                "policy": STATE_DOC[GREENFIELD]}

    is_repo = (repo / ".git").exists()
    signals["is_repo"] = is_repo
    files = _walk(repo)
    tracked = [f for f in files if ".git" not in f.parts]
    signals["file_count"] = len(tracked)

    code = [f for f in tracked if f.suffix.lower() in CODE_EXT]
    signals["code_files"] = len(code)

    commits, dirty, authors = 0, 0, 0
    if is_repo:
        rc, out = _run(["git", "rev-list", "--count", "HEAD"], repo, 20)
        commits = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
        rc, out = _run(["git", "status", "--porcelain"], repo, 20)
        dirty = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else 0
        rc, out = _run(["git", "shortlog", "-sne", "HEAD"], repo, 20)
        authors = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else 0
    signals.update(commit_count=commits, dirty_files=dirty, author_count=authors)

    has_src = any(d.name in SRC_DIRS for d in repo.iterdir() if d.is_dir()) or signals["code_files"] > 0
    signals["has_src"] = has_src

    tests = [f for f in tracked if re.search(r"(^|/)(test_.*\.py|.*_test\.py|.*Test\.java|.*\.spec\.ts)$", f.as_posix())]
    signals["test_files"] = len(tests)
    signals["has_tests"] = len(tests) > 0

    signals["has_ci"] = (repo / ".github" / "workflows").exists() or (repo / ".gitlab-ci.yml").exists()
    signals["has_migrations"] = any((repo / d).exists() for d in ("migrations", "db/migration", "alembic"))
    signals["has_lockfile"] = any((repo / f).exists() for f in
                                  ("package-lock.json", "pom.xml", "poetry.lock", "requirements.txt", "go.sum"))

    # 语言构成
    comp: dict[str, int] = {}
    for f in code:
        comp[f.suffix.lower()] = comp.get(f.suffix.lower(), 0) + 1
    signals["lang_composition"] = dict(sorted(comp.items(), key=lambda kv: -kv[1])[:5])

    # 基线分三层，成本递增（大仓库不能一上来跑全量）：
    #   L0 收集：pytest --collect-only —— 秒级，能抓语法/import 错误（大仓库最常见的问题）
    #   L1 全量：只在用例数 <= FULL_RUN_LIMIT 时跑（小仓库几百毫秒）
    #   L2 深验：deep=True 或 CLI --run-tests 时强制跑全量
    tests_green = None
    collect_ok = None
    test_count = None
    baseline = "no-tests"
    if signals["has_tests"]:
        # 用当前解释器而不是裸 "python"：CI/虚拟环境里 PATH 上的 python 未必是同一个
        rc, out = _run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                        "-p", "no:cacheprovider"], repo, 90)
        collect_ok = rc in (0, 5)
        m = re.search(r"(\d+)\s+tests?\s+collected", out)
        if m:
            test_count = int(m.group(1))
        elif rc == 5:
            test_count = 0
        signals["collect_tail"] = "\n".join(out.strip().splitlines()[-3:])

        if not collect_ok:
            baseline = "collect-failed"          # 连用例都收集不起来 → 基线不可信
        elif test_count is not None and test_count <= FULL_RUN_LIMIT:
            rc2, out2 = _run([sys.executable, "-m", "pytest", "-q", "--tb=no",
                              "-p", "no:cacheprovider"], repo, 600)
            tests_green = rc2 in (0, 5)
            baseline = "verified" if tests_green else "failing"
            signals["pytest_tail"] = "\n".join(out2.strip().splitlines()[-3:])
        elif deep:
            rc2, out2 = _run([sys.executable, "-m", "pytest", "-q", "--tb=no",
                              "-p", "no:cacheprovider"], repo, 1800)
            tests_green = rc2 in (0, 5)
            baseline = "verified" if tests_green else "failing"
            signals["pytest_tail"] = "\n".join(out2.strip().splitlines()[-3:])
        else:
            baseline = "unverified-large"        # 大仓库：收集通过但未跑全量
    signals.update(tests_green=tests_green, collect_ok=collect_ok,
                   test_count=test_count, baseline=baseline)

    if not is_repo and signals["file_count"] == 0:
        state, note = GREENFIELD, "空目录"
    elif commits == 0 and has_src:
        # 有源码但没有 git 历史：能开发但无版本保护，不该当成脚手架
        state, note = YOUNG, "有源码但未初始化 git（无提交历史）——建议先 git init 再改"
    elif not has_src:
        state, note = (GREENFIELD if signals["file_count"] == 0 else SCAFFOLDED), "无源码"
    elif tests_green is False or collect_ok is False:
        state, note = BROKEN_BASELINE, ("测试不绿，先建立基线" if tests_green is False
                                        else "测试用例收集失败（import/语法问题），先修基线")
    elif signals["has_tests"] and signals["has_ci"] and signals["has_migrations"] and authors >= 2:
        state, note = MATURE, STATE_DOC[MATURE]
    elif signals["has_tests"]:
        state, note = GROWING, STATE_DOC[GROWING]
    else:
        state, note = YOUNG, STATE_DOC[YOUNG]

    return {"state": state, "note": note, "signals": signals,
            "policy": STATE_DOC[state]}


# ---------------------------------------------------------------- 工具链

TC_SPEC = [
    ("java",   ["java", "-version"],  r'version "([\d.]+)', 17, "JAVA_HOME"),
    ("maven",  ["mvn", "-v"],         r"Apache Maven ([\d.]+)", 3, "M2_HOME"),
    ("node",   ["node", "-v"],        r"v([\d.]+)", 20, None),
    ("npm",    ["npm", "-v"],         r"([\d.]+)", 9, None),
    ("python", ["python", "--version"], r"Python ([\d.]+)", 3, None),
    ("git",    ["git", "--version"],  r"git version ([\d.]+)", 2, None),
    ("docker", ["docker", "--version"], r"([\d.]+)", 20, None),
]

COMMON_DIRS = {
    "java": ["C:/Program Files/Eclipse Adoptium/*", "C:/Program Files/Java/*", "D:/Java/*", "D:/jdk*"],
}


def _major(v: str) -> int:
    try:
        return int(str(v).split(".")[0])
    except Exception:
        return 0


def toolchains(overrides: dict | None = None) -> list[dict]:
    """探测顺序：用户设置 > 环境变量 > PATH > 常见目录（docs/11 §2.3）。"""
    overrides = overrides or {}
    result = []
    for tid, cmd, ver_re, need_major, env_key in TC_SPEC:
        entry = {"id": tid, "ok": False, "version": None, "path": None,
                 "source": None, "required_major": need_major,
                 "fix_hint": None}
        ov = (overrides.get(tid) or {}).get("exe") or (overrides.get(tid) or {}).get("home")
        if ov:
            ovs = str(ov)
            cands = []
            if ovs.lower().endswith((".exe", ".cmd", ".bat")):
                cands.append(ovs)
            else:
                base = os.path.join(ovs, "bin", cmd[0])
                # Windows 上实际是 java.exe / mvn.cmd，必须逐个试后缀，否则覆盖会被静默忽略
                cands += [base, base + ".exe", base + ".cmd", base + ".bat", ovs]
            for cand in cands:
                if Path(cand).is_file():
                    entry.update(path=cand, source="设置")
                    cmd = [cand] + cmd[1:]
                    break
        if not entry["path"]:
            env_dir = os.environ.get(env_key) if env_key else None
            if env_dir and Path(env_dir, "bin").exists():
                cand = Path(env_dir, "bin", cmd[0] + (".exe" if os.name == "nt" else ""))
                if cand.exists():
                    entry.update(path=str(cand), source=env_key)
        if not entry["path"]:
            which = shutil.which(cmd[0])
            if which:
                entry.update(path=which, source="PATH")
        if not entry["path"]:
            import glob
            for pat in COMMON_DIRS.get(tid, []):
                for hit in glob.glob(pat):
                    cand = Path(hit, "bin", cmd[0] + ".exe")
                    if cand.exists():
                        entry.update(path=str(cand), source="常见目录")
                        break
                if entry["path"]:
                    break
        if entry["path"]:
            rc, out = _run([entry["path"]] + cmd[1:], Path("."), 25)
            m = re.search(ver_re, out)
            version = m.group(1) if m else None
            entry["version"] = version
            entry["ok"] = bool(version) and _major(version) >= need_major
            if not entry["ok"]:
                entry["fix_hint"] = f"toolchain.{tid}.version_mismatch"
        else:
            entry["fix_hint"] = f"toolchain.{tid}.missing"
        result.append(entry)
    return result
