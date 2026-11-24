"""SixCoder 服务（FastAPI + SSE）。

启动：
    python server.py            # http://127.0.0.1:8760
首次运行会自动创建演示仓库 demo-repo/（git init + 2 个提交）。
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
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
from core import paths as _paths  # noqa: E402
from core import pathbind as _pathbind  # noqa: E402
from core import clarify as _clarify  # noqa: E402
from core import coderun as _coderun  # noqa: E402
from core import codelocate as _codelocate  # noqa: E402
from core import runtimes as _runtimes  # noqa: E402
from core.demo import DEMO, ensure_demo  # noqa: E402

# 自带 Git（安装器勾选 MinGit）不在系统 PATH 里：临时加进当前进程，
# 让 tools/isolate/arch 里的 ["git", ...] 真能用；不写系统 PATH。
try:
    _runtimes.ensure_on_path("git")
except Exception:                                    # noqa: BLE001
    pass

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


# ---------------------------------------------------------------- 人工审核（精修模式）
class ApprovalReq(BaseModel):
    decision: str = "approved"
    by: str = "ui"


@app.get("/v1/approvals")
def list_approvals():
    """待审单据：精修模式下前端轮询这个，弹出"通过/拒绝"。"""
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
                   "venv", "env", "target",           # 2026-11-23：与 fs/tree 的 _SKIP_DIRS 对齐
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
    """**启动探针必须快**（外壳只等几百毫秒）。

    这里不查 Ollama 模型列表：Ollama 没启动时每次探测都要等超时，health 会拖到
    10s+，外壳的 800ms 探针全部超时 → 界面永远停在"后端未就绪"。模型列表拆到
    `/v1/models`，由界面加载后异步补（慢一点不影响启动）。
    """
    try:
        up = ollama.alive(timeout=0.3)
    except TypeError:                                    # 兼容旧签名
        up = ollama.alive()
    return {
        "ok": True,
        # 桌面外壳靠这个字段确认"端口上跑的真是 SixCoder 后端"，而不是碰巧占用
        # 该端口的别的程序（否则会把别人的页面当成后端，界面全蓝无按钮）。
        "app": "sixcoder",
        "ollama": up,
        "models": [],
        "default_model": "",
        "model_policy": {"min_params_b": model_policy.MIN_PARAMS_B,
                         "blocked": [], "suggest": "", "message": ""},
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
    """常用工具全量清单（含未安装项 + PATH 绑定状态，默认未绑定）。"""
    ov = _load_tc_overrides()
    return {"items": _pathbind.state(ov), "overrides": ov}


class PathBindReq(BaseModel):
    id: str
    bind: bool = True


@app.post("/v1/pathbind")
def set_pathbind(req: PathBindReq):
    """用户自己决定要不要把工具目录写进当前用户 PATH（默认不写）。"""
    if not permissions.allows("toolchain.config"):
        raise HTTPException(403, "当前模式不允许修改 PATH 绑定")
    r = _pathbind.bind(req.id) if req.bind else _pathbind.unbind(req.id)
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail", "绑定失败"))
    return r


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
        # 平铺 api_key 为空但厂商 key 类里填了 key：状态/掩码要反映真实来源，
        # 否则界面显示"未配置"红点、下面却能看到已保存的 key（2026-11-21 实测）。
        if not _rem.get("key_configured"):
            _provs = _rem["providers"]
            _cur = next((p for p in _provs if p.get("current")), _provs[0] if _provs else None)
            if _cur:
                _k = next((k for k in (_cur.get("keys") or [])
                           if k.get("key_configured")), None)
                if _k:
                    _rem["key_configured"] = True
                    _rem["key_masked"] = _k.get("key_masked") or ""
                    _rem["key_source"] = "厂商 key"
    except Exception:                                    # noqa: BLE001
        pass
    return {"remote": _rem, "available": ok, "reason": why,
            "usage": remote_mod.usage_summary()}


# ---------------------------------------------------------------- UI 审美样式库
# 审美判据在人眼里，引擎不自检"好不好看"；这里只提供有限类型让用户挑，
# 颜色是用户自填插槽。选出来的约定经 uistyles.contract_text() 注入生成链路。
@app.get("/v1/uistyles")
def get_uistyles():
    """全部样式预设 + 类型清单 + 颜色插槽 + 当前选择（颜色无写死值）。"""
    from core import uistyles
    return uistyles.public_config()


class UIStyleSelectReq(BaseModel):
    id: str
    colors: dict[str, str] = {}


@app.post("/v1/uistyles/select")
def select_uistyle(req: UIStyleSelectReq):
    """用户挑一个预设并自填颜色插槽。非法色值/未知插槽 400，不写坏配置。"""
    from core import uistyles
    try:
        return {"ok": True, "selection": uistyles.select(req.id, req.colors),
                "contract": uistyles.contract_text()}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


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
    dev_remote: Optional[bool] = None       # 精修模式是否允许调用远端（甲方强制开、只读强制关）
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


class RemoteKeyTestReq(BaseModel):
    """测试某一吧 key（未保存的新 key 可直接放 api_key，不落盘）。"""
    provider: str
    key_id: str = ""
    model: str = ""
    api_key: str = ""


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
                # 厂商级 api_key 可能为空，但下面 key 类里有 key；只看厂商级会误报"未配置"
                "key_configured": bool(p.get("api_key")) or any(
                    str(k.get("api_key") or "").strip() for k in (p.get("keys") or [])),
                "key_masked": settings_store.mask(p.get("api_key") or "") or next(
                    (settings_store.mask(k.get("api_key") or "")
                     for k in (p.get("keys") or []) if str(k.get("api_key") or "").strip()), ""),
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


@app.post("/v1/settings/remote/key-test")
def test_remote_key(req: RemoteKeyTestReq):
    """按「厂商 + 具体 key」真打 1 token，验证这把 key 能不能用。

    和全局 `/remote/test` 的区别：全局测试读的是"当前生效"配置；用户在厂商详情页
    填的可能是备用 key / 未保存的新 key，必须能单独测。新 key 不落盘。
    """
    from core import remote as remote_mod, settings_store
    provs = settings_store.providers()
    p = next((x for x in provs if str(x.get("id")) == req.provider), None)
    if not p:
        return {"ok": False, "detail": f"没有这个厂商：{req.provider}"}
    k = next((x for x in (p.get("keys") or []) if str(x.get("id")) == req.key_id), {}) if req.key_id else {}
    key = (req.api_key or "").strip() or str(k.get("api_key") or "").strip() \
        or str(p.get("api_key") or "").strip()
    if not key:
        return {"ok": False, "detail": "这把 key 还没填"}
    models = [m for m in (k.get("models") or []) if str(m.get("model") or "").strip()]
    if not models:
        models = [m for m in (p.get("models") or []) if str(m.get("model") or "").strip()]
    model = (req.model or "").strip() or (str(models[0].get("model")) if models else "")
    if not model:
        preset = settings_store.PROVIDER_PRESETS.get(req.provider, {})
        model = str(((preset.get("models") or [{}])[0]).get("model") or "")
    if not model:
        return {"ok": False, "detail": "这个厂商还没有配置模型名"}
    base = str(p.get("base_url") or k.get("base_url") or "").strip()
    if not base:
        if req.provider == "deepseek":
            base = remote_mod.DEEPSEEK_BASE
        elif req.provider == "glm":
            base = remote_mod.GLM_BASE
        else:
            base = remote_mod.OPENAI_COMPAT_BASE
    payload = {"model": model, "messages": [{"role": "user", "content": "回复一个字：好"}],
               "max_tokens": 8, "stream": False}
    data, err, ms = remote_mod._post_once(payload, base.rstrip("/"), key, timeout=25)
    if data is None:
        detail = err or "调用失败"
        if "401" in detail:
            detail = ("401 鉴权失败：确认「请求网址 / 模型名 / key」属于同一家厂商。"
                      + detail)
        return {"ok": False, "detail": detail, "model": model, "base_url": base, "ms": ms}
    try:
        answer = data["choices"][0]["message"].get("content") or ""
    except Exception:                                    # noqa: BLE001
        answer = ""
    return {"ok": True, "detail": f"连通（{ms} ms，{model}）", "answer": answer[:80],
            "model": model, "base_url": base, "ms": ms}


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


# ---------------------------------------------------------------- 受控部署通道（§运行时补短板）
class DeployTargetsReq(BaseModel):
    targets: list[dict] = []
    enabled: bool | None = None
    reveal: bool = False


class DeployExecReq(BaseModel):
    target: str
    command: str
    reason: str = ""
    workspace: str = ""
    approval_timeout: float = 600.0   # 等人审秒数；CLI/夜班可调短，界面默认 600
    # 一次性密码（仅 credential=prompt 的目标用）：只在本次请求的内存里，
    # 经 secret_fn 注入 ssh 子进程，绝不落盘 / 不进日志 / 不回显 / 不进配置。
    password: str = ""


@app.get("/v1/deploy")
def get_deploy(reveal: bool = False):
    """部署通道配置（默认脱敏：私钥路径只给掩码）。GET 本身不执行任何命令。"""
    from core import deploy as _deploy
    out = _deploy.public_config(reveal=reveal)
    out["capability"] = _deploy.describe()
    return out


@app.post("/v1/deploy")
def set_deploy(req: DeployTargetsReq):
    """预留/修改部署目标（host/user/认证/命令白名单）。这是人工授权的入口。"""
    from core import deploy as _deploy
    if not permissions.allows("deploy.exec"):
        raise HTTPException(403, "当前模式不允许配置部署通道（readonly 模式禁用）")
    return _deploy.save_targets(req.targets, enabled=req.enabled)


@app.post("/v1/deploy/exec")
def run_deploy(req: DeployExecReq):
    """请求执行一次部署命令：命令白名单 + 强制人审 + 审计；失败如实返回 rc/原因。"""
    from core import deploy as _deploy
    ws = Path(req.workspace) if req.workspace else _paths.home()
    # 一次性密码：只在请求体内，包成 secret_fn 闭包传给 prompt 凭据；env:/file: 目标忽略它。
    # 注意：绝不把 req.password 写日志/审计/响应——execute 内部走 SSH_ASKPASS，用完即焚。
    secret_fn = (lambda: req.password) if req.password else None
    return _deploy.execute(req.target, req.command, reason=req.reason, workspace=ws,
                           approval_timeout=req.approval_timeout, secret_fn=secret_fn)


# ---------------------------------------------------------------- 闭环验收（最后落地）
class VerifyReq(BaseModel):
    pid: str = ""            # 指定 UI 风格预设（空=用当前选择）
    adapter: str = ""        # 指定插件桥适配器
    source: str = ""         # 指定判据开放来源
    checks: list[str] = []   # 额外真跑的 check_*.py（可选）


@app.post("/v1/verify")
def run_verify(req: VerifyReq):
    """跑闭环验收单：结构/约定/内存闭环/探针 + 可选真跑自检脚本。审美仍标人在环。"""
    from core import verify as _v
    return _v.run_all(pid=req.pid, adapter_id=req.adapter,
                      source_id=req.source, checks=req.checks)


@app.get("/v1/verify/capability")
def verify_capability():
    from core import verify as _v
    return _v.describe()


# ---------------------------------------------------------------- 判据开放调研
@app.get("/v1/judgment-sources")
def list_judgment_sources():
    """按公开开发文档整理的'判据开放'清单：哪些外部判据能搬成机判（只读）。"""
    from core import judgment_sources as _js
    return {"sources": _js.list_sources(), "capability": _js.describe()}


@app.get("/v1/judgment-sources/contract")
def judgment_source_contract(source: str = ""):
    """给远端的判据开放说明：原本判不了的现在能机读到什么。"""
    from core import judgment_sources as _js
    text = _js.contract_text(source)
    return {"ok": bool(text), "source": source, "contract": text}


# ---------------------------------------------------------------- 闭源宿主插件适配
@app.get("/v1/host-plugins")
def list_host_plugins():
    """按公开文档整理的办公软件插件桥清单：可验判据 + 人在环边界（只读）。"""
    from core import host_plugins as _hp
    return {"adapters": _hp.list_adapters(), "capability": _hp.describe()}


@app.get("/v1/host-plugins/probe")
def host_plugin_probe(adapter: str, workspace: str = ""):
    """给远端的一则插件桥验收约定（含探针建议；不真执行）。"""
    from core import host_plugins as _hp
    return _hp.probe_suggestion(adapter, workspace)


# ---------------------------------------------------------------- 外部真链路半自动
# 人填一次 → 会话内缓存（内存）→ 引擎后续自动代跑。凭据不落盘、不进审计/视图。
class HumanOncePutReq(BaseModel):
    namespace: str
    key: str
    value: str
    ttl: float = 900.0
    uses: int = 1


class HumanOnceRunReq(BaseModel):
    steps: list[dict] = []
    namespace: str = "flow"


@app.get("/v1/human-once")
def human_once_state():
    """会话内已缓存项 + 能力声明（都不含值）。"""
    from core import human_once as _h
    return {"pending": _h.pending(), "capability": _h.describe()}


@app.post("/v1/human-once/put")
def human_once_put(req: HumanOncePutReq):
    """人填一次：把验证码/OTP/登录态存入会话缓存。值不落盘、不回显。"""
    from core import human_once as _h
    try:
        return {"ok": True, "receipt": _h.put(req.namespace, req.key, req.value,
                                              ttl=req.ttl, uses=req.uses)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/v1/human-once/clear")
def human_once_clear(namespace: str = "", key: str = ""):
    """清除缓存（key 空=清该命名空间，都空=清全部）。"""
    from core import human_once as _h
    if namespace and key:
        return {"ok": True, "cleared": 1 if _h.clear(namespace, key) else 0}
    if namespace:
        n = 0
        for item in _h.pending():
            if item["namespace"] == namespace:
                n += 1 if _h.clear(namespace, item["key"]) else 0
        return {"ok": True, "cleared": n}
    return {"ok": True, "cleared": _h.clear_all()}


@app.post("/v1/human-once/run")
def human_once_run(req: HumanOnceRunReq):
    """跑半自动步骤表。缺人填的值就停在那一步并回报 need（UI 据此弹输入框）。

    执行器是**受控占位**：引擎不碰真实第三方；human 值只用于标记"该步人已提供"，
    真正的外部动作（输入验证码/确认支付）由人或平台插件完成——不谎报全自动。
    """
    from core import human_once as _h
    def executor(step: dict, value: str) -> dict:
        kind = str(step.get("kind") or "auto")
        return {"action": step.get("action") or step.get("slot") or kind,
                "human_provided": bool(value),
                "note": "占位执行器：外部动作由人/平台插件完成，引擎不自验真链路"}
    return _h.run_steps(req.steps, executor, namespace=req.namespace)


# ---------------------------------------------------------------- 声明式运维单元
class OpsUnitsReq(BaseModel):
    units: list[dict] = []
    enabled: bool | None = None


class OpsExecReq(BaseModel):
    unit: str
    workspace: str = ""
    timeout: float = 600.0
    password: str = ""   # 一次性密码，仅目标 credential=prompt 时用；不落盘/不回显


@app.get("/v1/ops")
def get_ops():
    """运维单元配置（不含凭据；目标 host 沿用 deploy 的脱敏视图）。"""
    from core import ops as _ops
    return _ops.public_config()


@app.post("/v1/ops")
def set_ops(req: OpsUnitsReq):
    """预留/修改运维单元（人工授权入口）。readonly 模式禁用。"""
    from core import ops as _ops
    if not permissions.allows("deploy.exec"):
        raise HTTPException(403, "当前模式不允许配置运维单元（readonly 模式禁用）")
    return _ops.save_units(req.units, enabled=req.enabled)


@app.post("/v1/ops/exec")
def run_ops(req: OpsExecReq):
    """执行一个运维单元：一次协商 + 一次人审覆盖整组，失败自动回滚。"""
    from core import ops as _ops
    ws = Path(req.workspace) if req.workspace else _paths.home()
    secret_fn = (lambda: req.password) if req.password else None
    return _ops.execute(req.unit, workspace=ws, timeout=req.timeout, secret_fn=secret_fn)


# ---------------------------------------------------------------- 多机编排
class OrchPlansReq(BaseModel):
    plans: list[dict] = []
    enabled: bool | None = None


class OrchExecReq(BaseModel):
    plan: str
    workspace: str = ""
    timeout: float = 600.0
    password: str = ""   # 一次性密码，仅目标 credential=prompt 时用；不落盘/不回显


@app.get("/v1/orchestration")
def get_orchestration():
    """编排计划配置（不含凭据；目标 host 沿用 deploy 的脱敏视图）。"""
    from core import orchestration as _orch
    return _orch.public_config()


@app.post("/v1/orchestration")
def set_orchestration(req: OrchPlansReq):
    """预留/修改编排计划（人工授权入口）。readonly 模式禁用。"""
    from core import orchestration as _orch
    if not permissions.allows("deploy.exec"):
        raise HTTPException(403, "当前模式不允许配置编排计划（readonly 模式禁用）")
    return _orch.save_plans(req.plans, enabled=req.enabled)


@app.post("/v1/orchestration/exec")
def run_orchestration(req: OrchExecReq):
    """执行一个编排计划：一次协商 + 一次人审覆盖整批主机，单机失败隔离。"""
    from core import orchestration as _orch
    ws = Path(req.workspace) if req.workspace else _paths.home()
    secret_fn = (lambda: req.password) if req.password else None
    return _orch.execute(req.plan, workspace=ws, timeout=req.timeout, secret_fn=secret_fn)


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


@app.get("/v1/walkthrough")
def run_walkthrough(verify: bool = False):
    """端到端链路走查：按用户旅程过一遍新能力（只读/内存态，不改配置、不连真实服务器）。

    `verify=true` 时额外真跑一张验收单（会开浏览器，较慢）。
    """
    from core import walkthrough as _w
    return _w.walkthrough(include_verify=verify)


# ---------------------------------------------------------------- 阶段③：只读审计 + 文档生成
#
# 为什么补这两个端点（2026-11-23）：盘查发现第③段"审阅模式"只有 walkthrough 一个入口，
# 而 archrules / lint / debt / testweight / detectors 这些审计模块**早就写好了**、
# 却只被自检脚本调用，用户根本碰不到。文档也只有 README + LICENSE。
# 这不是缺实现，是**缺接线**——能力在代码里，没接到产品面上。

@app.get("/v1/audit")
def run_audit(repo: str = ""):
    """只读审计：零 token 跑一遍确定性检查，返回结构化发现（不改任何东西）。"""
    _guard("file.read")
    from core import audit as _audit
    ws = _fs_base(repo) if repo.strip() else None
    if ws is None or not ws.is_dir():
        raise HTTPException(400, f"仓库不存在：{repo}")
    return _audit.audit(ws)


@app.get("/v1/audit/report")
def audit_report(repo: str = ""):
    """审计的人读版（Markdown 文本），前端可直接渲染。"""
    _guard("file.read")
    from core import audit as _audit
    ws = _fs_base(repo) if repo.strip() else None
    if ws is None or not ws.is_dir():
        raise HTTPException(400, f"仓库不存在：{repo}")
    res = _audit.audit(ws)
    return {"ok": res["ok"], "summary": res["summary"],
            "markdown": _audit.render_report(res)}


@app.post("/v1/docs/stage3")
def docs_stage3(req: dict):
    """生成阶段③的三份开发者文档：MODULES / ARCHITECTURE / HANDOVER。

    **只写这三份文档文件**，不碰源码、不改配置。项目名可不传（取目录名）。
    """
    _guard("file.write")                    # 审阅模式禁止写盘，语义正确
    from core import docs as _docs
    repo = str((req or {}).get("repo") or "").strip()
    ws = _fs_base(repo) if repo else None
    if ws is None or not ws.is_dir():
        raise HTTPException(400, f"仓库不存在：{repo}")
    project = str((req or {}).get("project") or "").strip()
    return _docs.write_stage3_docs(ws, project)


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
    plan: str = ""                 # 精修模式：人给的实现方案（权威）
    use_remote: bool = True
    # 目标代码规模：不同规模的约束完全不同（core/scale.py）。
    # 造物模式要求提需求时写明；没写就按实测推断，并在交付报告里注明。
    scale: str = ""                # S / M / L / XL
    remote_profile: str = ""       # 右上角「远端模型」：只走这个档案（flash / pro），空 = 整条阶梯


_NIGHT: dict[str, dict] = {}


@app.post("/v1/night")
def create_night(req: NightReq):
    """夜班（公司制）：造物模式提需求 → 系统内部做完所有决策 → 早上给交付报告。

    与 `/v1/tasks` 的区别：task 是"一个需求 → 一对测试/实现节点"；
    night 是"一个需求 → 反复体检/派工/开发/机械验收，直到收敛或预算耗尽"。
    进度直接用 `/v1/tasks/{task_id}/stream` 看（事件同一条通道）。
    """
    _guard("agent.run")
    pol = policy.current()
    if pol.code_escalation == policy.OFF:
        raise HTTPException(403, f"{pol.name}不写文件，不能跑夜班；请切到造物模式或精修模式")
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
            # 没写规模就把"该问的问题"显式抛给前端（造物模式：需求里要有明确规模）
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


# ---------------------------------------------------------------- 文件读写（精修模式编辑器）

# 噪音目录：编辑器树里一律不显示（这些不是"代码"，列出来只会淹没真正的文件）
_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env",
              "dist", "build", "target", ".pytest_cache", ".mypy_cache",
              ".ruff_cache", ".idea", ".vscode", ".sixcoder"}
_TEXT_MAX = 1024 * 1024          # 编辑器单文件上限 1MB（再大就不该在浏览器里改）


class FsWriteReq(BaseModel):
    repo: str
    path: str
    content: str
    backup: bool = True


def _fs_base(repo: str) -> Path:
    ws = Path(repo)
    if not ws.is_dir():
        raise HTTPException(400, f"仓库不存在：{repo}")
    return ws


@app.get("/v1/fs/tree")
def fs_tree(repo: str, path: str = "", depth: int = 4, limit: int = 2000):
    """仓库文件树（扁平列表，前端自己拼树）。

    路径一律过 `tools.safe_path`（沙箱牢笼）——编辑器和引擎改代码走同一道门，
    不允许"界面能改到仓库外面"。
    """
    _guard("file.read")
    ws = _fs_base(repo)
    base = tools.safe_path(ws, path or ".")
    if not base.is_dir():
        raise HTTPException(400, f"不是目录：{path}")
    root = ws.resolve()
    try:
        base_depth = len(base.resolve().relative_to(root).parts)
    except ValueError:
        raise HTTPException(400, "路径越界")
    items: list[dict] = []
    truncated = False
    for cur, dirs, files in os.walk(base):
        cur_p = Path(cur)
        try:
            rel_depth = len(cur_p.resolve().relative_to(root).parts) - base_depth
        except ValueError:
            continue
        dirs[:] = sorted(d for d in dirs
                         if d not in _SKIP_DIRS and not d.startswith("."))
        if rel_depth >= max(1, depth):
            dirs[:] = []
        for d in dirs:
            items.append({"path": str((cur_p / d).relative_to(root)).replace("\\", "/"),
                          "dir": True, "size": 0})
        for f in sorted(files):
            p = cur_p / f
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            items.append({"path": str(p.relative_to(root)).replace("\\", "/"),
                          "dir": False, "size": size})
        if len(items) >= limit:
            truncated = True
            break
    return {"root": str(root), "items": items[:limit], "truncated": truncated}


@app.get("/v1/fs/read")
def fs_read(repo: str, path: str):
    """读原始文本（**不带行号前缀**——带前缀的是给模型看的，编辑器要原文）。"""
    _guard("file.read")
    ws = _fs_base(repo)
    p = tools.safe_path(ws, path)
    if not p.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    size = p.stat().st_size
    if size > _TEXT_MAX:
        raise HTTPException(413, f"文件过大（{size // 1024} KB），编辑器只支持 1MB 以内")
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:                               # noqa: BLE001
        raise HTTPException(500, f"读失败：{exc}")
    return {"path": path, "text": text, "size": size,
            "binary": "\x00" in text[:2000],
            "lines": text.count("\n") + (0 if text.endswith("\n") else 1) if text else 0}


@app.post("/v1/fs/write")
def fs_write(req: FsWriteReq):
    """保存文件：走 `tools.write_file`（沙箱牢笼 + 默认备份），与远端改代码同一条写盘路径。"""
    _guard("file.write")
    ws = _fs_base(req.repo)
    try:
        tools.write_file(ws, req.path, req.content, backup=req.backup)
    except Exception as exc:                             # noqa: BLE001
        raise HTTPException(400, f"写入失败：{type(exc).__name__}: {exc}")
    return {"ok": True, "path": req.path,
            "bytes": len(req.content.encode("utf-8")), "backup": bool(req.backup)}


# ---------------------------------------------------------------- 编辑器：本地模型自动填充

class EditorFillReq(BaseModel):
    repo: str = ""
    path: str = ""
    before: str = ""          # 光标之前的文本（前端只发尾部，避免把整个文件塞给 7B）
    after: str = ""           # 光标之后的文本（用于衔接，模型不许重复它）
    selected: str = ""        # 有选中就改写这段，否则按续写处理
    model: str = ""           # 空 = 自动挑本地最强
    instruction: str = ""     # 可选：用户额外要求（例如"补全这个函数的错误处理"）
    source: str = "local"     # local = 本地 Ollama ｜ remote = 远端（精修模式自己选）


class EditorAssistReq(BaseModel):
    """选中即改：explain=解释 / edit=改 / test=补测试。"""
    repo: str = ""
    path: str = ""
    action: str = "explain"   # explain | edit | test
    selected: str = ""
    before: str = ""          # 选区前文（给模型看上下文）
    after: str = ""           # 选区后文
    instruction: str = ""     # edit 的意图，例如"先校验再入库"
    model: str = ""
    source: str = "local"


_LANG_BY_EXT = {
    ".py": "python", ".pyi": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".vue": "vue", ".java": "java", ".go": "go", ".rs": "rust", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp", ".php": "php",
    ".rb": "ruby", ".kt": "kotlin", ".swift": "swift", ".sql": "sql",
    ".html": "html", ".css": "css", ".scss": "scss", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".sh": "bash", ".ps1": "powershell",
    ".md": "markdown", ".toml": "toml", ".ini": "ini",
}


def _editor_lang(path: str) -> str:
    return _LANG_BY_EXT.get(Path(path).suffix.lower(), "text")


def _strip_fence(text: str) -> str:
    """去掉模型爱加的 markdown 围栏（7B 几乎每次都想包一层 ```）。"""
    t = (text or "").strip("\n")
    lines = t.splitlines()
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    elif lines and lines[-1].strip().startswith("```") and len(lines[-1].strip()) <= 6:
        lines = lines[:-1]
    return "\n".join(lines)


