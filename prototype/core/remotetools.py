"""远端感知/行动工具循环（§13.4 第 3 步）+ 受保护区只读（第 4 步）。

为什么需要（开发文档 §11.2 / §11.5）：远端现在只能回 JSON 补丁，看不见真实代码、只能猜
（L2）；而它最省力的「变绿」方式是删测试。工具循环给它**只读感知权**——读文件、grep、
跑指定测试、跑静态分析（结果原样回传）——同时用受保护区（第 4 步）把测试/契约/治理文件
设为对远端工具**只读**，杜绝「删测试变绿」。

设计边界（与 sandbox 同源，且更严）：
- 循环本身是**纯感知 + 记忆写入**：除 write_memory（只写 .sixcoder/knowledge.jsonl）外，
  不提供任意源码写工具；即便将来加 write_file，也必须过 `_tool_allow_write` 守卫。
- 所有读操作经 `tools` + `Sandbox`（readonly 模式），命令白名单 + 敏感路径拒绝。
- write_memory 是唯一写出口，目标固定为知识库（非受保护区），仍过 guard_write 防御纵深。
- 结果「原样回传」：不替远端解读，只把工具输出塞回对话，由远端自己判断。
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from . import tools as _tools
from . import scale as _scale
from . import memory as _memory
from . import capability as _capability

# 支持两种写法：自闭合 <tool name="x" /> 与显式闭合 <tool name="x">body</tool>
_TOOL_RE = re.compile(r"<tool\b([^>]*?)\s*(?:/>|>(.*?)</tool>)", re.DOTALL | re.IGNORECASE)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')

# 远端工具循环的写出口：测试/契约文件永远拒绝（受保护区只读，与级别无关）。
_TEST_RE = re.compile(r"(^|/)(test_.*\.py|.*_test\.py|contract.*\.py)$", re.IGNORECASE)

GUIDE = """\
# 你可用的只读工具（按需调用，不要臆测代码内容）

工具以**原生 function calling** 的形式挂在你面前（tools 数组里每个都有 JSON Schema），
**直接按 schema 调函数即可**，不要输出 XML、不要自己写 tool_calls 字段。

常用工具（完整列表见 tools 数组的 description）：
- read_file(path, start, end) —— 看文件真实内容
- outline(path) —— 看文件有哪些函数、签名是什么
- list_files(sub, limit) —— 列目录
- grep(pattern, path, limit) —— 搜代码
- imports(path) / check_import(path) —— 看依赖 / 能不能 import
- contract_for(module) —— 这个模块的验收判据（契约）要求什么
- route_status(method, path) —— 端点为什么 404（已接线 / 缺函数 / 接线被拒）
- git_history(path, limit) —— 看文件原来的样子
- run_python(code) —— 跑一段代码看真实结果
- run_tests(target) / static_analysis(path) —— 跑测试 / 静态分析
- requirement_for(topic) —— 查全局约束（验收指标、规模目标）
- write_memory(type, text) —— 记一条工程事实（唯一能写的东西）

规则：
- 这些工具都是**只读**的（write_memory 只写 .sixcoder/knowledge.jsonl）。
  结果是原样回传的，请结合它们做判断；不要凭空编造文件内容。
- **写之前先看真实代码**：这个文件现在有哪些函数、签名是什么（outline）、
  它引用了谁（imports）、能不能 import（check_import）、判据要求是什么（contract_for）。
  端点报 404 时用 route_status 查**为什么没接上**（已接线 / 缺函数 / 接线被拒）——
  接线被拒说明是引擎的问题，你不用改代码，别反复重写。
  要看某文件**原来的样子**用 git_history（改坏文件时先看，别凭记忆重写）；
  要验证一个函数/表达式用 run_python（跑一段代码看真实结果）；
  要查**全局约束**（验收指标、架构边界、规模目标）用 requirement_for
  （切片契约里只有这一片的考卷，全局要求只在需求原文里）。
