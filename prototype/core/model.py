"""领域模型层：唯一真相（`.sixcoder/model.json`）。

为什么要有这一层（2026-11-08 定，用户要求）：

现在的流水线是 `需求文档 → 能力清单(76条) → 切片 → 代码`，**中间跳过了建模**。
后果是每一处改都只改自己那一小块，没有"整体"这个概念：

  · `POST /topics` 返回 201，body 却是 `{"status":403}` —— 没有统一的题目状态定义；
  · `submit_topic` 既能建草稿又能提交 —— 没有状态机，谁都能改 status；
  · 批次不存在却返回 `matched:2` —— 没有实体关系，不知道"结果属于批次"；
  · 建专业 500 —— 字段类型（college_id 是 int 还是 str）没有单一来源。

**只建四种关系**（用户明确"只构建需要的关系即可"，不铺 34 种图）：

  entities     实体 + 字段 + 外键         → ER 图 / models.py 的真源
  states       状态枚举 + 合法转换        → 状态图 / 状态校验
  flows        主干时序（跨实体一条）      → 时序图 / 强制 e2e
  components   模块与依赖方向             → 组件图 / 层次门禁的期望

其余图（用例、活动、DFD、甘特、WBS…）不进引擎：它们要么是本层四张图的另一种画法，
要么是项目管理产物，跟"代码一致性"无关。要它们时从这一份模型渲染，不另建真相。

提取原则：**机械可复现，不调模型**。数据全部来自蓝图探针（路径/请求体/期望），
同输入永远同输出；看不懂的字段就留空并记 note，绝不猜。
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

MODEL_FILE = ".sixcoder/model.json"
MODEL_VERSION = 1

_NOISE = {"api", "rest", "internal", "v1", "v2", "v3", "v4", "v5",
          "auth", "me", "self", "health", "docs", "openapi"}
_LAYER_OF = (("app/api/", 0), ("app/services/", 1), ("app/", 2))

# HTTP 动作语义词（不是业务词）：出现在路径末段时说明这是**操作端点**，
# 资源在它前面。这是 REST 惯例，跨项目通用。
_ACTION_WORDS = {"register", "login", "logout", "signup", "signin", "refresh",
                 "run", "seed", "submit", "publish", "offline", "online",
                 "adopt", "reject", "confirm", "cancel", "review", "approve",
                 "preview", "selftest", "auto", "upload", "download",
                 "export", "import", "search", "query", "validate", "check",
                 "recommend", "recommendations", "metrics", "results", "logs"}


# ---------------------------------------------------------------- 结构

@dataclass
class Field:
    name: str
    type: str = "any"
    required: bool = False
    pk: bool = False
    fk: str = ""
    enum: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {"name": self.name, "type": self.type, "required": self.required}
        if self.pk:
            d["pk"] = True
        if self.fk:
            d["fk"] = self.fk
        if self.enum:
            d["enum"] = list(self.enum)
        return d


@dataclass
class Transition:
    from_: str
    to: str
    action: str = ""
    role: str = ""

    def to_dict(self) -> dict:
        return {"from": self.from_, "to": self.to,
                "action": self.action, "role": self.role}


@dataclass
class Entity:
    name: str
    resource: str
    fields: list[Field] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    initial: str = ""
    transitions: list[Transition] = field(default_factory=list)
    # 状态来源：observed = 探针断言里真的出现过（事实）；
    # declared = 远端按需求补的（声明，可能对也可能错，必须能追溯）。
    # 混在一起会出现"唯一的状态就是初始态"这种荒唐结论（实测 Adjustment: rerun）。
    status_src: dict[str, str] = field(default_factory=dict)

    @property
    def pk(self) -> str:
        for f in self.fields:
            if f.pk:
                return f.name
        return "id"

    @property
    def observed(self) -> list[str]:
        return [s for s in self.statuses if self.status_src.get(s, "observed") == "observed"]

    @property
    def declared(self) -> list[str]:
        return [s for s in self.statuses if self.status_src.get(s) == "declared"]

    def to_dict(self) -> dict:
        return {"name": self.name, "resource": self.resource,
                "fields": [f.to_dict() for f in self.fields],
                "statuses": list(self.statuses), "initial": self.initial,
                "status_src": dict(self.status_src),
                "transitions": [t.to_dict() for t in self.transitions]}


@dataclass
class Flow:
    name: str
    steps: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "steps": [dict(s) for s in self.steps]}


@dataclass
class Component:
    name: str
    layer: int = 2
    depends_on: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "layer": self.layer,
                "depends_on": list(self.depends_on)}


@dataclass
class Model:
    entities: list[Entity] = field(default_factory=list)
    flows: list[Flow] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    invariants: list[dict] = field(default_factory=list)
    version: int = MODEL_VERSION
    extracted_at: float = 0.0
    notes: list[str] = field(default_factory=list)

    def entity(self, name: str) -> Entity | None:
        for e in self.entities:
            if e.name.lower() == str(name or "").lower():
                return e
        return None

    def by_resource(self, res: str) -> Entity | None:
        for e in self.entities:
            if e.resource == str(res or "").lower():
                return e
        return None

    def to_dict(self) -> dict:
        return {"version": self.version, "extracted_at": self.extracted_at,
                "entities": [e.to_dict() for e in self.entities],
                "flows": [f.to_dict() for f in self.flows],
                "components": [c.to_dict() for c in self.components],
                "invariants": [dict(x) for x in self.invariants],
                "notes": list(self.notes)}


# ---------------------------------------------------------------- 工具

def _singular(w: str) -> str:
    w = str(w or "").lower().strip()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith(("ses", "xes", "ches")):
        return w[:-2]
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _camel(w: str) -> str:
    return "".join(p[:1].upper() + p[1:] for p in re.split(r"[^a-zA-Z0-9]+", str(w or "")) if p)


def _segs(path: str) -> tuple[list[str], int]:
    """路径 → (干净段列表, 第一个参数段的下标；没有则 -1)。

    剥 query（`?action=appeal.create` 不是路径）、剥版本前缀、剥纯数字 id。
    实测 2026-11-08：不剥 query 会造出 `LogsActionAppealCreate` 这种实体，
    不剥数字会造出 `999999` 实体。
    """
    p = str(path or "").split("?", 1)[0].split("#", 1)[0]
    raw = [x for x in p.strip("/").split("/") if x]
    out: list[str] = []
    param_at = -1
    for x in raw:
        if x.startswith("{") or x.startswith("$"):
            if param_at < 0:
                param_at = len(out)
            continue
        low = x.lower()
        if low in _NOISE or re.fullmatch(r"v\d+", low) or re.fullmatch(r"\d+", low):
            continue
        out.append(x)
    return out, param_at


def _resource(path: str) -> str:
    """路径 → 资源（单数）。识别不出资源就返回空，绝不硬造实体。

    规则（REST 惯例，不依赖业务词）：
      1. 有参数段 → 资源是参数段**前面**那一段：/topics/{id}/submit → topic
      2. 末段是 HTTP 动作词 → 资源是它前一段：/auth/register → auth（在噪声表里→空）
      3. 其余取末段：/org/majors → major
    """
    segs, param_at = _segs(path)
    if not segs:
        return ""
    if param_at > 0:
        return _singular(segs[param_at - 1])
    last = segs[-1].lower()
    if last in _ACTION_WORDS:
        return _singular(segs[-2]) if len(segs) >= 2 else ""
    return _singular(last)


def _py_type(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "any"


# ---------------------------------------------------------------- 提取

def extract(bp, ws=None) -> Model:
    """蓝图 → 领域模型。确定性：同蓝图永远同模型。"""
    m = Model(extracted_at=time.time())

    # ① 实体与字段：只从**带请求体的端点**建（无 body 的端点提供不了字段信息，
    #    建出来就是一堆只有 id 的空实体 —— 实测 2026-11-08 造出 45 个，全是垃圾）。
    #    动作端点（/topics/{id}/submit）不建实体，只贡献状态转换。
    seen: dict[str, dict] = {}

    def slot_of(res: str) -> dict:
        # statuses 用**有序列表**（不是 set）：初始态 = 第一次出现的状态。
        # 用 set 排序会把 adopted 排在 pending_teacher 前面 → Candidate 的初始态判错
        return seen.setdefault(res, {"samples": [], "statuses": [], "acts": []})

    def add_status(slot: dict, sts) -> None:
        for s in sts:
            if s and s not in slot["statuses"]:
                slot["statuses"].append(s)

    for c in bp.caps:
        pr = c.probe or {}
        if str(pr.get("kind") or "") != "http":
            continue
        for st in (pr.get("steps") or []):
            st = st or {}
            path = str(st.get("path") or "")
            res = _resource(path)
            if not res:
                continue
            body = st.get("json")
            has_body = isinstance(body, dict) and bool(body)
            sts: set[str] = set()
            ej = st.get("expect_json")
            if isinstance(ej, dict) and isinstance(ej.get("status"), str):
                sts.add(str(ej["status"]))
            for kw in (st.get("body_has") or []):
                mm = re.search(r"status\s*[=＝]\s*['\"]?([a-z_]+)", str(kw))
                if mm:
                    sts.add(mm.group(1))
            if has_body:
                slot_of(res)["samples"].append(body)
                add_status(slot_of(res), sorted(sts))
                segs, param_at = _segs(path)
                if param_at >= 0 and len(segs) > param_at and sts:
                    slot_of(res)["acts"].append(
                        {"action": segs[param_at].lower(), "to": sorted(sts)[0]})
            elif sts:
                add_status(slot_of(res), sorted(sts))

    for res, slot in sorted(seen.items()):
        if not slot["samples"] and not slot["statuses"]:
            continue
        ent = Entity(name=_camel(res), resource=res)
        samples = slot["samples"]
        keys: list[str] = []
        for s in samples:
            for k in s:
                if k not in keys:
                    keys.append(k)
        common = set(samples[0]) if samples else set()
        for s in samples[1:]:
            common &= set(s)
        ent.fields.append(Field(name="id", type="int", required=True, pk=True))
        for k in keys:
            if k == "id":
                continue
            types = {_py_type(s[k]) for s in samples if k in s}
            ty = next(iter(types)) if len(types) == 1 else "any"
            fk = ""
            if str(k).endswith("_id"):
                fk = _camel(_singular(str(k)[:-3]))
            ent.fields.append(Field(name=str(k), type=ty, required=k in common, fk=fk))
        ent.statuses = list(slot["statuses"])
        # 全部标 observed：这些是探针断言里真的出现过的值，属于事实
        ent.status_src = {s: "observed" for s in ent.statuses}
        # 初始态只在**有多个观测状态**时才敢取第一个：单状态时"唯一的状态"
        # 不等于"初始态"（实测 Adjustment 只有 rerun，那是确认调剂后的结果态）。
        # 单状态一律留空，交给语义层补，补不出来就如实报"未定"。
        if len(ent.statuses) > 1:
            ent.initial = ent.statuses[0]
        # 动作端点 → 状态转换（from 取初始态：机械可得；中间态记在 note 里，不假装精确）
        seen_tr: set[tuple[str, str]] = set()
        for a in slot["acts"]:
            k = (ent.initial, a["to"])
            if a["to"] and a["to"] != ent.initial and k not in seen_tr:
                seen_tr.add(k)
                ent.transitions.append(Transition(from_=ent.initial, to=a["to"],
                                                  action=a["action"]))
        # 没有动作端点可依时，至少把"初始态 → 其余每个状态"画出来：
        # 状态图有节点无边等于没有图，而"从初始态出发"是机械成立的（不猜方向）
        for s in ent.statuses:
            if s == ent.initial or (ent.initial, s) in seen_tr:
                continue
            seen_tr.add((ent.initial, s))
            ent.transitions.append(Transition(from_=ent.initial, to=s))
        if ent.transitions:
            m.notes.append(f"{ent.name}: 状态转换按初始态 {ent.initial} 推断，"
                           f"中间态需人工/契约确认")
        m.entities.append(ent)

    # ② 主干流：实体外键拓扑 → 建立端点 + 操作端点（跨资源的真主干）
    m.flows.append(_main_flow(bp, m.entities))

    # ③ 组件与依赖方向：模块 + 分层（api → services → 基础）
    comps: dict[str, Component] = {}
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        for mod in (c.modules or []):
            mod = str(mod)
            if not mod.endswith(".py"):
                continue
            layer = 2
            for pre, lay in _LAYER_OF:
                if mod.startswith(pre):
                    layer = lay
                    break
            comps.setdefault(mod, Component(name=mod, layer=layer))
    names = set(comps)
    for mod, comp in comps.items():
        base = "app/models.py"
        if comp.layer == 0 and base in names:
            comp.depends_on.append(base)
        if comp.layer == 1 and base in names:
            comp.depends_on.append(base)
        if comp.layer == 0:
            svc = f"app/services/{Path(mod).stem}.py"
            if svc in names:
                comp.depends_on.append(svc)
    m.components = [comps[k] for k in sorted(comps)]

    # ④ 外键 → 实体关系（只在两端都存在时连，连不上留 note，不猜）
    for ent in m.entities:
        for f in ent.fields:
            if not f.fk:
                continue
            if m.entity(f.fk) is None:
                m.notes.append(f"{ent.name}.{f.name} 指向不存在的实体 {f.fk}（未建关系）")
                f.fk = ""
    return m


def _entity_topo(ents: list[Entity]) -> list[Entity]:
    """实体按外键拓扑排序。同层**按被引用次数降序**（不是字典序）。

    为什么（2026-11-08 实测）：根实体之间没有外键约束，按字典序会把
    `algorithms/selftest` 排在 `org/colleges` 前面 —— 主干时序看起来像
    "先跑实验再建组织"。入度是结构信号（被多少实体引用 = 越基础），
    比字母序有依据；入度还相同才承认"无依据"（进 order_unknown 缺口）。
    """
    by_name = {e.name.lower(): e for e in ents}
    indeg = {e.name.lower(): 0 for e in ents}
    refs = {e.name.lower(): 0 for e in ents}
    out: dict[str, list[str]] = {e.name.lower(): [] for e in ents}
    for e in ents:
        for f in e.fields:
            if not f.fk:
                continue
            src, dst = f.fk.lower(), e.name.lower()
            if src in by_name and src != dst:
                out[src].append(dst)
                indeg[dst] += 1
                refs[src] += 1
    key = lambda k: (-refs.get(k, 0), k)          # noqa: E731
    ready = sorted((k for k, v in indeg.items() if v == 0), key=key)
    done: list[Entity] = []
    while ready:
        k = ready.pop(0)
        done.append(by_name[k])
        for nxt in sorted(out[k]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=key)
    for e in ents:
        if e not in done:
            done.append(e)
    return done


def _main_flow(bp, ents: list[Entity]) -> Flow:
    """主干时序：**按实体拓扑**串建立端点，再补该实体的操作端点。

    为什么不用"找最长依赖链"（旧尝试，实测 2026-11-08 只得 3 步）：
    能力依赖是稀的（外键边只补了 21 条），最长链根本不跨资源；
    而主干恰恰是**跨资源的**（学院→专业→方向→题目→志愿→批次→结果）。
    实体外键拓扑天然就是这个顺序，且不依赖任何业务词。
    """
    creat: dict[str, dict] = {}
    acts: dict[str, list[dict]] = {}
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        for st in ((c.probe or {}).get("steps") or []):
            st = st or {}
            path = str(st.get("path") or "")
            method = str(st.get("method") or "GET").upper()
            res = _resource(path)
            if not res:
                continue
            body = st.get("json")
            _segs_list, param_at = _segs(path)
            step = {"cap": c.id, "method": method, "path": path}
            # 负向用例不提供"建立端点"：`cap.auth.forbidden_role` 里也有
            # `POST /api/v1/topics`（期望 403），先到先得会把真正的发题端点挤掉
            _stt = st.get("expect_status")
            _neg = (isinstance(_stt, int) and _stt >= 400) or (
                isinstance(_stt, list) and _stt
                and all(isinstance(x, int) and x >= 400 for x in _stt))
            if (method in ("POST", "PUT") and param_at < 0
                    and isinstance(body, dict) and body and not _neg):
                creat.setdefault(res, step)
            elif method in ("POST", "GET") and param_at >= 0:
                acts.setdefault(res, [])
                if all(a["path"] != path for a in acts[res]):
                    acts[res].append(step)

    steps: list[dict] = []
    for e in _entity_topo(ents):
        s = creat.get(e.resource)
        if s:
            steps.append(s)
        for a in acts.get(e.resource, [])[:3]:
            steps.append(a)
    dedup: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for s in steps:
        k = (s["method"], s["path"])
        if k not in seen:
            seen.add(k)
            dedup.append(s)
    return Flow(name="main", steps=dedup)


# ---------------------------------------------------------------- 校验

def validate(m: Model) -> list[str]:
    """模型自洽性。返回问题列表（空 = 通过）。"""
    probs: list[str] = []
    names = {e.name.lower() for e in m.entities}
    for e in m.entities:
        if not e.fields:
            probs.append(f"{e.name}: 没有任何字段")
        if not any(f.pk for f in e.fields):
            probs.append(f"{e.name}: 没有主键")
        for f in e.fields:
            if f.fk and f.fk.lower() not in names:
                probs.append(f"{e.name}.{f.name}: 外键指向不存在的实体 {f.fk}")
        if e.statuses:
            if e.initial and e.initial not in e.statuses:
                probs.append(f"{e.name}: 初始状态 {e.initial} 不在枚举里")
            reach = {e.initial} if e.initial else set()
            changed = True
            while changed:
                changed = False
                for t in e.transitions:
                    if t.from_ in reach and t.to not in reach:
                        reach.add(t.to)
                        changed = True
            unreach = [s for s in e.statuses if s not in reach]
            if e.initial and unreach:
                probs.append(f"{e.name}: 状态 {','.join(unreach)} 从初始态不可达")
    return probs


def check_transition(m: Model, entity: str, from_: str, to: str) -> bool:
    """状态机判据：这个转换合法吗？没建状态机的实体一律放行（不猜）。"""
    e = m.entity(entity)
    if e is None or not e.transitions:
        return True
    return any(t.from_ == from_ and t.to == to for t in e.transitions)


# ---------------------------------------------------------------- 语义补全（远端）

ENRICH_PROMPT = """你是领域建模师。下面这份骨架是**从代码探针机械提取的事实**：
实体、字段、类型、状态枚举，都是代码里真实存在的，**一个字都不许改**。

