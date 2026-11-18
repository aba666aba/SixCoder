"""判据修复器（2026-11-18 用户口径：审判官 + 写作者共同投票后可改判据）。

## 为什么要它

真实夜班 23:10-23:42：写作者 6 轮白改，两名审核者（各自独立 key）投票都投
`keep`，理由一致 —— **判据自身互斥**，任何实现都不可能同时变绿：
  · 20 个能力里"第二个学生"沿用第一个学生的 username，两次注册都期望 201；
    而 cap.api.idempotent 要求同名重复注册必须 4xx —— 同一份代码不可能同时满足。
  · 同一 `POST /api/v1/matching/runs/`，13 条能力期望 status="done"，
    2 条期望 "completed"（产物实际返回 done）。
用户定稿：这种情况让**审判官和写作者共同投票**决定"改判据"，而不是收工等人。

## 只做**确定性、可复验**的两类修复（不花钱、不猜）

1. `probe.distinct_same_role_users`：同角色多账号拆成不同用户名（s / s_2 / s_3…），
   login 按出现顺序对应到各自账号；
2. `probe.dedupe_role_users`：同名多角色串味修补（存量规则，幂等）；
3. **status 词表统一**：同一 (method, path) 上多条能力对同一个 json 字段的枚举值
   不一致时，按**多数派**统一（少数派改写），并在 notes 里如实报告。
   只动 `expect_json`/`expect_contains` 里的**同名枚举字段**，不碰数值/文本断言。

修完落盘前**自动备份**；返回改了哪些能力，供夜班日志与用户核对。
"""
from __future__ import annotations

import json
import shutil
import time
from collections import Counter
from pathlib import Path

# 可统一的枚举字段名（保守白名单：只碰这些"状态词"）
_STATUS_FIELDS = ("status", "state", "phase")


def _bp_path(ws: Path) -> Path:
    return Path(ws) / ".sixcoder" / "blueprint.json"


def _steps(cap: dict) -> list:
    return ((cap.get("probe") or {}).get("steps")) or []


def unify_status_vocab(caps: list) -> tuple[int, list[str]]:
    """同一 (method, path) 的 status 枚举按多数派统一。返回 (改动数, notes)。"""
    # 收集： (method, path, field) -> [(cap_id, 值, 引用容器, 键)]
    seen: dict[tuple, list] = {}
    for c in caps:
        for s in _steps(c):
            if not isinstance(s, dict):
                continue
            key = (str(s.get("method") or "GET").upper(), str(s.get("path") or ""))
            for field in _STATUS_FIELDS:
                for holder in ("expect_json", "expect_contains"):
                    box = s.get(holder)
                    if isinstance(box, dict) and field in box:
                        seen.setdefault(key + (field,), []).append(
                            (str(c.get("id") or ""), str(box.get(field)), box, field))
    changed, notes = 0, []
    for key, rows in seen.items():
        values = Counter(r[1] for r in rows)
        if len(values) < 2:
            continue
        top, top_n = values.most_common(1)[0]
        if top_n < 2:                       # 多数派不成立就别乱统一
            continue
        for cap_id, val, box, field in rows:
            if val == top:
                continue
            old = box[field]
            box[field] = top
            changed += 1
            notes.append("%s: %s %s 的 %s=%s → %s（按多数派统一）"
                         % (cap_id, key[0], key[1], field, old, top))
    return changed, notes


def _ok_create_titles(steps: list) -> list[str]:
    """链里**真建成**的标题（POST 成功 2xx 且带 json.title）。"""
    out = []
    for s in steps:
        st = s.get("expect_status")
        sts = st if isinstance(st, list) else ([st] if st is not None else [])
        if not any(isinstance(x, int) and 200 <= x < 300 for x in sts):
            continue
        j = s.get("json") if isinstance(s.get("json"), dict) else {}
        t = j.get("title")
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def _teacher_login_users(steps: list) -> list[str]:
    """链里的**教师账号**（按出现顺序）。

    必须从 `/register` 的 `json.role=="teacher"` 取 —— 登录步骤的 json 只有
    username/password，没有 role（实测：cap.topic.detail 的教师账号名里
    "teacher" 都没有，靠登录步骤根本认不出来）。
    """
    out = []
    for s in steps:
        if "/register" not in str(s.get("path") or ""):
            continue
        j = s.get("json") if isinstance(s.get("json"), dict) else {}
        if str(j.get("role") or "").strip().lower() != "teacher":
            continue
        u = str(j.get("username") or "").strip()
        if u and u not in out:
            out.append(u)
    return out


