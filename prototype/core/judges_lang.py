"""多语言判据包 —— 按技术栈切换验证命令（2026-11-24）。

## 解决什么

lang.py 早就定义了 12 种语言的 test_cmd / typecheck_cmd，
但那只是**工具链元数据**，没有接进判据体系。
于是：Python 项目有判据，Java/JS/C 项目没有 —— 又回到"只能做部分软件"。

## 设计：判据就是"跑那个语言的官方验证命令"

每种语言的判据不需要重写一套，而是复用 lang.py 里已经写好的命令：

    python  -> python -m compileall .        （语法/类型）
               python -m pytest -q           （测试）
    java    -> mvn -q -B compile             （类型+语法）
               mvn -q -B test                （测试）
    js/ts   -> node --check <file>           （语法）
               npm test --silent             （测试）
    go      -> go vet ./...                  （类型）
               go test ./...                 （测试）
    csharp  -> dotnet build --nologo         （类型+语法）
               dotnet test --nologo          （测试）
    c/cpp   -> gcc/g++ -fsyntax-only -Wall   （语法）

## 三条硬规则

1. **工具链不可用 ≠ 通过**。没装 JDK 时说"跳过"而不是"通过"。
   这沿用 props.py:414 修完后的口径：没有判据不等于做对了。

2. **命令退出码是唯一结论**。不看输出文本猜测 —— 各语言的输出格式不同，
   猜格式迟早误判。退出码 0 = 过，非 0 = 不过，并附输出尾巴作证据。

3. **不认识的语言明说 UNSUPPORTED**。不替用户编一个默认验证方式。

## 与 CLI 判据包的分工

cli.json 那套验的是"这个程序的行为对不对"（退出码/输出/产物）。
本包验的是"这个语言的代码写对了吗"（能不能编译/类型检查/测试通过）。
两者正交，一个项目可能两个都跑。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from .gates import GateResult
from .judges import ANY_STACK, CtxJudge, JudgeContext, JudgeRegistry

# 语言名 -> 栈标识。栈标识沿用 lang.py 的键。
LANG_STACKS = (
    "python", "java", "javascript", "typescript", "csharp",
    "c", "cpp", "go", "rust", "kotlin", "swift", "php", "ruby",
)


def _needs_files(cmd: list, lang: str) -> bool:
    """这条命令需要显式给源文件吗（gcc/g++ 这类）。

    C/C++ 的 -fsyntax-only 不带参数就报 "no input files"（实测 2026-11-24）。
    """
    head = Path(str(cmd[0])).name.lower()
    if head in ("gcc", "gcc.exe", "g++", "g++.exe", "clang", "clang++"):
        return True
    # node --check 必须跟一个文件，否则只打印帮助（不算检查过）
    return head in ("node", "node.exe") and "--check" in [str(c) for c in cmd]


def _source_files(ws: Path, lang: str) -> list:
    """项目里这个语言的源文件（相对路径，最多 50 个）。"""
    exts = {
        "c": (".c", ".h"),
        "cpp": (".cpp", ".cc", ".hpp", ".hxx", ".c"),
        "javascript": (".js", ".mjs", ".cjs"),
    }.get(lang, ())
    if not exts:
        return []
    out = []
    for f in sorted(ws.rglob("*")):
        if f.is_file() and f.suffix.lower() in exts:
            rel = f.relative_to(ws).as_posix()
            if "/build/" in rel or rel.startswith("build/"):
                continue
            out.append(rel)
            if len(out) >= 50:
                break
    return out


def _child_env() -> dict:
    """子进程强制 UTF-8（中文 Windows 下 GBK 输出会被解成乱码）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _has_toolchain(lang_cfg: dict) -> tuple[bool, str]:
    """这个语言需要的工具都在 PATH 上吗？不在就说缺什么。"""
    need = lang_cfg.get("need") or []
    missing = []
    for exe in need:
        base = Path(str(exe)).name
        if shutil.which(base) is None:
            missing.append(base)
    if missing:
        return False, "缺工具链：{}（{}）".format(
            "、".join(missing), lang_cfg.get("install") or "请先安装")
    return True, ""


def _resolve_exe(name: str) -> str:
    """把命令名解析成真实可执行路径。

    为什么必须有（实测 2026-11-24）：Windows 上 mvn / npm 是 .cmd 包装脚本，
    subprocess 不经过 shell 直接 exec("mvn") 会 WinError 2 找不到文件。
    shutil.which 会按 PATHEXT 找到 mvn.cmd，返回完整路径就能直接 exec。
    """
    found = shutil.which(str(name))
    return found or str(name)


def _run_cmd(ws: Path, cmd: list, timeout: int) -> tuple[int, str]:
    """跑一条命令，返回 (退出码, 输出尾巴)。不抛。"""
    if not cmd:
        return -1, "命令为空"
    argv = [_resolve_exe(str(c)) if i == 0 else str(c) for i, c in enumerate(cmd)]
    try:
        p = subprocess.run(
            argv, cwd=str(ws), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=_child_env(),
        )
        out = ((p.stdout or "") + (p.stderr or "")).strip()
        return p.returncode, out[-600:]
    except subprocess.TimeoutExpired:
        return -9, "超时（{}s）".format(timeout)
    except Exception as exc:                              # noqa: BLE001
        return -1, "{}: {}".format(type(exc).__name__, exc)


