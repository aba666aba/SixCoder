"""编程语言层：**自动识别**主力语言 + 按语言切换验证模式。

设计原则（一条都不许松）：
  1. **能真验证才算验证**。Python 走现有的 pytest/mypy/ast 判据；Java/C/C++ 走真编译器 +
     真运行；JS/TS 走 node。做不到的语言（本机没工具链、或者需要转译器的 JSX/Vue）
     在**开跑前**就拒绝，并写清缺什么 —— 不许"生成了但没验证"却报成功。
  2. **契约里的示例级判据目前只有 Python 有**。别的语言即使编译通过，也只是"语法/类型级"，
     报告里必须写明"契约示例未机判"，验收数照样是 0，不许冒充通过。
  3. 识别不出来就按 Python 走（历史行为），不猜。

本次落地依据（都是本机实测过的命令）：
  · Java   : javac -d <out> *.java  → java -cp <out> <Main>      （实测打印 3）
  · C      : gcc -fsyntax-only / gcc -o → 运行                    （实测打印 3）
  · C++    : g++ 同上
  · JS     : node --check <file>                                  （实测无输出=通过）
  · TS     : node <file>（Node 22+ 类型剥离，直接跑）              （实测打印 2）

C 档落点 1（§20.6）：语言层从"能识别"升级成"能验" —— 给每种语言配好
  **测试套件命令**与**类型/静态检查命令**，用该语言自己的工具链真跑一遍。
  本机没装工具链（go/cargo/php/ruby/swift…）就如实说"测不了"，不假装通过。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 语言 → {后缀, 关键特征, 需要的命令, 验证器}
LANGS: dict[str, dict] = {
    "python": {
        "name": "Python",
        "exts": [".py"],
        "hints": [r"\bpython\b", r"\bpytest\b", r"\.py\b", r"\bdjango\b", r"\bfastapi\b"],
        "need": [sys.executable],
        "judge": "full",              # 契约示例级判据（现有的那套）
        "install": "已具备（当前主力语言）",
        "test_cmd": ["python", "-m", "pytest", "-q"],
        "typecheck_cmd": ["python", "-m", "compileall", "-q", "."],
        "has_tests": True,
    },
    "java": {
        "name": "Java",
        "exts": [".java"],
        "hints": [r"public\s+class\b", r"\bpackage\s+[\w.]+\s*;", r"\bjavac\b",
                  r"System\.out\.println", r"\bimport\s+java\."],
        "need": ["javac", "java"],
        "judge": "compile",           # 真编译（类型+语法），可运行 main
        "install": "装 JDK 17+ 并把 javac/java 加进 PATH",
        "test_cmd": ["mvn", "-q", "-B", "test"],
        "typecheck_cmd": ["mvn", "-q", "-B", "compile"],
        "has_tests": True,
    },
    "c": {
        "name": "C",
        "exts": [".c", ".h"],
        "hints": [r"#include\s*<", r"\bint\s+main\s*\(", r"\bprintf\s*\(", r"\bmalloc\s*\("],
        "need": ["gcc"],
        "judge": "compile",
        "install": "装 gcc（MinGW-w64 / MSYS2）并加进 PATH",
        "test_cmd": None,
        "typecheck_cmd": ["gcc", "-fsyntax-only", "-Wall"],
        "has_tests": False,
    },
    "cpp": {
        "name": "C++",
        "exts": [".cpp", ".cc", ".hpp", ".hxx"],
        "hints": [r"#include\s*<iostream>", r"\bstd::", r"\bstd::cout\b", r"\bnamespace\b"],
        "need": ["g++"],
        "judge": "compile",
        "install": "装 g++（MinGW-w64 / MSYS2）并加进 PATH",
        "test_cmd": None,
        "typecheck_cmd": ["g++", "-fsyntax-only", "-Wall"],
        "has_tests": False,
    },
    "javascript": {
        "name": "JavaScript",
        "exts": [".js", ".mjs", ".cjs"],
        "hints": [r"\bconst\b", r"\blet\b", r"=>", r"\brequire\(", r"\bmodule\.exports\b"],
        "need": ["node"],
        "judge": "syntax_run",        # node --check + 可运行
        "install": "装 Node.js 18+",
        "test_cmd": ["npm", "test", "--silent"],
        # node --check 能做单文件语法检查（2026-11-24 补；
        # 原来这里是 None，导致 JS 项目没有语法判据可跑）
        "typecheck_cmd": ["node", "--check"],
        "has_tests": True,
    },
    "typescript": {
        "name": "TypeScript",
        "exts": [".ts"],
        "hints": [r":\s*(string|number|boolean)\b", r"\binterface\s+\w+", r"\btype\s+\w+\s*=",
                  r"\bexport\s+(?:const|function|class)\b"],
        "need": ["node"],
        "judge": "syntax_run",        # Node 22+ 类型剥离，直接跑
        "install": "装 Node.js 22+（支持 TypeScript 类型剥离）",
        "test_cmd": ["npm", "test", "--silent"],
        "typecheck_cmd": ["npx", "tsc", "--noEmit"],
        "has_tests": True,
    },
    "go": {
        "name": "Go",
        "exts": [".go"],
        "hints": [r"\bpackage\s+main\b", r"\bfunc\s+\w+\s*\(", r"\bfmt\.Print"],
        "need": ["go"],
        "judge": "compile",
        "install": "装 Go 1.21+ 并加进 PATH",
        "test_cmd": ["go", "test", "./..."],
        "typecheck_cmd": ["go", "vet", "./..."],
        "has_tests": True,
    },
    "csharp": {
        "name": "C#",
        "exts": [".cs"],
        "hints": [r"\busing\s+System\b", r"\bnamespace\s+\w+", r"\bConsole\.WriteLine\b"],
        "need": ["dotnet"],
        "judge": "compile",
        "install": "装 .NET SDK 8（本机 dotnet 当前不可用：命令加载失败）",
        "test_cmd": ["dotnet", "test", "--nologo"],
        "typecheck_cmd": ["dotnet", "build", "--nologo"],
        "has_tests": True,
    },
    "rust": {
        "name": "Rust",
        "exts": [".rs"],
        "hints": [r"\bfn\s+main\s*\(", r"\blet\s+mut\b", r"\buse\s+std::"],
        "need": ["cargo", "rustc"],
        "judge": "compile",
        "install": "装 Rust（rustup）",
        "test_cmd": ["cargo", "test", "--quiet"],
        "typecheck_cmd": ["cargo", "check", "--quiet"],
        "has_tests": True,
    },
    "kotlin": {
        "name": "Kotlin", "exts": [".kt"], "hints": [r"\bfun\s+main\s*\(", r"\bval\s+\w+"],
        "need": ["kotlinc"], "judge": "compile", "install": "装 Kotlin 编译器",
        "test_cmd": ["mvn", "-q", "-B", "test"],
        "typecheck_cmd": ["mvn", "-q", "-B", "compile"],
        "has_tests": True,
    },
    "swift": {
        "name": "Swift", "exts": [".swift"], "hints": [r"\bfunc\s+\w+", r"\bimport\s+Foundation\b"],
        "need": ["swift"], "judge": "compile", "install": "装 Swift 工具链",
        "test_cmd": ["swift", "test"],
        "typecheck_cmd": None,
        "has_tests": True,
    },
    "php": {
        "name": "PHP", "exts": [".php"], "hints": [r"<\?php", r"\$\w+\s*="],
        "need": ["php"], "judge": "syntax_run", "install": "装 PHP 8+",
        "test_cmd": ["vendor/bin/phpunit"],
        "typecheck_cmd": None,
        "has_tests": True,
    },
    "ruby": {
        "name": "Ruby", "exts": [".rb"], "hints": [r"\bdef\s+\w+", r"\bputs\s+", r"\bend\b"],
        "need": ["ruby"], "judge": "syntax_run", "install": "装 Ruby 3+",
        "test_cmd": ["bundle", "exec", "rspec"],
        "typecheck_cmd": None,
        "has_tests": True,
    },
}

# 需要额外转译器的前端写法：本机没有对应工具链 → 明确不支持（不许假装）
FRONTEND_EXTRA = {
    "jsx": "JSX 需要 Babel/esbuild 转译（本机没有）",
    "tsx": "TSX 需要 Babel/esbuild 转译（本机没有）",
    "vue": "单文件组件需要 @vue/compiler（本机没有）",
    "svelte": "Svelte 需要编译器（本机没有）",
}

_EXT_TO_LANG = {ext: lang for lang, cfg in LANGS.items() for ext in cfg["exts"]}


_AVAIL_CACHE: dict[str, bool] = {}


def available(cmd: str) -> bool:
    """命令**真的能跑**才算有工具链。

    只看 which() 会骗人：本机 `dotnet` 在 PATH 里，但执行时报
    "The command could not be loaded"（实测）。所以这里真跑一次 --version。

    2026-11-11 修：Windows 上 `npm` / `npx` / `mvn` 是 `.CMD` 包装脚本，
    `subprocess.run(["npm", "--version"])` 会抛 FileNotFoundError（Python
    不走 shell 的 PATHEXT），于是明明装了 npm 却被判"没工具链" → 该语言的
    判据被全部跳过，**假绿**。用 `shutil.which()` 解析出的绝对路径执行即可。
    """
    if cmd in _AVAIL_CACHE:
        return _AVAIL_CACHE[cmd]
    ok = False
    resolved = shutil.which(cmd)
    if resolved:
        try:
            r = subprocess.run([resolved, "--version"], capture_output=True, text=True,
                               timeout=20, encoding="utf-8", errors="replace")
            ok = r.returncode == 0 or bool((r.stdout or "").strip() or (r.stderr or "").strip())
            if "could not be loaded" in ((r.stderr or "") + (r.stdout or "")).lower():
                ok = False
        except Exception:                                # noqa: BLE001
            ok = False
    _AVAIL_CACHE[cmd] = ok
    return ok


def detect(requirement: str, paths: list[str] | None = None) -> dict:
    """识别主力语言。判据顺序：模块后缀 → 语言特征关键词 → 回退 Python。"""
    text = requirement or ""
    counts: dict[str, int] = {}
    for p in (paths or []):
        ext = Path(str(p)).suffix.lower()
        lang = _EXT_TO_LANG.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    for ext, note in FRONTEND_EXTRA.items():
        if f".{ext}" in text.lower():
            return {"lang": ext, "name": ext.upper(), "by": "后缀", "ok": False,
                    "why": note, "need": [], "install": "换成本机能验证的写法（.ts/.js/.vue→.tsx 之外）"}
    if counts:
        lang = max(counts, key=lambda k: counts[k])
        return _describe(lang, f"模块后缀（{counts[lang]} 个文件）")
    scores = {}
    for lang, cfg in LANGS.items():
        if lang == "python":          # Python 特征太宽，放最后当兜底
            continue
        scores[lang] = sum(len(re.findall(h, text)) for h in cfg["hints"])
    best = max(scores, key=lambda k: scores[k]) if scores else "python"
    if not scores or scores[best] == 0:
        return _describe("python", "没识别出其它语言特征 → 按 Python")
    return _describe(best, f"语言特征关键词（命中 {scores[best]} 处）")


def _describe(lang: str, by: str) -> dict:
    cfg = LANGS.get(lang) or LANGS["python"]
    missing = [c for c in cfg["need"] if not available(c)]
    ok = not missing
    return {"lang": lang, "name": cfg["name"], "by": by, "ok": ok,
            "why": "" if ok else f"缺工具链：{', '.join(missing)}",
            "need": cfg["need"], "install": cfg["install"],
            "judge": cfg["judge"], "exts": cfg["exts"]}


def judge_level(lang: str) -> str:
    """该语言此刻能给出的证据等级（给报告用，不许夸大）。

    C 档落点 1：配了 test_cmd 且工具链齐全 → 如实升为"测试级"（真跑该语言
    测试套件）；工具链不全仍按旧口径"无法验证（缺 xxx）"，绝不假装测过。
    """
    cfg = LANGS.get(lang) or {}
    miss = [c for c in cfg.get("need", []) if not available(c)]
    if miss:
        return f"无法验证（缺 {', '.join(miss)}）"
    if cfg.get("test_cmd") and _tool_ready(cfg["test_cmd"][0]):
        return "测试级（真跑该语言的测试套件；契约示例未机判）"
    if cfg.get("judge") == "compile":
        return "编译级（真编译器过语法+类型；契约示例未机判）"
    return "运行级（能真跑通；契约示例未机判）"


def verify(ws: Path, rel: str, lang: str, timeout: int = 180) -> dict:
    """用**该语言自己的工具链**验证一个文件。返回 {ok, detail, how}。"""
    ws = Path(ws)
    p = ws / rel
    cfg = LANGS.get(lang) or {}
    miss = [c for c in cfg.get("need", []) if not available(c)]
    if miss:
        # 2026-11-11 修：**先查工具链，再查文件**。
        # 旧口径先判 p.exists()，Go 任务缺编译器时（文件本来就没写）报
        # "x.go 不存在" → 把人往"文件没落盘"方向带，而真因是本机没装 Go。
        # 零花费拒绝路径要求理由点名缺什么，错误信息不能指错方向。
        return {"ok": False, "detail": f"缺工具链 {', '.join(miss)}（不假装验证）", "how": "no-tool"}
    if not p.exists():
        return {"ok": False, "detail": f"{rel} 不存在", "how": "missing"}
    try:
        if lang == "java":
            out = ws / ".sixcoder" / "tmp" / "javac"
            out.mkdir(parents=True, exist_ok=True)
            r = subprocess.run(["javac", "-encoding", "UTF-8", "-d", str(out), str(p)],
                               capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "javac",
                    "detail": (r.stderr or r.stdout or "javac 通过").strip()[:600]}
        if lang in ("c", "cpp"):
            cc = "g++" if lang == "cpp" else "gcc"
            r = subprocess.run([cc, "-fsyntax-only", "-Wall", str(p)],
                               capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": f"{cc} -fsyntax-only",
                    "detail": (r.stderr or r.stdout or f"{cc} 通过").strip()[:600]}
        if lang == "javascript":
            r = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "node --check",
                    "detail": (r.stderr or r.stdout or "node --check 通过").strip()[:600]}
        if lang == "typescript":
            # Node 22+ 能直接跑 .ts（类型剥离）；用 --check 会误报，这里用"真跑一次"
            r = subprocess.run(["node", str(p)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "node <ts>（类型剥离运行）",
                    "detail": (r.stderr or r.stdout or "运行通过").strip()[:600]}
        if lang == "go":
            # C 档：从"只 go vet"升级成"真编译"。有 go.mod 时 `go build ./...`
            # 才是覆盖全仓的编译证据；没有 go.mod（单文件需求）时退回
            # `go vet <file>`，如实报 how，不假装编译过整个模块。
            gomod = ws / "go.mod"
            if gomod.is_file():
                r = subprocess.run(["go", "build", "./..."], cwd=str(ws),
                                   capture_output=True, text=True, timeout=timeout,
                                   encoding="utf-8", errors="replace")
                return {"ok": r.returncode == 0, "how": "go build ./...",
                        "detail": (r.stderr or r.stdout or "go build 通过").strip()[:600]}
            r = subprocess.run(["go", "vet", str(p)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "go vet（单文件，未编译全仓）",
                    "detail": (r.stderr or r.stdout or "go vet 通过").strip()[:600]}
        if lang == "csharp":
            r = subprocess.run(["dotnet", "build", "--nologo", "-v", "q"],
                               cwd=str(ws), capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "dotnet build",
                    "detail": (r.stderr or r.stdout or "构建通过").strip()[:600]}
        if lang == "rust":
            r = subprocess.run(["rustc", "--edition", "2021", "--emit=metadata", str(p)],
                               capture_output=True, text=True, timeout=timeout,
                               encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "rustc --emit=metadata",
                    "detail": (r.stderr or r.stdout or "rustc 通过").strip()[:600]}
        if lang == "php":
            r = subprocess.run(["php", "-l", str(p)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "php -l",
                    "detail": (r.stdout or r.stderr or "php -l 通过").strip()[:600]}
        if lang == "ruby":
            r = subprocess.run(["ruby", "-c", str(p)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "ruby -c",
                    "detail": (r.stdout or r.stderr or "ruby -c 通过").strip()[:600]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "how": "timeout", "detail": f"验证超过 {timeout}s"}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "how": type(exc).__name__,
                "detail": f"{type(exc).__name__}: {exc}"[:300]}
    return {"ok": False, "how": "unsupported", "detail": f"没有 {lang} 的验证方式（不假装通过）"}


# ---------------------------------------------------------------------------
# C 档落点 1：语言级"能验" —— 用该语言自己的测试/类型检查命令真跑一遍。
# ---------------------------------------------------------------------------

def test_command(lang: str) -> list[str] | None:
    """该语言在产物仓库根目录跑测试套件的命令；没配（c/cpp）返回 None。"""
    return (LANGS.get(lang) or {}).get("test_cmd") or None


def typecheck_command(lang: str) -> list[str] | None:
    """该语言的类型/静态检查命令；没配（javascript/swift/php/ruby）返回 None。"""
    return (LANGS.get(lang) or {}).get("typecheck_cmd") or None


def _tool_ready(tool: str) -> bool:
    """工具链是否真的在（继续用 available() 的 _AVAIL_CACHE）。

    为什么加 which 兜底（2026-11-11 实测）：Windows 上 npm/npx/mvn 是 .CMD 包装
    脚本，CreateProcess 不补 PATHEXT，available() 里 [tool,"--version"] 会
    FileNotFoundError → "装了却判没装"。shutil.which 能找到 .CMD，补上这个洞。
    """
    if tool == "python":
        return True                       # 实际用 sys.executable 跑，当前解释器必然在
    return available(tool) or (shutil.which(tool) is not None)


def _effective_cmd(cmd: list[str]) -> list[str]:
    """把命令首元素解析成真正能被执行的形式。

    为什么（本机实测）：
      · 裸 "python" 可能是别的解释器（没 pytest），而引擎跑在自带解释器
        （sys.executable，有 pytest）—— 用它才拿得到测试依赖。
      · Windows 上 npm/npx/mvn 是 .CMD，直接 Popen(["npm", ...]) 必
        FileNotFoundError；shutil.which 解析出带 .CMD 的绝对路径后才能真跑。
    """
    out = list(cmd)
    if not out:
        return out
    if out[0] == "python":
        out[0] = sys.executable
        return out
    real = shutil.which(out[0])
    if real:
        out[0] = real
    return out


def _tail(text: str, n: int = 800) -> str:
    t = text or ""
    return t[-n:] if len(t) > n else t


def _kill_tree(pid: int) -> None:
    """超时杀整棵进程树：孙进程（npm→node、pytest→worker）会继续占着资源。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:                                            # pragma: no cover - 非 Windows
            os.killpg(os.getpgid(pid), 9)
    except Exception:                                    # noqa: BLE001
        try:
            os.kill(pid, 9)
        except Exception:                                # noqa: BLE001
            pass


