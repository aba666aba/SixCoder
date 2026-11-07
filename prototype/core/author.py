"""整文件 / 多文件由远端写（甲方模式的主力方式）—— v2。

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
7. **可维护性硬指标**：单函数圈复杂度 ≤8、嵌套 ≤3、单函数 ≤40 行（本地会判红）；
   用提前返回、字典查表、拆小函数来降复杂度。
8. 如果你发现**契约或本地判据本身有问题**（漏定义字段、判据缺边界、工具链缺陷），
   写进 system_issues；不要因为契约有问题就自己改签名。
9. **机械性内容可以 delegate**（样板/参数校验/日志/适配层）：你只给一句话规格，最多 3 项。
   本地模型会写并过机械判据，不过会把失败原因还给你下一轮自己写。
   核心逻辑必须你自己写，别把"需要你想清楚"的部分委托出去。
10. **空桩不是实现**：上下文里 `raise NotImplementedError` / 只有 docstring 的函数，
    是**要你写**的桩，不是已有代码。照抄一遍等于什么都没做，会被判红。
"""


def _is_code_path(path: str) -> bool:
    """这个路径能不能当"要写的源码文件"？多语言：按 core/lang 的表判断后缀。"""
    try:
        from . import lang as _lang
        exts = tuple(e for cfg in _lang.LANGS.values() for e in cfg["exts"])
    except Exception:                                    # noqa: BLE001
        exts = (".py",)
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


def _compile_ok(src: str) -> tuple[bool, str]:
    try:
        compile(src, "<remote>", "exec")
        return True, ""
    except SyntaxError as exc:
        return False, f"第 {exc.lineno} 行语法错误：{exc.msg}"



# ---------------------------------------------------------------- 只回函数体
BODIES_PROMPT = """你是资深工程师。骨架已经由引擎按契约渲染好了，你**只写函数体**，不写 def 行。

{protocol}

{skeleton_block}

{contract_block}

{plan_block}

{evidence_block}

只输出 JSON（不要解释、不要围栏外的文字）：
{{"bodies":{{"函数名":"<函数体的若干行，顶格写，引擎会自动缩进>"}},
 "helpers":{{"_辅助函数名":"<完整 def ... 定义，可省略>"}},
 "delegate":[{{"kind":"validation|boilerplate|logging|adapter","target":"函数名","spec":"一句话规格"}}],
 "system_issues":[{{"where":"契约/判据/工具链","issue":"本地缺陷","suggest":"怎么改"}}]}}

铁律：
1. bodies 的键必须是上面的函数名，只给**函数体**（if/for/return 照常写，但不要写 def 行）。
2. 不要输出整文件、不要输出 import、不要输出 def 行。
3. 空输入/空值不许崩；契约要求抛的异常必须抛。
4. 契约里每条示例都要在你的实现上成立。
5. 机械性内容（样板/校验/日志/适配）可以 delegate 给本地模型写，你只给一句话规格，最多 3 项。
6. **可维护性硬指标**：单函数圈复杂度 ≤8、嵌套 ≤3、单函数 ≤40 行（本地会判红）。
   降复杂度用提前返回、字典查表、把大 if 拆成小函数（辅助函数放 helpers）。
"""


def render_skeleton(funcs, existing_src: str = "", module_doc: str = "") -> str:
    """本地渲染骨架：import + 模块 docstring + 每个函数的 def/注解/docstring + 占位体。

    这是"省输出"的关键：签名、注解、docstring、import 全由本地生成（确定性、零 token），
    远端只回函数体那几行。
    """
    lines: list[str] = []
    for ln in (existing_src or "").splitlines():
        st = ln.strip()
        if st.startswith(("import ", "from ")):
            lines.append(st)
    if lines:
        lines.append("")
    if not module_doc:
        module_doc = "由 SixCoder 生成（骨架由引擎渲染，函数体由远端填写）。"
    lines.append('"""' + module_doc + '"""')
    lines.append("")
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


