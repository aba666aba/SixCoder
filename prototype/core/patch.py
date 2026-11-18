"""**行级修复**：一行有问题就只改那一行，不再重写整个函数/整个文件。

用户的批评（成立）：
    整文件有问题就重写整文件、一个函数有问题就重写一个函数 —— 粒度太粗。
    一行错的事情，为什么要重发几百行？既费输出 token，又容易把本来对的代码改坏。

所以这里补最细的一档：**补丁式修复**（patch）。
    远端只回：{"patches":[{"path":"calc.py","line":12,"old":"    return a / b","new":"    return a / b if b else 0.0"}]}
    引擎按行号 + **原文逐字匹配**应用；原文对不上就拒绝（宁可不改，也不许乱改）。

三档粒度由证据决定（不是由我们拍脑袋）：
    · 证据定位到**行**（语法错第 N 行、traceback 的出错行、某个 raise 的类型不对）→ 行级补丁；
    · 证据定位到**函数**（示例/边界判据红，但说不出哪一行）→ 只重发那一个函数体；
    · 证据跨函数/架构问题 → 才重写整个文件。

所以"为什么只能整文件/整函数"的答案是：以前只有那两档；现在按证据自动降级到最细一档。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PATCH_PROMPT = """你在做**行级修复**。只改必要的那几行，不要重写整个函数或整个文件。

失败证据（本地机械判据给出）：
{evidence}

目标文件当前内容（行号已标出；每行行号就是真实行号）：
{numbered}

只输出 JSON（不要解释、不要围栏外的文字）：
{{"patches":[{{"path":"{path}","line":<行号>,"old":"<那一行的原文，逐字复制>","new":"<改后的行>"}}],
 "why":"一句话说明"}}

铁律：
1. 行号必须是上面标出的真实行号；old 必须与那一行**逐字相同**（缩进也要一致），
   否则引擎会拒绝应用（宁可不改，也不许乱改）。
2. 保持缩进层级不变；不要改动无关行；不要重排已有的 import。
3. 只改必要的最少行数（通常 1~3 行）。
4. **可以新增**：import 行、模块级常量、辅助函数。
   · 插在第 N 行之前 → line 填 N、old 留空字符串 "";
   · new 可以写**多行**（用 \\n 分隔），引擎会原样展开；
   · 新增的 import 只用标准库；第三方库会在依赖自查里被点名，能用标准库就别引入。
5. **不可以改函数签名**（参数/返回类型变了就不是"行修复"，那时会走函数级重写）。
6. 如果失败是因为某个名字根本没定义，就补上它的定义，而不是在调用处绕过去。
7. **old 可以跨多行**（引擎会按多行块定位与替换，起始行 = 你填的 line）：
   要改的是连续几行时，old 写这几行的完整原文（用 \\n 分隔），
   new 写改后的对应几行（同样用 \\n 分隔）；缩进按原样写，引擎会自己对齐。
   —— 但如果只改一行，就只写那一行。**能少写就少写，补丁越小越安全。**
