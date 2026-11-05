"""token 预算与自校准：让"能不能发得下"由 token 决定，而不是由拍的字符数决定。

为什么需要（用户指出的问题）：
    官方上限是 **1M tokens**，而我们过去用一个固定字符数当上限 —— 字符→token 的比率
    随内容漂（中文 ≈0.6 token/字，代码 ≈0.3 token/字符，实测代码型 0.41），
    固定字符数要么超上限（直接 HTTP 400，整夜白跑），要么过度保守（白白拆上下文，
    反而增加远端错判风险）。

做法：
    1. 预算按 **token** 计：max_prompt_tokens（默认 950K = 1M − 32K 输出 − 余量）；
    2. 估算比率**自动校准**：每次调用后拿接口返回的真实 prompt_tokens / 提示词字符数，
       做滑动平均（越用越准，不依赖 tokenizer 下载）；
    3. 估算超过预算的 95% 才压缩（压缩由 contextpack 做：先丢非焦点函数体 → 再丢调用关系）；
    4. 每次调用把"估算 vs 实际"留痕，供报告核对估算准不准。
"""
from __future__ import annotations

import threading
from pathlib import Path

_LOCK = threading.Lock()
# 校准值：token / 字符。初值取自实测（代码型填充 490481 tokens / 1199837 字符 ≈ 0.41）
_RATIO = 0.41
_SAMPLES: list[float] = []
MIN_RATIO, MAX_RATIO = 0.20, 1.2       # 中文最快 0.6/字，任何合理内容都不会超过 1.2


# ---------------- 官方 tokenizer（精确计数，可选） ----------------
# 官方给了 deepseek_v4_tokenizer（tokenizer.json 6MB）。实测两种加载方式的差别很大：
#   transformers.AutoTokenizer.from_pretrained(...) → 16.3 秒（太重，不适合嵌进程序）
#   tokenizers.Tokenizer.from_file("tokenizer.json") → 0.30 秒（就可以嵌了）
# 所以这里直读 tokenizer.json；缺库/缺文件时自动回落到"校准比率"（不报错）。
_TOK = None
_TOK_STATE = {"tried": False, "path": "", "why": ""}


def tokenizer_path() -> Path:
    from . import settings_store
    p = (settings_store.get_remote() or {}).get("tokenizer_path") or ""
    base = Path(__file__).resolve().parent.parent / "tools" / "tokenizer" / "deepseek_v4_tokenizer"
    return Path(p) if p else base


def exact_tokenizer():
    """加载官方 tokenizer（只尝试一次）。失败返回 None，由调用方回落到比率估算。"""
    global _TOK
    if _TOK is not None or _TOK_STATE["tried"]:
        return _TOK
    _TOK_STATE["tried"] = True
    try:
        from tokenizers import Tokenizer       # 官方 zip 依赖里自带的 C++ 实现
        path = tokenizer_path() / "tokenizer.json"
        if not path.exists():
            _TOK_STATE["why"] = f"没有 tokenizer.json：{path}"
            return None
        _TOK = Tokenizer.from_file(str(path))
        _TOK_STATE["path"] = str(path)
        return _TOK
    except Exception as exc:                   # noqa: BLE001
        _TOK_STATE["why"] = f"{type(exc).__name__}: {exc}"
        return None


def exact_count(text: str) -> int:
    """精确 token 数（官方 tokenizer）；不可用时返回 -1。"""
    tk = exact_tokenizer()
    if tk is None:
        return -1
    try:
        return len(tk.encode(text or "").ids)
    except Exception:                          # noqa: BLE001
        return -1


def mode() -> str:
    """当前用哪种计数：exact（官方 tokenizer）/ ratio（校准比率）。"""
    return "exact" if exact_tokenizer() is not None else "ratio"


def why_not_exact() -> str:
    return _TOK_STATE["why"]


def ratio() -> float:
    with _LOCK:
        return _RATIO


def estimate_tokens(text: str) -> int:
    """token 数：**优先用官方 tokenizer 精确算**，不可用才用校准比率估算。"""
    n = exact_count(text)
    if n >= 0:
        return n
    return int(len(text or "") * ratio())


