"""CLI 判据包 —— 阶段 2（2026-11-24）。

## 验证什么

阶段 1 把判据做成了可插拔的（Judge + applies_to），但**光有接口不算证明**。
要证明确实"通用"，必须真跑一个非 Web 项目。

这个包提供 CLI/脚本类项目能用的判据：不假设有 HTTP、不假设有数据库。

## 为什么先做 CLI

它是**最小可验证的非 Web 形态**：
  - 有明确的成功判据（退出码 0）
  - 有可观测的输出（stdout）
  - 有可检查的产物（文件存在）
如果连 CLI 都抽不出来，说明抽象错了。

## 判据设计原则（沿用在 Web 判据上学到的教训）

1. **宁可判红，不可静默通过** —— 没有可测的东西要明说"跳过"，
   而不是算"通过"（`props.py:414` 的教训）
2. **结论必须带证据** —— 退出码是多少、输出里有没有那段文本、文件在不在
3. **不猜** —— 命令没配置就是没配置，不替用户编一个默认命令

## 配置来源

CLI 项目在 `.sixcoder/cli.json` 里写：

    {
      "run": "python main.py",
      "smoke_args": "--help",
      "expect_stdout": ["用法", "usage"],
      "artifacts": ["dist/out.txt"]
    }

**不写 = 没有 CLI 判据可跑**，注册表返回空表，引擎明确报"该栈没有判据"，
而不是假装通过。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .gates import GateResult
from .judges import ANY_STACK, STACK_CLI, CtxJudge, JudgeContext, JudgeRegistry

CONFIG_REL = ".sixcoder/cli.json"


def load_config(ws: Path) -> dict:
    """读 CLI 配置。失败返回空字典（调用方据此判定"没配"）。"""
    p = Path(ws) / CONFIG_REL
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return d if isinstance(d, dict) else {}


def _child_env() -> dict:
    """子进程环境：强制 UTF-8。

    为什么必须有这个（实测 2026-11-24）：中文 Windows 下子进程默认用 GBK
    输出，父进程按 UTF-8 解码就得到乱码（"用法" 变成 "\ufffd\ufffd\ufffd"），
    于是 expect_stdout 永远匹配不上 —— 判据会**稳定地误判失败**。

    这与 SixCoder 里 console.py 解决的是同一个坑（那是自身脚本的 stdout，
    这里是**被测子进程**的 stdout）。父进程光切自己的编码不够，
    必须把 PYTHONIOENCODING 传下去。
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # 子进程的中文输出还受控制台代码页影响；chcp 太重，靠上面两个变量足够
    return env