你只做两件事：
1. 补全**状态转换**：每条边的 from / to / 触发动作 / 允许角色 / 前置条件
2. 列出**业务不变式**：跨实体必须成立的规则（上限、唯一、累计、互斥、顺序）

【事实骨架】
{facts}
（标注 observed 的状态是代码里真实出现过的；只有一个状态的实体，说明探针只测到了
 一个状态，**其余状态需要你按需求补全**）

【需求原文（节选）】
{goal}

只输出 JSON，不要任何解释：
{{"new_statuses":[{{"entity":"Adjustment","name":"pending","why":"需求§十一 调剂推荐生成后待确认"}}],
 "transitions":[{{"entity":"Topic","from":"published","to":"offline","action":"offline",
   "role":"teacher","condition":"仅限本人发布的题目"}}],
 "initials":[{{"entity":"Adjustment","state":"pending"}}],
 "invariants":[{{"entity":"Preference","rule":"一个学生最多填三个志愿","why":"需求§三",
   "assert_kind":"over_limit","limit":3}},
  {{"entity":"Topic","rule":"只有已发布状态的题目可被学生填报志愿","why":"需求§七",
    "assert_kind":"state_guard","guard_state":"published","action_path":"/api/v1/preferences"}}]}}

硬约束（写错会被机械校验丢弃，并在报告里记账）：
- entity 必须是上面列出的实体名，逐字一致；
- `new_statuses` 里每一条都**必须给 why**（引用需求哪一节），没给 why 直接丢弃；
- transitions 的 from/to 必须在"已有枚举 + 你新增的状态"里，逐字一致；
- invariants 每条都要给 `assert_kind`，只能取下列值之一：
{assert_help}
- `over_limit` 要同时给 `limit`（那个上限数字）；
- `state_guard` 要同时给：`guard_state`（允许的那个状态，**必须**是该实体枚举里的值）
  和 `action_path`（受守卫的操作端点路径，必须是蓝图里真实存在的路径）；
