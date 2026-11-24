"""整文件 / 多文件由远端写（造物模式的主力方式）—— v2。

与 v1 的区别（按用户要求改的）：
1. **上下文不再"无脑扔源码"**：先用 `contextpack` 压成
   契约摘要 + 模块签名表 + 跨模块调用关系 + 焦点函数完整源码 + 证据。
   同样的 token 预算里装进更多**有用**信息，远端能看清的反而更多。
2. **输出上限放开**：整文件模式默认给 32K 输出（档位默认 8K 只够一个函数）。
3. **允许远端反过来指出本地系统的毛病**：回复可带 `system_issues`
   （契约漏字段、判据缺边界、工具链缺陷…）。只进报告与建议，绝不让远端直接改系统代码。
4. **多模块并行**：无依赖模块同时发（flash 并发 2500，我们只用 3~4）。
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import tokens as _tokens

# ── 远端能力闸门（2026-11-09 用户原则：本地是远端的**放大器**，不是缰绳）────
# 不为省钱把上下文/反馈/输出掐小：掐小 = 让远端写不出来 = 把"机制问题"
# 记成"模型不会写"。实测代价：注入上限 12000 让 app/models.py（20,857 字符）
# 只剩头尾可见 → 远端在中间那段是瞎的；输出 floor 1200 让 26 次"契约函数缺失"
# 实为**被配额砍断**；判据反馈掐 1200 让远端看不到真正的失败原因。
# 下面几个上限只按**模型物理上限**兜底，不按成本设。
REMOTE_CTX_CHARS = 200000      # 目标文件全文注入上限（超出模型窗口由 provider 报错）
REMOTE_TEXT_CHARS = 40000      # 契约/签名参考等文本块上限
REMOTE_FEEDBACK_CHARS = 0      # 0 = 判据反馈不掐（要全量，这是远端唯一的失败真相）
REMOTE_OUT_FLOOR = 4000        # 输出配额下限（顶到上限会自动放大重试）

AUTHOR_PROMPT = """你是资深工程师。按下面的**契约**实现这些函数（改已有文件时**只写这次要写的函数**）。

{protocol}

{context}

{requirements}

输出格式（三选一，**优先 A**）：

（A）**只写函数** —— 要新增 / 重写**少数几个函数**时，用代码围栏只给这几个函数：
```python
def delete_ticket(ticket_id: int) -> dict:
    # 按 id 删除工单，成功返回 deleted=True
    ...
```
  引擎会把它们**合并**进现有文件：同名函数原位替换、新函数追加，
  **不会删掉文件里已有的其它函数**。这是改已有文件时最省事、最不容易出错的方式。

（B）**补丁** —— 要改的是函数中间的几行、且你能逐字复制原文时：
{{"patches":[{{"path":"app/x.py",
  "find":"<从当前文件**原样复制**的旧代码，2~30 行>",
  "replace":"<替换成的新代码>"}}]}}
  · `find` 必须逐字出现在文件里；找不到这份回复就作废

（C）**整份文件** —— 只在新建文件、或改动超过半个文件时用：
```python
<完整文件内容>
```

需要 delegate / system_issues 时，用 JSON 整份文件形式：
{{"files":[{{"path":"模块.py","content":"<完整文件内容>"}}],
 "delegate":[{{"kind":"validation|boilerplate|logging|adapter","target":"函数名","spec":"一句话规格"}}],
 "system_issues":[{{"where":"契约/判据/工具链","issue":"本地的缺陷","suggest":"怎么改"}}]}}

**代码里有大量引号/换行时不要用 JSON**（转义写错会导致整份回复作废）。

铁律：
1. （A）只给"这次要写的函数"（含它们用到的 import 写在最上面即可）；
   （C）里的 content 才是**完整文件**（含 import、docstring、辅助函数）。
2. 每个函数都要有类型注解，参数名与契约完全一致；返回类型一致。
3. **空输入/空值不许崩**：列表可能为空、字典可能缺键、值可能是 None。
   除零、`row["k"]` 直接下标、对 None 运算都会被边界判据判红；先给空情形一个确定结果。
4. 契约要求抛的异常必须抛（例如 `b == 0` 抛 ValueError）；抛错要用契约指定的异常类型。
5. 只写契约里出现的函数；模块内辅助函数用 `_` 开头。
6. 契约里每条示例都要在你的实现上成立。
7. **可维护性硬指标**：{limits}；
   用提前返回、字典查表、拆小函数来降复杂度。
8. 如果你发现**契约或本地判据本身有问题**（漏定义字段、判据缺边界、工具链缺陷），
   写进 system_issues；不要因为契约有问题就自己改签名。
9. **机械性内容可以 delegate**（样板/参数校验/日志/适配层）：你只给一句话规格，最多 3 项。
   本地模型会写并过机械判据，不过会把失败原因还给你下一轮自己写。
   核心逻辑必须你自己写，别把"需要你想清楚"的部分委托出去。
10. **空桩不是实现**：上下文里 `raise NotImplementedError` / 只有 docstring 的函数，
    是**要你写**的桩，不是已有代码。照抄一遍等于什么都没做，会被判红。
11. **前端组件（.jsx / .tsx / .vue）用（C）整份文件，且走 JSON files 格式**：
    {{"files":[{{"path":"src/pages/Home.jsx","content":"<完整组件源码>"}}]}}
    内容写**完整组件**（顶层 import + `export default function 组件名()` + JSX/模板）；
    不要用 `# ===== 函数名 =====` 分段（那是 Python 函数体写法），不要加 Python 类型注解。
    页面组件放 `src/pages/`，复用组件放 `src/components/`。JSX 里的引号用 `\\"` 正常转义。
"""


# 前端组件后缀（2026-11-19 生态包）：不在 core/lang 的 LANGS 表里（那是"能编译验证的语言"），
# 但前端确实要写这些文件。**只加组件后缀，不加 .html/.css**——.html 是故意拒的
# （见下面 `_is_code_path 不认 .html` 那条：防止把 HTML 当 Python 写进 .py）。
_FRONTEND_EXTS = (".jsx", ".tsx", ".vue")


def _is_code_path(path: str) -> bool:
    """这个路径能不能当"要写的源码文件"？多语言：按 core/lang 的表判断后缀。"""
    try:
        from . import lang as _lang
        exts = (tuple(e for cfg in _lang.LANGS.values() for e in cfg["exts"])
                + _FRONTEND_EXTS)
    except Exception:                                    # noqa: BLE001
        exts = (".py",) + _FRONTEND_EXTS
    p = (path or "").replace("\\", "/").strip()
    if not p or p.startswith("/") or ".." in p.split("/"):
        return False
    return p.lower().endswith(tuple(x.lower() for x in exts))


def _parse_reply(text: str) -> tuple[list[dict], list[dict], list]:
    """从回复里抽 (files, system_issues)。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    data = None
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        break
    if not isinstance(data, dict):
        return [], [], []
    files = data.get("files")
    if isinstance(files, dict):
        files = [{"path": k, "content": v} for k, v in files.items()]
    out: list[dict] = []
    for f in (files or []):
        if not isinstance(f, dict):
            continue
        path = str(f.get("path") or "").strip().replace("\\", "/")
        content = f.get("content")
        if content is None:
            content = f.get("code") or ""
        # 这里以前硬编码 `.py`，等于把"只支持 Python"钉在回复解析器里 ——
        # 换语言（Java/C/TS…）时模型给对了 JSON，却在这里被静默丢掉（实测 0 个文件）。
        if not _is_code_path(path) or not str(content).strip():
            continue
        out.append({"path": path, "content": str(content)})
    delegated = data.get("delegate") or data.get("delegates") or []
    issues = []
    for it in (data.get("system_issues") or []):
        if isinstance(it, dict) and (it.get("issue") or it.get("where")):
            issues.append({"where": str(it.get("where") or "")[:80],
                           "issue": str(it.get("issue") or "")[:300],
                           "suggest": str(it.get("suggest") or "")[:300]})
    out_deleg = delegated if isinstance(delegated, list) else []
    return out, issues, out_deleg


def _parse_patches(text: str) -> list[dict]:
    """从回复里抽 `{"patches":[{path, find, replace}]}`（局部替换，不重写整篇）。

    为什么要补丁（实测 2026-11-07）：作者每轮整文件重写，一是把上一片写好的函数
    顺手删掉（被函数保全打回，白烧一轮），二是输出量翻几倍、弱模型更容易写崩。
    局部替换把"改一个函数"变成"贴一段旧代码 + 给新代码"，风险与成本都低一个量级。
    """
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    data = None
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        break
    if not isinstance(data, dict):
        return []
    raw = data.get("patches") or data.get("edits") or []
    if isinstance(raw, dict):
        raw = [raw]
    out: list[dict] = []
    for it in (raw if isinstance(raw, list) else []):
        if not isinstance(it, dict):
            continue
        find = it.get("find") if it.get("find") is not None else it.get("old")
        rep = it.get("replace") if it.get("replace") is not None else it.get("new")
        if find is None or rep is None:
            continue
        out.append({"path": str(it.get("path") or "").strip().replace("\\", "/"),
                    "find": str(find), "replace": str(rep)})
    return out


def _apply_patches(src: str, patches: list[dict]) -> tuple[str, list[str]]:
    """把补丁应用到现有文件。返回 (新内容, 失败说明)。

    匹配顺序：**逐字**（含缩进）→ 忽略行尾空白/整体缩进差异的逐行匹配。
    两次都不中就算失败：宁可不落盘，也不能"猜着改"把无关代码改坏。
    """
    cur = src
    bad: list[str] = []
    for it in patches:
        find = str(it.get("find") or "")
        rep = str(it.get("replace") or "")
        if not find.strip():
            bad.append("find 为空")
            continue
        if find in cur:
            cur = cur.replace(find, rep, 1)
            continue
        # 宽松匹配：忽略行尾空白、允许模型少写/多写整体缩进
        # 先按"去掉缩进也认"比较：模型复制 find 时经常整体多缩/少缩一两级
        want = [ln.strip() for ln in find.splitlines() if ln.strip()]
        cur_lines = cur.splitlines()
        idx = -1
        if want:
            for i in range(len(cur_lines) - len(want) + 1):
                if cur_lines[i].strip() != want[0]:
                    continue
                if all(cur_lines[i + k].strip() == want[k] for k in range(len(want))):
                    idx = i
                    break
        if idx < 0:
            bad.append(want[0][:70] if want else "find 没有有效行")
            continue
        # 用被替换段的**原缩进**给新代码补齐（模型常写成顶格）
        base_indent = cur_lines[idx][:len(cur_lines[idx]) - len(cur_lines[idx].lstrip())]
        rep_lines = rep.splitlines()
        if base_indent and rep_lines and not rep_lines[0][:1].isspace():
            rep_lines = [base_indent + ln if ln.strip() else ln for ln in rep_lines]
        tail = cur_lines[idx + len(want):]
        cur = "\n".join(cur_lines[:idx] + rep_lines + tail)
        if src.endswith("\n") and not cur.endswith("\n"):
            cur += "\n"
    return cur, bad


_ROUTE_BEGIN = "# --- 引擎补的路由接线"
_ROUTE_END = "# --- 路由接线结束 ---"


def _toplevel_names(src: str) -> set[str]:
    import ast as _ast
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return set()
    return {n.name for n in tree.body
            if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef))}