def _editor_complete(prompt: str, *, source: str, model: str,
                    node_id: str = "editor_fill") -> dict:
    """编辑器里的模型调用（fill 与 assist 共用），返回 {ok,text,model,ms,tokens,source}。

    local = 本地 Ollama（快、免费、短续写）；remote = 远端最强档（慢、要钱、能读懂意图）。
    这里只负责"把 prompt 发出去拿文本"，业务 prompt 由调用方拼。
    """
    use_remote = str(source or "local").strip().lower() == "remote"
    if not use_remote:
        if not ollama.alive():
            return {"ok": False, "reason": "本地模型没在运行：请先启动 Ollama（ollama serve）"}
        model = (model or "").strip() or ollama.pick_default()
        if not model:
            return {"ok": False, "reason": "没有可用的本地模型：请先 ollama pull qwen2.5-coder:7b"}
        if model not in ollama.models():
            model = ollama.pick_default() or model

    if use_remote:
        # 远端：走 core.remote 的最强档。精修模式在这里是"人已经看着代码点了发送"，
        # 属于明确意图，不需要再走审批闸门（gate_remote_call 在作者链路里负责那件事）。
        t0 = time.time()
        try:
            from core import remote as _remote           # noqa: PLC0415
            from core import settings_store as _ss       # noqa: PLC0415
            ok, why = _remote.available()
            if not ok:
                return {"ok": False, "reason": f"远端不可用：{why}"}
            r = _remote.complete(prompt=prompt, node_id=node_id,
                                 step_index=_ss.strongest_step_index(), max_out=3000)
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "reason": f"远端调用失败：{type(exc).__name__}: {exc}"}
        if not r.get("ok"):
            return {"ok": False, "reason": f"远端拒绝/失败：{r.get('error') or '未知原因'}"}
        text = _strip_fence(r.get("answer") or "")
        model = "远端"
        ms = int((time.time() - t0) * 1000)
        tokens = (r.get("usage") or {}).get("completion_tokens")
    else:
        try:
            res = ollama.chat([{"role": "user", "content": prompt}], model,
                              temperature=0.15, num_predict=420, num_ctx=8192)
        except Exception as exc:                         # noqa: BLE001
            return {"ok": False, "reason": f"本地模型调用失败：{type(exc).__name__}: {exc}"}
        text = _strip_fence(res.get("text") or "")
        ms = res.get("ms")
        tokens = res.get("eval_count")

    if not text.strip():
        return {"ok": False, "reason": "模型没有给出内容（可再试一次或换个说法）",
                "model": model, "ms": ms}
    return {"ok": True, "text": text, "model": model, "ms": ms,
            "tokens": tokens, "source": "remote" if use_remote else "local"}