def _run_child(cmd: list[str], *, cwd: str, timeout: int) -> dict:
    """跑子进程：**输出写临时文件**（不用管道），超时杀**整棵进程树**。

    为什么不用管道读 stdout（core/probe.py:945 的血泪）：
      子进程若再起孙进程并继承了 stdout 管道，subprocess 到点 kill 掉子进程后
      communicate() 仍要等所有写端关闭 —— 孙进程活着就永远等下去（实测挂死
      13 分钟）。写文件没有这层耦合：到点就杀树，再读文件里已写下的内容。
    返回 {out, err, code, timed_out, exc}，永不抛异常。
    """
    out_f = err_f = None
    try:
        out_f = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8",
                                            errors="replace", suffix=".out", delete=False)
        err_f = tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8",
                                            errors="replace", suffix=".err", delete=False)
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out_f, stderr=err_f)
        timed_out = False
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                code = proc.wait(timeout=10)
            except subprocess.TimeoutExpired:            # 杀不动也不能再等
                code = -9
        for fh in (out_f, err_f):
            fh.flush()
            fh.seek(0)
        out = out_f.read() or ""
        err = err_f.read() or ""
        return {"out": out, "err": err, "code": code, "timed_out": timed_out, "exc": None}
    except Exception as exc:                             # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"
        return {"out": "", "err": msg, "code": -1, "timed_out": False, "exc": msg}
    finally:
        for fh in (out_f, err_f):
            if fh is not None:
                try:
                    name = fh.name
                    fh.close()
                    os.unlink(name)
                except Exception:                        # noqa: BLE001
                    pass


