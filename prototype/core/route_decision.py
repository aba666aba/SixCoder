"""补偿路径决策：让远端自己判断一个需求该走哪条实现/验收路径。

## 为什么需要（2026-11-23，用户提问链）

用户连续几轮追问能力边界：
  视频剪辑为什么没做 → 协商机制为什么没补 → 是不是写一类补一类栈 →
  能不能覆盖 99% 软件 → 前端/桌面补生态包 → 闭源宿主和审美判据有没有手段补 →
  **写成工具让远端判断路径**。

本模块把这条论证链的结论固化成一个**确定性决策器**：给定需求的结构化特征
（宿主类型 / 判据能否机械化 / 是否主观审美 / 能否起进程观测），输出该走哪条
路径，并逐条说明"引擎能验什么、人要做什么、补不到哪"。

远端通过 function calling 调 `decide_route`（schema 见 toolschema.py），
填它对当前需求的特征判断；引擎把决策**原样回传**，远端据此选择生成策略。

## 四条路径（与前几轮结论、生态包 manifest 口径一致）

1. direct（直接生成）
   判据可机械判定 + 引擎能起进程跑探针 → 引擎甜区，能自己验收自己修。
2. plugin_bridge（插件桥 + 产物验收）
   闭源宿主（PR/Office），引擎进不去进程 → 宿主内插件导出结构化产物，
   引擎用 cli/http 探针验**产物**不验**过程**。补得到"交付物对不对"，
   补不到"宿主内实时交互"；插件要人手写一次，宿主升级即破。
3. proxy_plus_human（降维判据 + 人在环）
   审美/体验型质量 → 把"好不好看"拆成可数的代理指标（时长/字幕/黑屏/
   分辨率/帧描述匹配）机械筛掉明显坏的，最终审美交人在预览上拍板。
   补得到"筛掉明显坏的 + 给审稿人结构化证据"，补不到"真懂审美"。
4. structural_limit（结构性拒绝）
   既进不去、判据又纯主观 → 不假装能交付，明确告诉远端此路不通，
   给出唯一可行的降级（人手工完成 / 换非宿主形态重构需求）。

## 设计纪律

- **纯决策、零副作用**：不碰文件系统、不联网、不写库，只读输入产结论。
- **确定性**：同样输入永远同样输出，可自检、可复现。
- **不谎报**：每条路径都带 `engine_can` / `human_must` / `cannot`，边界写死在结论里。
- **信号不全就保守**：缺失/异常字段一律降级到最保守路径，不冒进判 direct。
"""
from __future__ import annotations

from typing import Any

# 四条路径的稳定标识（远端据此分支，不要随意改名）
ROUTE_DIRECT = "direct"
ROUTE_PLUGIN_BRIDGE = "plugin_bridge"
ROUTE_PROXY_HUMAN = "proxy_plus_human"
ROUTE_DEPLOY = "authorized_deploy"
ROUTE_STRUCTURAL = "structural_limit"

ROUTES = (ROUTE_DIRECT, ROUTE_PLUGIN_BRIDGE, ROUTE_PROXY_HUMAN,
          ROUTE_DEPLOY, ROUTE_STRUCTURAL)

