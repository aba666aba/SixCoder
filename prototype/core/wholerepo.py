"""整仓会话：远端一次拿到**整个仓库**，并拥有**整仓写权限**。

为什么要换掉"按文件派单"（2026-11-15 用户定稿）：

    只给几个文件（`--max-files 6`）→ **跨文件的问题永远看不见**：
    入口配置错、契约与实现命名漂移、返回形状不统一、路由与函数签名不匹配、
    数据模型字段与断言不符 —— 这些"共同因"会同时引发七八处小症状。
    按文件派单时，远端只能看到其中一处症状，改完下一轮换个文件再来一遍，
    钱花了、根因还在。用户的原话："只给改几个文件，跨了好几个文件的问题
    就找不出来，也改不了。"

所以本模块的会话**每次都给整仓**：

  1. `collect`  采全仓源码（路径排序稳定 → 前缀缓存友好）
  2. `brief`    提示词：权限声明 + 全仓源码 + 错误报告 + 历史根因 + 归因优先
  3. `parse`    从回复里抽 `=== FILE: path ===` 整文件块 / `=== DELETE: path ===`
  4. `apply`    守卫：快照 → 逐文件 ast → 写 → 全仓复验 → **变差就整批回滚**

**归因优先**（这是提示词工程的核心，不是客套）：
报告里 20 条 P1 往往只是 1–2 个共同因的症状。所以硬性要求远端
**先写 `<rootcause>` 诊断（1–3 条，每条注明解释了报告里哪些编号），
再改根因，最后才治孤立小问题**；能随根因自动消失的症状不许单独改。

**"远端觉得没问题了"**：回复里出现 `=== DONE ===` 且没有文件块 → 记为
"远端自认完成"。但**不据此收工** —— 下一轮的本地检测报告才是判据
（铁律：判定层看业务事实，不看模型自述）。
"""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path

# 采集口径：这些目录不是"产品代码"，塞进上下文只会挤掉真正要看的东西。
EXCLUDE_DIRS = {
    "__pycache__", ".git", ".hg", ".svn", ".sixcoder", "_legacy_app",
    "node_modules", ".venv", "venv", "env", "dist", "build", "target",
    "migrations", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
    "screenshots", "staticfiles", "htmlcov", ".tmp",
}
# 只收文本源码/配置（二进制、锁文件、依赖目录一律不收）
KEEP_EXT = {
    ".py", ".html", ".htm", ".drl", ".sql", ".js", ".css", ".json",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".txt", ".bat", ".sh",
}
# 这些文件名太大且无信息量 / 是生成的，不要
EXCLUDE_FILES = {"manage.py.bak", "package-lock.json", "yarn.lock"}

# 全仓快照上限。为什么不直接顶满 200k（REMOTE_CTX_CHARS）：
# 输入 + 输出要一起塞进模型窗口。本项目全仓 179k 字符时，200k 快照会把
# 提示词顶到 118k token，再要它输出整文件（32k token）就贴天花板了。
# 140k 字符（≈88k token）+ 32k 输出 = 120k，留出安全余量；
# 超出的部分**按大小截断并在源码里留可见标记**，绝不静默丢。
# 中英混排的保守换算：实测 1.6~1.8 字符/token，这里按 1.5 算（**宁少算不多算**，
# 少算的后果是"少发一点源码"，多算的后果是"爆窗被 provider 拒绝"）。
CHARS_PER_TOKEN = 1.5
OUT_SAFETY_TOKENS = 8_000      # 提示词骨架 + 报告 + 根因历史的安全垫


def _ctx_tokens() -> tuple[int, int, str]:
    """(上下文 token 预算, 输出预留 token, 来源说明)。

    优先级：配置里的 `max_prompt_tokens`（就是模型窗口的**输入**上限）→
    退化到 `max_prompt_chars / 1.5` → 最后才用兜底常量。
    输出预留取"顶层 max_output_tokens"和"当前档位 max_output_tokens"的较大者。
    """
    ctx = out = 0
    src = "兜底常量"
    try:
        from . import settings_store as _ss
        s = _ss.get_remote() or {}
        ctx = int(s.get("max_prompt_tokens") or 0)
        out = int(s.get("max_output_tokens") or 0)
        if ctx > 0:
            src = "settings.max_prompt_tokens"
        else:
            cap = int(s.get("max_prompt_chars") or 0)
            if cap > 0:
                ctx, src = int(cap / CHARS_PER_TOKEN), "settings.max_prompt_chars"
        try:
            from . import remote as _r
            _st = (_r.step_at(0) or {})
            out = max(out, int(_st.get("max_output_tokens") or 0))
        except Exception:                                # noqa: BLE001
            pass
    except Exception:                                    # noqa: BLE001
        pass
    if ctx <= 0:
        ctx = 200_000                                    # 明显偏小会说出来（见 session 的 emit）
    return ctx, out, src


def budget_chars() -> int:
    """全仓快照的字符预算 = **模型上下文 − 输出预留 − 安全垫**，再折成字符。

    历史（2026-11-15 用户两次追问）：
      ① 这里原来写死 140_000（按"128k 窗口"拍的）→ 整仓只占配置的 8.6%，
         却开始切 `apps/*/urls.py`，等于让远端闭着眼睛改接线；
      ② 后来改成 `max_prompt_chars × 0.6`（拍 60%），仍然是个拍出来的比例。

    现在的口径**没有拍的比例**：上下文多少是配置写的，输出要多少是模型自己声明的，
    剩下的全部给输入。950k 上下文 / 12k 输出 → 930k token → 约 1.39M 字符。
    """
    ctx, out, _src = _ctx_tokens()
    input_tokens = max(0, ctx - max(0, out) - OUT_SAFETY_TOKENS)
    return max(200_000, int(input_tokens * CHARS_PER_TOKEN))


def max_out_tokens() -> int:
    """本次调用的输出上限：跟模型声明走（flash 12k / pro 16k），不硬要 32k。

    为什么要单独算：以前这里写死 32_000，而 flash 只允许 12_000 ——
    超了要么被 provider 拒，要么被静默截断（截断=整文件缺一半）。
    """
    _ctx, out, _src = _ctx_tokens()
    if out <= 0:
        return 12_000
    return max(4_000, min(64_000, int(out)))


MAX_SNAPSHOT_CHARS = 0     # 0 = 用 budget_chars() 现算（配置优先）
MAX_OUT_TOKENS = 0         # 0 = 用 max_out_tokens()（模型声明的输出上限）

# 截断优先级（只在真的超预算时才用）：**先切最不可能被改、且已绿的**。
# 接线/视图/模型是被改的主体，绝不切（切了等于让远端"闭着眼睛改"）。
CUT_PRIORITY = ("matching_core/", "tests/", "tools/", "docs/", "_legacy_app/")
PROTECT_SUFFIX = ("/urls.py", "/views.py", "/views_people.py", "/models.py",
                  "/settings.py", "/asgi.py", "/wsgi.py")
ROOTCAUSE_FILE = ".sixcoder/rootcause.json"
NEED_FILE = ".sixcoder/need.json"
HASH_FILE = ".sixcoder/snapshot_hashes.json"
PASSED_FILE = ".sixcoder/cap_passed.json"
REJECT_FILE = ".sixcoder/apply_reject.json"
ROLLBACK_FILE = ".sixcoder/rollback_feedback.json"
MARK_DONE = "=== DONE ==="
FILE_MARK_RE = r"^\s*===\s*FILE:\s*(?P<path>[^\s=]+)\s*===\s*$"
DEL_MARK_RE = r"^\s*===\s*DELETE:\s*(?P<path>[^\s=]+)\s*===\s*$"

# 小步限制（2026-11-16 用户定稿）：一轮**最多改 2 个文件**（含删除）。
# 为什么必须有：整仓权限下远端一轮改 6-13 个文件，实测"改对 4、改坏 4"，
# 回归护栏把整批回滚 → 净增长为 0，钱花了能力没多一条。
# 限制成 2 个后，"改对 1-2、改坏 0" 才可能净增长为正。
# 这是**写盘层硬闸**，不是提示词请求 —— 提示词只是让远端知道并配合。
# None = 不限制（保留给显式调试）；默认 2。
MAX_FILES_PER_ROUND = 2


def _emit(emit, tag: str, data: dict) -> None:
    if emit:
        try:
            emit(tag, data)
        except Exception:                                # noqa: BLE001
            pass


# ---------------------------------------------------------------- 1. 采集
def _skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in EXCLUDE_DIRS for p in parts[:-1]):
        return True
    if parts[-1] in EXCLUDE_FILES:
        return True
    return False