@app.post("/v1/editor/fill")
def editor_fill(req: EditorFillReq):
    """编辑器里的自动填充：把光标位置交给**本地或远端**模型，返回要插入的代码。

    两条路自己选（`source`）：
      · local  —— 本地 Ollama。快、免费、离线可用，但只擅长短续写；
      · remote —— 远端（最强思考档）。慢、要钱，但能读懂"把这个函数改成 X 语义"这种意图。
    为什么只发光标附近而不是整个文件：本地默认 7B（CPU 上几十 token/s），整文件会拖到
    几分钟；远端虽然有 200k 上下文，但补全不需要把它塞满（白花钱）。
    这里**不写盘**：返回文本由前端插进编辑器，不满意 Ctrl+Z。
    """
    _guard("file.read")
    if not (req.before.strip() or req.selected.strip() or req.instruction.strip()):
        return {"ok": False, "reason": "光标前没有内容，也没写要求，没什么可生成的"}

    lang = _editor_lang(req.path)
    ctx = req.before[-6000:]
    tail = req.after[:800]
    if req.selected.strip():
        task = ("请**改写**下面这段选中的代码，只输出改写结果（不要重复解释，不要围栏）：\n"
                f"```{lang}\n{req.selected[:4000]}\n```")
    else:
        task = "请只输出**紧接着光标处**应当出现的代码。"
    extra = f"\n额外要求：{req.instruction.strip()}" if req.instruction.strip() else ""
    prompt = (
        "你是代码补全引擎，嵌在一个编辑器里。规则：\n"
        "1) 只输出要插入的代码本身，不要解释、不要 markdown 围栏、不要重复已有内容；\n"
        "2) 缩进必须和上文一致（用空格，不用 tab）；\n"
        "3) 不需要补全时输出空行即可，不要硬编。\n\n"
        f"文件：{req.path or '(未命名)'}　语言：{lang}\n"
        f"光标之前的代码：\n```{lang}\n{ctx}\n```\n"
        + (f"光标之后的代码（供衔接，禁止重复输出）：\n```{lang}\n{tail}\n```\n" if tail.strip() else "")
        + task + extra
    )
    r = _editor_complete(prompt, source=req.source, model=req.model,
                         node_id="editor_fill")
    if not r.get("ok"):
        return r
    return {"ok": True, "insertion": r["text"], "model": r["model"], "lang": lang,
            "source": r["source"], "ms": r["ms"], "tokens": r["tokens"]}


