"""片内自验闭环：给作者一个**受限 shell**，让它在自己这一轮里看到真实输出。

为什么加这一层（2026-11-07，用户问「为什么你能生成那个不能」后的直接结论）：
    同一模型，差别不在能力，在**信息**。会话里的我能写一行就跑、看真 stdout、再改；
    而流水线里的作者是盲写的：探针在切片之外跑，它事后只拿到一句
    `stdout 里没有 'X'；退出码 0（期望 1）`，然后整片重来 —— 一轮就是一次全赔。
    所以这里把"跑"这件事接回作者手里，但用硬边界焊死：

    · 命令白名单：只许 `python` / `py` / `pytest`（跑它自己的验收命令与诊断脚本），
      不许管道、重定向、`&&`、删除、网络；cwd 锁在本仓库内；
    · 每条命令有超时与输出上限（尾部截断，全文不落盘也不进提示词）；
    · 步数上限（默认 6 步）：每步最多 2 条命令 + 一次改写，超了就停、绝不自己加餐；
    · **只许改本片涉及的模块**（其余路径一律拒写），且必须在仓库内；
    · 每步结束引擎**自己**跑一遍失败的探针，用真实证据推动下一步 —— 不许它自评"应该好了"。

协议（纯文本，故意简单，弱模型也不会写错）：
    @RUN python jlq.py --file d.jsonl --count
    === FILE: cli_core.py
    ...整文件内容...
    === END
    @DONE

反过来的纪律：作者说 @DONE 之后，**判定权仍归探针**。它说自己做完了不算数。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from . import pyenv as _pyenv

MAX_STEPS = 6            # 一次片内自验最多几步（每步 = 一次远端 + 至多 2 条命令）
CMDS_PER_STEP = 2
CMD_TIMEOUT = 60         # 单条命令超时（与探针同口径）
OUT_TAIL = 1200          # 单条命令的 stdout 尾部上限（进提示词的字数）
MAX_MODULES = 4          # 一次最多让作者改几个模块
MAX_MODULE_CHARS = 24000  # 单个模块贴进提示词的上限（超了截断并声明）
ALLOWED_BIN = ("python", "python3", "python.exe", "py", "pytest")
# 出现这些字符一律拒绝执行：这层不是 shell，是"跑验收命令"的通道
FORBIDDEN_CHARS = ("|", "&", ";", ">", "<", "`", "$(", "&&", "||", "\n", "\r")
FORBIDDEN_WORDS = ("rm ", "del ", "rmdir", "format ", "curl ", "wget ", "pip install",
                   "git push", "shutdown", "taskkill", "powershell", "cmd /c", "start ")

PROMPT = """你是这个仓库的实现者。**你能真的跑命令**，不要再猜输出。

【本片必须通过的能力（判据是命令 + 该看到的输出）】
@CAPS@

【当前代码（你要在此基础上修，不要另起一套）】
@CODE@

@EVIDENCE@
【你只有一个工具：跑命令】
在回复里单独一行写：
@RUN python <命令>
引擎会执行它，并在下一条消息里把**真实的 stdout / stderr / 退出码**贴给你（尾部截断）。
每条消息最多 @CMDS@ 条命令；除了这条命令通道，你没有别的观察手段。
**先跑，再改**：不要凭想象改代码。

【改文件】
要改某个文件，就交回**整份文件**（不要给 diff、不要省略号），格式严格是：
=== FILE: 相对路径.py
（整份文件的全部内容）
=== END
只许改这些路径：@MODS@（其它路径的写入会被拒绝并计一次失败）。

【收工】
你判断本片全部通过之后，输出单独一行：
@DONE
注意：**判定权在引擎的探针**。你说做完了但探针还是红，会被打回继续改；
第 @MAX@ 步还没过，就停手并如实报告卡在哪。