# 每条路径的固定结论模板：引擎能做什么 / 人必须做什么 / 补不到什么
_TEMPLATES: dict[str, dict[str, Any]] = {
    ROUTE_DIRECT: {
        "label": "直接生成（引擎甜区）",
        "engine_can": [
            "按契约生成代码并用机械判据自验（HTTP 状态码 / JSON 字段 / 单元测试）",
            "起进程跑探针，端到端确认交付物可用",
            "判据红了自己定位、自己修，闭环无需人盯",
        ],
        "human_must": "审交付报告即可；部署、第三方密钥、外部集成仍需人提供",
        "cannot": "部署运维、账号权限、跨系统集成等闭环外的活",
    },
    ROUTE_PLUGIN_BRIDGE: {
        "label": "插件桥 + 产物验收",
        "engine_can": [
            "生成宿主内插件（CEP/UXP/COM 加载项），由插件调用宿主 API 干活",
            "插件把执行结果导成结构化清单/文件，引擎用 cli 或 http 探针验产物",
            "验'交付物对不对、参数到位没'——PRAutoCut 就是这座桥",
        ],
        "human_must": "手写并安装插件（一次性有限成本），宿主升级后可能要重调",
        "cannot": "宿主内实时交互；宿主闭源 API 随版本变化，升级即破",
    },
    ROUTE_PROXY_HUMAN: {
        "label": "降维判据 + 人在环",
        "engine_can": [
            "把主观质量拆成可数的代理指标（时长/字幕/黑屏/分辨率/帧描述匹配）",
            "机械筛掉明显坏的候选，并给出结构化证据（截图/console/DOM）",
            "让远端'看'画面时走视觉模型描述，但判据仍落在可判的指标上",
        ],
        "human_must": "在预览上做最终审美拍板——引擎不代替人判断'好不好看'",
        "cannot": "真懂审美、代替人拍板；视觉判分不稳且烧 token（browser.py 刻意不喂截图）",
    },
    ROUTE_DEPLOY: {
        "label": "授权部署通道（预留目标 + 白名单 + 强制人审）",
        "engine_can": [
            "连人预先预留的服务器、只跑该目标的 allow_commands 白名单命令（含注入元字符防护）",
            "把 ssh 执行结果与 rc 原样回传，全程写 .sixcoder/audit.jsonl",
            "每次执行强制人工审核（fail-closed：没人答/超时=拒绝）",
        ],
        "human_must": ("预先登记服务器（host/user/认证方式/命令白名单）打开通道；"
                       "每条部署命令都要人审放行；凭据由本机 agent/私钥引用提供，引擎不碰"),
        "cannot": ("任意 ssh、密码认证、白名单外的命令、无人值守自动部署；"
                   "线上运行指标/监控告警仍需人看（判据不在本地）"),
    },
    ROUTE_STRUCTURAL: {
        "label": "结构性拒绝（此路不通）",
        "engine_can": [
            "诚实判定'进不去宿主 + 判据又纯主观 = 闭环无法成立'",
            "给出唯一可行的降级建议，不假装能交付",
        ],
        "human_must": "要么人工完成，要么把需求换成引擎可观测、判据可机械化的形态",
        "cannot": "在闭源宿主内自证结果、对主观质量给出可复验的通过结论",
    },
}

# 布尔字段：接受真布尔，也兼容远端可能回传的字符串/数字（保守解析）
def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y", "是")
    return False