def _fragments_only(code: str, base: str) -> str | None:
    """判断"这段代码是不是只包含**新增**的函数（+import）"，是就合并进 base。

    为什么需要（实测 2026-11-07）：glm-4-flash 被要求"只写新函数"时，最自然的输出
    就是围栏里一个 `def delete_ticket(...)`，而不是"复制旧代码 + 替换"。这属于
    **增量**，却会被当成"整份文件"落盘 → 上一片写好的函数全被删（再被函数保全打回）。
    所以：只要围栏里的顶层定义**都是新名字**，就按增量合并，而不是整份覆盖。
    """
    import ast as _ast
    if not base.strip() or not code.strip():
        return None
    try:
        tree = _ast.parse(code)
    except SyntaxError:
        return None
    defs: list = []
    imports: list[str] = []
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            defs.append(node)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            imports.append(_ast.get_source_segment(code, node) or "")
        elif isinstance(node, _ast.Expr) and isinstance(getattr(node, "value", None), _ast.Constant):
            continue                      # 模块级 docstring，忽略
        elif isinstance(node, _ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], _ast.Name) and \
                isinstance(node.value, _ast.Constant):
            continue                      # 只允许模块级**常量**（表名之类）；对象/调用不行
        else:
            return None                   # 出现语句/路由/if __main__：不是纯增量，按整份处理
    if not defs:
        return None
    existing = _toplevel_names(base)
    new_names = [d.name for d in defs]
    if any(n in existing for n in new_names):
        return None                       # 有同名定义 = 想重写已有函数，走整份（带保全校验）
    body = "\n\n".join((_ast.get_source_segment(code, d) or "").rstrip() for d in defs)
    if not body.strip():
        return None
    head, tail = base, ""
    if _ROUTE_BEGIN in base:             # 新函数插在引擎段之前，别插在路由后面
        head, tail = base.split(_ROUTE_BEGIN, 1)
        tail = _ROUTE_BEGIN + tail
    add_imports = [i for i in imports
                   if i.strip() and i.split()[1].split(".")[0] not in base]
    parts = [head.rstrip()]
    if add_imports:
        parts.append("\n".join(dict.fromkeys(add_imports)))
    parts.append(body)
    merged = "\n\n".join(parts) + "\n"
    if tail:
        merged += "\n" + tail
    return merged


def _code_fallback(text: str, path: str) -> str:
    """模型没给合法 JSON 时，退回"取代码围栏"（代码的主力载体）。

    实测（2026-11-07，glm-4-flash）：要求它把整份模块塞进 JSON 字符串，
    两次都交不出合法 JSON（代码里的引号/换行要转义，转义写错整份作废）。
    于是作者认为"什么都没生成"，同一份 NotImplementedError 骨架连撞 6 轮 ——
    时间全花在重写上，代码一行没长。代码本来就有更好的载体：围栏。
    """
    t = (text or "").strip()
    if not t:
        return ""
    blocks = re.findall(r"```[a-zA-Z0-9_+.#-]*[ \t]*\r?\n(.*?)```", t, re.DOTALL)
    body = ""
    if blocks:
        body = max(blocks, key=len).strip()
    elif re.match(r'^(?:#!|"""|\'\'\'|from\s|import\s|def\s|class\s|@)', t):
        body = t
    if not body:
        return ""
    # 必须真的像代码（防止把解释文字当代码写进文件）
    if not re.search(r"^\s*(?:def|class|import|from|@)\s|^\s*#!|^\s*[\"']", body, re.M):
        return ""
    return body if _is_code_path(path) else ""


def _parse_files(text: str) -> list[dict]:
    return _parse_reply(text)[0]


def _strip_fence(code: str) -> str:
    c = (code or "").strip()
    m = re.search(r"```(?:python|py)?\s*(.*?)```", c, re.DOTALL)
    if m:
        c = m.group(1).strip()
    return c + ("\n" if c and not c.endswith("\n") else "")


# ---------------------------------------------------------------- 产物文件（§20.4 A 档落点 3）
def parse_artifact_blocks(text: str) -> dict[str, str]:
    """从回复里抽"产物文件块"：```file:路径 围栏 / <file path="路径"> 块。

    为什么单独一条通道：bodies 路径只收 ```python 里的函数体，module 路径把
    第一个围栏当**整个模块**——模板/静态资源要是也走这两条路，要么被过滤
    （_is_code_path 不认 .html）、要么把 HTML 当 Python 写进 .py。
    围栏信息串必须是**带扩展名的路径**才认（防止把 ```python 误吞）。
    """
    t = str(text or "")
    out: dict[str, str] = {}
    # ① ```file:app/templates/index.html … ```（冒号可省、可有空格）
    for m in re.finditer(r"```(?:file:|file：)[ \t]*`?([\w][\w./\\ -]*?\.[A-Za-z0-9]{1,6})`?[ \t]*\r?\n(.*?)```",
                         t, re.S):
        out.setdefault(m.group(1).replace("\\", "/").strip(),
                       _strip_fence_free(m.group(2)))
    # ② ```app/templates/index.html … ```（围栏信息串直接是路径；限定常见前缀防误吞）
    for m in re.finditer(r"```[ \t]*`?((?:app|apps|src|static|templates|web|public)[\w./\\ -]*?\.[A-Za-z0-9]{1,6})`?[ \t]*\r?\n(.*?)```",
                         t, re.S):
        out.setdefault(m.group(1).replace("\\", "/").strip(),
                       _strip_fence_free(m.group(2)))
    # ③ <file path="...">…</file>
    for m in re.finditer(r"<file\s+path=[\"']([\w][\w./\\ -]*?\.[A-Za-z0-9]{1,6})[\"']\s*>\r?\n?(.*?)</file>",
                         t, re.S):
        out.setdefault(m.group(1).replace("\\", "/").strip(),
                       _strip_fence_free(m.group(2)))
    return {k: v for k, v in out.items() if v.strip()}


def _strip_fence_free(code: str) -> str:
    """产物内容若被模型再包了一层围栏就剥掉；否则原样（保留 HTML 的换行结构）。"""
    c = (code or "").strip("\n")
    m = re.match(r"^[ \t]*```[\w-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```[ \t]*$", c, re.S)
    return (m.group(1) if m else c) + "\n"


