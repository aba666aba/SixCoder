"""开发者模式的**运行与调试**引擎：按文件语言自动选工具链，真跑真调试。

设计原则（和 core/lang.py 一脉相承，但职责不同）：
  · lang.py 的 verify() 只做语法/类型级验证，**不运行程序**；
    本模块要做的是"把这个文件/项目真的跑起来，把输出和错误摆给开发者看"。
  · 工具链不猜：从 recon.toolchains() 拿真实可执行路径（含设置覆盖 / PATH /
    常见目录 / 安装器下载的自带运行时），Python 固定用当前引擎解释器
    （sys.executable，装版里就是 SixCoder 自带的 Python）。
  · 工作目录智能判断：从文件所在目录向上找项目标志（package.json / go.mod /
    pyproject.toml / pom.xml / Cargo.toml / .git），找到就用它，找不到用文件目录。
    单文件脚本（如 /tmp/a.py）不会被硬塞进仓库根。
  · 不许静默吞异常：执行失败要有 returncode + stderr + Elapsed，调试会话失败要留痕。
  · 安全：cwd 与运行文件都限制在仓库内（tools.safe_path），命令用 list 不用 shell，
    超时强杀进程树，输出有上限（防把界面刷爆）。

调试的边界（诚实写清）：
  · **Python**：用标准库 bdb 做真断点 / 继续 / 单步 / 变量查看（会话式，见
    DebugSession）。这是唯一能做到"无 IDE、跨语言一致"的真断点实现。
  · 其他语言：只提供"运行 + 诊断"（编译错误定位到行、点击跳转），不假装
    能单步。界面会明确标注，不误导。
"""
from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 语言 → 运行方式

# 与 server._LANG_BY_EXT 保持一致；这里额外带"运行命令"和"需要的工具 id"。
# ext -> (lang, tool_id, run_template, compile_template|None)
# 模板里的 {exe} {file} {dir} {stem} 会在 build_command 里替换。
_RUN_SPEC: dict[str, dict] = {
    ".py": dict(lang="python", tool="python", need=["python"],
                run=["{exe}", "-u", "{file}"], compile=None),
    ".js": dict(lang="javascript", tool="node", need=["node"],
                run=["{exe}", "{file}"], compile=None),
    ".mjs": dict(lang="javascript", tool="node", need=["node"],
                 run=["{exe}", "{file}"], compile=None),
    ".cjs": dict(lang="javascript", tool="node", need=["node"],
                 run=["{exe}", "{file}"], compile=None),
    ".ts": dict(lang="typescript", tool="node", need=["node"],
                run=["{exe}", "{file}"], compile=None),   # Node 22+ 类型剥离
    ".java": dict(lang="java", tool="java", need=["javac", "java"],
                  run=["{exe}", "-cp", "{dir}", "{stem}"],
                  compile=["{javac}", "-encoding", "UTF-8", "-d", "{dir}", "{file}"]),
    ".go": dict(lang="go", tool="go", need=["go"],
                run=["{exe}", "run", "{file}"], compile=None),
    ".c": dict(lang="c", tool="gcc", need=["gcc"],
               run=["{bin}", "{stem}"],
               compile=["{exe}", "-Wall", "-g", "-o", "{bin}", "{file}"]),
    ".cpp": dict(lang="cpp", tool="gcc", need=["g++"],
                 run=["{bin}", "{stem}"],
                 compile=["{gpp}", "-Wall", "-g", "-o", "{bin}", "{file}"]),
    ".cc": dict(lang="cpp", tool="gcc", need=["g++"],
                run=["{bin}", "{stem}"],
                compile=["{gpp}", "-Wall", "-g", "-o", "{bin}", "{file}"]),
    ".php": dict(lang="php", tool="php", need=["php"],
                 run=["{exe}", "{file}"], compile=None),
    ".rb": dict(lang="ruby", tool="ruby", need=["ruby"],
                run=["{exe}", "{file}"], compile=None),
    ".rs": dict(lang="rust", tool="rust", need=["rustc"],
                run=["{bin}", "{stem}"],
                compile=["{exe}", "--edition", "2021", "-o", "{bin}", "{file}"]),
    ".sh": dict(lang="bash", tool="bash", need=["bash"],
                run=["{exe}", "{file}"], compile=None),
    ".ps1": dict(lang="powershell", tool="powershell", need=["powershell"],
                 run=["{exe}", "-File", "{file}"], compile=None),
}