def dedupe_identical_steps(steps: list, cap_id: str = "") -> tuple[list, list[str]]:
    """同一链里**完全相同的请求且期望一致**重复出现 → 只留第一步。

    实测形态（2026-11-19）：cap.topic.detail / notfound / review_queue 里
    "同一教师建第二题要 403" 被连写两遍（第 4、5 步一模一样），而
    cap.topic.teacher_quota 又要求同一场景返回 409「配额已满」——
    同一件事两个互斥答案，写作者无论怎么改都必红。幂等类能力豁免（它就是要重复）。
    """
    if "idempotent" in str(cap_id or "").lower():
        return steps, []
    seen: dict[str, int] = {}
    keep, removed = [], []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            keep.append(s); continue
        key = json.dumps({k: s.get(k) for k in ("method", "path", "json", "expect_status")},
                         ensure_ascii=False, sort_keys=True)
        if key in seen:
            removed.append("%s: 删除第%d步（与第%d步完全重复：%s %s）"
                           % (cap_id, i + 1, seen[key] + 1,
                              s.get("method"), s.get("path")))
            continue
        seen[key] = i
        keep.append(s)
    return keep, removed


def fix_dangling_body_has(steps: list, cap_id: str = "") -> tuple[int, list[str]]:
    """body_has 断言了**从没建成**的数据 → 404 响应直接删，其他换成链里真建成的标题。

    实测形态：detail/search/review_queue 断言 body_has="测试题目"，但链里只建成
    「图神经网络推荐」「微服务网关设计」；notfound 是 404 响应却要求 body 含题目名。
    """
    titles = _ok_create_titles(steps)
    notes = []
    n = 0
    # **只动"占位标题"这一类**（2026-11-19 收紧）：body_has 里可能是用户名、
    # 错误文案（quota_overflow）等**非标题断言**，把它们换成标题 = 把判据改坏
    # （实测误伤 cap.matching.stability_positive_control 的 u_bp_stu1）。
    _PLACEHOLDER_TITLES = ("测试题目", "待定题目", "example", "test topic")
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        bh = s.get("body_has")
        if isinstance(bh, str):                 # 单值写法（实测大量存在）
            bh = [bh]
        if not isinstance(bh, list) or not bh:
            continue
        st = s.get("expect_status")
        sts = st if isinstance(st, list) else ([st] if st is not None else [])
        is_404 = any(x == 404 for x in sts)
        kept = []
        for item in bh:
            item = str(item)
            if item in titles:
                kept.append(item); continue
            if item not in _PLACEHOLDER_TITLES:
                kept.append(item); continue     # 非占位断言一律不动
            if is_404:
                n += 1
                notes.append("%s: 第%d步 404 响应删掉 body_has=%s（判据自相矛盾：404 却要题目名）"
                             % (cap_id, i + 1, item))
                continue
            if titles:
                kept.append(titles[-1])
                n += 1
                notes.append("%s: 第%d步 body_has %s → %s（换成链里真建成的标题）"
                             % (cap_id, i + 1, item, titles[-1]))
            else:
                n += 1
                notes.append("%s: 第%d步 删除占位 body_has=%s（链里没有建成任何标题）"
                             % (cap_id, i + 1, item))
        if kept:
            s["body_has"] = kept
        else:
            s.pop("body_has", None)
    return n, notes