def _run(ws: Path, cmd: str, extra: str = "", timeout: int = 60) -> tuple[int, str]:
    """跑命令，返回 (退出码, 合并输出)。命令本身有问题也返回，不抛。"""
    full = (cmd + (" " + extra if extra else "")).strip()
    try:
        proc = subprocess.run(
            full, shell=True, cwd=str(ws), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=_child_env(),
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, f"超时（{timeout}s）"
    except Exception as exc:                                  # noqa: BLE001
        return -1, f"{type(exc).__name__}: {exc}"


# ── 判据实现 ────────────────────────────────────────────

def judge_exit_code(ctx: JudgeContext) -> GateResult:
    """程序跑得起来，退出码为 0。

    这是 CLI 项目最硬的判据：连启动都做不到，后面全都是空谈。
    """
    t0 = time.time()
    cfg = load_config(ctx.ws)
    run = str(cfg.get("run") or "").strip()
    if not run:
        return GateResult("cli.exit_code", False, "config",
                          "no-run-command",
                          f"没配运行命令：在 {CONFIG_REL} 里写 {{\"run\": \"...\"}}",
                          int((time.time() - t0) * 1000))
    args = str(cfg.get("smoke_args") or "").strip()
    code, out = _run(ctx.ws, run, args, int(cfg.get("timeout") or 60))
    ms = int((time.time() - t0) * 1000)
    if code == 0:
        return GateResult("cli.exit_code", True, "exit_code", "0",
                          f"`{run} {args}` 退出码 0", ms)
    tail = (out or "").strip().splitlines()[-6:]
    return GateResult("cli.exit_code", False, "exit_code", str(code),
                      f"`{run} {args}` 退出码 {code}：" + " | ".join(tail)[:400], ms)


def judge_stdout(ctx: JudgeContext) -> GateResult:
    """输出里必须出现约定的文本（证明它真干了活，不是空跑）。"""
    t0 = time.time()
    cfg = load_config(ctx.ws)
    expect = cfg.get("expect_stdout") or []
    if not expect:
        return GateResult("cli.stdout", True, "config", "no-expectation",
                          "没配 expect_stdout，跳过（没有可检查的输出）",
                          int((time.time() - t0) * 1000))
    run = str(cfg.get("run") or "").strip()
    if not run:
        return GateResult("cli.stdout", False, "config", "no-run-command",
                          "有 expect_stdout 但没有 run 命令", int((time.time() - t0) * 1000))
    args = str(cfg.get("smoke_args") or "").strip()
    code, out = _run(ctx.ws, run, args, int(cfg.get("timeout") or 60))
    ms = int((time.time() - t0) * 1000)
    missing = [e for e in expect if str(e) not in out]
    if not missing:
        return GateResult("cli.stdout", True, "stdout", f"{len(expect)}/{len(expect)}",
                          f"输出里出现了全部 {len(expect)} 段约定文本", ms)
    return GateResult("cli.stdout", False, "stdout", f"缺 {len(missing)}",
                      "输出里找不到：" + "、".join(str(m)[:40] for m in missing[:5]),
                      ms)


def judge_artifact(ctx: JudgeContext) -> GateResult:
    """约定的产物文件必须真的存在（且非空）。"""
    t0 = time.time()
    cfg = load_config(ctx.ws)
    arts = cfg.get("artifacts") or []
    if not arts:
        return GateResult("cli.artifact", True, "config", "no-artifacts",
                          "没配 artifacts，跳过", int((time.time() - t0) * 1000))
    run = str(cfg.get("run") or "").strip()
    if run:
        _run(ctx.ws, run, str(cfg.get("smoke_args") or "").strip(),
             int(cfg.get("timeout") or 60))
    ms = int((time.time() - t0) * 1000)
    missing, empty = [], []
    for rel in arts:
        p = Path(ctx.ws) / str(rel)
        if not p.exists():
            missing.append(str(rel))
        elif p.is_file() and p.stat().st_size == 0:
            empty.append(str(rel))
    if missing:
        return GateResult("cli.artifact", False, "file", f"缺 {len(missing)}",
                          "产物文件不存在：" + "、".join(missing[:5]), ms)
    if empty:
        return GateResult("cli.artifact", False, "file", f"空 {len(empty)}",
                          "产物文件是空的：" + "、".join(empty[:5]), ms)
    return GateResult("cli.artifact", True, "file", f"ok/{len(arts)}",
                      f"{len(arts)} 个产物文件都存在且非空", ms)


# ── 注册进判据表 ────────────────────────────────────────

CLI_JUDGES = ("cli.exit_code", "cli.stdout", "cli.artifact")


def register_cli(reg: JudgeRegistry) -> None:
    """把 CLI 判据装进注册表，只对 cli 栈生效。"""
    reg.register(CtxJudge("cli.exit_code", judge_exit_code,
                          frozenset({STACK_CLI}), "程序能跑，退出码 0"))
    reg.register(CtxJudge("cli.stdout", judge_stdout,
                          frozenset({STACK_CLI}), "输出含约定文本"))
    reg.register(CtxJudge("cli.artifact", judge_artifact,
                          frozenset({STACK_CLI}), "产物文件存在且非空"))