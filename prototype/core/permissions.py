"""三种模式：甲方模式 / 开发者模式 / 只读模式。

这是**授权档位**，决定三件事：
1. 能用哪些能力（caps）；
2. 关键节点要不要**人来点头**（auto_approve / 审批门，见 core/approval.py）；
3. 文件写到哪（永远只在工作区内，越界由 core/sandbox.py 直接拒绝）。

三个模式（名字固定，不要改）：

- **甲方模式**（client）：甲方只提需求，不关心实现。人工处置全部放行（auto_approve），
  从 0 直接做到 1（空仓库也能一路建成可交付的成品），夜里跑完早上看报告。
  **边界：只能改工作区里的文件，越界一律拒绝**（沙箱牢笼）。

- **开发者模式**（developer）：给开发者用，做细节化版本控制和代码写作。
  **关键节点必须人工审核，不会自动放行**——每个节点抛 `needhuman` 事件并阻塞等待，
  人点"通过"才继续，点"拒绝"或超时就中止该节点。

- **只读模式**（readonly）：只让系统读代码，没有任何写权限。用于**最后成品的意图了解与改进分析**
  （读文件、检索、静态审查、语义比对），不写、不跑、不提交。

设计原则（docs/13 判定权分离）：
- 权限跟着「当前模式」走，不跟着代码路径走；
- 每个能力都在服务端与工具层被 require() 卡住；
- 拒绝时给可读原因 + 该切哪个模式，而不是一句 403。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------- capability

CAPS: dict[str, str] = {
    "probe.read":   "探测仓库形态 / 工具链",
    "file.read":    "读取仓库文件",
    "search.read":  "代码检索 / 符号大纲",
    "file.write":   "写入或修改文件（仅工作区内，自动备份）",
    "exec.run":     "执行命令（白名单 + 超时）",
    "test.run":     "运行构建与测试",
    "git.read":     "查看状态 / 历史 / 差异 / 分支",
    "git.commit":   "本地提交与撤销",
    "git.branch":   "新建 / 切换分支",
    "git.remote":   "远端同步（fetch / pull / push / merge / 设置远端）",
    "agent.run":    "让 Agent 自动改代码",
    "review.run":   "静态代码审查（改进意见）",
    "review.model": "审查时调用模型做语义比对",
    "toolchain.config": "修改工具链配置（指定 JDK 等目录）",
    "deploy.exec": "在预留的服务器上执行白名单命令（独立通道，强制人审）",
}

# 关键节点：开发者模式下这些动作**必须有人点头**
KEY_NODES: dict[str, str] = {
    "file.write":   "即将写入/修改工作区文件",
    "git.commit":   "即将创建 git 提交（版本控制关键点）",
    "git.push":     "即将推送/合并到远端",
    "remote.call":  "即将把代码发往远端模型（DeepSeek）",
    "deliver":      "即将把成果标记为可交付（收尾）",
    # 2026-11-23 部署通道：外联生产服务器是高危操作，**任何模式都强制人审**
    # （approval.request 的 force=True，绕过甲方 auto_approve）
    "deploy.exec":  "即将 ssh 到预留服务器执行命令（外联高危，强制人审）",
}


@dataclass(frozen=True)
class Mode:
    id: str
    name: str
    desc: str
    caps: frozenset[str]
    sandbox: str                      # readonly | write —— 传给 Sandbox
    color: str
    auto_approve: bool                # True=人工处置全部放行；False=关键节点必须人审
    key_nodes: tuple[str, ...] = ()   # 需要人审的节点（仅 auto_approve=False 时生效）
    boundary: str = ""                # 一句话边界说明（界面直接显示）
    extras: tuple[str, ...] = field(default=())   # 界面上的补充说明


_READ = {"probe.read", "file.read", "search.read", "git.read", "review.run", "review.model"}
_WRITE = {"file.write", "exec.run", "test.run", "git.commit", "git.branch", "agent.run",
          "toolchain.config"}
# 部署通道：client/developer 可用，但**强制人审**（force=True 绕过 auto_approve）；
# readonly 不开放。单独一组，别混进 _WRITE 让语义变糊。
_DEPLOY = {"deploy.exec"}

MODES: dict[str, Mode] = {
    "client": Mode(
        id="client", name="甲方模式", color="b-ok", sandbox="write",
        desc="甲方只提需求，不看实现。全部人工处置自动放行，从 0 做到 1，夜里跑完早上看交付报告。",
        caps=frozenset(_READ | _WRITE | _DEPLOY | {"git.remote"}),
        auto_approve=True,
        boundary="只能改工作区内的文件，越界一律拒绝；部署需预留目标且每次强制人审",
        extras=("需求一句话即可，不需要知道具体实现",
                "整条流水线自动跑：体检→工作单→开发→机械验收→交付报告",
                "出问题看 DELIVERY.md 与 TRACE.md，不用读代码"),
    ),
    "developer": Mode(
        id="developer", name="开发者模式", color="b-acc", sandbox="write",
        desc="给开发者用：细节化版本控制与代码写作。关键节点必须人工审核，不会自动放行。",
        caps=frozenset(_READ | _WRITE | _DEPLOY | {"git.remote"}),
        auto_approve=False,
        key_nodes=("file.write", "git.commit", "git.push", "remote.call", "deliver",
                   "deploy.exec"),
        boundary="只能改工作区内的文件；关键节点抛 needhuman，等人点通过才继续",
        extras=("关键节点：写文件 / 提交 / 推送 / 上报远端 / 收尾交付",
                "每次审批都记审计日志（.sixcoder/audit.jsonl）",
                "可以在审批时拒绝，该节点会中止并留下证据"),
    ),
    "readonly": Mode(
        id="readonly", name="只读模式", color="b-dim", sandbox="readonly",
        desc="只读：读取代码、检索、静态审查、语义比对。没有写权限，用于成品意图了解与改进分析。",
        caps=frozenset(_READ),
        auto_approve=True,           # 没有写操作，也就没有要批的事
        boundary="任何写入/执行/提交/推送都被拒绝（沙箱 readonly）",
        extras=("适合：接手别人的交付物时，先读懂它想干什么",
                "可以跑静态审查与模型语义比对，得到改进建议",
                "不会产生任何文件改动，也不会动 git"),
    ),
}

# 旧 id → 新 id：保证老配置/命令/书签不会突然失效
ALIASES = {
    "readonly": "readonly", "read": "readonly", "ro": "readonly",
    "edit": "developer", "safe": "developer", "developer": "developer",
    "full": "client", "auto": "client", "client": "client",
}

DEFAULT_MODE = "client"

_current = {"mode": DEFAULT_MODE}


def normalize(mode_id: str | None) -> str:
    key = str(mode_id or "").strip().lower()
    return ALIASES.get(key, DEFAULT_MODE)


def get(mode_id: str | None = None) -> Mode:
    return MODES[normalize(mode_id if mode_id is not None else _current["mode"])]


def current_mode() -> Mode:
    return get(None)


def set_mode(mode_id: str) -> Mode:
    key = normalize(mode_id)
    _current["mode"] = key
    return MODES[key]


def allows(cap: str, mode_id: str | None = None) -> bool:
    return cap in get(mode_id).caps


def require(cap: str, mode_id: str | None = None) -> None:
    """不满足时抛 PermissionError，消息里带「为什么 + 该开哪个模式」。"""
    mode = get(mode_id)
    if cap in mode.caps:
        return
    enabling = [m.name for m in MODES.values() if cap in m.caps]
    need = " / ".join(enabling) if enabling else "（无任何模式开放）"
    raise PermissionError(
        f"当前模式「{mode.name}」不允许该操作：{CAPS.get(cap, cap)}\n"
        f"请切换到「{need}」模式后再试。"
    )


def needs_human(node: str, mode_id: str | None = None) -> bool:
    """该节点在这个模式下是否必须人工审核。"""
    mode = get(mode_id)
    if mode.auto_approve:
        return False
    return node in mode.key_nodes


def describe(mode_id: str | None = None) -> dict:
    mode = get(mode_id)
    return {
        "mode": mode.id,
        "name": mode.name,
        "desc": mode.desc,
        "sandbox": mode.sandbox,
        "auto_approve": mode.auto_approve,
        "key_nodes": [{"id": n, "label": KEY_NODES.get(n, n)} for n in mode.key_nodes],
        "boundary": mode.boundary,
        "extras": list(mode.extras),
        "capabilities": sorted(mode.caps),
        "denied": sorted(set(CAPS) - set(mode.caps)),
        "modes": [
            {"id": m.id, "name": m.name, "desc": m.desc, "color": m.color,
             "auto_approve": m.auto_approve,
             "boundary": m.boundary,
             "extras": list(m.extras),
             "key_nodes": [{"id": n, "label": KEY_NODES.get(n, n)} for n in m.key_nodes],
             "capabilities": sorted(m.caps),
             "is_current": m.id == mode.id}
            for m in MODES.values()
        ],
        "capability_labels": CAPS,
        "key_node_labels": KEY_NODES,
    }