def align_teacher_identity(steps: list, cap_id: str = "") -> tuple[int, list[str]]:
    """教师身份对齐：教师档案 username / 详情里的 teacher_username 必须等于链里登录的教师。

    实测形态：
      · cap.topic.teacher_quota 第 4 步用 admin 建档案 {"username": "t1"}，但登录的
        教师是 u_cap_topic_teacher_qu_teacher → 配额永远不生效（假 bug）；
      · cap.topic.detail 期望 teacher_username="t1"，实际发布者是登录教师自己。
    """
    teachers = _teacher_login_users(steps)
    if not teachers:
        return 0, []
    t = teachers[0]
    n, notes = 0, []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        j = s.get("json") if isinstance(s.get("json"), dict) else None
        if j and "/people/teachers" in str(s.get("path") or ""):
            u = str(j.get("username") or "")
            if u and u != t:
                j["username"] = t
                n += 1
                notes.append("%s: 第%d步 教师档案 %s → %s（与登录教师一致，否则配额无效）"
                             % (cap_id, i + 1, u, t))
        ej = s.get("expect_json") if isinstance(s.get("expect_json"), dict) else None
        if ej and "teacher_username" in ej:
            cur = str(ej.get("teacher_username") or "")
            if cur and cur != t:
                ej["teacher_username"] = t
                n += 1
                notes.append("%s: 第%d步 teacher_username %s → %s（发布者就是登录教师）"
                             % (cap_id, i + 1, cur, t))
    return n, notes


# ---------------------------------------------------------------- 远端处方执行
# 2026-11-19 用户口径：**别让本地列举规则类型**（覆盖不到新形态就得改代码），
# 让审核者直接开处方 —— 它们看得到真实步骤，输出结构化操作；引擎只负责
# **校验 + 机械执行 + 备份**。多把独立 key 的多数决就是保险。
_PATCH_FIELDS = ("expect_status", "expect_json", "expect_contains", "body_has",
                 "body_not_has", "json", "path", "method", "save", "headers")
_PATCH_JSON_KEYS_MAX = 40
_PATCH_OPS_MAX = 60


def valid_patch_op(op) -> tuple[bool, str]:
    """补丁操作是否可执行（白名单 + 形状校验）。返回 (ok, 说明)。"""
    if not isinstance(op, dict):
        return False, "不是对象"
    kind = str(op.get("op") or "").strip()
    cap = str(op.get("cap") or "").strip()
    if not cap:
        return False, "缺 cap"
    # step：**步骤级操作必填**；能力级操作（set_cap / replace_probe）可省略
    _needs_step = kind in ("delete_step", "add_step", "set_field",
                           "set_json_key", "del_json_key")
    if _needs_step:
        try:
            step = int(op.get("step"))
        except Exception:                                # noqa: BLE001
            return False, "step 不是整数"
        if step < 1 or step > 200:
            return False, "step 越界：%s" % step
    field = str(op.get("field") or "").strip()
    if kind in ("delete_step",):
        return True, ""
    if kind == "add_step":
        val = op.get("value")
        if not isinstance(val, dict) or not str(val.get("path") or "").strip():
            return False, "add_step 的 value 必须是带 path 的步骤对象"
        return True, ""
    if kind == "set_cap":
        # 能力级字段（描述/是否启用…），**不含代码路径**
        if field not in ("desc", "why", "note"):
            return False, "set_cap 字段白名单：desc/why/note"
        return True, ""
    if kind == "replace_probe":
        val = op.get("value")
        if not isinstance(val, dict) or not isinstance(val.get("steps"), list) \
                or not val.get("steps"):
            return False, "replace_probe 的 value 必须是 {kind, steps:[...]}"
        for st in val["steps"][:200]:
            if not isinstance(st, dict) or not str(st.get("path") or "").strip():
                return False, "replace_probe 里每一步都要有 path"
        return True, ""
    if kind not in ("set_field", "del_json_key", "set_json_key"):
        return False, "未知 op：%s" % kind
    if field and field not in _PATCH_FIELDS:
        return False, "字段不在白名单：%s" % field
    if kind == "set_field" and not field:
        return False, "set_field 缺 field"
    if kind in ("set_json_key", "del_json_key") and not field:
        return False, "%s 缺 field（expect_json/json）" % kind
    if kind == "set_json_key" and not str(op.get("key") or "").strip():
        return False, "set_json_key 缺 key"
    return True, ""


def _op_identity(op: dict) -> tuple:
    return (str(op.get("op")), str(op.get("cap")), int(op.get("step") or 0),
            str(op.get("field") or ""), str(op.get("key") or ""))


