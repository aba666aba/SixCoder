"""模型能力表：这个模型支不支持原生工具 / 思考，**本地登记**，踩到新坑自动记住。

为什么需要（2026-11-10）：
    模型能力参差不齐 —— 有些模型不支持原生 function calling（GLM 视觉模型、
    某些精简版，官方直接返回 `{"code":"invalid_parameter","message":"tools not supported"}`），
    有些只支持一档思考。
    以前的行为是**硬带 tools**：不支持的模型要么 400、要么静默不返回工具调用，
    而提示词还教它"你有只读工具" → 它开始**编造文件内容**
    （11-08「字段名用错 8 次」的根源之一）。
    现在：**能力本地登记**（不联网、可编辑），按能力选通路，踩到新坑自动记住。

三条工具通路（`tool_mode`）
    native —— 官方 function calling（最优）；提示词教它"直接按 schema 调函数"
    xml    —— 文本协议 `<tool name="x" path="y" />`；提示词换 XML 版
    none   —— 明确告诉模型"你没有工具"；**不让它假装看过文件**

思考能力**不用单独声明**：模型档案的 `efforts` 长度 > 1 就表示支持多档思考
（`settings_store.single_effort()` 已经按这个口径分流）。

优先级（谁说了算）
    ① 档案里显式写 `supports_tools`（用户填的，最可信）
    ② **本地记忆**（真踩过 400 之后自动记下来的）
    ③ 内置已知表（本文件下方）
    ④ 未知 → 乐观用 native（先试一次，失败自动降级并记住）
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

# 本地记忆文件（**引擎级**，不是每个项目一份 —— 能力是模型的属性，不是仓库的）
MEMO_FILE = Path(__file__).resolve().parent.parent / "data" / "capability.json"

MODE_NATIVE = "native"
MODE_XML = "xml"
MODE_NONE = "none"

# 远端说"不支持工具"的措辞（各厂商不一样，都收）
_UNSUPPORTED_PATTERNS = (
    "tools not supported",
    "tool use is not supported",
    "does not support tool",
    "doesn't support tool",
    "not support function",
    "unsupported tool",
    "tool_use",
    "function calling is not supported",
    "不支持tool",
    "不支持 tool",
    "不支持工具",
)


def is_tools_unsupported(err: str) -> bool:
    """远端是不是说了"我不支持 tools"（而不是别的失败）。"""
    text = str(err or "").strip().lower()
    if not text:
        return False
    if any(p in text for p in _UNSUPPORTED_PATTERNS):
        return True
    # 智谱/有些网关回 invalid_parameter，但正文里提到 tool
    return "invalid_parameter" in text and "tool" in text


# ── 内置已知表 ──────────────────────────────────────────────────────────────
# 只登记**有出处**的；其余一律 unknown（先试一次，失败自动降级并记住）。
#   · DeepSeek：官方 function calling 全系支持（2026-11 真机验证过 flash）
#   · GLM：官方「GLM-4 系列已支持所有 tools」，GLM-5 系有专门的 function calling 文档
#   · GLM 视觉模型（4.5V / 4.6v）：官方路由明确标记不支持 function call
_VISION = re.compile(r"(4\.5v|4\.6v|-v$|vision)", re.I)


def known_mode(model: str, compat: str = "") -> str:
    """内置已知表查这个模型的工具能力。查不到 → ""（= 未知，先试）。"""
    name = str(model or "").strip().lower()
    fam = str(compat or "").strip().lower()
    if not name:
        return ""
    # 视觉模型：官方标记不支持 function call → 直接走 none（别试了，试也是 400）
    if _VISION.search(name):
        return MODE_NONE
    if fam in ("glm", "zhipu", "bigmodel"):
        if name.startswith("glm-4") or name.startswith("glm-5"):
            return MODE_NATIVE
        return ""
    if name.startswith("deepseek"):
        return MODE_NATIVE
    return ""


# ── 本地记忆（真踩过之后记下来） ────────────────────────────────────────────
def _memo_path() -> Path:
    return MEMO_FILE


def learned_mode(model: str) -> str:
    """本地记忆里这个模型的工具能力（没有 → ""）。"""
    p = _memo_path()
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return ""
    if not isinstance(data, dict):
        return ""
    rec = data.get(str(model or "").strip())
    if not isinstance(rec, dict):
        return ""
    mode = str(rec.get("tool_mode") or "")
    return mode if mode in (MODE_NATIVE, MODE_XML, MODE_NONE) else ""


def remember(model: str, mode: str, why: str = "") -> str:
    """记住"这个模型的工具能力是 X"（本地文件，下次直接按它走，不再试错）。

    只记**降级**（native → xml/none）；不记"升级"—— 试出来的成功不代表换端点也成立。
    """
    name = str(model or "").strip()
    if not name or mode not in (MODE_XML, MODE_NONE):
        return ""
    p = _memo_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.exists():
            try:
                loaded = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except Exception:                            # noqa: BLE001
                data = {}
        data[name] = {"tool_mode": mode, "why": str(why or "")[:200],
                      "at": time.strftime("%Y-%m-%d %H:%M:%S")}
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return mode
    except Exception:                                    # noqa: BLE001
        return ""


def declared_mode(profile: dict | None) -> str:
    """模型档案里显式写的 `supports_tools`（true/false/unknown→""）。"""
    prof = profile or {}
    raw = prof.get("supports_tools")
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    if text in ("true", "1", "yes", "y", "支持", "on"):
        return MODE_NATIVE
    if text in ("false", "0", "no", "n", "不支持", "off"):
        return MODE_NONE
    return ""


def tool_mode(model: str = "", compat: str = "", declared: str = "",
              profile: dict | None = None) -> str:
    """这个模型该走哪条工具通路。

    优先级：档案显式声明 → 本地记忆 → 内置已知表 → 未知（乐观走 native）。
    """
    dec = declared or declared_mode(profile)
    if dec:
        return dec
    learned = learned_mode(model)
    if learned:
        return learned
    known = known_mode(model, compat)
    if known:
        return known
    return MODE_NATIVE


def degrade(mode: str) -> str:
    """降一级：native → xml → none。已经是 none 就不动了。"""
    if mode == MODE_NATIVE:
        return MODE_XML
    if mode == MODE_XML:
        return MODE_NONE
    return MODE_NONE


def current_tool_mode() -> tuple[str, str, str]:
    """**当前模型档案**对应的工具能力 → (mode, model, compat)。

    档案里显式写的 `supports_tools` 最可信；其次本地记忆（真踩过）；再次内置表。
    读不到档案 → ("native", "", "")（乐观先试，失败自动降级）。
    """
    model = compat = ""
    profile: dict = {}
    try:
        from . import settings_store as _ss
        profile = _ss.active_model() or {}
        model = str(profile.get("model") or "")
        compat = str(profile.get("compat") or "")
    except Exception:                                    # noqa: BLE001
        profile = {}
    mode = tool_mode(model, compat, profile=profile)
    return mode, model, compat


def describe(model: str = "", compat: str = "", profile: dict | None = None) -> dict:
    """给界面/报告用的一份能力说明（如实说，别只说"支持"）。"""
    mode = tool_mode(model, compat, profile=profile)
    src = ("档案显式声明" if (declared_mode(profile)
                             or str((profile or {}).get("supports_tools") or "").strip())
           else ("本地记忆（真踩过）" if learned_mode(model)
                 else ("内置已知表" if known_mode(model, compat) else "未知（先试一次）")))
    return {"model": model, "compat": compat, "tool_mode": mode, "source": src}