@app.post("/v1/editor/assist")
def editor_assist(req: EditorAssistReq):
    """选中即改：解释 / 改写 / 补测试。**不写盘**，结果交给前端做 diff 预览与接受/拒绝。

    这是「选中一段代码 → 一个动作」的入口，不要求用户先组织一句完整的话：
      · explain：讲清作用/输入输出/关键分支/边界与风险，不改代码；
      · edit：按 instruction 给最小改写，保持接口不变（除非明确要求改）；
      · test：生成 pytest 测试（正常/边界/异常），只输出测试代码。
    """
    _guard("file.read")
    action = str(req.action or "explain").strip().lower()
    if action not in ("explain", "edit", "test"):
        return {"ok": False, "reason": f"不认识的 assist 动作：{action}"}
    if not req.selected.strip():
        return {"ok": False, "reason": "先选中一段代码"}

    lang = _editor_lang(req.path)
    ctx = req.before[-6000:]
    tail = req.after[:800]
    code = req.selected[:6000]
    head = (f"文件：{req.path or '(未命名)'}　语言：{lang}\n"
            f"选区之前的代码：\n```{lang}\n{ctx}\n```\n"
            + (f"选区之后的代码：\n```{lang}\n{tail}\n```\n" if tail.strip() else "")
            + f"选中的代码：\n```{lang}\n{code}\n```\n")
    if action == "explain":
        prompt = ("你是资深程序员，正在给同事解释一段代码。用中文、按下面四段回答，"
                  "不要改代码、不要输出围栏：\n"
                  "1) 这段代码做什么；2) 输入与输出/副作用；3) 关键分支与边界；"
                  "4) 风险或可疑点（没有就说没有）。\n\n" + head)
    elif action == "edit":
        extra = f"\n用户要求：{req.instruction.strip()}" if req.instruction.strip() else ""
        prompt = ("你是代码编辑引擎。只输出**改写后的完整选区代码**，不要解释、不要围栏；"
                  "保持原有缩进与接口签名，除非用户明确要求改；不要输出选区之外的内容。"
                  + extra + "\n\n" + head)
    else:
        prompt = ("你是测试工程师。为选中的代码补 pytest 测试，只输出测试代码（不要解释、"
                  "不要围栏）。要求：覆盖正常路径、边界值和至少一个异常/失败路径；"
                  "按现有项目风格 import；测试名能看出意图。\n\n" + head)

    r = _editor_complete(prompt, source=req.source, model=req.model,
                         node_id="editor_assist")
    if not r.get("ok"):
        return r
    return {"ok": True, "action": action, "text": r["text"], "model": r["model"],
            "lang": lang, "source": r["source"], "ms": r["ms"], "tokens": r["tokens"]}


# ---------------------------------------------------------------- 精修模式：运行与调试

class CodeRunReq(BaseModel):
    repo: str
    path: str
    args: str = ""            # 命令行参数（按空白切分，不解析 shell 语法）
    timeout: int = 30


class DebugStartReq(BaseModel):
    repo: str
    path: str
    breakpoints: list[int] = []


class DebugCmdReq(BaseModel):
    id: str
    op: str                   # continue | step | next | break | unbreak | locals | quit
    line: int | None = None
    exprs: list[str] = []


class NavigateReq(BaseModel):
    repo: str
    text: str                 # 中文意图，如「打开 payment.py 的 refund 函数并调试」


class SymbolReq(BaseModel):
    repo: str
    path: str
    symbol: str


class DebugSymbolReq(BaseModel):
    repo: str
    path: str
    symbol: str


def _run_ws(repo: str) -> Path:
    """运行用的仓库根：复用文件接口的同一套牢笼（tools.safe_path 的父目录）。"""
    ws = _fs_base(repo)
    if not ws.is_dir():
        raise HTTPException(400, f"仓库不存在：{repo}")
    return ws


@app.get("/v1/editor/runner")
def editor_runner(repo: str, path: str):
    """当前文件用什么语言/工具链/工作目录运行（界面显示 + 开跑前预检）。"""
    _guard("test.run")
    ws = _run_ws(repo)
    built = _coderun.build_command(ws, path)
    # 工具链健康一览：只返回相关的两项，别把整张探测表塞给界面
    tools_map = _coderun.available_tools()
    return {"ok": built["ok"], "lang": built.get("lang"), "tool": built.get("tool"),
            "cmd": built.get("cmd"), "cwd": built.get("cwd"),
            "reason": built.get("reason"),
            "debug": built.get("debug", False),
            "tool_version": (tools_map.get(built.get("tool") or "") or {}).get("version"),
            "tool_path": (tools_map.get(built.get("tool") or "") or {}).get("path")}


@app.post("/v1/editor/run")
def editor_run(req: CodeRunReq):
    """真运行当前文件：捕获 stdout/stderr/退出码/耗时，诊断行可点跳。

    同步接口，但带 30s 超时（用户可调大），超时强杀进程树，不许卡住界面。
    """
    _guard("test.run")
    ws = _run_ws(req.repo)
    path = tools.safe_path(ws, req.path)
    rel = str(path.relative_to(ws)).replace("\\", "/")
    timeout = max(1, min(int(req.timeout or 30), 300))
    return _coderun.run_program(ws, rel, args=req.args, timeout=timeout)


@app.post("/v1/editor/debug/start")
def editor_debug_start(req: DebugStartReq):
    """起 Python 断点调试会话（其他语言明确拒绝，不假装能单步）。"""
    _guard("test.run")
    ws = _run_ws(req.repo)
    path = tools.safe_path(ws, req.path)
    rel = str(path.relative_to(ws)).replace("\\", "/")
    return _coderun.start_debug(ws, rel, breakpoints=req.breakpoints)


@app.post("/v1/editor/debug/cmd")
def editor_debug_cmd(req: DebugCmdReq):
    """给调试会话发一条命令（继续/单步/下断点/看变量）。"""
    _guard("test.run")
    s = _coderun.get_session(req.id)
    if not s:
        raise HTTPException(404, "调试会话不存在或已结束")
    kw: dict[str, Any] = {}
    if req.line is not None:
        kw["line"] = req.line
    if req.exprs:
        kw["exprs"] = req.exprs
    r = s.send(req.op, **kw)
    r["events"] = s.drain(200)
    r["status"] = s.status()
    return r


@app.get("/v1/editor/debug/events")
def editor_debug_events(id: str):
    """轮询调试事件（暂停/输出/异常/结束）——前端定时拉，界面实时更新。"""
    _guard("test.run")
    s = _coderun.get_session(id)
    if not s:
        raise HTTPException(404, "调试会话不存在或已结束")
    return {"events": s.drain(300), "status": s.status()}


@app.post("/v1/editor/debug/stop")
def editor_debug_stop(req: DebugCmdReq):
    """停止调试会话，杀掉子进程（不留僵尸）。"""
    _guard("test.run")
    return _coderun.stop_debug(req.id)


# ---------------------------------------------------------------- AI 导航 / 按符号调试
# 用户口径（2026-11-20）：让 AI 也能「跳到我想去的文件/函数」和「调试指定函数」。
# 触发两条：① 编辑器选中函数名点按钮 → /symbol/locate；
#           ② AI 对话里说中文 → /navigate（本地规则解析，不花 token，抠不出就返回候选）。

@app.get("/v1/editor/outline")
def editor_outline(repo: str, path: str):
    """当前文件的**符号大纲**（右栏"精修模式"用）：函数/类 + 行号，供点击跳转。

    与 `/v1/editor/symbols` 的区别：那个是**全仓按名字搜**（跨文件找定义）；
    这个是**单个文件内部的结构**（我已在这个文件里，想快速跳到某个函数）。
    两者都走 codelocate，判据只有一份。
    """
    _guard("file.read")
    ws = _run_ws(repo)
    from core import tools as _tools
    p = _tools.safe_path(ws, path)
    if not p.is_file():
        raise HTTPException(404, f"文件不存在：{path}")
    syms = _codelocate.symbols_in_file(p)
    total = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
    return {"ok": True, "path": path, "lines": total,
            "symbols": [{"name": s.get("name"), "kind": s.get("kind"),
                         "line": s.get("line"), "signature": s.get("signature") or ""}
                        for s in syms]}


