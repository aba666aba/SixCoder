"""验证器白名单：远端只能"点菜"，本地负责"做菜"和"当裁判"。

为什么这样分工（2026-11-15 用户要求"按提示词自己决定调哪个验证器"）：

    远端知道自己在改什么（它刚写完 19 个文件），所以**它最适合决定"该拿哪把尺子量"**；
    但判据绝不能由它出 —— 自己出题自己判，判据会退化成讨好模型（历史教训：
    模型自写的 pytest 全绿而产物 TypeError）。所以：

      · 远端只能从**白名单 id** 里点名（`<verify>RUN: py.mypy</verify>`）
      · 本地执行：**只读**、有超时、有每轮个数上限
      · 结果结构化写盘，作为**下一轮报告**的一段回灌（不让模型自己解释结论）

白名单原则：**只收"零安装成本、只读、判据客观"的**。
没装工具（bandit/pip-audit）就如实标"不可用"，绝不假装跑过。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

# id → 元信息。`avail=False` 的一律拒绝执行并如实报告（绝不假装跑过）。
VALIDATORS: dict[str, dict] = {
    "d1.syntax": {"desc": "语法 / 模块内重名 / 空桩", "cost": "<1s"},
    "d4.crossref": {"desc": "调用了不存在的函数 / 参数个数不符", "cost": "<1s"},
    "py.compile": {"desc": "全仓 py_compile（比 import 更快的兜底）", "cost": "1-3s"},
    "py.mypy": {"desc": "类型一致性（参数/返回值/Optional 未判空）", "cost": "10-60s"},
    "py.ruff": {"desc": "静态质量（未用导入/变量、明显坏味道）", "cost": "<5s"},
    "py.migrate": {"desc": "模型改了但没生成迁移（Django）", "cost": "<15s",
                   "needs": "manage.py"},
    "py.tests": {"desc": "项目自带测试套件", "cost": "不定", "needs": "pytest"},
    "sec.bandit": {"desc": "安全扫描（eval/pickle/SQL 拼接/硬编码密钥）",
                   "cost": "不可用", "avail": False,
                   "why": "本机未安装 bandit（pip install bandit 后自动启用）"},
    "sec.ast": {"desc": "安全扫描·零依赖版（eval/exec、pickle、shell=True、SQL 拼接、硬编码密钥）",
                "cost": "1-3s"},
    "py.complexity": {"desc": "圈复杂度超标函数（>12 分支，难维护/易漏测）", "cost": "1-3s"},
    "dep.audit": {"desc": "依赖漏洞扫描", "cost": "不可用", "avail": False,
                  "why": "本机未安装 pip-audit（pip install pip-audit 后自动启用）"},
}

# 上限口径（2026-11-15 用户两次追问后定稿）：
# **不设个数上限。** 理由：个数是"估算"，时间才是"事实"——
# 我们真的知道每个验证器跑了多久，就没有理由再拿"最多几个"去猜。
# 保留的只有两件与钱无关、与"能不能继续跑"有关的事：
#   ① `BUDGET_SECONDS` 单轮验证总预算：一轮是**同步**的，卡住=整夜停摆；
#   ② 每个子进程自己的超时（见 `_run_one` 里各处 timeout）。
# 跑不完的不是"拒绝"，是**排队**到下一轮（`verify_queue.json`），请求永不丢。
# 成本侧没有任何上限：唯一停机线是预算 ×1.05（或官方余额 ≤0）。
MAX_PER_ROUND = None       # None = 不限个数（保留变量名，避免旧代码引用炸）
BUDGET_SECONDS = 600       # 单轮验证总预算（秒）；超时的排队到下一轮
QUEUE_FILE = ".sixcoder/verify_queue.json"
RESULT_FILE = ".sixcoder/verify_last.json"


def menu() -> str:
    """给远端看的"验证器菜单"（提示词里用）。"""
    rows = ["| id | 能抓什么 | 耗时 |", "|---|---|---|"]
    for vid, m in VALIDATORS.items():
        if m.get("avail") is False:
            rows.append("| `%s` | %s（**不可用**：%s） | — |"
                        % (vid, m["desc"], m.get("why", "")))
        else:
            rows.append("| `%s` | %s | %s |" % (vid, m["desc"], m.get("cost", "?")))
    return "\n".join(rows)


def parse_verify(text: str) -> dict:
    """从回复里抽 `<verify>` 段：`RUN: id` 列表 + `WHY:`。"""
    import re
    t = str(text or "")
    m = re.search(r"<verify>(.*?)</verify>", t, re.S | re.I)
    body = m.group(1) if m else ""
    ids: list[str] = []
    if body:
        for ln in body.splitlines():
            mm = re.match(r"\s*(?:RUN|run)\s*[:：]\s*`?([\w.\-]+)`?", ln)
            if mm:
                ids.append(mm.group(1).strip())
    why = ""
    mw = re.search(r"(?im)^\s*WHY\s*[:：]\s*(.+)$", body) if body else None
    if mw:
        why = mw.group(1).strip()[:400]
    return {"ids": ids, "why": why}


def _tail(text: str, n: int = 6, width: int = 200) -> str:
    lines = [x for x in str(text or "").strip().splitlines() if x.strip()]
    return " / ".join(x.strip()[:width] for x in lines[-n:])


def _run_cmd(cmd: list[str], ws: Path, timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "超时 %ss" % timeout
    except OSError as exc:
        return 127, "%s: %s" % (type(exc).__name__, exc)


def _run_one(vid: str, ws: Path, budget: int) -> dict:
    """跑一个验证器。返回 {id, ok, detail, rc, seconds}。**只读**。"""
    t0 = time.time()
    ws = Path(ws)
    try:
        if vid == "d1.syntax":
            from . import detectors as _d
            errs = _d.d1_syntax(ws)
            det = ("无问题" if not errs else
                   "%d 条：%s" % (len(errs), "；".join(
                       "%s[%s]" % (getattr(e, "where", "?"), getattr(e, "level", "?"))
                       for e in errs[:3])))
            return {"id": vid, "ok": not errs, "detail": det,
                    "seconds": round(time.time() - t0, 2)}
        if vid == "d4.crossref":
            from . import detectors as _d
            errs = _d.d4_crossref(ws)
            det = ("无问题" if not errs else
                   "%d 条：%s" % (len(errs), "；".join(
                       "%s: %s" % (getattr(e, "where", "?"), getattr(e, "what", ""))
                       for e in errs[:3])))
            return {"id": vid, "ok": not errs, "detail": det,
                    "seconds": round(time.time() - t0, 2)}
        if vid == "py.compile":
            rc, out = _run_cmd([sys.executable, "-m", "compileall", "-q", "."], ws,
                               min(budget, 120))
            return {"id": vid, "ok": rc == 0, "detail": _tail(out) or "编译通过",
                    "rc": rc, "seconds": round(time.time() - t0, 2)}
        if vid == "py.mypy":
            # 只把**存在的**目录当目标：给一个不存在的目录名，mypy 会报
            # "cannot find module named config" 并以非 0 退出 —— 那是工具误用，
            # 不是产物有类型错误（自检里就撞过：临时工程没有 config/）。
            _targets = [d for d in ("apps", "matching_core", "config") if (ws / d).is_dir()]
            if not _targets:
                _targets = ["."]
            rc, out = _run_cmd([sys.executable, "-m", "mypy", "--ignore-missing-imports",
                                "--no-error-summary", "--no-color-output", *_targets],
                               ws, min(budget, 180))
            # 1 = 有类型错误；0 = 干净；其它 = 工具本身出错（如实报）
            ok = rc == 0
            detail = ("类型检查通过" if ok else
                      "%s" % (_tail(out, 5) or "mypy 返回 %d" % rc))
            return {"id": vid, "ok": ok, "detail": detail, "rc": rc,
                    "seconds": round(time.time() - t0, 2)}
        if vid == "py.ruff":
            rc, out = _run_cmd([sys.executable, "-m", "ruff", "check", "--output-format=concise",
                                "--no-cache", "."], ws, min(budget, 120))
            return {"id": vid, "ok": rc == 0, "detail": _tail(out) or "静态检查通过",
                    "rc": rc, "seconds": round(time.time() - t0, 2)}
        if vid == "py.migrate":
            if not (ws / "manage.py").is_file():
                return {"id": vid, "ok": False, "detail": "不是 Django 工程（无 manage.py）",
                        "seconds": round(time.time() - t0, 2)}
            rc, out = _run_cmd([sys.executable, "manage.py", "makemigrations",
                                "--check", "--dry-run"], ws, min(budget, 120))
            return {"id": vid, "ok": rc == 0,
                    "detail": ("迁移与模型一致" if rc == 0 else
                               _tail(out, 4) or "有未生成的迁移"),
                    "rc": rc, "seconds": round(time.time() - t0, 2)}
        if vid == "sec.ast":
            return _scan_security(ws, t0)
        if vid == "py.complexity":
            return _scan_complexity(ws, t0)
        if vid == "py.tests":
            rc, out = _run_cmd([sys.executable, "-m", "pytest", "-q", "-x",
                                "-p", "no:cacheprovider", "--maxfail=1"], ws,
                               min(budget, 240))
            return {"id": vid, "ok": rc == 0, "detail": _tail(out, 4) or ("rc=%d" % rc),
                    "rc": rc, "seconds": round(time.time() - t0, 2)}
    except Exception as exc:                             # noqa: BLE001
        return {"id": vid, "ok": False,
                "detail": "验证器自身出错：%s: %s" % (type(exc).__name__, exc),
                "seconds": round(time.time() - t0, 2)}
    return {"id": vid, "ok": False, "detail": "未实现", "seconds": 0}


def run(ws, ids: list[str], *, max_n: int | None = None,
        total_timeout: int = BUDGET_SECONDS, emit=None) -> dict:
    """执行远端点名的验证器（白名单 + 时间预算 + 只读）。

    **不设个数上限**（`max_n=None`）。唯一的闸门是时间：一轮验证是同步的，
    卡住等于整夜停摆。跑不完的**排队**到下一轮，不丢弃、不拒绝。

    返回 {"ran": [...], "rejected": [...], "unavailable": [...],
          "deferred": [...], "seconds": n}
    """
    out = {"ran": [], "rejected": [], "unavailable": [], "deferred": [],
           "seconds": 0.0}
    t0 = time.time()
    seen: list[str] = []
    for vid in ids or []:
        vid = str(vid or "").strip()
        if not vid or vid in seen:
            continue
        seen.append(vid)
        m = VALIDATORS.get(vid)
        if m is None:
            out["rejected"].append({"id": vid, "why": "不在白名单（只能点菜单里的 id）"})
            continue
        if m.get("avail") is False:
            out["unavailable"].append({"id": vid, "why": m.get("why", "不可用")})
            continue
        left = int(total_timeout - (time.time() - t0))
        if (max_n is not None and len(out["ran"]) >= max_n) or left <= 5:
            out["deferred"].append({"id": vid,
                                    "why": ("本轮时间预算用尽（%ds）" % total_timeout)
                                    if left <= 5 else "本轮个数上限 %d" % max_n})
            continue
        r = _run_one(vid, Path(ws), left)
        out["ran"].append(r)
    out["seconds"] = round(time.time() - t0, 2)
    if emit:
        try:
            emit("verify_done", {"ran": [r["id"] for r in out["ran"]],
                                 "rejected": [r["id"] for r in out["rejected"]],
                                 "unavailable": [r["id"] for r in out["unavailable"]],
                                 "deferred": [r["id"] for r in out["deferred"]],
                                 "seconds": out["seconds"]})
        except Exception:                                # noqa: BLE001
            pass
    return out


def _load_queue(ws) -> list[str]:
    try:
        data = json.loads((Path(ws) / QUEUE_FILE).read_text(encoding="utf-8"))
        return [str(x) for x in (data.get("ids") or []) if x]
    except Exception:                                    # noqa: BLE001
        return []


def _save_queue(ws, ids: list[str]) -> None:
    p = Path(ws) / QUEUE_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not ids:
            if p.is_file():
                p.unlink()
            return
        p.write_text(json.dumps({"ids": ids, "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                                ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def run_with_queue(ws, ids: list[str], *, max_n: int | None = None,
                   total_timeout: int = BUDGET_SECONDS, emit=None) -> dict:
    """带队列的执行：**先补上一轮欠的**，再把本轮欠的存起来。"""
    queued = _load_queue(ws)
    merged = list(dict.fromkeys(queued + [str(x) for x in (ids or [])]))
    out = run(ws, merged, max_n=max_n, total_timeout=total_timeout, emit=emit)
    _save_queue(ws, [d["id"] for d in out.get("deferred") or []])
    out["from_queue"] = [q for q in queued if q in merged]
    return out


def save(ws, result: dict, round_no: int = 0) -> None:
    p = Path(ws) / RESULT_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"round": round_no, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                 **result}, ensure_ascii=False, indent=1),
                     encoding="utf-8")
    except OSError:
        pass


def load(ws) -> dict:
    try:
        return json.loads((Path(ws) / RESULT_FILE).read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def render_results(result: dict) -> str:
    """把上一次的验证结果渲染成给远端看的文本（下一轮 brief 里用）。"""
    if not result:
        return ""
    lines = ["## 上一轮你点名的验证器结果（本地跑的，判据客观）"]
    for r in result.get("ran") or []:
        lines.append("- `%s`：%s（%.1fs）— %s"
                     % (r.get("id"), "**通过**" if r.get("ok") else "**失败**",
                        float(r.get("seconds") or 0), str(r.get("detail") or "")[:400]))
    for r in result.get("rejected") or []:
        lines.append("- `%s`：被拒（%s）" % (r.get("id"), r.get("why")))
    for r in result.get("unavailable") or []:
        lines.append("- `%s`：不可用（%s）" % (r.get("id"), r.get("why")))
    return "\n".join(lines) + "\n" if len(lines) > 1 else ""


# ---------------------------------------------------------------- 零依赖扫描器
_SECRET_RE = None


def _scan_security(ws: Path, t0: float) -> dict:
    """安全扫描·零依赖版（不装 bandit 也能抓最常见的一类）。

    为什么自己做：bandit 要装（还要联网），而"eval/exec、pickle.loads、
    shell=True、SQL 字符串拼接、硬编码密钥"这五类占了新手项目安全问题的绝大多数，
    用 AST + 一条正则就能抓，零依赖、零网络、只读。
    """
    import ast as _ast
    import re as _re
    global _SECRET_RE
    if _SECRET_RE is None:
        _SECRET_RE = _re.compile(
            r"(?i)\b(passwd|password|secret|token|api[_-]?key)\b\s*=\s*['\"][^'\"]{8,}['\"]")
    issues: list[str] = []
    for p in sorted(Path(ws).rglob("*.py")):
        rel = p.as_posix()
        # **引擎自己的临时区不算产品代码**：`.sixcoder/tmp/_props_run.py` 里有 eval()，
        # 那是判据渲染出来的测试桩，扫它只会把"我们自己生成的东西"报成安全问题。
        if any(x in rel for x in ("/.sixcoder/", "/migrations/", "__pycache__",
                                 "/_legacy_app/", "/.tmp")):
            continue
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
            tree = _ast.parse(src)
        except (OSError, SyntaxError):
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            try:
                name = _ast.unparse(node.func)
            except Exception:                            # noqa: BLE001
                name = ""
            ln = getattr(node, "lineno", 0)
            if name in ("eval", "exec"):
                issues.append("%s:%d %s()" % (rel, ln, name))
            elif name.startswith("os.system") or name.startswith("os.popen"):
                issues.append("%s:%d %s()" % (rel, ln, name))
            elif "pickle.loads" in name or "yaml.load" == name:
                issues.append("%s:%d %s()" % (rel, ln, name))
            elif name.startswith("subprocess.") and any(
                    k.arg == "shell" and isinstance(k.value, _ast.Constant)
                    and k.value.value is True for k in node.keywords):
                issues.append("%s:%d %s(shell=True)" % (rel, ln, name))
            elif name.endswith(".execute") and node.args:
                a0 = node.args[0]
                if isinstance(a0, _ast.BinOp) and isinstance(a0.op, _ast.Add):
                    issues.append("%s:%d execute(... + ...) SQL 拼接" % (rel, ln))
        for i, line in enumerate(src.splitlines(), 1):
            if _SECRET_RE.search(line) and "os.environ" not in line:
                issues.append("%s:%d 疑似硬编码密钥" % (rel, i))
    ok = not issues
    return {"id": "sec.ast", "ok": ok,
            "detail": ("未发现 eval/pickle/shell=True/SQL 拼接/硬编码密钥"
                       if ok else "%d 处：%s" % (len(issues), "；".join(issues[:4]))),
            "seconds": round(time.time() - t0, 2)}


def _scan_complexity(ws: Path, t0: float, threshold: int = 12) -> dict:
    """圈复杂度超标函数（>12 个分支）。零依赖（radon 未装）。

    为什么值得有一条：整仓会话一次改十几个文件时，最容易长出的就是
    "一个函数里塞十个 if" —— 它不报错、测试也可能过，但下一次改它的人会很痛。
    """
    import ast as _ast
    _BRANCH = (_ast.If, _ast.For, _ast.AsyncFor, _ast.While, _ast.ExceptHandler,
               _ast.With, _ast.AsyncWith, _ast.IfExp, _ast.BoolOp)
    hot: list[str] = []
    for p in sorted(Path(ws).rglob("*.py")):
        rel = p.as_posix()
        if any(x in rel for x in ("/.sixcoder/", "__pycache__", "/_legacy_app/",
                                 "/.tmp", "/migrations/")):
            continue
        try:
            tree = _ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in _ast.walk(tree):
            if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                continue
            n = 1 + sum(1 for x in _ast.walk(node) if isinstance(x, _BRANCH))
            if n > threshold:
                hot.append("%s::%s 复杂度 %d" % (rel, node.name, n))
    ok = not hot
    return {"id": "py.complexity", "ok": ok,
            "detail": ("没有超过 %d 的函数" % threshold if ok else
                       "%d 个超标：%s" % (len(hot), "；".join(sorted(hot, key=len)[:4]))),
            "seconds": round(time.time() - t0, 2)}