def write_artifacts(ws: Path, reply: str, arts, emit=None, how: str = "") -> dict:
    """把回复里的产物文件块写到声明的路径（只写契约声明过的；幂等）。

    返回 {"written": [...], "missing": [...]}：missing = 声明了但这次没交/没写成的。
    """
    try:
        from . import tools as _tools                    # 模块级没导 tools（本文件惯例：函数内导）
        declared = {a.path: a for a in (arts or []) if getattr(a, "path", "")}
        if not declared:
            return {"written": [], "missing": []}
        blocks = parse_artifact_blocks(reply)
        written: list[str] = []
        for path in declared:
            content = blocks.get(path)
            if content is None:
                continue
            p = _tools.safe_path(ws, path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            written.append(path)
        missing = [path for path in declared
                   if path not in written and not (Path(ws) / path).is_file()]
        if emit and (written or missing):
            emit("author_artifacts", {"written": written, "missing": missing,
                                      "how": how})
        return {"written": written, "missing": missing}
    except Exception:                                    # noqa: BLE001
        return {"written": [], "missing": []}


def _toplevel_symbols(src: str) -> set[str]:
    """顶层公开符号（函数/类），忽略引擎自己的 `_engine_*` / `_Body_*`。"""
    import ast as _ast
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            n = node.name
            if n.startswith("_engine_") or n.startswith("_Body_"):
                continue
            out.add(n)
    return out


def _deleted_symbols(old: str, new: str) -> list[str]:
    """这次重写删掉了哪些已有符号 —— 整文件重写最容易犯的错。

    实测（2026-11-07）：作者为了加一条能力把整篇重写，顺手把上一片写好的
    `list_tickets/search_tickets/...` 删了 → 那些能力从"已完成"退回"缺"，
    整片再重写一次、再删一次，来回振荡到停机。
    """
    return sorted(_toplevel_symbols(old) - _toplevel_symbols(new))


def _restore_symbols(old: str, new: str, names: list[str]) -> str:
    """把被误删的顶层符号按**旧实现原样**接回新文件；接不回来返回空串。

    为什么由引擎补而不是打回给模型：缺哪些函数是确定性事实（AST 一比就知道），
    让远端再猜一次既费钱又会得到同一份"干净文件"。这里只补函数本体，不动新文件
    里已经写好的内容。
    """
    from . import symedit
    edits: list[dict] = []
    for name in names:
        code = symedit.source_of(old, name)
        if not code.strip():
            continue
        edits.append({"op": "add", "symbol": name, "code": code, "after": ""})
    if not edits:
        return ""
    rep = symedit.apply_edits(new, edits)
    if not rep.ok or not (rep.new_source or "").strip():
        return ""
    try:
        compile(rep.new_source, "<restore>", "exec")
    except SyntaxError:
        return ""
    return rep.new_source


def _compile_ok(src: str) -> tuple[bool, str]:
    try:
        compile(src, "<remote>", "exec")
        return True, ""
    except SyntaxError as exc:
        return False, f"第 {exc.lineno} 行语法错误：{exc.msg}"



# ---------------------------------------------------------------- 只回函数体

def _quality_limits_text(ws=None) -> str:
    """可维护性阈值的一句话说明（**从 scale 动态取，不写死**）。

    2026-11-24 修：原来两处提示词都写死「圈复杂度 ≤8、嵌套 ≤3、≤40 行」，
    但 gates.gate_quality_metrics 实际按项目规模判：
        S 12/4/60、M 10/4/50、L 8/3/40、XL 8/3/40
    于是 S 级项目里模型被告知"必须 ≤8"，写出来反而更绕；
    实测失败数据里 33 条卡在"圈复杂度超限"。
    提示词说的必须和判据判的一致 —— 否则模型在为一个过严的数字挣扎。
    """
    try:
        from . import scale as _scale
        lv = _scale.resolve("", ws)["level"] if ws is not None else "S"
        lim = _scale.quality_limits(lv)
        return ("单函数圈复杂度 ≤%d、嵌套 ≤%d、单函数 ≤%d 行（%s 级；本地会判红）"
                % (lim["complexity"], lim["depth"], lim["lines"], lv))
    except Exception:                                # noqa: BLE001
        return "单函数圈复杂度 ≤12、嵌套 ≤4、单函数 ≤60 行（本地会判红）"


BODIES_PROMPT = """你是资深工程师。骨架已经由引擎按契约渲染好了，你**只写函数体**，不写 def 行。

{protocol}

{file_block}

{skeleton_block}

{contract_block}

{plan_block}

{evidence_block}

【输出格式：用 ```python 围栏包住代码，不要 JSON、不要解释】
（下面的例子**就是本次要写的函数名**，照抄名字，别用别的名字）

```python
{format_example}
```

规则：
1. 每个函数一段，用 `# ===== 函数名 =====` 开头（名字必须和骨架里的一字不差）。
2. 段内是**函数体**：**顶格写**，不要写 def 行、不要自己加外层缩进（引擎会缩进到 def 下面）。
3. 段内的 if/for/try 内部的缩进照常写（4 空格），引擎只负责整体平移。
4. 不要输出整文件、不要输出 import。
5. 空输入/空值不许崩；契约要求抛的异常必须抛。
6. 契约里每条示例都要在你的实现上成立。
7. **可维护性硬指标**：{limits}。
   降复杂度用提前返回、字典查表、把大 if 拆成小函数（辅助函数放 helpers）。

辅助函数（可选）：围栏里再用 `# ===== helper:_名字 =====` 开头给**完整 def 定义**，
引擎会把它放在模块级。
"""


def parse_fenced_bodies(text: str) -> tuple[dict, dict]:
    """从 ```python 围栏里取 {函数名: 函数体} 与 {辅助函数名: 完整 def}。

    为什么换掉 JSON（实测 2026-11-09）：旧协议要求把函数体塞进 JSON 字符串
    （`{"bodies":{"f":"def f():\n    ..."}}`），缩进和换行全靠手写转义。
    结果 Flash 反复在 `'(' was never closed` 上卡死——**代码写在围栏里是模型的
    原生形态，塞进 JSON 是反模式**。

    兼容：围栏里如果直接给了完整 `def`，按名字整体收进 helpers 之外的 bodies 也认
    （用 AST 判断首个非注释节点是不是 FunctionDef），避免模型"多写了个 def 行"就全废。
    """
    import ast as _ast
    import re as _re

    def _dedent_body(src: str) -> str:
        lines = src.splitlines()
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        pad = min((len(x) - len(x.lstrip()) for x in lines if x.strip()), default=0)
        return "\n".join(x[pad:] if x.strip() else "" for x in lines)

    bodies: dict = {}
    helpers: dict = {}
    blocks = _re.findall(r"```(?:python|py)?[ \t]*\n(.*?)```", str(text or ""), _re.S)
    for blk in blocks:
        cur, buf, is_helper = None, [], False
        def _flush():
            nonlocal cur, buf, is_helper
            if cur is None:
                return
            raw = "\n".join(buf)
            if is_helper:
                helpers[cur.lstrip("_") if False else cur] = raw.strip("\n")
            else:
                code = _dedent_body(raw)
                if not code.strip():
                    return bodies.get(cur)
                try:
                    tree = _ast.parse(code)
                except SyntaxError:
                    tree = None
                # 模型多写了整段 def：剥掉 def 行，只留函数体
                if (isinstance(tree, _ast.Module) and len(tree.body) == 1
                        and isinstance(tree.body[0], (_ast.FunctionDef, _ast.AsyncFunctionDef))):
                    fn = tree.body[0]
                    seg = _ast.get_source_segment(code, fn)
                    if seg:
                        inner = seg.split("\n", 1)[1] if "\n" in seg else ""
                        bodies[cur] = _dedent_body(inner)
                        return
                bodies[cur] = code
        for line in blk.splitlines():
            m = _re.match(r"^\s*#\s*={3,}\s*(?:函数|helper)?[:：]?\s*"
                          r"(?P<h>helper[:：])?\s*(?P<n>[A-Za-z_]\w*)\s*={3,}\s*$", line)
            if m:
                _flush()
                cur = m.group("n")
                is_helper = bool(m.group("h")) or cur.startswith("_") and "helper" in line.lower()
                buf = []
            elif cur is not None:
                buf.append(line)
        _flush()
    return bodies, helpers


def render_skeleton(funcs, existing_src: str = "", module_doc: str = "",
                    include_bodies: bool = False) -> str:
    """本地渲染骨架：import + 模块 docstring + 每个函数的 def/注解/docstring + 占位体。

    这是"省输出"的关键：签名、注解、docstring、import 全由本地生成（确定性、零 token），
    远端只回函数体那几行。
    """
    lines: list[str] = []
    # 模块级赋值必须随骨架保留（实测 2026-11-08 org 切片：旧版只留 import 行，
    # 契约要求的 _ORG_STATE 全局在重写后凭空消失 → 模型被迫 globals() 动态建
    # → 每次姿势不同 → 判红 → models.py 半天 30 次失败）。用 AST 精确取顶层
    # import/赋值/注解赋值，坏语法退回旧行为（只留 import）。
    # 注意：所有模块级语句统一铺在文件头、函数桩之前 —— apply_bodies 按 "def "
    # 行切块填体，赋值若夹在函数桩之间会被并进上一个函数的体里变成局部变量。
    src0 = existing_src or ""
    try:
        import ast as _ast
        tree = _ast.parse(src0)
    except SyntaxError:
        tree = None
    if tree is not None:
        for node in tree.body:
            if isinstance(node, (_ast.Import, _ast.ImportFrom,
                                 _ast.Assign, _ast.AnnAssign)):
                seg = _ast.get_source_segment(src0, node)
                if seg:
                    lines.extend(seg.splitlines())
    else:
        for ln in src0.splitlines():
            st = ln.strip()
            if st.startswith(("import ", "from ")):
                lines.append(st)
    if lines:
        lines.append("")
    if not module_doc:
        module_doc = "由 SixCoder 生成（骨架由引擎渲染，函数体由远端填写）。"
    lines.append('"""' + module_doc + '"""')
    lines.append("")
    # 现有实现：include_bodies=True 时原样放进骨架，让作者**看得见**已有函数长什么样。
    # 旧骨架只有 raise NotImplementedError，作者被要求"只改这几个、其余保留"，
    # 却完全不知道其余函数实现了什么（实测 2026-11-09：把已验证函数反复重写成别的姿势）。
    ex_map: dict = {}
    if include_bodies and existing_src:
        try:
            import ast as _ast2
            _tree = _ast2.parse(existing_src)
            for _node in _tree.body:
                if isinstance(_node, (_ast2.FunctionDef, _ast2.AsyncFunctionDef)):
                    _seg = _ast2.get_source_segment(existing_src, _node)
                    if _seg:
                        # 记下旧函数的参数名：签名一变（`(authorization, body)` →
                        # `(body, teacher_id)`），旧函数体里的变量名就是错的，
                        # 再塞进骨架只会让模型照抄错名（实测 2026-11-09：
                        # 判据按新签名传参 → 0/2，整片 rollback）。
                        _names = [a.arg for a in _node.args.args]
                        ex_map[_node.name] = (_seg, _names)
        except SyntaxError:
            ex_map = {}

    for f in funcs:
        params = []
        for p in (getattr(f, "params", None) or []):
            t = str(getattr(p, "type", "") or "")
            params.append(f"{p.name}: {t}" if t and t != "object" else p.name)
        ret = str(getattr(f, "returns", "") or "")
        sig = f"def {f.name}({', '.join(params)})" + (f" -> {ret}" if ret else "") + ":"
        lines.append(sig)
        doc = (getattr(f, "doc", "") or "").strip()
        # 不再硬切 160 字符：那会把"抛 ZeroDivisionError"切成"ZeroDivisionEr"，
        # 规范丢半句 → 模型写不对 → 判红 → 无限重写（计算器那次就是这么废的）。
        # 改成 token 预算：够用就整句保留，不够才裁并留省略标记。
        _raw = doc.splitlines()[0] if doc else f"{f.name}({', '.join(params)})"
        first = _tokens.cut(_raw, 300)
        # 契约描述里常带英文双引号（例如 接受 "YYYY-MM-DD"）。直接塞进三引号会生成
        # """...YYYY-MM-DD"""" 这种四引号结尾的非法字符串（实测 clean.py 连炸 6 轮）。
        safe = first.replace('"""', "'''").replace('"', "'").rstrip("'").strip()
        lines.append('    """' + safe + '"""')
        _hit = ex_map.get(f.name)
        if _hit is not None and list(_hit[1]) != [p.name for p in
                                                 (getattr(f, "params", None) or [])]:
            # 签名变了 → 旧体作废，重新写；同时把"签名已变更"写进 docstring 提示
            lines.append("    # 注意：本函数签名已按契约变更，旧实现不可直接复用")
            _hit = None
        if _hit is not None:
            _bl = _hit[0].splitlines()
            _i = 0
            while _i < len(_bl) and not _bl[_i].rstrip().endswith(':'):
                _i += 1
            _rest = _bl[_i + 1:]
            while _rest and (not _rest[0].strip()
                             or _rest[0].strip()[0] in ('"', "'")):
                _rest.pop(0)
            if _rest:
                lines.extend(_rest)
            else:
                lines.append("    raise NotImplementedError")
        else:
            lines.append("    raise NotImplementedError")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _indent_body(body: str, indent: str = "    ") -> list[str]:
    """把远端给的函数体统一缩进（它可能顶格写，也可能已经缩进）。"""
    raw = (body or "").replace("\t", "    ").splitlines()
    if not raw or not any(x.strip() for x in raw):
        return [indent + "pass"]
    lead = min((len(x) - len(x.lstrip()) for x in raw if x.strip()), default=0)
    out: list[str] = []
    for ln in raw:
        out.append("" if not ln.strip() else indent + ln[lead:])
    return out


def apply_bodies(skeleton: str, bodies: dict, helpers: dict | None = None,
                 stub_missing: bool = True) -> tuple[str, list[str]]:
    """把函数体填进骨架。返回 (新源码, 没被填的函数名)。

    stub_missing=True（默认，用于"全新骨架"）：没给函数体的就写 NotImplementedError。
    stub_missing=False（用于"只重写失败的几个函数"）：**没给的函数体原样保留** ——
        实测踩过：这一档原来也会把其它函数的实现擦成 NotImplementedError，
        于是每次局部重写都把已验证的函数弄坏 → 触发回归回滚 → 无限重写同一模块。
    """
    import re as _re
    lines = skeleton.splitlines()
    filled: list[str] = []
    missing: list[str] = []
    idxs = [i for i, ln in enumerate(lines) if ln.startswith("def ")]
    blocks: list[tuple[str, int, int]] = []
    for k, start in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        m = _re.match(r"def\s+(\w+)", lines[start])
        blocks.append((m.group(1) if m else f"f{k}", start, end))

    out: list[str] = []
    cursor = 0
    for name, start, end in blocks:
        out.extend(lines[cursor:start])
        head = lines[start]
        original = lines[start + 1:end]
        keep = [ln for ln in original
                if ln.strip() not in ("raise NotImplementedError", "pass")]
        out.append(head)
        if name in (bodies or {}):
            filled.append(name)
            out.extend(keep)
            out.extend(_indent_body(bodies[name]))
        elif not stub_missing:
            missing.append(name)
            out.extend(original)                    # 原样保留，绝不擦掉
        else:
            missing.append(name)
            out.extend(keep)
            out.append("    raise NotImplementedError")
        cursor = end
    out.extend(lines[cursor:])
    code = "\n".join(out).rstrip() + "\n"
    extra: list[str] = []
    for _hname, hcode in (helpers or {}).items():
        if str(hcode or "").strip():
            extra.append(_strip_fence(str(hcode)))
    if extra:
        code = code.rstrip() + "\n\n\n" + "\n\n".join(extra).strip() + "\n"
    return code, missing



def _parse_json(text: str) -> dict:
    """把回复里的 JSON 对象取出来（容忍围栏与杂质）；取不到返回 {}。"""
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.DOTALL)
    if m:
        t = m.group(1).strip()
    for cand in (t, (re.search(r"\{.*\}", t, re.DOTALL) or [None])[0]):
        if not cand:
            continue
        try:
            data = json.loads(cand)
        except Exception:                                # noqa: BLE001
            continue
        if isinstance(data, dict):
            return data
    return {}


def rebuild_partial(existing: str, funcs, targets: list[str], bodies: dict,
                    helpers: dict | None = None) -> tuple[str, list[str]]:
    """在**现有文件**上只替换 targets 里的函数体，其余函数逐字保留。

    这是"局部重写"的唯一正确入口。它多做一步"补回"：
        模型有时只返回部分函数体（targets 里有 3 个，只给了 1 个）。
        以前这些没给的会被留成 NotImplementedError —— 实测把已验证的函数弄坏，
        触发回归回滚、整夜在同一个模块上打转。现在：没给的一律**还原成原实现**。
    返回 (新源码, 真正缺失的函数名)。
    """
    merged = _merge_skeleton(existing, funcs, targets)
    # **目标函数在文件里还不存在 → 必须把它加进去**（实测 2026-11-09 真机）：
    # 远端交回了完全正确的函数体（parse_fenced_bodies → ['normalize_quota']），
    # 但 `_merge_skeleton` 只保留**文件里已有**的函数 → 新函数根本不在骨架里 →
    # `apply_bodies` 只能替换已有的 def → 新函数被**静默丢掉** →
    # 判据报"契约函数缺失：<名>（文件里根本没有这些函数）"→ 无限重试。
    # 夜班日志里 26 次"契约函数缺失"就是这么来的（不是模型不会写）。
    _have = {ln.split("(", 1)[0].replace("def ", "").strip()
             for ln in existing.splitlines() if ln.startswith("def ")}
    _new_targets = [f for f in funcs if f.name in targets and f.name not in _have]
    if _new_targets:
        _extra = render_skeleton(_new_targets, existing_src="", include_bodies=False)
        merged = merged.rstrip() + "\n\n\n" + _extra.strip() + "\n"
    code, missing = apply_bodies(merged, bodies, helpers, stub_missing=False)
    if not missing:
        return code, []
    restored = _restore_blocks(code, existing, missing)
    return restored, missing


def _restore_blocks(code: str, existing: str, names: list[str]) -> str:
    """把 code 里 names 这几个函数整块换回 existing 里的原样实现。"""
    import re as _re

    def split(src: str) -> tuple[list[str], dict[str, tuple[int, int]]]:
        lines = src.splitlines()
        idx = [i for i, ln in enumerate(lines) if ln.startswith("def ")]
        out: dict[str, tuple[int, int]] = {}
        for k, s0 in enumerate(idx):
            e0 = idx[k + 1] if k + 1 < len(idx) else len(lines)
            m = _re.match(r"def\s+(\w+)", lines[s0])
            if m:
                out[m.group(1)] = (s0, e0)
        return lines, out

    new_lines, new_blocks = split(code)
    old_lines, old_blocks = split(existing)
    # 从后往前替换，避免行号错位
    for name in sorted(names, key=lambda n: -(new_blocks.get(n, (0, 0))[0])):
        if name not in new_blocks or name not in old_blocks:
            continue
        ns, ne = new_blocks[name]
        os_, oe = old_blocks[name]
        new_lines[ns:ne] = old_lines[os_:oe]
    return "\n".join(new_lines).rstrip() + "\n"