@app.get("/v1/editor/symbols")
def editor_symbols(repo: str, q: str, kind: str | None = None, limit: int = 60):
    """全仓按符号名搜函数/类定义（AI 说「find_symbols 在哪」时用）。"""
    _guard("file.read")
    ws = _run_ws(repo)
    limit = max(1, min(int(limit or 60), 200))
    return _codelocate.find_symbols(ws, q, kind=kind, limit=limit)


@app.post("/v1/editor/symbol/locate")
def editor_symbol_locate(req: SymbolReq):
    """在当前文件里精确定位一个符号 → {path, line, name, kind, signature}。"""
    _guard("file.read")
    ws = _run_ws(repo=req.repo)
    target = _codelocate.locate(ws, req.path, req.symbol)
    if not target:
        return {"ok": False, "reason": f"没在 {req.path} 里找到符号「{req.symbol}」"}
    return {"ok": True, **target}


@app.post("/v1/editor/navigate")
def editor_navigate(req: NavigateReq):
    """解析中文意图并定位目标（本地规则，零 token）。

    返回 {action, targets, reason, ...}：
      · action=debug/navigate + targets 非空 → 前端直接打开文件、跳到行、按需启动调试；
      · action=search → targets 是全仓候选列表，前端展示让用户挑；
      · 抠不出目标 → action=none/空 targets + reason，交回代码填充，不瞎跳。
    """
    _guard("file.read")
    ws = _run_ws(req.repo)
    intent = _codelocate.parse_intent(req.text)
    action, symbol, file_hint = intent["action"], intent["symbol"], intent["file_hint"]
    if action == "none":
        return {"ok": False, "action": "none", "targets": [],
                "reason": "这句话没有导航/调试意图（说「打开 X 的 Y 函数」或「调试 Y」）"}

    targets: list[dict] = []
    reason = ""
    if action == "search" or not symbol:
        # 全仓搜（给了符号搜符号；只给了文件名就搜该文件的所有顶层符号）
        query = symbol or file_hint or req.text
        found = _codelocate.find_symbols(ws, query, limit=40)
        targets = found["hits"]
        if not targets:
            reason = f"没找到与「{query}」相关的函数/类"
        return {"ok": bool(targets), "action": "search" if action == "search" else "navigate",
                "targets": targets, "reason": reason, "files": found.get("files", 0)}

    # 有明确符号名：先单文件（给了 file_hint），否则全仓搜同名定义
    if file_hint:
        one = _codelocate.locate(ws, file_hint, symbol)
        if one:
            targets = [one]
    if not targets:
        found = _codelocate.find_symbols(ws, symbol, limit=40)
        targets = [h for h in found["hits"] if h["name"] == symbol or h["score"] >= 70]
        if file_hint:
            prefer = [h for h in targets if file_hint.split("/")[-1].lower() in h["path"].lower()]
            if prefer:
                targets = prefer
    if not targets:
        reason = f"没找到函数/类「{symbol}」" + (f"（在 {file_hint}）" if file_hint else "")
    return {"ok": bool(targets), "action": action, "targets": targets[:40], "reason": reason,
            "symbol": symbol, "file_hint": file_hint}


@app.post("/v1/editor/debug/symbol")
def editor_debug_symbol(req: DebugSymbolReq):
    """按符号调试：在该函数定义行自动下断点并启动 Python 调试会话。

    其他语言拒绝单步（和 debug/start 一致，不假装能单步）；走同一套 bdb 会话。
    """
    _guard("test.run")
    ws = _run_ws(req.repo)
    target = _codelocate.locate(ws, req.path, req.symbol)
    if not target:
        return {"ok": False, "reason": f"没在 {req.path} 里找到符号「{req.symbol}」"}
    built = _coderun.build_command(ws, target["path"])
    if not built.get("debug"):
        return {"ok": False, "reason": "断点调试只支持 Python；其他语言请用「运行」看诊断输出",
                "target": target}
    # 断点下在函数体首行（signature 行单独 step 会停在 def，body_line 才是第一条可执行语句）
    bp = target.get("body_line") or target["line"]
    r = _coderun.start_debug(ws, target["path"], breakpoints=[bp])
    return {**r, "target": target, "breakpoint": bp}


@app.get("/v1/fs/search")
def fs_search(repo: str, q: str, limit: int = 80, case: bool = False):
    """全仓关键词搜索（编辑器用）：返回命中文件+行号+整行文本。"""
    _guard("search.read")
    ws = _fs_base(repo)
    key = q if case else q.lower()
    if not key.strip():
        return {"q": q, "hits": [], "files": 0}
    root = ws.resolve()
    hits: list[dict] = []
    scanned = 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs
                         if d not in _SKIP_DIRS and not d.startswith("."))
        for f in sorted(files):
            p = Path(cur) / f
            try:
                if p.stat().st_size > 512 * 1024:
                    continue
                raw = p.read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            scanned += 1
            if "\x00" in raw[:2000]:
                continue
            for no, line in enumerate(raw.splitlines(), 1):
                hay = line if case else line.lower()
                if key in hay:
                    hits.append({"path": str(p.relative_to(root)).replace("\\", "/"),
                                 "line": no, "text": line[:240]})
                    if len(hits) >= max(1, limit):
                        return {"q": q, "hits": hits, "files": scanned, "truncated": True}
    return {"q": q, "hits": hits, "files": scanned, "truncated": False}


# ---------------------------------------------------------------- 夜班（脱离界面长跑）

# ---------------------------------------------------------------- 需求澄清协商（夜班模式的第一步）

class ClarifyReq(BaseModel):
    repo: str
    requirement: str = ""            # 首次必填；之后可空（沿用已存的需求）
    answers: dict[str, str] = {}     # {待定项 key: 用户选的选项}
    reset: bool = False              # 重新开始（丢掉已存的协商状态）
    effort: str = "max"              # 协商用最强档：这一步省钱最不划算


def _clarify_file(repo: Path) -> Path:
    return repo / ".sixcoder" / "clarify.json"


def _doc_to_json(doc) -> dict:
    return {"requirement": doc.requirement, "rounds": doc.rounds,
            "narrative": doc.narrative, "satisfied": doc.satisfied,
            "sufficient": doc.sufficient,
            "choices": [{"key": c.key, "question": c.question,
                         "options": list(c.options), "picked": c.picked}
                        for c in doc.choices]}


def _doc_from_json(d: dict):
    doc = _clarify.DesignDoc(requirement=str(d.get("requirement") or ""))
    doc.rounds = int(d.get("rounds") or 0)
    doc.narrative = str(d.get("narrative") or "")
    doc.satisfied = bool(d.get("satisfied"))
    doc.sufficient = bool(d.get("sufficient"))
    doc.choices = [_clarify.Choice(key=str(c.get("key") or ""),
                                   question=str(c.get("question") or ""),
                                   options=[str(x) for x in (c.get("options") or [])],
                                   picked=str(c.get("picked") or ""))
                   for c in (d.get("choices") or [])]
    return doc


@app.post("/v1/night/clarify")
def night_clarify(req: ClarifyReq):
    """需求澄清协商：远端先问清楚，确认"能做完"才允许开工。

    为什么必须有这一步（2026-11-20 用户口径）：造物模式的承诺是"提需求 → 早上看交付
    报告"，但技术栈/范围/验收口径没定时，远端只能自己猜 —— 猜错的代价是整夜的钱。
    所以动手前让它把待定项列成多选，人点完、它再自评"这些细节够不够开工"。

    设计成**一轮一次请求**（不是服务端阻塞跑完 8 轮）：
      · 用户看得见每轮问了什么、自己答了什么，能随时改主意；
      · 关掉界面再打开，状态从 `.sixcoder/clarify.json` 接着走；
      · 每轮最多 2 次远端调用（列问题 + 判够不够），待定项还在时不问"够不够"省一次。
    """
    _guard("agent.run")
    repo = Path(req.repo)
    if not repo.is_dir():
        raise HTTPException(400, f"仓库不存在：{req.repo}")
    text = (req.requirement or "").strip()
    cf = _clarify_file(repo)

    doc = None
    if not req.reset and cf.is_file():
        try:
            doc = _doc_from_json(json.loads(cf.read_text(encoding="utf-8")))
        except Exception:                                # noqa: BLE001
            doc = None
    if doc is None:
        if not text:
            raise HTTPException(400, "第一次协商要先给需求（requirement）")
        doc = _clarify.DesignDoc(requirement=text)
    elif text:
        doc.requirement = text

    t0 = time.time()
    try:
        if doc.rounds == 0:
            doc = _clarify.start(doc.requirement, _clarify.default_complete, req.effort)
        elif req.answers:
            doc = _clarify.apply_choices(doc, req.answers,
                                         _clarify.default_complete, req.effort)
        # 还有待定项 = 明显不够开工，不必再花一次远端调用问"够不够"
        if not doc.open_choices():
            doc.sufficient = _clarify.is_sufficient(doc, _clarify.default_complete, req.effort)
    except Exception as exc:                             # noqa: BLE001
        raise HTTPException(500, f"协商失败：{type(exc).__name__}: {exc}")

    ready = bool(doc.sufficient and not doc.open_choices())
    spec_path = ""
    try:
        cf.parent.mkdir(parents=True, exist_ok=True)
        cf.write_text(json.dumps(_doc_to_json(doc), ensure_ascii=False, indent=2),
                      encoding="utf-8")
        if ready:
            sp = cf.parent / "night_spec.md"
            sp.write_text("# 已确认的实现规格（协商结果，夜班按它开工）\n\n" + doc.render(),
                          encoding="utf-8")
            spec_path = str(sp)
    except OSError:
        pass

    return {"ok": True, "ready": ready, "round": doc.rounds,
            "requirement": doc.requirement,
            "narrative": doc.narrative,
            "questions": [{"key": c.key, "question": c.question, "options": c.options}
                          for c in doc.open_choices()],
            "answered": [{"key": c.key, "question": c.question, "picked": c.picked}
                         for c in doc.choices if c.picked],
            "render": doc.render(), "spec_path": spec_path,
            "ms": int((time.time() - t0) * 1000),
            "note": ("远端已确认：这些细节足以开工 → 可以点「开始夜班」" if ready
                     else "远端还有待定项：回答后继续（每轮约 30–90 秒）")}