def _typecheck_sources(ws, lang: str) -> list[str]:
    """c/cpp 的 `-fsyntax-only` 必须带源文件，光跑 `gcc` 会 "no input files" 假红。

    只扫产物仓里**用户代码**（跳过 .sixcoder / node_modules / .git / 隐藏目录），
    按路径排序保证可重复。找不到源文件就返回空 → 由 `_run` 如实判"测不了"。

    2026-11-23 修（与 `enterprise._iter_py` 同一个 bug）：旧版对 **`p.parts`（整条
    绝对路径的所有段）** 做 `startswith(".tmp")` 判断，于是
        · 产物仓在 `C:\\Users\\x\\AppData\\Local\\Temp\\...` 下 → 命中 `Temp`？不，命中不了；
        · 但产物仓只要在**任何** `.tmp*` 目录下（包括 `prototype/.tmp/`，
          自检全在那儿跑），所有 .c 源文件被静默跳过 → "找不到源文件" → 判"测不了"。
    只看仓内相对路径，才是"跳过仓里的临时目录"，而不是"跳过仓本身"。
    """
    if lang not in ("c", "cpp"):
        return []
    exts = (".c", ".h") if lang == "c" else (".cpp", ".cc", ".cxx", ".hpp", ".h")
    skip = {".sixcoder", "node_modules", ".git", "__pycache__", ".tmp"}
    root = Path(ws).resolve()
    out: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in exts:
            continue
        try:
            dirs = p.relative_to(root).parts[:-1]
        except ValueError:
            continue
        if any(part in skip or str(part).startswith(".tmp") for part in dirs):
            continue
        out.append(str(p))
    return out