- 查「端点为什么 404 / 判据为什么红」时，用 grep 搜 ".sixcoder" 目录，
  可搜到事实文件：web_routes.json（引擎接线表）、blueprint.json（探针链）、
  slices/*.txt（本片契约）；也可用 read_file 直接读它们（一律只读）。
- write_memory 是唯一能写的东西，只能写 .sixcoder/knowledge.jsonl，且只记事实/约束/坑。
- 测试/契约/治理文件对工具循环只读，写入会被拒绝。

## 最重要的一条：**最终必须给出答案正文**

调用工具只是**中间步骤**。查够了之后，你必须**停止调工具**，
用一条**普通消息**给出最终答案（最终答案里不要再有 tool_calls）。

如果你这一轮调了工具，工具结果会回给你；收到结果后请直接给最终答案。
最终答案的格式按任务提示里说的（A/B/C 三选一）。
**只调工具不给正文 = 这一轮白跑**（实测 2026-11-09：连着 3 轮都在调工具、
正文为空 → 引擎拿不到任何代码 → 整轮废弃重写）。
"""

# ── XML 版提示词（模型不支持原生 function calling 时用）────────────────────
# 以前只有 native 一版，而 native 版明写"不要输出 XML" → XML 兜底**永远不可能触发**，
# 降级成了"模型没有工具还假装有"。所以每条通路都要有**对应的提示词**。
GUIDE_XML = """\
# 你可用的只读工具（这个端点**不支持原生函数调用**，请用 XML 协议表达）

调用格式（自闭合标签，一次只调一个）：
    <tool name="工具名" 参数="值" 参数="值" />

例子：
    <tool name="read_file" path="app/x.py" />
    <tool name="outline" path="app/x.py" />
    <tool name="grep" pattern="create_topic" path="app" />
    <tool name="list_files" sub="app" />
    <tool name="imports" path="app/x.py" />
    <tool name="check_import" path="app/x.py" />
    <tool name="contract_for" module="app/x.py" />
    <tool name="route_status" method="GET" path="/api/v1/batches" />
    <tool name="git_history" path="app/x.py" />
    <tool name="run_python" code="print(1+1)" />
    <tool name="run_tests" target="app/x.py" />
    <tool name="static_analysis" path="app/x.py" />
    <tool name="requirement_for" topic="验收指标" />
    <tool name="write_memory" type="fact" text="记住一条事实" />

参数一律用双引号，值里不要出现双引号。调用后你会收到
`<tool_result name="..." ok="true|false">…</tool_result>`。

工具都是**只读**的（write_memory 只写 .sixcoder/knowledge.jsonl）。
**结果原样回传，请结合它判断；不要凭空编造文件内容。**
**写之前先看真实代码**（outline 看有什么函数、imports 看引用了谁、
check_import 看能不能 import、contract_for 看判据要求什么）；
端点 404 时用 route_status 查**为什么没接上**，别盲目重写。

## 最重要的一条：**最终必须给出答案正文**
查够了就**停止调工具**，用一条普通消息给出最终答案（不要再有 <tool> 标签）。
**只调工具不给正文 = 这一轮白跑。**
"""

# ── 无工具版提示词（模型压根不支持工具时用）──────────────────────────────
GUIDE_NONE = """\
# 你**没有**任何工具可用

这个端点既不支持原生函数调用，也不支持文本协议工具调用。
**你看不到任何文件** —— 提示词里没贴出来的代码对你不存在。

因此：
- **绝对不要假装看过文件、编造文件内容、编造函数签名或字段名。**
  编造出来的名字会让上层以为"它看过了"，比明说"看不到"危险得多。
- 需要某个文件的内容时，**明确写出来**："我需要看 app/x.py 的当前内容才能确定"。
  上层会把内容贴给你，或把这轮交给能用工具的模型。
- 你仍然可以**只依据提示词里已有的契约/要求**给出实现草稿，但要标明哪些地方
  是按契约推的、哪些地方需要看真实文件才能定。
- 如果任务提示里给了具体的代码片段/契约，就按它写，别去猜文件里还有什么。
"""


def guide_for(mode: str) -> str:
    """按工具通路取对应的提示词（**每条通路一份**，别用一份糊弄所有模型）。"""
    m = str(mode or "").strip().lower()
    if m == "xml":
        return GUIDE_XML
    if m == "none":
        return GUIDE_NONE
    return GUIDE


def parse_tool_calls(text: str) -> list[tuple[str, dict, str]]:
    """解析回复里的 <tool ...> 调用，返回 [(name, attrs, body)]。解析失败安全返回空。"""
    out: list[tuple[str, dict, str]] = []
    try:
        for m in _TOOL_RE.finditer(text or ""):
            attrs = {k: v for k, v in _ATTR_RE.findall(m.group(1))}
            name = (attrs.get("name") or "").strip().lower()
            if name:
                out.append((name, attrs, (m.group(2) or "").strip()))
    except Exception:                            # noqa: BLE001
        pass
    return out


def _grep(ws: Path, pattern: str, path: str = ".", limit: int = 200) -> str:
    """在仓库里按正则搜代码（只读，跳过非源码与体积过大文件）。"""
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return f"grep 正则非法：{exc}"
    ws_r = Path(ws).resolve()
    root = (ws_r / path) if path and not Path(path).is_absolute() else ws_r
    hits: list[str] = []
    # `.sixcoder` 里放着诊断所需的**事实**（web_routes.json / blueprint.json /
    # slices/*.txt 契约）。默认仍跳过（保持既有行为），但调用方显式把 path 指到
    # `.sixcoder` 时必须可见，否则远端工具连"这个端点有没有接线"都查不到，
    # 只能在 404 上反复猜（实测 2026-11-09）。
    skip = {".git", "node_modules", "__pycache__", ".pytest_cache",
            ".venv", ".sixcoder", ".mypy_cache", "dist", "build"}
    if ".sixcoder" in Path(str(path or "")).parts:
        skip.discard(".sixcoder")
    for p in sorted(root.rglob("*")):
        if len(hits) >= limit:
            break
        if not p.is_file():
            continue
        if any(part in skip for part in p.parts):
            continue
        if p.suffix not in (".py", ".js", ".ts", ".md", ".txt", ".yml", ".yaml", ".json", ".toml"):
            continue
        try:
            if p.stat().st_size > 2 * 1024 * 1024:
                continue
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        rel = p.relative_to(ws_r).as_posix()
        for i, ln in enumerate(lines, 1):
            if rx.search(ln):
                hits.append(f"{rel}:{i}: {ln.strip()[:200]}")
                if len(hits) >= limit:
                    break
    return "\n".join(hits) if hits else f"在 {path} 下未匹配到「{pattern}」"


def _static_analysis(ws: Path, rel: str) -> str:
    """轻量静态分析：py_compile 检查语法 + 行数/复杂度粗报（不依赖外部工具）。"""
    p = _tools.safe_path(ws, rel)
    if not p.exists():
        return f"文件不存在：{rel}"
    try:
        code = p.read_text(encoding="utf-8-sig", errors="replace")
        compile(code, str(p), "exec")         # 语法/编译检查（最便宜的真判据）
    except SyntaxError as exc:
        return f"语法错误 {p.name}:{exc.lineno}: {exc.msg}"
    except Exception as exc:                  # noqa: BLE001
        return f"静态分析异常：{type(exc).__name__}: {exc}"
    nlines = len(code.splitlines())
    nfuncs = code.count("def ")
    return f"{p.name} 语法 OK（{nlines} 行，约 {nfuncs} 个 def）；未装更复杂静态分析时为仅此一项"


def _outline(ws: Path, rel: str) -> str:
    """AST 结构：这个文件里有哪些函数/类/常量、各自多少行（远端要精确定位时用）。

    为什么需要：远端只有"按行读"这一种定位手段，而它不知道这个文件里**已经有**
    哪些实现 → 容易重写已有函数、或把字段名猜错。给它一张结构表，
    它就能先看再改（实测：`_account_view() 用了字段 'r...'` 这类错就是没先看结构）。
    """
    p = _tools.safe_path(ws, rel)
    if not p.exists():
        return f"文件不存在：{rel}"
    try:
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src, str(p))
    except SyntaxError as exc:
        return f"语法错误 line {exc.lineno}: {exc.msg}"
    lines = src.splitlines()
    out: list[str] = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in n.args.args)
            ret = ""
            if n.returns is not None:
                try:
                    ret = " -> " + ast.unparse(n.returns)
                except Exception:                            # noqa: BLE001
                    ret = ""
            end = getattr(n, "end_lineno", n.lineno) or n.lineno
            out.append(f"{n.lineno:>4}-{end:<4} def {n.name}({args}){ret}")
        elif isinstance(n, ast.ClassDef):
            end = getattr(n, "end_lineno", n.lineno) or n.lineno
            out.append(f"{n.lineno:>4}-{end:<4} class {n.name}")
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            names = []
            if isinstance(n, ast.Assign):
                names = [t.id for t in n.targets if isinstance(t, ast.Name)]
            else:
                names = [n.target.id] if isinstance(n.target, ast.Name) else []
            for nm in names:
                out.append(f"{n.lineno:>4}      常量 {nm}")
    return (f"{rel}（共 {len(lines)} 行）\n" + "\n".join(out)) if out else f"{rel}：没有顶层定义"


def _imports(ws: Path, rel: str) -> str:
    """依赖关系：我 import 了谁 / 谁调用我 / 疑似未定义的名字。

    "疑似未定义"这一段是重点：远端最常见的错就是**用了没 import 的名字**
    （日志里 `_account_view() 用了字段 'r...'` 就是这一类），
    让它自己先查一遍比引擎事后回滚便宜得多。
    """
    p = _tools.safe_path(ws, rel)
    if not p.exists():
        return f"文件不存在：{rel}"
    try:
        src = p.read_text(encoding="utf-8-sig", errors="replace")
        tree = ast.parse(src, str(p))
    except SyntaxError as exc:
        return f"语法错误 line {exc.lineno}: {exc.msg}（先修语法再谈依赖）"
    imported: set[str] = set()
    used: set[str] = set()
    defined: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                imported.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            used.add(n.id)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
    # 内置名要用 builtins 模块本身：在导入模块里 `__builtins__` 是 **dict**，
    # `dir(dict)` 取到的是字典方法 → ValueError/int 全被误报成"未定义"。
    import builtins as _bi
    builtins = set(dir(_bi))
    missing = sorted(x for x in (used - imported - defined - builtins)
                     if not x.startswith("_") and x.isidentifier())
    # 谁调用了我（粗粒度：全仓搜这个模块路径）
    callers: list[str] = []
    mod = str(rel).replace("\\", "/").removesuffix(".py").replace("/", ".")
    try:
        for hit in _grep(ws, re.escape(str(rel).replace("\\", "/")), ".", limit=40).splitlines():
            if hit and not hit.startswith(str(rel).replace("\\", "/") + ":"):
                callers.append(hit[:160])
    except Exception:                                    # noqa: BLE001
        pass
    out = [f"{rel}：",
           "import 了：" + (", ".join(sorted(imported)) or "（无）"),
           "本地定义：" + (", ".join(sorted(defined)) or "（无）"),
           "疑似未定义的名字：" + (", ".join(missing[:20]) or "（无）")]
    if callers:
        out.append("引用了本模块的地方（最多 10 条）：")
        out.extend(callers[:10])
    return "\n".join(out)


def _check_import(ws: Path, rel: str) -> str:
    """import 冒烟：这个模块现在能不能 import？报错是什么？（等价本地的 import_smoke）"""
    mod = str(rel).replace("\\", "/").removesuffix(".py").replace("/", ".")
    if not mod or mod.endswith(".__init__"):
        mod = mod.removesuffix(".__init__")
    try:
        rc, out = _tools.run_cmd(ws, [sys.executable, "-c",
                                      f"import {mod}"], timeout=60)
    except Exception as exc:                            # noqa: BLE001
        return f"import 检查失败：{type(exc).__name__}: {exc}"
    if rc == 0:
        return f"import {mod} 成功"
    tail = (out or "").strip().splitlines()[-6:]
    return f"import {mod} 失败（rc={rc}）：\n" + "\n".join(tail)


def _contract_for(ws: Path, module: str) -> str:
    """本片契约里**这个模块**那一节：签名 + 示例 + 假设（远端的判据从哪来）。"""
    mod = str(module or "").replace("\\", "/")
    sdir = _tools.safe_path(ws, ".sixcoder/slices")
    if not sdir.is_dir():
        return "找不到 .sixcoder/slices/（本片契约不在这里）"
    cands = sorted(sdir.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    for c in cands:
        try:
            text = c.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if mod and mod not in text:
            continue
        # 抽出包含该模块的那一节（以 "N) 路径" 分节）
        secs = re.split(r"(?m)^\s*\d+[.)、]\s*", text)
        for sec in secs:
            if sec.strip().startswith(mod) or mod in sec.split("\n")[0]:
                return f"[{c.name}]\n" + sec.strip()[:4000]
        if mod:
            return (f"[{c.name}] 里没有 {mod} 的独立小节；该契约包含 {mod}，"
                    f"全文如下（截断 4000 字）：\n" + text[:4000])
    return f"没找到含 {mod} 的切片契约"


def _route_status(ws: Path, method: str = "GET", path: str = "") -> str:
    """这个端点**为什么没接上**？分因归类，让远端分清"该我写代码"还是"该引擎修"。

    为什么必须分因（实测 2026-11-09 15:00）：远端写完代码后判据报 404，
    但 404 的文案只说"该路径没有注册的处理函数"——它**分不清**两种完全不同的情况：
      · **缺函数**：远端确实没写那个函数 → 该远端写；
      · **接线被拒**：函数**已经写了**（甚至写对了），但引擎的路由接线判定
        没把它认出来 → **该引擎修**，远端再写一百遍也没用。
    实测真例：远端把 `create_batch` / `create_batch_endpoint` 都写对了，
    引擎因单数化 bug 把 `batches` 判成"没有匹配函数" → 恒 404 → 远端在
    同一片上反复重写正确的代码（gained=[]）。文案不区分 → 远端只能重试。

    本工具给出三种结论：
      · 已接线        → 路由已注册（可让远端放心，不必再碰）
      · 缺函数        → 该模块里**没有**匹配函数 → 需要远端写
      · 接线被拒      → 函数**存在**但引擎没认出来 → **引擎的锅**，远端不用改
    """
    meth = str(method or "GET").upper().strip()
    pth = str(path or "").strip()
    if not pth:
        return "要查哪个端点？给 method 和 path，例如 method=POST path=/api/v1/batches"
    try:
        from . import auto as _auto
    except Exception as exc:                            # noqa: BLE001
        return f"路由检查不可用：{type(exc).__name__}"
    # ① 已接线？
    try:
        reg = _auto._load_routes(ws) or {}
    except Exception:                                    # noqa: BLE001
        reg = {}
    for _mod, _routes in (reg or {}).items():
        for _r in _routes or []:
            if (str(_r.get("method") or "").upper() == meth
                    and str(_r.get("path") or "") == pth):
                return (f"✅ 已接线：{meth} {pth} → {_mod}::{_r.get('func')}"
                        f"（状态码 { _r.get('status') }）——路由已注册，不需要再改。")
    # ② 引擎能不能找到匹配函数？
    try:
        mod, fn = _auto._locate_route(ws, meth, pth)
    except Exception as exc:                            # noqa: BLE001
        mod, fn = "", ""
        return f"路由检查出错：{type(exc).__name__}"
    if mod and fn:
        return (f"⚠️ 接线被拒：{meth} {pth} 的处理函数**已经存在**"
                f"（{mod}::{fn}），但引擎的路由接线没把它认出来 → 这是**引擎的问题**，"
                f"不是你没写代码。不用改这个函数，把现象报给系统即可。")
    # ③ 该模块里到底有没有相近的函数？
    primary = _auto._module_for_path(ws, pth)
    hint = ""
    if primary:
        try:
            from . import auto as _auto2
            src = (Path(ws) / primary).read_text(encoding="utf-8-sig", errors="replace")
            sigs = _auto2._module_signatures(_auto2._author_area(src))
            near = [n for n in sigs
                    if pth.strip("/").split("/")[-1].lower() in n.lower()
                    or pth.strip("/").split("/")[-1].lower().rstrip("s") in n.lower()]
            if near:
                hint = (f"；{primary} 里已有相近的函数 {near[:5]}，"
                        f"但**名字里没有该路径的末段**（引擎按末段匹配，"
                        f"路径末段要在函数名里出现）")
        except Exception:                                # noqa: BLE001
            hint = ""
    return (f"❌ 缺函数：{meth} {pth} 在 {primary or '（对应模块）'} 里**没有**匹配的"
            f"处理函数 → 需要你写{hint}。")


def _git_history(ws: Path, rel: str = "", limit: int = 3) -> str:
    """这个文件（或全仓）最近几个版本长什么样 —— 远端改坏文件时的"原文"。

    为什么必须有（实测 2026-11-09）：引擎自己有 `linkfix.restore_constants_from_history`
    （用 `git log -S` + `git show` 把被重写丢掉的常量找回来），说明**产物仓库有 git 历史、
    而且引擎已经在用** —— 但远端调不到，只能凭记忆重写，于是把上一片验证过的实现改坏
    （实测：作者重写后丢了 `_ALLOWED_ROLES`/`_REQUIRED_FIELDS` → 整条 auth 链 NameError
    → 已完成能力被观测打回）。

    只读：只跑 `git log` / `git show`，不写不提交。
    """
    import subprocess
    rel = str(rel or "").replace("\\", "/").strip()
    if rel and not _tools.safe_path(ws, rel):
        return f"路径不在仓库内：{rel}"
    try:
        if rel:
            args = ["git", "log", "--all", f"-{max(1, int(limit or 3))}",
                    "--format=%H %s", "--", rel]
        else:
            args = ["git", "log", "--all", f"-{max(1, int(limit or 3))}", "--format=%H %s"]
        p = subprocess.run(args, cwd=str(ws), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        if p.returncode != 0:
            return f"git log 失败：{(p.stderr or '').strip()[:200]}"
        commits = [ln.strip() for ln in (p.stdout or "").splitlines() if ln.strip()]
        if not commits:
            return "git 历史里没有这个文件（可能从没提交过）"
        out = ["[git 历史（最近 %d 个版本）]" % len(commits)]
        for row in commits[: max(1, int(limit or 3))]:
            sha, _, subject = row.partition(" ")
            out.append(f"  {sha[:10]} {subject[:80]}")
            if rel:
                q = subprocess.run(["git", "show", f"{sha}:{rel}"], cwd=str(ws),
                                   capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=30)
                if q.returncode == 0:
                    body = (q.stdout or "").splitlines()
                    out.append(f"    该版本共 {len(body)} 行，前 40 行：")
                    for i, ln in enumerate(body[:40], 1):
                        out.append(f"    {i:4d}| {ln}")
                    if len(body) > 40:
                        out.append(f"    …（还有 {len(body) - 40} 行）")
                else:
                    out.append("    （该版本里没有这个文件）")
        return "\n".join(out)
    except Exception as exc:                            # noqa: BLE001
        return f"git 历史不可用：{type(exc).__name__}: {exc}"


def _run_python(ws: Path, code: str, timeout: int = 60) -> str:
    """跑一段 Python 片段看真实结果（不只是跑 pytest）。

    为什么必须有（实测 2026-11-09）：远端只有 `run_tests`（跑测试文件），
    想验"我这个函数对某个输入返回什么"、"这个表达式求出来是几"都没法验 ——
    只能猜，或者把验证写进测试文件（而测试文件由引擎渲染，不该远端写）。
    引擎内部已有 `tools.run_cmd`（列表 argv + 沙箱 + 审计），这里只是把它开给远端。

    只读：只执行代码，不写文件（沙箱已限制危险命令）。
    """
    code = str(code or "")
    if not code.strip():
        return "要跑什么？给 code（Python 片段）"
    if len(code) > 8000:
        return "代码太长（>8000 字符），请精简后再试"
    try:
        import sys as _sys
        rc, out = _tools.run_cmd(ws, [_sys.executable, "-c", code], timeout=timeout)
    except Exception as exc:                            # noqa: BLE001
        return f"执行失败：{type(exc).__name__}: {exc}"
    return f"[rc={rc}]\n{str(out)[-3000:]}"


def _requirement_for(ws: Path, topic: str = "") -> str:
    """需求原文里**关于这个主题**的段落（跨切片的全局约束在切片契约里看不到）。

    为什么必须有（实测 2026-11-09）：切片契约是"这一片"的考卷，
    但需求里有些约束是**全局**的，契约里不会出现：
      · "blocking pair 数为零""一志愿命中率 ≥75%"（验收口径）
      · "目标代码规模 30000 行"
      · "算法包不导入 Django 模型""LLM 只做辅助生成不自动发题"（架构边界）
    远端只看到切片契约，就可能写出违反全局约束的代码（比如在算法包里 import Django）。
    本工具直接给原文段落。
    """
    topic = str(topic or "").strip()
    try:
        req = (Path(ws) / "REQUIREMENTS.md")
        if not req.is_file():
            cands = sorted(Path(ws).glob("*.md"))
            if not cands:
                return "仓库里没有 REQUIREMENTS.md 或任何 .md 需求文件"
            req = cands[0]
        text = req.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:                            # noqa: BLE001
        return f"读需求文件失败：{type(exc).__name__}"
    if not topic:
        head = "\n".join(text.splitlines()[:60])
        return (f"[{req.name} 开头 60 行]\n{head}\n\n"
                f"（全文 {len(text.splitlines())} 行；给 topic 可取相关段落）")
    # 找包含 topic 的段落（按空行分段）
    blocks = [b for b in text.split("\n\n") if topic.lower() in b.lower()]
    if not blocks:
        # 退化：按行匹配
        lines = text.splitlines()
        hits = [i for i, ln in enumerate(lines) if topic.lower() in ln.lower()]
        if not hits:
            return f"需求里没有提到「{topic}」"
        lo = max(0, hits[0] - 3)
        hi = min(len(lines), hits[-1] + 12)
        return (f"[{req.name} 含「{topic}」的片段]\n"
                + "\n".join(lines[lo:hi]))
    return (f"[{req.name} 含「{topic}」的段落（{len(blocks)} 处，取前 3）]\n"
            + "\n\n---\n\n".join(b.strip()[:2500] for b in blocks[:3]))


class ToolLoop:
    """在 remote.complete 之上做多轮工具往返：模型发 tool 调用 → 系统执行 → 回结果 → 再 complete。

    complete_fn: 可选 (instruction, messages) -> str，用于测试注入假模型；缺省走 remote.complete。
    """

    def __init__(self, ws: Path, level: str = "S", max_rounds: int = 6,
                 emit=None, complete_fn=None, memory=None,
                 native_tools: bool = True, max_out: int = 0,
                 tool_mode: str = "", model: str = "", compat: str = ""):
        self.ws = Path(ws)
        self.level = str(level).upper() or "S"
        self.max_rounds = max_rounds if max_rounds > 0 else 6
        self.emit = emit
        self.complete_fn = complete_fn
        self.memory = memory or _memory.MemoryStore(self.ws)
        self.rounds = 0
        self.tool_calls: list[str] = []
        self.memory_written: list[dict] = []
        self.denied: list[str] = []
        self.native_tools = bool(native_tools)   # 原生 function calling（官方通路）
        # 2026-11-09：工具路径的输出上限也不按任务量掐（0 = 不设，走模型物理上限）
        self.max_out = int(max_out or 0)
        self._tool_log: list[dict] = []          # 每次工具调用的 {name, ok}（可观测性）
        self._had_native = False
        self._last_finish_reason = ""           # 2026-11-09：截断判定的硬信号
        # ── 工具通路（2026-11-10：模型能力参差不齐，按能力选路）──────────────
        # native / xml / none；不给就按"模型名 + 厂商"查本地能力表（未知则先试 native）。
        self.model = str(model or "")
        self.compat = str(compat or "")
        self.tool_mode = str(tool_mode or "").strip().lower() or _capability.tool_mode(
            self.model, self.compat)
        self.downgraded_from = ""               # 从哪条降下来的（可观测）

    def _guide(self) -> str:
        """按当前通路取提示词（每条通路一份，别用一份糊弄所有模型）。"""
        return guide_for(self.tool_mode)

    # -------------------------------------------------- 受保护区（第 4 步）
    def _tool_allow_write(self, rel: str) -> tuple[bool, str]:
        low = str(rel).replace("\\", "/").lower()
        # 测试/契约文件：对远端工具永远只读（与规模级别无关，因为工具循环没有人在回路里兜底）
        if _TEST_RE.search(low):
            return False, f"远端工具循环禁止写测试/契约文件（受保护区只读）：{rel}"
        # 认证/部署/治理/AGENTS.md 等：对远端工具也永远只读（不受 L+ 才 block 的限制）
        hits = _scale.protected_hits(self.ws, [rel])
        if hits:
            return False, f"受保护区（远端工具只读）：{hits[0]} 不允许工具循环写入"
        return True, ""

    # -------------------------------------------------- 工具分发
    def _run_tool(self, name: str, attrs: dict, body: str) -> tuple[bool, str]:
        try:
            if name == "read_file":
                out = _tools.read_file(self.ws, attrs.get("path", ""),
                                       int(attrs["start"]) if attrs.get("start") else None,
                                       int(attrs["end"]) if attrs.get("end") else None)
                return True, out
            if name == "outline":
                return True, _outline(self.ws, attrs.get("path", ""))
            if name == "imports":
                return True, _imports(self.ws, attrs.get("path", ""))
            if name == "check_import":
                return True, _check_import(self.ws, attrs.get("path", ""))
            if name == "contract_for":
                return True, _contract_for(self.ws, attrs.get("module") or attrs.get("path", ""))
            if name == "route_status":
                return True, _route_status(self.ws, attrs.get("method") or "GET",
                                           attrs.get("path") or "")
            if name == "git_history":
                return True, _git_history(self.ws, attrs.get("path") or attrs.get("rel") or "",
                                          int(attrs.get("limit") or 3))
            if name == "run_python":
                return True, _run_python(self.ws, attrs.get("code") or "",
                                         int(attrs.get("timeout") or 60))
            if name == "requirement_for":
                return True, _requirement_for(self.ws, attrs.get("topic") or "")
            if name == "list_files":
                out = "\n".join(_tools.list_files(self.ws, attrs.get("sub", ""),
                                                  int(attrs.get("limit") or 200)))
                return True, out or "(空)"
            if name == "grep":
                return True, _grep(self.ws, attrs.get("pattern", ""),
                                   attrs.get("path", "."), int(attrs.get("limit") or 200))
            if name == "run_tests":
                rc, out = _tools.run_bash(self.ws, f"pytest {attrs.get('target','')} -q", timeout=120)
                return rc == 0, f"pytest rc={rc}\n{out[:1500]}"
            if name == "static_analysis":
                return True, _static_analysis(self.ws, attrs.get("path", ""))
            if name == "write_memory":
                text = (body or attrs.get("text") or "").strip()
                if not text:
                    return False, "write_memory 缺少 text"
                entry = self.memory.append(text, attrs.get("type", "fact"),
                                           attrs.get("src", "tool_loop"))
                if entry is None:
                    return False, "记忆写入失败（路径不可写或内容为空）"
                self.memory_written.append(entry)
                return True, f"已写入记忆 [{entry['type']}]：{entry['text'][:80]}"
            return False, (f"未知工具：{name}（可用：read_file/outline/list_files/grep/imports/"
                           f"check_import/contract_for/run_tests/static_analysis/write_memory）")
        except Exception as exc:                # noqa: BLE001
            return False, f"工具 {name} 执行异常：{type(exc).__name__}: {exc}"

    def _complete(self, messages: list[dict], allow_tools: bool = True) -> dict:
        """跑一次远端。返回 {answer, tool_calls}。

        **原生 function calling 优先**：把 10 个工具的 JSON Schema 交给 API
        （DeepSeek 官方格式），模型用 `tool_calls` 正式"调用"工具；
        拿不到 tool_calls 时再退回旧的 `<tool .../>` 文本协议（兼容不支持
        tools 的端点/本地模型）。两条路都把结果回灌进对话。

        `allow_tools=False`：**不挂 tools**（收尾追问用）——
        远端已经连着几轮在调工具了，追问时必须明确"现在只给答案"。
        """
        from . import remote
        from . import toolschema as _ts
        # **只有 native 通路才在请求里带 tools**（xml / none 通路带过去会被端点拒）
        _tools_schemas = (_ts.tool_schemas()
                          if (self.native_tools and self.tool_mode == _capability.MODE_NATIVE)
                          else None) \
            if allow_tools else None
        if self.complete_fn:
            try:
                _r = self.complete_fn(self.instruction, messages, _tools_schemas)
            except TypeError:
                # 旧的两参 complete_fn（strategy.py 那类）仍然可用
                _r = self.complete_fn(self.instruction, messages)
        else:
            body = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
            _r = remote.complete(prompt=body or self.instruction, workspace=str(self.ws),
                                 task_id="toolloop", messages=messages, tools=_tools_schemas,
                                 max_out=self.max_out or None)
        if isinstance(_r, dict):
            self._last_finish_reason = str(_r.get("finish_reason") or "")
            return {"answer": str(_r.get("answer") or ""),
                    "tool_calls": list(_r.get("tool_calls") or []),
                    "message": _r.get("message") or {},
                    # 2026-11-09：回传 finish_reason，让上层能判"是不是被配额截断"
                    "finish_reason": self._last_finish_reason,
                    # 2026-11-10：**错误必须带上来** —— 上层要靠它判"是不是这个模型
                    # 不支持 tools"（不支持就降级到 XML 通路，而不是当成能力不足重试）
                    "error": str(_r.get("error") or "")[:300]}
        return {"answer": str(_r or ""), "tool_calls": [], "message": {},
                "finish_reason": "", "error": ""}

    def run(self, instruction: str) -> dict:
        """跑一轮工具循环，返回 {answer, rounds, tool_calls, memory_written, denied}。

        两条通路：
          · **原生 function calling**（DeepSeek 官方）：请求带 `tools`（JSON Schema），
            响应带 `tool_calls`（function.arguments 是 JSON 字符串）→ 执行 →
            把助手那轮（含 tool_calls）原样送回 + 逐条 `{"role":"tool","tool_call_id"}`。
          · **XML 文本协议兜底**（端点不支持 tools 时）：模型吐 `<tool name=... />`。
        """
        self.instruction = instruction
        messages: list[dict] = [
            {"role": "system", "content": self._guide()},
            {"role": "user", "content": instruction},
        ]
        last = ""
        _last_with_text = ""          # 最后一次**有实质正文**的回复（不含纯工具轮）
        for rnd in range(1, self.max_rounds + 1):
            self.rounds = rnd
            _res = self._complete(messages)
            # ── 端点说"这个模型不支持 tools" → **降级换路重试**（不是能力不足）──
            # 2026-11-10：以前这里没有这一段 → 400 被当成"远端调用失败" →
            # 升档重试 → 还是同样失败 → 模块判红 → **机制问题被记成模型能力问题**。
            _err = str(_res.get("error") or "")
            if _err and self.tool_mode == _capability.MODE_NATIVE \
                    and _capability.is_tools_unsupported(_err):
                _from = self.tool_mode
                self.tool_mode = _capability.degrade(self.tool_mode)
                self.downgraded_from = _from
                if self.model:
                    _capability.remember(self.model, self.tool_mode, why=_err)
                messages[0]["content"] = self._guide()        # 换对应通路的提示词
                if self.emit:
                    self.emit("tool_mode_downgrade", {
                        "model": self.model, "from": _from, "to": self.tool_mode,
                        "why": _err[:200]})
                _res = self._complete(messages)                # 用新通路重试同一轮
            reply = _res.get("answer") or ""
            native = list(_res.get("tool_calls") or [])
            if not native and reply.strip():
                # 只有"这一轮没有再调工具"的回复才是**答案**。
                # 纯工具轮的 content 通常是空的（模型在请求工具），
                # 拿它当答案 → 上层解析不到 JSON → 整轮白跑
                # （实测 2026-11-09：tools=5 rounds=3，最后一轮还在调工具，
                #   结果 "整文件模式两版都未通过：没有给出 {"files":[...]}"）。
                _last_with_text = reply
            last = _last_with_text or reply
            native = list(_res.get("tool_calls") or [])
            if native:
                self._had_native = True
                # 助手那轮必须**原样**送回（content 为空 + tool_calls），否则 API 对不上 id。
                #
                # `reasoning_content` 也必须带上（DeepSeek 官方 2026-11 核对）：
                # **请求带 tools 时，上一轮的 reasoning_content 必须原样回传**，
                # 否则 API 返回 400；不带 tools 时回传会被忽略。
                # 漏了它 = 工具循环第二轮 400，且模型丢掉自己上一轮的思考链。
                _rc = str((_res.get("message") or {}).get("reasoning_content") or "")
                _asst = {"role": "assistant", "content": reply or "", "tool_calls": native}
                if _rc:
                    _asst["reasoning_content"] = _rc
                messages.append(_asst)
                for _tc in native:
                    _fn = _tc.get("function") or {}
                    _name = str(_fn.get("name") or "").strip().lower()
                    _raw = _fn.get("arguments")
                    _args: dict = dict(_raw) if isinstance(_raw, dict) else {}
                    if isinstance(_raw, str):
                        try:
                            _args = json.loads(_raw or "{}")
                        except Exception:                   # noqa: BLE001
                            _args = {}
                    if not _name:
                        continue
                    self.tool_calls.append(
                        f"{_name}({_args.get('path') or _args.get('target') or ''})")
                    self._tool_log.append({"name": _name, "ok": None})
                    _ok, _out = self._run_tool(_name, _args, "")
                    if self._tool_log:
                        self._tool_log[-1]["ok"] = bool(_ok)
                    messages.append({"role": "tool",
                                     "tool_call_id": str(_tc.get("id") or ""),
                                     "content": str(_out)})
                if self.emit:
                    self.emit("tool_loop", {"round": rnd, "tools": len(native),
                                            "memory": len(self.memory_written),
                                            "denied": len(self.denied),
                                            "names": [x["name"] for x in self._tool_log],
                                            "failed": [x["name"] for x in self._tool_log
                                                       if x.get("ok") is False],
                                            "mode": "native"})
                continue
            # ── XML 文本协议兜底 ──
            calls = parse_tool_calls(reply)
            if not calls:
                break
            results: list[str] = []
            for name, attrs, body in calls:
                self.tool_calls.append(f"{name}({attrs.get('path') or attrs.get('target') or ''})")
                self._tool_log.append({"name": name, "ok": None})
                if name == "write_file":
                    allowed, reason = self._tool_allow_write(attrs.get("path", ""))
                    if not allowed:
                        self.denied.append(reason)
                        results.append(f"<tool_result name=\"{name}\">\n拒绝：{reason}\n</tool_result>")
                        continue
                ok, out = self._run_tool(name, attrs, body)
                if self._tool_log:
                    self._tool_log[-1]["ok"] = bool(ok)
                results.append(f"<tool_result name=\"{name}\" ok=\"{ok}\">\n{out}\n</tool_result>")
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": "工具结果：\n" + "\n".join(results)})
            if self.emit:
                self.emit("tool_loop", {"round": rnd, "tools": len(calls),
                                        "memory": len(self.memory_written),
                                        "denied": len(self.denied),
                                        "names": [x["name"] for x in self._tool_log],
                                        "failed": [x["name"] for x in self._tool_log
                                                   if x.get("ok") is False],
                                        "mode": "xml"})
        # ── 收尾：跑满轮次还没给正文 → 明确要求它给最终答案（不再给工具） ──
        # 实测 2026-11-09：远端连着几轮都在调工具、正文为空 → 引擎拿不到代码 →
        # 整轮废弃重写（还白烧了那些工具轮）。这里补一轮"只给答案"的追问。
        if not _last_with_text.strip():
            try:
                messages.append({"role": "user",
                                 "content": "不要再调用任何工具。请**现在**直接给出最终答案正文"
                                            "（按任务要求的三种格式之一），只输出答案本身。"})
                _res2 = self._complete(messages, allow_tools=False)
                _rep2 = _res2.get("answer") or ""
                if _rep2.strip():
                    _last_with_text = _rep2
                    last = _rep2
                    self.rounds += 1
                    if self.emit:
                        self.emit("tool_loop", {"round": self.rounds, "tools": 0,
                                                "memory": len(self.memory_written),
                                                "denied": len(self.denied),
                                                "names": [], "failed": [],
                                                "mode": "final_answer"})
            except Exception:                            # noqa: BLE001
                pass
        return {
            "answer": last, "rounds": self.rounds,
            "tool_calls": list(self.tool_calls),
            "memory_written": list(self.memory_written),
            "denied": list(self.denied),
            # 2026-11-09：把最后一轮的 finish_reason 传出去，
            # 让上层能判"是不是被配额截断"（硬信号），而不是拿 token 数去猜。
            "finish_reason": self._last_finish_reason,
            "hit_max_rounds": self.rounds >= self.max_rounds and bool(
                self._had_native or parse_tool_calls(last)),
        }


def run_with_tools(ws, prompt: str, call, *, max_rounds: int = 3, emit=None) -> dict:
    """带**原生 function calling**跑一次远端对话；工具由引擎执行、结果回灌。

    `call(messages, tools) -> {"ok","answer","usage","tool_calls"}`：调用方自己负责
    拼 `remote.complete(...)`（这样 task_id / node_id / max_out / 用量统计仍由调用方掌握）。
    返回 {"ok","answer","rounds","tool_calls"}。

    为什么统一走这里：夜班里有 5 条"远端要动手改代码"的路径（写函数体 / 写整文件 /
    行级修复 / 救援 / 升级求援），每条都该让远端**先看真实代码再动手**；
    复制五份同样的循环代码 = 五处会各自漂移的真相源。
    """
    from pathlib import Path as _P

    def _fc(_instr, messages, _tools=None):
        r = call(messages, _tools)
        if isinstance(r, dict):
            return {"answer": r.get("answer") or "",
                    "tool_calls": list(r.get("tool_calls") or []),
                    "message": r.get("message") or {}}
        return {"answer": str(r or ""), "tool_calls": [], "message": {}}

    loop = ToolLoop(_P(ws), max_rounds=max_rounds, emit=emit, complete_fn=_fc)
    res = loop.run(prompt)
    return {"ok": True, "answer": res.get("answer") or "",
            "rounds": res.get("rounds"),
            "tool_calls": list(res.get("tool_calls") or [])}


def tool_loop_guide(context_complete: bool = False) -> str:
    """给 author_module 注入的工具说明（远端开启 tool_loop 时追加到提示词）。

    context_complete=True 表示**目标文件的全文与契约已经放在提示词里了**。
    这时必须明说"别再调工具去读它" —— 实测（2026-11-10 22:04 那一轮）：
      15 次 author_start、23 次重试，却打出 **166 次 tool_loop**，
      每次工具轮都是一次**带完整上下文的付费请求** → 调用量是必要量的 3~4 倍。
    这不是"少给上下文"，而是**别让它为了看已经给过的东西再请求一遍**：
    该看的一个字都没少，只是不再重复付一次费。
    """
    if not context_complete:
        return GUIDE
    return GUIDE + (
        "\n\n【重要】本次提示词里**已经给了**目标文件的当前全文、契约、签名表与失败证据，"
        "内容完整、可直接据此作答。**不要为了再看这个文件而调用工具**"
        "（read_file / outline / grep 都会把同一份内容再取一遍，纯属浪费一轮）。"
        "只有当你确实需要看**别的文件**（依赖模块、调用方）时才用工具。"
    )


# ---------------------------------------------------------------- 诊断回路（接主线）
_DIAGNOSE_INSTRUCTION = """\
你是**诊断角色**，不是实现角色。本轮只做一件事：找出下面这次失败最可能的原因。

【目标文件】{path}

【失败证据】
{failure}

【你要做的】
1. 用只读工具查证，不要凭印象猜。特别是：
   - `<tool name="grep" pattern="端点或函数名" path=".sixcoder" />`
     查 web_routes.json（接线表）/ blueprint.json（探针链）/ slices/*.txt（本片契约）；
   - `read_file` 看目标文件里实际有哪些函数、签名是什么；
   - `static_analysis` 看语法，`run_tests` 只在确实有对应测试时用。
2. 给出最多 3 个假设，按可能性排序，每条写清「证据 / 为什么能解释这次失败 / 怎么验证」。
3. 指出下一轮**最小的一处改动**应该落在哪里。

【硬约束】
- 只诊断，**不要改代码**，不要输出补丁、函数体或整文件。
- 没查到证据的假设要标「未验证」，不要编造。
- 结论用纯文本，不要 JSON，不要代码块。
"""


def diagnose_fail(ws, path: str, failure: str, emit=None,
                  complete_fn=None, max_rounds: int = 3) -> str:
    """写作判据失败后，让远端**带工具自己诊断**，把结论回灌给下一轮证据。

    为什么必须接（实测 2026-11-09）：`ToolLoop` 状态机 + 6 个只读工具早已实现，
    但 `tool_loop=True` 没有任何调用点，`author.py` 只追加了一句 GUIDE 说明、
    不解析 / 不执行 / 不回灌。于是失败信号永远只有一句 `404 路由未命中`，
    远端拿不到「为什么」，只能在同一处反复整文件重写。这是「没有人在环」时
    最该补的一环：**让远端 API 自己把问题查出来**。

    设计约束：
      · 角色分离——诊断只读、不产出代码（避免既诊断又实现导致自我确认）；
      · 只读工具与受保护区守卫沿用 `ToolLoop` 既有实现；
      · 轮次硬上限 3；每片至多触发一次由调用方控制，远端花费可控；
      · 远端异常**不吞**：emit 留痕后返回空串，写作路径照常继续。
    """
    instruction = (_DIAGNOSE_INSTRUCTION
                   .replace("{path}", str(path or ""))
                   .replace("{failure}", str(failure or "")[:2000]))
    try:
        loop = ToolLoop(Path(ws), max_rounds=max_rounds, emit=emit,
                        complete_fn=complete_fn)
        r = loop.run(instruction)
        answer = str(r.get("answer") or "").strip()
        if emit:
            emit("diagnose_done", {"path": str(path), "rounds": r.get("rounds"),
                                   "tools": len(r.get("tool_calls") or []),
                                   "chars": len(answer)})
        return answer[:1600]
    except Exception as exc:                        # noqa: BLE001
        if emit:
            emit("diagnose_error", {"path": str(path),
                                    "error": f"{type(exc).__name__}: {exc}"[:200]})
        return ""