【铁律】
1. 判据是命令的输出，不是你的意图：命令没跑出该看到的东西，就不算过。
2. 不要改验收命令、不要改参数名、不要为了过判据把输出写死（探针会换数据再来一次）。
3. 改一处就修一处：不要顺手重构不相关的模块。
"""

RUN_RE = re.compile(r"^\s*@RUN\s+(.+?)\s*$", re.M)
FILE_RE = re.compile(r"^=== FILE:\s*(.+?)\s*$\n(.*?)^=== END\s*$", re.M | re.S)


# ---------------------------------------------------------------- 命令通道

def check_cmd(cmd: str, ws) -> str:
    """返回空串=允许；否则返回拒绝理由（理由会回给作者，让它改写法）。"""
    c = (cmd or "").strip()
    if not c:
        return "命令是空的"
    low = c.lower()
    for ch in FORBIDDEN_CHARS:
        if ch in c:
            return f"这条通道只跑单条命令（不许 {ch!r}：管道/重定向/串联都不支持）"
    for w in FORBIDDEN_WORDS:
        if w in low:
            return f"不许 {w.strip()}：这里只用来跑你自己的程序与测试"
    head = low.split()[0]
    if head not in ALLOWED_BIN:
        return (f"只允许 {'/'.join(ALLOWED_BIN)} 开头的命令（你写的是 {head!r}）；"
                f"跑自己的程序请写 `python {Path(str(head)).name} ...`")
    return ""


def _env(ws) -> dict:
    """跑命令用的环境：**绝不写 pycache**。

    实测（2026-11-07，本自检抓到的真 bug）：作者自己跑 `python main.py` 会生成
    `__pycache__/cli_core.cpython-XXX.pyc`；紧接着引擎改写同尺寸的模块（只改一个字符），
    在同一秒内完成 —— pyc 头里存的是**秒级** mtime + 文件大小，两者都没变，
    于是 CPython 认为缓存有效，探针继续跑**旧字节码**：
    正确的实现被判红、错误的实现被判绿。`-B` 只阻止"写"pyc，不阻止"读"。
    所以这里连写都禁掉，并且写入文件时清掉同名缓存（见 apply_files）。
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    p = str(Path(ws).resolve())
    old = env.get("PYTHONPATH") or ""
    env["PYTHONPATH"] = p + (os.pathsep + old if old else "")
    return env


_RUN_SEQ = 0


def run_cmd(ws, cmd: str) -> dict:
    """在仓库内跑一条命令，返回真实证据。绝不抛异常。

    `python -c "<代码>"` 单独处理：命令白名单为了防注入会拒收带引号/分号的写法，
    而**片内自验恰恰全靠这种写法**（作者要看 "import app.tickets; print(...)"）。
    实测 2026-11-07：自验命令全被 rejected，作者拿不到任何证据，只能瞎改 6 轮。
    这里把代码落到临时脚本再执行 —— 没有 shell 参与，语义一致，注入面反而更小。
    """
    ws = Path(ws).resolve()
    m = re.match(r'^\s*(?:python|python3|python\.exe|py)\s+-c\s+(["\'])(?P<code>[\s\S]*)\1\s*$',
                 (cmd or "").strip())
    if m:
        global _RUN_SEQ
        _RUN_SEQ += 1
        d = ws / ".sixcoder" / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        script = d / f"run_{os.getpid()}_{_RUN_SEQ}.py"
        try:
            # 前置一行把工作区塞进 sys.path：内置 Python 的 `._pth` 下，临时脚本所在目录
            # 不会自动进 path，`import app.xxx` 会 ModuleNotFound（2026-11-23）。
            script.write_text(f"import sys; sys.path.insert(0, {str(ws)!r})\n"
                              + m.group("code"), encoding="utf-8")
        except OSError as exc:
            return {"cmd": cmd, "ok": False, "exit": None, "out": "",
                    "err": f"引擎写不了临时脚本：{exc}"}
        return _exec(ws, [sys.executable, "-B", str(script)], cmd)
    why = check_cmd(cmd, ws)
    if why:
        return {"cmd": cmd, "ok": False, "rejected": True, "exit": None,
                "out": "", "err": why}
    argv = [a.strip('"\'') for a in re.split(r'\s+(?=(?:[^"]*"[^"]*")*[^"]*$)', cmd.strip()) if a]
    head = argv[0].lower()
    # 用**当前解释器**跑 python/pytest，避免"作者跑的是别的环境"这种最隐蔽的假绿
    if head in ("python", "python3", "python.exe", "py"):
        _rest = argv[1:]
        # 脚本形态走 runpy：`._pth` 下 `python main.py` 的**脚本目录不进 sys.path**，
        # main.py 里 `from cli_core import ...` 会直接 ModuleNotFound（2026-11-23）。
        # runpy 显式注入脚本目录，语义与正常解释器下 `python main.py` 等价。
        if _rest and _rest[0].endswith(".py"):
            # 统一走 pyenv（._pth 下脚本目录不进 sys.path），并把工作区根也注入
            argv = _pyenv.script_argv(ws / _rest[0], _rest[1:], root_dir=ws)
        else:
            argv = [sys.executable, "-B", *_rest]
    elif head == "pytest":
        argv = [sys.executable, "-B", "-m", "pytest", *argv[1:]]
    return _exec(ws, argv, cmd)


