"""最小 Ollama 客户端（给本地模型用）。

注意：故意用 127.0.0.1 而非 localhost——Windows 上 localhost 优先解析 ::1，
而 Ollama 只监听 IPv4，会让每次请求白等约 2 秒（docs/09 §6）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:11434"


def _post(path: str, payload: dict, timeout: float = 240.0) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(path: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def alive() -> bool:
    try:
        _get("/api/tags", timeout=2.0)
        return True
    except Exception:
        return False


def models() -> list[str]:
    try:
        return [m["name"] for m in _get("/api/tags").get("models", [])]
    except Exception:
        return []


def pick_default() -> str:
    """选默认本地模型：门槛见 `core/model_policy.py`（实测下限 7B）。

    顺序按**实测能力**排，不按显存排：同一个夜班任务（3 模块 / 8 函数），
    7B(CPU) 机械验收 8/8、独立验收 19/19；3B(GPU) 只有 2/8 与 2/16，且绝大多数失败是
    "生成代码无法解析"。所以这里绝不把 3B 类小模型当默认。
    """
    ms = models()
    for want in ("qwen2.5-coder:7b", "qwen2.5:7b", "qwen2.5-coder:14b", "qwen2.5:14b",
                 "qwen2.5-coder:32b"):
        if want in ms:
            return want
    try:
        from . import model_policy
        best = model_policy.best_available()
        if best:
            return best
    except Exception:                                    # noqa: BLE001
        pass
    for m in ms:
        if "coder" in m:
            return m
    if ms:
        return ms[0]
    # 一个模型都没装：返回空串表示"没有可用的本地模型"。
    # 以前这里回落 `qwen2.5:1.5b` —— 那正好是本项目明令不许当默认的小模型
    # （1.5B 连结构化输出都不稳）。CI/新机器上没有任何模型，于是默认值成了 1.5B，
    # check_model_policy 立刻判红。**宁可说"没有"，也不要给一个不合格的默认值。**
    return ""


def chat(messages: list[dict], model: str, *, json_mode: bool = False,
         temperature: float = 0.15, num_predict: int = 900, num_ctx: int = 8192,
         stop: list[str] | None = None) -> dict:
    """返回 {text, tokens, ms, eval_count, prompt_eval_count}。

    注意：stop 里【不要】放期望输出的开头标记（如 `<<<<<<< SEARCH`），
    否则模型会在第一个 token 处就被截断（实测：输出 2 个 token 直接停）。
    只在需要截断"收尾"时用（如代码围栏 ```）。
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.9,
            "repeat_penalty": 1.1,      # 3B 容易陷入重复输出，轻微惩罚很有用
            "num_predict": num_predict,
            "num_ctx": num_ctx,
        },
    }
    if stop:
        payload["options"]["stop"] = stop
    if json_mode:
        payload["format"] = "json"
    t0 = time.time()
    try:
        data = _post("/api/chat", payload)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Ollama 调用失败：{exc}（确认 ollama serve 在跑、模型已 pull）") from exc
    text = (data.get("message") or {}).get("content", "")
    return {
        "text": text,
        "ms": int((time.time() - t0) * 1000),
        "eval_count": data.get("eval_count", 0),
        "prompt_eval_count": data.get("prompt_eval_count", 0),
    }
