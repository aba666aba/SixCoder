"""远端能力闸门自检：引擎不许为省钱掐远端的上下文/输出/反馈（2026-11-09）。

用户原则：**本地是远端的放大器，不是缰绳**。掐小 = 让远端写不出来 =
把"机制问题"记成"模型不会写"。本文件把三条闸门钉死：

  1. 输出被配额截断 → **放大配额重试**，不能记成"模型不会写"
     （实测 26 次"契约函数缺失"实为被砍断；整文件路径早有此判据，函数体路径没接）
  2. 判据反馈 = 远端唯一的失败真相 → 不掐条数、不掐字数
  3. 目标文件全文注入 → 不因超预算改成"只给头尾 + 禁止整篇重写"

跑法: python checks/check_remote_capacity.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import author  # noqa: E402
from core import iterate  # noqa: E402
from core import readplan  # noqa: E402
from core import remote as _remote_mod  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class F:
    def __init__(self, n: str):
        self.name = n
        self.params = []
        self.returns = "int"
        self.doc = ""
        self.examples = []


def main() -> int:
    print("[1] 输出被截断 → 放大配额重试（不是能力问题）")
    ws = Path(tempfile.mkdtemp())
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "m.py").write_text("def alpha(x: int) -> int:\n    return 0\n", encoding="utf-8")
    seen: list[int] = []
    events: list[tuple[str, dict]] = []

    def _fake(prompt, **kw):
        cap = int(kw.get("max_out") or 0)
        seen.append(cap)
        if len(seen) == 1:
            # 2026-11-09：截断的**硬信号**是 finish_reason='length'。
            # 原来靠 `completion_tokens == cap`（正文顶到配额）来模拟，那是数字猜测 ——
            # 而输出上限现在恒为物理上限，再也顶不到，所以数字猜测这条路已废弃。
            return {"ok": True, "answer": "```python\n# ===== alpha =====\nreturn 1\n```\n",
                    "finish_reason": "length",
                    "usage": {"completion_tokens": cap, "reasoning_tokens": 0}}
        return {"ok": True, "answer": "```python\n# ===== alpha =====\nreturn 2\n```\n",
                "finish_reason": "stop",
                "usage": {"completion_tokens": 12, "reasoning_tokens": 0}}

    _orig_complete, _orig_verify = _remote_mod.complete, author._verify
    _remote_mod.complete = _fake
    author._verify = lambda w, c, scope=None: {"ok": True, "feedback": ""}
    try:
        author.author_bodies(ws, "app/m.py", type("C", (), {"funcs": [F("alpha")]})(), {},
                             attempts=3, emit=lambda k, v: events.append((k, v)))
    finally:
        _remote_mod.complete, author._verify = _orig_complete, _orig_verify
    check("远端被调用了至少 2 次", len(seen) >= 2, str(seen))
    # 2026-11-09：输出上限已断开（恒为模型物理上限），"放大配额"这条路不存在了。
    # 正确行为：**识别出截断（finish_reason='length'）→ 重试一次**，
    # 而不是"配额变大"（变不大，也不该变）。
    check("截断后确实重试了一轮（不是同一份输出反复用）",
          len(seen) >= 2 and seen[1] == seen[0], str(seen))
    check("引擎把这次记成「截断而非能力问题」",
          any(k == "output_budget" and "截断" in str(v.get("why") or "") for k, v in events),
          str([v for k, v in events if k == "output_budget"])[:200])
    check("截断事件里带了 finish_reason 这个硬信号",
          any(k == "output_budget" and str(v.get("finish_reason") or "") == "length"
              for k, v in events),
          str([v for k, v in events if k == "output_budget"])[:200])

    print("[2] 判据反馈不掐（远端唯一的失败真相）")
    _orig_inspect = iterate.inspect
    long_msg = "契约判红：" + "细节" * 900          # 单条就 ~1800 字符
    msgs = [long_msg + f"（第{i}条）" for i in range(9)]   # 互不相同，才测得出条数
    iterate.inspect = lambda ws, c: {
        "app/m.py": {"hard_failed": [], "fails": [], "fails_all": msgs}
    }
    try:
        v = author._verify(ws, {"app/m.py": None}, scope=["app/m.py"])
    finally:
        iterate.inspect = _orig_inspect
    fb = str(v.get("feedback") or "")
    check("反馈不是空的", bool(fb))
    check("反馈没有被掐在 1200 字符", len(fb) > 1200, "长度=%d" % len(fb))
    check("9 条失败都在（没有只给前几条）", all(m in fb for m in msgs),
          "缺 %d 条" % sum(1 for m in msgs if m not in fb))

    print("[3] 目标文件全文注入（超过 12000 也不给头尾）")
    ws2 = Path(tempfile.mkdtemp())
    (ws2 / "app").mkdir(parents=True)
    big = ["def f%d() -> int:\n    return %d\n" % (i, i) for i in range(1200)]
    marker = "MARKER_IN_MIDDLE_777"
    body = "".join(big[:600]) + "def mid() -> int:\n    # " + marker + "\n    return 0\n" + "".join(big[600:])
    (ws2 / "app" / "big.py").write_text(body, encoding="utf-8")
    check("测试文件确实超过 12000 字符", len(body) > 12000, "长度=%d" % len(body))
    plan = readplan.plan("edit_existing", target="app/big.py")
    got = readplan.collect(ws2, plan, budget_chars=author.REMOTE_CTX_CHARS)
    check("全文给全了（full=True）", bool(got.get("full")), str(got.get("note")))
    check("中间那段也在（远端不是瞎的）", marker in str(got.get("text") or ""))
    check("没有「禁止整篇重写」这种束缚", "禁止整篇重写" not in str(got.get("text") or ""))

    # [4] 2026-11-09 修：整文件写作路径的输出配额**绕过了** REMOTE_OUT_FLOOR
    #      （原来写 floor=3000 if stubby else 1200），函数体路径早就是 4000。
    #      实测证据（真实夜班）：`output_budget max_out=3000` → 远端写不完 →
    #      判据红 → 下一轮同一失败证据 → heal_stop（无新信息）→ 白烧一轮钱。
    print("[4] 整文件路径的输出配额也必须走 REMOTE_OUT_FLOOR（不许绕过）")
    check("REMOTE_OUT_FLOOR 是 4000", author.REMOTE_OUT_FLOOR == 4000,
          str(author.REMOTE_OUT_FLOOR))
    src = (Path(author.__file__).parent / "author.py").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
    import re as _re
    bad = _re.findall(r"floor\s*=\s*(?:3000|1200|800)\b", code)
    check("author.py 里没有写死的 1200/3000/800 floor", not bad, str(bad))
    check("整文件路径用的是 REMOTE_OUT_FLOOR",
          _re.search(r"output_budget\(want[^)]*floor=REMOTE_OUT_FLOOR", code, _re.S) is not None)
    # 实算：给一个很小的桩文件，配额也不能低于 floor
    from core import tokens as _tk
    got_budget = _tk.output_budget("def f():\n    raise NotImplementedError\n",
                                    factor=2.0, floor=author.REMOTE_OUT_FLOOR)
    check("桩文件的配额也不低于 REMOTE_OUT_FLOOR", got_budget >= author.REMOTE_OUT_FLOOR,
          str(got_budget))

    print("[5] 输出上限彻底断开：不再按任务量掐（用户指令 2026-11-09 19:30）")
    # 用户原话：「为什么要限制输出，这不是限制远端能力吗，先断开这个限制」
    # 真相：output_budget 原来算出的数**不约束远端**（实际 max_tokens 由
    # remote.max_tokens_for 取 max(desired, safety_cap()) ≥ 128000），
    # 但它被当成了**截断判定阈值**：正文 token >= max_out*0.98 → 判"被截断" →
    # 放大重试 → 同一份失败证据 → heal_stop → 白烧钱。
    # 现在：output_budget 恒给模型物理上限；截断判定改用硬信号 finish_reason=='length'。
    check("output_budget 不再按文本长度缩放（小文本也给物理上限）",
          _tk.output_budget("x", floor=author.REMOTE_OUT_FLOOR) == _tk.output_budget(
              "x" * 100000, floor=author.REMOTE_OUT_FLOOR))
    check("输出上限 = 模型物理上限 384000",
          _tk.output_budget("x", floor=author.REMOTE_OUT_FLOOR) == 384000)
    src2 = (Path(author.__file__).parent / "author.py").read_text(encoding="utf-8")
    check("author.py 用了 finish_reason 判截断（硬信号）",
          "finish_reason" in src2 and '_fin == "length"' in src2)
    rsrc = (Path(author.__file__).parent / "remote.py").read_text(encoding="utf-8")
    check("remote.complete 回传 finish_reason 给调用方",
          "'finish_reason': str((data.get('choices')" in rsrc)
    tsrc = (Path(author.__file__).parent / "remotetools.py").read_text(encoding="utf-8")
    check("ToolLoop 回传 finish_reason", "_last_finish_reason" in tsrc)
    check("ToolLoop 支持 max_out（工具路径也不被掐）",
          "max_out: int = 0" in tsrc and "max_out=self.max_out or None" in tsrc)

    print("[6] 总 max_tokens 必须按厂商硬上限夹住（2026-11-09 HTTP 400 实锤）")
    from core import remote as _rm
    check("按厂商族定义了总 max_tokens 上限",
          set(_rm.MODEL_MAX_TOKENS) >= {"deepseek", "glm", "openai"},
          str(_rm.MODEL_MAX_TOKENS))
    for _c, _ceil in _rm.MODEL_MAX_TOKENS.items():
        for _e in ("none", "low", "high", "max"):
            _mt = _rm.max_tokens_for(384000, _e, _ceil)
            check(f"{_c}/{_e} 档的 max_tokens ≤ 厂商上限",
                  0 < _mt <= _ceil, f"{_mt} > {_ceil}")
    check("deepseek 上限 = 官方报错里的 393216",
          _rm.MODEL_MAX_TOKENS["deepseek"] == 393216)
    check("_ceiling_for 能按协议族取上限",
          _rm._ceiling_for({"compat": "glm"}) == _rm.MODEL_MAX_TOKENS["glm"])
    check("max_tokens_for 支持 ceiling 参数",
          "ceiling: int = 0" in rsrc)
    check("配额放大也不会突破厂商上限",
          "min(max(cur * 2, cur + 8000, 16000), _ceil)" in rsrc)

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（截断会放大重试 / 反馈不掐 / 全文注入）")
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
