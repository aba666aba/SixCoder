"""token 预算自检：官方 tokenizer 精确计数、比率兜底、上限口径、超了压缩不拆分。

用户口径：
    ① 上限要按**模型真实上限**（1M tokens，输入输出共用）算，不是拍的字符数；
    ② 超了**压缩**，绝不把仓库拆成两份（拆开反而增加远端错判）。

跑法: python check_tokens.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import contextpack, settings_store, tokens  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("token 预算自检")
    r = settings_store.get_remote()

    # 1) 官方 tokenizer 能不能嵌进程序（这是"精确计数"的前提）
    print("[1] 官方 tokenizer")
    mode = tokens.mode()
    check("官方 tokenizer 可用（exact 模式）", mode == "exact", f"mode={mode} {tokens.why_not_exact()}")
    if mode == "exact":
        n_cn = tokens.exact_count("写一个计算器，支持加减乘除")
        n_code = tokens.exact_count("def f(x: int) -> int:\n    return x + 1\n")
        check("中文 13 字 → 9 tokens（官方口径 ≈0.69/字）", n_cn == 9, str(n_cn))
        check("代码 39 字符 → 16 tokens（≈0.41/字符）", n_code == 16, str(n_code))
        check("空串 → 0", tokens.exact_count("") == 0, str(tokens.exact_count("")))
        # 精确值：中文 6 字 = 4 token（与 exact_count 必须一致，不许走估算）
        check("estimate_tokens 与 exact_count 一致",
              tokens.estimate_tokens("写一个计算器") == tokens.exact_count("写一个计算器"),
              f"{tokens.estimate_tokens('写一个计算器')} vs "
              f"{tokens.exact_count('写一个计算器')}")

    # 2) 上限口径
    print("[2] 上限口径（按 token，不按字符）")
    budget = int(r.get("max_prompt_tokens") or 0)
    check("预算以 token 计", budget > 0, str(budget))
    check("预算低于官方 1M（给输出留池子）", budget < 1_000_000, str(budget))
    check("给输出留了 ≥20K", 1_000_000 - budget >= 20_000, str(1_000_000 - budget))
    check("字符上限只是辅助（≥ 预算换算值）",
          int(r.get("max_prompt_chars") or 0) >= budget * 1.5, str(r.get("max_prompt_chars")))

    # 3) 压缩判断（用"真实密度"的文本，不用重复字符——BPE 会把重复压得很小）
    print("[3] 压缩判断")
    small = "写一个计算器，支持加减乘除、根号、求导、不定积分"
    ok_small, est_small, b = tokens.fits(small)
    check("小需求放得下", ok_small and est_small < b, f"{est_small}/{b}")
    dense = ("写一个计算器，支持加减乘除、根号、求导、不定积分。" * 120000)   # ≈0.69 token/字
    need, info = tokens.needs_compression(dense)
    check("约 300 万字符中文 → 要求压缩", need, str(info))
    check("判断里带估算/预算/触发原因",
          info["est_tokens"] > 0 and info["trigger"] in ("超预算", "接近上限", "正常"), str(info))

    # 4) 压缩语义：压缩而不是拆分/腰斩
    print("[4] 压缩语义")
    import inspect
    src = inspect.getsource(contextpack.pack)
    check("超限先丢非焦点完整源码", "build(True, True, False)" in src)
    check("再丢调用关系（契约与签名仍保留）", "build(False, False, False)" in src)
    check("写明不做拆分调用", "不做拆分调用" in src)
    check("默认不限长（budget_chars 默认 0）",
          inspect.signature(contextpack.pack).parameters["budget_chars"].default == 0)

    # 5) 没有 tokenizer 时回落比率估算（不许报错）
    print("[5] 兜底：没有 tokenizer 时用校准比率")
    saved = (tokens._TOK, dict(tokens._TOK_STATE))
    try:
        tokens._TOK = None
        tokens._TOK_STATE.update({"tried": True, "why": "测试强制不可用"})
        check("mode 变成 ratio", tokens.mode() == "ratio", tokens.mode())
        est = tokens.estimate_tokens("x" * 1000)
        check("比率估算生效（1000 字符 ≈ 410 token）", 300 <= est <= 500, str(est))
        check("回落时 fits 仍能给出结论", isinstance(tokens.fits("abc")[0], bool))
    finally:
        tokens._TOK, tokens._TOK_STATE = saved
        tokens._TOK_STATE.clear()
        tokens._TOK_STATE.update({"tried": False, "path": "", "why": ""})

    # 6) 空桩不许当"现有实现"喂给远端
    print("[6] 空桩源码不进上下文（否则模型会照抄桩）")
    stub_src = ('"""m。"""\n\n\ndef add(a: int, b: int) -> int:\n    """相加"""\n'
                '    raise NotImplementedError\n\n\ndef mul(a: int, b: int) -> int:\n'
                '    """相乘"""\n    return a * b\n')
    d = contextpack.module_digest("m.py", stub_src, focus=("add", "mul"))
    check("识别出空桩", d["stubs"] == ["add"], str(d["stubs"]))
    check("焦点源码里只有真实现（不含桩）", list(d["full"]) == ["mul"], str(list(d["full"])))
    check("每个函数都带 stub 标记",
          [(f["name"], f["stub"]) for f in d["funcs"]] == [("add", True), ("mul", False)],
          str([(f["name"], f["stub"]) for f in d["funcs"]]))
    import tempfile as _tf
    with _tf.TemporaryDirectory() as td:
        Path(td, "m.py").write_text(stub_src, encoding="utf-8")
        pack = contextpack.pack(Path(td), {}, paths=["m.py"], focus=("add", "mul"))
    check("包体里有『空桩…必须你自己写』的显式提示", "目前是空桩" in pack)
    check("桩函数的源码没有被当成『焦点符号完整源码』贴进去",
          "### m.py::add" not in pack, pack[:200])
    check("真实现仍然给全文（决策要用）", "### m.py::mul" in pack)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（官方 tokenizer 精确计数 / token 口径上限 / 压缩不拆分 / 比率兜底）")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