def _lang_of(ctx: JudgeContext) -> str:
    """从上下文里判定语言：显式 extras 优先，其次看文件后缀。"""
    forced = str((ctx.extras or {}).get("lang") or "").strip().lower()
    if forced:
        return forced
    try:
        from . import lang as _lang
        return str(_lang.detect(ctx.ws) or "python").lower()
    except Exception:                                     # noqa: BLE001
        return "python"


def judge_typecheck(ctx: JudgeContext) -> GateResult:
    """这个语言的代码能通过类型/语法检查吗。"""
    t0 = time.time()
    from . import lang as _lang
    name = _lang_of(ctx)
    cfg = _lang.LANGS.get(name)
    if not cfg:
        return GateResult("lang.typecheck", False, "config", "unsupported",
                          "不认识的语言 {}：没有对应的验证方式".format(name),
                          int((time.time() - t0) * 1000))
    ok_env, why = _has_toolchain(cfg)
    if not ok_env:
        # 工具链缺失 ≠ 通过。明确判为"没法验证"。
        return GateResult("lang.typecheck", False, "toolchain", "missing",
                          "{}：{}".format(cfg.get("name"), why),
                          int((time.time() - t0) * 1000))
    cmd = list(cfg.get("typecheck_cmd") or [])
    if not cmd:
        return GateResult("lang.typecheck", True, "config", "no-typecheck",
                          "{} 没有配置类型检查命令（跳过，不算通过）".format(cfg.get("name")),
                          int((time.time() - t0) * 1000))
    if _needs_files(cmd, name):
        files = _source_files(ctx.ws, name)
        if not files:
            return GateResult("lang.typecheck", False, "config", "no-source",
                              "{} 项目里找不到源文件".format(cfg.get("name")),
                              int((time.time() - t0) * 1000))
        cmd = cmd + files
    code, out = _run_cmd(ctx.ws, cmd, int((ctx.extras or {}).get("timeout") or 180))
    ms = int((time.time() - t0) * 1000)
    label = " ".join(str(c) for c in cmd)
    if code == 0:
        return GateResult("lang.typecheck", True, "exit_code", "0",
                          "`{}` 通过（{}）".format(label, cfg.get("name")), ms)
    return GateResult("lang.typecheck", False, "exit_code", str(code),
                      "`{}` 退出码 {}：{}".format(label, code, out[:400]), ms)


def judge_unittest(ctx: JudgeContext) -> GateResult:
    """这个语言的测试跑得过吗。"""
    t0 = time.time()
    from . import lang as _lang
    name = _lang_of(ctx)
    cfg = _lang.LANGS.get(name)
    if not cfg:
        return GateResult("lang.tests", False, "config", "unsupported",
                          "不认识的语言 {}".format(name),
                          int((time.time() - t0) * 1000))
    if not cfg.get("has_tests"):
        return GateResult("lang.tests", True, "config", "no-tests",
                          "{} 没有测试框架约定（跳过，不算通过）".format(cfg.get("name")),
                          int((time.time() - t0) * 1000))
    ok_env, why = _has_toolchain(cfg)
    if not ok_env:
        return GateResult("lang.tests", False, "toolchain", "missing",
                          "{}：{}".format(cfg.get("name"), why),
                          int((time.time() - t0) * 1000))
    cmd = cfg.get("test_cmd")
    if not cmd:
        return GateResult("lang.tests", True, "config", "no-test-cmd",
                          "{} 没有配置测试命令".format(cfg.get("name")),
                          int((time.time() - t0) * 1000))
    code, out = _run_cmd(ctx.ws, cmd, int((ctx.extras or {}).get("test_timeout") or 300))
    ms = int((time.time() - t0) * 1000)
    label = " ".join(str(c) for c in cmd)
    if code == 0:
        return GateResult("lang.tests", True, "exit_code", "0",
                          "`{}` 通过".format(label), ms)
    return GateResult("lang.tests", False, "exit_code", str(code),
                      "`{}` 退出码 {}：{}".format(label, code, out[:400]), ms)


LANG_JUDGES = ("lang.typecheck", "lang.tests")


def register_lang(reg: JudgeRegistry) -> None:
    """注册多语言判据：**对任何栈都生效**（由判据内部按语言决定用哪条命令）。

    为什么用 ANY_STACK 而不是给每种语言单独注册：
    判据要跑哪条命令取决于**项目实际语言**，而不是外层声明的栈。
    一个 fastapi 项目里可能同时有 TS 前端 —— 用 ANY_STACK 让判据自己判断，
    比在外层堆 13 个 per-language 判据更实在。
    """
    reg.register(CtxJudge("lang.typecheck", judge_typecheck,
                          frozenset({ANY_STACK}), "按语言跑类型/语法检查"))
    reg.register(CtxJudge("lang.tests", judge_unittest,
                          frozenset({ANY_STACK}), "按语言跑测试"))