@app.get("/v1/night/clarify/status")
def night_clarify_status(repo: str):
    """已存的协商进度（关掉界面再打开时接着看，不重复花钱）。"""
    cf = _clarify_file(Path(repo))
    if not cf.is_file():
        return {"ok": True, "exists": False}
    try:
        d = json.loads(cf.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {"ok": True, "exists": False}
    doc = _doc_from_json(d)
    return {"ok": True, "exists": True, "round": doc.rounds,
            "ready": bool(doc.sufficient and not doc.open_choices()),
            "requirement": doc.requirement, "narrative": doc.narrative,
            "questions": [{"key": c.key, "question": c.question, "options": c.options}
                          for c in doc.open_choices()],
            "answered": [{"key": c.key, "question": c.question, "picked": c.picked}
                         for c in doc.choices if c.picked],
            "render": doc.render()}

def _pythonw_exe() -> str:
    """同目录的 pythonw.exe（无控制台窗口）；没有就退回当前解释器。"""
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.is_file() else exe)


def _spawn_reparented(exe: str, args: list[str], cwd: str,
                      env_extra: dict[str, str] | None = None,
                      pid_file: Path | None = None) -> int:
    """起一个**不在本进程树下**的进程，返回 PID（Windows）。

    为什么不能用裸 subprocess.Popen（2026-11-21 实测）：安装器升级时会
    `taskkill /F /T /IM SixCoder.exe`，/T 顺着父子关系杀整棵树 —— 后端是
    SixCoder 的子进程、看门狗又是后端的子进程，于是"关窗也能跑"的夜班在装新版时
    被一起杀掉，用户看到的就是"明明在跑，装完就没了"。

    做法：`cmd /c start "" /b <exe> ...` —— cmd 启动完立刻退出，子进程的父进程
    随即消失，不再挂在应用进程树里；安装器/关窗的 /T 都追不到它。
    PID 由被拉起的进程自己写（看门狗写 watchdog.pid），这里等一小会儿读回来。
    """
    env = dict(os.environ)
    env.update({k: str(v) for k, v in (env_extra or {}).items()})
    if sys.platform != "win32":
        proc = subprocess.Popen([exe, *args], cwd=cwd, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return proc.pid
    cmdline = subprocess.list2cmdline([exe, *args])
    comspec = os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe")
    base = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    # 能脱离 Job 就脱离：上层（安装器/桌面外壳/测试宿主）常用 job 对象做"关掉就全杀"，
    # 不 breakaway 的话夜班虽然换了父进程，仍会随 job 关闭被带走。
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    for flags in (base | breakaway, base):
        try:
            subprocess.Popen(f'"{comspec}" /c start "" /b {cmdline}', cwd=cwd, env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags)
            break
        except OSError:
            if flags == base:
                raise
    if pid_file:
        for _ in range(30):
            time.sleep(0.1)
            try:
                pid = int((pid_file.read_text(encoding="utf-8") or "").strip() or 0)
                if pid and _pid_alive(pid):
                    return pid
            except (OSError, ValueError):
                pass
    return 0


def _pid_alive(pid: int) -> bool:
    """进程还活着吗。**绝不能用 os.kill**（在 Windows 上那是真杀进程）。"""
    try:
        pid = int(pid or 0)
    except Exception:                                    # noqa: BLE001
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        if not psutil.pid_exists(pid):
            return False
        p = psutil.Process(pid)
        return p.is_running()
    except Exception:                                    # noqa: BLE001
        return False


def _night_procs(repo: str) -> list[dict]:
    """当前在跑 `cli.py night` 的进程（用来防双开 / 显示状态）。"""
    out: list[dict] = []
    try:
        import psutil
    except Exception:                                    # noqa: BLE001
        return out
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:                                # noqa: BLE001
            continue
        if "cli.py" in cmd and "night" in cmd:
            if not repo or repo.replace("/", "\\").lower() in cmd.replace("/", "\\").lower():
                out.append({"pid": p.info.get("pid"), "cmd": cmd[-180:]})
    return out


def _resume_interrupted_nights() -> None:
    """上次夜班被中断（装新版本时安装器会清安装目录下的 python）就自动续跑。

    为什么要有：安装器升级前必须杀掉 `$INSTDIR` 下的所有 python（否则文件被占用，
    改名/覆盖会失败），夜班跟着死。看门狗在开跑时写 `night_<仓库标签>.want`，
    正常收工/用户主动停会删掉它；后端启动看到标记、又确认没有夜班在跑，
    就按原参数重新拉起 —— 不用用户再点一次，也不至于白丢一夜。
    """
    if not _paths.is_packaged():
        return
    seen: set[str] = set()
    for f in _paths.logs_dir().glob("night_*.want"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            repo = str(d.get("repo") or "")
            key = repo.replace("\\", "/").lower()
            if key and key in seen:
                f.unlink()                    # 同一仓库的重复标记（旧版标签不一致留下的）
                continue
            seen.add(key)
            args = [str(a) for a in (d.get("args") or [])]
            ts = float(d.get("ts") or 0)
            if not repo or not args or time.time() - ts > 12 * 3600:
                f.unlink()
                continue
            if _night_procs(repo):
                continue                          # 还在跑，不重复起
            _spawn_reparented(_pythonw_exe(), args, str(ROOT),
                              {"SIXCODER_REPO": repo, "SIXCODER_HOME": str(_paths.home())},
                              pid_file=_paths.logs_dir() / "watchdog.pid")
            print(f"[SixCoder] 检测到夜班被中断，已自动续跑：{repo}", flush=True)
        except Exception as exc:                             # noqa: BLE001
            print(f"[SixCoder] 续跑跳过 {f.name}：{type(exc).__name__}: {exc}", flush=True)


class DetachedNightReq(BaseModel):
    repo: str
    requirement: str = ""          # 甲方需求（写进 .sixcoder/night_requirement.txt）
    goal: str = ""                 # 全自主目标（写进 .sixcoder/night_goal.txt）    fresh: bool = False


@app.post("/v1/night/detached")
def create_night_detached(req: DetachedNightReq):
    """**以脱离界面的方式**起夜班：spawn 独立 watchdog 进程。

    为什么必须有（2026-11-20 用户口径："点开始夜班就跑一整夜"）：
    原来的 `/v1/night` 在 server.py 里起线程跑，而 `shell/main.cjs` 在关窗时
    会 `backend.kill()` —— 一关界面夜班就死，而且没有看门狗兜底。这里改为：
      · `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`：不继承本进程生命期
      · 由 `tools/watchdog_night.py` 托管：崩溃/停机自动重启（上限 20 次）
      · 输出重定向到文件（**不用 PIPE**：缓冲写满会死锁，本项目踩过这个坑）
    进度不走 SSE，改由 `/v1/night/detached/status` 从磁盘读（关掉界面也能看）。
    """
    _guard("agent.run")
    pol = policy.current()
    if pol.code_escalation is policy.OFF:
        raise HTTPException(403, f"{pol.name}不写文件，不能跑夜班；请切到造物模式或精修模式")
    repo = Path(req.repo)
    if not repo.is_dir():
        raise HTTPException(400, f"仓库不存在：{req.repo}")

    # 防双开：已有 cli.py night 在跑就别再起一个（两个进程会同写一个产物仓）
    running = _night_procs(str(repo))
    if running:
        return {"ok": True, "already": True, "procs": running,
                "note": "已有夜班在跑（同一仓库），没有重复启动",
                "status": "/v1/night/detached/status?repo=%s" % req.repo}

    log_dir = _paths.logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    # 用户任务文本要真的被用上：写进仓库 .sixcoder/ 下的目标文件，再把路径传下去
    # （watchdog 只认 --goal-file / --requirement-file；不传就退回 REQUIREMENTS.md）
    goal_args: list[str] = []
    goal_src = ""
    sd = repo / ".sixcoder"
    try:
        sd.mkdir(parents=True, exist_ok=True)
        if (req.goal or "").strip():
            gf = sd / "night_goal.txt"
            gf.write_text(req.goal.strip(), encoding="utf-8")
            goal_args = ["--goal-file", str(gf)]
            goal_src = "goal"
        elif (req.requirement or "").strip():
            rf = sd / "night_requirement.txt"
            rf.write_text(req.requirement.strip(), encoding="utf-8")
            goal_args = ["--requirement-file", str(rf)]
            goal_src = "requirement"
    except OSError as exc:
        raise HTTPException(500, f"写目标文件失败：{exc}")
    cmd = ([_pythonw_exe(), "-u", "tools/watchdog_night.py", "--now", "--repo", str(repo)]
           + goal_args)
    env = dict(os.environ)
    env["SIXCODER_REPO"] = str(repo)     # 巡检没有 --repo 参数，靠 env 知道盯哪个仓库
    env["SIXCODER_HOME"] = str(_paths.home())
    try:
        wd_pid = _spawn_reparented(_pythonw_exe(), cmd[1:], str(ROOT), env,
                                   pid_file=log_dir / "watchdog.pid")
    except Exception as exc:                             # noqa: BLE001
        raise HTTPException(500, f"起夜班失败：{type(exc).__name__}: {exc}")

    # 巡检（产物质量那一层）：删掉 START_NIGHT.bat 之后这里必须补回来，否则
    # "编译/冒烟/覆盖回退/烧钱速率"没人看 —— 看门狗只管进程活着，看不见产物坏了。
    # 它自带单实例守卫（pid 文件里的还活着就自己退出），重复起是安全的。
    patrol = {"started": False}
    try:
        pp_pid = _spawn_reparented(_pythonw_exe(), ["-u", "tools/patrol.py", "--persist"],
                                   str(ROOT), env)
        patrol = {"started": True, "pid": pp_pid,
                  "log": str(log_dir / "patrol_detached.out")}
    except Exception as exc:                             # noqa: BLE001
        patrol = {"started": False, "why": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "already": False, "pid": wd_pid,
            "cmd": " ".join(cmd), "log": str(log_dir / "watchdog.log"),
            "goal_source": goal_src or "REQUIREMENTS.md",
            "watchdog_log": str(log_dir / "watchdog.log"),
            "patrol": patrol,
            "note": "已脱离界面启动：关掉这个窗口、装新版本都不会中断（进程不在应用进程树里）；"
                    "崩溃由看门狗重启，巡检并行盯着产物质量",
            "status": "/v1/night/detached/status?repo=%s" % req.repo}


class PackReq(BaseModel):
    repo: str


def _pack_procs(repo: str) -> list[dict]:
    out: list[dict] = []
    try:
        import psutil
    except Exception:                                    # noqa: BLE001
        return out
    for p in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
        except Exception:                                # noqa: BLE001
            continue
        if "core.exepack" in cmd and repo.lower() in cmd.lower():
            out.append({"pid": p.info.get("pid")})
    return out


@app.post("/v1/night/package")
def night_package(req: PackReq):
    """把产物仓打成 exe（造物模式交付的最后一步）。

    为什么单开一条 detached 路径：PyInstaller 要 1-3 分钟，塞进请求线程会占住后端，
    关窗还会被杀；按既有的「独立进程 + 状态文件轮询」模型走。
    """
    _guard("agent.run")
    repo = Path(req.repo)
    if not repo.is_dir():
        raise HTTPException(400, f"仓库不存在：{req.repo}")
    from core import exepack as _exepack
    if _pack_procs(str(repo)):
        return {"ok": True, "already": True, "note": "已经在打包了，等它跑完"}
    info = _exepack.detect(repo)
    if not info.get("exists"):
        return {"ok": False, "reason": "仓库里没有可打包的入口（app/main.py / main.py / cli.py…）",
                "entry": "", "kind": "none"}
    logs = _paths.logs_dir()
    try:
        (logs / f"pack_{repo.name}.log").unlink()
    except OSError:
        pass
    try:
        pid = _spawn_reparented(sys.executable,
                                ["-u", "-m", "core.exepack", "--repo", str(repo),
                                 "--logs", str(logs)],
                                str(ROOT),
                                {"SIXCODER_HOME": str(_paths.home())})
    except Exception as exc:                             # noqa: BLE001
        raise HTTPException(500, f"起打包进程失败：{type(exc).__name__}: {exc}")
    return {"ok": True, "already": False, "pid": pid, "entry": info["entry"],
            "kind": info["kind"],
            "note": "已开始打包（1-3 分钟）：装 PyInstaller → 编译 → dist/<名字>.exe"}


@app.get("/v1/night/package/status")
def night_package_status(repo: str):
    from core import exepack as _exepack
    ws = Path(repo)
    st = _exepack.read_status(_paths.logs_dir(), ws)
    tail = ""
    for cand in (_paths.logs_dir() / f"pack_{ws.name}.log",
                 Path(st.get("log") or "")):
        try:
            if cand.is_file():
                tail = "\n".join(cand.read_text(encoding="utf-8", errors="replace")
                                 .splitlines()[-25:])
                break
        except OSError:
            continue
    return {"ok": True, **st, "log_tail": tail,
            "running": bool(_pack_procs(str(ws)))}


@app.get("/v1/night/detached/log")
def night_detached_log(repo: str, offset: int = 0):
    """后台夜班的**增量日志**：界面用它把"执行时间线"喂起来。

    为什么需要（2026-11-21 用户报"执行时间线不显示远端的回复了"）：
    夜班默认走后台长跑（detached），不发 SSE；时间线原来只由
    `/v1/tasks/{id}/stream` 填充，这条路在 detached 下根本不存在，于是整晚空白。
    这里按字节 offset 只回新行，前端 2 秒轮询，既不重复也不漏。
    """
    ws = Path(repo)
    lg = _paths.night_log(str(ws))
    if not lg or not lg.is_file():
        return {"ok": True, "lines": [], "offset": offset, "running": False, "log": ""}
    try:
        size = lg.stat().st_size
        if offset < 0 or offset > size:
            offset = 0                       # 换了一夜（日志被替换/截断）→ 从头读
        with lg.open("rb") as f:
            f.seek(offset)
            raw = f.read(400_000)
    except OSError as exc:                                   # noqa: BLE001
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}",
                "lines": [], "offset": offset, "running": False, "log": str(lg)}
    parts = raw.split(b"\n")
    if parts and not raw.endswith(b"\n"):
        parts.pop()                          # 最后半行不返回，等下次写完再读
    consumed = sum(len(p) + 1 for p in parts)
    lines = [p.decode("utf-8", "replace").rstrip("\r") for p in parts if p.strip()]
    return {"ok": True, "lines": lines[-400:], "offset": offset + consumed,
            "running": bool(_night_procs(str(ws))), "log": str(lg)}