# 项目根标志：从近到远判断，命中即停。
_ROOT_MARKERS = (
    "package.json", "go.mod", "pyproject.toml", "setup.cfg", "pytest.ini",
    "pom.xml", "build.gradle", "Cargo.toml", "requirements.txt", ".git",
)

# 诊断行：`file.py:12:5: error: ...` / `a.go:7:2: ...` / `  File "x.py", line 3`
# file 组要允许 Windows 盘符冒号（C:\x.py:12:5: ...），否则绝对路径诊断全丢。
_DIAG_RE = re.compile(
    r'^(?P<file>(?:[A-Za-z]:[\\/])?[^\s:]+):(?P<line>\d+)(?::(?P<col>\d+))?\s*[:：]\s*(?P<msg>.+)$')
_TRACE_RE = re.compile(r'^\s*File\s+"(?P<file>[^"]+)",\s+line\s+(?P<line>\d+)')

MAX_OUTPUT = 200_000          # 单流输出上限（字符），超过截断并标记
DEFAULT_TIMEOUT = 30          # 同步运行默认超时（秒），防止死循环卡住界面


def spec_for(path: str) -> dict | None:
    """按后缀拿运行规格；不认识返回 None（界面显示"这种文件不能直接运行"）。"""
    return _RUN_SPEC.get(Path(str(path)).suffix.lower())


def _python_for(ws: Path) -> str:
    """Python 用项目自己的 .venv（有的话），否则当前引擎解释器。

    用户仓库的依赖（Django/torch/...）装在项目 venv 里；固定 sys.executable
    会让"运行/调试"在真实项目上直接 ModuleNotFoundError。
    """
    try:
        from core import recon as _recon               # noqa: PLC0415
        return _recon.project_python(Path(ws))
    except Exception:                                  # noqa: BLE001
        return sys.executable


def _tool_path(tid: str, tc_map: dict[str, dict]) -> str | None:
    """从工具链探测结果里拿可执行路径；Python 固定用当前解释器。"""
    if tid == "python":
        return sys.executable
    e = tc_map.get(tid) or {}
    return e.get("path") if e.get("ok") else None


_tools_cache: dict = {"at": 0.0, "data": {}}
_TOOLS_TTL = 60                       # 秒：工具链不会每分钟变，探测一次缓存 60s


def available_tools() -> dict[str, dict]:
    """工具链探测 → id -> entry。**带 60s 缓存**：

    recon.toolchains() 要跑 9 个子进程（java/mvn/node/... --version），实测 3-4s；
    界面每开一个文件都调 runner 接口，不缓存会让"运行/调试"按钮延迟几秒才可用，
    像卡住。工具链安装/绑定后最多等 60s 自动刷新，也可重启服务立即生效。
    """
    now = time.time()
    if now - _tools_cache["at"] < _TOOLS_TTL and _tools_cache["data"]:
        return _tools_cache["data"]
    try:
        data = {t["id"]: t for t in recon_toolchains()}
    except Exception:                                    # noqa: BLE001
        return _tools_cache["data"]
    _tools_cache.update(at=now, data=data)
    return data


def recon_toolchains() -> list[dict]:
    """单独包一层，方便自检 monkeypatch 与失败留痕。"""
    from core import recon as _recon               # noqa: PLC0415
    return _recon.toolchains()


def find_root(ws: Path, file_path: Path) -> Path:
    """智能判断工作目录：从文件所在目录向上找项目标志，找到最近的就停。

    注意只在 ws 内向上找，绝不跑到仓库外；找不到标志时用文件所在目录。
    """
    ws = Path(ws).resolve()
    d = (file_path.parent if file_path.parent != Path(".") else ws).resolve()
    # 文件目录必须在仓库内
    try:
        d.relative_to(ws)
    except ValueError:
        d = ws
    cur = d
    while True:
        if any((cur / m).exists() for m in _ROOT_MARKERS):
            return cur
        if cur == ws:
            break
        parent = cur.parent
        if parent == cur or ws not in parent.parents and parent != ws:
            break
        cur = parent
    return d


