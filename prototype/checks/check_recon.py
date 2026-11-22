"""仓库/环境探测自检：classify（仓库形态）与 toolchains（工具链）恢复后可用。

这条钉的是 2026-11-07 修掉的回归：586294b 重构 probe.py 时删除了 classify/toolchains
却未同步 cli.py/agent.py 的调用点，导致 `cli.py doctor` / `cli.py probe` / agent 探测步
直接 AttributeError。修复后这两个函数落在 core/recon.py（与"验收探针" probe.py 分离），
本检查确保：classify 能判出仓库形态、toolchains 能列出工具链、字段齐全。

跑法: python checks/check_recon.py
"""
from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import paths, recon  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("仓库/环境探测自检（classify 形态 + toolchains 工具链）")

    print("[1] classify：不存在的路径 → GREENFIELD + 说明")
    r = recon.classify(Path("Z:/no/such/path_9f3k"))
    check("state=GREENFIELD", r["state"] == recon.GREENFIELD, str(r["state"]))
    check("note 写清路径不存在", "不存在" in r["note"], r["note"])
    check("返回结构含 policy/signals", "policy" in r and "signals" in r)

    print("[2] classify：空目录 → GREENFIELD")
    with tempfile.TemporaryDirectory() as td:
        r = recon.classify(Path(td))
        check("空目录 = GREENFIELD", r["state"] == recon.GREENFIELD, str(r["state"]))
        check("signals 有 file_count=0", r["signals"].get("file_count") == 0)

    print("[3] classify：有源码无 git → YOUNG（不该当脚手架）")
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "calc.py").write_text("def add(a, b):\n    return a + b\n",
                                          encoding="utf-8")
        r = recon.classify(Path(td))
        check("有源码无 git = YOUNG", r["state"] == recon.YOUNG, str(r["state"]))
        check("note 建议先 git init", "git" in r["note"].lower(), r["note"])
        check("signals 数到 1 个代码文件", r["signals"].get("code_files") == 1)

    print("[4] toolchains：声明的全部工具，字段齐全")
    tcs = recon.toolchains()
    ids = [t["id"] for t in tcs]
    check("列出 TC_SPEC 声明的全部工具",
          len(tcs) == len(recon.TC_SPEC) and len(tcs) >= 9, str(ids))
    check("java/maven/node/python/git/docker/npm/go/gcc 都在",
          {"java", "maven", "node", "python", "git", "docker", "npm", "go", "gcc"} <= set(ids))
    for t in tcs:
        check(f"{t['id']} 字段齐全（ok/version/path/source）",
              all(k in t for k in ("id", "ok", "version", "path", "source")), str(t.keys()))
    py = next(t for t in tcs if t["id"] == "python")
    check("python 工具链可用（本机必有解释器）", py["ok"] is True, str(py))

    print("[5] 版本正则：MinGW 首行的 x86_64 不能被当成 gcc 版本")
    gcc_re = next(r for tid, _cmd, r, _maj, _env in recon.TC_SPEC if tid == "gcc")
    sample = ("gcc.exe (x86_64-win32-seh-rev0, Built by MinGW-Builds project) 15.2.0\n"
              "Copyright (C) 2025 Free Software Foundation, Inc.\n")
    m = re.search(gcc_re, sample)
    check("gcc 版本解析为 15.2.0（不是 86）", bool(m) and m.group(1) == "15.2.0",
          m.group(1) if m else "no match")
    git_re = next(r for tid, _cmd, r, _maj, _env in recon.TC_SPEC if tid == "git")
    m = re.search(git_re, "git version 2.55.0.")
    check("git 版本无尾点", bool(m) and m.group(1) == "2.55.0", m.group(1) if m else "no match")

    print("[6] 自带运行时兜底：gcc 探测可以找到 mingw-* 目录")
    with tempfile.TemporaryDirectory() as td:
        fake_bin = Path(td) / "runtimes" / "mingw-16" / "bin"
        fake_bin.mkdir(parents=True)
        (fake_bin / "gcc.exe").write_bytes(b"")
        with patch.object(paths, "home", return_value=Path(td)):
            hits = recon._bundled_dirs("gcc")
        check("gcc → mingw-16 映射生效", any("mingw-16" in h for h in hits), str(hits))

    print("[7] 收集失败要说人话：pytest 缺失不能只报 collect-failed")
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        (Path(td) / "test_calc.py").write_text(
            "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
        real_run = recon._run

        def fake_run(cmd, cwd, timeout=120, env=None):
            if "pytest" in cmd:
                return 1, "No module named pytest"
            return real_run(cmd, cwd, timeout, env)

        with patch.object(recon, "_run", side_effect=fake_run):
            r = recon.classify(Path(td))
        sig = r["signals"]
        check("缺 pytest 仍判 collect-failed", sig.get("baseline") == "collect-failed",
              str(sig.get("baseline")))
        check("给出 collect_reason 人话", "没装 pytest" in (sig.get("collect_reason") or ""),
              str(sig.get("collect_reason")))
        check("保留 pytest 原始尾巴", "No module named pytest" in (sig.get("collect_tail") or ""),
              str(sig.get("collect_tail")))

    print("[8] 界面：收集失败不能只给一句红字")
    app = (Path(__file__).resolve().parents[1] / "web" / "src" / "App.vue").read_text(encoding="utf-8")
    check("界面显示 pytest 尾巴", 'class="probe-tail"' in app
          and "probeInfo.signals.collect_tail" in app)
    check("界面显示 collect_reason（缺 pytest 的人话）",
          "probeInfo.signals.collect_reason" in app)
    check("界面给出排查命令", "pytest --collect-only" in app)

    print("[9] 项目自带 .venv 优先（真实项目依赖不在引擎解释器里）")
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / ".venv" / "Scripts" / "python.exe"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"")
        got = recon.project_python(Path(td))
        check("找到 .venv/Scripts/python.exe", got == str(fake), got)
        check("没有 venv 时退回当前解释器",
              recon.project_python(Path(td) / "nope") == sys.executable)
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "calc.py").write_text("x = 1\n", encoding="utf-8")
        (Path(td) / "test_calc.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
        r = recon.classify(Path(td))
        check("signals 带 python 字段", bool(r["signals"].get("python")), str(r["signals"].get("python")))
        check("无 venv 时 project_venv=False", r["signals"].get("project_venv") is False)

    print("[10] Git 也是可选自带运行时（装不上 Git 不许崩后端）")
    from core import runtimes as _rt
    check("runtimes 登记了 git", "git" in _rt.RUNTIMES, list(_rt.RUNTIMES))
    g = _rt.RUNTIMES.get("git", {}).get("versions", {}).get("2.56.1", {})
    check("MinGit 用 cmd/git.exe", g.get("bin_sub") == "cmd" and g.get("exe") == "git.exe", g)
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "runtimes" / "git-2.56.1" / "cmd" / "git.exe"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"")
        with patch.object(paths, "home", return_value=Path(td)):
            hits = recon._bundled_dirs("git")
        check("recon 能找到 runtimes/git-*", any("git-2.56.1" in h for h in hits), str(hits))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（形态判定 / 信号字段 / 工具链全项 / 版本正则 / 自带运行时兜底 / 收集失败说明 / 界面 / venv / git）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