def _blame_body(funcs, bodies: dict) -> list[str]:
    """逐个把函数体拼成最小可编译单元，找出"自己就不合法"的那几个。

    实测动机：clean.py 的解析错误来自某个函数体里的引号没闭合，但整文件编译只会报
    "第 16 行"，说不清是谁，于是一路重试整文件、白烧 6 轮。
    """
    out: list[str] = []
    for f in funcs:
        body = (bodies or {}).get(f.name)
        if body is None:
            continue
        snippet = "def _probe():\n" + "\n".join(_indent_body(body))
        try:
            compile(snippet, "<probe>", "exec")
        except SyntaxError as exc:
            out.append(f"{f.name}:第 {exc.lineno} 行 {exc.msg}")
    return out


def _fix_imports_after_write(ws: Path, p: Path, emit, how: str) -> str:
    """写完文件后**立刻**补齐确定性 import（HTTPException / Header / JSONResponse …）。

    为什么必须在落盘后做，而不是只靠每轮的接线阶段（2026-11-10 23:10 实测）：
      接线（wire_web_routes）在**每轮开头**跑，作者在**之后**才写文件。作者整文件重写
      会把引擎补的 import 冲掉，而对齐/判据都发生在这之后 → 文件带着
      `NameError: name 'Header' is not defined` 落盘，判据整模块红，
      系统记成"模型写不出"，还白跑几轮（当晚 app/api/people.py 就是这么红的）。
    缺哪个 import 是**确定性信息**，本地补一行即可，不该让远端猜。

    返回补齐后的源码（读不出来就返回空串，调用方忽略即可）。
    """
    try:
        src = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    out = src
    added: list[str] = []
    try:
        from . import auto as _auto_mod
        # **必须传 ws**（2026-11-15）：不传时 `_stack(None)` 落回 fastapi
        # → Django 项目补出来的是 `from fastapi import ...` 或干脆不补
        # → 远端写的 `models.CharField` / `JsonResponse` 全是 NameError。
        out, _imp = _auto_mod._ensure_imports(out, ws)
        added += list(_imp or [])
        out, _rfix = _auto_mod._ensure_router_decl(out)
        if _rfix:
            added.append("router = APIRouter()")
        out, _local = _auto_mod._ensure_local_imports(ws, out, str(p))
        added += list(_local or [])
    except Exception:                                    # noqa: BLE001
        return src
    if out != src:
        try:
            p.write_text(out, encoding="utf-8")
        except OSError:
            return src
        if emit:
            emit("author_imports", {"path": str(p), "how": how,
                                    "added": added[:8], "n": len(added)})
    return out