- 拿不准的**不要写**，宁可少写（或归到 manual），不要编。
"""


def facts_text(m: Model) -> str:
    """给远端看的事实骨架：只陈述，不引导。"""
    out: list[str] = []
    for e in m.entities:
        line = f"{e.name}（资源 {e.resource}）"
        if e.statuses:
            parts = []
            if e.observed:
                parts.append("observed=" + ",".join(e.observed))
            if e.declared:
                parts.append("declared=" + ",".join(e.declared))
            line += f" 状态({'; '.join(parts)})；初始 {e.initial or '未定'}"
        out.append(line)
        fks = [f"{f.name}→{f.fk}" for f in e.fields if f.fk]
        if fks:
            out.append(f"  外键: {', '.join(fks)}")
        if e.transitions:
            edges = [f"{t.from_}->{t.to}" + (f"({t.action})" if t.action else "")
                     for t in e.transitions]
            out.append(f"  现有边（机械推的，可能错，请以需求为准改正）: {', '.join(edges)}")
    return "\n".join(out) or "（没有提取到实体）"


def merge_semantics(m: Model, data: dict) -> tuple[int, list[str]]:
    """把远端给的语义合并进模型。**校验不通过的一律丢弃**（幻觉拦截）。

    合并规则：某个实体只要远端给了至少一条合法边，就用它**替换**该实体全部机械边
    —— 机械边是"从初始态出发"的猜测，留着只会和真实转换打架（实测 draft->offline）。
    """
    applied = 0
    dropped: list[str] = []

    # ① 远端可以**新增状态**，但必须显式列出并给依据 —— 否则丢弃。
    #    为什么必须开这个口子（实测 2026-11-08）：Adjustment 只观测到 rerun 一个状态，
    #    真正的 pending/confirmed 从没被探针断言过。旧规则"枚举外一律当幻觉"把这
    #    整类实体锁死成建不出边的孤点。现在新增走 declared 通道，与事实分开记。
    for ns in (data.get("new_statuses") or []):
        if not isinstance(ns, dict):
            continue
        ent = m.entity(str(ns.get("entity") or ""))
        name = str(ns.get("name") or "").strip()
        why = str(ns.get("why") or "").strip()
        if ent is None:
            dropped.append(f"新增状态指向未知实体 {ns.get('entity')!r}")
            continue
        if not name or name in ent.statuses:
            continue
        if not why:
            dropped.append(f"{ent.name}.{name}: 新增状态未给依据，丢弃")
            continue
        ent.statuses.append(name)
        ent.status_src[name] = "declared"
        applied += 1

    by_ent: dict[str, list[Transition]] = {}
    for tr in (data.get("transitions") or []):
        if not isinstance(tr, dict):
            continue
        ent = m.entity(str(tr.get("entity") or ""))
        if ent is None:
            dropped.append(f"未知实体 {tr.get('entity')!r}")
            continue
        fr = str(tr.get("from") or "")
        to = str(tr.get("to") or "")
        if fr not in ent.statuses or to not in ent.statuses:
            dropped.append(f"{ent.name}: {fr}→{to} 不在枚举 {ent.statuses}")
            continue
        if fr == to:
            dropped.append(f"{ent.name}: {fr}→{to} 自环无意义")
            continue
        by_ent.setdefault(ent.name, []).append(
            Transition(from_=fr, to=to,
                       action=str(tr.get("action") or ""),
                       role=str(tr.get("role") or "")))
    for name, trs in by_ent.items():
        ent = m.entity(name)
        if ent is None:
            continue
        ent.transitions = trs
        applied += len(trs)

    inv: list[dict] = []
    for it in (data.get("invariants") or []):
        if not isinstance(it, dict):
            continue
        rule = str(it.get("rule") or "").strip()
        if not rule:
            continue
        ent_name = str(it.get("entity") or "").strip()
        if ent_name and m.entity(ent_name) is None:
            dropped.append(f"不变式指向未知实体 {ent_name!r}")
            continue
        inv.append({"entity": ent_name, "rule": rule,
                    "why": str(it.get("why") or ""),
                    "assert_kind": str(it.get("assert_kind") or ""),
                    "limit": int(it.get("limit") or 0),
                    "guard_state": str(it.get("guard_state") or ""),
                    "action_path": str(it.get("action_path") or "")})
    if inv:
        m.invariants = inv

    # ④ 初始态：远端显式给了就用；没给就从转换图推源点（只出不进）
    for ir in (data.get("initials") or []):
        if not isinstance(ir, dict):
            continue
        ent = m.entity(str(ir.get("entity") or ""))
        state = str(ir.get("state") or "").strip()
        if ent is None or state not in ent.statuses:
            dropped.append(f"初始态 {ir.get('entity')}.{state} 不合法")
            continue
        ent.initial = state
    for ent in m.entities:
        if ent.initial or not ent.transitions:
            continue
        tos = {t.to for t in ent.transitions}
        srcs = [s for s in ent.statuses
                if s in {t.from_ for t in ent.transitions} and s not in tos]
        if srcs:
            ent.initial = srcs[0]
            m.notes.append(f"{ent.name}: 初始态由转换图源点推得 = {srcs[0]}（机械可证）")
    return applied, dropped


def enrich(m: Model, llm, goal_text: str, *, max_out: int = 4000) -> dict:
    """远端只补**语义**（状态转换 / 业务不变式），返回 {ok, applied, dropped, usage}。"""
    if not m.entities:
        return {"ok": False, "error": "没有实体，不用补语义", "applied": 0, "dropped": []}
    prompt = (ENRICH_PROMPT.replace("{facts}", facts_text(m))
              .replace("{assert_help}",
                       "\n".join(f"    · {k}：{v}" for k, v in ASSERT_HELP.items()))
              .replace("{goal}", str(goal_text or "")[:6000]))
    r = llm(prompt, task_id="model", node_id="model:enrich",
            json_mode=True, max_out=max_out)
    if not r.get("ok"):
        return {"ok": False, "error": str(r.get("error") or "")[:200],
                "applied": 0, "dropped": []}
    data = _json_of(r.get("answer") or "")
    if not isinstance(data, dict):
        return {"ok": False, "error": "输出不是 JSON 对象", "applied": 0, "dropped": []}
    applied, dropped = merge_semantics(m, data)
    return {"ok": True, "applied": applied, "dropped": dropped,
            "usage": r.get("usage") or {}}


def _json_of(text: str) -> dict | None:
    """从可能带围栏/前后缀的输出里抠出第一个 JSON 对象。"""
    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        got = json.loads(s)
        return got if isinstance(got, dict) else None
    except ValueError:
        pass
    start = s.find("{")
    while start >= 0:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        got = json.loads(s[start:i + 1])
                        if isinstance(got, dict):
                            return got
                    except ValueError:
                        pass
                    break
        start = s.find("{", start + 1)
    return None


# ---------------------------------------------------------------- 缺口机制

@dataclass
class Gap:
    """模型里"不知道"的地方。**不猜**：能机械消解就消解，不能就变成待办。

    为什么要这个机制（2026-11-08）：模型必然不完整 —— 探针只断言了它测过的状态、
    主干只覆盖了被测过的路径。旧做法是在缺失处**填默认值**（initial=statuses[0]、
    同层按字典序），于是"不知道"被伪装成"知道"，错的地方看不出来。
    缺口机制把不确定性显式化：每一项都带 kind / 证据 / 消解方式，
    报告里能看见、能排期、能验收。
    """
    kind: str
    entity: str = ""
    detail: str = ""
    fixable: bool = False        # 引擎能否自动消解
    how: str = ""               # 消解方式；不能消解时写"需要什么证据"
    probe: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "entity": self.entity, "detail": self.detail,
             "fixable": self.fixable, "how": self.how}
        if self.probe:
            d["probe"] = self.probe
        return d


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 业务不变式能落到哪几种**可执行**断言上。分类由远端做（自然语言归类比写正则准），
# 模板由引擎出（每种模板的执行方式是确定的，不让模型自由发挥）。
ASSERT_KINDS = ("over_limit", "unique", "not_negative", "state_guard", "manual")

ASSERT_HELP = {
    "over_limit": "数量上限：连续创建到第 N+1 个应被拒",
    "unique": "唯一/互不重复：同一业务键提交两次应被拒或返回同一条",
    "not_negative": "数值下界：提交负值应被拒",
    "state_guard": "状态守卫：只有某状态允许该操作，其它状态调用应被拒",
    "manual": "以上都不是，或需要跨请求聚合才能判定",
}


def _limit_of(rule: str) -> int | None:
    """从自然语言里抠出数量上限（"最多三个志愿" → 3）。抠不出返回 None，不硬猜。

    要容忍中间夹动词："最多**填**三个"、"上限**为** 5 条" —— 直接贴着数字匹配会漏，
    而漏了就等于把可执行规则降级成手工待办（实测 2026-11-08）。
    """
    mm = re.search(r"(?:最多|至多|不超过|上限(?:为)?)\s*"
                   r"[^0-9一二两三四五六七八九十]{0,6}?"
                   r"([0-9]+|[一二两三四五六七八九十]+)",
                   str(rule or ""))
    if not mm:
        return None
    tok = mm.group(1)
    if tok.isdigit():
        return int(tok)
    if len(tok) == 1:
        return _CN_NUM.get(tok)
    # "十二" 这种组合：只认十位 + 个位，别的形态不猜
    if tok.startswith("十") and len(tok) == 2:
        return 10 + _CN_NUM.get(tok[1], 0)
    if tok.endswith("十") and len(tok) == 2:
        return _CN_NUM.get(tok[0], 0) * 10
    return None


def _invariant_covered(m: Model, bp, ent_name: str, limit: int) -> str:
    """蓝图里是否已有能力覆盖这条"最多 N"的不变式？返回覆盖它的 cap id。

    为什么要先认领（2026-11-08 打脸）：我原以为业务不变式"只记录不执行"，
    实际上 `cap.pref.reject_fourth` 早就把它测穿了 —— 填 rank=1/2/3 各 201，
    重复 rank=1 给 422 rank_conflict，填 rank=4 给 422 preference_limit。
    缺的不是"执行"，是**缺口机制不知道已有资产存在**，于是还在报"需要探针"，
    真按它去做就会重复造一条更差的探针，白烧预算。

    机械判据（只看探针形状，不读描述文字，不做语义猜测）：
      1. 该资源的成功创建步骤 ≥ limit 次（2xx）
      2. 且该资源至少被明确拒绝过一次（4xx）
    两条同时成立 = "前 N 个能建、再往下被拒"已经被测过了。
    """
    if bp is None:
        return ""
    ent = m.entity(ent_name)
    res = ent.resource if ent else _singular(ent_name)
    if not res:
        return ""
    for c in bp.caps:
        pr = c.probe or {}
        if str(pr.get("kind") or "") != "http":
            continue
        ok = denied = 0
        for st in (pr.get("steps") or []):
            st = st or {}
            if _resource(str(st.get("path") or "")) != res:
                continue
            stt = st.get("expect_status")
            if isinstance(stt, int):
                if 200 <= stt < 300:
                    ok += 1
                elif 400 <= stt < 500:
                    denied += 1
            elif isinstance(stt, list) and stt and all(
                    isinstance(x, int) and 200 <= x < 300 for x in stt):
                ok += 1
        if ok >= limit and denied:
            return c.id
    return ""


def gaps(m: Model, bp=None) -> list[Gap]:
    """收集全部缺口。纯静态，零 token。

    `bp` 给了就先**认领**：蓝图里已有能力覆盖的不变式不再报缺口（避免重复造探针）。
    """
    out: list[Gap] = []
    for e in m.entities:
        if not e.statuses:
            continue
        if not e.initial:
            src = [s for s in e.statuses
                   if s in {t.from_ for t in e.transitions}
                   and s not in {t.to for t in e.transitions}]
            out.append(Gap(
                kind="initial_unknown", entity=e.name,
                detail=f"状态 {','.join(e.statuses)}；未观测到初始态"
                       + (f"（图论源点候选 {','.join(src)}）" if src else ""),
                fixable=bool(src),
                how="用只出不进的源点作为初始态" if src
                    else "需要一条探针：创建后立即读状态，断言之"))
        if len(e.statuses) == 1:
            out.append(Gap(
                kind="single_state", entity=e.name,
                detail=f"只观测到唯一状态 {e.statuses[0]}，无法形成转换边",
                fixable=False,
                how=f"需要探针覆盖该资源的其他状态（创建后/操作后各读一次）；"
                    f"或由需求声明完整枚举"))
            continue
        if not e.transitions:
            out.append(Gap(
                kind="no_transition", entity=e.name,
                detail=f"有 {len(e.statuses)} 个状态但没有任何转换",
                fixable=False,
                how="需要语义补全（远端按需求补 transitions）或动作端点探针"))
            continue
        reach = {e.initial} if e.initial else set()
        changed = True
        while changed:
            changed = False
            for t in e.transitions:
                if t.from_ in reach and t.to not in reach:
                    reach.add(t.to)
                    changed = True
        un = [s for s in e.statuses if s not in reach]
        if e.initial and un:
            out.append(Gap(
                kind="unreachable", entity=e.name,
                detail=f"状态 {','.join(un)} 从 {e.initial} 不可达",
                fixable=False,
                how="补一条进入该状态的转换，或确认该状态是否真的存在"))

    # 同层顺序：没有外键关系的实体之间，谁先谁后没有结构依据
    indeg = {e.name: 0 for e in m.entities}
    for e in m.entities:
        for f in e.fields:
            if f.fk and f.fk in indeg:
                indeg[f.fk] += 1
    roots = [e.name for e in m.entities
             if not any(f.fk for f in e.fields) and indeg.get(e.name, 0) == 0]
    if len(roots) > 1:
        out.append(Gap(
            kind="order_unknown", entity=",".join(roots[:6]),
            detail=f"{len(roots)} 个根实体既无外键也不被引用，彼此顺序无结构依据",
            fixable=True,
            how="按被引用次数（入度）降序；入度相同仍无依据，按需求流程序"))

    for it in m.invariants:
        rule = str(it.get("rule") or "")
        ent = str(it.get("entity") or "")
        lim = _limit_of(rule)
        if lim is not None:
            cover = _invariant_covered(m, bp, ent, lim)
            if cover:
                m.notes.append(f"不变式「{rule}」已由 {cover} 覆盖，不另建探针")
                continue
            out.append(Gap(
                kind="invariant_limit", entity=ent, detail=f"{rule}（上限 {lim}）",
                fixable=False,
                how=f"需要一条负向探针：构造第 {lim + 1} 个同类请求，断言 4xx",
                probe={"kind": "http", "intent": "over_limit",
                       "limit": lim, "resource": (m.entity(ent).resource if m.entity(ent) else "")}))
        else:
            out.append(Gap(
                kind="invariant_manual", entity=ent, detail=rule,
                fixable=False,
                how="自然语言规则，暂无法机械转断言：需人工确认判据或补专用探针"))
    return out


def resolve_gaps(m: Model, bp=None) -> list[Gap]:
    """自动消解可消解的缺口，返回**仍未解决**的清单。

    目前能自动消解的只有两类，且都有机械依据（不是猜）：
      · initial_unknown + 有转换 → 图论源点（只出不进的那个状态）
      · order_unknown → 由 _entity_topo 的入度排序处理（这里只记录已消解）
    已在蓝图里被能力覆盖的不变式，在 gaps() 阶段就认领掉了，不会走到这里。
    """
    left: list[Gap] = []
    for g in gaps(m, bp):
        if g.kind == "initial_unknown":
            e = m.entity(g.entity)
            if e is not None:
                src = [s for s in e.statuses
                       if s in {t.from_ for t in e.transitions}
                       and s not in {t.to for t in e.transitions}]
                if src:
                    e.initial = src[0]
                    m.notes.append(f"{e.name}: 初始态由转换图源点推得 = {src[0]}（机械可证）")
                    continue
        if g.kind == "order_unknown" and g.fixable:
            m.notes.append("根实体顺序按入度降序排列（同入度仍无依据，按需求流程序）")
            continue
        left.append(g)
    return left


def _inv_cap_id(ent_name: str, kind: str, rule: str) -> str:
    """不变式能力的 id：实体 + 类型 + **规则指纹**。

    只按"实体+类型"会撞车（Preference 有两条 unique：「同一题目只能填一次」
    和「志愿序不可重复」），撞了就会互相覆盖，白做一条。用规则文本的短哈希，
    顺序变了也不影响 id（确定性）。
    """
    import hashlib
    h = hashlib.sha1(str(rule or "").encode("utf-8")).hexdigest()[:4]
    return f"cap.inv.{_snake(ent_name)}.{kind}_{h}"


def invariant_probes(m: Model, bp) -> list[dict]:
    """把不变式变成**可执行探针骨架**。用真实 body 样本，不凭空造字段。

    返回 [{cap_id, desc, kind, steps, why}]，可直接当能力塞进蓝图。

    为什么模板化（2026-11-08）：自然语言规则若让模型自由生成断言，它一定编字段；
    若让引擎用正则啃，只能认出"最多 N 个"这一种（13 条里只认出 1 条）。
    折中是让远端只**归类**到 5 种已知形态，引擎按固定模板执行 ——
    分类错了有白名单拦，模板执行不了就退回 manual，两边都不撒谎。
    """
    out: list[dict] = []
    if bp is None:
        return out
    for it in m.invariants:
        kind = str(it.get("assert_kind") or "").strip()
        if kind not in ASSERT_KINDS or kind == "manual":
            continue
        ent_name = str(it.get("entity") or "")
        ent = m.entity(ent_name)
        # state_guard 允许 entity 标错：它常常指向"操作对象"（Preference）而不是
        # "被守卫的资源"（Topic），下面用 guard_state 反查纠正。
        if ent is None and kind != "state_guard":
            continue
        rule = str(it.get("rule") or "")
        lim = int(it.get("limit") or 0) or (_limit_of(rule) or 0)
        # 覆盖检查只对 over_limit 成立：它的判据是"成功 N 次 + 被拒过"。
        # unique / not_negative 没有 N，套同一个判据会被"最多三志愿"那条能力
        # 误覆盖（实测 2026-11-08：4 条 unique 全被 reject_fourth 吃掉，一条没生成）。
        if kind == "over_limit" and _invariant_covered(m, bp, ent_name, lim or 1):
            continue
        # state_guard 不走这个前置（它的 entity 可能标错，要等纠正后才谈得上找创建端点）
        method = path = ""
        body: dict = {}
        if kind != "state_guard":
            maker = _creator_step(m, bp, ent.resource if ent else "")
            if maker is None:
                continue                  # 找不到真实请求体 → 不造
            method, path, body = maker

        if kind == "over_limit":
            n = lim or 2
            steps = []
            seq_key = _seq_field(body)
            for i in range(1, n + 2):
                b = dict(body)
                if seq_key and isinstance(b.get(seq_key), int):
                    b[seq_key] = i
                steps.append({"method": method, "path": path, "json": b,
                              "expect_status": 201 if i <= n else [400, 409, 422]})
            out.append({"cap_id": _inv_cap_id(ent_name, f"max_{n}", rule),
                        "desc": f"不变式：{rule}（创建第 {n + 1} 个应被拒）",
                        "kind": kind, "steps": steps, "why": rule})
        elif kind == "unique":
            steps = [{"method": method, "path": path, "json": dict(body),
                      "expect_status": 201},
                     {"method": method, "path": path, "json": dict(body),
                      "expect_status": [400, 409, 422]}]
            out.append({"cap_id": _inv_cap_id(ent_name, "unique", rule),
                        "desc": f"不变式：{rule}（重复提交应被拒）",
                        "kind": kind, "steps": steps, "why": rule})
        elif kind == "not_negative":
            key = _num_field(body)
            if not key:
                continue
            bad = dict(body)
            bad[key] = -1
            steps = [{"method": method, "path": path, "json": bad,
                      "expect_status": [400, 422]}]
            out.append({"cap_id": _inv_cap_id(ent_name, f"no_neg_{key}", rule),
                        "desc": f"不变式：{rule}（{key} 为负应被拒）",
                        "kind": kind, "steps": steps, "why": rule})
        elif kind == "state_guard":
            # 状态守卫有**两种**形态，靠"初始态是否就是允许态"机械判定：
            #   A. 初始态 ≠ 允许态（如题目 draft 但要求 published 才能被填报）
            #      → 刚创建就去调，必须被拒（4xx）
            #   B. 初始态 == 允许态（如题目 draft 才可提交审核）
            #      → 第一次调用成功把状态推走，第二次必须被拒（4xx）
            # 两种都用真实请求体，不编造中间状态；判不了就不造（宁可少一条）。
            guard = str(it.get("guard_state") or "")
            # guard_state 常被标到"操作对象"上，而不是"被守卫的资源"上
            # （实测 2026-11-09：「只有已发布状态的题目可被学生填报志愿」被标成
            #  entity=Preference，但 published 是 Topic 的状态 → 整条被跳过）。
            # 用状态反查唯一持有它的实体，唯一才纠正；不唯一就放弃（不猜）。
            if ent is None or (guard and guard not in ent.statuses):
                owners = [x for x in m.entities if guard and guard in x.statuses]
                if len(owners) != 1:
                    continue
                ent = owners[0]
                ent_name = ent.name
            if ent is None or not guard or guard not in ent.statuses:
                continue
            act = _action_step(bp, ent, guard, str(it.get("action_path") or ""))
            if act is None:
                continue
            amethod, apath, abody = act
            if ent.initial and ent.initial != guard:
                maker = _creator_step(m, bp, ent.resource)
                if maker is None:
                    continue
                cmethod, cpath, cbody = maker
                var = f"{ent.resource}_id"
                steps = [
                    {"method": cmethod, "path": cpath, "json": dict(cbody),
                     "expect_status": [200, 201], "save": {var: ["id"]}},
                    {"method": amethod, "path": _fill_params(apath, var),
                     "json": dict(abody), "expect_status": [400, 403, 409, 422]},
                ]
                out.append({"cap_id": _inv_cap_id(ent_name, f"guard_{guard}", rule),
                            "desc": f"不变式：{rule}（对象刚创建还不是 {guard} 时，"
                                    f"调用 {apath} 必须被拒）",
                            "kind": kind, "steps": steps, "why": rule})
            else:
                steps = [
                    {"method": amethod, "path": apath, "json": dict(abody),
                     "expect_status": [200, 201]},
                    {"method": amethod, "path": apath, "json": dict(abody),
                     "expect_status": [400, 403, 409, 422]},
                ]
                out.append({"cap_id": _inv_cap_id(ent_name, f"guard_twice_{guard}", rule),
                            "desc": f"不变式：{rule}（同一操作连调两次，"
                                    f"第二次因状态已变必须被拒）",
                            "kind": kind, "steps": steps, "why": rule})
    return out


def _fill_params(path: str, var: str) -> str:
    """`/api/v1/topics/{topic_id}/submit` → `/api/v1/topics/${topic_id}/submit`。

    参数名统一成 `<资源>_id`，与创建步骤 save 的键对上（探针按名取变量）。
    """
    return re.sub(r"\{[^}]+\}", "${%s}" % var, str(path or ""))


def _action_step(bp, ent, guard: str, action_path: str) -> tuple[str, str, dict] | None:
    """受守卫的操作端点：(method, path, body)。三级回退，都不猜。

    ① 远端给的 action_path，**必须**是蓝图里真实存在的路径（它经常编：
       `/batches/{id}/run_matching`、`/candidates/{id}/adopt` 实际都不存在）；
    ② 用**状态机反查**：从 guard_state 出发的那条转换，其 action 名（`run_matching`、
       `adopt`、`review_approve`）就是操作名，拿它在蓝图里找路径末段匹配的端点。
       这一级不依赖中文词表，只依赖模型自己的转换图 —— 换项目照样成立；
    ③ 最后才退回"路径含该资源 + 带参数段"的 POST。
    """
    if bp is None:
        return None
    cands: list[dict] = []
    for c in bp.caps:
        pr = c.probe or {}
        if str(pr.get("kind") or "") != "http":
            continue
        for st in (pr.get("steps") or []):
            st = st or {}
            if str(st.get("method") or "").upper() not in ("POST", "PUT", "DELETE"):
                continue
            p = str(st.get("path") or "")
            if not p:
                continue
            _stt = st.get("expect_status")
            if isinstance(_stt, int) and _stt >= 400:
                continue
            cands.append(st)

    def pack(st: dict) -> tuple[str, str, dict]:
        return (str(st.get("method") or "POST").upper(), str(st.get("path") or ""),
                dict(st.get("json") or {}))

    if action_path:
        for st in cands:
            if str(st.get("path") or "") == action_path:
                return pack(st)
        # 给了但不存在 → 不信，继续往下找（不能因为一条假路径整条不变式作废）

    for t in (ent.transitions if ent is not None else []):
        if t.from_ != guard or not t.action:
            continue
        words = {w for w in re.split(r"[^a-z0-9]+", t.action.lower()) if len(w) >= 3}
        for st in cands:
            p = str(st.get("path") or "")
            tail = re.split(r"[^a-z0-9]+", p.lower())
            if any(w in tail for w in words):
                return pack(st)

    for st in cands:
        p = str(st.get("path") or "")
        _s, param_at = _segs(p)
        if param_at >= 0 and ent is not None and _resource(p) == ent.resource:
            return pack(st)
    return None


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", str(name or "")).lower()


def _seq_field(body: dict) -> str:
    """请求体里最像"序号/排名"的整数字段（rank/seq/order/index/no）。"""
    for k in body:
        if isinstance(body[k], int) and str(k).lower() in (
                "rank", "seq", "sequence", "order", "index", "no", "position"):
            return str(k)
    return ""


def _num_field(body: dict) -> str:
    for k in body:
        if isinstance(body[k], int) and str(k).lower() not in (
                "id", "rank", "seq", "sequence", "order", "index", "no", "position"):
            return str(k)
    return ""


def _creator_step(m: Model, bp, resource: str) -> tuple[str, str, dict] | None:
    """该资源的**真实创建请求**：(method, path, body)，来自蓝图探针样本。"""
    if not resource or bp is None:
        return None
    for c in bp.caps:
        pr = c.probe or {}
        if str(pr.get("kind") or "") != "http":
            continue
        for st in (pr.get("steps") or []):
            st = st or {}
            if str(st.get("method") or "").upper() not in ("POST", "PUT"):
                continue
            if _resource(str(st.get("path") or "")) != resource:
                continue
            body = st.get("json")
            if not isinstance(body, dict) or not body:
                continue
            _s, param_at = _segs(str(st.get("path") or ""))
            if param_at >= 0:
                continue                  # 操作端点不是创建端点
            _stt = st.get("expect_status")
            if isinstance(_stt, int) and _stt >= 400:
                continue                  # 负向用例不提供样本
            return (str(st.get("method") or "POST").upper(),
                    str(st.get("path") or ""), dict(body))
    return None


# ---------------------------------------------------------------- 存取

def save(m: Model, ws) -> Path:
    p = Path(ws) / MODEL_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m.to_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def load(ws) -> Model | None:
    p = Path(ws) / MODEL_FILE
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    m = Model(version=int(raw.get("version") or MODEL_VERSION),
              extracted_at=float(raw.get("extracted_at") or 0.0),
              notes=[str(x) for x in (raw.get("notes") or [])])
    for er in (raw.get("entities") or []):
        ent = Entity(name=str(er.get("name") or ""),
                     resource=str(er.get("resource") or ""),
                     statuses=[str(x) for x in (er.get("statuses") or [])],
                     initial=str(er.get("initial") or ""),
                     status_src={str(k): str(v) for k, v in
                                 (er.get("status_src") or {}).items()})
        for fr in (er.get("fields") or []):
            ent.fields.append(Field(name=str(fr.get("name") or ""),
                                    type=str(fr.get("type") or "any"),
                                    required=bool(fr.get("required")),
                                    pk=bool(fr.get("pk")),
                                    fk=str(fr.get("fk") or ""),
                                    enum=[str(x) for x in (fr.get("enum") or [])]))
        for tr in (er.get("transitions") or []):
            ent.transitions.append(Transition(from_=str(tr.get("from") or ""),
                                              to=str(tr.get("to") or ""),
                                              action=str(tr.get("action") or ""),
                                              role=str(tr.get("role") or "")))
        m.entities.append(ent)
    for fr in (raw.get("flows") or []):
        m.flows.append(Flow(name=str(fr.get("name") or ""),
                            steps=[dict(s) for s in (fr.get("steps") or [])]))
    for cr in (raw.get("components") or []):
        m.components.append(Component(name=str(cr.get("name") or ""),
                                      layer=int(cr.get("layer") or 2),
                                      depends_on=[str(x) for x in (cr.get("depends_on") or [])]))
    m.invariants = [dict(x) for x in (raw.get("invariants") or []) if isinstance(x, dict)]
    return m


def ensure(bp, ws, write: bool = True) -> Model:
    """拿到模型：有就用（并增量补新实体），没有就从蓝图提。

    `write=False` 用于只读查看：**仍然要带上磁盘上的语义层**（transitions /
    invariants / status_src），否则 `model` 审阅模式会看到一份"没有不变式"的模型，
    于是探针生成永远是 0 条（实测 2026-11-08，排查了半小时）。
    """
    old = load(ws)
    fresh = extract(bp, ws)
    if old is None:
        if write:
            save(fresh, ws)
        return fresh
    # 只补新实体/新字段，不删旧的（旧的是既有代码的事实，不能因为蓝图暂时没提到就抹掉）
    known = {e.name.lower() for e in old.entities}
    for e in fresh.entities:
        if e.name.lower() not in known:
            old.entities.append(e)
            continue
        cur = old.entity(e.name)
        have = {f.name for f in cur.fields}
        for f in e.fields:
            if f.name not in have:
                cur.fields.append(f)
        for s in e.statuses:
            if s not in cur.statuses:
                cur.statuses.append(s)
                cur.status_src[s] = e.status_src.get(s, "observed")
    if not old.flows:
        old.flows = fresh.flows
    if not old.components:
        old.components = fresh.components
    old.extracted_at = fresh.extracted_at
    if write:
        save(old, ws)
    return old


# ---------------------------------------------------------------- 渲染（Mermaid）

def to_mermaid(m: Model) -> dict[str, str]:
    """一份模型 → 四张图（ER / 状态 / 时序 / 组件）。文档与代码共用同一份真相。"""
    out: dict[str, str] = {}

    lines = ["erDiagram"]
    for e in m.entities:
        lines.append(f"  {e.name} {{")
        for f in e.fields:
            mark = " PK" if f.pk else (" FK" if f.fk else "")
            lines.append(f"    {f.type} {f.name}{mark}")
        lines.append("  }")
    for e in m.entities:
        for f in e.fields:
            if f.fk:
                lines.append(f'  {e.name} }}o--|| {f.fk} : "{f.name}"')
    out["er"] = "\n".join(lines)

    lines = ["stateDiagram-v2"]
    for e in m.entities:
        if not e.statuses:
            continue
        lines.append("  state " + e.name + " {")
        if e.initial:
            lines.append("    [*] --> " + e.initial)
        for t in e.transitions:
            label = t.action or ""
            lines.append(f"    {t.from_} --> {t.to}" + (f" : {label}" if label else ""))
        lines.append("  }")
    out["state"] = "\n".join(lines)

    # 时序图：每个资源一个参与者（旧实现把 participant 写死成 S，
    # 于是"客户端调了 8 个不同资源"看起来像在跟同一个对象说话）
    lines = ["sequenceDiagram", "  participant C as Client"]
    roles: list[str] = []
    for fl in m.flows:
        for s in fl.steps:
            r = _camel(_resource(s.get("path") or "")) or "API"
            if r not in roles:
                roles.append(r)
    for r in roles:
        lines.append(f"  participant {r} as {r}")
    for fl in m.flows:
        for s in fl.steps:
            r = _camel(_resource(s.get("path") or "")) or "API"
            lines.append(f"  C->>{r}: {s.get('method')} {s.get('path')}")
            lines.append(f"  {r}-->>C: 200")
    out["sequence"] = "\n".join(lines)

    lines = ["graph TD"]
    for c in m.components:
        nid = re.sub(r"[^A-Za-z0-9]", "_", c.name)
        lines.append(f'  {nid}["{c.name}"]')
    for c in m.components:
        nid = re.sub(r"[^A-Za-z0-9]", "_", c.name)
        for d in c.depends_on:
            lines.append(f"  {nid} --> {re.sub(r'[^A-Za-z0-9]', '_', d)}")
    out["component"] = "\n".join(lines)
    return out
