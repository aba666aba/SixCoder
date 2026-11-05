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
"""

LINE_HINTS = (
    re.compile(r"(?:line|第)\s*(\d+)\s*行?"),
    re.compile(r'File "[^"]+", line (\d+)'),
    re.compile(r"^\s*(\d+)\s*\|", re.M),
    # traceback 常见写法：store.py:93: in _row_values（旧实现认不出，定位退回模块级）
    re.compile(r"[\w./\\-]+\.py:(\d+)"),
)


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

    安全规则（防止"改错行"把对的代码改坏）：
      · 行号越界 → 拒绝（插入允许 N = 行数+1，即追加到末尾）；
      · 那一行原文与 old 不一致 → 拒绝；
      · 缩进层级按原行对齐（插入则原样保留 new 的缩进）。
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
            if n < 1 or n > len(lines):
                rejected.append(f"第 {n} 行不存在（文件只有 {len(lines)} 行）")
                continue
            cur = lines[n - 1]
            if cur.strip() != old.strip():
                rejected.append(f"第 {n} 行原文对不上：期望 {old.strip()[:40]!r}，"
                                f"实际 {cur.strip()[:40]!r}")
                continue
            indent = cur[:len(cur) - len(cur.lstrip())]
            lines[n - 1] = (indent + new.strip()) if new.strip() else ""
            applied.append(f"第 {n} 行替换")
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
        max_out = _tokens.output_budget(span or code, factor=0.6, floor=800)
    prompt = PATCH_PROMPT.format(evidence=narrow_evidence(evidence), numbered=view_for_patch(code, hint),
                                 path=path)
    r = remote.complete(prompt, task_id=task_id or "patch", node_id=f"patch:{path}",
                        workspace=str(ws), emit=emit, step_index=step_index, json_mode=True,
                        max_out=max_out)
    if not r.get("ok"):
        return {"ok": False, "reason": f"远端调用失败：{r.get('error')}"[:180]}
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
