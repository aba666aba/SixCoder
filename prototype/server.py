"""SixCoder 服务（FastAPI + SSE）。

启动：
    python server.py            # http://127.0.0.1:8760
首次运行会自动创建演示仓库 demo-repo/（git init + 2 个提交）。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core import agent, approval, db, gitops, model_policy, ollama, permissions, probe, recon, review as review_mod, tools  # noqa: E402
from core import company, policy  # noqa: E402
from core import budget_estimate as _be  # noqa: E402
from core.demo import DEMO, ensure_demo  # noqa: E402

WEB_DIST = ROOT / "web" / "dist"

app = FastAPI(title="SixCoder (prototype)")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ---------------------------------------------------------------- 请求模型

class ProbeReq(BaseModel):
    repo: str
    deep: bool = False        # True 时强制跑全量测试建立基线


class TaskReq(BaseModel):
    repo: str
    requirement: str
    target: str = ""
    model: str = ""


class GitCommitReq(BaseModel):
    message: str = ""


class GitPathReq(BaseModel):
    path: str


class GitBranchReq(BaseModel):
    name: str


class SandboxUndoReq(BaseModel):
    steps: int = 1


class GitDoReq(BaseModel):
    """人类通道的 Git 操作（UI 按键 / CLI）。repo 缺省时使用任务工作区。"""
    op: str
    repo: str = ""
    task_id: str = ""
    args: Any = None       # 容错：客户端可能传 [] / null


class ModeReq(BaseModel):
    mode: str


class ReviewReq(BaseModel):
    repo: str = ""
    task_id: str = ""
    expectation: str = ""
    use_model: bool = True


GIT_OP_CAP = {
    "init": "git.commit", "stage": "git.commit", "commit": "git.commit",
    "undo": "git.commit", "undo_sandbox": "git.commit", "undo_sandbox_file": "git.commit",
    "new_branch": "git.branch", "switch": "git.branch", "merge": "git.branch",
    "set_remote": "git.remote", "fetch": "git.remote", "pull": "git.remote",
    "push": "git.remote", "sync": "git.remote",
}


def _guard(cap: str) -> None:
    """权限卡口：不足时抛 403，并把「该开哪个模式」写进消息。"""
    try:
        permissions.require(cap)
    except PermissionError as exc:
        raise HTTPException(403, str(exc))


# ---------------------------------------------------------------- 权限模式

@app.get("/v1/modes")
def get_modes():
    return permissions.describe()


@app.post("/v1/mode")
def set_mode(req: ModeReq):
    try:
        mode = permissions.set_mode(req.mode)
    except KeyError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "mode": mode.id, "name": mode.name,
            "capabilities": sorted(mode.caps), "sandbox": mode.sandbox,
            "auto_approve": mode.auto_approve,
            "key_nodes": list(mode.key_nodes), "boundary": mode.boundary}


# ---------------------------------------------------------------- 人工审核（开发者模式）
class ApprovalReq(BaseModel):
    decision: str = "approved"
    by: str = "ui"


@app.get("/v1/approvals")
def list_approvals():
    """待审单据：开发者模式下前端轮询这个，弹出"通过/拒绝"。"""
    mode = permissions.current_mode()
    return {"mode": mode.id, "name": mode.name, "auto_approve": mode.auto_approve,
            "pending": approval.pending()}


@app.post("/v1/approvals/{ticket_id}")
def decide_approval(ticket_id: str, req: ApprovalReq):
    r = approval.resolve(ticket_id, req.decision, by=req.by)
    if not r.get("ok"):
        raise HTTPException(400, r.get("reason") or "审批失败")
    return r


def resolve_repo(repo: str = "", task_id: str = "") -> Path:
    if task_id:
        return ws_of(task_id)
    if repo:
        p = Path(repo)
        if not p.exists():
            raise HTTPException(400, f"仓库路径不存在：{repo}")
        return p
    raise HTTPException(
        400,
        "还没有 Git 目标：请求里既没有 repo 也没有 task_id。"
        "请在客户端先选择一个仓库目录（如 /v1/git/status?repo=<绝对路径>），"
        "或先跑一个任务再用 task_id（Git 会作用于该任务的工作区副本）。")


@app.get("/v1/repo/tree")
def repo_tree(repo: str, path: str = ""):
    """仓库目录树（VSCode 式文件管理器用）。

    只读：列出 repo 下某个子目录的直接子项，交给前端做懒加载展开。
    路径必须落在 repo 内（防目录穿越），二进制文件不返回内容。
    """
    root = Path(repo)
    if not root.exists() or not root.is_dir():
        raise HTTPException(400, f"仓库路径不存在或不是目录：{repo}")
    root_r = root.resolve()
    target = (root_r / path).resolve() if path else root_r
    try:
        target.relative_to(root_r)
    except ValueError:
        raise HTTPException(400, "路径越界：只能浏览仓库内的文件")
    if not target.exists() or not target.is_dir():
        raise HTTPException(404, f"目录不存在：{path or '.'}")
    items = []
    try:
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            if p.name in _TREE_SKIP_DIRS:
                continue
            rel = p.relative_to(root_r).as_posix()
            entry = {"name": p.name, "path": rel, "is_dir": p.is_dir()}
            if p.is_file():
                try:
                    entry["size"] = p.stat().st_size
                except OSError:
                    entry["size"] = 0
            items.append(entry)
    except PermissionError:
        raise HTTPException(403, f"无权读取目录：{path or '.'}")
    return {"repo": str(root_r), "path": path, "items": items}


_TREE_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".venv",
                   ".sixcoder", ".mypy_cache", ".ruff_cache", "dist", "build", ".idea"}


@app.get("/v1/git/status")
def git_read_status(repo: str = "", task_id: str = ""):
    _guard("git.read")
    return gitops.status(resolve_repo(repo, task_id))


@app.get("/v1/git/log")
def git_read_log(repo: str = "", task_id: str = "", n: int = 15):
    _guard("git.read")
    return gitops.log(resolve_repo(repo, task_id), n)


@app.get("/v1/git/diff")
def git_read_diff(repo: str = "", task_id: str = "", rev: str = "", path: str = "", staged: bool = False):
    _guard("git.read")
    return gitops.diff(resolve_repo(repo, task_id), rev, path, staged)


@app.get("/v1/git/branches")
def git_read_branches(repo: str = "", task_id: str = ""):
    _guard("git.read")
    return gitops.branches(resolve_repo(repo, task_id))


@app.post("/v1/git/do")
def git_do(req: GitDoReq):
    """人类通道：完整 Git 客户端能力（受当前权限模式约束；模型通道另有一套限制）。"""
    if req.op == "init" and req.repo and not Path(req.repo).exists():
        Path(req.repo).mkdir(parents=True, exist_ok=True)   # 允许"新建目录并初始化"
    ws = resolve_repo(req.repo, req.task_id)
    cap = GIT_OP_CAP.get(req.op)
    if not cap:
        raise HTTPException(400, f"未知操作：{req.op}")
    _guard(cap)
    a = req.args if isinstance(req.args, dict) else {}
    op = req.op
    if op == "init":
        return gitops.init(ws, a.get("branch") or "main")
    if op == "stage":
        return gitops.stage_all(ws)
    if op == "commit":
        return gitops.snapshot(ws, a.get("message") or "commit from UI",
                               a.get("author") or "SixCoder")
    if op == "new_branch":
        return gitops.new_branch(ws, a.get("name", ""), a.get("checkout", True))
    if op == "switch":
        return gitops.switch(ws, a.get("name", ""))
    if op == "merge":
        return gitops.merge(ws, a.get("source", ""), a.get("message", ""))
    if op == "set_remote":
        return gitops.set_remote(ws, a.get("name") or "origin", a.get("url", ""))
    if op == "fetch":
        return gitops.fetch(ws, a.get("remote") or "origin", a.get("prune", True))
    if op == "pull":
        return gitops.pull(ws, a.get("remote") or "origin", a.get("branch", ""), a.get("ff_only", False))
    if op == "push":
        return gitops.push(ws, a.get("remote") or "origin", a.get("branch", ""),
                           bool(a.get("force", False)), a.get("set_upstream", True))
    if op == "sync":
        return gitops.sync(ws, a.get("remote") or "origin")
    if op == "undo":
        return gitops.undo_file(ws, a.get("path", ""))
    if op == "undo_sandbox_file":
        return gitops.sandbox_undo_file(ws, a.get("path", ""))
    if op == "undo_sandbox":
        return gitops.sandbox_undo(ws, int(a.get("steps", 1)))
    raise HTTPException(400, f"未知操作：{op}")


def ws_of(task_id: str) -> Path:
    rows = db.query("SELECT workspace FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "任务不存在")
    ws = Path(rows[0]["workspace"] or "")
    if not ws.exists():
        raise HTTPException(409, "任务工作区不存在（任务可能尚未进入执行阶段）")
    return ws


# ---------------------------------------------------------------- 基础接口

@app.get("/v1/health")
def health():
    models = ollama.models()
    return {
        "ok": True,
        "ollama": ollama.alive(),
        "models": models,
        "default_model": ollama.pick_default(),
        "model_policy": _model_policy_view(models),
        "db": str(db.DB_PATH),
        "workspaces": str(agent.WORKSPACES),
        "python": sys.version.split()[0],
    }


def _model_policy_view(models: list[str]) -> dict:
    """门槛信息给界面用：低于门槛的模型照样列出，但界面要标"不可选"并说明原因。"""
    blocked = [m for m in models if not model_policy.verdict(m)["ok"]]
    return {"min_params_b": model_policy.MIN_PARAMS_B,
            "blocked": blocked,
            "suggest": model_policy.best_available(),
            "message": (f"本地模型必须 ≥{model_policy.MIN_PARAMS_B:g}B；"
                        f"低于门槛的（{', '.join(blocked)}）不能选用——实测它们连结构化输出都不稳。"
                        if blocked else
                        f"本地模型门槛 ≥{model_policy.MIN_PARAMS_B:g}B，当前模型均达标")}


@app.get("/v1/models")
def models():
    lst = ollama.models()
    return {"models": lst, "default": ollama.pick_default(),
            "policy": _model_policy_view(lst)}


@app.get("/v1/toolchains")
def toolchains():
    return {"items": recon.toolchains(_load_tc_overrides()), "overrides": _load_tc_overrides()}


# ---------------------------------------------------------------- 远端模型设置
@app.get("/v1/settings/remote")
def get_remote_settings(reveal: bool = False):
    """远端模型设置。默认只回掩码；reveal=true 时回明文（key 本就是明文存本地设置文件）。

    只有本机回环地址才允许 reveal：避免把明文暴露给局域网。
    """
    from core import remote as remote_mod, settings_store
    ok, why = remote_mod.available()
    _rem = settings_store.public_remote(reveal=reveal)
    # **key 类层必须在这条链上也带上**（2026-11-18 实测坑）：界面「厂商卡片 → key 列表」
    # 读的是这个 GET 的 remote.providers，而 public_remote 不带 keys —— 导致
    # 保存了多少把 key 界面上都显示 0 把。用 _public_model_view 的厂商视图覆盖合并，
    # 保证两条读取路径（这个 GET 与 /remote/models）口径完全一致。
    try:
        _rem["providers"] = _public_model_view(settings_store)["providers"]
    except Exception:                                    # noqa: BLE001
        pass
    return {"remote": _rem, "available": ok, "reason": why,
            "usage": remote_mod.usage_summary()}


@app.get("/v1/regressions")
def list_regressions(limit: int = 50):
    """回归用例库：远端/人工成功过的判据，同需求每次运行都要过。"""
    from core import regression
    return {"items": regression.listing(limit)}


@app.delete("/v1/regressions/{rid}")
def delete_regression(rid: int):
    from core import regression
    n = regression.delete(rid)
    return {"ok": bool(n), "deleted": n, "items": regression.listing()}


@app.delete("/v1/regressions")
def clear_regressions():
    from core import regression
    n = regression.clear()
    return {"ok": True, "cleared": n, "items": regression.listing()}


class RemoteSettingsReq(BaseModel):
    api_key: Optional[str] = None          # 只有显式传非空才更新；清空请用 clear_key=true
    clear_key: Optional[bool] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    compat: Optional[str] = None         # deepseek（默认）| openai 兼容格式
    enabled: Optional[bool] = None
    max_output_tokens: Optional[int] = None
    max_prompt_chars: Optional[int] = None
    escalate_after: Optional[int] = None    # 同一档连续失败几次才升下一档
    dev_remote: Optional[bool] = None       # 开发者模式是否允许调用远端（甲方强制开、只读强制关）
    local_first: Optional[bool] = None       # 阶梯第 0 档是否用本地模型（省钱）
    active_model: Optional[str] = None      # 当前使用的那**一个**模型档案 id（空 = 自动解析）
    pricing: Optional[dict] = None          # 旧价目表（美元）；新逻辑用 models[].pricing
    offpeak_window: Optional[dict] = None   # 旧字段，保留兼容
    peak_window: Optional[dict] = None      # 闲时判定真正读的字段：ranges / weekdays_only / tz
    models: Optional[list] = None           # 模型档案整表（增/删/改都走这里）


class ModelProfileReq(BaseModel):
    """单个模型档案的增改：只传要改的字段即可（按 id 合并）。"""
    id: str
    model: Optional[str] = None
    label: Optional[str] = None
    base_url: Optional[str] = None
    compat: Optional[str] = None         # 档案级协议，优先于全局
    api_key: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[float] = None
    efforts: Optional[list] = None
    max_output_tokens: Optional[int] = None
    temperature: Optional[float] = None
    pricing: Optional[dict] = None


def _public_model_view(settings_store) -> dict:
    """模型档案对外视图的**唯一出口**：api_key 一律剥掉，阶梯只回展示字段。

    四个端点（GET / 增改 / 删除 / 设优先）都必须走这里。运行期 ladder() 内部
    带 api_key 是必要的（发请求要用），但那不等于可以把它回给界面。
    """
    return {"models": [settings_store.sanitize_profile(p) for p in settings_store.models()],
            "ladder": [{"profile_id": s.get("profile_id"), "model": s.get("model"),
                        "effort": s.get("effort")} for s in settings_store.ladder()],
            # 厂商层：界面渲染"下拉 + 三个长方矩形 + 每家的模型列表"用这份。
            # api_key 只给掩码 + 是否已配置（绝不回明文）。
            "providers": [{
                "id": p["id"], "label": p["label"], "base_url": p["base_url"],
                "efforts": list(p.get("efforts") or []),
                "current": bool(p.get("current")),
                "key_configured": bool(p.get("api_key")),
                "key_masked": settings_store.mask(p.get("api_key") or ""),
                "models": [settings_store.sanitize_profile(m)
                           for m in (p.get("models") or [])],
                # key 类层（2026-11-18 多远端协作）：写作者用「当前」类，
                # 审核者优先用**其它 key 类**的模型（独立限流池）。
                # models 随类返回（UI 改版：每个 key 独立配自己的模型）。
                "keys": [{
                    "id": k["id"], "label": k.get("label") or k["id"],
                    "role": str(k.get("role") or ""),
                    "current": bool(k.get("current")),
                    "key_configured": bool(str(k.get("api_key") or "").strip()),
                    "key_masked": settings_store.mask(k.get("api_key") or ""),
                    "model_count": len(k.get("models") or []),
                    "models": [settings_store.sanitize_profile(m)
                               for m in (k.get("models") or [])],
                } for k in (p.get("keys") or [])],
            } for p in settings_store.providers()]}


@app.get("/v1/settings/remote/models")
def get_model_profiles():
    """模型档案 + 由它摊出来的执行阶梯。

    阶梯是**当前这一个模型**的思考强度序列（none→low→high→max），不是多模型并排：
    一次运行只用一个模型，强度由 core/effort.py 按需求/难度自动切。
    """
    from core import settings_store
    return {**_public_model_view(settings_store),
            "escalate_after": int(settings_store.get_remote().get("escalate_after") or 2),
            "efforts": list(settings_store.EFFORTS)}


@app.post("/v1/settings/remote/models")
def upsert_model_profile(req: ModelProfileReq):
    """新增或修改一个模型档案（按 id 合并）。

    为什么按 id 合并而不是整表覆盖：界面上改一个字段就提交一次，
    整表覆盖会在并发编辑时把别人的改动冲掉。
    """
    from core import settings_store
    profs = settings_store.models()
    raw = {k: v for k, v in req.model_dump().items() if v is not None}
    hit = next((p for p in profs if p["id"] == req.id), None)
    if hit is None:
        profs.append(settings_store._norm_model_profile(raw))
    else:
        hit.update({k: v for k, v in raw.items() if k != "pricing"})
        if raw.get("pricing"):
            hit["pricing"] = raw["pricing"]
    settings_store.save_models(profs)
    return {"ok": True, **_public_model_view(settings_store)}


@app.delete("/v1/settings/remote/models/{model_id}")
def delete_model_profile(model_id: str):
    """删除一个模型档案（删完至少还要剩一个，否则夜班没模型可用）。"""
    from core import settings_store
    profs = [p for p in settings_store.models() if p["id"] != model_id]
    if not profs:
        raise HTTPException(400, "至少要留一个模型档案，否则夜班没有可用模型")
    settings_store.save_models(profs)
    return {"ok": True, **_public_model_view(settings_store)}


@app.post("/v1/settings/remote/models/primary/{model_id}")
def set_primary_model(model_id: str):
    """把某个档案设为**优先使用**：优先级数值改成最小，它自然排到阶梯最前面。"""
    from core import settings_store
    profs = settings_store.models()
    if not any(p["id"] == model_id for p in profs):
        raise HTTPException(404, f"没有这个模型档案：{model_id}")
    top = min((p["priority"] for p in profs), default=10.0) - 1
    for p in profs:
        if p["id"] == model_id:
            p["priority"] = top
    settings_store.save_models(profs)
    return {"ok": True, **_public_model_view(settings_store)}


@app.post("/v1/settings/remote/providers")
def upsert_provider(req: dict):
    """保存一个**厂商**的配置（2026-11-09 用户口径：厂商是一等公民）。

    界面上的动作全部收敛到这里：
      · 切换「正在使用」→ 改 current；
      · 填 apikey → 改 api_key（留空 = 不修改已有）；
      · 勾选思考强度等级 → 改 efforts；
      · 增删该厂商的模型 → 改 models。

    **按 id 合并**（不整表覆盖）：界面上改一个字段就提交一次，整表覆盖会在
    并发编辑时把别人的改动冲掉。
    """
    from core import settings_store
    pid = str((req or {}).get("id") or "").strip().lower()
    if pid not in settings_store.PROVIDER_IDS:
        raise HTTPException(400, f"未知厂商：{pid}（可选 "
                                 f"{'/'.join(settings_store.PROVIDER_IDS)}）")
    provs = settings_store.providers()
    hit = next((p for p in provs if p["id"] == pid), None)
    if hit is None:
        hit = settings_store._norm_provider(dict(settings_store.PROVIDER_PRESETS[pid]), pid)
        provs.append(hit)
    raw = dict(req or {})
    for k in ("label", "base_url", "efforts", "current"):
        if k in raw and raw[k] is not None:
            hit[k] = raw[k]
    if str(raw.get("api_key") or "").strip():      # 留空 = 不修改已有 key
        hit["api_key"] = str(raw["api_key"]).strip()
    # **key 类整体合并**（2026-11-18 UI 改版：每个 key 独立配自己的模型）。
    # 带了 keys 就按 id 合并：label/api_key（留空不清）/current/models 各自落位；
    # 合并完派生厂商级 models = 各 key 类模型的展平（兼容旧读取路径）。
    # raw["models"]（旧平铺口径）在带 keys 时被忽略——两套口径不能同时生效。
    if isinstance(raw.get("keys"), list):
        keys = hit.setdefault("keys", [])
        # **显式指派优先**（2026-11-18 晚）：请求里带了 role 字段，roles 就以请求为准——
        # 没被提及的 key 角色清零。否则预设 k1 的 current=True 会派生 writer，
        # 把用户刚指派给新 key 的 writer 顶掉（实测踩过）。
        _role_auth = any("role" in x for x in raw["keys"] if isinstance(x, dict))
        _seen_ids: set[str] = set()          # 本请求**实际触碰**的 key（含自动编号的新 key）
        for rk in raw["keys"]:
            if not isinstance(rk, dict):
                continue
            kid = str(rk.get("id") or "").strip()
            kt = next((k for k in keys if k["id"] == kid), None)
            if kt is None:
                kid = kid or _next_free_kid(keys)      # 复用被删掉腾出的编号
                kt = {"id": kid, "label": "", "api_key": "", "role": "",
                      "current": False,
                      # 新 key **自动继承厂商默认模型**（用户口径：新增时不配模型）
                      "models": settings_store.default_models_for(pid)}
                keys.append(kt)
            if str(rk.get("label") or "").strip():
                kt["label"] = str(rk["label"]).strip()[:48]
            if str(rk.get("api_key") or "").strip():
                kt["api_key"] = str(rk["api_key"]).strip()
            if "role" in rk:      # 角色指派（writer/reviewer1/reviewer2/空）
                _r = str(rk.get("role") or "").strip().lower()
                kt["role"] = _r if _r in settings_store.KEY_ROLES else ""
            if isinstance(rk.get("models"), list):
                kt["models"] = settings_store._norm_profiles(rk["models"])
                for m in kt["models"]:
                    m["provider"] = pid
            _seen_ids.add(kt["id"])          # 显式 id 与自动编号都要记
        if bool(raw.get("replace_keys")):
            # **整体保存 = 以请求为准**（2026-11-18 实测坑：界面删了一把 key，
            # 合并语义只增不删 → 保存后又被"复活"，用户看到删除不同步）。
            hit["keys"] = [k for k in keys if k["id"] in _seen_ids]
            keys = hit["keys"]
        if _role_auth:
            for k in keys:
                if k["id"] not in _seen_ids:
                    k["role"] = ""
        # 角色的唯一性/current 同步交给 _norm_provider（save_providers 内）统一裁决；
        # 这里只做显式指派：current_key → 那把是 writer（旧口径兼容）。
        if str(raw.get("current_key") or "").strip() and keys:
            ck = str(raw.get("current_key") or "")
            for k in keys:
                if k["id"] == ck:
                    k["role"] = "writer"
                elif k.get("role") == "writer":
                    k["role"] = ""
        hit["models"] = [m for k in keys for m in (k.get("models") or [])]
    elif isinstance(raw.get("models"), list):
        hit["models"] = raw["models"]
    # 「正在使用」有且只有一个：设了 current 就把别家的取消
    if bool(raw.get("current")):
        for p in provs:
            p["current"] = (p["id"] == pid)
    settings_store.save_providers(provs)
    return {"ok": True, **_public_model_view(settings_store)}


def _next_free_kid(keys: list) -> str:
    """给新 key 类分配一个**空出来的最小编号**（2026-11-18 晚：id 对用户隐藏，
    只在内部用；删掉的 key 的空位要能被自动复用，不许编号一路涨）。"""
    used = {str((k or {}).get("id") or "") for k in keys if isinstance(k, dict)}
    i = 1
    while f"k{i}" in used:
        i += 1
    return f"k{i}"


@app.post("/v1/settings/remote/providers/{pid}/keys")
def upsert_provider_key(pid: str, req: dict):
    """新增/修改一个 **key 类**（2026-11-18 多远端协作）。

    语义（与厂商级 key 一致）：
      · 带 id → 修改那个类；不带 id → 新建（自动编号 k2/k3…）；
      · api_key 留空 = 不修改已有（绝不因空输入悄悄清 key）；
      · current=true → 切「正在使用」（写作者用的那把），同类只允许一个。
    """
    from core import settings_store
    # 注意：providers() 每次从盘上重读规范化，改完必须把**同一个列表**传给
    # save_providers —— 实测（2026-11-18）在保存行里再调一次 providers() 会把
    # 刚才的修改整个丢掉（测试 k2 建不出来）。
    provs = settings_store.providers()
    hit = next((p for p in provs if p["id"] == pid), None)
    if hit is None:
        raise HTTPException(404, f"没有这个厂商：{pid}")
    raw = dict(req or {})
    kid = str(raw.get("id") or "").strip()
    klabel = str(raw.get("label") or "").strip()[:48]
    kkey = str(raw.get("api_key") or "").strip()
    keys = hit.setdefault("keys", [])
    tgt = next((k for k in keys if k["id"] == kid), None) if kid else None
    if tgt is None:
        kid = kid or _next_free_kid(keys)              # 复用被删掉腾出的编号
        tgt = {"id": kid, "label": klabel or kid, "api_key": "", "role": "",
               "current": False,
               "models": settings_store.default_models_for(pid)}
        keys.append(tgt)
    if klabel:
        tgt["label"] = klabel
    if kkey:                                        # 留空 = 不修改已有
        tgt["api_key"] = kkey
    if "role" in raw:                               # 角色指派（空 = 不参与）
        _r = str(raw.get("role") or "").strip().lower()
        tgt["role"] = _r if _r in settings_store.KEY_ROLES else ""
    if bool(raw.get("current")):                    # 旧口径：current = writer
        for k in keys:
            k["role"] = "writer" if k["id"] == kid else (""
                                                        if k.get("role") == "writer" else k.get("role"))
    settings_store.save_providers(provs)
    return {"ok": True, **_public_model_view(settings_store)}


@app.delete("/v1/settings/remote/providers/{pid}/keys/{kid}")
def delete_provider_key(pid: str, kid: str):
    """删一个 key 类。类里的模型**挪到「正在使用」的类**（不丢档案）；
    只剩一个类时不许删（写作者总得有一把 key）。"""
    from core import settings_store
    provs = settings_store.providers()               # 同一列表改完再存（见上）
    hit = next((p for p in provs if p["id"] == pid), None)
    if hit is None:
        raise HTTPException(404, f"没有这个厂商：{pid}")
    keys = hit.get("keys") or []
    tgt = next((k for k in keys if k["id"] == kid), None)
    if tgt is None:
        raise HTTPException(404, f"没有这个 key 类：{kid}")
    # 允许删到 0 把（2026-11-18 用户口径：一把也支持删，删除必须真同步到配置）。
    # 删光了 = 这个厂商暂时没有可用 key，配置层如实为空，界面显示"新增第一把 key"。
    keep = next((k for k in keys if k.get("current") and k["id"] != kid), None) \
        or next((k for k in keys if k["id"] != kid), None)
    # 删的是**最后一把**时没有"接盘"的 key（用户口径：一把也支持删）——
    # 它名下的模型随它一起消失，不许在这里崩（实测 AttributeError）。
    if keep is not None:
        keep["models"] = (keep.get("models") or []) + (tgt.get("models") or [])
    was_current = bool(tgt.get("current"))
    hit["keys"] = [k for k in keys if k["id"] != kid]
    if was_current and keep is not None:
        for k in hit["keys"]:
            k["current"] = (k["id"] == keep["id"])
    settings_store.save_providers(provs)
    return {"ok": True, **_public_model_view(settings_store)}


@app.delete("/v1/settings/remote/providers/{pid}/models/{model_id}")
def delete_provider_model(pid: str, model_id: str):
    """从某个厂商下删掉一个模型档案（不影响别的厂商）。"""
    from core import settings_store
    provs = settings_store.providers()
    hit = next((p for p in provs if p["id"] == pid), None)
    if hit is None:
        raise HTTPException(404, f"没有这个厂商：{pid}")
    before = len(hit.get("models") or [])
    hit["models"] = [m for m in (hit.get("models") or []) if m.get("id") != model_id]
    if len(hit["models"]) == before:
        raise HTTPException(404, f"这个厂商下没有模型：{model_id}")
    settings_store.save_providers(provs)
    return {"ok": True, **_public_model_view(settings_store)}


@app.get("/v1/settings/remote/usage")
def remote_usage(task_id: str = ""):
    """用量与花费：按模型、按思考档位分开算（价格来自模型档案，人民币）。"""
    from core import remote as remote_mod
    return remote_mod.usage_summary(task_id)


@app.get("/v1/settings/remote/test")
def test_remote_settings():
    """真打一次最小请求，验证 Key 到底能不能用（1 token 级别，成本可忽略）。

    为什么需要：`available()` 只能判断"有没有填 Key"，判不了"Key 还能不能用"。
    实测踩过——Key 轮换后本地还留着旧值，远端一路 401，界面上却显示"已配置"。
    """
    from core import remote as remote_mod
    ok, why = remote_mod.available()
    if not ok:
        return {"ok": False, "detail": why or "远端未配置"}
    r = remote_mod.complete("回复一个字：好", task_id="selftest", node_id="selftest")
    if r.get("ok"):
        return {"ok": True, "detail": f"连通（{r.get('usage', {}).get('total_tokens', '?')} tokens）",
                "answer": (r.get("answer") or "")[:80]}
    return {"ok": False, "detail": r.get("error") or "调用失败"}


@app.post("/v1/settings/remote")
def set_remote_settings(req: RemoteSettingsReq):
    """更新远端模型设置（改完下一次请求立即生效，不需要重启）。

    重要：空的 api_key **不会**清掉已有 key —— 只有显式 clear_key=true 才清除。
    （实测教训：输入框为空时按回车/点保存，会把之前填好的 key 悄悄删掉。）
    """
    from core import db as db_mod, remote as remote_mod, settings_store
    raw = req.model_dump()
    patch = {}
    for k, v in raw.items():
        if v is None or k == "clear_key":
            continue
        if k == "api_key":
            if str(v).strip():
                patch["api_key"] = str(v).strip()
            continue
        if k == "active_model":
            # 空串 = 回到"自动解析"（按平铺 model / 第一个启用的档案），所以这里要允许清空
            patch["active_model"] = str(v or "").strip()
            continue
        if isinstance(v, str) and not v.strip():
            continue
        if isinstance(v, dict) and not v:
            continue
        patch[k] = v
    if raw.get("clear_key"):
        patch["api_key"] = ""
    if "api_key" in patch:
        patch["configured_at"] = db_mod.now()
    changed = []
    if patch:
        _remote_cfg, changed = settings_store.update_remote(patch)
    ok, why = remote_mod.available()
    return {"ok": True, "changed": changed,
            "remote": settings_store.public_remote(), "available": ok, "reason": why,
            "usage": remote_mod.usage_summary()}


class ToolchainOverrideReq(BaseModel):
    id: str
    path: str = ""


TC_OVERRIDE_FILE = ROOT / "data" / "toolchains.json"


def _load_tc_overrides() -> dict:
    try:
        return json.loads(TC_OVERRIDE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.post("/v1/toolchains/override")
def set_toolchain_override(req: ToolchainOverrideReq):
    """设置里指定工具链目录（如 JDK）。path 为空表示清除覆盖。"""
    if not permissions.allows("toolchain.config"):
        raise HTTPException(403, "当前模式不允许修改工具链配置（需要「安全编辑」或更高）")
    data = _load_tc_overrides()
    if req.path.strip():
        p = Path(req.path)
        if not p.exists():
            raise HTTPException(400, f"路径不存在：{req.path}")
        data[req.id] = {"exe": str(p) if p.is_file() else "",
                        "home": "" if p.is_file() else str(p)}
    else:
        data.pop(req.id, None)
    TC_OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    TC_OVERRIDE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    items = recon.toolchains(data)
    hit = next((i for i in items if i["id"] == req.id), None)
    if req.path.strip() and (not hit or hit.get("source") != "设置"):
        # 目录存在但里面找不到可执行文件：明确报错，不要静默回落到环境变量
        raise HTTPException(
            400,
            f"在 {req.path} 下没找到 {req.id} 的可执行文件（期望 {req.id}\\bin\\{req.id}.exe，或该目录本身就是可执行文件）。"
            f"已保存的覆盖不会生效，请选择正确的安装目录。")
    return {"ok": True, "item": hit, "overrides": data}


@app.post("/v1/probe")
def probe_repo(req: ProbeReq):
    info = recon.classify(Path(req.repo), deep=req.deep)
    files = []
    p = Path(req.repo)
    if p.exists():
        files = tools.list_files(p, limit=60)
    return {**info, "files": files}


@app.post("/v1/demo")
def make_demo():
    p = ensure_demo()
    return {"repo": str(p), "state": recon.classify(p)["state"]}


# ---------------------------------------------------------------- 远端工具循环（§13.4 第 3/4 步）
class RemoteToolReq(BaseModel):
    """远端（或前端）调用工具循环的请求：在给定工作区跑一轮只读工具往返。"""
    ws: str                      # 工作区绝对路径
    instruction: str = ""        # 给远端的指令（会作为首条 user 消息）
    level: str = "S"             # 规模级别（决定受保护区 block 策略）
    max_rounds: int = 6          # 工具往返上限


@app.post("/v1/remote/tool")
def remote_tool(req: RemoteToolReq):
    ws = Path(req.ws)
    if not ws.exists():
        raise HTTPException(400, f"工作区不存在：{req.ws}")
    # 真实运行：complete_fn 缺省走 remote.complete（需已配置远端 API）。
    # 受保护区（测试/契约/治理）对工具循环只读，写入在 _tool_allow_write 里被拒。
    from core import remotetools
    loop = remotetools.ToolLoop(ws, level=req.level, max_rounds=req.max_rounds)
    result = loop.run(req.instruction)
    return result


# ---------------------------------------------------------------- 任务

@app.post("/v1/tasks")
def create_task(req: TaskReq):
    _guard("agent.run")
    repo = Path(req.repo)
    if not repo.exists():
        raise HTTPException(400, f"仓库不存在：{req.repo}")
    model = req.model or ollama.pick_default()
    # 门槛：本地模型必须 ≥7B。用户手选的小模型直接拒掉（而不是跑一夜产出垃圾）。
    v = model_policy.verdict(model)
    if not v["ok"]:
        raise HTTPException(400, v["message"])
    if not ollama.alive():
        raise HTTPException(503, "Ollama 不可达，请先 `ollama serve`")
    task_id = agent.new_task(repo, req.requirement, req.target, model)
    emit = agent.Emitter(task_id)
    agent.start_thread(task_id, repo, req.requirement, req.target, model, emit)
    return {"task_id": task_id, "model": model}


class NightReq(BaseModel):
    repo: str
    requirement: str = ""
    goal: str = ""                 # 全自主模式：一句话目标（切片/验收对象/预算分配由系统自己规划）
    fresh: bool = False            # 全自主模式：忽略已有蓝图重新推导（默认续跑）
    target: str = ""
    model: str = ""
    plan: str = ""                 # 开发者模式：人给的实现方案（权威）
    use_remote: bool = True
    # 目标代码规模：不同规模的约束完全不同（core/scale.py）。
    # 甲方模式要求提需求时写明；没写就按实测推断，并在交付报告里注明。
    scale: str = ""                # S / M / L / XL
    remote_profile: str = ""       # 右上角「远端模型」：只走这个档案（flash / pro），空 = 整条阶梯


_NIGHT: dict[str, dict] = {}


@app.post("/v1/night")
def create_night(req: NightReq):
    """夜班（公司制）：甲方模式提需求 → 系统内部做完所有决策 → 早上给交付报告。

    与 `/v1/tasks` 的区别：task 是"一个需求 → 一对测试/实现节点"；
    night 是"一个需求 → 反复体检/派工/开发/机械验收，直到收敛或预算耗尽"。
    进度直接用 `/v1/tasks/{task_id}/stream` 看（事件同一条通道）。
    """
    _guard("agent.run")
    pol = policy.current()
    if pol.code_escalation == policy.OFF:
        raise HTTPException(403, f"{pol.name}不写文件，不能跑夜班；请切到甲方模式或开发者模式")
    repo = Path(req.repo)
    if not repo.exists():
        raise HTTPException(400, f"仓库不存在：{req.repo}")
    model = req.model or ollama.pick_default()
    # 模型选择归用户：低于建议门槛只提示，不拒绝（见 core/model_policy.py）
    model_warn = model_policy.advise(model)
    if model_warn:
        print(f"[WARN] {model_warn}")
    if not ollama.alive():
        raise HTTPException(503, "Ollama 不可达，请先 `ollama serve`")
    task_id = agent.new_task(repo, req.requirement or req.goal, req.target, model)
    emit = agent.Emitter(task_id)
    _NIGHT[task_id] = {"state": "running", "delivery": "", "reason": "", "verified": 0,
                       "total": 0, "mode": pol.mode, "policy": policy.describe(pol),
                       "started": db.now()}

    def _run() -> None:
        info = _NIGHT[task_id]
        try:
            if (req.goal or "").strip():
                # 全自主模式：只给一句话目标 → 蓝图 → 切片 → 逐片生成 → 覆盖 → 报告
                from core import auto as _auto
                _snap = _be.budget_snapshot(repo)
                emit("night_start", {"mode": "auto", "goal": req.goal,
                                     "policy": policy.describe(pol),
                                     "model_warning": model_warn, "fresh": req.fresh,
                                     "system_budget_cny": _snap.get("system_budget_cny"),
                                     "stop_threshold_cny": _snap.get("stop_threshold_cny")})
                res = _auto.night_auto(repo, req.goal, model=model, emit=emit,
                                       fresh=req.fresh)
                ratios = res.get("ratios") or {}
                info.update({"state": "done" if res.get("ok") else "failed",
                             "delivery": res.get("report") or "",
                             "reason": res.get("reason") or "",
                             "verified": ratios.get("done", 0),
                             "total": ratios.get("total", 0)})
                emit("night_done", {"verified": ratios.get("done", 0),
                                    "total": ratios.get("total", 0),
                                    "pct": ratios.get("pct", 0),
                                    "reason": res.get("reason"),
                                    "report": res.get("report"),
                                    "spent_cny": res.get("spent_cny"),
                                    "slices": res.get("slices"),
                                    "replans": res.get("replans"),
                                    "descoped": res.get("descoped"),
                                    "blocked": res.get("blocked")})
                db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                           ("done" if res.get("ok") else "failed", db.now(), task_id))
                return
            # 规模分级：优先用前端/客户端声明的，其次从需求原文读，最后按实测推断
            from core import scale as _scale
            scope = _scale.resolve(req.requirement, repo)
            if (req.scale or "").strip().upper() in _scale.TIERS:
                scope = {**scope, "level": req.scale.strip().upper(), "source": "client"}
            _snap = _be.budget_snapshot(repo)
            emit("night_start", {"mode": pol.mode,
                                 "policy": policy.describe(pol),
                                 "plan": bool(req.plan.strip()),
                                 "model_warning": model_warn,
                                 "scale": scope["level"], "scale_source": scope["source"],
                                 "remote_profile": req.remote_profile,
                                 "system_budget_cny": _snap.get("system_budget_cny"),
                                 "stop_threshold_cny": _snap.get("stop_threshold_cny"),
                                 "scale_warning": scope.get("warning", "")})
            # 没写规模就把"该问的问题"显式抛给前端（甲方模式：需求里要有明确规模）
            if scope.get("warning"):
                emit("notice", {"message": scope["warning"], "kind": "scale"})
            res = company.night(repo, req.requirement, model,
                                use_remote=req.use_remote, emit=emit, plan=req.plan,
                                remote_profile=req.remote_profile)
            info.update({"state": "done" if res.ok else "failed", "delivery": res.delivery_path,
                         "reason": res.reason, "verified": res.verified, "total": res.total,
                         "scale": scope["level"], "scale_source": scope["source"]})
            emit("night_done", {"verified": res.verified, "total": res.total,
                                "reason": res.reason, "delivery": res.delivery_path,
                                "scale": scope["level"]})
            db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                       ("done" if res.ok else "failed", db.now(), task_id))
        except Exception as exc:                       # noqa: BLE001
            info.update({"state": "failed", "reason": f"{type(exc).__name__}: {exc}"})
            emit("error", {"message": f"{type(exc).__name__}: {exc}"})
            db.execute("UPDATE tasks SET state=?, updated_at=? WHERE id=?",
                       ("failed", db.now(), task_id))

    threading.Thread(target=_run, daemon=True).start()
    return {"task_id": task_id, "model": model, "mode": pol.mode,
            "policy": policy.describe(pol), "stream": f"/v1/tasks/{task_id}/stream"}


@app.post("/v1/night/estimate")
def estimate_night(req: NightReq):
    """两步法第一步：只构建蓝图 + 估算花费（不跑夜班），供用户充值参考。

    单价（谷/峰）与系统预算来自产品仓 .sixcoder/budget.json（用户/运维填写），引擎不编造价格。
    返回 {ok, estimate, budget, blueprint_caps, report}。
    """
    _guard("agent.run")
    text = (req.goal or req.requirement).strip()
    if not text:
        raise HTTPException(400, "estimate 需要 goal 或 requirement")
    ws = Path(req.repo)
    if not ws.exists():
        try:
            ws.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise HTTPException(400, f"仓库不可建：{req.repo}")
    from core import blueprint as _bp
    got = _bp.derive(text, llm=_bp._default_llm, ws=ws)
    if not got.get("ok"):
        raise HTTPException(422, f"蓝图推导失败：{got.get('problems')}")
    bp = got["blueprint"]
    try:
        _bp.save(bp, ws)
    except Exception:                                  # noqa: BLE001
        pass
    est = _be.estimate_from_blueprint(ws)
    snap = _be.budget_snapshot(ws)
    return {"ok": bool(est.get("ok")), "estimate": est,
            "budget": snap, "blueprint_caps": len(bp.caps),
            "report": (_be.format_report(est) if est.get("ok") else est.get("reason"))}


@app.get("/v1/plan")
def get_plan(repo: str):
    """自主规划只读视图：蓝图一页 + 切片 + 最近一次覆盖（不执行探针、不花 token）。

    全自主跑的进度就靠这三样东西看：计划长什么样、下一刀切在哪、上一轮观测结果。
    """
    _guard("agent.run")
    from core import blueprint, coverage, frontier, slicer
    ws = Path(repo)
    bp = blueprint.load(ws)
    if not bp.caps:
        return {"ok": False, "reason": "还没有蓝图：先用目标模式跑一次", "caps": []}
    plan = slicer.plan(bp)
    hs = coverage.history(ws, limit=1)
    last = hs[-1] if hs else {}
    return {"ok": True, "summary": blueprint.summary(bp), "version": bp.version,
            "goal": bp.goal, "total_budget_cny": bp.total_budget_cny,
            "caps": [c.to_dict() for c in bp.caps],
            "slices": plan["slices"], "blocked": plan["blocked"],
            "problems": plan["problems"], "funcs_left": plan["funcs"],
            "last_coverage": {"at": last.get("at"), "done": last.get("done"),
                              "total": last.get("total"),
                              "statuses": last.get("statuses") or {}},
            "next": [{"cap": c.id, "why": frontier.why(bp, c)}
                     for _s, c in frontier.rank(bp)[:8]]}


@app.get("/v1/plan/cover")
def plan_cover(repo: str):
    """真跑覆盖探针（本地执行，不花一次远端调用）：项目到底做完没有。"""
    _guard("agent.run")
    from core import blueprint, coverage
    ws = Path(repo)
    bp = blueprint.load(ws)
    if not bp.caps:
        raise HTTPException(400, "还没有蓝图")
    cov = coverage.run(bp, ws)
    coverage.record(ws, cov, "界面观测")
    blueprint.save(coverage.mark(bp, cov), ws)
    return {"ok": True, "ratios": coverage.ratios(cov),
            "report": coverage.report(coverage.mark(bp, cov), cov), "coverage": cov}


@app.get("/v1/plan/trace")
def plan_trace(repo: str, cap: str):
    """某条能力的来龙去脉：内容 / 观测方式 / 依赖 / 被依赖 / 切片 / 契约 / 最近观测。"""
    _guard("agent.run")
    from core import blueprint, coverage, probe
    ws = Path(repo)
    bp = blueprint.load(ws)
    c = bp.by_id(cap)
    if c is None:
        raise HTTPException(404, f"蓝图里没有 {cap}")
    hs = coverage.history(ws, limit=1)
    slice_files = {}
    if c.slice_id:
        d = ws / ".sixcoder" / "slices"
        for label, name in (("contract", f"{c.slice_id}.txt"),
                            ("delivery", f"{c.slice_id}.DELIVERY.md")):
            p = d / name
            slice_files[label] = {"path": str(p), "exists": p.exists()}
    return {"ok": True, "cap": c.to_dict(), "observable": probe.describe(c.probe),
            "dependents": [x.id for x in bp.caps if c.id in x.deps],
            "slice_files": slice_files,
            "last_status": (dict(hs[-1].get("statuses") or {}).get(cap) if hs else None)}


@app.get("/v1/night")
def list_night():
    """所有夜班的当前状态（前端"夜间任务"列表用）。"""
    out = []
    for tid, info in sorted(_NIGHT.items(), key=lambda kv: kv[1].get("started") or "",
                            reverse=True):
        out.append({"task_id": tid, **info})
    return {"nights": out}


@app.get("/v1/night/{task_id}")
def get_night(task_id: str):
    """夜班进展 + 交付报告（甲方早上就看这一份）。"""
    info = _NIGHT.get(task_id)
    if info is None:
        raise HTTPException(404, "没有这个夜班（进程重启后不保留）")
    body = ""
    path = info.get("delivery") or ""
    if path and Path(path).exists():
        body = Path(path).read_text(encoding="utf-8", errors="replace")[:20000]
    return {"task_id": task_id, **info, "delivery_md": body}


class AnswerReq(BaseModel):
    text: str
    repo: str


@app.get("/v1/questions")
def list_questions(repo: str):
    """待人回答的问题（开发者模式里本地模型主动提问）。"""
    from core import questions
    return {"questions": questions.pending(Path(repo))}


@app.post("/v1/questions/{qid}/answer")
def answer_question(qid: str, req: AnswerReq):
    """回答问题：答案作为 human 证据入账，同一问题以后不再重复问。"""
    from core import questions
    r = questions.answer(Path(req.repo), qid, req.text, by="ui")
    if not r.get("ok"):
        raise HTTPException(400, r.get("error") or "回答失败")
    return r


@app.get("/v1/repos")
def list_repos():
    """用过的仓库（= 对话）。一个仓库一行，带最新状态与任务数。"""
    rows = db.query("SELECT * FROM tasks ORDER BY datetime(updated_at) DESC")
    seen: dict[str, dict] = {}
    for r in rows:
        key = (r["repo"] or "").replace("\\", "/").lower()
        if not key:
            continue
        item = seen.get(key)
        if item is None:
            item = seen[key] = {
                "repo": r["repo"], "name": Path(r["repo"] or "").name,
                "state": r["state"], "task_id": r["id"],
                "requirement": r["requirement"], "updated_at": r["updated_at"],
                "workspace": r["workspace"], "conversations": 0, "nodes": 0,
            }
        item["conversations"] += 1
    for item in seen.values():
        n = db.query("SELECT COUNT(*) AS c FROM nodes WHERE task_id=?", (item["task_id"],))
        item["nodes"] = n[0]["c"] if n else 0
    return {"repos": list(seen.values())}


@app.get("/v1/tasks")
def list_tasks(repo: str = ""):
    """任务列表。传 repo 时只返回该仓库的任务（按时间倒序），用于"回到上次对话"。"""
    base = ("SELECT id,repo,state,model,requirement,created_at,updated_at,workspace FROM tasks")
    if repo.strip():
        # Windows 路径大小写/分隔符不敏感：两头统一
        needle = repo.strip().replace("\\", "/").lower()
        rows = [r for r in db.query(base + " ORDER BY created_at DESC LIMIT 200")
                if (r["repo"] or "").replace("\\", "/").lower() == needle]
        return {"tasks": rows[:50], "repo": repo}
    return {"tasks": db.query(base + " ORDER BY created_at DESC LIMIT 50")}


@app.delete("/v1/tasks")
def delete_all_tasks(confirm: str = "", with_workspace: bool = False):
    """一键清空历史任务。

    必须显式 confirm=all —— 否则像 `/v1/tasks/`（id 拼空的 URL）这种误拼会被当成"清空全部"，
    实测踩过：一条拼错 id 的删除请求把整个历史清空了。
    """
    _guard("agent.run")
    if confirm != "all":
        raise HTTPException(
            400,
            "清空全部历史需要显式确认：DELETE /v1/tasks?confirm=all"
            "（单条删除请用 DELETE /v1/tasks/{task_id}）")
    rows = db.query("SELECT id,workspace,state FROM tasks")
    res = db.delete_all_tasks(with_finished_only=True)
    removed = 0
    if with_workspace:
        import shutil
        root = agent.WORKSPACES.resolve()
        for r in rows:
            if r["id"] in res["kept_running"]:
                continue
            ws = Path(r["workspace"] or "")
            if str(ws) and ws.exists() and str(ws.resolve()).startswith(str(root)):
                shutil.rmtree(ws, ignore_errors=True)
                removed += 1
    return {"ok": True, "deleted_count": len(res["deleted"]), "rows": res["counts"],
            "kept_running": res["kept_running"], "workspace_removed": removed}


class NodeResolveReq(BaseModel):
    action: str          # accept | drop
    reason: str = ""


@app.post("/v1/tasks/{task_id}/nodes/{node_id}/resolve")
def resolve_node(task_id: str, node_id: str, req: NodeResolveReq):
    """人工处置一个 stuck（有节点没通过）节点：接受 / 丢弃（都写 human 证据）。

    与"等人工审批"（开发者模式的 needhuman）不是一回事：
    needhuman 是**事前**要人点头才继续；stuck 是**事后**节点没通过、等人裁量。
    """
    _guard("agent.run")
    rows = db.query("SELECT id,state FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, f"任务不存在：{task_id}")
    nodes = db.query("SELECT id,status,path FROM nodes WHERE task_id=? AND id=?", (task_id, node_id))
    if not nodes:
        raise HTTPException(404, f"节点不存在：{node_id}")
    if req.action not in ("accept", "drop"):
        raise HTTPException(400, "action 只能是 accept 或 drop")
    reason = (req.reason or "").strip() or ("人工接受（未填理由）" if req.action == "accept" else "人工丢弃")
    if req.action == "accept":
        db.set_node_status(task_id, node_id, "accepted", "human_accept", True, reason)
    else:
        db.set_node_status(task_id, node_id, "dropped", "human_drop", False, reason)
    state = db.recompute_state(task_id)
    return {"ok": True, "node": node_id, "status": "accepted" if req.action == "accept" else "dropped",
            "task_state": state, "reason": reason}


@app.post("/v1/tasks/{task_id}/retry")
def retry_task(task_id: str):
    """以该任务的工作区为基点再跑一轮（人工改好了想继续修时用）。"""
    _guard("agent.run")
    rows = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, f"任务不存在：{task_id}")
    t = rows[0]
    ws = Path(t["workspace"] or "")
    if not str(ws) or not ws.exists():
        raise HTTPException(409, "该任务没有可用的工作区副本，无法续跑（可改用它原始 repo 新建任务）")
    new_id = agent.new_task(ws, t["requirement"], t["target"] or "", t["model"])
    emit = agent.Emitter(new_id)
    agent.start_thread(new_id, ws, t["requirement"], t["target"] or "", t["model"], emit)
    return {"ok": True, "task_id": new_id, "based_on": task_id, "repo": str(ws)}


@app.delete("/v1/tasks/{task_id}")
def delete_task(task_id: str, with_workspace: bool = False):
    """删除历史任务。默认只删记录；with_workspace=true 时连带删除工作区副本。"""
    _guard("agent.run")
    rows = db.query("SELECT id,workspace,state FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, f"任务不存在：{task_id}")
    if rows[0]["state"] == "running":
        raise HTTPException(409, "任务还在执行中，先等它结束或重启服务清理僵尸任务后再删除")
    ws = Path(rows[0]["workspace"] or "")
    counts = db.delete_task(task_id)
    removed = False
    # 只删 workspaces/ 下的副本，绝不碰用户原始仓库
    if with_workspace and str(ws) and ws.exists():
        root = agent.WORKSPACES.resolve()
        try:
            inside = ws.resolve().is_relative_to(root)
        except AttributeError:            # Python 3.8 兼容
            inside = str(ws.resolve()).startswith(str(root))
        if inside:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)
            removed = True
    return {"ok": True, "deleted": task_id, "rows": counts, "workspace_removed": removed}


@app.get("/v1/tasks/{task_id}")
def get_task(task_id: str):
    rows = db.query("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "任务不存在")
    task = rows[0]
    nodes = db.query("SELECT * FROM nodes WHERE task_id=? ORDER BY seq", (task_id,))
    gate_runs = db.query(
        "SELECT node_id,attempt,gate,ok,evidence_kind,evidence_ref,detail,elapsed_ms"
        " FROM gate_runs WHERE task_id=? ORDER BY id", (task_id,))
    events = db.query("SELECT id,ts,kind,payload FROM events WHERE task_id=? ORDER BY id", (task_id,))
    audit = db.audit(task_id)
    return {"task": task, "nodes": nodes, "gate_runs": gate_runs,
            "events": events, "audit": audit}


@app.get("/v1/tasks/{task_id}/stream")
async def stream(task_id: str):
    """轮询 SQLite 的 SSE（比跨线程 asyncio 队列更稳，3B 场景下延迟足够）。"""
    async def gen():
        last = 0
        terminal = {"done", "stuck", "failed", "needs_human"}   # needs_human 兼容旧记录
        idle = 0
        while True:
            rows = db.query("SELECT id,ts,kind,payload FROM events WHERE task_id=? AND id>? ORDER BY id",
                            (task_id, last))
            for r in rows:
                last = r["id"]
                data = {"kind": r["kind"], "ts": r["ts"],
                        "payload": json.loads(r["payload"] or "{}")}
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
            t = db.query("SELECT state FROM tasks WHERE id=?", (task_id,))
            state = t[0]["state"] if t else "unknown"
            if state in terminal and not rows:
                idle += 1
                if idle >= 2:
                    yield f"data: {json.dumps({'kind': 'stream_end', 'payload': {'state': state}}, ensure_ascii=False)}\n\n"
                    return
            else:
                idle = 0
            await asyncio.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/v1/tasks/{task_id}/diff")
def task_diff(task_id: str):
    rows = db.query("SELECT DISTINCT workspace FROM tasks WHERE id=?", (task_id,))
    if not rows:
        raise HTTPException(404, "任务不存在")
    ws = Path(rows[0]["workspace"] or "")
    if not ws.exists():
        return {"diff": "", "stat": ""}
    rc, diff = tools.git(ws, "diff", "HEAD~1", "HEAD") if (rc := tools.git(ws, "rev-parse", "HEAD")[0]) == 0 else (1, "")
    rc2, stat = tools.git(ws, "log", "--oneline", "-8")
    return {"diff": diff, "log": stat}


# ---------------------------------------------------------------- Git 按键（固化常见操作）

@app.get("/v1/tasks/{task_id}/git/status")
def git_status(task_id: str):
    return gitops.status(ws_of(task_id))


@app.get("/v1/tasks/{task_id}/git/log")
def git_log(task_id: str, n: int = 12):
    return gitops.log(ws_of(task_id), n)


@app.get("/v1/tasks/{task_id}/git/diff")
def git_diff(task_id: str, rev: str = "", path: str = ""):
    return gitops.diff(ws_of(task_id), rev, path)


@app.get("/v1/tasks/{task_id}/git/branches")
def git_branches(task_id: str):
    return gitops.branches(ws_of(task_id))


@app.post("/v1/tasks/{task_id}/git/snapshot")
def git_snapshot(task_id: str, req: GitCommitReq):
    return gitops.snapshot(ws_of(task_id), req.message or "手动快照")


@app.post("/v1/tasks/{task_id}/git/undo")
def git_undo(task_id: str, req: GitPathReq):
    return gitops.undo_file(ws_of(task_id), req.path)


@app.post("/v1/tasks/{task_id}/git/undo-sandbox")
def git_undo_sandbox(task_id: str, req: SandboxUndoReq):
    return gitops.sandbox_undo(ws_of(task_id), req.steps)


@app.post("/v1/tasks/{task_id}/git/branch")
def git_branch(task_id: str, req: GitBranchReq):
    return gitops.new_branch(ws_of(task_id), req.name)


@app.get("/v1/sandbox")
def sandbox_info(repo: str = ""):
    """沙箱策略说明（只读展示；边界见 docs 与 core/sandbox.py 文件头）。"""
    target = Path(repo) if repo else DEMO
    try:
        return tools.sandbox(target).describe()
    except Exception as exc:
        return {"error": str(exc), "workspace": str(target)}


# ---------------------------------------------------------------- 产物浏览（完整 demo 用）

@app.get("/v1/tasks/{task_id}/files")
def task_files(task_id: str):
    ws = ws_of(task_id)
    files = tools.list_files(ws, limit=300)
    return {"files": files, "count": len(files)}


@app.get("/v1/tasks/{task_id}/file")
def task_file(task_id: str, path: str):
    ws = ws_of(task_id)
    try:
        text = tools.read_file(ws, path, 1, 600)
    except tools.ToolError as exc:
        raise HTTPException(400, str(exc))
    return {"path": path, "content": text}


# ---------------------------------------------------------------- 代码审查（改进意见）

@app.post("/v1/review")
def do_review(req: ReviewReq):
    """对现有代码提改进意见；给了「预期功能」时同时找实现与预期不符之处。"""
    _guard("review.run")
    ws = resolve_repo(req.repo, req.task_id)
    if req.use_model and not permissions.allows("review.model"):
        result = review_mod.review(ws, req.expectation, None, False)
        result["model"] = {"used": False, "skipped_by_mode": True,
                           "note": f"当前模式「{permissions.current_mode().name}」不调用模型，已降级为纯静态审查"}
        return result
    model = ollama.pick_default() if (req.use_model and ollama.alive()) else None
    return review_mod.review(ws, req.expectation, model, bool(req.use_model and model))


# ---------------------------------------------------------------- 静态前端

@app.middleware("http")
async def _no_cache_html(request, call_next):
    """HTML 一律不缓存：否则改了前端、重开窗口还是旧页面（实测踩过）。"""
    resp = await call_next(request)
    path = request.url.path
    if path in ("/", "/index.html", "/v1/web-version"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/v1/web-version")
def web_version():
    """当前前端 bundle 指纹：用来确认界面到底加载了哪一版。"""
    assets = WEB_DIST / "assets"
    files = sorted(assets.glob("index-*.js")) if assets.exists() else []
    newest = max(files, key=lambda p: p.stat().st_mtime) if files else None
    return {"bundle": newest.name if newest else "", "built_at": newest.stat().st_mtime if newest else 0}


if WEB_DIST.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIST), html=True), name="web")
else:
    @app.get("/")
    def no_dist():
        return JSONResponse({"message": "前端未构建。请在 web/ 下执行 npm run build，或使用 npm run dev（5273 端口）。"})


if __name__ == "__main__":
    db.init()
    stale = db.recover_stale()
    ensure_demo()
    import uvicorn
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8760
    print(f"[SixCoder] http://127.0.0.1:{port}   前端构建产物：{WEB_DIST.exists()}   清理僵尸任务：{stale}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
