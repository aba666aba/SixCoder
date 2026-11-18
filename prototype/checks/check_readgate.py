"""写前必读门禁自检（§8.5，2026-11-07 定稿）。

事故：`author.py` 只把"当前文件全文"注入 prompt，而且**超过 12000 字符就静默不注入**。
于是大文件每轮都在"没看过全文"的情况下被整篇重写 —— 模型顺手改坏/删掉已验收的函数，
引擎再回滚，下一轮重复。da.py 连续 6 轮同一指纹，这条是根因之一。

修法：把"看没看过全文"变成显式状态（`read_ok`）——
  · 小文件：注入全文，允许整篇重写；
  · 大文件：只注入头尾 + 明确"禁止整篇重写，只能用 patches"，
    若模型仍给整份文件 → **拦下，不落盘**，并告诉它为什么。

跑法: python checks/check_readgate.py
"""
from __future__ import annotations


def _sandbox_temp():
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import author, remote  # noqa: E402

FAILS: list[str] = []
BIG = 14000


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class _C:
    def __init__(self, raw: str = ""):
        self.raw = raw
        self.funcs: list = []


def big_src() -> str:
    parts = ['"""大文件（>12000 字符）：用来验证"没看过全文不许整篇重写"。"""\n']
    i = 0
    while sum(len(p) for p in parts) < BIG:
        parts.append(f"\n\ndef f{i}(x: int) -> int:\n"
                     f'    """第 {i} 个函数：原样返回入参。"""\n    return x\n')
        i += 1
    return "".join(parts)


def run_case(src: str, payload: str) -> tuple[dict, str, list[dict], str]:
    """跑一次 author_module，返回 (结果, 执行后文件内容, 事件, prompt 片段)。"""
    events: list[dict] = []
    prompts: list[str] = []
    orig = remote.complete

    def fake(prompt, **kw):                                   # noqa: ANN001
        prompts.append(prompt)
        return {"ok": True, "answer": payload, "usage": {"completion_tokens": 40}}

    remote.complete = fake                                    # type: ignore[assignment]
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            p = ws / "m.py"
            if src:
                p.write_text(src, encoding="utf-8")
            res = author.author_module(
                ws, "m.py", _C("- f(x: int) -> int：原样返回"), {"m.py": _C("- f(x: int) -> int：原样返回")},
                attempts=1, emit=lambda k, v: events.append({"k": k, **v}))
            after = p.read_text(encoding="utf-8") if p.exists() else ""
    finally:
        remote.complete = orig                                # type: ignore[assignment]
    return res, after, events, (prompts[0] if prompts else "")


def main() -> int:
    print("写前必读门禁自检")

    print("[1] 新文件（盘上还没有）→ 允许整篇写")
    payload_new = '{"files":[{"path":"m.py","content":"def f(x: int) -> int:\\n    return x\\n"}]}'
    r, after, ev, _ = run_case("", payload_new)
    gate = [e for e in ev if e["k"] == "read_gate"]
    check("新文件 read_ok=True", gate and gate[0]["read_ok"] is True, str(gate))
    check("新文件能正常落盘", r.get("ok") and "def f" in after, str(r.get("reason"))[:120])

    print("[2] 小文件 → 注入全文，整篇重写允许")
    small = 'def f(x: int) -> int:\n    raise NotImplementedError\n'
    payload_small = '{"files":[{"path":"m.py","content":"def f(x: int) -> int:\\n    return x + 1\\n"}]}'
    r2, after2, ev2, prompt2 = run_case(small, payload_small)
    gate2 = [e for e in ev2 if e["k"] == "read_gate"]
    check("小文件 read_ok=True", gate2 and gate2[0]["read_ok"] is True, str(gate2))
    check("小文件全文真的进了 prompt", "raise NotImplementedError" in prompt2, "没注入旧内容")
    check("小文件整篇重写能落盘", r2.get("ok") and "return x + 1" in after2, str(r2.get("reason"))[:120])

    print("[3] 大文件 → **全文给全**（不再只给头尾），整篇重写也不丢已实现函数")
    # 2026-11-09 原则变更：本地是远端的**放大器**，不为省钱掐上下文。
    # 旧行为（大文件只给头尾 + 禁止整篇重写）是**在模型看不见全文时**的替代方案，
    # 现在全文看得见了，就该给全；"别把已实现的函数弄丢"改由 `_deleted_symbols` +
    # `_restore_symbols`（被删的函数自动接回）来兜底 —— 比拦下整篇重写更不浪费。
    big = big_src()
    payload_big = '{"files":[{"path":"m.py","content":"def f(x: int) -> int:\\n    return 0\\n"}]}'
    r3, after3, ev3, prompt3 = run_case(big, payload_big)
    gate3 = [e for e in ev3 if e["k"] == "read_gate"]
    check("大文件也 read_ok=True（远端看得见全部）",
          bool(gate3) and gate3[0]["read_ok"] is True, str(gate3))
    check("没有「中间省略」标记（模型不会误以为看全了）", "中间省略" not in prompt3)
    check("整篇重写没把已实现函数弄丢（被删的自动接回）",
          after3.count("def f") >= big.count("def f"),
          "before=%d after=%d" % (big.count("def f"), after3.count("def f")))
    check("接回有留痕（author_gone_restored）",
          any(e["k"] == "author_gone_restored" for e in ev3),
          str([e["k"] for e in ev3]))

    print("[4] 大文件 + 补丁 → 允许（只改看得见的那段）")
    anchor = 'def f0(x: int) -> int:\n    """第 0 个函数：原样返回入参。"""\n    return x\n'
    payload_patch = ('{"patches":[{"path":"m.py","find":%s,"replace":%s}]}'
                     % (__import__("json").dumps(anchor),
                        __import__("json").dumps(anchor.replace("return x", "return x + 1"))))
    r4, after4, ev4, _ = run_case(big, payload_patch)
    check("补丁能通过门禁落盘", after4 != big and "return x + 1" in after4,
          str(r4.get("reason"))[:120])
    check("补丁后其它函数没被动",
          after4.count("def f") == big.count("def f"), "补丁把别的函数弄丢了")

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（新文件放行 / 小文件注入全文 / 大文件也注入全文且不丢已实现函数 / 补丁放行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