def _sig_lines(src: str) -> tuple[list[str], int, int]:
    """从源码里抽「签名清单」：(签名行列表, 函数数, 空桩数)。

    只发签名、不发函数体 —— 这是"架构"的最小完整表达：
    远端据此能判断"该动哪个函数、签名是什么、会不会牵连调用方"。
    """
    import ast as _ast
    out: list[str] = []
    n_f = n_s = 0
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return out, 0, 0
    for node in tree.body:
        if isinstance(node, _ast.ClassDef):
            bases = ", ".join(_ast.unparse(b) for b in node.bases)
            out.append("class %s%s:" % (node.name, ("(" + bases + ")") if bases else ""))
            for sub in node.body:
                if isinstance(sub, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                    n_f += 1
                    n_s += 1 if _is_stub(sub) else 0
                    out.append("    " + _sig_of(sub))
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            n_f += 1
            n_s += 1 if _is_stub(node) else 0
            out.append(_sig_of(node))
        elif isinstance(node, _ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, _ast.Name)]
            if names and getattr(node.value, "lineno", 0):
                out.append("%s = …" % ", ".join(names))
    return out, n_f, n_s


def _sig_of(node) -> str:
    import ast as _ast
    a = node.args
    parts = [x.arg for x in a.posonlyargs] + [x.arg for x in a.args]
    if a.vararg:
        parts.append("*" + a.vararg.arg)
    parts += ["%s=%s" % (x.arg, _ast.unparse(x)) for x in a.kwonlyargs if x.arg]
    if a.kwarg:
        parts.append("**" + a.kwarg.arg)
    ret = (" -> " + _ast.unparse(node.returns)) if getattr(node, "returns", None) else ""
    prefix = "async def" if isinstance(node, _ast.AsyncFunctionDef) else "def"
    return "%s %s(%s)%s: ..." % (prefix, node.name, ", ".join(parts), ret)


def _is_stub(node) -> bool:
    import ast as _ast
    body = [b for b in node.body
            if not (isinstance(b, _ast.Expr) and isinstance(b.value, _ast.Constant)
                    and isinstance(b.value.value, str))]
    if len(body) != 1:
        return False
    b = body[0]
    return isinstance(b, _ast.Pass) or (
        isinstance(b, _ast.Raise) and "NotImplementedError" in _ast.dump(b))


def _stub_text(rel: str, src: str) -> str:
    """把一个文件降级成"签名版"（保留 import 与签名，函数体变 `...`）。"""
    sigs, _n_f, _n_s = _sig_lines(src)
    head = ("# [引擎：本文件**只发了签名**（全文超出上下文预算）。\n"
            "#  需要全文请在下轮回复里写： === NEED: %s === ]\n" % rel)
    return head + "\n".join(sigs) + "\n"