def _pick_route(sig: dict[str, Any]) -> tuple[str, str]:
    """按优先级选路径，返回 (route, 判定依据)。顺序即优先级，不可随意调整。"""
    closed_host = sig["closed_host"]
    can_observe = sig["can_observe_runtime"]
    mechanical = sig["mechanical_verdict"]
    subjective = sig["subjective_quality"]
    deploy_ok = sig["deploy_authorized"]

    # ① 主观质量是最高优先级的分岔：只要涉及"好不好看/体验"，即便判据部分可机械化，
    #    最终验收也必须有人在环 —— 不能因为有几个代理指标就冒进判 direct。
    #    人在环这条路本身就是为"判据不可机械化"设计的，所以它排在②的否决项之前。
    if subjective:
        extra = "；同时是闭源宿主，宿主操作仍需插件桥导出产物" if closed_host else ""
        return ROUTE_PROXY_HUMAN, ("质量含主观审美/体验成分，机器判不了最终好坏" + extra)

    # ② 判据不可机械化是早否决：插件桥的前提是"产物可验"，判据不可判时，
    #    即便插件导出了产物也无法判对错，闭环照样断 —— 不能因为这步是闭源宿主
    #    就误判成插件桥（这正是烟测抓到的顺序 bug）。既不在环也判不了 → 拒绝。
    if not mechanical:
        return ROUTE_STRUCTURAL, ("判据不可机械化，无法自证交付对错"
                                  + ("；且闭源宿主进不去，连产物都无从验" if closed_host else ""))

    # ②b 授权部署通道（2026-11-23）：判据可机械化 + 人已预留服务器并开放通道 +
    #    不是闭源宿主 GUI → 走受控部署：白名单命令 + 强制人审 + 审计。
    #    这是"线上部署/运维"从结构性边界变成有条件可补的唯一入口；未授权就落到下面的
    #    插件桥/拒绝，绝不因为用户提了"部署"二字就冒进。
    if deploy_ok and not closed_host:
        return ROUTE_DEPLOY, "人已预留部署目标并开放通道，判据可机械化；走白名单+强制人审的受控部署"

    # ③ 闭源宿主：引擎进不去进程，但判据可机械化、产物可验 → 插件桥。
    #    这是"有没有手段补闭源宿主"的答案。
    if closed_host:
        return ROUTE_PLUGIN_BRIDGE, "闭源宿主，引擎无法进入进程；可让宿主插件导出产物间接验收"

    # ④ 起不了进程但判据可机械化：能验代码/静态产物，不能端到端 → 退到验落盘产物路径。
    if not can_observe:
        return ROUTE_PLUGIN_BRIDGE, "无可起的目标进程，但判据可机械化；退到'验落盘产物'路径"

    # ⑥ 全部满足：引擎甜区。
    return ROUTE_DIRECT, "判据可机械判定且引擎能起进程跑探针，可自验自修"


def decide_route(
    target: str = "",
    closed_host: Any = False,
    can_observe_runtime: Any = True,
    mechanical_verdict: Any = True,
    subjective_quality: Any = False,
    deploy_authorized: Any = False,
    requirement: str = "",
) -> dict[str, Any]:
    """给定需求特征，输出补偿路径决策。纯函数，无副作用。

    Args:
        target: 目标形态描述（如 "PR 自动剪辑插件" / "订单管理 API"），只用于回显，不参与判定。
        closed_host: 目标是否寄生在闭源宿主里（PR / Office / AutoCAD 等）。
        can_observe_runtime: 引擎能否起目标进程并跑探针观测。
        mechanical_verdict: 验收判据能否写成机器可判定（HTTP/DOM/数值/退出码）。
        subjective_quality: 交付质量是否主要依赖主观审美/体验判断。
        deploy_authorized: 人是否已预留部署目标并开放受控通道（deploy.enabled + targets）。
        requirement: 需求原文摘要，只用于回显。

    Returns:
        {ok, route, label, why, engine_can, human_must, cannot, signals, target, requirement}
    """
    sig = {
        "closed_host": _as_bool(closed_host),
        "can_observe_runtime": _as_bool(can_observe_runtime),
        "mechanical_verdict": _as_bool(mechanical_verdict),
        "subjective_quality": _as_bool(subjective_quality),
        "deploy_authorized": _as_bool(deploy_authorized),
    }
    route, why = _pick_route(sig)
    tpl = _TEMPLATES[route]
    return {
        "ok": True,
        "route": route,
        "label": tpl["label"],
        "why": why,
        "engine_can": list(tpl["engine_can"]),
        "human_must": tpl["human_must"],
        "cannot": tpl["cannot"],
        "signals": sig,
        "target": str(target or "")[:300],
        "requirement": str(requirement or "")[:500],
    }


def tool_summary() -> str:
    """给远端的一段速查：四条路径各自适用什么信号（进 GUIDE 用）。"""
    return (
        "补偿路径决策：closed_host（闭源宿主 PR/Office）=插件桥+产物验收；"
        "subjective_quality（审美/体验）=降维判据+人在环；"
        "deploy_authorized（人预留了服务器并开放通道）=授权部署（白名单+强制人审）；"
        "都不成立且 mechanical_verdict（判据可机械化）且 can_observe_runtime"
        "（能起进程跑探针）=直接生成；判据不可机械化又进不去=结构性拒绝。"
    )
