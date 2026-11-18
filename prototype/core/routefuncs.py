"""契约点名表：**端点路径 → 处理函数名**（从契约/需求文本解析，零 token）。

为什么必须有这个模块（2026-11-11 实测，§11.4g）：

系统里"端点 → 函数 → 签名"这个三元组有 **3 份独立副本** —— 能力表探针 /
`interfaces.json` / 模块契约 / 切片契约，**没有一处是权威**，于是必然对不上：

- 切片契约点名 `get_teacher_me(authorization)`，远端写出了 `teacher_me(account)`
  → 契约检查永远判"函数缺失" → `stuck_same_evidence` → `order_failed`；
- `interfaces.json` 说 `teachers_me_route(authorization)`，模块契约说
  `teachers_me_route(account)` → 同一函数两套签名 → 永远卡死；
- 更糟的是**路由抢跑**：甲方线早期把路由写进了引擎拥有的 `app/api/people.py`，
  FastAPI 里它先注册 → 探针打到旧实现 → 业务层写对了也轮不到 → 覆盖永远不涨
  （实测 `GET /api/v1/people/teachers/me` 返回
  `[{'teacher':'','quota':0,'assigned':0,'load_ratio':0.0}]`，
  而业务层正确的 `get_teacher_me` 就在文件里）。

**本模块的作用**：把契约文本里本来就有的"函数签名：端点"映射解析出来，
作为**唯一权威**交给三处消费 —— 写码（点名）、接线（按名接）、体检（按名判）。
这样"远端写的名字"与"引擎认的名字"必然是同一个。

**只解析，不生成**：契约是远端写的，签名行自带路径（实测可解析率 3/3），
引擎不需要再发明一套命名规则 —— 那样只会多出第 4 份副本。
"""
from __future__ import annotations

import json
import re

__all__ = ["parse", "normalize", "describe"]

# 签名行：`- create_topic(body: dict, authorization: str) -> dict：POST /api/v1/topics 服务层入口：…`
# 冒号可能是全角/半角；签名与描述之间可能有 `-> dict` / `-> object`。
_SIG_RE = re.compile(
    r"^\s*[-*•]\s*"
    r"(?P<fn>[A-Za-z_]\w*)\s*\((?P<args>[^)]*)\)"
    r"(?:\s*->\s*[^\s：:]+)?\s*[：:]\s*"
    r"(?P<verb>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>\S+)",
    re.M,
)

# 契约里也可能写成裸的 `- POST /api/v1/topics → create_topic`（反向）。
_REV_RE = re.compile(
    r"^\s*[-*•]\s*(?P<verb>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>\S+)"
    r"(?:\s*[→>\-–—]+\s*)(?P<fn>[A-Za-z_]\w*)\s*\(",
    re.M,
)

# 路径里的动态段：契约写 `{topic_id}`，探针发 `999999`。
_PARAM_RE = re.compile(r"^\{[^{}]+\}$")


def normalize(path: str) -> str:
    """端点路径归一（让契约里的 `{topic_id}` 与探针里的 `999999` 能对上）。

    规则：去方法前缀、去查询串、去尾部斜杠；
    **任何参数段**（`{topic_id}` 形态或纯数字形态）统一换成 `{*}` ——
    契约写的是 `{topic_id}`、探针发的是 `999999`、日志里是 `POST /api/v1/topics/999999`，
    三种写法必须落到**同一个键**，否则点名表查不到。
    """
    p = str(path or "").strip()
    if not p:
        return ""
    p = p.split("?", 1)[0].strip()
    low = p.lower()
    for verb in ("delete", "patch", "post", "put", "get"):
        if low.startswith(verb + " "):
            p = p[len(verb) + 1:].strip()
            break
    if not p.startswith("/"):
        p = "/" + p
    segs: list[str] = []
    for s in p.strip("/").split("/"):
        if not s:
            continue
        if s.isdigit() or _PARAM_RE.match(s):
            s = "{*}"
        segs.append(s)
    return "/" + "/".join(segs)