def _exec(ws: Path, argv: list[str], cmd: str) -> dict:
    """真正跑子进程（不抛异常；超时/异常都变成可观测结果）。"""
    t0 = time.time()
    try:
        proc = subprocess.run(argv, cwd=str(ws), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=CMD_TIMEOUT,
                              env=_env(ws))
        out, err, code = proc.stdout or "", proc.stderr or "", proc.returncode
    except subprocess.TimeoutExpired:
        return {"cmd": cmd, "ok": False, "exit": None, "out": "",
                "err": f"超时 {CMD_TIMEOUT}s（可能有死循环或等待输入）"}
    except Exception as exc:                             # noqa: BLE001
        return {"cmd": cmd, "ok": False, "exit": None, "out": "",
                "err": f"引擎跑不动这条命令：{type(exc).__name__}: {exc}"}
    return {"cmd": cmd, "ok": True, "exit": code, "out": out, "err": err,
            "took_s": round(time.time() - t0, 2)}


def _tail(text: str, n: int = OUT_TAIL) -> str:
    t = text or ""
    if len(t) <= n:
        return t
    return "…（前面省略）\n" + t[-n:]


def format_evidence(ev: list[dict]) -> str:
    """把一批执行结果渲染成给作者看的证据块（真实、逐字、带退出码）。"""
    lines = ["【刚刚真实跑出来的结果（这是唯一的事实来源）】"]
    for e in ev or []:
        if e.get("rejected"):
            lines.append(f"$ {e['cmd']}\n  ⊘ 被拒绝：{e['err']}")
            continue
        code = e.get("exit")
        lines.append(f"$ {e['cmd']}\n  退出码：{code}")
        if (e.get("out") or "").strip():
            lines.append("  stdout（尾部）：")
            for ln in _tail(e["out"]).splitlines():
                lines.append("    " + ln)
        else:
            lines.append("  stdout：（空）")
        if (e.get("err") or "").strip():
            lines.append("  stderr（尾部）：")
            for ln in _tail(e["err"], 600).splitlines():
                lines.append("    " + ln)
    return "\n".join(lines)


# ---------------------------------------------------------------- 文件通道

def parse_files(text: str, allowed: list[str]) -> tuple[dict[str, str], list[str]]:
    """取出作者交回的整份文件。返回 (要写的文件, 被拒的路径)。"""
    allowed_set = {str(a).replace("\\", "/").lstrip("./") for a in allowed}
    writes: dict[str, str] = {}
    rejected: list[str] = []
    for m in FILE_RE.finditer(text or ""):
        rel = m.group(1).strip().replace("\\", "/").lstrip("./")
        body = m.group(2)
        if rel not in allowed_set:
            rejected.append(rel)
            continue
        if "..." in body and body.count("...") >= 2 and len(body) < 400:
            rejected.append(rel + "（内容像省略号，退回整份文件）")
            continue
        if not body.strip():
            rejected.append(rel + "（内容为空）")
            continue
        writes[rel] = body if body.endswith("\n") else body + "\n"
    return writes, rejected


def _purge_pycache(p: Path) -> None:
    """删掉这个模块的字节码缓存。

    不清就会出事：pyc 头里是**秒级** mtime + 源码大小，同尺寸改写又在同一秒内完成时，
    CPython 会把旧字节码当成有效缓存 —— 探针跑的是改之前的代码（判定直接失真）。
    """
    import shutil
    cache = p.parent / "__pycache__"
    if not cache.is_dir():
        return
    for f in cache.glob(f"{p.stem}.*.pyc"):
        try:
            f.unlink()
        except OSError:
            pass
    try:                                                 # 目录空了就顺手删掉
        if not any(cache.iterdir()):
            shutil.rmtree(cache, ignore_errors=True)
    except OSError:
        pass


def apply_files(ws, writes: dict[str, str]) -> list[str]:
    """落盘（只写仓库内的相对路径）。返回写成功的列表。"""
    ws = Path(ws).resolve()
    done: list[str] = []
    for rel, body in (writes or {}).items():
        p = (ws / rel).resolve()
        try:
            p.relative_to(ws)
        except ValueError:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
        _purge_pycache(p)
        done.append(rel)
    return done


# ---------------------------------------------------------------- 判定

def _cap_lines(caps) -> str:
    out: list[str] = []
    for c in caps:
        out.append(f"- {c.id}：{c.desc}")
        cmd = _probe_cmd(c.probe)
        if cmd:
            out.append(f"  验收命令：{cmd}")
        if c.why:
            out.append(f"  上次判红的原因：{str(c.why)[:400]}")
        files = (c.probe or {}).get("files") if isinstance(c.probe, dict) else None
        if files:
            out.append("  验收素材（探针自己会准备好，你不用造）：" + "、".join(map(str, files)))
    return "\n".join(out)