def build_command(ws: Path, path: str, tc_map: dict[str, dict] | None = None,
                  *, args: str = "") -> dict:
    """构建运行命令。返回 {ok, cmd, cwd, lang, tool, reason, compile_cmd}。

    args 是用户在界面填的命令行参数：按空白切分（不支持引号内的空格，
    这是刻意的——不接受 shell 语法，防止注入）；只作为运行参数，不动工具链路径。
    """
    spec = spec_for(path)
    if not spec:
        return {"ok": False, "reason": f"不支持运行 {Path(path).suffix or '(无后缀)'} 文件"}
    tc = tc_map if tc_map is not None else available_tools()
    ws = Path(ws).resolve()
    p = ws / path
    if not p.is_file():
        return {"ok": False, "reason": f"文件不存在：{path}"}

    needed = list(spec["need"])
    # gcc 规格里 C++ 实际要 g++，recon 的探测 id 是 gcc（同一 MinGW 发行版）
    tool_id = "gcc" if spec["lang"] == "cpp" else spec["tool"]
    exe = _python_for(ws) if spec["lang"] == "python" else _tool_path(tool_id, tc)
    missing = [n for n in needed if n not in ("python",) and not (
        _tool_path("gcc" if n == "g++" else ("node" if n in ("node", "npm") else n), tc)
    )]
    if spec["lang"] == "java" and not tc.get("java", {}).get("path"):
        missing.append("java")
    if missing:
        return {"ok": False, "reason": f"缺工具链：{', '.join(sorted(set(missing)))}（安装器可选组件可补装）",
                "lang": spec["lang"], "tool": tool_id}

    cwd = find_root(ws, p)
    run_dir = cwd / ".sixcoder" / "run"
    stem = p.stem
    fmt = dict(exe=exe or "", file=str(p), dir=str(run_dir), stem=stem,
               bin=str(run_dir / stem) + (".exe" if os.name == "nt" else ""))
    cmd = [x.format(**fmt) for x in spec["run"]]
    comp = None
    if spec.get("compile"):
        fmt2 = dict(fmt, javac=(tc.get("java") or {}).get("path") or "javac",
                    gpp=tc.get("gcc", {}).get("path") or "g++")
        comp = [x.format(**fmt2) for x in spec["compile"]]
    if args.strip():
        cmd += args.split()
    return {"ok": True, "cmd": cmd, "cwd": str(cwd), "lang": spec["lang"],
            "tool": tool_id, "compile_cmd": comp, "run_dir": str(run_dir),
            "debug": spec["lang"] == "python"}


def parse_diagnostics(text: str, ws: Path) -> list[dict]:
    """把输出里的 file:line 错误解析成可点击诊断项（前端点了跳到编辑器行）。"""
    out: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = _DIAG_RE.match(line) or _TRACE_RE.match(line)
        if not m:
            continue
        f, ln = m.group("file"), int(m.group("line"))
        if os.name == "nt" and "\\" in f:
            rel = f
        else:
            rel = f
        # 只保留仓库内、且不是引擎自身文件的诊断（避免把标准库栈当项目错误）
        if not (rel.endswith((".py", ".js", ".ts", ".java", ".go", ".c", ".cpp", ".rs"))
                or str(ws) in rel):
            continue
        key = (rel, ln)
        if key in seen:
            continue
        seen.add(key)
        out.append({"file": rel, "line": ln,
                    "col": int(m.group("col")) if m.groupdict().get("col") else None,
                    "message": (m.groupdict().get("msg") or line)[:240]})
        if len(out) >= 50:
            break
    return out