# 只有签名、没有路径的签名行：`- create_topics(title: str, ...) -> dict：说明…`
_SIG_ONLY_RE = re.compile(
    r"^\s*[-*•]\s*(?P<fn>[A-Za-z_]\w*)\s*\([^)]*\)"
    r"(?:\s*->\s*[^：:\s]+)?\s*[：:]"
)

# 模块段里的路径声明：`1) app/services/topics.py：POST /api/v1/topics 的服务层入口：…`
_PATH_ONLY_RE = re.compile(
    r"(?P<verb>GET|POST|PUT|PATCH|DELETE)\s+(?P<path>/[^\s：:，,。；;）)]+)")


def parse(text: str) -> dict[str, str]:
    """契约/需求文本 → `{归一路径: 函数名}`。

    **两种契约格式都要认**（实测 2026-11-11 18:38，远端每次合成的格式会变）：
    ① **签名行自带路径**（旧格式）：
       `- create_topic(body: dict, authorization: str) -> dict：POST /api/v1/topics 服务层入口：…`
    ② **路径在模块段、签名行只管签名**（新格式）：
       ```
       1) app/services/topics.py：POST /api/v1/topics 的服务层入口：…
          - create_topics(title: str, ...) -> dict：创建一条教师发题草稿并回显：…
       ```
       → 签名**归属到它上面最近的那个路径声明**。

    只认格式① 时，新契约解析出 **0 条** → 点名表整个空掉
    → 接线退回字符串猜 → 路由指着旧函数 → 远端写了新函数也白写（实测 `source` 永远 None）。

    同一路径被点名多次时**第一个赢**（契约前面的段落通常是主定义）。
    """
    out: dict[str, str] = {}
    for m in _SIG_RE.finditer(text or ""):
        key = normalize(m.group("path"))
        if key and key not in out:
            out[key] = m.group("fn")
    for m in _REV_RE.finditer(text or ""):
        key = normalize(m.group("path"))
        if key and key not in out:
            out[key] = m.group("fn")

    # 格式②：逐行扫，维护"当前路径"，签名行归属到它
    cur = ""
    for ln in (text or "").splitlines():
        sm = _SIG_ONLY_RE.match(ln)
        if sm:
            if cur and cur not in out:
                out[cur] = sm.group("fn")
            continue
        pm = _PATH_ONLY_RE.search(ln)
        if pm and not ln.strip().startswith(("-", "*", "•")):
            cur = normalize(pm.group("path"))
    return out