def author_bodies(ws: Path, path: str, contract, contracts_all: dict, model_plan: str = "",
                  evidence: str = "", step_index: int = 0, task_id: str = "",
                  emit=None, attempts: int = 3, max_out: int = 0,
                  context_budget: int = 0, local_model: str = "",
                  only: list[str] | None = None, workingset: str = "",
                  diagnose: bool = True, tool_loop: bool = True,
                  tool_rounds: int = 3) -> dict:
    """骨架本地渲染，远端**只回函数体**（省输出的关键一步）。

    收益：远端输出从"整个文件（含 import/def/注解/docstring）"降到"几个函数体"；
    重试时只重发**失败的那几个**函数体（only 参数），输出量再降一个量级。

    max_out=0（默认）→ 按"要填的函数体多大"自动算输出配额。
    """
    from . import contextpack, delegate as _delegate, protocol, remote, tools
    from . import tokens as _tokens

    p = tools.safe_path(ws, path)
    existing = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
    funcs = list(getattr(contract, "funcs", None) or [])
    want_funcs = [f for f in funcs if not only or f.name in only]
    # include_bodies=True：把文件里**已有的函数实现**也放进骨架。
    # 作者被要求"只改这几个、其余保留"，就必须看得见其余函数现在长什么样，
    # 否则它会按自己的想象重写，把已验证的实现弄坏（实测 2026-11-09）。
    skeleton = render_skeleton(want_funcs, existing_src=existing, include_bodies=True)
    if not max_out:
        # 只回函数体：基准取"要填的骨架"（含需要保留的既有签名/注解），再留重构余量
        max_out = _tokens.output_budget(skeleton, factor=1.2, floor=REMOTE_OUT_FLOOR)
        if emit:
            emit("output_budget", {"path": path, "max_out": max_out,
                                   "est_tokens": _tokens.estimate_tokens(skeleton),
                                   "basis": "骨架", "only": list(only or [])})
    names = [f.name for f in want_funcs]
    ctx = contextpack.pack(ws, contracts_all, paths=[k for k in sorted(contracts_all) if k != path],
                           focus=(), budget_chars=0, evidence="")
    contract_txt = ""
    try:
        from . import contract as cm
        contract_txt = _tokens.cut(cm.describe(contract), REMOTE_TEXT_CHARS)
    except Exception:                                    # noqa: BLE001
        contract_txt = ""
    # 写前必读（与 author_module 对齐）：片段模式旧版**只给签名骨架**，不给文件全文，
    # 于是模型蒙着眼睛重写函数体，把已验证的实现改坏（实测 2026-11-09：
    # `include_bodies` 只补了同名函数体，模块级常量/相邻函数的调用方式仍然看不见）。
    # 文档 §三 写的是"写前必读全文"，这里是把它真正落到第二条写作路径上。
    def _read_file_block(retry: bool = False) -> str:
        """读磁盘上**此刻**的文件内容喂给远端（本地读盘，不花 token）。

        retry=True 是"失败后重贴"：上一轮判红时坏代码**已经落盘**，远端必须看到
        **它自己刚写的那一份**，否则只能凭记忆改 → 退化成整文件重写。
        实测 2026-11-09：日志里连续 `decision 跨函数/说不清位置，整文件重写一次`
        就是这么来的 —— 第 1 轮喂进去的 file_block 是**动手前的旧版**，之后再没更新过。
        """
        try:
            from . import readplan as _rp2
            _rplan2 = _rp2.plan("edit_existing" if existing.strip() else "new_module",
                                target=path)
            _got2 = _rp2.collect(ws, _rplan2, budget_chars=REMOTE_CTX_CHARS)
            if not _got2.get("text"):
                return ""
            head = ("【上一版（刚被判红）的真实内容 —— **这就是写下去被判不合格的那一版**；"
                    "请只改必要之处，其余逐字保留】"
                    if retry else
                    "【现有文件当前真实内容（**必须读过再改**；"
                    "不要凭想象重写，保留没让你改的部分）】")
            if emit:
                emit("read_gate", {"path": path, "chars": len(existing),
                                   "read_ok": bool(_got2.get("full")),
                                   "mode": ("retry_refresh" if retry else
                                            ("full" if _got2.get("full") else "head_tail")),
                                   "tiers": _rplan2.get("tiers")})
            return head + "\n" + _got2["text"]
        except Exception as _e:                             # noqa: BLE001
            if emit:
                emit("notice", {"message": f"read_plan 跳过：{type(_e).__name__}"})
            return ""

    _file_block = _read_file_block()

    last = evidence
    issues: list[dict] = []
    _diagnosed = [False]
    _failed_src: list = [None]        # 语法检查失败时那份**没写盘**的拼接代码

    def _maybe_diagnose(reason: str) -> str:
        """每片至多一次：判据失败或**输出解析失败**时让远端带只读工具自诊断。

        实测 2026-11-09 10:12：诊断只接在 verdict 失败路径，真实夜班
        `order_failed '(' was never closed` 走的是解析失败 → continue → 直接返回，
        diagnose_done=0，远端永远拿不到「为什么」。改为两条出口共用这一处；
        `_diagnosed` 保证不因多次失败重复花钱。开关 diagnose=False 可整体回退。
        """
        if not diagnose or _diagnosed[0] or not str(reason or "").strip():
            return reason
        _diagnosed[0] = True
        _payload = str(reason)
        # 「打开第 303 行看」的前提：那份代码得看得见。
        # _compile_ok(code) 失败后直接 continue，**没有 write_text** —— 出错的是内存里
        # 拼接后的版本，磁盘上还是旧文件。若只把行号丢给远端，它 read_file(303)
        # 读到的是另一份代码，反而会得出错误结论。所以把带行号的上下文原样附上，
        # 并明确说清「磁盘是旧版，别按行号去读」。
        _m = re.search(r"第\s*(\d+)\s*行", _payload)
        _src = _failed_src[0]
        # 只认 author_bodies 解析失败的专属措辞「解析不了」：判据反馈里也可能
        # 出现「语法错误」字样，那时 _failed_src 是上一次遗留的，不该误附。
        if _m and _src and "解析不了" in _payload:
            try:
                n = int(_m.group(1))
                lines = str(_src).splitlines()
                if n > len(lines):
                    # 版本错位（行号来自另一份代码）也要如实说，不能静默丢证据
                    lo, hi = max(1, len(lines) - 10), len(lines)
                    note = (f"【注意：报告行号 {n} 超出拼接版本行数 {len(lines)}，"
                            f"可能版本错位；下面给出拼接版本尾部】\n")
                else:
                    lo, hi = max(1, n - 5), min(len(lines), n + 5)
                    note = ""
                ctx = "\n".join(f"  {i}| {lines[i - 1]}"
                                for i in range(lo, hi + 1) if 0 < i <= len(lines))
                if ctx:
                    _payload += ("\n\n【出错代码上下文（内存里拼接后、尚未写盘的版本；"
                                 "磁盘上的文件是旧版，请勿按行号 read_file 读磁盘，"
                                 "直接看下面带行号的片段）】\n" + note + ctx)
            except (ValueError, IndexError, TypeError):
                pass
        from . import remotetools as _rtl
        _diag = _rtl.diagnose_fail(ws, path, _payload, emit=emit)
        if not _diag:
            return reason
        return str(reason) + "\n\n【远端自诊断（只读查证，未改代码）】\n" + _diag

    _need_refresh = [False]      # 上一轮写了盘且被判红 → 下一轮重新读盘
    _prev_ev = [None]            # 上一轮的失败证据（用来判断"写不出来"）
    _stuck = [False]             # 同一错误重复出现 → 换路，别原样重试
    _grew = [0]                  # 输出配额放大次数（顶到上限 → 放大重试，不是能力问题）
    _tool_fin = ""                # 2026-11-09：工具循环的 finish_reason（截断判定硬信号）
    for attempt in range(1, max(1, attempts) + 1):
        if attempt > 1 and _need_refresh[0]:
            # 失败后引擎手里的"文件现状"必须更新：`existing` 是合并基线，
            # `skeleton` 是给远端看的骨架，`_file_block` 是喂给远端的全文。
            # 三样都停在动手前的旧版 → 远端看不到自己刚写坏的那份 → 只能整文件重写，
            # 而且合并基线也是旧的 → 上一轮已经写对的部分被丢掉重来。
            existing = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
            skeleton = render_skeleton(want_funcs, existing_src=existing, include_bodies=True)
            _file_block = _read_file_block(retry=True) or _file_block
            _need_refresh[0] = False
        # 输出格式示例用**本次真实的函数名**：原来写死的是别的项目的函数名
        # （create_topic_draft），远端照抄 → 名字不在骨架里 → 被过滤掉 →
        # 报"契约函数缺失：<函数名>（文件里根本没有这些函数）"（真机实测 2026-11-09）。
        _ex_names = [n for n in (only or names)][:3] or names[:3]
        _format_example = "\n".join(
            f"# ===== {n} =====\n# 在这里写 {n} 的函数体（顶格，不要 def 行）"
            for n in _ex_names) or "# ===== 目标函数名 =====\n..."
        prompt = BODIES_PROMPT.format(
            format_example=_format_example,
            file_block=_file_block,
            protocol=protocol.render("delegate"),
            limits=_quality_limits_text(ws),
            skeleton_block=f"【骨架（已由引擎渲染好，你只补函数体）】\n```python\n{skeleton}\n```",
            contract_block=f"【契约】\n{contract_txt}",
            plan_block=(f"【实现方案】\n{_tokens.cut(model_plan.strip(), REMOTE_TEXT_CHARS)}" if model_plan else ""
                        ) + (f"\n{workingset}" if workingset else ""),
            # 判据反馈是远端**唯一的失败真相**：不掐（只有 provider 上下文兜底）。
            evidence_block=(f"【上一版被判红，必须改掉】\n{_tokens.cut(last, REMOTE_TEXT_CHARS)}"
                            if attempt > 1 else ""))
        if attempt > 1 and only:
            prompt += ("\n\n【这次只重写这几个函数体】" + "、".join(only))
        if attempt > 1 and _stuck[0]:
            # 同一错误重复 → 明确换路：先查证，再改；别再凭记忆重写一遍
            prompt += ("\n\n【上一版的失败和这次**完全相同**，说明原样重写没用。"
                       "请先查证再动手：\n"
                       "  1. 用 outline / imports / check_import / contract_for / read_file "
                       "看清这个文件的真实结构、依赖和契约要求；\n"
                       "  2. 判断它到底是**判据与实现矛盾**、**缺上下文**还是**真的不会写**；\n"
                       "  3. 只改上面点名的函数，别重写整个文件。")
        # §20.4 落点 3：契约声明了产物文件 → 提示词带上清单与交回格式
        _arts_b = list(getattr(contract, "artifacts", None) or [])
        if _arts_b:
            try:
                from . import contract as _cm_a
                prompt += "\n\n" + _cm_a.artifacts_prompt(_arts_b)
            except Exception:                            # noqa: BLE001
                pass
        # §20.6 C 档：契约里还声明了工程产物（迁移/锁文件/compose/健康检查/
        # 可观测性）时，提示词必须带上"交回格式与判据要求"。
        # 只注入段声明而不告诉远端怎么交文件 → 它看到清单也不会产出，
        # 判据永远红、系统还以为是"模型写不出来"（与 §20.1 证据 2 同病根）。
        try:
            from . import enterprise as _ent_a
            _eng_specs = _ent_a.parse_sections(str(contract_txt or ""))
            _eng_hint = _ent_a.sections_prompt(_eng_specs)
            if _eng_hint:
                prompt += "\n\n" + _eng_hint
        except Exception:                                # noqa: BLE001
            pass
        if ctx:
            prompt += "\n\n【其它模块的签名参考（只读，不要改）】\n" + _tokens.cut(ctx, REMOTE_TEXT_CHARS)
        # json_mode=False：代码走 ```python 围栏（原生形态），不再塞进 JSON 字符串。
        # 实测 2026-11-09：JSON 里的多行括号反复 `'(' was never closed`，同样模型、
        # 同样契约，只换输出协议。
        # 远端**带只读工具**写码：先看真实代码/契约/依赖，再交函数体。
        # 这条路之前是**零工具**的：远端只能凭注入的片段猜文件里长什么样 ->
        # "用了没 import 的名字""字段名猜错""重写已有函数"这类错反复出现。
        #
        # **现状（2026-11-24 核对）**：已经接上了 —— 见下面 if tool_loop 分支里
        # `_lp = _rtl.ToolLoop(...)` + `_lr = _lp.run(prompt)`，工具调用会被真的
        # 解析/执行/回灌。原先那句"从不解析/执行/回灌"是**修复前**的描述，
        # 留着只说明为什么要有这段代码；判断现状请直接看下面的调用。
        _ans = ""
        _usage: dict = {}
        _remote_failed = [False]   # 远端调用本身失败 → 不再补打一次（省一次花费）
        _remote_err = [""]        # 远端失败的**原始原因**（不丢信息：401/402/网络/限流要分得清）
        r: dict = {}          # 统一形状：工具路径没有远端响应对象，先给空壳
        if tool_loop:
            try:
                from . import remotetools as _rtl

                def _fc(_instr: str, messages: list, _tools=None) -> dict:
                    """远端调用：带**原生 function calling**（官方 tools 通路）。"""
                    nonlocal _usage
                    rr = remote.complete(_instr, task_id=task_id or "bodies",
                                        node_id=f"bodies:{path}", workspace=str(ws),
                                        emit=emit, step_index=step_index,
                                        json_mode=False, max_out=max_out,
                                        messages=messages, tools=_tools)
                    if isinstance(rr, dict):
                        _usage = rr.get("usage") or {}
                        if not rr.get("ok"):
                            _remote_failed[0] = True      # 远端本身失败 → 别再打一次
                            _remote_err[0] = str(rr.get("error") or "")[:200]
                        return {"answer": rr.get("answer") or "",
                                "tool_calls": list(rr.get("tool_calls") or []),
                                "message": rr.get("message") or {},
                                # 2026-11-09：finish_reason 必须透传（截断判定的硬信号）
                                "finish_reason": str(rr.get("finish_reason") or ""),
                                # 2026-11-10：error 也要透传 —— 要靠它判"这个模型不支持
                                # tools"（那就换通路，不是当成能力不足）
                                "error": str(rr.get("error") or "")[:300]}
                    return {"answer": "", "tool_calls": [], "message": {},
                            "finish_reason": "", "error": ""}

                from . import capability as _cap0
                _mode0, _model0, _compat0 = _cap0.current_tool_mode()
                _lp = _rtl.ToolLoop(ws, max_rounds=tool_rounds, emit=emit, complete_fn=_fc,
                                    max_out=max_out, tool_mode=_mode0,
                                    model=_model0, compat=_compat0)
                _lr = _lp.run(prompt)
                _ans = str(_lr.get("answer") or "")
                if emit:
                    emit("author_tools", {"path": path, "rounds": _lr.get("rounds"),
                                          "tools": len(_lr.get("tool_calls") or []),
                                          "denied": len(_lr.get("denied") or [])})
                _tool_fin = str(_lr.get("finish_reason") or "")
                r = {"usage": _usage, "by": "", "answer": _ans,
                     # 2026-11-09：工具循环的 finish_reason 也要传下去（截断判定的硬信号）
                     "finish_reason": _tool_fin}
            except Exception as exc:                        # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"写码工具循环跳过：{type(exc).__name__}"})
                _ans = ""
        if not _ans and not _remote_failed[0]:
            r = remote.complete(prompt, task_id=task_id or "bodies", node_id=f"bodies:{path}",
                                workspace=str(ws), emit=emit, step_index=step_index,
                                json_mode=False, max_out=max_out)
            if not r.get("ok"):
                # 2026-11-09：断网要和"远端答错"分开标记。
                # 断网不是模型能力问题 —— 记成"模型不会写"就会误导后续决策
                # （换模型/加轮次这类无效动作）。network_unreachable=True 时，
                # 上层知道这次失败与代码质量无关，可以等网络恢复再来。
                _err = str(r.get("error") or "")
                _net = remote.is_network_error(_err)
                return {"ok": False,
                        "reason": (f"远端网络不可达（等网络恢复后可重试）：{_err}" if _net
                                   else f"远端调用失败：{_err}")[:200],
                        "network_unreachable": _net,
                        "attempt": attempt, "system_issues": issues}
            _ans = r.get("answer") or ""
            _usage = r.get("usage") or {}
        elif _remote_failed[0]:
            # 2026-11-09：把远端的**原始原因**带出去。
            # 原来只写"远端调用失败（工具循环已试过）" —— 401 key 失效 / 402 欠费 /
            # 网络不通 / 被限流 全被抹平成同一句话，人和报告都看不出该做什么。
            _why = _remote_err[0] or "（远端没给出原因）"
            # 2026-11-10 修复：`_remote_mod` 从未导入 → 远端调用一失败就 NameError
            # 崩溃，把真实错误（401/402/限流）掩盖成崩溃现场（check_budget 实锤）。
            from . import remote as _remote_mod
            return {"ok": False,
                    "reason": f"远端调用失败：{_why}"[:300],
                    "remote_error": _why,
                    "remote_fatal": bool(_remote_mod.is_fatal_remote_error(_why)),
                    "attempt": attempt, "system_issues": issues}
        _u = r.get("usage") or {}
        _used = max(0, int(_u.get("completion_tokens") or 0)
                    - int(_u.get("reasoning_tokens") or 0))          # 只看正文
        # 2026-11-09 修：截断判定改用**硬信号** finish_reason=='length'。
        # 原来用 `_used >= max_out*0.98` 去猜 —— 而 max_out 曾被按任务量算成 3000/1200，
        # 于是远端正常写 3000 token 正文就被误判成"被配额截断" → 放大重试 → 白烧一轮钱
        # → 下一轮同一份失败证据 → heal_stop。实测本轮真实夜班：
        # `output_budget max_out=3000` 紧接 `heal_stop（失败证据与上一轮完全相同）`。
        # 现在 max_out 恒为模型物理上限（tokens.output_budget 不再按任务量掐），
        # 数字猜测这条路已经没意义；finish_reason 是远端自己说的，够硬。
        _fin = str(r.get("finish_reason") or "")
        _truncated = (_fin == "length") or (_used >= max(1, int(max_out * 0.98)))
        if _truncated and _grew[0] < 2:
            _grew[0] += 1
            _old = max_out
            max_out = min(384000, max(max_out * 4, REMOTE_OUT_FLOOR))
            if emit:
                emit("output_budget", {"path": path, "max_out": max_out, "grew_from": _old,
                                       "finish_reason": _fin, "content_tokens": _used,
                                       "why": "上一版输出顶到配额上限，判为截断而非能力问题"})
            last = (f"上一版输出被截断（正文 {_used} token，finish_reason={_fin or 'n/a'}）。"
                    "请**完整**输出每个函数体，不要省略、不要用「...」代替。")
            continue
        _ans = r.get("answer") or ""
        data = _parse_json(_ans) or {}
        bodies, helpers = parse_fenced_bodies(_ans)
        if not bodies:                       # 回退：老格式（模型仍给了 JSON）
            bodies = (data or {}).get("bodies") or {}
        if not helpers:
            helpers = (data or {}).get("helpers") or {}
        issues += [x for x in ((data or {}).get("system_issues") or []) if isinstance(x, dict)]
        delegated = (data or {}).get("delegate") or []
        if not isinstance(bodies, dict) or not bodies:
            last = '没有给出 {"bodies":{...}}'
            continue
        # 只接受骨架里有的函数名（防止它自造名字）
        bodies = {k: v for k, v in bodies.items() if k in names}
        base = existing if only else ""
        # **关键**：只要文件已经存在，就必须"在现有文件上只替换本轮目标函数"。
        # 踩过的坑：only 为空（=本轮要重写全部）时会走 render_skeleton（所有函数都是桩），
        # 于是模型**没返回的那些函数**就被擦成 NotImplementedError → 已验证的函数被弄坏 →
        # 回归回滚 → 无限重写。所以这里永远合并到现有文件，只有文件本来不存在才用全新骨架。
        targets = only or names
        if existing.strip():
            # 局部重写：只替换 targets；模型少给的函数体**还原成原实现**（绝不擦成桩）
            code, missing = rebuild_partial(existing, funcs, targets, bodies, helpers)
        else:
            code, missing = apply_bodies(render_skeleton(want_funcs, existing_src=existing),
                                         bodies, helpers, stub_missing=True)
        ok_syntax, why = _compile_ok(code)
        if not ok_syntax:
            _failed_src[0] = code                # 这份没写盘，诊断时要把上下文带给远端
            # 先定位是**哪一个函数体**写坏的（窄证据）；下一轮只让它重写那一个
            blame = _blame_body(want_funcs, bodies)
            last = f"填进骨架后解析不了：{why}"
            if blame:
                last += "（问题函数：" + "；".join(blame[:3]) + "）"
                only = [b.split(":")[0] for b in blame]
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")
        code = _fix_imports_after_write(ws, p, emit, "bodies")
        # §20.4 落点 3：把回复里的产物文件块写到声明的路径（模板/静态资源）。
        # 缺了不在这里拦 —— 判据（artifact_exists）会红，反馈自动带着"文件不存在"。
        if getattr(contract, "artifacts", None):
            write_artifacts(ws, _ans, contract.artifacts, emit, "bodies")
        if delegated and local_model:
            try:
                _delegate.run(ws, path, delegated, contract, local_model, emit=emit)
            except Exception:                            # noqa: BLE001
                pass
        verdict = _verify(ws, {**contracts_all, path: contract} if contract is not None
                          else contracts_all, scope=[path])
        if verdict["ok"]:
            return {"ok": True, "applied": [path], "attempt": attempt, "bodies": sorted(bodies),
                    "by": r.get("by") or "", "system_issues": issues,
                    "usage": r.get("usage") or {}}
        last = _maybe_diagnose(verdict["feedback"])
        # 刚落盘的那一版就是被判红的版本 → 下一轮必须重新读盘（见循环顶部）
        _need_refresh[0] = True
        # **写不出来**的识别：同一份失败证据连续出现 = 远端在同一个地方反复撞墙
        # （实测 2026-11-09：app/services/topics.py 连续 4 轮 bodies_feedback）。
        # 这种情况下**不要再原样重试**：换路 —— 明确要求它先用工具查证真实代码/契约，
        # 并且只改被点名的函数。机制问题（判据矛盾/上下文不足）不该被当成能力问题反复烧钱。
        _ev_key = (last or "").strip()[:400]
        if _ev_key and _ev_key == _prev_ev[0]:
            _stuck[0] = True
            if emit:
                emit("stuck_same_evidence", {"path": path, "attempt": attempt,
                                              "evidence": _ev_key[:160]})
        _prev_ev[0] = _ev_key
        failed = [n for n in names if n in (last or "")]
        # 粒度阶梯（按证据自动选最细的一档）：
        #   证据定位到行 → 行级补丁（只改那几行）
        #   定位到函数   → 下一轮只重发那几个函数体
        #   都不行       → 下一轮重发全部函数体
        try:
            from . import patch as _patch
            if _patch.is_line_localized(last):
                fix = _patch.request_line_fix(
                    ws, path, last, step_index=step_index, task_id=task_id, emit=emit,
                    verify=lambda: _verify(ws, {**contracts_all, path: contract}
                                          if contract is not None else contracts_all,
                                          scope=[path]))
                if fix.get("ok"):
                    return {"ok": True, "applied": [path], "attempt": attempt,
                            "bodies": sorted(bodies), "by": fix.get("by") or "",
                            "line_patch": fix.get("applied"), "system_issues": issues,
                            "usage": fix.get("usage") or {}}
                if emit:
                    emit("patch_rejected", {"path": path,
                                            "reason": str(fix.get("reason"))[:200]})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"行级修复跳过：{type(exc).__name__}"})
        only = failed or only
        if emit:
            emit("bodies_feedback", {"path": path, "attempt": attempt,
                                     "only": only, "evidence": (last or "")[:300]})
    # 保持下面这行 reason 的原样（台账按代码片段指纹登记，改包裹方式会让它失配）。
    _ret = {"ok": False, "reason": "只回函数体两次都未通过：" + (last or "")[:300],
            "attempt": attempts, "system_issues": issues}
    _ret["reason"] = _maybe_diagnose(_ret["reason"])
    return _ret


