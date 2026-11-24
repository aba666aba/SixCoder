"""诊断回路接入主线自检（2026-11-09）。

钉住的事：
  1. 诊断工具能看见 .sixcoder 里的事实（web_routes / blueprint / 契约）——
     旧 _grep 把 .sixcoder 排除，远端连事实都读不到，谈不上自行找问题；
  2. diagnose_fail() 存在、只读工具往返、instruction 要求「只诊断不改代码」
     （诊断/实现双角色分离）；
  3. 远端异常时降级不抛（不吞异常：emit 留痕）；
  4. **接入主线**：author_bodies 第一次判据失败后，诊断结论必须进入下一轮 prompt。

跑法: python checks/check_diagnose_wire.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import author, remotetools as rtl  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _workspace(d: str) -> Path:
    ws = Path(d)
    (ws / ".sixcoder").mkdir()
    (ws / ".sixcoder" / "web_routes.json").write_text(
        '{"app/api/auth.py":[{"method":"POST","path":"/api/v1/auth/login"}]}',
        encoding="utf-8")
    (ws / "app" / "api").mkdir(parents=True)
    (ws / "app" / "api" / "auth.py").write_text(
        "def f():\n    return 0\n", encoding="utf-8")
    return ws


def main() -> int:
    print("诊断回路接入主线自检")
    with tempfile.TemporaryDirectory() as d:
        ws = _workspace(d)

        print("[1] 诊断工具能看见 .sixcoder 里的事实")
        g = rtl._grep(ws, "auth/login", ".sixcoder")
        check("grep 能搜 .sixcoder（旧实现排除了它）",
              "web_routes.json" in g, g[:110])

        print("[2] diagnose_fail 存在且能跑只读往返")
        diag = getattr(rtl, "diagnose_fail", None)
        check("diagnose_fail 已实现", callable(diag), "属性不存在")
        if not callable(diag):
            print("\n有失败项：" + ", ".join(FAILS))
            return 1

        events: list[tuple] = []
        seen_instruction: list[str] = []

        def fake(instruction, messages):
            seen_instruction.append(instruction)
            n = sum(1 for m in messages if m.get("role") == "assistant")
            if n == 0:
                return '<tool name="grep" pattern="auth/login" path=".sixcoder" />'
            return "诊断：web_routes.json 有该端点，但 auth.py 只有 parse_ 前缀函数，接线层会否决。"

        got = diag(ws, "app/api/auth.py", "第1步 POST /api/v1/auth/login → 404",
                   emit=lambda k, v: events.append((k, v)), complete_fn=fake)
        check("返回诊断结论", "parse_" in str(got), str(got)[:110])
        check("工具结果确实回灌（≥2 轮）", len(seen_instruction) >= 2,
              str(len(seen_instruction)))
        check("emit 留痕", any(k == "tool_loop" for k, _ in events), str(events))
        check("instruction 要求只诊断不改代码（角色分离）",
              "不要改" in seen_instruction[0] or "只诊断" in seen_instruction[0],
              seen_instruction[0][:110])

        print("[3] 远端异常时降级，不吞异常也不中断写作")

        def boom(instruction, messages):
            raise RuntimeError("remote down")

        events2: list[tuple] = []
        try:
            out = diag(ws, "app/api/auth.py", "判据 0/2",
                       emit=lambda k, v: events2.append((k, v)), complete_fn=boom)
            check("异常时返回空而非抛出", not str(out).strip(), str(out)[:80])
            check("异常有留痕（不是静默吞掉）", len(events2) > 0, str(events2))
        except Exception as exc:                       # noqa: BLE001
            check("异常时返回空而非抛出", False, type(exc).__name__)

        print("[4] 接入主线：失败后诊断结论进入下一轮 prompt")
        import core.remote as _remote
        orig_complete, orig_verify = _remote.complete, author._verify
        prompts: list[str] = []
        body_seq = [
            '```python\n# ===== f =====\nreturn 1\n```',
            '```python\n# ===== f =====\nreturn 2\n```',
        ]

        def fake_complete(prompt, **kw):
            if not kw.get("node_id"):   # 诊断角色：写作轮带 node_id=bodies:<path>                  # ToolLoop 的诊断调用
                return {"ok": True, "answer": "诊断结论：函数名被接线层否决",
                        "usage": {}}
            prompts.append(prompt)
            return {"ok": True,
                    "answer": body_seq.pop(0) if body_seq else
                    '```python\n# ===== f =====\nreturn 3\n```',
                    "usage": {}}

        def fake_verify(ws_, contracts, scope=None):
            return {"ok": False, "feedback": "判据 0/2 通过"}

        class _F:
            name, params, returns, doc = "f", [], "int", "x"

        class _C:
            funcs = [_F()]

        _remote.complete, author._verify = fake_complete, fake_verify
        try:
            author.author_bodies(ws, "app/api/auth.py", _C(), {}, attempts=2,
                                 emit=lambda k, v: None)
            check("至少发起两轮写作", len(prompts) >= 2, str(len(prompts)))
            check("第二轮 prompt 带诊断结论",
                  any("诊断结论" in p for p in prompts[1:]),
                  str([p[:60] for p in prompts[1:]]))
        except Exception as exc:                       # noqa: BLE001
            check("author_bodies 未因诊断回路中断", False,
                  f"{type(exc).__name__}: {exc}")
        finally:
            _remote.complete, author._verify = orig_complete, orig_verify

        print("[5] 输出解析失败也要触发诊断（真实夜班 10:12 暴露的缺口）")
        # 实测日志：order_failed「第 303 行语法错误 '(' was never closed」而
        # diagnose_done=0 —— 诊断只接在 verdict 失败路径，解析失败走 continue 直接返回。
        _orig_compile = author._compile_ok
        _diag_calls = [0]
        _orig_complete2, _orig_verify2 = _remote.complete, author._verify
        author._compile_ok = lambda code: (False, "'(' was never closed")

        def fake_complete2(prompt, **kw):
            if not kw.get("node_id"):   # 诊断角色：写作轮带 node_id=bodies:<path>
                _diag_calls[0] += 1
                return {"ok": True, "answer": "诊断结论：括号未闭合，检查围栏代码",
                        "usage": {}}
            return {"ok": True,
                    "answer": '```python\n# ===== f =====\nreturn 1\n```',
                    "usage": {}}

        _remote.complete = fake_complete2
        author._verify = lambda w, c, scope=None: {"ok": True, "feedback": ""}
        try:
            res = author.author_bodies(ws, "app/api/auth.py", _C(), {}, attempts=2,
                                       emit=lambda k, v: None)
            check("解析失败路径返回失败", not res.get("ok"), str(res)[:90])
            check("解析失败也触发了诊断", _diag_calls[0] == 1, str(_diag_calls[0]))
            check("诊断结论进了 reason",
                  "诊断结论" in str(res.get("reason") or ""),
                  str(res.get("reason"))[:120])
        except Exception as exc:                       # noqa: BLE001
            check("解析失败路径不中断", False, f"{type(exc).__name__}: {exc}")
        finally:
            author._compile_ok = _orig_compile
            _remote.complete, author._verify = _orig_complete2, _orig_verify2

        print("[6] 语法错误的行号属于「未落盘代码」，诊断必须拿到那一段")
        # 实测 2026-11-09 10:12：order_failed「第 303 行 '(' was never closed」，
        # 但 _compile_ok 失败后直接 continue，没有 write_text → 磁盘是旧版。
        # 远端拿行号去 read_file 会读到另一份代码。必须把内存版本的上下文直接给它。
        (ws / "app/api/auth.py").write_text(
            "def f():\n    return 0\n"
            + "".join(f"# line {i}\n" for i in range(3, 311)), encoding="utf-8")
        _orig_compile3 = author._compile_ok
        _orig_rebuild3 = author.rebuild_partial
        _orig_complete3, _orig_verify3 = _remote.complete, author._verify
        # 模拟真实场景：拼接后的代码有 300+ 行，第 303 行括号未闭合，且这份代码没写盘
        _long_code = "".join(f"line_{i} = {i}\n" for i in range(1, 311))
        _long_code = _long_code.replace("line_303 = 303\n", "line_303 = (303\n")
        author.rebuild_partial = lambda *a, **k: (_long_code, [])
        author._compile_ok = lambda code: (False, "第 303 行语法错误：'(' was never closed")
        seen6: list[str] = []

        def fake_complete3(prompt, **kw):
            if not kw.get("node_id"):   # 诊断角色：写作轮带 node_id=bodies:<path>
                seen6.append(str(prompt))
                return {"ok": True, "answer": "诊断结论：303 行括号未闭合", "usage": {}}
            return {"ok": True,
                    "answer": '```python\n# ===== f =====\nreturn 1\n```',
                    "usage": {}}

        _remote.complete, author._verify = fake_complete3, (
            lambda w, c, scope=None: {"ok": True, "feedback": ""})
        try:
            author.author_bodies(ws, "app/api/auth.py", _C(), {}, attempts=1,
                                 emit=lambda k, v: None)
            blob6 = "\n".join(seen6)
            check("诊断 prompt 带第 303 行上下文", "303|" in blob6, blob6[:160])
            check("明确标注这是未落盘版本（别去读磁盘）",
                  "未落盘" in blob6 or "尚未写盘" in blob6, blob6[:160])
            check("带行号而不是裸代码", "298|" in blob6 or "300|" in blob6,
                  blob6[:160])
        except Exception as exc:                       # noqa: BLE001
            check("未落盘上下文路径不中断", False, f"{type(exc).__name__}: {exc}")
        finally:
            author._compile_ok = _orig_compile3
            author.rebuild_partial = _orig_rebuild3
            _remote.complete, author._verify = _orig_complete3, _orig_verify3

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（事实可见 / 只读往返 / 角色分离 / 异常降级 / 接入主线 / "
          "解析失败也诊断 / 行号上下文）")
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