def _probe_cmd(probe: dict) -> str:
    from . import slicer
    try:
        return slicer.cmd_line(probe) or ""
    except Exception:                                    # noqa: BLE001
        return ""


def verify_caps(ws, caps) -> dict:
    """引擎自己跑一遍探针，拿**真实**证据。返回 {cap_id: probe_result}。

    out_cap 放宽到 OUT_TAIL：判定永远看全文，但作者要修得动，就得看到足够长的输出。
    """
    from . import probe as _probe
    res: dict[str, dict] = {}
    for c in caps:
        try:
            res[c.id] = _probe.run(ws, c.probe if isinstance(c.probe, dict) else {},
                                   out_cap=OUT_TAIL)
        except Exception as exc:                         # noqa: BLE001
            res[c.id] = {"ok": False, "engine_error": True,
                         "detail": f"探针执行异常：{type(exc).__name__}: {exc}"}
    return res


def evidence_from_verify(ws, caps, res: dict) -> list[dict]:
    """把探针结果转成作者看得懂的证据：**探针自己的真实输出**，不要再跑一遍。

    为什么不许重跑：探针是在临时目录里准备好验收素材再跑的（`d.jsonl` 之类）。
    在仓库根重跑同一条命令，作者会看到"文件不存在"—— 用假证据修代码，
    比不给证据更糟（它会把好好的读取逻辑改坏）。
    """
    out: list[dict] = []
    for c in caps:
        r = res.get(c.id) or {}
        if r.get("ok"):
            continue
        ev = r.get("evidence") or {}
        cmd = _probe_cmd(c.probe) or f"（{c.id}：不是命令行能力，探针直接调用）"
        out.append({"cap": c.id, "cmd": cmd,
                    "exit": ev.get("exit"),
                    "out": ev.get("stdout") or "",
                    "err": (ev.get("stderr") or r.get("detail") or "")})
    return out


# ---------------------------------------------------------------- 主循环

TURN_PROMPT = """【刚跑出来的结果（唯一的事实来源）】
@EVIDENCE@

【仍然没过的能力】
@STILL@

继续：要看别的就再 `@RUN python ...`；要改就交回**整份**文件（`=== FILE: 路径` … `=== END`）；
全部通过就单独一行 `@DONE`。不要重复解释，直接给命令或文件。
"""


def _call(llm, prompt: str, *, messages: list[dict] | None = None, **kw) -> dict:
    """调远端。支持多轮就传 messages；不支持（老签名/假远端）就退化成单轮。

    退化不是"降级糊弄"：多轮只是省 token + 保留上下文，单轮塞全量提示词仍然正确。
    所以这里允许 TypeError 退路，但**绝不**在远端报错时假装成功。
    """
    if messages:
        try:
            return llm(prompt, messages=messages, **kw)
        except TypeError:
            pass
    try:
        return llm(prompt, **kw)
    except TypeError:                                    # 连 kwargs 都不收的极简实现
        return llm(prompt)


