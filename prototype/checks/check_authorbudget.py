"""回归：night-v2a 暴露的两个自身缺陷，必须被钉住。

事故回放（`_logs/runs/night-v2a/events.jsonl`，第一次跑就抓到）：
  ① `output_budget` 按**盘上的桩骨架**（221 token）算 → 配额 1200，
     于是 11 次调用里 10 次输出恰好停在 1200（被砍断），全被判成"没给出 files"；
  ② 结构体检在**作者失败**时也照跑，于是盘上旧骨架的"还是桩"把真实原因
     （输出被截断）覆盖掉，诊断信号消失，还按错误的理由升了档。

跑法: python check_authorbudget.py
"""
from __future__ import annotations


def _sandbox_temp():
    """临时目录放进工作区（沙箱只允许写工作区内的路径）。"""
    import tempfile as _tf
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import author, remote, tokens  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class _C:
    """最小契约壳：raw 是"这个模块要实现什么"的原文。"""

    def __init__(self, raw: str):
        self.raw = raw
        self.funcs: list = []


STUB_SRC = '''"""clean 模块。"""


def normalize_age(x: str) -> int:
    """去掉首尾空格后转整数"""
    raise NotImplementedError


def clean_email(s: str) -> str:
    """去首尾空格并转小写"""
    raise NotImplementedError
'''

CONTRACT = (
    "- normalize_age(x: str) -> int：去掉首尾空格后转整数；结果须在 0..150 内，越界或无法转整数抛 ValueError。\n"
    "- clean_email(s: str) -> str：去首尾空格并转小写；必须含 '@' 且 '@' 前后均非空，否则抛 ValueError。\n"
)


def main() -> int:
    print("作者配额与被截断自愈自检")

    print("[1] 盘上是桩时，配额不能按桩算")
    calls: list[dict] = []
    orig = remote.complete

    def fake_ok(prompt, **kw):                                    # noqa: ANN001
        calls.append({"max_out": kw.get("max_out"), "prompt": prompt[:60],
                      "grew": "被截断" in prompt})
        payload = ('{"files":[{"path":"clean.py","content":"def f():\\n    return 1\\n"}]}')
        return {"ok": True, "answer": payload, "usage": {"completion_tokens": 40}}

    remote.complete = fake_ok                                      # type: ignore[assignment]
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "clean.py").write_text(STUB_SRC, encoding="utf-8")
            author.author_module(ws, "clean.py", _C(CONTRACT), {"clean.py": _C(CONTRACT)})
    finally:
        remote.complete = orig                                    # type: ignore[assignment]
    got = int(calls[0]["max_out"])
    stub_est = tokens.estimate_tokens(STUB_SRC)
    contract_est = tokens.estimate_tokens(CONTRACT)
    print(f"     桩骨架 {stub_est} token｜契约 {contract_est} token｜配额 {got}")
    check("配额不是按桩算出来的（> 桩估算的 3 倍）", got > stub_est * 3, f"{got} vs {stub_est}")
    check("配额对得起契约规模（> 契约 token × 2）", got >= contract_est * 2,
          f"{got} vs {contract_est}")
    # 2026-11-09（用户指令："为什么要限制输出，这不是限制远端能力吗，先断开这个限制"）：
    # 原来的断言是「配额依然远小于老的 32,000（不回到漫游）」——那是**旧语义**：
    # 输出按任务量掐、截断靠放大配额解决。现在输出上限**彻底断开**（恒为模型物理上限），
    # 截断改用硬信号 finish_reason=='length' 判定 + 重试。
    check("输出上限 = 模型物理上限 384000（不再按任务量掐）", got == 384000, str(got))

    print("[2] 输出被截断 → 识别出来并重试（硬信号 finish_reason）")
    calls.clear()

    def fake_trunc(prompt, **kw):                                 # noqa: ANN001
        calls.append({"max_out": kw.get("max_out"), "grew": "截断" in prompt})
        if len(calls) == 1:                                       # 第一次：被截断、内容不完整
            return {"ok": True, "answer": '{"files":[{"path":"clean.py","content":"def f():',
                    "finish_reason": "length",
                    "usage": {"completion_tokens": int(kw.get("max_out") or 0)}}
        payload = '{"files":[{"path":"clean.py","content":"def f():\\n    return 1\\n"}]}'
        return {"ok": True, "answer": payload, "finish_reason": "stop",
                "usage": {"completion_tokens": 40}}

    remote.complete = fake_trunc                                  # type: ignore[assignment]
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            r = author.author_module(ws, "clean.py", _C(CONTRACT), {"clean.py": _C(CONTRACT)})
            written = (ws / "clean.py").read_text(encoding="utf-8")
    finally:
        remote.complete = orig                                    # type: ignore[assignment]
    check("识别出截断并重试（不再靠放大配额）",
          len(calls) >= 2 and calls[1]["max_out"] == calls[0]["max_out"],
          str([c["max_out"] for c in calls]))
    check("重试时明确告知模型上一版被截断", bool(calls[1]["grew"]), str(calls[1]))
    check("重试后成功落盘", r.get("ok") and "return 1" in written, str(r.get("reason"))[:120])

    print("[3] 一直截断也不会无限放大（有上限）")
    calls.clear()

    def fake_always(prompt, **kw):                                # noqa: ANN001
        calls.append({"max_out": kw.get("max_out")})
        return {"ok": True, "answer": '{"files":[{"path":"clean.py","content":"x',
                "usage": {"completion_tokens": int(kw.get("max_out") or 0)}}

    remote.complete = fake_always                                 # type: ignore[assignment]
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            r = author.author_module(ws, "clean.py", _C(CONTRACT), {"clean.py": _C(CONTRACT)},
                                     attempts=4)
    finally:
        remote.complete = orig                                    # type: ignore[assignment]
    check("放大次数有上限（不会每次都翻倍）", len(calls) <= 4, str([c["max_out"] for c in calls]))
    check("最终还是失败（不会假装成功）", not r.get("ok"), str(r.get("reason"))[:120])

    print("[4] 思考 token 不算'正文被截断'（实测 round-01a 第 7 次栽在这）")
    calls.clear()

    def fake_think(prompt, **kw):                                 # noqa: ANN001
        calls.append({"max_out": kw.get("max_out"), "grew": "截断" in prompt})
        # completion 17 552 里 15 000 是思考，正文只有 2 552 —— 远小于配额，**完全没被截断**。
        # （测试用例必须让"正文 < 配额"：否则放大是正确行为，测不出归因 bug）
        return {"ok": True,
                "answer": '{"files":[{"path":"clean.py","content":'
                          '"def f():\\n    return 1\\n"}]}',
                "usage": {"completion_tokens": 17552, "reasoning_tokens": 15000}}

    remote.complete = fake_think                                  # type: ignore[assignment]
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            r = author.author_module(ws, "clean.py", _C(CONTRACT), {"clean.py": _C(CONTRACT)})
    finally:
        remote.complete = orig                                    # type: ignore[assignment]
    check("思考多但正文没满 → 不判截断（只调一次）", len(calls) == 1,
          str([c["max_out"] for c in calls]))
    check("不因为思考多就放大配额", not any(c["grew"] for c in calls), str(calls))
    check("这一次照常落盘成功", bool(r.get("ok")), str(r.get("reason"))[:120])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（输出上限=模型物理上限 / 截断靠 finish_reason 识别并重试 / 放大有上限）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
