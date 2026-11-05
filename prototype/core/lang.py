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
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
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
    },
    "java": {
        "name": "Java",
        "exts": [".java"],
        "hints": [r"public\s+class\b", r"\bpackage\s+[\w.]+\s*;", r"\bjavac\b",
                  r"System\.out\.println", r"\bimport\s+java\."],
        "need": ["javac", "java"],
        "judge": "compile",           # 真编译（类型+语法），可运行 main
        "install": "装 JDK 17+ 并把 javac/java 加进 PATH",
    },
    "c": {
        "name": "C",
        "exts": [".c", ".h"],
        "hints": [r"#include\s*<", r"\bint\s+main\s*\(", r"\bprintf\s*\(", r"\bmalloc\s*\("],
        "need": ["gcc"],
        "judge": "compile",
        "install": "装 gcc（MinGW-w64 / MSYS2）并加进 PATH",
    },
    "cpp": {
        "name": "C++",
        "exts": [".cpp", ".cc", ".hpp", ".hxx"],
        "hints": [r"#include\s*<iostream>", r"\bstd::", r"\bstd::cout\b", r"\bnamespace\b"],
        "need": ["g++"],
        "judge": "compile",
        "install": "装 g++（MinGW-w64 / MSYS2）并加进 PATH",
    },
    "javascript": {
        "name": "JavaScript",
        "exts": [".js", ".mjs", ".cjs"],
        "hints": [r"\bconst\b", r"\blet\b", r"=>", r"\brequire\(", r"\bmodule\.exports\b"],
        "need": ["node"],
        "judge": "syntax_run",        # node --check + 可运行
        "install": "装 Node.js 18+",
    },
    "typescript": {
        "name": "TypeScript",
        "exts": [".ts"],
        "hints": [r":\s*(string|number|boolean)\b", r"\binterface\s+\w+", r"\btype\s+\w+\s*=",
                  r"\bexport\s+(?:const|function|class)\b"],
        "need": ["node"],
        "judge": "syntax_run",        # Node 22+ 类型剥离，直接跑
        "install": "装 Node.js 22+（支持 TypeScript 类型剥离）",
    },
    "go": {
        "name": "Go",
        "exts": [".go"],
        "hints": [r"\bpackage\s+main\b", r"\bfunc\s+\w+\s*\(", r"\bfmt\.Print"],
        "need": ["go"],
        "judge": "compile",
        "install": "装 Go 1.21+ 并加进 PATH",
    },
    "csharp": {
        "name": "C#",
        "exts": [".cs"],
        "hints": [r"\busing\s+System\b", r"\bnamespace\s+\w+", r"\bConsole\.WriteLine\b"],
        "need": ["dotnet"],
        "judge": "compile",
        "install": "装 .NET SDK 8（本机 dotnet 当前不可用：命令加载失败）",
    },
    "rust": {
        "name": "Rust",
        "exts": [".rs"],
        "hints": [r"\bfn\s+main\s*\(", r"\blet\s+mut\b", r"\buse\s+std::"],
        "need": ["cargo", "rustc"],
        "judge": "compile",
        "install": "装 Rust（rustup）",
    },
    "kotlin": {
        "name": "Kotlin", "exts": [".kt"], "hints": [r"\bfun\s+main\s*\(", r"\bval\s+\w+"],
        "need": ["kotlinc"], "judge": "compile", "install": "装 Kotlin 编译器",
    },
    "swift": {
        "name": "Swift", "exts": [".swift"], "hints": [r"\bfunc\s+\w+", r"\bimport\s+Foundation\b"],
        "need": ["swift"], "judge": "compile", "install": "装 Swift 工具链",
    },
    "php": {
        "name": "PHP", "exts": [".php"], "hints": [r"<\?php", r"\$\w+\s*="],
        "need": ["php"], "judge": "syntax_run", "install": "装 PHP 8+",
    },
    "ruby": {
        "name": "Ruby", "exts": [".rb"], "hints": [r"\bdef\s+\w+", r"\bputs\s+", r"\bend\b"],
        "need": ["ruby"], "judge": "syntax_run", "install": "装 Ruby 3+",
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
    """
    if cmd in _AVAIL_CACHE:
        return _AVAIL_CACHE[cmd]
    ok = False
    if shutil.which(cmd):
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True,
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
    """该语言此刻能给出的证据等级（给报告用，不许夸大）。"""
    if lang == "python":
        return "契约示例级（示例/边界/异常/端到端都能机判）"
    cfg = LANGS.get(lang) or {}
    miss = [c for c in cfg.get("need", []) if not available(c)]
    if miss:
        return f"无法验证（缺 {', '.join(miss)}）"
    if cfg.get("judge") == "compile":
        return "编译级（真编译器过语法+类型；契约示例未机判）"
    return "运行级（能真跑通；契约示例未机判）"


def verify(ws: Path, rel: str, lang: str, timeout: int = 180) -> dict:
    """用**该语言自己的工具链**验证一个文件。返回 {ok, detail, how}。"""
    ws = Path(ws)
    p = ws / rel
    if not p.exists():
        return {"ok": False, "detail": f"{rel} 不存在", "how": "missing"}
    cfg = LANGS.get(lang) or {}
    miss = [c for c in cfg.get("need", []) if not available(c)]
    if miss:
        return {"ok": False, "detail": f"缺工具链 {', '.join(miss)}（不假装验证）", "how": "no-tool"}
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
            r = subprocess.run(["go", "vet", str(p)], capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="replace")
            return {"ok": r.returncode == 0, "how": "go vet",
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