def collect(ws, *, limit: int = 0, keep_full=None) -> tuple[dict[str, str], dict]:
    """采全仓文本源码，返回 ({相对路径: 内容}, 统计)。

    排序**按路径**（不是按大小/时间）：稳定的前缀才能吃到缓存。

    `limit=0` → 用 `budget_chars()`（= 模型上下文 − 输出预留 − 安全垫）。

    **超预算不再截断**（2026-11-15 用户定稿）：截断会让远端"闭着眼睛改"，
    而且切到 `apps/*/urls.py` 就等于把接线文件藏起来。现在的降级顺序是：
      ① `keep_full` 里的文件（报告点到 / 上轮改过 / 上轮 NEED）**永远全文**；
      ② 受保护文件（urls/views/models/settings/asgi）**永远全文**；
      ③ 其余按体量从大到小降级成**签名版**（保留 import/类/签名，函数体变 `...`）；
      ④ 非 .py 的（html/drl/sql）在没有签名时整份丢弃 —— 但记进 stats。
    架构信息（有这个文件、有哪些函数、签名是什么）**永不丢**。
    """
    ws = Path(ws)
    if not limit:
        limit = budget_chars()
    keep = {str(x).replace("\\", "/") for x in (keep_full or ())}
    files: dict[str, str] = {}
    for p in sorted(ws.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(ws).as_posix()
        if _skip(rel) or p.suffix.lower() not in KEEP_EXT:
            continue
        try:
            files[rel] = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
    total = sum(len(v) for v in files.values())
    # **稳定序**（2026-11-15）：provider 的前缀缓存只在"前缀逐字相同"时命中。
    # 按路径排序时，任何一个文件变了，它后面的全部前缀就废了
    # （实测 whole_repo 命中率只有 2.2%）。所以把**本轮没变的文件排前面**，
    # 变了的排最后 —— 没变的部分就成了长且稳定的前缀。
    order, changed = _stable_order(ws, files)
    stats = {"files": len(files), "chars": total, "truncated": [],
             "signatures_only": [], "dropped": [], "skipped_big": [],
             "order": order, "changed": changed}
    if total <= limit:
        return files, stats

    def _protected(rel: str) -> bool:
        return rel.endswith(PROTECT_SUFFIX)

    for rel in sorted(list(files), key=lambda k: -len(files[k])):
        if total <= limit:
            break
        if rel in keep or _protected(rel):
            continue
        body = files[rel]
        if rel.endswith(".py"):
            stub = _stub_text(rel, body)
            if len(stub) >= len(body) - 100:
                continue
            files[rel] = stub
            total -= len(body) - len(stub)
            stats["signatures_only"].append(rel)
        else:
            total -= len(files.pop(rel))
            stats["dropped"].append(rel)
    stats["chars"] = sum(len(v) for v in files.values())
    stats["truncated"] = [("signatures", r) for r in stats["signatures_only"]]
    # 受保护文件/keep_full 是**永不降级**的，所以极端情况下仍可能超预算。
    # 这时不切它们，而是如实标出来（下一轮 NEED/报告会自然收缩）。
    stats["over_budget"] = stats["chars"] > limit
    return files, stats


def _stable_order(ws, files: dict[str, str]) -> tuple[list[str], list[str]]:
    """(发送顺序, 本轮变了的文件)。没变的在前 → 前缀可命中；变了的在后。"""
    import hashlib
    import json as _json
    p = Path(ws) / HASH_FILE
    prev: dict[str, str] = {}
    try:
        prev = _json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:                                    # noqa: BLE001
        prev = {}
    now = {rel: hashlib.md5(src.encode("utf-8", "replace")).hexdigest()
           for rel, src in files.items()}
    changed = sorted([r for r in files if prev.get(r) != now[r]])
    # 排序键用**文件 mtime**（客观、跨轮稳定），不用"本轮是否改动"。
    # 为什么：如果用 changed 分组，上一轮改过的文件这一轮又变回"未改动"、
    # 从尾部弹回字母序位置 —— 前缀在第 37k 字符处就断了（实测 13.4%）。
    # mtime 只会单调前进：改过的文件永远留在尾部，而且先改的在前、后改的在后。
    mt: dict[str, float] = {}
    for rel in files:
        try:
            mt[rel] = (Path(ws) / rel).stat().st_mtime
        except OSError:
            mt[rel] = 0.0
    unchanged = sorted(files, key=lambda r: (mt.get(r, 0.0), r))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(now, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return unchanged + changed, changed


def map_text(ws, files: dict[str, str], stats: dict | None = None) -> str:
    """架构地图：文件规模 + 函数签名 + 依赖边 + 端点表。

    为什么地图比源码值钱：实测本项目源码 26 万字符，签名清单只有 2.4 万（9%）。
    超预算时"发地图 + 按需全文"能把架构 100% 保留，而截断只能保留 60%。
    """
    rows = ["## 文件规模（路径 / 行数 / 函数 / 空桩）"]
    for rel in sorted(files):
        src = files[rel]
        n_f, n_s = _sig_lines(src)[1:] if rel.endswith(".py") else (-1, -1)
        rows.append("%-52s %5d 行  函数%-5s 空桩%s"
                    % (rel, src.count("\n") + 1, n_f, n_s))
    rows.append("")
    rows.append("## 函数签名（每个函数都在，只是没有函数体）")
    for rel in sorted(files):
        if not rel.endswith(".py"):
            continue
        sigs, _nf, _ns = _sig_lines(files[rel])
        if not sigs:
            continue
        rows.append("### %s" % rel)
        rows.extend(sigs)
    rows.append("")
    rows.append("## 依赖边（文件 -> 它 import 的仓内模块）")
    for rel in sorted(files):
        if not rel.endswith(".py"):
            continue
        deps: list[str] = []
        for ln in files[rel].splitlines()[:60]:
            t = ln.strip()
            if t.startswith("from apps.") or t.startswith("import apps."):
                mod = t.split()[1].split(" import")[0]
                deps.append(mod.replace(".", "/") + ".py")
            elif t.startswith("from .") and " import " in t:
                deps.append("（相对导入）" + t[:60])
        if deps:
            rows.append("%s -> %s" % (rel, ", ".join(sorted(set(deps))[:8])))
    # 端点表优先取**蓝图探针**（那是判据真正在发的请求），比猜函数名可靠。
    eps: list[tuple[str, str, str]] = []
    try:
        from . import blueprint as _bpmod
        _bp = _bpmod.load(Path(ws))
        for _c in (getattr(_bp, "caps", None) or []):
            for _st in ((getattr(_c, "probe", None) or {}).get("steps") or []):
                if isinstance(_st, dict) and _st.get("path"):
                    eps.append((str(_st.get("method") or "GET").upper(),
                                str(_st["path"]),
                                str(_st.get("expect_status") or "-")))
    except Exception:                                    # noqa: BLE001
        eps = []
    if eps:
        rows.append("")
        rows.append("## 端点表（判据真正在发的请求）")
        seen_ep: set = set()
        for m, path, code in eps:
            k = (m, path)
            if k in seen_ep:
                continue
            seen_ep.add(k)
            rows.append("%-6s %-46s 期望 %s" % (m, path, code))
    try:
        from . import routefuncs as _rf
        rf = _rf.from_workspace(Path(ws)) or {}
    except Exception:                                    # noqa: BLE001
        rf = {}
    if rf:
        rows.append("")
        rows.append("## 端点表（端点 -> 处理函数）")
        owner: dict[str, str] = {}
        for rel, src in files.items():
            if rel.endswith(".py"):
                for ln in src.splitlines():
                    t = ln.strip()
                    if t.startswith("def ") or t.startswith("async def "):
                        nm = t.split("(")[0].split()[-1]
                        owner.setdefault(nm, rel)
        for path, func in sorted(rf.items())[:200]:
            rows.append("%-46s -> %s::%s" % (path, owner.get(func, "?"), func))
    if stats and stats.get("signatures_only"):
        rows.append("")
        rows.append("## 以下文件**只发了签名**（要全文：`=== NEED: <路径> ===`）")
        for rel in stats["signatures_only"][:80]:
            rows.append("- %s" % rel)
    return "\n".join(rows)


def tree_text(ws, files: dict[str, str]) -> str:
    """目录树 + 每文件行数/函数数/空桩数（给远端一张"地图"）。"""
    rows: list[str] = []
    for rel in sorted(files):
        src = files[rel]
        lines = src.count("\n") + 1
        if not rel.endswith(".py"):
            rows.append("%-46s %5d 行" % (rel, lines))
            continue
        _sigs, n_f, n_s = _sig_lines(src)
        rows.append("%-46s %5d 行  函数%-5s 空桩%s"
                    % (rel, lines, n_f, n_s))
    return "\n".join(rows)


# ---------------------------------------------------------------- 2. 提示词
BRIEF_HEAD = """你是这个仓库的**唯一负责人**。本轮你做一次**全仓会诊 + 全仓改造**。

## 你的权限（看整仓，但每轮只交 @MAXFILES@ 个文件）
- 你看到的是**整个仓库**，诊断时可以跨文件找共同因 —— 但**每轮最多提交
  @MAXFILES@ 个被改的文件**（`=== FILE:` 与 `=== DELETE:` 块加起来算）。
  超出的部分本地会**拒收并顺延到下一轮**：这不是你做错了，而是刻意小步 ——
  实测一次改 6~13 个文件会"改对 4、改坏 4"，护栏整批回滚后一条能力都没多。
- 所以这一轮**只挑最关键、最有把握改对的 @MAXFILES@ 个文件**，把它们在**本轮
  彻底改对**（ast 能过、不弄红已通过的能力）；其余根因写进 `<rootcause>`，
  下一轮接着来。改透 @MAXFILES@ 个，胜过改一半的 8 个。
- 报告里点到的位置只是**线索**，不是权限边界 —— 但仍受上面的 @MAXFILES@ 约束。
- 唯一不许动的：`.sixcoder/` 下的引擎账本与锁定的全量契约
  （`.sixcoder/full_contract.txt`）。契约里的**函数名与签名**是已锁定的接口，
  改它会让整轮缓存失效并让判据漂移；确实必须改接口时，在 `<rootcause>` 里
  单独写一段 `CONTRACT-CHANGE:` 说明，本轮**仍然按原签名实现**。

## 本轮必须按这个顺序做（禁止直接开始改代码）
1) **归因**：把报告里的问题归到**共同的深层因**。常见深层因（都不是"某一行写错"）：
   - 入口/工程配置错（settings、ASGI/WSGI 暴露名、INSTALLED_APPS、URL include）
   - 契约与实现**命名漂移**（同一函数三轮三个名字、路由末段与函数名对不上）
   - **返回形状不统一**（有的返回 `{"data": ...}`、有的直接返回对象；状态码塞在 body 里）
   - 路由与函数签名不匹配（路由只能给 `authorization: str`，业务却要 `user` 对象）
   - 数据模型字段与断言不符（表里没有那个字段、类型不对、必填/可空相反）
   - 同一资源被两个模块各实现一份（改一处、另一处还是旧的）
   给出 1–3 条根因，每条都要写清：**它解释了报告里的哪些编号**、涉及哪些文件。
2) **分级**：哪些症状会在根因修掉后**自动消失**（→ 不要单独改）；
   哪些是孤立问题（→ 最后单独改）。
3) **动手**：先修根因、再改孤立问题，但受"每轮最多 @MAXFILES@ 个文件"硬约束；
   一轮只交 @MAXFILES@ 个，剩下的根因/孤立问题按优先级留到下一轮。
4) 改完自检：每个改过的 `.py` 必须能 `ast.parse`；跨模块 import 必须真的存在；
   不许留 `NotImplementedError` / `TODO` / `pass  # 占位`（除非契约明确要求抽象基类）。

## 输出格式（严格遵守，解析器按字面读）
先用 `<rootcause>` 段写诊断，然后**每个被改的文件给整文件内容**（不是 diff、不是片段）：

<rootcause>
RC1: …（解释了 E-003 / E-017 / E-025）→ 涉及 apps/x/views.py、config/asgi.py
RC2: …
自动消失: E-017, E-025（RC1 修完即好，不要单独改）
孤立: E-031（与上面无关，单独修）
</rootcause>

=== FILE: apps/xxx/views.py ===
（这个文件的完整内容，从第一行到最后一行）
=== FILE: config/settings.py ===
（完整内容）
=== DELETE: apps/legacy_gone.py ===
=== DONE ===

规则：
- `=== FILE: 路径 ===` 里的路径必须与仓库里的相对路径**逐字一致**（用 `/`）。
- 给整文件；**不许省略**（不许写 `# ... 其余不变 ...`）——解析器会整文件覆盖。
- 只输出**你改过的**文件。没改的不要输出（省 token，也免得覆盖别人）。
- `apps/<域>/urls.py` 里的「引擎补的路由」块由**引擎每轮重渲染**：可以在它上面
  加作者区路由，但**不要删除或改动那个块**（删了这次的 urls.py 会被拒收）。
  发现"端点接到了错的函数 / 前缀挂错"，写成 `WIRING-BUG: <端点> 接到了 <函数>，
  应为 <函数/模块>` 放到 `<rootcause>` 里，由引擎修接线层 —— 那比你在 urls.py 里
  手接更可靠（下轮重渲染会覆盖手写部分）。
- 快照里若某个文件**只给了签名**（行首有「只发了签名」说明），需要全文就在回复里写
  `=== NEED: <路径> ===`，下一轮会全文发给你。
- 你认为整仓已经没有需要改的地方时，只输出 `<rootcause>` + `=== DONE ===`。

## 可选：点名本地验证器（你点菜，本地跑，本地判）
你刚改完十几个文件，**你最清楚该拿哪把尺子量**。想额外验证就在回复里加一段：
<verify>
RUN: py.mypy
RUN: py.migrate
WHY: RC1 牵涉类型边界与模型字段漂移，这两条能证伪我的假设
</verify>
只能点下面的菜单（白名单，写别的会被拒）；不点就只跑常规检测器。**判据由本地执行并判定，不要自己下结论**。
@MENU@
"""

BRIEF_TAIL = """
## 硬约束（违反其一，本轮改动会被整批回滚）
1. 不许为了让判据变绿而删测试、改断言、改探针、改 `.sixcoder/` 里的账本。
2. 不许降低实现换取"能跑"（返回空 list、吞异常、`except: pass`）。
3. 不许改已锁定的公开函数名/签名（见上）。
4. 你要修的是**这个仓库真实存在的错误**；报告说你没写过的函数时，先确认它是否
   存在于别的模块（可能是落点漂移），而不是凭空新建重复实现。
5. **报告里"已经通过的能力"是红线**：不许为了让新能力变绿而把它们弄红。
   修一个、红两个是**净亏损**，比不改更糟。必须动它们所在文件时，在 `<rootcause>`
   里写清"为什么不会影响它们"；如果做不到，就不要动那个文件。
6. 报告里标注"你上一轮弄红的能力"时，**先修回它们**，再谈新能力。
"""


def brief(ws, rep, *, files: dict[str, str], stats: dict,
          history: list[dict] | None = None, round_no: int = 1,
          total_rounds: int = 1, extra: str = "",
          verify_result: dict | None = None) -> str:
    """拼提示词：权限 → 验证器菜单 → 地图 → 全仓源码 → 报告 → 历史根因 → 顺序与格式。"""
    from . import validators as _val

    rep_txt = rep.render(limit_per_level=60) if rep is not None else "（无报告）"
    verify_txt = _val.render_results(verify_result or {})
    hist_txt = ""
    if history:
        rows = []
        for h in history[-4:]:
            rows.append("- 第 %s 轮结论：%s" % (h.get("round", "?"),
                                              str(h.get("text") or "")[:1200]))
        hist_txt = ("\n## 前几轮你已经下过的根因结论（不要重复分析；如果本轮证据推翻它，"
                    "明确写“推翻 RCx：原因”）\n" + "\n".join(rows) + "\n")
    # **顺序即缓存**：稳定不变的部分（规则/菜单）放最前，易变的部分（轮次/报告/根因）
    # 放最后。以前"第 N 轮"紧跟在开头的规则后面，一句话就把后面全部前缀废掉了。
    parts = [BRIEF_HEAD.replace("@MENU@", _val.menu())]
    # **顺序即缓存**（2026-11-15 实测）：仓库地图里每行都有"行数/函数数"，
    # 每轮必变 —— 它原来紧跟在规则后面（第 2830 字符），于是后面 27 万字符
    # 的前缀全部失效（命中率 1.0%，实测 90,207 输入只有 896 命中）。
    # 现在：源码（稳定序）紧跟规则，地图/报告/轮次一律放到后面。
    # 这个标题**必须逐字恒定**：它后面紧跟 27 万字符源码，
    # 一旦标题里带"文件数/字符数"，文件一改就会出现差异 → 整个前缀失效
    # （实测：标题里带字符数时公共前缀只有 2849 字符 = 1.0%）。
    parts.append("\n## 全仓源码（按稳定序：未改动的在前、本轮改动的在后）\n")
    _order = (stats or {}).get("order") or sorted(files)
    for rel in _order:
        if rel in files:
            parts.append("=== FILE: %s ===\n%s\n" % (rel, files[rel].rstrip("\n")))
    if verify_txt:
        parts.append("\n" + verify_txt)
    _rej = load_reject(ws)
    if _rej:
        parts.append("\n## ⚠ 上一轮你交的文件被**拒收**了（这些文件本轮仍是你改之前的样子）\n")
        for it in _rej[:10]:
            parts.append("- `%s`：%s\n" % (it.get("path"), str(it.get("why"))[:160]))
        parts.append("看每条的具体原因：若是语法/结构问题（中文标点混进代码、缩进错、"
                     "括号未闭合）先修好再交；若写的是「文件数超限/顺延」，那是本轮名额限制，"
                     "这些文件没被写入、**下一轮请优先把它们交上来**。\n")
    # 回滚独立成段：语义是"你白改了、别重做"，不是"文件被拒收"——
    # 混在拒收框里会被当噪声（实测远端重复同一个无效改动 5 轮）。
    _rb_txt = render_rollback(load_rollback(ws))
    if _rb_txt:
        parts.append(_rb_txt)
    # 附录：地图/树放这里（导航用，且每轮会变，放前面会毁掉整个前缀缓存）
    if stats.get("signatures_only") or stats.get("dropped"):
        parts.append("\n## 架构地图（附录：完整签名 + 依赖边 + 端点表）\n```\n%s\n```\n"
                     % map_text(ws, files, stats))
    parts.append("\n## 仓库地图（附录：路径 行数 函数数 空桩数）\n```\n%s\n```\n"
                 % tree_text(ws, files))
    parts.append("\n## 错误报告（本地检测器）\n%s\n" % rep_txt)
    if hist_txt:
        parts.append(hist_txt)
    if extra:
        parts.append("\n## 附加说明\n%s\n" % extra)
    parts.append("\n## 本轮位置\n第 %d/%d 轮。检测器是本地跑的（零 token），"
                 "报告是**整仓**的，包含 P0–P3 全部级别。\n"
                 % (round_no, total_rounds))
    parts.append(BRIEF_TAIL)
    text = "".join(parts)
    # 小步限制写死进提示词（单一真相来源 = MAX_FILES_PER_ROUND）；None 时说"不限"。
    cap = globals().get("MAX_FILES_PER_ROUND", 2)
    text = text.replace("@MAXFILES@", str(cap) if cap else "不限（但仍要稳）")
    return text


# ---------------------------------------------------------------- 3. 解析
def parse(text: str) -> dict:
    """解析回复：文件块 / 删除块 / 根因 / DONE。

    宽松但不含糊：`=== FILE: x ===` 之后到下一个 `=== ` 标记之间的**全部内容**
    就是这个文件（整文件语义，不做 diff 合并）。兼容 ```file:path``` 围栏。
    """
    t = str(text or "")
    files: dict[str, str] = {}
    deletes: list[str] = []
    lines = t.splitlines()
    cur: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur is not None:
            body = "\n".join(buf)
            if body.strip():
                files[cur] = body if body.endswith("\n") else body + "\n"

    import re
    for ln in lines:
        m = re.match(FILE_MARK_RE, ln)
        if m:
            flush()
            cur, buf = m.group("path").replace("\\", "/").strip(), []
            continue
        m = re.match(DEL_MARK_RE, ln)
        if m:
            flush()
            cur, buf = None, []
            deletes.append(m.group("path").replace("\\", "/").strip())
            continue
        if re.match(r"^\s*===\s*(DONE|END)\s*===\s*$", ln, re.I):
            flush()
            cur, buf = None, []
            continue
        if cur is not None:
            buf.append(ln)
    flush()

    # 兜底：```file:path``` 围栏（远端有时不用 ===）
    if not files:
        try:
            from . import author as _author
            for k, v in (_author.parse_artifact_blocks(t) or {}).items():
                files.setdefault(k.replace("\\", "/").strip(), v)
        except Exception:                                # noqa: BLE001
            pass

    rc = ""
    m = re.search(r"<rootcause>(.*?)</rootcause>", t, re.S | re.I)
    if m:
        rc = m.group(1).strip()
    else:
        m = re.search(r"(?ms)^#{1,3}\s*(根因|common root cause|root ?cause).*?(?=^=== FILE|\Z)", t)
        if m:
            rc = m.group(0).strip()[:4000]
    # 远端"点名"要看全文的文件（架构地图之外的部分）
    needs = []
    for m in re.finditer(r"===+\s*NEED\s*:\s*([^\s=]+)\s*===+", t, re.I):
        needs.append(m.group(1).replace("\\", "/").strip())
    # 远端"点名"要本地跑的验证器（只认白名单，判据由本地执行）
    try:
        from . import validators as _val
        verify = _val.parse_verify(t)
    except Exception:                                    # noqa: BLE001
        verify = {"ids": [], "why": ""}
    return {"files": files, "deletes": deletes, "rootcause": rc,
            "verify": verify, "need": list(dict.fromkeys([x for x in needs if x])),
            "done": MARK_DONE in t or bool(re.search(r"^\s*===\s*DONE\s*===\s*$", t, re.I | re.M)),
            "raw_chars": len(t)}


# ---------------------------------------------------------------- 4. 落盘守卫
def _py_ok(src: str) -> tuple[bool, str]:
    try:
        ast.parse(src)
        return True, ""
    except SyntaxError as exc:
        return False, "第 %s 行语法错误：%s" % (exc.lineno, exc.msg)


def _syntax_failures(ws: Path) -> int:
    bad = 0
    for p in ws.rglob("*.py"):
        rel = p.relative_to(ws).as_posix()
        if _skip(rel):
            continue
        try:
            ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            bad += 1
        except OSError:
            continue
    return bad


def _project_check(ws: Path) -> tuple[bool, str]:
    """Django 工程跑 `manage.py check`；其它工程直接算过（D1 已经兜了语法）。"""
    if not (ws / "manage.py").is_file():
        return True, ""
    try:
        from . import django_wire as _djw
        if not _djw.is_django(ws):
            return True, ""
        return _djw.check_project(ws)
    except Exception as exc:                             # noqa: BLE001
        return True, "跳过工程自检：%s" % type(exc).__name__


def apply(ws, parsed: dict, *, emit=None, allow_delete: bool = True,
          truncation_ratio: float = 0.35) -> dict:
    """把解析结果落盘（带守卫）。**变差就整批回滚。**

    守卫顺序（每一步都能单独失败，失败即回滚）：
      ① 路径必须落在仓库内、不许碰 `.sixcoder/`
      ② 每个新 .py 必须 `ast.parse` 通过（有一个不过 → 整批不写）
      ③ 截断体检：新文件比原文件短到 `truncation_ratio` 以下且原文够大 → 判为
         输出被截断（远端只给了半截），**跳过该文件**（不是整批回滚）
      ④ 写盘 + 删除
      ⑤ 复验：语法失败数、`manage.py check` —— 比改前差 → 回滚
    """
    ws = Path(ws)
    files = dict(parsed.get("files") or {})
    deletes = list(parsed.get("deletes") or [])
    out = {"written": [], "deleted": [], "skipped": [], "ok": False,
           "detail": "", "backup": ""}

    # ① 路径合法性
    safe_files: dict[str, str] = {}
    for rel, body in files.items():
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/") or rel.startswith(".sixcoder/"):
            out["skipped"].append("%s（路径非法/受保护）" % rel)
            continue
        safe_files[rel] = body
    safe_dels = []
    for rel in deletes:
        rel = str(rel).replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/") or rel.startswith(".sixcoder/"):
            out["skipped"].append("删除 %s（路径非法/受保护）" % rel)
            continue
        safe_dels.append(rel)

    # ② 语法先过 —— **按文件拒收，不整批连坐**（2026-11-15 实测修正）：
    # 远端一次交 20 个文件，只要 1 个语法错就整批丢弃 → 19 个文件的活白干、
    # 一轮的钱白花（实测 20:14 那轮就是这么废掉的，还退化成逐单派工）。
    # 语法错的文件本来也过不了 ast，本地不需要它"整批不动"来保护仓库：
    # 逐个拦住即可，好文件照写。
    _bad: list[str] = []
    for rel, body in list(safe_files.items()):
        if rel.endswith(".py"):
            ok, why = _py_ok(body)
            if not ok:
                out["skipped"].append("%s %s → 该文件拒收" % (rel, why))
                out.setdefault("rejected", []).append({"path": rel, "why": why})
                _bad.append(rel)
                safe_files.pop(rel, None)
    if not safe_files and not deletes:
        if not _bad:
            # 远端这一轮**什么都没交**（只给了根因/或只给了 DONE）。
            # 以前这里报"全部文件都没通过语法检查（0 个）"，把自己人骗了：
            # 看着像"交了坏文件"，实际是"没交"。语义不同，后续动作也不同
            # （没交 = 它认为没事做；交坏了 = 要把它拦下来）。
            out["detail"] = "远端本轮没有提交任何文件（只给了说明）→ 不算失败"
            out["no_files"] = True
            out["ok"] = True
            return out
        out["detail"] = "全部文件都没通过语法检查（%d 个）→ 本轮不写入" % len(_bad)
        return out

    # ③ 截断体检 + 引擎块保护
    keep: dict[str, str] = {}
    for rel, body in safe_files.items():
        p = ws / rel
        try:
            old = p.read_text(encoding="utf-8-sig", errors="replace") if p.is_file() else ""
        except OSError:
            old = ""
        # 引擎块保护（2026-11-15 实测）：`apps/*/urls.py` 里的路由块由引擎每轮
        # 重渲染。远端整仓重写 urls.py 时若把块删了，下一轮接线会把整个文件重算，
        # 它的作者区也可能一起被绕开 —— 这种改动直接拒收（保留旧文件），
        # 要求它把接线问题写成 WIRING-BUG 交给引擎。
        try:
            from .django_wire import MARK_BEGIN as _MB
        except Exception:                                # noqa: BLE001
            _MB = ""
        if _MB and _MB in old and _MB not in body:
            out["skipped"].append("%s（把「引擎补的路由」块删掉了 → 拒收，接线问题请写 WIRING-BUG）"
                                  % rel)
            continue
        if old and len(old) > 800 and len(body) < len(old) * truncation_ratio:
            out["skipped"].append("%s（新版只有原文的 %d%%，判定输出被截断）"
                                  % (rel, int(100 * len(body) / max(1, len(old)))))
            continue
        keep[rel] = body
    if not keep and not safe_dels:
        out["detail"] = "没有可落盘的改动（全部跳过）"
        return out

    # ③.8 小步硬截断（2026-11-16 用户定稿）：一轮最多改 MAX_FILES_PER_ROUND 个
    # 文件（写 + 删合计）。这是**写盘层**硬闸，不依赖远端自觉遵守提示词。
    # 放在语法检查(②)/截断体检(③)之后：坏文件、判为输出被截的文件本就不写，
    # 不该白占名额。按远端提交顺序取前 cap 个（keep 保序 = 远端 FILE 块顺序，
    # 写优先于删），其余**按文件拒收**并记入 rejected → session() 会 save_reject
    # → 下一轮提示词点名"这几个文件顺延，优先交它们"，不静默丢、不整批连坐。
    _cap = globals().get("MAX_FILES_PER_ROUND", 2)
    if _cap:
        _ordered = [("写", r) for r in keep] + [("删", r) for r in safe_dels]
        if len(_ordered) > _cap:
            for _kind, _rel in _ordered[_cap:]:
                _why = ("本轮文件数超限（你交了 %d 个，硬上限 %d）——刻意小步，不是错误。"
                        "该文件未落盘，**下一轮优先交它**（只交到 %d 个为止）。"
                        % (len(_ordered), _cap, _cap))
                out["skipped"].append("%s（%s）" % (_rel, _why))
                out.setdefault("rejected", []).append({"path": _rel, "why": _why})
                if _kind == "写":
                    keep.pop(_rel, None)
                else:
                    safe_dels.remove(_rel)
            _emit(emit, "whole_step_cap", {"limit": _cap, "submitted": len(_ordered),
                                           "written": len(keep), "deleted": len(safe_dels)})

    # ③.5 快照
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = ws / ".sixcoder" / "snapshots" / ("%s-whole" % stamp)
    touched = sorted(set(list(keep) + [d for d in safe_dels]))
    manifest: dict[str, str] = {}
    for rel in touched:
        p = ws / rel
        if p.is_file():
            dst = bak / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                dst.write_text(p.read_text(encoding="utf-8-sig", errors="replace"),
                               encoding="utf-8")
                manifest[rel] = "existed"
            except OSError:
                manifest[rel] = "unreadable"
        else:
            manifest[rel] = "absent"
    if manifest:
        bak.mkdir(parents=True, exist_ok=True)
        (bak / "_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
        out["backup"] = str(bak)

    before = _syntax_failures(ws)
    _check_before, _ = _project_check(ws)

    # ④ 写
    for rel, body in keep.items():
        p = ws / rel
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body, encoding="utf-8")
            out["written"].append(rel)
        except OSError as exc:
            out["skipped"].append("%s（写不了：%s）" % (rel, exc))
    if allow_delete:
        for rel in safe_dels:
            p = ws / rel
            if p.is_file():
                try:
                    p.unlink()
                    out["deleted"].append(rel)
                except OSError as exc:
                    out["skipped"].append("删除 %s 失败：%s" % (rel, exc))

    # ⑤ 复验
    after = _syntax_failures(ws)
    ok_chk, chk_detail = _project_check(ws)
    worse = after > before or (not ok_chk)
    if worse:
        _restore(ws, bak, manifest)
        out["detail"] = ("复验变差（语法失败 %d→%d；工程自检 %s）→ 已整批回滚：%s"
                         % (before, after, "通过" if ok_chk else "失败",
                            (chk_detail or "")[:200]))
        _emit(emit, "whole_rollback", {"reason": out["detail"], "backup": out["backup"]})
        out["written"], out["deleted"] = [], []
        return out

    out["ok"] = True
    out["detail"] = "写入 %d 个、删除 %d 个（语法失败 %d→%d）" % (
        len(out["written"]), len(out["deleted"]), before, after)
    return out


def _restore(ws: Path, bak: Path, manifest: dict[str, str]) -> int:
    """按快照回滚：存在过的写回、原本不存在的删掉。"""
    n = 0
    ws = Path(ws)
    for rel, state in manifest.items():
        src = bak / rel
        dst = ws / rel
        try:
            if state == "existed" and src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
                n += 1
            elif state == "absent" and dst.is_file():
                dst.unlink()
                n += 1
        except OSError:
            continue
    return n


def restore_from_backup(ws, bak) -> int:
    """按快照目录里的 `_manifest.json` 整批回滚（回归护栏用）。"""
    import json as _json
    bak = Path(bak)
    try:
        manifest = _json.loads((bak / "_manifest.json").read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return 0
    return _restore(Path(ws), bak, manifest)


def _app_of(rel: str) -> str:
    """文件 -> 所属 app 目录（apps/x/... -> apps/x）。"""
    parts = str(rel).replace("\\", "/").split("/")
    return "/".join(parts[:2]) if len(parts) >= 2 and parts[0] == "apps" else (parts[0] if parts else "")


def split_unlocked(regressed: list[str],
                   unlock: dict | None) -> tuple[list[str], list[str]]:
    """把回归清单按解锁范围切分成 (仍要回滚的, 解锁范围内留证的)。

    解锁 ≠ 通过：范围内红**不触发回滚**但照记——它们是三方复审的证据。
    范围外照旧立即回滚，解锁绝不放行无关改动。
    """
    if not unlock:
        return list(regressed or []), []
    scoped = set(unlock.get("caps") or [])
    in_scope = [c for c in (regressed or []) if c in scoped]
    out_scope = [c for c in (regressed or []) if c not in scoped]
    return out_scope, in_scope


def regression_check(ws, written: list[str], *, passed: list[str] | None = None,
                     emit=None, only_touched_apps: bool = False) -> dict:
    """**回归护栏**：改完之后，把"已通过且与本次改动相关"的能力当场重测一遍。

    为什么必须有（2026-11-15 实测）：整仓一轮改 5-12 个文件，改的又是
    views/urls 这种全局连接点，必然踩坏别的能力。而发现渠道是"下一轮全量观测"，
    代价是**整整一轮（10 分钟 + ¥0.2）**，而且这中间它还会基于坏状态继续改。
    所以：改完立刻测，红了就整批回滚 —— 宁可这轮不改，
    也不要把仓库从"27% 能跑"改成"23% 能跑"。

    `only_touched_apps` 默认 **False**（2026-11-15 23:46 实测修正）：
    默认只测"落点重叠"的能力时，本轮检查了 10 条全绿，但覆盖照样从 31.8%
    掉到 28.2% —— 因为掉的那几条的 `modules` 字段跟本次改动没交集
    （共享工具层/接线层一改，影响面比 modules 字段写的大）。
    护栏漏检比慢一点更糟，所以默认**全测已通过的**（20-25 条，约 1-2 分钟）。
    """
    out = {"checked": [], "regressed": [], "detail": ""}
    passed = list(passed if passed is not None else load_passed(ws))
    if not passed or not written:
        return out
    try:
        from . import blueprint as _bpmod
        from . import openhouse as _oh
        bp = _bpmod.load(Path(ws))
    except Exception as exc:                             # noqa: BLE001
        out["detail"] = "护栏跳过（拿不到蓝图）：%s" % type(exc).__name__
        return out
    touched = {_app_of(r) for r in written}
    targets: list[str] = []
    for _c in (getattr(bp, "caps", None) or []):
        cid = str(getattr(_c, "id", "") or "")
        if cid not in passed:
            continue
        mods = {_app_of(str(m)) for m in (getattr(_c, "modules", None) or [])}
        if only_touched_apps and mods and not (mods & touched):
            continue
        targets.append(cid)
    if not targets:
        out["detail"] = "本次改动不涉及已通过能力的落点，无需护栏"
        return out
    try:
        cov = _oh.observe(Path(ws), bp, emit=None, only=set(targets))
    except Exception as exc:                             # noqa: BLE001
        out["detail"] = "护栏跳过（观测失败）：%s" % type(exc).__name__
        return out
    caps = (cov or {}).get("caps") or {}
    regressed = [cid for cid in targets
                 if str((caps.get(cid) or {}).get("status") or "") != "done"]
    # **解锁范围**（2026-11-18 死局协商）：死局三方多数决通过后，范围内的能力红
    # 不判回归（不触发整批回滚），但**照记不误** —— 它们是复审的证据，复审不过
    # 照样回滚。解锁不是通过，是"待裁决"。
    try:
        from . import deadlock as _dl
        _unl = _dl.unlock_active(ws)
    except Exception:                                    # noqa: BLE001
        _unl = None
    out_scope, in_scope = split_unlocked(regressed, _unl)
    if in_scope:
        out["unlocked_red"] = in_scope
        out["unlock"] = _unl
    regressed = out_scope
    out["checked"] = targets
    out["regressed"] = regressed
    out["detail"] = ("检查 %d 条相关能力：%s" %
                     (len(targets), "全部仍然通过" if not regressed
                      else "**%d 条被弄红**：%s" % (len(regressed), ", ".join(regressed[:6]))))
    if out.get("unlocked_red"):
        out["detail"] += ("｜另有解锁范围内 %d 条仍红（不回滚，留证待三方复审）：%s"
                          % (len(out["unlocked_red"]),
                             ", ".join(out["unlocked_red"][:6])))
    if emit:
        try:
            emit("whole_regression", {"checked": len(targets), "regressed": regressed,
                                      "detail": out["detail"]})
        except Exception:                            # noqa: BLE001
            pass
    return out


# ---------------------------------------------------------------- 报告适配
class SimpleReport:
    """给 `brief()` 用的最小报告对象（只要 `render` / `render_short`）。

    为什么不用 `errorreport.Report`：夜班主循环（`company.night`）用的是
    "工作单 + 能力覆盖"的事实，而不是 D1–D8 的 Report。硬造一个 Report 会
    把两套判据搅在一起；只要形状对（能 render 出给人看的文本），
    提示词那边不需要知道来源。
    """

    def __init__(self, text: str, short: str = "") -> None:
        self._text = text or "（无错误）"
        self._short = short or ""

    def render(self, limit_per_level: int = 60) -> str:      # noqa: ARG002
        return self._text

    def render_short(self) -> str:
        return self._short


def _cap_assertion(cap: dict) -> str:
    """抽一条能力**到底断言了什么行为**（方法/路径/状态码/字段）。

    只取行为契约，不取探针的用户名/密码等实现细节 —— 远端要保住的是
    "POST /x 必须返回 403 + detail=权限不足"，不是"用 u_cap_xxx 这个账号"。
    """
    steps = ((cap.get("probe") or {}).get("steps") or [])
    key = [s for s in steps if isinstance(s, dict) and s.get("expect_status") is not None]
    if not key:
        return ""
    s = key[-1]
    txt = "%s %s → %s" % (s.get("method", "?"), s.get("path", "?"),
                           s.get("expect_status", "?"))
    ej = s.get("expect_json") or {}
    if isinstance(ej, dict) and ej:
        fields = ["%s=%s" % (k, str(v)[:24]) for k, v in list(ej.items())[:3]]
        txt += "（字段 " + ", ".join(fields) + "）"
    return txt


def cap_contract_hint(ws, passed: list[str]) -> str:
    """把"已绿能力"从**ID 列表**升级为**行为契约 + 命门文件点名**。

    2026-11-16 实测根因：远端每轮主修 accounts/views.py / topics/views.py
    （19 条红灯的共同因），可这两个文件同时是 4 条已绿能力的命门 ——
    改完必红 → 护栏整批回滚 → 净增长为零。护栏是对的，错在远端**只知道能力 ID、
    不知道这些能力具体断言了什么行为**。这里把行为断言喂给它。

    读不到 blueprint 时返回空串：报告退回只列 ID（不炸、不误导）。
    """
    ids = [str(x) for x in (passed or [])]
    if not ids or ws is None:
        return ""
    try:
        from . import blueprint as _bp
        caps = {}
        for c in (_bp.load(ws).caps or []):
            d = c.to_dict()
            caps[str(d.get("id"))] = d
    except Exception:                                    # noqa: BLE001
        return ""

    lines: list[str] = []
    mod_map: dict[str, list[str]] = {}
    for cid in ids:
        c = caps.get(cid)
        if not c:
            continue
        desc = str(c.get("desc") or "")[:110]
        mods = [str(m) for m in (c.get("modules") or [])]
        assertion = _cap_assertion(c)
        lines.append("- **%s**%s" % (cid, (" — " + desc) if desc else ""))
        if mods:
            lines.append("    依赖文件：%s" % ", ".join(mods[:6]))
            for m in mods:
                mod_map.setdefault(m, []).append(cid)
        if assertion:
            lines.append("    必须保持：%s" % assertion)
    missing = [cid for cid in ids if cid not in caps]
    if missing:
        lines.append("- 其它（blueprint 无契约，仍需保持绿）：" + ", ".join(missing[:20]))
    if not lines:
        return ""

    # 命门文件：被 ≥2 条已绿能力依赖 —— 改它一次就可能同时弄红多条，
    # 护栏整批回滚（实测每轮 ¥1 买一次回滚、零增长）。按承载数降序，最多点 8 个。
    hot = sorted(((m, cids) for m, cids in mod_map.items() if len(cids) >= 2),
                 key=lambda x: (-len(x[1]), x[0]))[:8]
    head = ("下面每条都列了**它具体断言的行为**（方法/路径/状态码/字段）。"
            "改依赖文件时这些行为必须保持，否则本轮整批回滚。")
    if hot:
        head += "\n\n### ⚠ 命门文件（改一次会同时威胁多条已绿能力）"
        for m, cids in hot:
            head += ("\n- `%s`：承载 %d 条已绿能力（%s）。"
                     "动它前必须在 `<rootcause>` 逐条写清为什么这些断言不变；"
                     "做不到就**别动这个文件**，先改别的。"
                     % (m, len(cids), ", ".join(cids[:5]) +
                        ("…" if len(cids) > 5 else "")))
    return head + "\n" + "\n".join(lines) + "\n"


def report_from_state(*, cap_cov: dict | None = None, cap_detail: dict | None = None,
                      state: dict | None = None, triage: dict | None = None,
                      orders: list | None = None, passed: list | None = None,
                      prev_passed: list | None = None, ws=None,
                      descoped: list | None = None) -> SimpleReport:
    """把夜班当轮的事实拼成"给远端看的报告"。

    三段（按修复价值排序，不是按级别）：
      ① 端到端能力失败（每条 = 一个真实请求真的红了，附失败证据）
      ② 模块级未验证函数（契约层没过的桩/未实现）
      ③ 分诊结论（哪些是本地可修/环境问题 —— 让远端别白花力气）
    """
    lines: list[str] = []
    caps = (cap_cov or {}).get("caps") or {}
    bad = [(cid, v) for cid, v in caps.items()
           if str((v or {}).get("status") or "") not in ("done", "descoped")]
    if bad:
        lines.append("## ① 端到端能力失败（%d 条 / 共 %d 条）"
                     % (len(bad), len(caps)))
        for cid, v in sorted(bad):
            lines.append("")
            lines.append("### %s — %s" % (cid, str(v.get("status") or "?")))
            det = str(v.get("detail") or "").strip()
            for i, ln in enumerate(det.splitlines()[:6]):
                lines.append(("  证据：%s" if i == 0 else "        %s") % ln[:300])
    elif caps:
        lines.append("## ① 端到端能力：全部通过")

    pend = []
    for path, st in (state or {}).items():
        for f in sorted((st or {}).get("stubs") or []):
            pend.append("%s::%s（空桩/未实现）" % (path, f))
    if pend:
        lines.append("")
        lines.append("## ② 模块级未完成（%d 处）" % len(pend))
        for x in pend[:80]:
            lines.append("- %s" % x)

    if triage:
        lines.append("")
        lines.append("## ③ 分诊结论（引擎的判断，可推翻但要说理由）")
        for k in ("dispatch", "local", "env"):
            v = triage.get(k)
            if v:
                lines.append("- %s：%s" % (k, ", ".join(str(x) for x in v)[:600]))

    _done_ids = [str(c) for c in (passed or [])]
    if _done_ids:
        lines.append("")
        lines.append("## ④ **已经通过的能力（红线：不许让它们变红）**（%d 条）"
                     % len(_done_ids))
        lines.append("动这些能力所在的文件时，你必须在 `<rootcause>` 里写清"
                     "「为什么不会影响它们」。修一个新能力、弄红两个旧能力 = 净亏损。")
        _hint = cap_contract_hint(ws, _done_ids)
        if _hint:
            lines.append(_hint)
        else:
            # 读不到 blueprint 时退回原来的 ID 清单（不误导、不炸）
            for cid in _done_ids[:40]:
                lines.append("- %s" % cid)
            if len(_done_ids) > 40:
                lines.append("- …（另 %d 条）" % (len(_done_ids) - 40))
    # 回归 = 上轮通过、这轮**真的红了**。`descoped`（引擎主动放弃的能力，
    # 例如需要前端才能判的 cap.ui.*）不算回归 —— 否则报告会对远端说
    # "你弄红了 3 条"，而它根本没碰那部分（实测 23:04 那轮就是这样误导的）。
    _descoped = {str(x) for x in (descoped or [])}
    _bad = sorted(set(str(x) for x in (prev_passed or [])) - set(_done_ids) - _descoped)
    if _bad:
        lines.append("")
        lines.append("## ⑤ **你上一轮弄红的能力（回归，优先修回）**（%d 条）" % len(_bad))
        for cid in _bad[:40]:
            lines.append("- %s" % cid)
    if orders:
        lines.append("")
        lines.append("## ⑥ 引擎本轮原本要派的工作单（供你判断优先级，不必照做）")
        for o in orders[:12]:
            lines.append("- [%s] %s%s" % (o.get("kind"), o.get("target"),
                                          (":" + str(o.get("symbol")))
                                          if o.get("symbol") else ""))
    short = "① %d 红 / ② %d 桩" % (len(bad), len(pend))
    return SimpleReport("\n".join(lines) or "（无错误）", short)


# ---------------------------------------------------------------- 5. 会话
def load_need(ws) -> list[str]:
    """上一轮远端点名要全文的文件（本轮优先全文发）。"""
    try:
        import json as _json
        data = _json.loads((Path(ws) / NEED_FILE).read_text(encoding="utf-8"))
        return [str(x).replace("\\", "/") for x in (data.get("paths") or []) if x]
    except Exception:                                    # noqa: BLE001
        return []


def save_need(ws, paths: list[str]) -> None:
    import json as _json
    p = Path(ws) / NEED_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not paths:
            if p.is_file():
                p.unlink()
            return
        p.write_text(_json.dumps({"paths": list(dict.fromkeys(paths))[:40],
                                  "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def save_reject(ws, items: list[dict]) -> None:
    """把"上一轮哪些文件被拒收、为什么"落盘 —— 下一轮必须告诉远端。

    为什么必须有（2026-11-15 23:12 实测）：远端交 1 个文件、里面有中文顿号
    → 守卫拒收 → **整轮白费**，而它下一轮根本不知道自己被拒过，会原样再交一次。
    拒收信息不回流，等于每轮都在重复同一个错。
    """
    import json as _json
    p = Path(ws) / REJECT_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not items:
            if p.is_file():
                p.unlink()
            return
        p.write_text(_json.dumps(items[:20], ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_reject(ws) -> list[dict]:
    try:
        import json as _json
        data = _json.loads((Path(ws) / REJECT_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:                                    # noqa: BLE001
        return []


# --- 回滚反馈：与"文件拒收"分开存/分开渲染（2026-11-18 血泪）-------------------
# 旧的回滚被塞进 reject 文件，用"文件被拒收"的框渲染，假路径 + 语法/超限话术，
# 远端把它当噪声忽略 → 同一个无效改动重复 5 轮 → 护栏每次回滚 → 振荡叫停。
# 回滚是"你这次白改了、别再这么做"，语义完全不同，必须独立成段、说人话。

def save_rollback(ws, info: dict | None) -> None:
    """落盘"上一轮整批回滚"的反馈；传 None 清空（本轮没回滚就不该继续显示）。"""
    import json as _json
    p = Path(ws) / ROLLBACK_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not info:
            if p.is_file():
                p.unlink()
            return
        p.write_text(_json.dumps(info, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_rollback(ws) -> dict | None:
    try:
        import json as _json
        data = _json.loads((Path(ws) / ROLLBACK_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and data else None
    except Exception:                                    # noqa: BLE001
        return None


def render_rollback(info: dict | None) -> str:
    """把回滚反馈渲染成独立段落。纯函数，便于 check 钉行为。"""
    if not info:
        return ""
    regressed = [str(x) for x in (info.get("regressed") or [])]
    files = [str(x) for x in (info.get("files") or [])]
    detail = str(info.get("detail") or "")
    out = ["\n## ⛔ 上一轮你的改动被**整批回滚**了（本轮白改，仓库仍是改动前的样子）\n",
           "**原因**：这次改动弄红了 %d 条已通过能力：%s\n"
           % (len(regressed), "、".join(regressed[:8]) or "（未列出）")]
    if files:
        # 2026-11-18：光说"被回滚的文件"挡不住远端——它会在 <rootcause> 里
        # 重新论证"改这个是唯一办法"然后照改（实测连续 3 轮撞同一命门）。
        # 升级成逐文件硬约束：明确"本轮禁止提交对它的改动"，与 BRIEF_TAIL 红线同级。
        out.append("\n### 🚫 本轮禁止提交对以下文件的任何改动（踩过的命门）\n")
        _warn = ("- `%s` —— 上一轮改它被整批回滚；即使你在 `<rootcause>` 里"
                 "论证非改它不可，也**不许交**。想办法在别处达成目标，"
                 "或明确写 `BLOCKED: <原因>` 交 `=== DONE ===` 把名额留下。\n")
        for f in files[:8]:
            out.append(_warn % f)
    if detail:
        out.append("护栏原文：%s\n" % detail[:200])
    out.append("\n**本轮不要重做上次的改动**。先查清「为什么会弄红」，"
               "换一个不碰这些命门的做法；做不到就只输出 `<rootcause>` + `=== DONE ===`。\n")
    return "".join(out)


def save_passed(ws, ids: list[str]) -> None:
    """把"本轮通过的能力"落盘 —— 下一轮用它当**红线**（不许弄红）。"""
    import json as _json
    p = Path(ws) / PASSED_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps({"ids": sorted(ids),
                                  "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                 ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_passed(ws) -> list[str]:
    try:
        import json as _json
        return [str(x) for x in
                (_json.loads((Path(ws) / PASSED_FILE).read_text(encoding="utf-8"))
                 .get("ids") or [])]
    except Exception:                                    # noqa: BLE001
        return []


def load_history(ws) -> list[dict]:
    p = Path(ws) / ROOTCAUSE_FILE
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:                                    # noqa: BLE001
        return []


def save_history(ws, history: list[dict]) -> None:
    p = Path(ws) / ROOTCAUSE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(history[-20:], ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError:
        pass


def session(ws, rep=None, *, emit=None, contracts=None, round_no: int = 1,
            total_rounds: int = 1, max_out: int = 0,
            task_id: str = "", extra: str = "", dry_run: bool = False) -> dict:
    """跑一次整仓会话：采全仓 → 提示词 → 远端 → 解析 → 守坠落盘。

    返回 {ok, written, deleted, skipped, rootcause, done, chars, cost?}
    """
    from . import remote as _remote
    ws = Path(ws)
    # keep_full = 报告点到 + 上轮改过 + 上轮 NEED —— 这三类**必须全文**，
    # 它们是本轮真正要动的东西；其余文件超预算时降级成签名版（架构不丢）。
    _keep: set[str] = set(load_need(ws))
    try:
        for e in (rep.sorted() if rep is not None else []):
            _w = str(getattr(e, "where", "") or "").split(":")[0].strip()
            if _w.endswith(".py") or "." in _w.split("/")[-1]:
                _keep.add(_w.replace("\\", "/"))
    except Exception:                                    # noqa: BLE001
        pass
    for _h in (load_history(ws) or [])[-3:]:
        for _f in (_h.get("files") or []):
            _keep.add(str(_f).replace("\\", "/"))
    files, stats = collect(ws, keep_full=_keep)
    if not files:
        return {"ok": False, "detail": "仓库里没有可采的源码", "written": []}
    if stats.get("signatures_only") or stats.get("dropped") or stats.get("over_budget"):
        _emit(emit, "whole_scale", {
            "signatures_only": len(stats.get("signatures_only") or []),
            "dropped": stats.get("dropped") or [],
            "chars": stats.get("chars"), "over_budget": bool(stats.get("over_budget")),
            "note": ("超预算 → 非关键文件降级为签名版（架构完整、无半截文件）"
                     if stats.get("signatures_only") else "无需降级")})
    history = load_history(ws)
    try:
        from . import validators as _valmod
        _prev_verify = _valmod.load(ws)
    except Exception:                                    # noqa: BLE001
        _prev_verify = {}
    text = brief(ws, rep, files=files, stats=stats, history=history,
                 round_no=round_no, total_rounds=total_rounds, extra=extra,
                 verify_result=_prev_verify)
    _emit(emit, "whole_brief", {"chars": len(text), "files": len(files),
                                "report": (rep.render_short() if rep is not None else "")})
    if dry_run:
        return {"ok": True, "dry_run": True, "chars": len(text), "written": [],
                "brief": text}

    try:
        from . import attribution as _at
        _mo = int(max_out or 0) or max_out_tokens()   # 0 → 跟模型声明的输出上限
        _emit(emit, "whole_budget", {"snapshot_chars": stats.get("chars"),
                                     "brief_chars": len(text),
                                     "max_out_tokens": _mo,
                                     "budget_chars": budget_chars(),
                                     "note": "预算=模型上下文−输出预留−安全垫（不另设缰绳）"})
        with _at.unit(phase="whole_repo"):
            r = _remote.complete(text, task_id=task_id or "whole-repo",
                                 node_id="whole:repo", thinking="on",
                                 max_out=_mo, emit=emit, workspace=str(ws))
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "detail": "远端调用失败：%s: %s"
                % (type(exc).__name__, exc), "written": []}
    if not r.get("ok"):
        return {"ok": False, "detail": "远端返回失败：%s" % (r.get("error") or ""),
                "written": []}
    answer = str(r.get("answer") or "")
    parsed = parse(answer)
    _emit(emit, "whole_reply", {"chars": len(answer), "files": len(parsed["files"]),
                                "deletes": len(parsed["deletes"]),
                                "rootcause": bool(parsed["rootcause"]),
                                "done": parsed["done"]})
    if parsed["rootcause"]:
        history.append({"round": round_no, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "text": parsed["rootcause"][:4000],
                        "files": sorted(parsed["files"]), "done": parsed["done"]})
        save_history(ws, history)
    if parsed.get("need"):
        save_need(ws, parsed["need"])
        _emit(emit, "whole_need", {"paths": parsed["need"][:10],
                                   "note": "下一轮这些文件全文发送"})
    else:
        save_need(ws, [])
    res = apply(ws, parsed, emit=emit)
    # 拒收原因落盘 → 下一轮提示词里"上一轮被拒收的文件"（不让它重复同一个错）
    save_reject(ws, res.get("rejected") or [])
    # 回滚反馈先清空：本轮若没触发回滚，就不该让上一轮的"白改"提示继续挂着。
    save_rollback(ws, None)
    # **回归护栏**：落盘后当场重测"已通过且与本次改动相关"的能力，红了整批回滚。
    # 这是"整仓放肆改"能成立的唯一前提：不允许用一次大改把仓库从能跑改成不能跑。
    _dl_res: dict = {}
    try:
        from . import deadlock as _dlmod
        _unl = _dlmod.unlock_active(ws, round_no)        # 顺带做过期检查
    except Exception:                                    # noqa: BLE001
        _dlmod = None
        _unl = None
    if res.get("ok") and (res.get("written") or res.get("deleted")):
        _reg = regression_check(ws, list(res.get("written") or []), emit=emit)
        res["regression"] = _reg
        if _reg.get("regressed"):
            _n = restore_from_backup(ws, res.get("backup") or "")
            res["ok"] = False
            res["rolled_back"] = True
            res["written_before_rollback"] = res.get("written") or []
            res["written"] = []
            res["deleted"] = []
            res["detail"] = ("回归护栏拦下：%s → 整批回滚（恢复 %d 个文件）。"
                             "本轮白改，但仓库保持原状（不倒退）。"
                             % (_reg.get("detail"), _n))
            _emit(emit, "whole_rollback_regression",
                  {"regressed": _reg.get("regressed"), "restored": _n,
                   "detail": res["detail"]})
            # 回滚 ≠ 文件拒收：独立落盘 + 独立成段，明确说"白改/别重做"。
            # 旧实现塞进 reject 用"文件被拒收"框渲染 → 远端当噪声忽略 → 5 轮死循环。
            save_rollback(ws, {"regressed": list(_reg.get("regressed") or []),
                               "files": list(res.get("written_before_rollback") or []),
                               "detail": str(_reg.get("detail") or ""),
                               "round": round_no})
            # **死局协商钩子**：同一批能力反复被回滚弄红 → 唤醒审核者三方协商
            if _dlmod is not None:
                try:
                    _dl_res = _dlmod.on_rollback(
                        ws, list(_reg.get("regressed") or []),
                        list(res.get("written_before_rollback") or []),
                        round_no, str(_reg.get("detail") or ""), emit=emit)
                except Exception as exc:                     # noqa: BLE001
                    _dl_res = {"woke": False, "error": "%s: %s"
                               % (type(exc).__name__, exc)}
                res["deadlock"] = _dl_res
        elif _reg.get("unlocked_red"):
            # 解锁轮：只有范围内的红 → 不回滚，改完由三方复审裁决
            res["unlocked_red"] = list(_reg["unlocked_red"])
            res["detail"] = ("解锁范围内 %d 条仍红（不回滚，留证待复审）：%s"
                             % (len(_reg["unlocked_red"]),
                                ", ".join(_reg["unlocked_red"][:6])))
            _emit(emit, "whole_unlock_red", {"caps": _reg["unlocked_red"]})
    # 远端点名的验证器：**只读**跑一遍，结果落盘给下一轮（它自己不下结论）
    _vres: dict = {}
    _vids = (parsed.get("verify") or {}).get("ids") or []
    # 队列里还有上一轮欠的 → 即使本轮什么都没点，也要补跑（否则永远欠着）
    try:
        from . import validators as _valq
        _vqueued = _valq._load_queue(ws)
    except Exception:                                    # noqa: BLE001
        _vqueued = []
    if (_vids or _vqueued) and res.get("ok"):
        try:
            from . import validators as _valrun
            _vres = _valrun.run_with_queue(ws, _vids, emit=emit)
            _valrun.save(ws, _vres, round_no=round_no)
        except Exception as exc:                         # noqa: BLE001
            _vres = {"ran": [], "rejected": [], "unavailable": [], "deferred": [],
                     "error": "%s: %s" % (type(exc).__name__, exc)}
    # **解锁轮三方复审**：写作者独改完毕、机械证据已重跑 → 审核者裁决。
    # 多数决（≥2/3 过）→ 死局解决、审核者休眠；不过 → 用本轮备份整批回滚。
    if _unl and res.get("ok") and _dlmod is not None:
        _red = set(res.get("unlocked_red") or [])
        try:
            _review = _dlmod.on_round_result(
                ws, round_no,
                regressed=list(_red),
                green=[c for c in (_unl.get("caps") or []) if c not in _red],
                backup=str(res.get("backup") or ""),
                rootcause=str(parsed.get("rootcause") or ""),
                emit=emit)
            res["deadlock_review"] = {k: v for k, v in _review.items()
                                      if k not in ("renegotiate",)}
            if not _review.get("resolved") and _review.get("restore_backup"):
                _n2 = restore_from_backup(ws, _review.get("restore_backup") or "")
                res["ok"] = False
                res["rolled_back"] = True
                res["written_before_rollback"] = res.get("written") or []
                res["written"] = []
                res["deleted"] = []
                res["detail"] = ("三方复审未通过 → 整批回滚（恢复 %d 个文件）。"
                                 "仓库保持原状（不倒退）。" % _n2)
                save_rollback(ws, {
                    "regressed": list(_red),
                    "files": list(res.get("written_before_rollback") or []),
                    "detail": "三方复审未通过：%s"
                              % json.dumps(_review.get("votes") or {}, ensure_ascii=False)[:300],
                    "round": round_no})
        except Exception as exc:                             # noqa: BLE001
            res["deadlock_review"] = {"error": "%s: %s" % (type(exc).__name__, exc)}
    res.update({"rootcause": parsed["rootcause"], "done": parsed["done"],
                "verify": _vres, "verify_why": (parsed.get("verify") or {}).get("why", ""),
                "raw_chars": len(answer), "snapshot_chars": len(text)})
    _emit(emit, "whole_done", {"ok": res.get("ok"), "written": len(res.get("written") or []),
                               "deleted": len(res.get("deleted") or []),
                               "verify": [r.get("id") for r in (_vres.get("ran") or [])],
                               "detail": str(res.get("detail") or "")[:200]})
    return res
