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

    print("[2] 工具链诚实性：装不上/跑不起来都要算缺")
    dotnet_ok = lang.available("dotnet")
    check("dotnet 被判定为不可用（PATH 里有但执行失败）", not dotnet_ok, "dotnet 竟被评为可用")
    check("gcc/g++/javac/node 可用（本机真实环境）",
          all(lang.available(c) for c in ("gcc", "g++", "javac", "node")),
          str({c: lang.available(c) for c in ("gcc", "g++", "javac", "node")}))

    print("[3] C++ 必须用 g++ 验（不是 gcc）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "stats.cpp").write_text(
            '#include <vector>\n'
            'double mean_sum(std::vector<double> xs) { return xs.empty() ? 0.0 : 1.0; }\n',
            encoding="utf-8")
        v = lang.verify(ws, "stats.cpp", "cpp")
        check("C++ 验证走 g++", "g++" in str(v.get("how")), str(v))
        check("这段 C++ 真通过", bool(v.get("ok")), str(v)[:200])

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
    before = remote.usage_summary("")
    with _sandbox_temp() as td:
        ws = Path(td)
        res = company.night(ws, "1) main.go：\n   - func Add(a int, b int) int：两数相加",
                            "qwen2.5-coder:7b", hours=0.01, use_remote=True, max_cny=0.05)
        files = sorted(p.name for p in ws.iterdir() if p.suffix in (".go", ".py"))
    after = remote.usage_summary("")
    calls = int(after["calls"]) - int(before["calls"])
    check("Go 任务被拒绝（没有静默生成）", not files, str(files))
    check("一次远端都没调（零花费）", calls == 0, f"{calls} 次")
    check("理由里写清了缺什么、装什么",
          "go" in res.reason.lower() and "装" in res.reason, res.reason[:200])
    check("验收数为 0（不冒充通过）", res.verified == 0, str(res.verified))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          f"（识别 / 工具链诚实 / C++ 用 g++ / 回复解析 / 拒绝零花费）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