"""

LINE_HINTS = (
    re.compile(r"(?:line|第)\s*(\d+)\s*行?"),
    re.compile(r'File "[^"]+", line (\d+)'),
    re.compile(r"^\s*(\d+)\s*\|", re.M),
    # traceback 常见写法：store.py:93: in _row_values（旧实现认不出，定位退回模块级）
    re.compile(r"[\w./\\-]+\.py:(\d+)"),
)

# 多行锚点定位时，在提示行号**上下**各找这么多行（§11.4m）。
# 为什么要有：远端给的 old 首行可能是文件那一行的一部分，行号只当提示，
# 真实位置可能偏几行。窗口开太大又会"改错地方"——定位仍以**内容逐字匹配**为准，
# 匹配不上就拒绝（宁可不改，也不许乱改）。
_PATCH_LOOKBACK = 40


def _line_match(want: str, actual: str) -> bool:
    """old 的一行与文件那一行是否算匹配（§11.4m）。

    远端复制 old 时经常**丢掉尾部标点**（`\'k\': keywords` vs 文件里的 `\'k\': keywords,`）
    或**截断**（只复制一行的一部分）。
    口径：先逐字相同；再允许"去掉尾部逗号后相同"；再允许"文件行包含 old"。
    仍然匹配不上 → 拒绝（安全规则不退让）。
    """
    a = actual.strip()
    if a == want:
        return True
    if a.rstrip(",") == want.rstrip(","):
        return True
    if want and want in a:
        return True
    return False


def _numbered(code: str, start: int = 1) -> str:
    return "\n".join(f"{i:>4} | {ln}" for i, ln in enumerate(code.splitlines(), start))


def parse_patches(text: str) -> list[dict]:
    """从回复里抽补丁清单（容忍围栏与杂质）。"""
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
    out: list[dict] = []
    for it in (data.get("patches") or [])[:6]:
        if not isinstance(it, dict):
            continue
        try:
            line = int(it.get("line"))
        except (TypeError, ValueError):
            continue
        old = str(it.get("old") or "")
        new = str(it.get("new") or "")
        path = str(it.get("path") or "").strip()
        if not old and not new:
            continue
        out.append({"path": path, "line": line, "old": old, "new": new})
    return out


def apply_patches(code: str, patches: list[dict]) -> dict:
    """按行号 + 原文逐字匹配应用补丁。

    两种操作（由 old 是否为空决定）：
      · old 非空 → **替换**第 N 行（原文必须逐字匹配，否则拒绝）；
      · old 为空 → **在第 N 行之前插入** new（new 可含多行），原第 N 行不受影响。

    **old 支持多行**（2026-11-11 实测后加，§11.4m）：
      远端经常把 `old` 写成**跨几行的一段代码**（从带行号的源码里复制，
      复制到换行为止）—— 实测日志原文：
      `期望 "'direction': direction_text,\n           "，实际 "'direction': direction_text,"`
      旧实现拿**单行**去比多行 old → **永远不等** → 100% 被拒 →
      引擎拿不到行级修复 → 退回**整文件重写** → 打掉该文件里已通过的能力
      （实测 `app/models.py` 承载 9 条 done 能力）→ 净倒退。
      现在：`old` 含换行时，按**多行块**定位与替换（起始行 = N）。

    安全规则（防止"改错行"把对的代码改坏）：
      · 行号越界 → 拒绝（插入允许 N = 行数+1，即追加到末尾）；
      · 那一行（多行时：那几行）原文与 old 不一致 → 拒绝；
      · 缩进层级按原行对齐（插入则原样保留 new 的缩进）；
      · 多行块替换时，**new 的每一行都套用原块首行的缩进**（远端给的 new
        常不带缩进，直接拼进去会语法错）。
    """
    lines = code.splitlines()
    applied: list[str] = []
    rejected: list[str] = []
    # 从后往前应用，避免行号错位
    for pt in sorted(patches, key=lambda x: -int(x["line"])):
        n = int(pt["line"])
        old = (pt.get("old") or "").strip()
        new = pt.get("new") or ""
        if old:
            old_lines = old.splitlines()
            if n < 1 or n > len(lines):
                rejected.append(f"第 {n} 行不存在（文件只有 {len(lines)} 行）")
                continue
            if len(old_lines) == 1:
                # ---- 单行替换（原有语义，逐字不变）----
                cur = lines[n - 1]
                _want = old_lines[0].strip()
                if not _line_match(_want, cur):
                    rejected.append(f"第 {n} 行原文对不上：期望 {_want[:40]!r}，"
                                    f"实际 {cur.strip()[:40]!r}")
                    continue
                if cur.strip() != _want:
                    # **片段替换**（§11.4m）：old 是那一行的一部分 →
                    # 只替换内部这一段（保留前后缀），不动行首缩进。
                    _pre, _post = cur.split(_want, 1)
                    lines[n - 1] = _pre + new.strip() + _post
                    applied.append(f"第 {n} 行替换（行内片段）")
                    continue
                indent = cur[:len(cur) - len(cur.lstrip())]
                lines[n - 1] = (indent + new.strip()) if new.strip() else ""
                applied.append(f"第 {n} 行替换")
            else:
                # ---- 多行块替换（§11.4m 新增）----
                # 远端给的 old 是**连续几行**的原文；但它的首行可能只是文件那一行的
                # **一部分**（复制时截了一段），也可能整体缩进与文件不同。
                # → 不死绑"第 N 行起"，改成**按块内容在文件里定位**：
                #   行号只当提示，定位以内容为准；定位不到就拒绝（安全规则不退让）。
                _norm = [o.strip() for o in old_lines if o.strip()]   # 去掉纯缩进的空行
                if not _norm:
                    rejected.append(f"第 {n} 行的 old 只有空白，没法定位")
                    continue
                _start = -1
                _hit_len = 0
                _total = len(lines)
                _lo = max(0, n - 1 - _PATCH_LOOKBACK)
                _hi = min(_total, n - 1 + _PATCH_LOOKBACK + 1)
                # **必须整块匹配**：old 有 k 行就要连续命中 k 行，**不许退化成只用首行**
                # （退化 = 把"这块要改的地方"缩成一行 → 改错位置）。
                # 首行允许 old 是文件那一行的**子串**（远端复制时可能只复制一段）；
                # 其余行必须逐字相同（容忍尾部逗号差异）。
                _k = len(_norm)
                for _i in range(_lo, max(_lo, _hi - _k + 1)):
                    if _i + _k > _total:
                        break
                    _blk = lines[_i:_i + _k]
                    if _norm[0] not in _blk[0]:
                        continue
                    if all(_line_match(_norm[_j], _blk[_j]) for _j in range(1, _k)):
                        _start = _i
                        _hit_len = _k
                        break
                if _start < 0:
                    rejected.append(f"第 {n} 行附近找不到 old 的原文（多行锚点定位失败）："
                                    f"{_norm[0][:40]!r}")
                    continue
                # 命中的块：把 old 的每一行替换成 new 的对应行
                new_lines = new.splitlines()
                if not new_lines:
                    lines[_start:_start + _hit_len] = []
                    applied.append(f"第 {_start + 1}~{_start + _hit_len} 行删除（{_hit_len} 行）")
                    continue
                # 缩进：把 new 的**相对缩进**（相对它自己的首行）套到原块首行的缩进上。
                # 为什么不能"每行都套首行缩进"：new 的第 2、3 行通常比首行多缩进
                # （续行/字典项），全平掉会语法错（实测用例4）。
                _first = new_lines[0]
                own = _first[:len(_first) - len(_first.lstrip())]
                _base = lines[_start][:len(lines[_start]) - len(lines[_start].lstrip())]
                out = []
                for _nl in new_lines:
                    if own and _nl.startswith(own):
                        out.append(_base + _nl[len(own):])
                    elif _nl.strip():
                        out.append(_base + _nl.lstrip())
                    else:
                        out.append(_nl)          # 空行保持空
                lines[_start:_start + _hit_len] = out
                applied.append(f"第 {_start + 1}~{_start + _hit_len} 行替换"
                               f"（{_hit_len} 行 → {len(out)} 行）")
        else:
            if n < 1 or n > len(lines) + 1:
                rejected.append(f"插入位置 {n} 越界（文件只有 {len(lines)} 行）")
                continue
            if not new.strip():
                rejected.append(f"第 {n} 行插入内容为空")
                continue
            lines[n - 1:n - 1] = new.splitlines()
            applied.append(f"第 {n} 行前插入 {len(new.splitlines())} 行")
    return {"code": "\n".join(lines) + ("\n" if code.endswith("\n") else ""),
            "applied": applied, "rejected": rejected,
            "ok": bool(applied) and not rejected}


def line_hint(evidence: str) -> int:
    """从证据里猜出错行号（猜不到返回 0）。"""
    t = str(evidence or "")
    for pat in LINE_HINTS:
        m = pat.search(t)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 100000:
                    return n
            except (TypeError, ValueError):
                continue
    return 0


def is_line_localized(evidence: str) -> bool:
    """证据是否定位到"行"（语法错 / traceback 行 / 某个 raise 的类型不对）。"""
    t = str(evidence or "")
    if not t:
        return False
    if line_hint(t):
        return True
    return any(k in t for k in ("SyntaxError", "NameError", "unbound", "IndentationError"))


def narrow_evidence(text: str, limit: int = 1200) -> str:
    """窄证据：保留"失败行优先 + 头尾"，**不是**从头砍到尾。

    为什么不能简单截断：pytest 的断言与 `E   ` 行往往在尾部，砍掉尾巴等于把
    最有用的一行删了；而整段发过去又会把定位退化成模块级（实测过）。
    """
    t = str(text or "").strip()
    if len(t) <= limit:
        return t
    rows = t.splitlines()
    keep: list[int] = []
    for i, ln in enumerate(rows):
        s = ln.strip()
        if s.startswith(("FAILED ", "E   ", "ERROR ", "assert ")) or "Error:" in s \
                or re.match(r"^\d+\s*\|", s) or re.search(r'line (\d+)', s):
            keep.append(i)
    picked: list[int] = []
    for i in keep:                                  # 关键行前后各带 1 行
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(rows) and j not in picked:
                picked.append(j)
    picked.sort()
    out: list[str] = []
    used = 0
    for i in picked:                               # 先放关键行（截断时优先保住）
        ln = rows[i]
        if used + len(ln) + 1 > limit:
            break
        out.append(ln)
        used += len(ln) + 1
    if not out:                                    # 没有关键行 → 头 + 尾
        head = t[: int(limit * 0.6)]
        tail = t[-int(limit * 0.35):]
        return head + "\n…（中间省略）…\n" + tail
    return "\n".join(out)[:limit]


def view_for_patch(code: str, hint: int = 0, budget_tokens: int | None = None) -> str:
    """把文件渲染成"带行号的输入"：放得下就全给，放不下才做结构骨架 + 出错窗口。

    为什么不再用字符上限：12,000 字符是拍脑袋的数，2000 行文件会被砍掉 4/5，
    而出错行常常正好在被砍掉的尾部 → 模型只能盲改。真正的上限是模型上下文
    （默认 950K token），放得下就没有理由截。
    """
    from . import tokens as _tokens
    full = _numbered(code)
    ok, est, budget = _tokens.fits(full, budget_tokens)
    if ok:
        return full
    rows = code.splitlines()
    want: list[str] = []
    for i, ln in enumerate(rows, 1):
        s = ln.strip()
        if s.startswith(("import ", "from ")) or s.startswith(("def ", "class ")) \
                or (ln[:1] not in (" ", "\t") and s.endswith(":") and "=" in s):
            want.append(f"{i:>4} | {ln}")
    if hint > 0:                                   # 出错窗口 ±40 行
        lo, hi = max(1, hint - 40), min(len(rows), hint + 40)
        want.append(f"     | …（以上是结构与 import 骨架，以下是第 {lo}~{hi} 行原文）…")
        want += [f"{i:>4} | {rows[i - 1]}" for i in range(lo, hi + 1)]
    want.append(f"     | …（文件共 {len(rows)} 行，其余行未显示；不要改未显示的行，"
                f"需要改就用 add 的方式插到已显示的行号上）…")
    _ = (est, budget)
    return "\n".join(want)


def request_line_fix(ws: Path, path: str, evidence: str, step_index: int = 0,
                     task_id: str = "", emit=None, max_out: int = 0,
                     verify=None) -> dict:
    """让远端做**一次行级修复**。改完由 verify 回调重新判定（没人给就只做语法检查）。

    max_out=0（默认）→ 按"要改的那几行有多大"自动算配额（行补丁本来就小）。
    """
    from . import remote, tools
    from . import tokens as _tokens
    p = tools.safe_path(ws, path)
    if not p.exists():
        return {"ok": False, "reason": "目标文件不存在"}
    code = p.read_text(encoding="utf-8-sig", errors="replace")
    hint = line_hint(str(evidence))
    if not max_out:
        # 行级修复只回"替换哪几行 + 新行"，体量很小；用证据里的上下文行做基准
        span = ""
        if hint > 0:
            rows = code.splitlines()
            span = "\n".join(rows[max(0, hint - 3):hint + 3])
        max_out = _tokens.output_budget(span or code, factor=0.6, floor=1200)
    prompt = PATCH_PROMPT.format(evidence=narrow_evidence(evidence), numbered=view_for_patch(code, hint),
                                 path=path)
    # 行级修复也带**只读工具**：远端改之前可以先 outline/read_file 看真实代码
    # （原来它只能看提示词里那几行编号，容易把相邻行改坏）。
    _ans = ""
    _usage: dict = {}
    _failed = [False]
    try:
        from . import remotetools as _rtl

        def _fc(_instr: str, messages: list, _tools=None) -> dict:
            nonlocal _usage
            rr = remote.complete(_instr, task_id=task_id or "patch", node_id=f"patch:{path}",
                                 workspace=str(ws), emit=emit, step_index=step_index,
                                 json_mode=True, max_out=max_out,
                                 messages=messages, tools=_tools)
            if isinstance(rr, dict):
                _usage = rr.get("usage") or {}
                if not rr.get("ok"):
                    _failed[0] = True
                return {"answer": rr.get("answer") or "",
                        "tool_calls": list(rr.get("tool_calls") or []),
                        "message": rr.get("message") or {}}
            return {"answer": "", "tool_calls": [], "message": {}}

        _lp = _rtl.ToolLoop(ws, max_rounds=2, emit=emit, complete_fn=_fc)
        _lr = _lp.run(prompt)
        _ans = str(_lr.get("answer") or "")
        if emit:
            emit("patch_tools", {"path": path, "rounds": _lr.get("rounds"),
                                 "tools": len(_lr.get("tool_calls") or [])})
    except Exception as exc:                            # noqa: BLE001
        if emit:
            emit("notice", {"message": f"行级修复工具循环跳过：{type(exc).__name__}"})
        _ans = ""
    if not _ans and not _failed[0]:
        r = remote.complete(prompt, task_id=task_id or "patch", node_id=f"patch:{path}",
                            workspace=str(ws), emit=emit, step_index=step_index, json_mode=True,
                            max_out=max_out)
        if not r.get("ok"):
            return {"ok": False, "reason": f"远端调用失败：{r.get('error')}"[:180]}
        _ans = r.get("answer") or ""
    elif _failed[0]:
        return {"ok": False, "reason": "远端调用失败（工具循环已试过，不重复补打）"[:180]}
    r = {"ok": True, "answer": _ans, "usage": _usage}
    patches = parse_patches(r.get("answer") or "")
    if not patches:
        return {"ok": False, "reason": "没有给出 patches"}
    res = apply_patches(code, patches)
    if not res["ok"]:
        return {"ok": False, "reason": "补丁未应用：" + "；".join(res["rejected"])[:200],
                "applied": res["applied"], "rejected": res["rejected"]}
    try:
        compile(res["code"], "<patched>", "exec")
    except SyntaxError as exc:
        return {"ok": False, "reason": f"打完补丁解析不了：第 {exc.lineno} 行 {exc.msg}"}
    p.write_text(res["code"], encoding="utf-8")
    verdict = {"ok": True}
    if verify is not None:
        try:
            verdict = verify()
        except Exception as exc:                         # noqa: BLE001
            verdict = {"ok": False, "feedback": f"{type(exc).__name__}"}
    if not verdict.get("ok"):
        p.write_text(code, encoding="utf-8")            # 回滚：补丁没解决问题就别留着
        return {"ok": False, "reason": "行级修复后仍不通过：" + str(verdict.get("feedback"))[:200],
                "applied": res["applied"]}
    return {"ok": True, "applied": res["applied"], "patches": len(patches),
            "by": r.get("by") or "", "usage": r.get("usage") or {}}