@app.get("/v1/night/detached/status")
def night_detached_status(repo: str):
    """脱离模式的状态：看门狗/夜班进程 + 端到端进度 + 预算 + 日志尾巴。

    全部从磁盘读（coverage.jsonl / budget.json / watchdog.log），不依赖 SSE ——
    这样关掉界面再打开也能看到刚才那一夜跑到哪了。
    """
    ws = Path(repo)
    pid_file = _paths.logs_dir() / "watchdog.pid"
    wd_pid = 0
    try:
        wd_pid = int((pid_file.read_text(encoding="utf-8") or "").strip() or 0)
    except Exception:                                    # noqa: BLE001
        wd_pid = 0
    procs = _night_procs(str(ws))
    cov: dict[str, Any] = {}
    try:
        lines = [ln for ln in (ws / ".sixcoder" / "coverage.jsonl")
                 .read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if lines:
            last = json.loads(lines[-1])
            st = last.get("statuses") or {}
            if st:
                from collections import Counter
                c = Counter(st.values())
                cov = {"done": c.get("done", 0), "absent": c.get("absent", 0),
                       "descoped": c.get("descoped", 0), "total": len(st),
                       "pct": round(100.0 * c.get("done", 0) / len(st), 1)}
            cov["ts"] = last.get("ts") or 0
    except Exception:                                    # noqa: BLE001
        cov = {}
    budget: dict[str, Any] = {}
    try:
        snap = _be.budget_snapshot(str(ws))
        spent = _be.project_spent_cny(str(ws))
        budget = {"spent": round(spent, 2),
                  "threshold": snap.get("stop_threshold_cny"),
                  "remaining": round((snap.get("stop_threshold_cny") or 0) - spent, 2),
                  "stop_level": snap.get("stop_level")}
    except Exception:                                    # noqa: BLE001
        budget = {}
    tail = ""
    try:
        lg = (_paths.logs_dir() / "watchdog.log")
        raw = lg.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(raw[-12:])
    except Exception:                                    # noqa: BLE001
        tail = ""
    # 夜班自己的日志（cli.py night 每轮一个 night_*.log）：看门狗日志只说"第几次启动"，
    # 真正"跑到哪一步"在这个文件里。界面重开时没有 SSE，只能靠它显示进度。
    night_tail = ""
    try:
        lg = _paths.night_log(str(ws))
        if lg:
            raw = lg.read_text(encoding="utf-8", errors="replace").splitlines()
            night_tail = "\n".join(raw[-30:])
    except Exception:                                    # noqa: BLE001
        night_tail = ""
    # 最近产物：让用户不用翻目录就知道"它到底写了没有"
    recent: list[dict[str, Any]] = []
    try:
        now = time.time()
        for p in ws.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ws).as_posix()
            if rel.startswith((".git/", ".sixcoder/")) or "__pycache__" in rel:
                continue
            mt = p.stat().st_mtime
            if now - mt <= 3600:
                recent.append({"path": rel, "size": p.stat().st_size,
                               "mtime": time.strftime("%H:%M:%S", time.localtime(mt))})
        recent.sort(key=lambda x: x["mtime"], reverse=True)
        recent = recent[:12]
    except Exception:                                    # noqa: BLE001
        recent = []
    return {"repo": str(ws),
            "watchdog": {"pid": wd_pid, "alive": _pid_alive(wd_pid)},
            "night": {"running": bool(procs), "procs": procs},
            "coverage": cov, "budget": budget, "log_tail": tail,
            "night_log_tail": night_tail, "recent_files": recent}


class StopNightReq(BaseModel):
    repo: str = ""            # 空 = 停掉所有本引擎的夜班进程

def _kill_tree(proc) -> list[int]:
    """杀掉一个进程连同它的子进程（先子后父），返回被杀的 pid。

    为什么要连子一起杀：`cli.py night` 是 watchdog 的子进程，只杀父进程会留下
    一个"孤儿夜班"继续写产物仓 —— 用户以为已经停了，实际还在花钱。
    """
    killed: list[int] = []
    try:
        kids = proc.children(recursive=True)
    except Exception:                                    # noqa: BLE001
        kids = []
    for k in kids:
        try:
            k.kill()
            killed.append(k.pid)
        except Exception:                                # noqa: BLE001
            pass
    try:
        proc.kill()
        killed.append(proc.pid)
    except Exception:                                    # noqa: BLE001
        pass
    return killed


def _engine_proc_kind(cmd: str) -> str:
    """cmdline → watchdog / patrol / night / ''（只认本引擎自己的进程名）。"""
    c = cmd.replace("\\", "/")
    if "watchdog_night.py" in c:
        return "watchdog"
    if "patrol.py" in c:
        return "patrol"
    if "cli.py" in c and "night" in c:
        return "night"
    return ""


