"""模型门槛：本地工人模型建议 **> 4B**，但**从不拒绝用户的选择**。

门槛来自用户口径（"超过 4b 就能用"），并保留了原来的实测依据作为**下限说明**：
3B 在同一个夜班任务上机械验收 2/8、独立验收 2/16，绝大多数失败是
「生成代码无法解析 (ParserSyntaxError)」—— 连结构化作答都交不出来。

**但选哪个模型是用户的判断，不是系统的判断**（用户明确要求过）。
所以本模块只做两件事：
  1) 把"低于建议门槛可能会怎样"讲清楚（advise / enforce 的第二返回值）；
  2) 给"自动挑一个"的场景一个排序（best_available）。
历史包袱说明：`verdict()["ok"]` 曾经被 CLI/后端当成硬拒依据，现在它恒为 True，
只带 `advisory` 标记；任何调用点都不该再拿它拦人。

本模块是**唯一**的门槛判定点：CLI / 后端 / 夜班 / 界面提示都从这里取，避免各处各写一套。
"""
from __future__ import annotations

import re

MIN_PARAMS_B = 4.0        # 建议门槛（不是红线）：4.0 本身不算"超过"

_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*b\b", re.I)


def params_b(model: str) -> float:
    """从名字里读参数量（`qwen2.5-coder:3b` → 3.0）。读不出来返回 0（视为"未标注"）。"""
    m = _SIZE_RE.search(str(model or ""))
    return float(m.group(1)) if m else 0.0


def is_remote(model: str) -> bool:
    """远端模型（deepseek-* 等）不受本地门槛约束——它本来就不是小模型。"""
    name = str(model or "").lower()
    return name.startswith(("deepseek", "gpt", "claude", "glm", "qwen-max", "qwen-plus")) \
        or name in ("", "remote")


def verdict(model: str) -> dict:
    """判定一个模型当工人的**建议**。返回 {ok, advisory, model, params_b, min_params_b, message}。

    ok 恒为 True（不拒绝）；advisory=True 表示"低于建议门槛，质量可能明显下降"。
    """
    size = params_b(model)
    base = {"model": model, "params_b": size, "min_params_b": MIN_PARAMS_B}
    if is_remote(model):
        return {**base, "ok": True, "advisory": False,
                "message": "远端模型，不受本地门槛约束"}
    if size <= 0:
        return {**base, "ok": True, "advisory": False,
                "message": f"名字里没有参数量，按可信处理（建议 > {MIN_PARAMS_B:g}B）"}
    if size > MIN_PARAMS_B:
        return {**base, "ok": True, "advisory": False,
                "message": f"{size:g}B > 建议门槛 {MIN_PARAMS_B:g}B"}
    return {**base, "ok": True, "advisory": True,
            "message": (f"{model} 只有 {size:g}B，低于建议门槛 {MIN_PARAMS_B:g}B："
                        f"实测这一档连结构化输出都不稳（机械验收 2/8、独立验收 2/16）。"
                        f"仍按你的选择使用；想更稳可换 {best_available(model) or 'qwen2.5-coder:7b'}")}


def is_coder(model: str) -> bool:
    """是不是代码专精模型（qwen2.5-coder / deepseek-coder / codegemma …）。

    为什么要区分：这套系统让工人干的是"按契约写一个函数、只输出 JSON 符号编辑"，
    **代码专精模型比通用模型更听指令、更少废话**。实测同尺寸下通用 7B 经常
   在 JSON 前后加解释，代码专精的几乎不加。
    """
    name = str(model or "").lower()
    return any(k in name for k in ("coder", "code-", "-code", "codegemma", "codellama",
                                   "starcoder", "granite-code"))


def best_available(current: str = "") -> str:
    """已安装模型里最合适当工人的那个（只在"用户没指定"或"建议换一个"时使用）。

    排序规则（按这套系统的实际需要，不是按参数大小拍脑袋）：
      1) 必须 >4B（建议门槛）；
      2) **代码专精优先**（同样的 7B，coder 明显更会按契约写、更少废话）；
      3) 同条件下参数更大者优先。
    没有合格的就返回空串。
    """
    try:
        from . import ollama
        avail = [m for m in ollama.models() if m != current]
    except Exception:                                    # noqa: BLE001
        return ""
    ok = [m for m in avail if not verdict(m)["advisory"] and params_b(m) > 0]
    if not ok:
        return ""
    ok.sort(key=lambda m: (-params_b(m) if not is_coder(m) else -params_b(m) - 100, m))
    return ok[0]


def candidates() -> list[dict]:
    """给界面/CLI 用：列出已安装模型的判定结果 + 是否代码专精 + 推荐用途。

    "低于建议门槛"不等于"该删"：小模型（1.5B/3B）写小嵌入式 AI 完全够用，
    只是**当这套系统的工人时质量会明显下降**。所以把两件事分开说。
    """
    try:
        from . import ollama
        avail = ollama.models()
    except Exception:                                    # noqa: BLE001
        return []
    out = []
    for m in avail:
        v = verdict(m)
        size = params_b(m)
        if not v["advisory"]:
            use = "适合当工人" + ("（代码专精，推荐）" if is_coder(m) else "（通用）")
        elif size and size <= MIN_PARAMS_B:
            use = (f"低于建议门槛（{size:g}B ≤ {MIN_PARAMS_B:g}B），当工人质量会明显下降；"
                   f"写小体量/嵌入式代码仍可用（仍按你的选择使用）")
        else:
            use = "未标注参数量，请自行确认"
        out.append({"model": m, "params_b": size, "coder": is_coder(m),
                    "ok_as_worker": not v["advisory"], "advisory": v["advisory"], "use": use})
    out.sort(key=lambda x: (-x["params_b"], not x["coder"], x["model"]))
    return out


def enforce(model: str) -> tuple[str, str]:
    """返回 (最终模型, 说明)。**不替换、不拒绝** —— 选哪个模型由用户自己决定。"""
    v = verdict(model)
    return model, "" if not v["advisory"] else v["message"]


def require(model: str) -> str:
    """兼容旧入口：不抛错，只把模型原样返回。要提示请用 advise()。"""
    return model


def advise(model: str) -> str:
    """给界面/CLI 的一句说明（低于建议门槛时返回原因，否则空串）。"""
    return verdict(model).get("message", "") if verdict(model)["advisory"] else ""