def _bp_index(ws) -> dict:
    """能力 → 步骤数（用于合并阶段就挡住越界处方）。"""
    try:
        raw = json.loads(_bp_path(ws).read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}
    return {str(c.get("id") or ""): len(((c.get("probe") or {}).get("steps")) or [])
            for c in (raw.get("caps") or [])}


def merge_patches(patches: list, ws=None) -> tuple[list, list[str]]:
    """合并多名审核者的处方：**一致的操作才执行**，冲突一律跳过并如实记录。

    为什么取交集：两把独立 key 的模型看到同一份证据，只有都主张的改动才是
    "多远端一致"。单方主张的改动留痕不落盘（宁可少改，不许改坏）。
    """
    idx = _bp_index(ws) if ws is not None else {}
    seen: dict[tuple, list] = {}
    notes: list[str] = []
    for patch in patches or []:
        if not isinstance(patch, list):
            continue
        for op in patch:
            ok, why = valid_patch_op(op)
            if not ok:
                notes.append("跳过非法操作（%s）：%s" % (why, str(op)[:120]))
                continue
            if idx:
                cid = str(op.get("cap") or "")
                if cid not in idx:
                    notes.append("跳过：能力不存在 %s" % cid)
                    continue
                _limit = idx[cid] + (1 if str(op.get("op")) == "add_step" else 0)
                if int(op.get("step") or 0) > _limit:
                    notes.append("跳过：%s 只有 %d 步，处方指向第 %s 步"
                                 % (cid, idx[cid], op.get("step")))
                    continue
            seen.setdefault(_op_identity(op), []).append(op)
    out = []
    for ident, ops in seen.items():
        bodies = {json.dumps(o, ensure_ascii=False, sort_keys=True) for o in ops}
        if len(bodies) > 1:
            notes.append("冲突跳过（%s）：多份处方不一致" % (ident,))
            continue
        out.append(ops[0])
    return out[:_PATCH_OPS_MAX], notes


def apply_patch(ws: Path, ops: list) -> dict:
    """机械执行审核者处方。只碰蓝图 probe.steps，绝不碰产物代码。"""
    ws = Path(ws)
    p = _bp_path(ws)
    if not p.is_file():
        return {"changed": 0, "notes": [], "skipped": ["no-blueprint"]}
    raw = json.loads(p.read_text(encoding="utf-8"))
    caps = {str(c.get("id") or ""): c for c in (raw.get("caps") or [])}
    changed, notes, skipped = 0, [], []
    # 先按 cap 分组，step 大的先删（避免删完后续 step 号错位）
    for op in sorted(ops or [], key=lambda o: (str(o.get("cap")), -int(o.get("step") or 1))):
        ok, why = valid_patch_op(op)
        if not ok:
            skipped.append("%s（%s）" % (str(op)[:80], why))
            continue
        cid, step = str(op["cap"]), int(op.get("step") or 1)
        c = caps.get(cid)
        if c is None:
            skipped.append("%s：没有这个能力" % cid)
            continue
        steps = (c.get("probe") or {}).get("steps") or []
        if step > len(steps) + (1 if str(op.get("op")) == "add_step" else 0):
            skipped.append("%s 第%d步不存在（共%d步）" % (cid, step, len(steps)))
            continue
        st = steps[step - 1]
        kind = str(op.get("op"))
        try:
            if kind == "delete_step":
                steps.pop(step - 1)
                changed += 1
                notes.append("%s: 删除第%d步（远端处方）" % (cid, step))
            elif kind == "add_step":
                steps.insert(min(step - 1, len(steps)), op.get("value") or {})
                changed += 1
                notes.append("%s: 第%d步插入新步骤（远端处方）" % (cid, step))
            elif kind == "set_cap":
                c[str(op["field"])] = op.get("value")
                changed += 1
                notes.append("%s: %s ← %s（远端处方）"
                             % (cid, op["field"], str(op.get("value"))[:80]))
            elif kind == "replace_probe":
                c["probe"] = op.get("value") or {}
                changed += 1
                notes.append("%s: 整段重写探针（%d 步，远端处方）"
                             % (cid, len(((op.get("value") or {}).get("steps") or []))))
            elif kind == "set_field":
                st[str(op["field"])] = op.get("value")
                changed += 1
                notes.append("%s: 第%d步 %s ← %s（远端处方）"
                             % (cid, step, op["field"], str(op.get("value"))[:80]))
            elif kind == "del_json_key":
                box = st.get(str(op["field"]))
                if isinstance(box, dict) and str(op.get("key")) in box:
                    box.pop(str(op["key"]), None)
                    changed += 1
                    notes.append("%s: 第%d步 删 %s.%s（远端处方）"
                                 % (cid, step, op["field"], op["key"]))
            elif kind == "set_json_key":
                box = st.setdefault(str(op["field"]), {})
                if not isinstance(box, dict):
                    skipped.append("%s 第%d步 %s 不是对象" % (cid, step, op["field"]))
                    continue
                if len(box) > _PATCH_JSON_KEYS_MAX:
                    skipped.append("%s 第%d步 %s 过大" % (cid, step, op["field"]))
                    continue
                box[str(op["key"])] = op.get("value")
                changed += 1
                notes.append("%s: 第%d步 %s.%s ← %s（远端处方）"
                             % (cid, step, op["field"], op["key"], str(op.get("value"))[:80]))
        except Exception as exc:                         # noqa: BLE001
            skipped.append("%s 第%d步 执行失败：%s" % (cid, step, exc))
    if not changed:
        return {"changed": 0, "notes": notes, "skipped": skipped}
    bak = p.with_suffix(".json.bak-patch-%s" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(p, bak)
    except OSError:
        bak = None
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"changed": changed, "notes": notes, "skipped": skipped,
            "backup": str(bak) if bak else ""}


