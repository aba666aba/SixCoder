"""仓库/环境探测自检：classify（仓库形态）与 toolchains（工具链）恢复后可用。

这条钉的是 2026-11-07 修掉的回归：586294b 重构 probe.py 时删除了 classify/toolchains
却未同步 cli.py/agent.py 的调用点，导致 `cli.py doctor` / `cli.py probe` / agent 探测步
直接 AttributeError。修复后这两个函数落在 core/recon.py（与"验收探针" probe.py 分离），
本检查确保：classify 能判出仓库形态、toolchains 能列出工具链、字段齐全。

跑法: python checks/check_recon.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import recon  # noqa: E402

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

    print("[4] toolchains：7 种工具，字段齐全")
    tcs = recon.toolchains()
    ids = [t["id"] for t in tcs]
    check("列出 7 种工具", len(tcs) == 7, str(ids))
    check("java/maven/node/python/git/docker 都在",
          {"java", "maven", "node", "python", "git", "docker", "npm"} <= set(ids))
    for t in tcs:
        check(f"{t['id']} 字段齐全（ok/version/path/source）",
              all(k in t for k in ("id", "ok", "version", "path", "source")), str(t.keys()))
    py = next(t for t in tcs if t["id"] == "python")
    check("python 工具链可用（本机必有解释器）", py["ok"] is True, str(py))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（形态判定 / 信号字段 / 工具链 7 项齐全）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