def repair(ws, caps, *, modules: list[str] | None = None, llm=None, emit=None,
           max_steps: int = MAX_STEPS, multi_turn: bool = True) -> dict:
    """片内自验：跑→看→改，最多 max_steps 步。判定权始终在探针手里。

    返回 {ok, steps, fixed, still, changed, cost_cny, calls, evidence, stop}
    """
    ws = Path(ws).resolve()
    if llm is None:
        from . import blueprint as _bp
        llm = _bp._default_llm
    caps = [c for c in (caps or []) if c is not None]
    if not caps:
        return {"ok": True, "steps": 0, "fixed": [], "still": [], "changed": [],
                "cost_cny": 0.0, "calls": 0, "stop": "没有待修的能力"}
    # 只许改本片涉及、且真实存在的模块（改写不存在的文件 = 无中生有，容易造出重复实现）
    cand: list[str] = []
    for c in caps:
        for m in (c.modules or []):
            m = str(m).replace("\\", "/").lstrip("./")
            if m and m not in cand and (ws / m).is_file():
                cand.append(m)
    for m in (modules or []):
        m = str(m).replace("\\", "/").lstrip("./")
        if m and m not in cand and (ws / m).is_file():
            cand.append(m)
    cand = cand[:MAX_MODULES]
    if not cand:
        return {"ok": False, "steps": 0, "fixed": [], "still": [c.id for c in caps],
                "changed": [], "cost_cny": 0.0, "calls": 0,
                "stop": "这一片没有可改的模块（能力声明的模块都还没落盘）"}

    def _read(mods: list[str]) -> str:
        parts = []
        for m in mods:
            try:
                body = (ws / m).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(body) > MAX_MODULE_CHARS:
                body = body[:MAX_MODULE_CHARS] + "\n…（过长已截断）"
            parts.append(f"--- {m} ---\n{body}")
        return "\n".join(parts)

    calls = 0
    steps_used = 0
    changed: list[str] = []
    stop = "步数到顶"
    evidence: list[dict] = []
    last_ev_text = ""
    for step in range(1, max(1, max_steps) + 1):
        res = verify_caps(ws, caps)
        still = [c for c in caps if not res.get(c.id, {}).get("ok")]
        if not still:
            stop = "全部通过"
            break
        evidence = evidence_from_verify(ws, still, res)
        if step == 1 and not evidence:
            stop = "拿不到可诊断的证据（探针没给出命令）"
            break
        steps_used = step
        if step == 1:
            # 第一轮：契约 + 现有代码 + 首批证据，一次说清
            prompt = (PROMPT.replace("@CAPS@", _cap_lines(still))
                            .replace("@CODE@", _read(cand))
                            .replace("@EVIDENCE@", format_evidence(evidence) + "\n")
                            .replace("@CMDS@", str(CMDS_PER_STEP))
                            .replace("@MODS@", "、".join(cand))
                            .replace("@MAX@", str(max_steps)))
            convo = [{"role": "user", "content": prompt}]
        else:
            # 后续轮：**只给新事实**，不重发契约与代码 —— 它自己的上一版就在对话历史里
            prompt = (TURN_PROMPT.replace("@EVIDENCE@", format_evidence(evidence) or "（无）")
                                 .replace("@STILL@", _cap_lines(still)))
            if multi_turn:
                convo += [{"role": "user", "content": prompt}]
            else:
                convo = [{"role": "user", "content":
                          PROMPT.replace("@CAPS@", _cap_lines(still))
                                .replace("@CODE@", _read(cand))
                                .replace("@EVIDENCE@", format_evidence(evidence) + "\n")
                                .replace("@CMDS@", str(CMDS_PER_STEP))
                                .replace("@MODS@", "、".join(cand))
                                .replace("@MAX@", str(max_steps))}]
        r = _call(llm, prompt, messages=convo if multi_turn else None,
                  task_id="selfcheck", node_id="selfcheck", max_out=16000)
        calls += 1
        if not r.get("ok"):
            stop = f"远端调用失败：{r.get('error')}"
            break
        text = str(r.get("answer") or "")
        if multi_turn:
            convo += [{"role": "assistant", "content": text}]
        if emit:
            emit("selfcheck_step", {"step": step, "still": [c.id for c in still][:6],
                                    "cmds": len(RUN_RE.findall(text))})
        # 命令通道：作者要看什么，就给它跑什么（并把这批输出当下一步的证据）
        ran: list[dict] = []
        for cmd in RUN_RE.findall(text)[:CMDS_PER_STEP]:
            e = run_cmd(ws, cmd)
            ran.append(e)
            if emit:
                emit("selfcheck_run", {"cmd": cmd[:90], "exit": e.get("exit"),
                                       "rejected": bool(e.get("rejected"))})
        writes, rejected = parse_files(text, cand)
        wrote = apply_files(ws, writes)
        if wrote:
            changed.extend(wrote)
        if rejected and emit:
            emit("selfcheck_reject", {"paths": rejected[:4]})
        if ran:
            last_ev_text = format_evidence(ran)
        if re.search(r"^\s*@DONE\s*$", text, re.M):
            stop = "作者自报完成（以探针为准）"
        if ran or wrote:
            continue
        # 既没跑也没改：说明它在空转，别把预算喂给复述
        if not ran and not wrote:
            stop = "这一步既没跑命令也没交回文件（空转）"
            break

    res = verify_caps(ws, caps)
    fixed = [cid for cid, r in res.items() if r.get("ok")]
    still = [c.id for c in caps if not res.get(c.id, {}).get("ok")]
    if not still:
        stop = "全部通过"
    return {"ok": not still, "steps": steps_used,
            "fixed": fixed, "still": still, "changed": sorted(set(changed)),
            "calls": calls, "stop": stop,
            "evidence": [{k: e.get(k) for k in ("cap", "cmd", "exit")} for e in evidence][:6],
            "last_evidence": last_ev_text[:1200]}