def apply(ws: Path, use_rules: bool | None = None) -> dict:
    """本地**确定性规则**兜底（2026-11-19 用户口径后**默认关闭**）。

    用户定稿：引擎本地不要针对项目堆规则 —— 那是过拟合，反而伤害引擎对
    "没见过的项目"的通用性；开放性问题一律交给三方协商（远端开处方）。
    因此默认只跑远端处方（`apply_patch`），本地规则仅在显式开关下启用：
        · 调用时 use_rules=True，或
        · 环境变量 SIXCODER_JUDGEFIX_RULES=1
    """
    if use_rules is None:
        import os as _os
        use_rules = str(_os.environ.get("SIXCODER_JUDGEFIX_RULES", "0")).strip() in ("1", "true", "yes")
    if not use_rules:
        return {"changed": 0, "notes": [], "disabled": True,
                "note": "本地规则默认关闭（交三方协商开处方）"}
    ws = Path(ws)
    p = _bp_path(ws)
    if not p.is_file():
        return {"changed": 0, "notes": ["没有蓝图文件，跳过"], "error": "no-blueprint"}
    from . import probe as _probe
    raw = json.loads(p.read_text(encoding="utf-8"))
    caps = raw.get("caps") or []
    changed, notes = 0, []

    for c in caps:
        steps = _steps(c)
        if not steps:
            continue
        cid = str(c.get("id") or "")
        fixed, n1 = _probe.distinct_same_role_users(steps, cid)
        fixed2, n2 = _probe.dedupe_role_users(fixed)
        fixed2, _notes3 = dedupe_identical_steps(fixed2, cid)
        _n4, _notes4 = fix_dangling_body_has(fixed2, cid)
        _n5, _notes5 = align_teacher_identity(fixed2, cid)
        if fixed2 != steps:
            c.setdefault("probe", {})["steps"] = fixed2
            changed += 1
            notes.extend("%s: %s" % (c.get("id"), n) for n in (n1 + n2))
            notes.extend(_notes3 + _notes4 + _notes5)

    n3, notes3 = unify_status_vocab(caps)
    changed += n3
    notes.extend(notes3)

    if not changed:
        return {"changed": 0, "notes": []}

    bak = p.with_suffix(".json.bak-judgefix-%s" % time.strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(p, bak)
    except OSError:
        bak = None
    p.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"changed": changed, "notes": notes,
            "backup": str(bak) if bak else ""}
