"""精修模式「运行与调试」自检（2026-11-20）。

钉住的能力（都是实测踩过的坑，不许退化）：
  1. 语言自动识别 + 按语言选真实工具链（Python 用 sys.executable，其余用 recon path）；
  2. cwd 智能判断：向上找项目标志，找不到用文件目录；不跑出仓库；
  3. 真执行：Python/JS/C 真跑出正确输出，编译型先编译到 .sixcoder/run；
  4. 诊断解析：file:line 错误可定位；
  5. Python 断点会话：断点命中 / 单步 / 看变量 / 继续 / 停止；
  6. 非 Python 明确拒绝调试（不假装能单步）；
  7. 工具链探测带 60s 缓存（否则界面按钮延迟几秒才可用 —— 实测踩过）；
  8. 安全：命令用 list 不用 shell、路径限仓库内、超时杀进程树。

跑法: python checks/check_coderun.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import coderun  # noqa: E402

FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:240]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 语言识别 + 工具链选择")
    for ext, lang in ((".py", "python"), (".js", "javascript"), (".ts", "typescript"),
                      (".java", "java"), (".go", "go"), (".c", "c"), (".cpp", "cpp"),
                      (".php", "php"), (".rb", "ruby"), (".rs", "rust")):
        sp = coderun.spec_for("x" + ext)
        ck(f"{ext} 识别为 {lang}", bool(sp) and sp["lang"] == lang, str(sp))
    ck("不认识的后缀不支持", coderun.spec_for("x.xyz") is None)
    tc = coderun.available_tools()
    ck("工具链探测返回字典", isinstance(tc, dict) and "python" in tc, str(list(tc)[:6]))
    ck("Python 用当前解释器（自带 Python）",
       tc.get("python", {}).get("path") == sys.executable or coderun._tool_path("python", tc) == sys.executable)

    print("[2] cwd 智能判断")
    with tempfile.TemporaryDirectory(prefix="cr_cwd_") as td:
        ws = Path(td)
        (ws / "proj").mkdir()
        (ws / "proj" / "package.json").write_text("{}", encoding="utf-8")
        (ws / "proj" / "src").mkdir()
        f = ws / "proj" / "src" / "a.js"
        f.write_text("console.log(1)", encoding="utf-8")
        ck("有 package.json → 用项目根", coderun.find_root(ws, f) == ws / "proj",
           str(coderun.find_root(ws, f)))
        plain = ws / "solo.py"
        plain.write_text("x=1", encoding="utf-8")
        ck("无标志 → 用文件目录", coderun.find_root(ws, plain) == ws)
        outside = Path(tempfile.gettempdir()) / "outside_cr.py"
        outside.write_text("x=1", encoding="utf-8")
        ck("仓库外文件 → 退回仓库根（不跑出去）", coderun.find_root(ws, outside) == ws)

    print("[2b] Python 解释器：项目 .venv 优先（真实依赖不在引擎解释器里）")
    with tempfile.TemporaryDirectory(prefix="cr_venv_") as td:
        ws = Path(td)
        (ws / "a.py").write_text("print(42)\n", encoding="utf-8")
        fake = ws / ".venv" / "Scripts" / "python.exe"
        fake.parent.mkdir(parents=True)
        fake.write_bytes(b"")
        build = coderun.build_command(ws, "a.py", {})
        ck("venv 存在时运行命令用它", build.get("ok") and build["cmd"][0] == str(fake),
           str(build.get("cmd")))
        ck("_python_for 也认 venv", coderun._python_for(ws) == str(fake), coderun._python_for(ws))
        (ws / ".venv").rename(ws / "no_venv")
        ck("无 venv 退回引擎解释器", coderun._python_for(ws) == sys.executable,
           coderun._python_for(ws))

    print("[3] 真执行（Python / JS / C 编译运行）")
    with tempfile.TemporaryDirectory(prefix="cr_run_") as td:
        ws = Path(td)
        (ws / "a.py").write_text("print(1+2)\n", encoding="utf-8")
        (ws / "b.js").write_text("console.log(4*5)\n", encoding="utf-8")
        (ws / "c.c").write_text('#include <stdio.h>\nint main(){printf("7");return 0;}\n', encoding="utf-8")
        r = coderun.run_program(ws, "a.py", tc_map=tc)
        ck("Python 真跑出 3", r["ok"] and r["stdout"].strip() == "3" and r["returncode"] == 0,
           str(r)[:200])
        r = coderun.run_program(ws, "b.js", tc_map=tc)
        ck("JS 真跑出 20", r["ok"] and r["stdout"].strip() == "20", str(r)[:200])
        if tc.get("gcc", {}).get("ok"):
            r = coderun.run_program(ws, "c.c", tc_map=tc)
            ck("C 先编译再运行，输出 7", r["ok"] and r["stdout"].strip() == "7", str(r)[:200])
            ck("编译产物落在 .sixcoder/run（不污染源码）",
               (ws / ".sixcoder" / "run" / "c.exe").exists() or
               (ws / ".sixcoder" / "run" / "c").exists())
        else:
            print("  SKIP  C 编译（本机无 gcc）")
        # 诊断解析
        diag = coderun.parse_diagnostics(f'{ws}/bad.py:12:5: error: bad token', ws)
        ck("解析 file:line 诊断可点跳",
           len(diag) == 1 and diag[0]["line"] == 12 and diag[0]["col"] == 5, str(diag))
        # 超时护栏
        (ws / "loop.py").write_text("while True: pass\n", encoding="utf-8")
        r = coderun.run_program(ws, "loop.py", timeout=3, tc_map=tc)
        ck("死循环超时被杀（不挂住）", r.get("timed_out") is True, str(r)[:160])

    print("[4] Python 断点调试会话")
    with tempfile.TemporaryDirectory(prefix="cr_dbg_") as td:
        ws = Path(td)
        (ws / "s.py").write_text("x=1\ny=2\nprint(x+y)\n", encoding="utf-8")
        r = coderun.start_debug(ws, "s.py", breakpoints=[3])
        ck("启动调试会话成功", r.get("ok") is True, str(r))
        sid = r.get("id") or ""
        s = coderun.get_session(sid)
        ck("能拿到会话对象", s is not None)
        if s:
            time.sleep(1.2)
            ev = s.drain(200)
            paused = [e for e in ev if e.get("event") == "paused"]
            ck("断点命中第 3 行", len(paused) == 1 and paused[0]["line"] == 3, str(paused)[:200])
            ck("暂停时能看到局部变量 x=1 y=2",
               bool(paused) and paused[0]["locals"].get("x") == "1"
               and paused[0]["locals"].get("y") == "2", str(paused)[:200])
            s.send("locals", exprs=["x+y"])
            time.sleep(0.4)
            lv = [e for e in s.drain(50) if e.get("event") == "locals"]
            ck("表达式求值 x+y=3",
               bool(lv) and lv[-1].get("exprs", {}).get("x+y") == "3", str(lv)[:200])
            s.send("continue")
            time.sleep(0.6)
            ev2 = s.drain(200)
            ck("继续后程序输出 sum 3",
               any(e.get("event") == "output" and "3" in (e.get("text") or "") for e in ev2),
               str(ev2)[:200])
            ck("继续后正常结束", any(e.get("event") == "ended" for e in ev2), str(ev2)[:200])
            coderun.stop_debug(sid)
        # 非 Python 拒绝
        (ws / "x.js").write_text("console.log(1)", encoding="utf-8")
        r = coderun.start_debug(ws, "x.js")
        ck("非 Python 明确拒绝调试（不假装单步）",
           r.get("ok") is False and "只支持 Python" in (r.get("reason") or ""), str(r))

    print("[5] 工具链缓存（界面按钮不延迟）")
    a = coderun.available_tools()
    t0 = time.time()
    coderun.available_tools()
    ck("二次调用走缓存（<0.05s）", time.time() - t0 < 0.05, f"{time.time() - t0:.3f}s")
    src = Path(coderun.__file__).read_text(encoding="utf-8")
    ck("命令用 list 不用 shell=True", "shell=True" not in src)
    ck("有进程树清理", "_kill_tree" in src)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
