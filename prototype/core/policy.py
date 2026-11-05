"""按模式决定"谁来拍板、谁来写代码"——三种模式的**协作方式**（不是权限）。

用户把两档的本质说清楚了，这里把它落成代码里的策略对象：

    甲方模式 = 写代码的**完全体**。上限是远端 API 的推理能力：需求一句话进来，
               内部所有决策自己做完，**任何问题都由远端 API 做最终裁判**。
               本地小模型只负责便宜、可机械验证的活；一旦本地拿不出硬证据，
               立刻把决策权和代码生成交给远端，直到交付。

    开发者模式 = **工具**。上限是开发者本人的思维架构能力：
               必须由人说出具体的实现方案，系统照着实现；
               系统不许悄悄换架构、不许让远端替人拍板 —— 有分歧就抛 needhuman 问人。

    只读模式 = 不写任何东西，只做意图理解与改进分析（不能跑夜班）。

所以这三档的差别不只是"审批开关"，而是三个策略位：
    arbiter          谁做最终裁判：remote（甲方） / human（开发者）
    code_escalation  什么时候把写代码交给远端：first-red（甲方） / on-approval（开发者） / off（只读）
    plan_authority   实现方案的权威：remote（甲方自己定） / human（开发者给）
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import permissions

# code_escalation 三档
FIRST_RED = "first-red"          # 本地一旦被硬判据判红，就交给远端写
ON_APPROVAL = "on-approval"      # 只在人批准后升级（关键节点 remote.call 本来就要审批）
OFF = "off"                      # 不升级


@dataclass(frozen=True)
class Policy:
    mode: str
    name: str
    arbiter: str                     # remote | human
    code_escalation: str
    plan_authority: str              # remote | human
    remote_code_budget: int          # 整夜"远端写代码"的次数上限（0 = 不设上限，由时间预算兜）
    notes: tuple[str, ...] = field(default=())
    # 谁来写代码：ladder（按八档阶梯逐级升）/ local（只本地写）/ none
    code_author: str = "local"
    # 开局先让远端出"实现方案"（把远端当架构师用，而不是等卡住才问）
    plan_first: bool = False
    # 规划/诊断阶段的思考默认值（远端推理能力是甲方模式的上限，这里该开就开）
    thinking_plan: str = "off"
    # ------------------------------------------------------------------
    # 「规格优先」（甲方模式 v1.1，目标：从 0 生产、人不改代码）
    # 规格（接口/数据形状/错误语义/示例）决定后面每个模块的对错，是最高杠杆的一步。
    # 旧行为是"从最低档开始生成规格然后固化"——用最弱的算力做最高杠杆的决定，本末倒置。
    # ------------------------------------------------------------------
    spec_tier: str = "ladder"          # ladder（从最低档起）| strongest（最强档）
    spec_thinking: str = "off"         # 生成规格时开不开思考
    # 写代码从哪一档起（**单模型**下就是"从哪一档思考强度起"）：
    #   auto     甲方模式：由 core/effort.py 按需求/难度自动选强度（本次运行只用一个模型）
    #   ladder   开发者模式：从最低强度往上爬（省钱优先）
    #   max      总是用该模型最深的一档（最贵、最不求人）
    effort_mode: str = "ladder"
    # 规格有歧义时怎么办：decide = 远端自己拍板并把假设写清楚（甲方模式）；
    #                    ask    = 停下来问人，不许猜（开发者模式）
    ambiguity: str = "decide"
    # 模块划分 / 范围由谁定：remote（甲方模式，远端拆分）| human（开发者模式）
    scope_authority: str = "human"
    # 单个模块的"自愈"轮数：AI 内部反复改到判据全绿，中间不需要人介入
    heal_attempts: int = 2
    # 新一轮的失败证据与上一轮完全相同 → 立刻停（同一个提示词再问一遍只是白烧钱）
    heal_stop_on_repeat: bool = True

    @property
    def remote_decides(self) -> bool:
        return self.arbiter == "remote"

    @property
    def human_plan_required(self) -> bool:
        return self.plan_authority == "human"

    @property
    def remote_writes_code(self) -> bool:
        return self.code_author in ("remote", "ladder")


POLICIES: dict[str, Policy] = {
    "client": Policy(
        mode="client", name="甲方模式", arbiter="remote", code_escalation=FIRST_RED,
        plan_authority="remote", remote_code_budget=0,
        code_author="remote", plan_first=True, thinking_plan="on",
        spec_tier="strongest", spec_thinking="on", ambiguity="decide",
        scope_authority="remote", heal_attempts=4, heal_stop_on_repeat=True,
        effort_mode="auto",
        notes=("甲方模式就是夜班：提需求 → 系统自己做完 → 早上看交付报告",
               "**规格优先**：接口/数据形状/错误语义/示例由最强档 + 思考全开产出（一次几毛钱，"
               "却决定后面每个模块的对错）",
               "规格有歧义时远端自己拍板并把假设写进交付报告，不回头问人、不猜着往下走",
               "模块拆分与范围也由远端定（scope=remote），系统只提供确定性兜底",
               "代码由远端整文件写；单个模块允许 4 轮内部自愈，人不介入",
               "每一次落盘仍要过全部机械判据：远端强不等于必然对"),
    ),
    "developer": Policy(
        mode="developer", name="开发者模式", arbiter="human", code_escalation=ON_APPROVAL,
        plan_authority="human", remote_code_budget=0,
        code_author="local", plan_first=False, thinking_plan="off",
        spec_tier="ladder", spec_thinking="off", ambiguity="ask",
        scope_authority="human", heal_attempts=2, heal_stop_on_repeat=True,
        notes=("实现方案由人给，系统照着做，不许自行改架构",
               "规格有歧义时停下来问人（ambiguity=ask），由人做这个决定",
               "本地模型按方案打字；远端写代码属于关键节点，必须人批准",
               "需要架构决策但没有方案时抛 needhuman，不替人拍板",
               "每次决策与审批都留在审计日志里"),
    ),
    "readonly": Policy(
        mode="readonly", name="只读模式", arbiter="human", code_escalation=OFF,
        plan_authority="human", remote_code_budget=0,
        code_author="none", plan_first=False, thinking_plan="off",
        notes=("不写任何文件，不跑夜班",
               "只做意图理解、静态审查、语义比对"),
    ),
}


def for_mode(mode_id: str | None = None) -> Policy:
    m = permissions.get(mode_id) if mode_id else permissions.current_mode()
    return POLICIES.get(m.id, POLICIES["client"])


def current() -> Policy:
    return for_mode(None)


def should_escalate(pol: Policy, n_fail: int, hard_evidence: bool, used: int) -> tuple[bool, str]:
    """本张单要不要把代码交给远端？返回 (要不要, 理由)。

    甲方模式：只要本地被**硬判据**判红就升级（不等第二次失败）——这是"完全体"的关键，
              本地小模型做不出来的东西，多试两次只是多烧时间。
    开发者模式：远端写代码是关键节点，必须走审批；次数预算不作为自动接管依据。
    只读模式：永不接管（它根本没有写权限）。
    """
    if pol.code_escalation == OFF:
        return False, "该模式不升级（只读）"
    if pol.remote_code_budget and used >= pol.remote_code_budget:
        return False, f"远端写代码预算已用完（{used}/{pol.remote_code_budget}）"
    if pol.code_escalation == FIRST_RED:
        if hard_evidence:
            return True, "本地被硬判据判红 → 远端接管（甲方模式）"
        if n_fail >= 2:
            return True, "连续失败 2 次 → 远端接管（甲方模式兜底）"
        return False, "本地还有重试余地"
    # ON_APPROVAL：只在连续失败后提请远端（是否会真的执行由审批门决定）
    if n_fail >= 2:
        return True, "连续失败 2 次 → 提请远端（需人工批准）"
    return False, "本地还有重试余地"


def plan_block(plan: str, pol: Policy | None = None) -> str:
    """把"实现方案"渲染成提示词里的一段（权威性说明写清楚，免得模型自行发挥）。"""
    text = (plan or "").strip()
    if not text:
        return ""
    pol = pol or current()
    who = "甲方（需求方）" if pol.plan_authority == "remote" else "开发者（你只能照它做）"
    return ("\n【实现方案（" + who + "给的，必须照做）】\n"
            + text[:2000]
            + "\n（不要自行更换架构、改模块划分或换数据流；方案里没写到的细节，" +
            ("按契约与判据自行决定" if pol.plan_authority == "remote"
             else "留空并在结果里说明，不要猜") + "）")



def remote_allowed(mode_id: str | None = None) -> tuple[bool, str]:
    """远端调用允许不允许（三档口径不同，用户明确要求）。

    · 甲方模式（= 夜班）：**强制开启**，上限就是远端能力；
    · 开发者模式：**开关**（设置 → 远端模型 → 开发者模式远端），默认关，
      开了等于"一定程度加强本地模型能力"；
    · 只读模式：**强制关闭**（它本来就不该有任何远端写操作）。
    """
    from . import permissions, settings_store
    m = permissions.get(mode_id).id if mode_id else permissions.current_mode().id
    if m == "client":
        return True, "甲方模式：强制开启远端（上限就是远端能力）"
    if m == "readonly":
        return False, "只读模式：强制关闭远端"
    on = bool(settings_store.get_remote().get("dev_remote", False))
    return on, ("开发者模式：远端开关已打开" if on else
                "开发者模式：远端开关已关闭（本地模型自己写；要开去设置里打勾）")


def describe(pol: Policy | None = None) -> str:
    """一句话人话版（进交付报告/命令行输出）。"""
    pol = pol or current()
    arb = "远端 API 拍板" if pol.arbiter == "remote" else "开发者拍板"
    esc = {FIRST_RED: "本地判红即远端重做",
           ON_APPROVAL: "远端写代码需人工批准",
           OFF: "不做代码升级"}[pol.code_escalation]
    plan = "实现方案由系统内部决定" if pol.plan_authority == "remote" else "实现方案由人给"
    author = {"remote": "，代码默认由远端写",
              "ladder": "，代码由远端整文件写（flash:none→…）",
              "local": "，代码由本地按方案写", "none": ""}.get(pol.code_author, "")
    budget = (f"，远端写代码上限 {pol.remote_code_budget} 次/夜"
              if pol.remote_code_budget else "，远端写代码不设次数上限（由时间预算兜）")
    spec = {"strongest": "，规格由最强档生成" if pol.spec_thinking == "on" else "，规格由最强档生成（不思考）",
            "ladder": "，规格从最低档起" if pol.spec_thinking != "on" else "，规格走阶梯（思考开）",
            "off": "，不单独生成规格"}.get(pol.spec_tier, "")
    amb = "，歧义自己拍板" if pol.ambiguity == "decide" else "，歧义停下来问人"
    heal = f"，单模块自愈 {pol.heal_attempts} 轮"
    return f"{pol.name}：{arb}｜{esc}｜{plan}{author}{spec}{amb}{heal}{budget}"
