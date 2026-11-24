"""把 229 个自检脚本纳入 pytest（P1，2026-11-24）。

## 为什么做这个

之前自检是 checks/run_all.py 一次性跑完的独立体系：

    pytest 用例：  101 个（14 个文件）
    自检脚本：     229 个（手写 print + exit code）

两套体系的问题：

1. 没人跑：全量自检要 15-20 分钟，日常开发只跑 pytest ->
   15 个失效自检在仓库里躺了很久才被发现（2026-11-24 实测）。
2. 没有隔离：自检共享 prototype/.tmp/，一个脚本崩了可能影响下一个。
3. 无法筛选：pytest 有 -k / --lf / xdist，自检只有 --batch。

## 做法

不重写 229 个脚本（那才是真风险），而是用 parametrize 把它们逐个
作为独立 pytest 用例跑。这样：

    pytest tests/test_all_checks.py -q            # 全量
    pytest tests/test_all_checks.py -k ecosystem  # 只跑生态包相关
    pytest tests/test_all_checks.py --lf          # 只跑上次失败的

## 跳过规则（必须显式，不能静默）

· check_remote_http.py 需要后端在跑 -> 没设 SIXCODER_BASE_URL 就 skip，
  并在 skip 原因里写清楚怎么启用。skip 不等于 pass。
· 环境删除护栏拦截（SAFE_DELETE_BULK）-> xfail，说明是环境问题不是代码问题。

## 为什么默认标记为 slow

229 个脚本全跑要 15-20 分钟。常规 pytest 不应该被它拖慢；
想跑全量就显式加 -m slow。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECKS = ROOT / "prototype" / "checks"

#: 需要外部条件才能跑的自检
NEEDS_BACKEND = "check_remote_http.py"


def _scripts() -> list:
    return sorted(p.name for p in CHECKS.glob("check_*.py"))


def _child_env() -> dict:
    """子进程环境。必须强制 UTF-8。

    2026-11-24 实测：中文 Windows 下不设这个，7 个自检会
    UnicodeEncodeError: gbk codec can not encode 崩掉，
    看起来像「7 个自检失败」，实际是编码问题。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


SCRIPTS = _scripts()


@pytest.mark.slow
@pytest.mark.parametrize("name", SCRIPTS, ids=SCRIPTS)
def test_check_script(name: str):
    """跑一个自检脚本，退出码 0 才算通过。"""
    if name == NEEDS_BACKEND and not os.environ.get("SIXCODER_BASE_URL"):
        pytest.skip(
            name + " 需要后端在跑。启用方式："
            " 先起 python -m uvicorn server:app --port 8798，"
            " 再设 SIXCODER_BASE_URL=http://127.0.0.1:8798")

    proc = subprocess.run(
        [sys.executable, str(CHECKS / name)],
        cwd=str(ROOT / "prototype"),
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        env=_child_env(), timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")

    # 宿主护栏拦截 不等于 代码失败（沿用 run_all.py 的口径）
    if proc.returncode != 0 and "SAFE_DELETE_BULK" in out:
        pytest.xfail("环境删除护栏拦下（>50 文件/回合）：不是代码问题，请无护栏终端复跑")

    if proc.returncode != 0:
        lines = [ln.strip() for ln in out.splitlines()
                 if "FAIL" in ln or "Traceback" in ln or "Error" in ln]
        tail = "\n".join(lines[:15]) if lines else "\n".join(out.splitlines()[-10:])
        pytest.fail(name + " 退出码 " + str(proc.returncode) + "\n" + tail)


def test_scripts_discovered():
    """自检脚本必须能被发现（防路径写错导致 0 个用例静默通过）。"""
    assert len(SCRIPTS) > 200, "只发现 " + str(len(SCRIPTS)) + " 个自检脚本，路径可能不对"