def observe(prompt_chars: int, prompt_tokens: int) -> float:
    """用一次真实调用的结果校准比率。返回校准后的比率。"""
    global _RATIO
    if prompt_chars <= 0 or prompt_tokens <= 0:
        return ratio()
    # 有了官方 tokenizer，样本只用来给"无 tokenizer 时"兜底，不再驱动估算
    sample = prompt_tokens / prompt_chars
    if not (MIN_RATIO <= sample <= MAX_RATIO):
        return ratio()                            # 明显异常（比如只发了几个字）不采信
    with _LOCK:
        _SAMPLES.append(sample)
        del _SAMPLES[:-20]                        # 只保留最近 20 次
        _RATIO = sum(_SAMPLES) / len(_SAMPLES)
        return _RATIO


def fits(text: str, budget_tokens: int | None = None) -> tuple[bool, int, int]:
    """这段文本放不放得下。返回 (是否放得下, 估算 token, 预算 token)。"""
    from . import settings_store
    budget = int(budget_tokens if budget_tokens is not None
                 else (settings_store.get_remote().get("max_prompt_tokens") or 950000))
    est = estimate_tokens(text)
    return est <= budget, est, budget


def cut(text: str, budget_tokens: int = 6000, keep_tail: bool = False) -> str:
    """按 **token 预算**裁一段输入文本（不是按字符数拍脑袋），裁了会留标记。

    为什么统一走 token：这套系统里 2000 字符的中文 ≈ 1400 token，2000 字符的英文
    ≈ 500 token —— 同一个字符上限在两种文本上差 3 倍。而且实测过一次：
    `patch.py` 里 `[:12000]` 把 2000 行文件砍成前 1/5，模型只能盲改。
    上限只应该来自物理预算，不应该来自"我觉得这个数字好看"。
    """
    s = str(text or "")
    if not s or estimate_tokens(s) <= budget_tokens:
        return s
    r = max(0.2, float(ratio()))
    limit_chars = int(budget_tokens / r)
    if keep_tail:                                  # 失败证据常常在尾部
        head = int(limit_chars * 0.45)
        tail = limit_chars - head - 60
        return (s[:head] + "\n…（中间省略，按 token 预算 "
                f"{budget_tokens} 裁剪）…\n" + s[-max(0, tail):])
    return (s[:limit_chars] + "\n…（已按 token 预算 "
            f"{budget_tokens} 裁剪，原文 {estimate_tokens(s)} token）…")


def output_budget(text: str, factor: float = 1.5, floor: int = 1200,
                  ceil: int = 384000) -> int:
    """按"这次要写的东西有多大"给输出配额，而不是拍一个固定上限。

    为什么不是固定值：
      · 实测 76 行的 clean.py 整份 = 706 token（JSON 包裹 878），而整文件模式原先固定给
        32,000 —— 模型在额度里漫游，最多一次写了 24,636 token，是需要的 27 倍；
      · 另一方面大文件必须能重写：2,000 行 ≈ 18,000 token，配额随之到 ~27,000，
        不会被人造天花板挡住。

    上限只有模型的物理上限（384K）；本函数的作用是"按任务量给"，不是"砍到某个数"。
    factor 留出重构/增删辅助函数的余量；floor 保证一个函数体不会被掐得太紧。
    """
    need = estimate_tokens(text or "")
    return int(max(int(floor), min(int(ceil), need * float(factor) + 400)))


def needs_compression(text: str, budget_tokens: int | None = None,
                      headroom: float = 0.95) -> tuple[bool, dict]:
    """到没到"该压缩"的线（默认预算的 95%）。"""
    ok, est, budget = fits(text, budget_tokens)
    trigger = est >= int(budget * headroom)
    return (not ok) or trigger, {"est_tokens": est, "budget_tokens": budget,
                                 "ratio": round(ratio(), 3),
                                 "trigger": "超预算" if not ok else
                                            ("接近上限" if trigger else "正常")}