def _norm_name(n: str) -> str:
    """函数名归一（用于"同一个函数的两种写法"判定）。

    去掉动作前缀（get_/create_/make_/build_/parse_/handle_/read_）、
    去掉 `_route`/`_endpoint` 后缀、单复数归一、去掉下划线。
    `teachers_me` / `teacher_me` / `get_teachers_me` / `teachers_me_route`
    → 同一个键 `teacher`。
    """
    s = str(n or "").strip().lower()
    for suf in ("_route", "_endpoint", "_handler", "_view"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = re.sub(r"^(get|post|put|delete|create|make|build|parse|handle|read|list|fetch)_+", "", s)
    # **按词**做单数化（不能对整串去尾 s）：`teachers_me` 的复数 s 在**词中间** ——
    # 对整串去尾 s 得 `teachersme`，`teacher_me` 得 `teacherme` → 对不上
    # （实测 2026-11-11：命名对齐一条都没对上）。
    return "".join(_singular(x) for x in s.split("_") if x)


def _singular(w: str) -> str:
    """一个词的单数形式。"""
    w = str(w or "")
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith(("sses", "shes", "ches", "xes", "zes")):
        return w[:-2]
    if len(w) > 2 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def align(rf: dict[str, str], ws) -> tuple[dict[str, str], dict[str, str]]:
    """点名表 → (对齐后的点名表, 别名表{契约名: 产物实际名})。

    为什么需要（2026-11-11 实测，§11.4g）：
    **契约每次重新合成就会重新命名** —— 实测同一个端点在三轮里分别被叫
    `teachers_me` / `teacher_me` / `get_teacher_me`，
    而产物里留下的是上一轮那个名字 → 契约点名找不到函数 → 体检判"缺函数"
    → 远端反复补 → 接线退回字符串猜 → 端到端永远红。

    **对齐层把这条链断在源头**：契约点名 X、产物实际是 Y（只差前缀/单复数/后缀）时，
    认定二者是同一个函数，点名表改用产物里的真实名字 Y。
    这样 **接线接得上、体检判得过、远端看得见"沿用 Y"**。

    只在"产物里真存在 Y"且"Y 的归一键与 X 相同"时才对齐 ——
    宁可留白让引擎回退猜测，也不硬凑（否则会把路由接到语义无关的函数上，
    那正是 2026-08 踩过的坑）。
    """
    from pathlib import Path

    ws = Path(ws)
    out: dict[str, str] = {}
    alias: dict[str, str] = {}
    # 产物里现存的函数名（按模块）
    have: dict[str, set[str]] = {}
    try:
        mods = [p for p in (ws / "app").rglob("*.py")] + \
               [p for p in (ws / "matching_core").glob("*.py")]
    except OSError:
        mods = []
    for p in mods:
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        names = set(re.findall(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(", src, re.M))
        if names:
            have[str(p.relative_to(ws)).replace("\\", "/")] = names

    by_norm: dict[str, list[str]] = {}
    all_names: set[str] = set()
    for _mod, names in have.items():
        all_names |= names
        for n in names:
            by_norm.setdefault(_norm_name(n), []).append(n)

    for path, fn in (rf or {}).items():
        # ① **精确命中**：产物里真有同名函数 → 直接用（不需要对齐）
        if fn in all_names:
            out[path] = fn
            continue
        # ② 归一候选：只有一个候选才敢认（多个同归一但不同名 → 不猜，
        #    宁可留白让接线回退，也不硬凑 —— 硬凑就是 2026-08 踩过的坑：
        #    把路由接到语义无关的函数上，几十条能力一起红）
        cands = by_norm.get(_norm_name(fn)) or []
        if len(cands) == 1:
            out[path] = cands[0]
            alias[fn] = cands[0]
        else:
            out[path] = fn
    return out, alias


def describe(rf: dict[str, str] | None) -> str:
    """一行摘要，给日志/报告用。"""
    n = len(rf or {})
    return "契约点名表：%d 个端点" % n


def from_workspace(ws) -> dict[str, str]:
    """产物仓里找点名表（**兜底**：调用方没传时的单一入口）。

    顺序：
      ① `.sixcoder/slices/*.txt`（最新修改那份 = 当前片契约，**最准**）
      ② `.sixcoder/interfaces.json`（接口清单；`doc` 字段里带端点路径）

    为什么要有兜底：点名表要在**接线**这一层用，而接线的调用点很多
    （`auto` 两处 + `openhouse` 一处）—— 靠"每个调用点都记得传"必然漏一处，
    漏一处就退回字符串猜。**兜底 = 单一实现，零漏传。**
    """
    from pathlib import Path

    ws = Path(ws)
    out: dict[str, str] = {}

    # ① 当前片契约（最新修改）
    sl = ws / ".sixcoder" / "slices"
    try:
        files = sorted(sl.glob("*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        files = []
    for f in files[:1]:
        try:
            for k, v in parse(f.read_text(encoding="utf-8-sig", errors="replace")).items():
                out.setdefault(k, v)
        except OSError:
            continue

    # ② 接口清单（甲方线模式没有切片契约时用）
    if not out:
        ifc = ws / ".sixcoder" / "interfaces.json"
        try:
            data = json.loads(ifc.read_text(encoding="utf-8-sig", errors="replace"))
        except Exception:                                # noqa: BLE001
            data = {}
        for _mod, blob in (data or {}).items():
            if not isinstance(blob, dict):
                continue
            for f in (blob.get("funcs") or []):
                if not isinstance(f, dict):
                    continue
                name = str(f.get("name") or "")
                doc = str(f.get("doc") or "")
                if not name or not doc:
                    continue
                m = re.search(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/\S+)", doc)
                if not m:
                    continue
                key = normalize(m.group(2))
                if key and key not in out:
                    out[key] = name
    return out