def _sig_last_line(lines: list[str], start: int, end: int) -> int:
    """多行签名的最后一行（括号深度归零、且以冒号收尾）。

    实测 2026-11-09：`app/models.py:303 create_topic_draft_record(` 的签名跨 7 行。
    只取 `def` 那一条**物理行**当桩头，就把签名截断了 → 括号永不闭合 →
    夜班连续两轮报 `第 303 行语法错误：'(' was never closed`，而 `_blame_body`
    一个函数都点不出来（每个函数体单独都合法）→ 判成"跨函数/说不清位置"→
    整文件重写 → 同一处再犯。全仓 52 个含函数模块里 11 个都踩这个。
    """
    depth = 0
    for i in range(start, end):
        seg = lines[i].split("#", 1)[0]          # 行尾注释里的括号不参与计数
        depth += sum(seg.count(c) for c in "([{") - sum(seg.count(c) for c in ")]}")
        if depth <= 0 and seg.rstrip().endswith(":"):
            return i
    return start


def _doc_lines(lines: list[str], i: int, end: int) -> list[str]:
    """从第 i 行起若是 docstring，返回**完整**那一段（可能跨多行）。

    原来只留第一条以三引号开头的行：开引号留下、闭引号被丢掉，整份文件从那里起
    全被吞进字符串（unterminated triple-quoted string literal）。
    """
    if i >= end:
        return []
    head = lines[i].strip()
    for q in ('"""', "'''"):
        if not head.startswith(q):
            continue
        if head.count(q) >= 2:                   # 单行 docstring
            return [lines[i]]
        k = i + 1
        while k < end:
            if q in lines[k]:
                return lines[i:k + 1]
            k += 1
        return lines[i:end]                      # 本来就没闭合：原样带上，交给语法检查报
    return []


def _merge_skeleton(existing: str, funcs, only: list[str]) -> str:
    """在"已有实现"的文件上，只把 only 里的函数换回占位体，其余保持原样。"""
    import re as _re
    if not existing.strip():
        return render_skeleton([f for f in funcs if f.name in only] or funcs)
    lines = existing.splitlines()
    idxs = [i for i, ln in enumerate(lines) if ln.startswith("def ")]
    blocks = []
    for k, start in enumerate(idxs):
        end = idxs[k + 1] if k + 1 < len(idxs) else len(lines)
        m = _re.match(r"def\s+(\w+)", lines[start])
        if m:
            blocks.append((m.group(1), start, end))
    out: list[str] = []
    cursor = 0
    for name, start, end in blocks:
        out.extend(lines[cursor:start])
        if name in only:
            sig_end = _sig_last_line(lines, start, end)
            out.extend(lines[start:sig_end + 1])         # 签名可能跨多行，必须整段保留
            out.extend(_doc_lines(lines, sig_end + 1, end))
            out.append("    raise NotImplementedError")
        else:
            out.extend(lines[start:end])
        cursor = end
    out.extend(lines[cursor:])
    return "\n".join(out).rstrip() + "\n"


def _ensure_packages(ws: Path, rel: str) -> list[str]:
    """给嵌套模块补齐包的 `__init__.py`，返回新建的文件。

    为什么要补：`cal/core/numeric.py` 这种布局，没有 `__init__.py` 就不是包，
    `import cal.core.numeric` 会失败（判据已改成"按路径加载"兜住，但真实项目
    仍然需要包结构才能被正常 import / 被 pytest 收集）。这一步是确定性的，不花 token。
    """
    made: list[str] = []
    cur = Path(ws)
    for part in Path(rel).parts[:-1]:
        cur = cur / part
        init = cur / "__init__.py"
        if init.exists():
            continue
        try:
            cur.mkdir(parents=True, exist_ok=True)
            init.write_text('"""包。"""\n', encoding="utf-8")
            made.append(str(init.relative_to(ws)).replace("\\", "/"))
        except OSError:
            pass
    return made