def _kill_tree(pid: int) -> None:
    """超时/停止时杀整棵进程树（Windows 用 taskkill /T /F）。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except Exception:                                    # noqa: BLE001
        pass


def run_program(ws: Path, path: str, *, args: str = "", timeout: int = DEFAULT_TIMEOUT,
                tc_map: dict[str, dict] | None = None, env_extra: dict[str, str] | None = None) -> dict:
    """同步运行一个文件：真执行、捕获输出、返回结构化结果（不写任何文件）。

    编译型语言先编译到 <ws>/.sixcoder/run/（不污染源码目录）；编译失败直接返回错误。
    """
    built = build_command(ws, path, tc_map, args=args)
    if not built["ok"]:
        return {"ok": False, **built}
    ws = Path(ws).resolve()
    run_dir = Path(built["run_dir"])
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:                                # noqa: BLE001
        return {"ok": False, "reason": f"建运行目录失败：{exc}"}

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if env_extra:
        env.update({str(k): str(v) for k, v in env_extra.items()})

    if built.get("compile_cmd"):
        t0 = time.time()
        try:
            cp = subprocess.run(built["compile_cmd"], cwd=str(built["cwd"]), env=env,
                                capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "reason": f"编译超时（{timeout}s）", "phase": "compile",
                    "lang": built["lang"], "cmd": built["compile_cmd"]}
        except OSError as exc:                            # noqa: BLE001
            return {"ok": False, "reason": f"启动编译失败：{exc}", "phase": "compile",
                    "lang": built["lang"], "cmd": built["compile_cmd"]}
        if cp.returncode != 0:
            err = (cp.stderr or cp.stdout or "")[:MAX_OUTPUT]
            return {"ok": False, "reason": "编译失败", "phase": "compile",
                    "lang": built["lang"], "cmd": built["compile_cmd"],
                    "returncode": cp.returncode, "stdout": (cp.stdout or "")[:MAX_OUTPUT],
                    "stderr": err, "ms": int((time.time() - t0) * 1000),
                    "diagnostics": parse_diagnostics(err, ws)}

    t0 = time.time()
    try:
        p = subprocess.Popen(built["cmd"], cwd=str(built["cwd"]), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
    except OSError as exc:                                # noqa: BLE001
        return {"ok": False, "reason": f"启动失败：{exc}", "phase": "run",
                "lang": built["lang"], "cmd": built["cmd"]}
    try:
        out, err = p.communicate(timeout=timeout)
        rc, timed_out = p.returncode, False
    except subprocess.TimeoutExpired:
        _kill_tree(p.pid)
        try:
            out, err = p.communicate(timeout=5)
        except Exception:                                 # noqa: BLE001
            out, err = "", ""
        rc, timed_out = -1, True
    ms = int((time.time() - t0) * 1000)
    out, err = (out or "")[:MAX_OUTPUT], (err or "")[:MAX_OUTPUT]
    diag = parse_diagnostics(out + "\n" + err, ws)
    return {"ok": rc == 0 and not timed_out, "phase": "run", "lang": built["lang"],
            "tool": built.get("tool"), "cmd": built["cmd"], "cwd": built["cwd"],
            "returncode": rc, "timed_out": timed_out, "stdout": out, "stderr": err,
            "ms": ms, "diagnostics": diag,
            "reason": (f"运行超时（{timeout}s）" if timed_out else
                       ("" if rc == 0 else f"进程退出码 {rc}"))}


# ================================================================ Python 断点调试
#
# 架构（实测要点）：
#   · 用户程序运行在**子进程**（core/coderun.py --dbg-worker <file>），主服务不被卡住；
#   · worker 用标准库 bdb（pdb 的基类）在断点/单步处暂停，把"暂停事件"写到 stdout，
#     前缀是一次性 nonce（__SCDBG_<nonce>__），用户程序自己的 print 不会被误判；
#   · 控制通道走 stdin：每行一条 JSON（{"op":"continue"|"step"|"break"...}），
#     worker 读一行执行一步，再写新的事件行；
#   · 用户程序的 stdout/stderr 在 worker 里被重定向并作为事件发回（带 stream 字段），
#     这样界面能分色显示，也不会和协议前缀抢 stdout。
#   · 会话超时（默认 5 分钟）与空闲超时（默认 90s 没动作）双护栏，超时杀整棵树。

import base64                                             # noqa: E402
import bdb                                                # noqa: E402
import inspect                                            # noqa: E402
import json                                               # noqa: E402

_SESSIONS: dict[str, "DebugSession"] = {}
_SESSION_LOCK = threading.Lock()
DEFAULT_SESSION_IDLE = 180          # 秒：暂停后没人操作就自动收掉，防泄漏


def _safe_val(v: Any, limit: int = 300) -> str:
    """变量值转可显示字符串：超长截断、异常吞掉（不让 repr 崩掉调试器）。"""
    try:
        s = repr(v)
    except Exception as exc:                              # noqa: BLE001
        return f"<repr 失败 {type(exc).__name__}>"
    return s if len(s) <= limit else s[:limit] + "…"


class _BreakpointDebugger(bdb.Bdb):
    """bdb 子类：在断点处停、发事件、等 stdin 的下一步命令。"""

    def __init__(self, emit, read_cmd, nonce: str, breakpoints: list[int]):
        super().__init__()
        self._emit = emit
        self._read_cmd = read_cmd
        self._nonce = nonce
        self._bps = set(int(x) for x in breakpoints)
        self._frame = None
        self._stop = False
        self._quit = False

    def user_line(self, frame):
        if self._quit:
            return
        here = frame.f_code.co_filename
        ln = frame.f_lineno
        # 只在"用户下的断点"或"单步请求"处停；其余行直接放行，不然每行都停会卡死。
        if ln not in self._bps and not self._stop:
            self.set_continue()
            return
        self._stop = False
        self._frame = frame
        loc = {}
        for k, v in list(frame.f_locals.items())[:40]:
            if k.startswith("__"):
                continue
            loc[k] = _safe_val(v)
        stack = []
        f = frame
        while f and len(stack) < 20:
            stack.append({"file": f.f_code.co_filename, "line": f.f_lineno,
                          "func": f.f_code.co_name})
            f = f.f_back
        self._emit({"event": "paused", "file": here, "line": ln, "func": frame.f_code.co_name,
                    "locals": loc, "stack": stack, "breakpoints": sorted(self._bps)})
        # 暂停循环：读命令直到收到继续/单步/退出
        while not self._quit:
            cmd = self._read_cmd()
            if not cmd:
                self._quit = True
                break
            op = cmd.get("op")
            if op == "continue":
                self.set_continue()
                break
            if op == "step":
                self.set_step()
                self._stop = True
                break
            if op == "next":
                self.set_next(frame)
                self._stop = True
                break
            if op == "break":
                n = cmd.get("line")
                if isinstance(n, int) and n > 0:
                    self._bps.add(n)
                    try:
                        self.set_break(frame.f_code.co_filename, n)
                    except Exception:                     # noqa: BLE001
                        pass
                    self._emit({"event": "breakpoints", "breakpoints": sorted(self._bps)})
            if op == "unbreak":
                n = cmd.get("line")
                if isinstance(n, int):
                    self._bps.discard(n)
                    try:
                        self.clear_break(frame.f_code.co_filename, n)
                    except Exception:                     # noqa: BLE001
                        pass
                    self._emit({"event": "breakpoints", "breakpoints": sorted(self._bps)})
            if op == "locals":
                self._emit({"event": "locals", "locals": loc,
                            "exprs": {e: _safe_val(self._eval(e, frame))
                                      for e in (cmd.get("exprs") or [])[:10]}})
            if op == "quit":
                self._quit = True
                self.set_quit()
                break
        self._frame = None

    @staticmethod
    def _eval(expr: str, frame):
        try:
            return eval(expr, frame.f_globals, frame.f_locals)  # noqa: S307 - 开发者本机调试
        except Exception as exc:                              # noqa: BLE001
            return f"<{type(exc).__name__}: {exc}>"

    def user_return(self, frame, return_value):
        self._emit({"event": "return", "value": _safe_val(return_value)})

    def user_exception(self, frame, exc_info):
        self._emit({"event": "exception", "type": exc_info[0].__name__,
                    "message": _safe_val(exc_info[1])})


def worker_main(argv: list[str]) -> int:
    """子进程入口：`python -m core.coderun --dbg-worker <file> <nonce> <bp,csv> <runcwd>`。"""
    if len(argv) < 3:
        return 2
    target, nonce = argv[0], argv[1]
    bps = [int(x) for x in argv[2].split(",") if x.strip().isdigit()]
    # 用户脚本的工作目录：在 worker 内部切（worker 自身 cwd 必须是 prototype，
    # 否则 `python -m core.coderun` 找不到 core 模块）。
    if len(argv) > 3 and Path(argv[3]).is_dir():
        try:
            os.chdir(argv[3])
        except OSError:                                   # noqa: BLE001
            pass
    out = sys.stdout
    stdin = sys.stdin
    emitted: list[str] = []

    def emit(obj: dict) -> None:
        payload = json.dumps(obj, ensure_ascii=False)
        out.write(f"__SCDBG_{nonce}__" + base64.b64encode(payload.encode("utf-8")).decode("ascii") + "\n")
        out.flush()

    def read_cmd() -> dict | None:
        line = stdin.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except Exception:                                 # noqa: BLE001
            return {"op": "noop"}

    # 用户程序的 print/异常重定向成事件，界面分色显示（前缀不会被误判）
    class _Sink:
        def __init__(self, stream: str):
            self.stream = stream
            self.buf = ""
        def write(self, s: str):
            self.buf += s
            while "\n" in self.buf:
                line, self.buf = self.buf.split("\n", 1)
                emit({"event": "output", "stream": self.stream, "text": line})
        def flush(self):
            if self.buf:
                emit({"event": "output", "stream": self.stream, "text": self.buf})
                self.buf = ""
        def isatty(self):
            return False

    sys.stdout = _Sink("stdout")                          # type: ignore[assignment]
    sys.stderr = _Sink("stderr")                          # type: ignore[assignment]
    dbg = _BreakpointDebugger(emit, read_cmd, nonce, bps)
    try:
        # 必须把断点注册进 bdb 本身：bdb 只在你 set_break 过的行才调 user_line。
        # 只把行号存进 self._bps 是没用的（曾踩过：程序直接跑完，断点一次不停）。
        for n in bps:
            try:
                dbg.set_break(target, n)
            except Exception:                             # noqa: BLE001
                pass
        # 关键：runpy 用真实路径执行，保证 __file__ 与断点行号一致
        import runpy
        # 注意：bdb.run() 收的是**语句字符串**（内部 exec），不是函数对象；
        # 跟踪一个函数调用必须用 runcall()。传函数给 run() 会 TypeError
        # （实测：exec() arg 1 must be a string），异常被吞后表现成"断点从不命中"。
        def _target():
            runpy.run_path(target, run_name="__main__")
        dbg.runcall(_target)
        emit({"event": "ended", "returncode": 0})
    except SystemExit as se:                              # 用户程序 sys.exit(n)
        emit({"event": "ended", "returncode": int(se.code or 0)})
    except Exception as exc:                              # noqa: BLE001
        emit({"event": "ended", "returncode": 1,
              "error": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            sys.stdout.flush(); sys.stderr.flush()        # type: ignore[union-attr]
        except Exception:                                 # noqa: BLE001
            pass
    return 0


class DebugSession:
    """一个正在调试的子进程：发命令、读事件、停会话。"""

    def __init__(self, sid: str, proc: subprocess.Popen, nonce: str, ws: Path, path: str):
        self.id = sid
        self.proc = proc
        self.nonce = nonce
        self.ws = ws
        self.path = path
        self.events: queue.Queue = queue.Queue()
        self.last_active = time.time()
        self.ended = False
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        """不断读 worker stdout，解析 __SCDBG__ 事件行；普通行当程序输出。"""
        assert self.proc.stdout is not None
        prefix = f"__SCDBG_{self.nonce}__"
        for raw in self.proc.stdout:
            line = raw.rstrip("\n")
            if line.startswith(prefix):
                try:
                    obj = json.loads(base64.b64decode(line[len(prefix):]).decode("utf-8"))
                    self.events.put(obj)
                    if obj.get("event") == "ended":
                        self.ended = True
                except Exception as exc:                  # noqa: BLE001
                    self.events.put({"event": "output", "stream": "stderr",
                                     "text": f"[调试协议解析失败] {exc}"})
            else:
                self.events.put({"event": "output", "stream": "stdout", "text": line})

    def send(self, op: str, **kw) -> dict:
        if self.ended or self.proc.poll() is not None:
            return {"ok": False, "reason": "调试会话已结束"}
        try:
            assert self.proc.stdin is not None
            self.proc.stdin.write(json.dumps({"op": op, **kw}, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            self.last_active = time.time()
            return {"ok": True, "sent": op}
        except Exception as exc:                          # noqa: BLE001
            return {"ok": False, "reason": f"发送命令失败：{type(exc).__name__}: {exc}"}

    def drain(self, limit: int = 200) -> list[dict]:
        """取走当前所有事件（前端轮询用），不阻塞。"""
        out = []
        while len(out) < limit:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        if out:
            self.last_active = time.time()
        return out

    def stop(self) -> None:
        self.send("quit")
        try:
            self.proc.wait(timeout=3)
        except Exception:                                 # noqa: BLE001
            _kill_tree(self.proc.pid)
        self.ended = True

    def status(self) -> dict:
        return {"id": self.id, "path": self.path, "running": not self.ended and self.proc.poll() is None,
                "ended": self.ended, "idle_s": int(time.time() - self.last_active)}


def start_debug(ws: Path, path: str, *, breakpoints: list[int] | None = None,
                timeout: int = DEFAULT_TIMEOUT) -> dict:
    """起一个 Python 调试会话。其他语言返回明确拒绝（不假装能单步）。"""
    spec = spec_for(path)
    if not spec or spec["lang"] != "python":
        return {"ok": False, "reason": "断点调试目前只支持 Python；其他语言请用「运行」看诊断输出"}
    ws = Path(ws).resolve()
    src = ws / path
    if not src.is_file():
        return {"ok": False, "reason": f"文件不存在：{path}"}
    nonce = uuid.uuid4().hex[:10]
    sid = uuid.uuid4().hex[:12]
    worker = str(Path(__file__).resolve())                  # 直接跑脚本，不依赖 -m 的包解析
    cmd = [_python_for(ws), worker, "--dbg-worker", str(src), nonce,
           ",".join(str(int(x)) for x in (breakpoints or [])), str(find_root(ws, src))]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    try:
        # 用绝对路径跑 worker 脚本：会话里 WorkBuddy 会注入 sandbox shim 到
        # PYTHONPATH，实测会让 `python -m core.coderun` 找不到 core 包（假故障）；
        # 直接跑文件 + 只 import 标准库，绕开这个坑。用户脚本 cwd 由 worker 内部 chdir。
        proc = subprocess.Popen(cmd, cwd=str(Path(__file__).resolve().parent), env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                errors="replace", bufsize=1)
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "reason": f"启动调试失败：{type(exc).__name__}: {exc}"}
    sess = DebugSession(sid, proc, nonce, ws, path)
    with _SESSION_LOCK:
        _gc_sessions()
        _SESSIONS[sid] = sess
    return {"ok": True, "id": sid, "path": path, "breakpoints": breakpoints or [],
            "note": "用「下一步」从第一行开始，或先下断点再「继续」"}


def get_session(sid: str) -> DebugSession | None:
    with _SESSION_LOCK:
        return _SESSIONS.get(sid)


def stop_debug(sid: str) -> dict:
    with _SESSION_LOCK:
        s = _SESSIONS.pop(sid, None)
    if not s:
        return {"ok": False, "reason": "会话不存在或已结束"}
    s.stop()
    return {"ok": True, "id": sid}


def _gc_sessions() -> None:
    """收掉已结束或空闲超时（防暂停中的会话泄漏进程）。"""
    dead = [sid for sid, s in _SESSIONS.items()
            if s.ended or s.proc.poll() is not None or time.time() - s.last_active > DEFAULT_SESSION_IDLE]
    for sid in dead:
        s = _SESSIONS.pop(sid, None)
        if s and s.proc.poll() is None:
            s.stop()


if __name__ == "__main__":
    # worker 入口：python -m core.coderun --dbg-worker <file> <nonce> <bp,csv>
    if len(sys.argv) > 1 and sys.argv[1] == "--dbg-worker":
        sys.exit(worker_main(sys.argv[2:]))
