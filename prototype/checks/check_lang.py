"""多语言支持自检：识别、工具链诚实性、零花费拒绝、C++ 用 g++ 验。

这一批全部来自真跑撞出来的事故：
  · `stats.cpp` 被判成 C → 用 gcc 验 C++ 代码，"通过"毫无意义；
  · `author._parse_reply` 里硬编码 `.endswith(".py")` → 模型给对了 Calc.java 也被静默丢掉；
  · 本机 `dotnet` 在 PATH 里但执行报 "could not be loaded" → 只看 which() 会谎报工具链齐全。

跑法: python check_lang.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import author, company, lang, remote  # noqa: E402
from core.planner import MODULE_RE  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("多语言支持自检")

    print("[1] 识别：C++ 不许被判成 C")
    for text, want in [("写 stats.cpp", "cpp"), ("ringbuf.c", "c"),
                       ("x.hpp", "cpp"), ("app.ts", "typescript"), ("Main.java", "java")]:
        paths = [m.group(1) for m in MODULE_RE.finditer(text)]
        got = lang.detect(text, paths)
        check(f"{text} → {want}", got["lang"] == want, f"得到 {got['lang']}（paths={paths}）")

    print("[2] 工具链诚实性：装不上/跑不起来都要算缺，且**不许撒谎**")
    # dotnet 这条以前是"必须判成不可用"——那是**本机**的事实（这台机器上 dotnet 在 PATH
    # 但执行失败）。GitHub 的 ubuntu/windows runner 上 dotnet 是能用的，于是 CI 两个 OS
    # 一起红。正确口径：结论必须与"真的跑一次 dotnet --version"一致。
    import shutil as _shutil
    import subprocess as _sp

    def _really_works(cmd: str) -> bool:
        if not _shutil.which(cmd):
            return False
        try:
            r = _sp.run([cmd, "--version"], capture_output=True, text=True, timeout=30)
            return r.returncode == 0
        except Exception:                                # noqa: BLE001
            return False

    real_dotnet = _really_works("dotnet")
    check("dotnet 的判定与真实执行结果一致（跑不起来就算缺）",
          bool(lang.available("dotnet")) == real_dotnet,
          f"判定={lang.available('dotnet')} 实测={real_dotnet}")

    # 工具链可用性同理：**装了的不能说不可用**；没装的把"真编译"断言标成测不了。
    tools = ("gcc", "g++", "javac", "node")
    truth = {c: bool(_shutil.which(c)) for c in tools}
    claim = {c: bool(lang.available(c)) for c in tools}
    lies = {c: (truth[c], claim[c]) for c in tools if truth[c] and not claim[c]}
    check("装了的工具链不许被判成不可用（诚实性）", not lies, str(lies))
    missing = [c for c in tools if not truth[c]]
    have = [c for c in tools if truth[c]]
    if missing:
        print(f"  SKIP  本机缺工具链：{'、'.join(missing)} ——"
              f" 这些语言的'真编译'断言测不了（不算通过，也不算失败）")
    if have:
        print(f"  NOTE  本机可用：{'、'.join(have)}（下面按可用项验证真编译）")

    print("[3] C++ 必须用 g++ 验（不是 gcc）")
    if truth["g++"]:
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "stats.cpp").write_text(
                '#include <vector>\n'
                'double mean_sum(std::vector<double> xs) { return xs.empty() ? 0.0 : 1.0; }\n',
                encoding="utf-8")
            v = lang.verify(ws, "stats.cpp", "cpp")
            check("C++ 验证走 g++", "g++" in str(v.get("how")), str(v))
            check("这段 C++ 真通过", bool(v.get("ok")), str(v)[:200])
    else:
        print("  SKIP  没有 g++，C++ 真编译测不了（不是通过）")

    print("[4] 回复解析：非 .py 也要接受，但仍然拒绝越界路径")
    reply = '{"files":[{"path":"Calc.java","content":"public class Calc {}"}]}'
    files, _, _ = author._parse_reply(reply)
    check("java 文件被接受", len(files) == 1 and files[0]["path"] == "Calc.java", str(files)[:120])
    bad = '{"files":[{"path":"../evil.py","content":"x=1"}]}'
    files_bad, _, _ = author._parse_reply(bad)
    check("越界路径被拒绝", not files_bad, str(files_bad)[:120])
    weird = '{"files":[{"path":"notes.txt","content":"hi"}]}'
    files_txt, _, _ = author._parse_reply(weird)
    check("非代码文件（.txt）被拒绝", not files_txt, str(files_txt)[:120])

    print("[5] 缺工具链：开跑前拒绝、零花费、写清装什么")
    # 不能靠"本机恰好没装 go"来触发这条路径：GitHub 的 ubuntu 镜像自带 Go，
    # 于是这条断言在不同机器上时红时绿（CI 就是这么红的）。这里**注入**缺失条件。
    _orig_available = lang.available
    lang.available = lambda name: False if str(name).lower() in ("go", "golang") \
        else _orig_available(name)
    try:
        # **不能用"全局用量表差"来判零花费**：这台机器上可能同时跑着别的真实任务
        # （实测 2026-11-06：我一边跑全自主验证，一边跑自检 → 这条断言假红，
        # 排查半小时才发现是并发污染）。改成拦死远端入口：被调用一次就记下来。
        _hits = []
        _orig_complete = remote.complete

        def _trap(*a, **kw):
            _hits.append(kw.get("task_id") or "?")
            return {"ok": False, "error": "自检拦截：不该有远端调用", "answer": ""}
        remote.complete = _trap
        try:
            with _sandbox_temp() as td:
                ws = Path(td)
                res = company.night(ws, "1) main.go：\n   - func Add(a int, b int) int：两数相加",
                                    "qwen2.5-coder:7b", use_remote=True)
                files = sorted(p.name for p in ws.iterdir() if p.suffix in (".go", ".py"))
        finally:
            remote.complete = _orig_complete
    finally:
        lang.available = _orig_available
    check("Go 任务被拒绝（没有静默生成）", not files, str(files))
    check("一次远端都没调（零花费）", not _hits, f"{len(_hits)} 次：{_hits[:3]}")
    check("理由里写清了缺什么、装什么",
          "go" in res.reason.lower() and "装" in res.reason, res.reason[:200])
    check("验收数为 0（不冒充通过）", res.verified == 0, str(res.verified))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          f"（识别 / 工具链诚实 / C++ 用 g++ / 回复解析 / 拒绝零花费）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