def author_module(ws: Path, path: str, contract, contracts_all: dict, model_plan: str = "",
                  evidence: str = "", step_index: int = 0, task_id: str = "",
                  emit=None, attempts: int = 3, max_out: int = 0,
                  context_budget: int = 0, local_model: str = "",
                  workingset: str = "", verify_fn=None,
                  stop_on_repeat: bool = True,
                  deadline: float | None = None,
                  tool_loop: bool = True, tool_rounds: int = 3) -> dict:
    """让远端把**整个文件**写出来：提炼上下文 → 一次生成 → 本地验收 → 窄证据重试。

    max_out=0（默认）→ 按"要写的文件多大"自动算输出配额（见 tokens.output_budget）。
    attempts 是"自愈轮数"（造物模式默认 4，来自 policy.heal_attempts）；
    stop_on_repeat=True 时，若新一轮的失败证据与已用过的完全相同就不发这次调用。
    deadline 是本片的时间截止点（unix 秒）：单轮 4 次自愈每次可能耗两分钟，
    只在外层 round 边界查时间会让 --slice-hours 形同虚设（实测 dev1106c）。
    """
    # 进程级栈：一次设定，后续所有 `_stack()`（不带 ws 的几十处调用）都用它。
    # 不设 = 恒落回 fastapi（Django 项目会补出 `router = APIRouter()` 之类）。
    try:
        from . import stack as _stk
        _stk.activate(ws)
    except Exception:                                    # noqa: BLE001
        pass
    from . import contextpack, protocol, remote, tools
    from . import tokens as _tokens

    p = tools.safe_path(ws, path)
    if not max_out:
        src = ""
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace") if p.exists() else ""
        except OSError:
            src = ""
        raw = str(getattr(contract, "raw", "") or "")
        # 基准取三者最大：现状源码 / 契约原文 / 现有骨架。
        # 为什么不能只看源码：首次实现时盘上是"桩骨架"（~220 token），而写成的模块要
        # ~700 token + JSON 包装 —— 按桩算出来的 1200 会把每一次输出都截断，
        # 实测 10/11 次调用恰好停在 1200（= 被配额砍断，不是模型不会写）。
        basis, want = "现状源码", src
        for label, cand in (("契约文本", raw),):
            if _tokens.estimate_tokens(cand) > _tokens.estimate_tokens(want):
                basis, want = label, cand
        stubby = not src.strip() or "raise NotImplementedError" in src
        # 2026-11-09 修：原来这里写 `floor=3000 if stubby else 1200` ——
        # **绕过了 REMOTE_OUT_FLOOR**。函数体路径早就改用 REMOTE_OUT_FLOOR(=4000)，
        # 整文件路径漏了，于是整文件写作又一次被掐到 1200/3000。
        # 实测证据（本轮真实夜班）：`output_budget max_out=3000`，远端写不完 →
        # 判据红 → 下一轮同一失败证据 → `heal_stop（无新信息）` → 白烧一轮钱。
        # 这正是"掐小 = 把机制问题记成模型能力问题"，按铁律必须放开。
        max_out = _tokens.output_budget(want, factor=2.0 if stubby else 1.5,
                                        floor=REMOTE_OUT_FLOOR)
        if emit:
            emit("output_budget", {"path": path, "max_out": max_out,
                                   "est_tokens": _tokens.estimate_tokens(want),
                                   "basis": basis, "from_stub": stubby})
    else:
        stubby = False
    focus = tuple(f.name for f in (getattr(contract, "funcs", None) or []))
    from . import layout as _layout
    ctx = contextpack.pack(ws, contracts_all, paths=sorted(contracts_all),
                           focus=focus, budget_chars=context_budget, evidence=evidence)
    # 把提炼结果切成稳定/易变两块：契约+签名表稳定（可命中缓存），焦点源码+证据易变
    head, _, tail = ctx.partition("# 焦点符号完整源码")
    # 项目宪法：目录/命名/分层/错误/测试/铁律 —— 最稳定的前缀（吃缓存），放最前。
    try:
        from . import constitution as _const
        _const_prefix = _const.prefix(Path(ws))
        if _const_prefix:
            head = _const_prefix + "\n\n" + head
    except Exception:                            # noqa: BLE001
        pass
    # §13.4 第 5 步：把跨轮记忆前置进稳定前缀（命中缓存、让远端越用越强）
    try:
        from . import memory as _mem
        _mem_prefix = _mem.MemoryStore(Path(ws)).render_prefix()
        if _mem_prefix:
            head = _mem_prefix + "\n\n" + head
    except Exception:                            # noqa: BLE001
        pass
    chunks = [
        _layout.Chunk("a_stable_context", head, True),
        _layout.Chunk("z_focus_and_evidence",
                      ("# 焦点符号完整源码" + tail) if tail else "", False),
    ]
    ctx = _layout.render(chunks)
    if emit:
        emit("context_layout", _layout.audit(chunks))
    # §8.5 读取策略 + 写前必读门禁：**没给过全文，就不许整篇重写**。
    # 旧行为是"文件超过 12000 字符就静默不注入"，模型照样整篇重写 —— 等于蒙着眼睛改，
    # 改坏已验收函数是必然结果。现在读取档位由 `core/readplan.py` 决定，并且把
    # "看没看过全文"变成显式状态：超长只给头尾，同时**禁止**整篇重写（只收补丁）。
    from . import readplan as _rp
    READ_CHARS = REMOTE_CTX_CHARS
    _cur_txt = ""
    try:
        _cur = Path(ws) / path
        if _cur.is_file():
            _cur_txt = _cur.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _cur_txt = ""
    _rplan = _rp.plan("edit_existing" if _cur_txt.strip() else "new_module", target=path)
    _got = _rp.collect(ws, _rplan, budget_chars=READ_CHARS)
    if _got.get("text"):
        ctx += "\n\n" + _got["text"]
    read_ok = True if not _cur_txt.strip() else bool(_got.get("full"))
    if emit:
        emit("read_gate", {"path": path, "chars": len(_cur_txt), "read_ok": read_ok,
                           "mode": "full" if read_ok else "head_tail",
                           "tiers": _rplan["tiers"]})
    req_block = ("【实现方案（必须照它做）】\n" + _tokens.cut(model_plan.strip(), REMOTE_TEXT_CHARS)) if model_plan else ""
    if workingset:
        req_block = (req_block + "\n\n" + workingset).strip()
    prompt = AUTHOR_PROMPT.format(protocol=protocol.render("files"), context=ctx,
                                  requirements=req_block,
                                  limits=_quality_limits_text(ws))
    # §20.4 落点 3：契约声明了产物文件 → 提示词带上清单与交回格式
    _arts_m = list(getattr(contract, "artifacts", None) or [])
    if _arts_m:
        try:
            from . import contract as _cm_am
            prompt += "\n\n" + _cm_am.artifacts_prompt(_arts_m)
        except Exception:                                # noqa: BLE001
            pass
    # §13.4 第 3 步：开启工具循环时，把只读工具说明追加给远端（它可发 <tool> 调用）
    if tool_loop:
        try:
            from . import remotetools as _rtl
            # read_ok=True 表示目标文件**全文已注入**（read_gate 拿全了）——
            # 这时额外告诉它"别再调工具重读同一份内容"：实测每一轮工具调用
            # 都是一次带完整上下文的付费请求，白读一遍等于把调用量放大 3~4 倍。
            prompt = prompt + "\n\n" + _rtl.tool_loop_guide(context_complete=bool(read_ok))
        except Exception:                        # noqa: BLE001
            pass
    last = evidence
    issues: list[dict] = []
    _seen_evidence: set[str] = set()                # 已用过的失败证据（防止同证据反复刺激）
    grew = 0                                        # 因为"被截断"而放大配额的次数（最多 2 次）
    _tool_fin = ""                                # 2026-11-09：工具循环的 finish_reason（截断判定硬信号）
    # 配额放大跨调用要**记住**：以前每次调 author_module 都从 3000 重算，于是同一个模块
    # 每轮都先被截断一次、白烧一次调用（实测 search-index 连输三次）。放大值通过返回值
    # max_out 交给调用方保存（见 company._author_round 的 rec["max_out"]）。
    timed_out = False
    for attempt in range(1, max(1, attempts) + 1):
        # 时间闸门必须落在自愈循环**内部**：只在外层 round 边界查时间的话，
        # 单轮 4 次自愈 × 每次约两分钟（委托本地 7B）就能把 3 分钟的限时实验
        # 拖到十分钟以上（实测 dev1106c）。
        if deadline:
            import time as _time
            if _time.time() >= deadline:
                timed_out = True
                issues.append({"where": "author", "issue": "本片时间预算耗尽，停止自愈",
                               "suggest": "下一片/下一轮再继续，不要在预算外反复刺激同一份证据"})
                if emit:
                    emit("heal_stop", {"path": path, "attempt": attempt,
                                       "why": "本片时间预算耗尽"})
                break
        # 同一个失败证据再问一遍只会白烧钱（远端看到完全相同的输入，答案大概率也一样）。
        # 自愈轮数放开后这条更关键：它把"反复刺激"限制在"每轮都有新信息"的前提下。
        _ev = (last or "").strip()
        # 2026-11-09（用户反馈：「突然断网然后网络恢复的情况下远端调用没用恢复」）：
        # 断网时**每一轮的失败证据都一样**（都是"网络不可达"）→ 撞上下面这条
        # "同一份证据再问一遍只会白烧钱"的早停 → 第二轮就 heal_stop，
        # **网络恢复了也没机会再跑**。
        # 现在：网络类失败**不走早停**（那不是"没有新信息"，是"根本没能问"），
        # 并且把它标成 network_unreachable，让上层知道这次失败与代码质量无关。
        _ev_is_net = remote.is_network_error(_ev)
        if (attempt > 1 and stop_on_repeat and _ev and _ev in _seen_evidence
                and not _ev_is_net):
            issues.append({"where": "author", "issue": f"连续两轮失败证据完全相同，提前停止自愈（{path}）",
                           "suggest": "让判据给出更具体的差异（行号/期望值），否则远端没有新信息可用"})
            if emit:
                emit("heal_stop", {"path": path, "attempt": attempt,
                                   "why": "失败证据与上一轮完全相同（无新信息）"})
            break
        _seen_evidence.add(_ev)
        body = prompt if attempt == 1 else (
            prompt + "\n\n【上一版被机械判据判红，必须改掉】\n" + _tokens.cut(last or "", REMOTE_TEXT_CHARS))
        # 整文件路径**真的跑工具循环**（原生 function calling）：原来只是把工具说明
        # **现状（2026-11-24 核对）**：已接上，工具会被真的解析/执行/回灌
    # （见下面 if tool_loop 分支的 _lp.run）。原注释描述的是修复前的状态。
        # 夜班「整文件模式两版都未通过」29 次就在这条路上（实测 2026-11-09）。
        _ans = ""
        _usage: dict = {}
        _remote_failed = [False]
        _remote_err = [""]        # 远端失败的**原始原因**（不丢信息）
        if tool_loop:
            try:
                from . import remotetools as _rtl

                def _fc(_instr: str, messages: list, _tools=None) -> dict:
                    nonlocal _usage
                    rr = remote.complete(_instr, task_id=task_id or "author",
                                        node_id=f"author:{path}", workspace=str(ws),
                                        emit=emit, step_index=step_index,
                                        json_mode=False, max_out=max_out,
                                        messages=messages, tools=_tools)
                    if isinstance(rr, dict):
                        _usage = rr.get("usage") or {}
                        if not rr.get("ok"):
                            _remote_failed[0] = True
                            _remote_err[0] = str(rr.get("error") or "")[:200]
                        return {"answer": rr.get("answer") or "",
                                "tool_calls": list(rr.get("tool_calls") or []),
                                "message": rr.get("message") or {},
                                # 2026-11-09：finish_reason 必须透传（截断判定的硬信号）
                                "finish_reason": str(rr.get("finish_reason") or ""),
                                # 2026-11-10：error 也要透传（判"模型不支持 tools"→换通路）
                                "error": str(rr.get("error") or "")[:300]}
                    return {"answer": "", "tool_calls": [], "message": {},
                            "finish_reason": "", "error": ""}

                from . import capability as _cap1
                _mode1, _model1, _compat1 = _cap1.current_tool_mode()
                _lp = _rtl.ToolLoop(ws, max_rounds=tool_rounds, emit=emit, complete_fn=_fc,
                                    max_out=max_out, tool_mode=_mode1,
                                    model=_model1, compat=_compat1)
                _lr = _lp.run(body)
                _ans = str(_lr.get("answer") or "")
                _tool_fin = str(_lr.get("finish_reason") or "")
                if emit:
                    emit("author_tools", {"path": path, "rounds": _lr.get("rounds"),
                                          "tools": len(_lr.get("tool_calls") or []),
                                          "denied": len(_lr.get("denied") or [])})
            except Exception as exc:                        # noqa: BLE001
                if emit:
                    emit("notice", {"message": f"整文件工具循环跳过：{type(exc).__name__}"})
                _ans = ""
        if not _ans and not _remote_failed[0]:
            r = remote.complete(body, task_id=task_id or "author", node_id=f"author:{path}",
                                workspace=str(ws), emit=emit, step_index=step_index,
                                json_mode=False, max_out=max_out)
            if not r.get("ok"):
                _err = str(r.get("error") or "")
                _net = remote.is_network_error(_err)
                return {"ok": False,
                        "reason": (f"远端网络不可达（等网络恢复后可重试）：{_err}" if _net
                                   else f"远端调用失败：{_err}")[:200],
                        "network_unreachable": _net,
                        "by": "", "attempt": attempt, "system_issues": issues, "max_out": max_out}
            _ans = r.get("answer") or ""
            _usage = r.get("usage") or {}
        elif _remote_failed[0]:
            _why = _remote_err[0] or "（远端没给出原因）"
            # 2026-11-10：整文件路径也必须带 network_unreachable ——
            # 函数体路径早就有，缺了它 company 层就分不清"断网"和"远端答错"，
            # 于是网络一抖就把每个模块都判一次失败、整轮 0 产出。
            _net = remote.is_network_error(_why)
            return {"ok": False,
                    "reason": (f"远端网络不可达（等网络恢复后可重试）：{_why}" if _net
                               else f"远端调用失败：{_why}")[:300],
                    "network_unreachable": _net,
                    "remote_error": _why,
                    "remote_fatal": bool(remote.is_fatal_remote_error(_why)),
                    "by": "", "attempt": attempt, "system_issues": issues, "max_out": max_out}
        r = {"answer": _ans, "usage": _usage, "ok": True, "by": "",
             # 2026-11-09：工具循环的 finish_reason 也要传下去（截断判定的硬信号）
             "finish_reason": _tool_fin}
        # 被配额截断 ≠ 模型不会写：**正文**顶到 max_out 才说明配额太小，放大重试一次。
        # 实测教训（night-v2a）：10/11 次调用恰好停在 1200，全被判成"没给出 files"。
        #
        # 关键修正（2026-11-05 round-01a 第 7 次）：**必须扣掉思考 token 再比**。
        # completion_tokens 是"思考 + 正文"的总和，开着思考时它会天然超过 max_out：
        # 实测一条被判"被截断（17552 token 就顶到配额 12000）"的调用里，1.4 万是思考、
        # 正文只有三千多 —— 完全没被截断。误判的代价是把 max_out 放大 4 倍、白烧一次调用，
        # 并给模型一句错误的提示（它会以为要删注释重写）。
        _u = r.get("usage") or {}
        used_all = int(_u.get("completion_tokens") or 0)
        used_reason = int(_u.get("reasoning_tokens") or 0)
        used = max(0, used_all - used_reason)          # 只看正文
        # 2026-11-09 修：改用硬信号 finish_reason=='length'（见函数体路径的同一处说明）。
        # 数字猜测（used >= max_out*0.98）会把"正常写完"误判成"被截断"，
        # 代价是放大重试、白烧一次调用，还给模型一句错误的提示。
        _fin = str(r.get("finish_reason") or "")
        _truncated = (_fin == "length") or (used >= max(1, int(max_out * 0.98)))
        if _truncated and grew < 2:
            grew += 1
            old = max_out
            max_out = min(384000, max(max_out * 4, REMOTE_OUT_FLOOR))
            if emit:
                emit("output_budget", {"path": path, "max_out": max_out, "grew_from": old,
                                       "finish_reason": _fin, "content_tokens": used,
                                       "why": "上一版输出顶到配额上限，判为截断而非能力问题"})
            last = (f"上一版输出被截断（正文 {used} token，finish_reason={_fin or 'n/a'}）。"
                    "请只输出完整 JSON（不要解释、不要省略），必要时删掉非必要注释。")
            continue
        files, got_issues, delegated = _parse_reply(r.get("answer") or "")
        _whole = bool(files)          # 直接给 files = 整篇重写；补丁/片段合并不算
        issues += got_issues
        _patch_err = ""
        if not files:
            # ① 先试**补丁**（改已有文件的首选形态：只替换某一段，不重写整篇）
            _pl = _parse_patches(r.get("answer") or "")
            _wrong = [x for x in _pl if x.get("path") and x["path"] != path]
            if _wrong:
                # 补丁必须落在本轮的目标文件上：打错文件比不落盘更糟
                _patch_err = (f"patch 的 path 必须是 {path}（收到 {_wrong[0]['path']}）")
                _pl = [x for x in _pl if not x.get("path") or x["path"] == path]
            if _pl:
                try:
                    _base = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
                except OSError:
                    _base = ""
                _newc, _bad = _apply_patches(_base, _pl)
                if _base.strip() and not _bad and _newc != _base:
                    files = [{"path": path, "content": _newc}]
                    if emit:
                        emit("author_patched", {"path": path, "patches": len(_pl)})
                elif _bad:
                    _patch_err = ("有 %d 处 patch 的 find 在文件里找不到：%s。"
                                  "find 必须**逐字**复制当前文件里的旧代码（连缩进一起），"
                                  "或者改用整份文件（围栏）"
                                  % (len(_bad), "；".join(_bad[:2])))
                    if emit:
                        emit("author_patch_miss", {"path": path, "missing": _bad[:2]})
        if not files:
            # ② 围栏：**先判它是不是"只含新增函数"的增量**，是就合并，不是才当整份文件
            _fb = _code_fallback(r.get("answer") or "", path)
            if _fb.strip():
                try:
                    _base = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
                except OSError:
                    _base = ""
                _merged = _fragments_only(_fb, _base) if _base.strip() else None
                if _merged:
                    files = [{"path": path, "content": _merged}]
                    if emit:
                        emit("author_fragment", {"path": path, "chars": len(_fb),
                                                 "names": sorted(_toplevel_names(_fb))[:4]})
                else:
                    files = [{"path": path, "content": _fb}]
                    _whole = True
                    if emit:
                        emit("author_fenced", {"path": path, "chars": len(_fb)})
        if not files:
            last = _patch_err or '没有给出 {"files":[...]} 或 {"patches":[...]}'
            continue
        mine = [f for f in files if f["path"] == path] or files[:1]
        content = _strip_fence(mine[0]["content"])
        ok_syntax, why = _compile_ok(content)
        if not ok_syntax:
            last = f"生成的代码解析不了：{why}"
            continue
        # §8.5 硬门禁：没给过全文就不许整篇重写（只收补丁）
        if _whole and not read_ok:
            last = ("这个文件超过全文注入上限，本轮你只看到头尾 —— **不许整篇重写**。"
                    "请用 patches（find 逐字复制你确实看到的旧代码）改；"
                    "只改你看得见的那几段，看不到的部分不要动。")
            if emit:
                emit("read_gate_block", {"path": path, "chars": len(_cur_txt)})
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        # 写回前先确认"没把已实现的函数删掉"：删了就打回，不落盘。
        try:
            _old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        except OSError:
            _old = ""
        _gone = _deleted_symbols(_old, content)
        if _gone:
            # 打回重试是**最贵且最没用**的做法：模型下一轮大概率还是给一份"按契约写的
            # 干净文件"，同样缺这些函数 → 同一指纹重试到停机（实测 2026-11-07：da.py
            # 连续多轮 author_fenced → author_regress，一个字都没往前挪）。
            # 缺哪些函数是**确定性信息**，引擎自己就能补：按旧实现原样接回新文件。
            _merged_gone = _restore_symbols(_old, content, _gone)
            if _merged_gone:
                content = _merged_gone
                if emit:
                    emit("author_gone_restored", {"path": path, "funcs": _gone[:6],
                                                  "chars": len(content)})
            else:
                last = ("你把已实现的函数删掉了：%s。整文件重写不许删已有函数 —— "
                        "保留它们，只在里面补新功能。" % "、".join(_gone[:6]))
                if emit:
                    emit("author_regress", {"path": path, "deleted": _gone[:6]})
                continue
        p.write_text(content, encoding="utf-8")
        _ensure_packages(ws, path)          # 嵌套模块要有包结构才 import 得动
        content = _fix_imports_after_write(ws, p, emit, "module")
        # 作者用了 `@router.xxx` 却没写 `router = APIRouter()` → 补一行样板。
        # **必须在落盘后立刻补**：不然接下来的 import_smoke 会报
        # `NameError: name 'router' is not defined`，远端为这一行样板返工好几轮
        # （实测 2026-11-10：attempt=1/2/3 全在改这件事，最后整轮接线还回滚了 32 个文件）。
        try:
            from . import auto as _auto_mod
            _src2 = p.read_text(encoding="utf-8-sig", errors="replace")
            _src2, _rfix = _auto_mod._ensure_router_decl(_src2)
            if _rfix:
                p.write_text(_src2, encoding="utf-8")
                if emit:
                    emit("author_router_decl", {
                        "path": path,
                        "note": "作者用了 @router 但没定义 router → 引擎补 router = APIRouter()"})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"router 声明修补跳过：{type(exc).__name__}"})
        # §20.4 落点 3：把回复里的产物文件块写到声明的路径（模板/静态资源）。
        # 缺了不在这里拦 —— 判据（artifact_exists）会红，反馈自动带着"文件不存在"。
        if getattr(contract, "artifacts", None):
            write_artifacts(ws, r.get("answer") or "", contract.artifacts, emit, "module")
        # 委托：远端把机械输出交给本地模型（省远端输出）。委托失败要打回给远端。
        deleg_report = {"done": [], "failed": []}
        if delegated and local_model:
            try:
                from . import delegate as _delegate
                deleg_report = _delegate.run(ws, path, delegated, contract, local_model,
                                             emit=emit)
            except Exception as exc:                     # noqa: BLE001
                deleg_report = {"done": [], "failed": [
                    {"target": str(delegated[:1]), "reason": f"{type(exc).__name__}"}]}
        # 委托失败必须**回传给远端**（以前只记在返回值里，远端下一轮根本不知道，
        # 于是它以为那块机械活已经有人做了 —— 结果那个函数一直是错的）。
        if deleg_report.get("failed"):
            _dl = "\n".join(f"- {x.get('target')}: {x.get('reason')}"
                            for x in deleg_report["failed"][:3])
            last = ((last + "\n") if (last or "").strip() else "") + \
                "【委托失败：这些机械活本地没做出来，请你自己写】\n" + _dl
            if emit:
                emit("delegate_bounced", {"path": path, "failed": len(deleg_report["failed"])})
        # verify_fn 是**语言切换**的钩子：Python 走契约判据（默认），
        # 别的语言走它自己的编译器/运行时（core/lang.py）。两者都返回 {ok, feedback}。
        verdict = (verify_fn(ws, path) if verify_fn
                   else _verify(ws, {**contracts_all, path: contract} if contract is not None
                                else contracts_all, scope=[path]))
        if verdict["ok"]:
            return {"ok": True, "applied": [path], "files": [f["path"] for f in files],
                    "by": r.get("by") or "", "attempt": attempt, "reason": "",
                    "usage": r.get("usage") or {}, "system_issues": issues,
                    "delegate": deleg_report, "max_out": max_out}
        bounce = ""
        if deleg_report["failed"]:
            bounce = ("\n【委托给本地模型的这几项没通过，请你自己写】\n" + "\n".join(
                f"- {x['target']}：{x['reason'][:120]}" for x in deleg_report["failed"]))
        last = verdict["feedback"] + bounce
        if emit:
            emit("author_feedback", {"path": path, "attempt": attempt,
                                     "evidence": (last or "")[:400]})
    return {"ok": False, "reason": ("本片时间预算耗尽，停止自愈" if timed_out
                                    else "整文件模式两版都未通过：" + (last or "")[:300]),
            "by": "", "attempt": attempts, "feedback": last, "system_issues": issues,
            "max_out": max_out, "timed_out": timed_out}