@app.post("/v1/night/stop")
def stop_night(req: StopNightReq):
    """停夜班（替代 STOP_NIGHT.bat，2026-11-20 用户口径："功能全进源码"）。

    顺序是硬约束：**先 watchdog → 再 patrol → 最后 cli.py night**。
    watchdog 会重启夜班，先杀它才不会出现"杀了又自己起来"。
    安全性：只杀 (a) 本引擎 pid 文件里记录的进程树，或 (b) cmdline 命中本引擎脚本
    且 cwd 在引擎目录内的进程。用户自己的其它 python 项目绝不动。
    """
    _guard("agent.run")
    want_repo = (req.repo or "").replace("/", "\\").lower()
    killed: list[dict] = []
    notes: list[str] = []

    # 1) pid 文件（watchdog / patrol）→ 连子树一起杀
    try:
        import psutil
    except Exception as exc:                             # noqa: BLE001
        raise HTTPException(500, f"缺少 psutil，无法安全停夜班：{exc}")
    order = ("watchdog.pid", "patrol.pid", "ops.pid")
    for name in order:
        pf = _paths.logs_dir() / name
        pid = 0
        try:
            pid = int((pf.read_text(encoding="utf-8") or "").strip() or 0)
        except Exception:                                # noqa: BLE001
            pid = 0
        if not pid:
            continue
        try:
            p = psutil.Process(pid)
            got = _kill_tree(p)
            killed.extend({"pid": x, "from": name} for x in got)
        except Exception:                                # noqa: BLE001
            pass
        try:
            pf.unlink()
        except OSError:
            pass

    # 2) 兜底扫描：cmdline 命中 + cwd 在引擎目录内（拿不到 cwd 就保守跳过，绝不误杀）
    engine_dir = str(ROOT).replace("\\", "/").lower()
    try:
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
            except Exception:                            # noqa: BLE001
                continue
            kind = _engine_proc_kind(cmd)
            if not kind or p.pid == os.getpid():
                continue
            if want_repo and kind == "night" and want_repo not in cmd.replace("/", "\\").lower():
                continue                                 # 指定了仓库 → 只停这个仓库的夜班
            inside = False
            try:
                inside = str(p.cwd()).replace("\\", "/").lower().startswith(engine_dir)
            except Exception:                            # noqa: BLE001
                inside = engine_dir in cmd.replace("\\", "/").lower()
            if not inside:
                notes.append(f"跳过 pid={p.pid}（不在本引擎目录，避免误杀）")
                continue
            try:
                got = _kill_tree(p)
                killed.extend({"pid": x, "from": kind} for x in got)
            except Exception:                            # noqa: BLE001
                pass
    except Exception as exc:                             # noqa: BLE001
        notes.append(f"扫描进程失败：{type(exc).__name__}: {exc}")

    # 3) 清掉 ops 状态，避免界面一直显示"运行中"
    try:
        (_paths.logs_dir() / "ops.json").unlink()
    except OSError:
        pass
    # 4) 用户主动停 = 这一夜收工：删掉"被中断就续跑"的标记，别下次启动又自己跑起来
    for f in _paths.logs_dir().glob("night_*.want"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if not want_repo or want_repo in str(d.get("repo") or "").replace("/", "\\").lower():
            try:
                f.unlink()
            except OSError:
                pass
    return {"ok": True, "killed": killed, "count": len(killed), "notes": notes,
            "note": "已停止夜班与运维任务" if killed else "没有在跑的夜班进程"}


# ---------------------------------------------------------------- 运维任务（原 START_NIGHT.bat 的 full/report/contract）

class OpsReq(BaseModel):
    repo: str
    op: str = "report"        # report（只检测，零 token）/ repair（检测+修）/ contract（生成全项目契约）


_OPS_DEFS = {
    "report": {"args": [], "desc": "检测并出错误报告（零 token，不改代码）"},
    "repair": {"args": ["--repair"], "desc": "检测 + 只改有错处（会花 token）"},
    "contract": {"args": ["--gen-contract-only"], "desc": "生成全项目契约（慢，可续跑）"},
}


def _ops_state_file():
    return _paths.logs_dir() / "ops.json"


def _read_ops_state() -> dict:
    try:
        return json.loads(_ops_state_file().read_text(encoding="utf-8")) or {}
    except Exception:                                    # noqa: BLE001
        return {}


@app.post("/v1/ops/start")
def ops_start(req: OpsReq):
    """起一个运维任务（脱离界面跑，日志落文件，界面轮询 status）。

    为什么脱离：`full --repair` 实测 10-15 分钟、契约生成 1-3 分钟/域，
    在请求线程里跑会占住 SSE 且关窗即死 —— 和夜班踩的是同一个坑。
    """
    _guard("agent.run")
    op = (req.op or "").strip().lower()
    if op not in _OPS_DEFS:
        raise HTTPException(400, f"未知运维任务：{req.op}（可选 {', '.join(_OPS_DEFS)}）")
    repo = Path(req.repo)
    if not repo.is_dir():
        raise HTTPException(400, f"仓库不存在：{req.repo}")

    st = _read_ops_state()
    if st and _pid_alive(int(st.get("pid") or 0)):
        return {"ok": True, "already": True, "op": st.get("op"), "pid": st.get("pid"),
                "note": "已有运维任务在跑，等它结束再起下一个"}

    out_path = _paths.logs_dir() / f"ops_{op}.out"
    cmd = ([_pythonw_exe(), "-u", "cli.py", "full", "--repo", str(repo)]
           + _OPS_DEFS[op]["args"])
    flags = 0
    if sys.platform == "win32":
        flags = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                 | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    try:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(f"# {op} @ {repo}\n# {' '.join(cmd)}\n")
            fh.flush()
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=fh,
                                    stderr=subprocess.STDOUT, creationflags=flags)
    except Exception as exc:                             # noqa: BLE001
        raise HTTPException(500, f"起运维任务失败：{type(exc).__name__}: {exc}")
    state = {"op": op, "repo": str(repo), "pid": proc.pid, "log": str(out_path),
             "started": int(time.time()), "desc": _OPS_DEFS[op]["desc"]}
    try:
        _ops_state_file().write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        (_paths.logs_dir() / "ops.pid").write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    return {"ok": True, "already": False, **state}


@app.get("/v1/ops/status")
def ops_status():
    """运维任务状态：是否在跑、跑了多久、日志尾巴（全部从磁盘读，关窗也能看）。"""
    st = _read_ops_state()
    if not st:
        return {"running": False, "op": "", "log_tail": ""}
    pid = int(st.get("pid") or 0)
    alive = _pid_alive(pid)
    tail = ""
    try:
        raw = Path(st.get("log", "")).read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(raw[-40:])
    except Exception:                                    # noqa: BLE001
        tail = ""
    elapsed = 0
    if alive:
        elapsed = max(0, int(time.time()) - int(st.get("started") or 0))
    return {"running": alive, "pid": pid, "op": st.get("op"), "repo": st.get("repo"),
            "desc": st.get("desc"), "elapsed_s": elapsed, "log": st.get("log"),
            "log_tail": tail}


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
    """待人回答的问题（精修模式里本地模型主动提问）。"""
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
    rows = db.query("SELECT id,repo,workspace,state FROM tasks")
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
    # 引擎本地临时区归档：只清**确实删干净了的**那些仓库（还在跑的要留着备用）
    purged = 0
    try:
        from core import scratch_store as _scratch
        done_repos = {r["repo"] for r in rows if r["id"] not in res["kept_running"]
                      and r.get("repo")}
        for repo in done_repos:
            if (_scratch.purge_repo(repo) or {}).get("purged"):
                purged += 1
    except BaseException:                 # noqa: BLE001
        pass
    return {"ok": True, "deleted_count": len(res["deleted"]), "rows": res["counts"],
            "kept_running": res["kept_running"], "workspace_removed": removed,
            "scratch_purged_repos": purged}


class NodeResolveReq(BaseModel):
    action: str          # accept | drop
    reason: str = ""


@app.post("/v1/tasks/{task_id}/nodes/{node_id}/resolve")
def resolve_node(task_id: str, node_id: str, req: NodeResolveReq):
    """人工处置一个 stuck（有节点没通过）节点：接受 / 丢弃（都写 human 证据）。

    与"等人工审批"（精修模式的 needhuman）不是一回事：
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
    """删除历史任务。默认只删记录；with_workspace=true 时连带删除工作区副本。

    另外**一定**会清掉该仓库在引擎本地的临时区归档
    （`prototype/data/scratch/<指纹>/`，2026-11-19 用户口径：删记录就顺手删备份）。
    """
    _guard("agent.run")
    rows = db.query("SELECT id,repo,workspace,state FROM tasks WHERE id=?", (task_id,))
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
    # 引擎本地该仓库的临时区归档：记录都没了就没人看，一并清掉（不留暗账）
    purged = {}
    try:
        from core import scratch_store as _scratch
        purged = _scratch.purge_repo(rows[0]["repo"])
    except BaseException:                 # noqa: BLE001
        purged = {"purged": False, "error": "purge 跳过"}
    return {"ok": True, "deleted": task_id, "rows": counts,
            "workspace_removed": removed, "scratch_purged": purged}


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
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    db.init()
    stale = db.recover_stale()
    try:
        ensure_demo()
    except Exception as exc:                             # noqa: BLE001
        # 演示仓库只是"示例入口"，初始化失败（没 Git / 目录只读 / 杀软）不许
        # 阻断整个后端；否则用户只会看到"后端未就绪"，真正原因被埋掉。
        print(f"[SixCoder] demo 初始化跳过：{type(exc).__name__}: {exc}", flush=True)
    # 装新版本会把夜班杀掉（安装器要清安装目录下的 python）：开后端先看有没有
    # "被中断还没收工"的标记，有就原地续跑，别等用户重新点一次。
    try:
        _resume_interrupted_nights()
    except Exception as exc:                             # noqa: BLE001
        print(f"[SixCoder] 续跑检查跳过：{type(exc).__name__}: {exc}", flush=True)
    import uvicorn
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8760
    print(f"[SixCoder] http://127.0.0.1:{port}   前端构建产物：{WEB_DIST.exists()}   清理僵尸任务：{stale}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