def _run(ws, lang: str, timeout: int, ok_how: str) -> dict:
    """run_tests / run_typecheck 的公共实现；返回固定五键，如实反映真跑结果。"""
    cmd = test_command(lang) if ok_how == "test" else typecheck_command(lang)
    base = {"ok": False, "how": ok_how, "cmd": [], "detail": "", "skipped": False}
    if not cmd:
        return {**base, "how": "no-cmd", "cmd": [],
                "detail": f"{lang} 没配 {ok_how} 命令（不假装通过）", "skipped": True}
    if lang in ("c", "cpp") and ok_how == "typecheck":
        srcs = _typecheck_sources(ws, lang)
        if not srcs:
            return {**base, "how": "no-cmd", "cmd": list(cmd),
                    "detail": f"{lang} 没有可检查的源文件（不假装通过）", "skipped": True}
        cmd = list(cmd) + srcs
    tool = cmd[0]
    if not _tool_ready(tool):
        return {**base, "how": "no-toolchain", "cmd": list(cmd),
                "detail": f"缺工具链 {tool}（本机没装，如实说测不了）", "skipped": True}
    effective = _effective_cmd(cmd)
    r = _run_child(effective, cwd=str(Path(ws).resolve()), timeout=timeout)
    tail = _tail((r["out"] or "") + (r["err"] or ""))
    if r["timed_out"]:
        return {**base, "how": "timeout", "cmd": effective,
                "detail": f"{ok_how} 超过 {timeout}s\n{tail}"}
    if r["exc"]:
        return {**base, "how": "error", "cmd": effective,
                "detail": f"命令起不来：{r['exc']}\n{tail}"}
    if r["code"] == 0:
        return {"ok": True, "how": ok_how, "cmd": effective,
                "detail": tail or f"{ok_how} 通过", "skipped": False}
    return {**base, "how": "failed", "cmd": effective,
            "detail": f"{ok_how} 命令返回码 {r['code']}\n{tail}"}


def run_tests(ws, lang: str, timeout: int = 300) -> dict:
    """在产物仓库根目录真跑该语言的测试套件。

    返回 {ok, how, cmd, detail, skipped}：
      · ok=True 仅当命令真跑完且 returncode==0；
      · skipped=True 仅当没配命令或工具链没装（如实说测不了，不假装通过）；
      · detail 失败时带 stdout/stderr 尾部 800 字符。
    """
    return _run(ws, lang, timeout, "test")


def run_typecheck(ws, lang: str, timeout: int = 300) -> dict:
    """在产物仓库根目录真跑该语言的类型/静态检查。语义同 run_tests。"""
    return _run(ws, lang, timeout, "typecheck")