def author_modules(ws: Path, paths: list[str], contracts: dict, model_plan: str = "",
                   step_index: int = 0, task_id: str = "", emit=None,
                   parallel: int = 3, local_model: str = "") -> dict:
    """多模块并行写：并行写、串行验（验收顺序由调用方统一做）。

    `local_model` 曾经被函数体用了、却没在签名里（2026-11-15 静态检查发现）——
    后果是**空项目 scratch 生成一调用就 NameError**（`fullgen` 三层生成走这里），
    而它被包在 try 里，只留一条 "层 models 生成跳过" 的 notice，看起来像"没生成"。
    """
    results: dict[str, dict] = {}
    if not paths:
        return {"ok": True, "results": {}}
    n = max(1, min(int(parallel or 1), 4, len(paths)))
    if n == 1:
        for rel in paths:
            results[rel] = author_module(ws, rel, contracts.get(rel), contracts,
                                         model_plan=model_plan, step_index=step_index,
                                         task_id=task_id, emit=emit,
                                         local_model=local_model)
        return {"ok": all(v.get("ok") for v in results.values()), "results": results}

    def one(rel: str) -> tuple[str, dict]:
        return rel, author_module(ws, rel, contracts.get(rel), contracts,
                                  model_plan=model_plan, step_index=step_index,
                                  task_id=task_id, emit=emit,
                                  local_model=local_model)

    if emit:
        emit("author_parallel", {"modules": paths[:n], "parallel": n})
    with ThreadPoolExecutor(max_workers=n) as pool:
        for rel, res in pool.map(one, paths):
            results[rel] = res
    return {"ok": all(v.get("ok") for v in results.values()), "results": results}


def _verify(ws: Path, contracts: dict, scope: list[str] | None = None) -> dict:
    """跑一遍逐函数机械判据，返回 {ok, feedback}（feedback 是窄证据）。

    注意**验收范围**：只验本次写的模块（+ 全部模块都在时的 e2e）。
    实测踩过：验全仓时，只要别的模块还坏着，这次写的模块永远过不了 ——
    远端会以为是自己写错，白白重试一轮。
    """
    from . import iterate
    use = dict(contracts)
    if scope:
        all_there = all((Path(ws) / k).exists() for k in contracts)
        keep = [k for k in contracts if k in scope]
        use = {k: contracts[k] for k in keep}
    state = iterate.inspect(ws, use)
    bad: list[str] = []
    for path, st in state.items():
        for f in (st.get("hard_failed") or []):
            bad.append(f"{path}::{f}")
        for msg in (st.get("fails_all") or st.get("fails") or []):
            t = str(msg)
            if t not in bad:
                bad.append(t)
    if bad:
        # 判据反馈是远端**唯一的失败真相**：不掐条数、不掐字数。
        # 原来只给前 6 条、共 1200 字符 → 远端看不到真正的失败原因，
        # 只能重写两遍（实测 29 次"整文件模式两版都未通过"就是这么来的）。
        _fb = "；".join(bad)
        if REMOTE_FEEDBACK_CHARS:
            _fb = _fb[:REMOTE_FEEDBACK_CHARS]
        return {"ok": False, "feedback": _fb}
    return {"ok": True, "feedback": ""}


def save_issues(ws: Path, issues: list[dict]) -> int:
    """把远端指出的"本地系统缺陷"落盘（给人和下一轮架构师看）。

    只做建议，不改任何系统代码：改系统本身必须走精修模式、由人批准。
    """
    if not issues:
        return 0
    p = Path(ws) / ".sixcoder" / "system_issues.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with p.open("a", encoding="utf-8") as f:
        for it in issues:
            f.write(json.dumps(it, ensure_ascii=False) + "\n")
            n += 1
    return n

# ── 成本归因（2026-11-15）────────────────────────────────────────────────
# 为什么用"末尾包一层"而不是在每个函数体里手动 with：
# 这两个函数上千行、多个 return，手动加 with 要整体重排缩进（改动面巨大、易错）。
# 装饰器只多一层调用，`__wrapped__` 保留，自检/回溯看到的仍是原函数。
def _attributed(fn):
    import functools

    @functools.wraps(fn)
    def _wrap(ws, path, *args, **kwargs):
        try:
            from . import attribution as _at
            _ctx = _at.unit(module=str(path), phase=fn.__name__)
        except Exception:                                # noqa: BLE001
            return fn(ws, path, *args, **kwargs)
        with _ctx:
            return fn(ws, path, *args, **kwargs)

    return _wrap


author_module = _attributed(author_module)
author_bodies = _attributed(author_bodies)
