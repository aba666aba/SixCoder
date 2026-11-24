"""C 档落点 1（§20.6）语言层"能验"自检：命令表 / 真跑 / 判据接入 / 证据等级。

为什么必须真跑（不是查表）：
    "能识别"只要 LANGS 里有条目就行；"能验"必须用该语言自己的工具链真跑一遍
    测试套件，returncode==0 才算过。工具链没装（go/cargo/php/ruby…）就如实说
    测不了，不假装通过 —— 这一条是 C 档和旧口径的分界线。

自检必须用 D:/Python/python.exe（它有 pytest）；Bash 通道的 python 是
WorkBuddy 自带 3.13、没有 pytest，会把成功路径误判成失败。
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import lang                                          # noqa: E402
from core import langtest                                      # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


def main() -> int:
    print("[1] 命令表齐全")
    test_langs = ["python", "go", "java", "javascript", "typescript", "csharp", "rust"]
    check("python/go/java/javascript/typescript/csharp/rust 的 test_cmd 非空",
          all(lang.LANGS[l]["test_cmd"] for l in test_langs))
    check("c/cpp 的 test_cmd 如实写 None（本机没测试框架）",
          lang.LANGS["c"]["test_cmd"] is None and lang.LANGS["cpp"]["test_cmd"] is None)
    check("每个语言都有 typecheck_cmd 键",
          all("typecheck_cmd" in cfg for cfg in lang.LANGS.values()))
    check("每个语言 has_tests = (test_cmd 非空)",
          all(cfg["has_tests"] == bool(cfg["test_cmd"]) for cfg in lang.LANGS.values()))

    print("[2] 命令函数：返回 argv list（不是整条字符串）")
    tc = lang.test_command("go")
    check("test_command('go') 是 list 且首元素是可执行名（不是字符串）",
          isinstance(tc, list) and not isinstance(tc, str)
          and len(tc) > 0 and isinstance(tc[0], str) and tc[0] == "go", str(tc))
    check("test_command('c') 没配测试 → None", lang.test_command("c") is None)
    check("typecheck_command('typescript') 是 list",
          isinstance(lang.typecheck_command("typescript"), list))
    # 2026-11-24 改：原来这条断言要求 JS 的类型检查命令必须是 None，
    # 但 Node 的 `node --check` 确实能做单文件语法检查，写 None 是低估了能力
    # （实测：把语法错的 .js 交给 node --check 会退出码 1 并指出行号）。
    # 断言改成本意：**只接受"如实"的两种形态** —— 要么明确 None，
    # 要么是一个真正的 argv list（不许编一条跑不通的命令）。
    _js_tc = lang.typecheck_command("javascript")
    check("typecheck_command('javascript') 如实：None 或真实 argv list",
          _js_tc is None or (isinstance(_js_tc, list) and len(_js_tc) > 0
                             and all(isinstance(x, str) for x in _js_tc)), str(_js_tc))

    print("[3] run_tests 成功路径（真跑 pytest）")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n",
                                       encoding="utf-8")
        r = lang.run_tests(ws, "python", timeout=60)
        check("pytest 能过的测试 → ok=True/how='test'/skipped=False",
              r.get("ok") is True and r.get("how") == "test" and r.get("skipped") is False,
              str({k: r.get(k) for k in ("ok", "how", "skipped", "detail")})[:200])

    print("[4] run_tests 失败路径（故意失败的测试）")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "test_fail.py").write_text("def test_fail():\n    assert 1 + 1 == 3\n",
                                         encoding="utf-8")
        r = lang.run_tests(ws, "python", timeout=60)
        d = str(r.get("detail") or "")
        check("失败 → ok=False/how='failed' 且 detail 能看到失败信息",
              r.get("ok") is False and r.get("how") == "failed"
              and ("fail" in d.lower() or "assert" in d.lower()), d[:200])

    print("[5] 超时路径（timeout=2 必然超时）")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "test_slow.py").write_text("import time\ndef test_slow():\n    time.sleep(30)\n",
                                         encoding="utf-8")
        r = lang.run_tests(ws, "python", timeout=2)
        check("必然超时的命令 → how='timeout'/ok=False",
              r.get("how") == "timeout" and r.get("ok") is False, str(r.get("how")))

    print("[6] 工具链缺失如实报告（不假装通过）")
    for l in ("go", "rust"):
        with tempfile.TemporaryDirectory() as td:
            r = lang.run_tests(Path(td), l)
            check(f"{l}: skipped=True/how='no-toolchain'/ok=False",
                  r.get("skipped") is True and r.get("how") == "no-toolchain"
                  and r.get("ok") is False, str(r.get("detail"))[:120])

    print("[7] 没配命令 → no-cmd")
    with tempfile.TemporaryDirectory() as td:
        r = lang.run_tests(Path(td), "c")
        check("c 没配 test_cmd → how='no-cmd'/skipped=True/ok=False",
              r.get("how") == "no-cmd" and r.get("skipped") is True
              and r.get("ok") is False, str(r.get("how")))

    print("[8] run_typecheck 成功路径（python compileall）")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        r = lang.run_typecheck(ws, "python", timeout=60)
        check("compileall 通过 → ok=True/how='typecheck'/skipped=False",
              r.get("ok") is True and r.get("how") == "typecheck"
              and r.get("skipped") is False,
              str({k: r.get(k) for k in ("ok", "how", "detail")})[:160])

    print("[9] checks_for 产出的 kind 正确")
    py = langtest.checks_for(Path("."), "python")
    py_kinds = [c["kind"] for c in py]
    check("python → 含 lang_test", "lang_test" in py_kinds, str(py_kinds))
    c_chk = langtest.checks_for(Path("."), "c")
    c_kinds = [c["kind"] for c in c_chk]
    check("c（test_cmd=None）→ 不含 lang_test", "lang_test" not in c_kinds, str(c_kinds))
    check("每个 check dict 都带非空 cmd",
          all(isinstance(c.get("cmd"), list) and c["cmd"] for c in py + c_chk))
    check("kinds() 返回 ['lang_test','lang_typecheck']",
          langtest.kinds() == ["lang_test", "lang_typecheck"])
    check("每个 check dict 都带 desc/hard/lang",
          all(c.get("desc") and c.get("hard") is True and c.get("lang")
              for c in py + c_chk))

    print("[10] 真跑外部语言：javascript（npm 本机有）")
    if shutil.which("npm") is None:
        print("  SKIP  npm 不可用，跳过 javascript 真跑（不算失败）")
    else:
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "package.json").write_text(
                json.dumps({"scripts": {"test": 'node -e "process.exit(0)"'}}),
                encoding="utf-8")
            r = lang.run_tests(ws, "javascript", timeout=120)
            check("package.json 的 test 脚本 exit(0) → run_tests ok=True",
                  r.get("ok") is True and r.get("how") == "test",
                  str({k: r.get(k) for k in ("ok", "how", "detail")})[:200])

    print("[11] judge_level 升级")
    check("python → 含'测试级'", "测试级" in lang.judge_level("python"),
          lang.judge_level("python"))
    check("go（无工具链）→ 仍含'无法验证'", "无法验证" in lang.judge_level("go"),
          lang.judge_level("go"))
    check("rust（无工具链）→ 仍含'无法验证'", "无法验证" in lang.judge_level("rust"),
          lang.judge_level("rust"))

    print()
    if FAILS:
        print(f"有失败项 {len(FAILS)} 个：{FAILS}")
        return 1
    print("全部通过（命令表 / 真跑测试 / 失败 / 超时 / 无工具链 / 判据接入 / 证据等级）")
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