def author_bodies(ws: Path, path: str, contract, contracts_all: dict, model_plan: str = "",
                  evidence: str = "", step_index: int = 0, task_id: str = "",
                  emit=None, attempts: int = 2, max_out: int = 0,
                  context_budget: int = 0, local_model: str = "",
                  only: list[str] | None = None, workingset: str = "") -> dict:
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
    skeleton = render_skeleton(want_funcs, existing_src=existing)
    if not max_out:
        # 只回函数体：基准取"要填的骨架"（含需要保留的既有签名/注解），再留重构余量
        max_out = _tokens.output_budget(skeleton, factor=1.2, floor=1600)
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
        contract_txt = _tokens.cut(cm.describe(contract), 8000)
    except Exception:                                    # noqa: BLE001
        contract_txt = ""
    last = evidence
    issues: list[dict] = []
    for attempt in range(1, max(1, attempts) + 1):
        prompt = BODIES_PROMPT.format(
            protocol=protocol.render("delegate"),
            skeleton_block=f"【骨架（已由引擎渲染好，你只补函数体）】\n```python\n{skeleton}\n```",
            contract_block=f"【契约】\n{contract_txt}",
            plan_block=(f"【实现方案】\n{_tokens.cut(model_plan.strip(), 2000)}" if model_plan else ""
                        ) + (f"\n{workingset}" if workingset else ""),
            evidence_block=(f"【上一版被判红，必须改掉】\n{_tokens.cut(last, 1200)}" if attempt > 1 else ""))
        if attempt > 1 and only:
            prompt += ("\n\n【这次只重写这几个函数体】" + "、".join(only))
        if ctx:
            prompt += "\n\n【其它模块的签名参考（只读，不要改）】\n" + _tokens.cut(ctx, 8000)
        r = remote.complete(prompt, task_id=task_id or "bodies", node_id=f"bodies:{path}",
                            workspace=str(ws), emit=emit, step_index=step_index,
                            json_mode=True, max_out=max_out)
        if not r.get("ok"):
            return {"ok": False, "reason": f"远端调用失败：{r.get('error')}"[:200],
                    "attempt": attempt, "system_issues": issues}
        data = _parse_json(r.get("answer") or "")
        bodies = (data or {}).get("bodies") or {}
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
            # 先定位是**哪一个函数体**写坏的（窄证据）；下一轮只让它重写那一个
            blame = _blame_body(want_funcs, bodies)
            last = f"填进骨架后解析不了：{why}"
            if blame:
                last += "（问题函数：" + "；".join(blame[:3]) + "）"
                only = [b.split(":")[0] for b in blame]
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(code, encoding="utf-8")
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
        last = verdict["feedback"]
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
    return {"ok": False, "reason": "只回函数体两次都未通过：" + (last or "")[:300],
            "attempt": attempts, "system_issues": issues}


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
            out.append(lines[start])
            doc = [ln for ln in lines[start + 1:end] if ln.strip().startswith('"""')][:1]
            out.extend(doc)
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
                  emit=None, attempts: int = 2, max_out: int = 0,
                  context_budget: int = 0, local_model: str = "",
                  workingset: str = "", verify_fn=None,
                  stop_on_repeat: bool = True,
                  deadline: float | None = None,
                  tool_loop: bool = False) -> dict:
    """让远端把**整个文件**写出来：提炼上下文 → 一次生成 → 本地验收 → 窄证据重试。

    max_out=0（默认）→ 按"要写的文件多大"自动算输出配额（见 tokens.output_budget）。
    attempts 是"自愈轮数"（甲方模式默认 4，来自 policy.heal_attempts）；
    stop_on_repeat=True 时，若新一轮的失败证据与已用过的完全相同就不发这次调用。
    deadline 是本片的时间截止点（unix 秒）：单轮 4 次自愈每次可能耗两分钟，
    只在外层 round 边界查时间会让 --slice-hours 形同虚设（实测 dev1106c）。
    """
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
        max_out = _tokens.output_budget(want, factor=2.0 if stubby else 1.5,
                                        floor=3000 if stubby else 1200)
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
    # 把**当前文件的全文**也给作者（限长）：只给"焦点符号源码"会让它看不见 import 区
    # 与模块级接线，于是每轮都从头重写、顺手丢东西（实测 2026-11-07 振荡 6 轮）。
    try:
        _cur = Path(ws) / path
        if _cur.is_file():
            _txt = _cur.read_text(encoding="utf-8", errors="replace")
            if _txt.strip() and len(_txt) <= 12000:
                ctx += ("\n\n# 当前文件内容（在它基础上**增量修改**：保留已有 import、"
                        "已有函数、已有路由，别整篇重写）\n```python\n"
                        + _txt.rstrip() + "\n```\n")
    except OSError:
        pass
    req_block = ("【实现方案（必须照它做）】\n" + _tokens.cut(model_plan.strip(), 2500)) if model_plan else ""
    if workingset:
        req_block = (req_block + "\n\n" + workingset).strip()
    prompt = AUTHOR_PROMPT.format(protocol=protocol.render("files"), context=ctx,
                                  requirements=req_block)
    # §13.4 第 3 步：开启工具循环时，把只读工具说明追加给远端（它可发 <tool> 调用）
    if tool_loop:
        try:
            from . import remotetools as _rtl
            prompt = prompt + "\n\n" + _rtl.tool_loop_guide()
        except Exception:                        # noqa: BLE001
            pass
    last = evidence
    issues: list[dict] = []
    _seen_evidence: set[str] = set()                # 已用过的失败证据（防止同证据反复刺激）
    grew = 0                                        # 因为"被截断"而放大配额的次数（最多 2 次）
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
        if attempt > 1 and stop_on_repeat and _ev and _ev in _seen_evidence:
            issues.append({"where": "author", "issue": f"连续两轮失败证据完全相同，提前停止自愈（{path}）",
                           "suggest": "让判据给出更具体的差异（行号/期望值），否则远端没有新信息可用"})
            if emit:
                emit("heal_stop", {"path": path, "attempt": attempt,
                                   "why": "失败证据与上一轮完全相同（无新信息）"})
            break
        _seen_evidence.add(_ev)
        body = prompt if attempt == 1 else (
            prompt + "\n\n【上一版被机械判据判红，必须改掉】\n" + _tokens.cut(last or "", 1500))
        r = remote.complete(body, task_id=task_id or "author", node_id=f"author:{path}",
                            workspace=str(ws), emit=emit, step_index=step_index,
                            json_mode=False, max_out=max_out)
        if not r.get("ok"):
            return {"ok": False, "reason": f"远端调用失败：{r.get('error')}"[:200],
                    "by": "", "attempt": attempt, "system_issues": issues, "max_out": max_out}
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
        if used >= max(1, int(max_out * 0.98)) and grew < 2:
            grew += 1
            old = max_out
            max_out = min(384000, max(max_out * 4, 3000))
            if emit:
                emit("output_budget", {"path": path, "max_out": max_out, "grew_from": old,
                                       "why": "上一版输出顶到配额上限，判为截断而非能力问题"})
            last = (f"上一版输出被截断（{used} token 就顶到配额 {old}）。"
                    "请只输出完整 JSON（不要解释、不要省略），必要时删掉非必要注释。")
            continue
        files, got_issues, delegated = _parse_reply(r.get("answer") or "")
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
        p.parent.mkdir(parents=True, exist_ok=True)
        # 写回前先确认"没把已实现的函数删掉"：删了就打回，不落盘。
        try:
            _old = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        except OSError:
            _old = ""
        _gone = _deleted_symbols(_old, content)
        if _gone:
            last = ("你把已实现的函数删掉了：%s。整文件重写不许删已有函数 —— "
                    "保留它们，只在里面补新功能。" % "、".join(_gone[:6]))
            if emit:
                emit("author_regress", {"path": path, "deleted": _gone[:6]})
            continue
        p.write_text(content, encoding="utf-8")
        _ensure_packages(ws, path)          # 嵌套模块要有包结构才 import 得动
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
                   parallel: int = 3) -> dict:
    """多模块并行写：并行写、串行验（验收顺序由调用方统一做）。"""
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
        for msg in (st.get("fails") or [])[:2]:
            t = str(msg)
            if t not in bad:
                bad.append(t)
    if bad:
        return {"ok": False, "feedback": "；".join(bad[:6])[:1200]}
    return {"ok": True, "feedback": ""}


def save_issues(ws: Path, issues: list[dict]) -> int:
    """把远端指出的"本地系统缺陷"落盘（给人和下一轮架构师看）。

    只做建议，不改任何系统代码：改系统本身必须走开发者模式、由人批准。
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
