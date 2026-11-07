"""--goal-file 长需求通道自检（2026-11-07）。

背景：全自主模式（--goal）才有规模扩产机制 `_target_lines`；而详细需求动辄
数千字，Windows 命令行长度放不下。以前只能二选一：要么丢掉细节用短目标，
要么用 --requirement-file 走按需求模式（没有规模机制）。补 --goal-file 让两者兼得。

跑法: python checks/check_goal_file.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "cli.py"
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run(*args: str) -> tuple[int, str]:
    p = subprocess.run([sys.executable, str(CLI), "night", "--repo",
                        str(ROOT.parent / "_sandbox" / "goal_file_probe"), *args],
                       cwd=str(ROOT), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=30)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    print("--goal-file 长需求通道自检")
    src = CLI.read_text(encoding="utf-8")
    check("CLI 注册了 --goal-file", '"--goal-file"' in src)
    check("读文件用 utf-8-sig（能吃 BOM）", 'encoding="utf-8-sig"' in src)
    check("与 --goal 互斥有校验", "--goal 与 --goal-file 只能用一个" in src)
    check("空文件有校验", "目标文件是空的" in src)

    tmp = ROOT / ".tmp"
    tmp.mkdir(exist_ok=True)
    good = tmp / "goal_good.md"
    # 模拟真实长需求：超过 Windows 命令行常见长度，且带规模声明
    good.write_text("目标代码规模：30000 行\n" + ("Django 双向选题与智能匹配系统。" * 400),
                    encoding="utf-8")
    empty = tmp / "goal_empty.md"
    empty.write_text("", encoding="utf-8")
    try:
        print("[2] 失败路径（都不该触发夜班/花钱）")
        rc, out = run("--goal-file", str(empty))
        check("空文件被拒绝", rc != 0 and "目标文件是空的" in out, out[:120])
        rc, out = run("--goal", "x", "--goal-file", str(good))
        check("与 --goal 冲突被拒绝", rc != 0 and "只能用一个" in out, out[:120])
        rc, out = run("--goal-file", str(tmp / "not_exist_zz.md"))
        check("文件不存在被拒绝", rc != 0 and "读不了目标文件" in out, out[:120])

        print("[3] 正常路径：长文本整份进 goal（借互斥校验让 CLI 立刻停下，零远端调用）")
        # 同时给 --requirement：CLI 先读目标文件并打印字符数，随后因互斥 sys.exit(2)，
        # 不会走到 Ollama / 蓝图派生 / 远端调用 —— 自检绝不花钱。
        rc, out = run("--goal-file", str(good), "--requirement", "x")
        check("读到了目标文件并报告字符数", "> 目标文件" in out and "字符" in out, out[:200])
        check("长文本未被截断（字符数远大于一句话）",
              any(int(n) > 1000 for n in __import__("re").findall(r"（(\d+) 字符）", out)),
              out[:200])
        check("互斥校验按预期拦下、没有启动夜班",
              rc != 0 and "只能用一个" in out and "全自主模式：目标" not in out, out[:200])
    finally:
        for p in (good, empty):
            try:
                p.unlink()
            except OSError:
                pass

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（长需求可进全自主模式 / 失败路径不花钱 / 规模声明可触发扩产）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
