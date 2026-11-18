"""C 档落点 1 补强：多语言开发通路自检（远端写 → 本地编译 → 真跑测试 → 如实计入）。

用户新要求（2026-11-11）：「最好可以继续开展其他语言的开发工作，远端会写、本地也有编译器」。
本自检钉死四件事：
  1. 远端交**多个文件**（主文件 + 测试文件）时，全部落盘 —— 旧版 `or files[:1]` 只留 1 个；
  2. 提示词明确要求测试文件、不再说"不要写测试文件"；
  3. `_night_foreign` 真跑该语言的测试套件，测试绿 → `ok=True/verified>0`；
     测试没绿 / 工具链没装 → 如实停在编译级，不冒充通过；
  4. 缺工具链时错误信息点名"缺什么"，不许报成"文件不存在"（指错方向的血泪）。

用 monkeypatch 模拟远端（不花钱），用本机真 javac / 真 npm 做编译。
"""
import ast
import json
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import company as CO                                    # noqa: E402
from core import lang as LG                                       # noqa: E402
from core import remote as RM                                     # noqa: E402


def _code_only(path: Path) -> str:
    """剥掉 docstring 再给文本断言用。

    2026-11-11 踩坑（开发文档 §9.47 同款）：docstring 里写了
    「旧版 `or files[:1]`」和「旧版明令"不要写测试文件"」，
    纯 `"xxx" in src` 反向断言会把**描述旧行为的注释**当成现行代码 → 假红。
    AST 找出每个 docstring 的行范围抹掉，断言才看的是真代码。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    drop: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            d = body[0]
            drop.update(range(d.lineno, (d.end_lineno or d.lineno) + 1))
    return "\n".join("" if i + 1 in drop else ln
                     for i, ln in enumerate(src.splitlines()))


FAILS = []
N = 0


def check(name, cond, detail=""):
    global N
    N += 1
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


print("[1] 测试文件命名（各语言自带约定，不引第三方框架）")

check("Go → *_test.go", CO._test_name("main.go", "go") == "main_test.go")
check("Java → Test*.java", CO._test_name("Foo.java", "java") == "TestFoo.java")
check("TS → *.test.ts", CO._test_name("app.ts", "typescript") == "app.test.ts")
check("Python → test_*.py", CO._test_name("calc.py", "python") == "test_calc.py")
check("未知语言按后缀兜底", CO._test_name("x.kt", "kotlin").endswith(".kt"))

print("[2] 提示词与源码级接线断言")

CSRC = _code_only(Path(__file__).resolve().parents[1] / "core" / "company.py")
check("提示词要求写测试文件", "要写测试文件" in CSRC)
check("提示词不再禁止测试文件", "不要写测试文件" not in CSRC)
check("多个文件全量落盘（不再只留 1 个）",
      "for f in files:" in CSRC and "or files[:1]" not in CSRC)
check("结果段真跑 run_tests", "tests = _lang.run_tests(ws, lang)" in CSRC)
check("结果段真跑 run_typecheck", "typecheck = _lang.run_typecheck(ws, lang)" in CSRC)
check("测试绿才计入验收", "suite_green = bool(tests.get(\"ok\"))" in CSRC)
check("NightResult 如实返回 verified", "verified=len(ok_files) if passed else 0" in CSRC)
check("emit foreign_suite 事件可观测", 'emit("foreign_suite"' in CSRC)
lsrc = _code_only(Path(__file__).resolve().parents[1] / "core" / "lang.py")
check("Go 有 go.mod 时真编译", '"go", "build", "./..."' in lsrc)
check("c/cpp 类型检查补源文件", "def _typecheck_sources" in lsrc)
check("verify 先查工具链再查文件",
      lsrc.index("缺工具链") < lsrc.index('f"{rel} 不存在"'))

print("[3] verify 错误口径（缺工具链不许报成文件不存在）")

v = LG.verify(Path(tempfile.mkdtemp()), "x.go", "go")
check("缺 Go 时点名缺 go", v.get("how") == "no-tool" and "go" in v.get("detail", ""),
      str(v)[:140])
check("缺工具链时 detail 不含'不存在'", "不存在" not in v.get("detail", ""),
      str(v)[:140])

print("[4] 本地真编译：javac 能验 Java 源码")

ws = Path(tempfile.mkdtemp(prefix="sixcoder-java-"))
java_ok = textwrap.dedent("""
    public class Adder {
        public static int add(int a, int b) { return a + b; }
    }
    """)
(ws / "Adder.java").write_text(java_ok, encoding="utf-8")
r = LG.verify(ws, "Adder.java", "java")
check("合法 Java 真编译通过", r.get("ok") is True and "javac" in str(r.get("how")),
      str(r)[:160])
(ws / "Broken.java").write_text("public class Broken { this is not java }", encoding="utf-8")
r2 = LG.verify(ws, "Broken.java", "java")
check("坏 Java 源码被 javac 判红", r2.get("ok") is False, str(r2)[:160])

print("[5] c/cpp 类型检查：源文件自动补齐（不假红）")

check("c 的 typecheck_cmd 存在", LG.typecheck_command("c") is not None)
wc = Path(tempfile.mkdtemp(prefix="sixcoder-c-"))
(wc / "add.c").write_text("int add(int a, int b) { return a + b; }\n", encoding="utf-8")
srcs = LG._typecheck_sources(wc, "c")
check("扫到 .c 源文件", len(srcs) == 1 and srcs[0].endswith("add.c"), str(srcs))
(wc / ".sixcoder").mkdir(exist_ok=True)
(wc / ".sixcoder" / "junk.c").write_text("not c at all !!!", encoding="utf-8")
check("跳过 .sixcoder 里的临时文件",
      all(".sixcoder" not in s for s in LG._typecheck_sources(wc, "c")))
if LG.available("gcc"):
    rc = LG.run_typecheck(wc, "c")
    check("合法 C 真过 gcc -fsyntax-only", rc.get("ok") is True, str(rc)[:180])
else:
    print("  SKIP  gcc 未安装，如实跳过真编译")

print("[6] 端到端：模拟远端交 2 个文件 → 全量落盘 → 真跑测试 → 如实计入")

# 用 python 语言跑这条链（本机 pytest 可用，且 lang.verify 对 python 没有分支，
# 会返回 unsupported —— 所以这里改用 javascript：node --check 单文件验证，
# npm test 全仓真跑。两者本机都有。）
ws2 = Path(tempfile.mkdtemp(prefix="sixcoder-js-"))
main_js = "function add(a, b) { return a + b; }\nmodule.exports = { add };\n"
test_js = "const { add } = require('./calc.js');\nif (add(2, 3) !== 5) process.exit(1);\n"
pkg = {"name": "sixcoder-js-probe", "version": "1.0.0",
       "scripts": {"test": "node calc.test.js"}}
(ws2 / "package.json").write_text(json.dumps(pkg), encoding="utf-8")

calls = []


def fake_complete(body, **kw):
    calls.append(body)
    return {"ok": True, "by": "mock-remote",
            "answer": json.dumps({"files": [
                {"path": "calc.js", "content": main_js},
                {"path": "calc.test.js", "content": test_js}]}, ensure_ascii=False)}


orig = RM.complete
RM.complete = fake_complete
try:
    res = CO.night(ws2, "1) calc.js：\n   - add(a, b) -> int：两数相加",
                   "mock-model", use_remote=True)
finally:
    RM.complete = orig

check("远端被调用（且不写死 .py 单文件）", len(calls) >= 1, str(len(calls)))
check("提示词里点名了测试文件", "calc.test.js" in (calls[0] if calls else ""),
      (calls[0] if calls else "")[:0])
check("两个文件都落盘（旧版只留 1 个）",
      (ws2 / "calc.js").is_file() and (ws2 / "calc.test.js").is_file(),
      str(sorted(p.name for p in ws2.iterdir() if p.is_file())))
check("主文件内容正确", (ws2 / "calc.js").read_text(encoding="utf-8").strip() == main_js.strip())
suite = LG.run_tests(ws2, "javascript")
check("本地 npm test 真跑绿", suite.get("ok") is True, str(suite)[:200])
check("夜班结果如实计入（测试绿 → ok/verified）",
      res.ok is True and res.verified >= 1,
      f"ok={res.ok} verified={res.verified} reason={res.reason[:160]}")
check("理由里写了类型/测试证据口径", "测试套件" in res.reason, res.reason[:200])

print("[7] 失败路径：测试红 → 不许冒充通过")

ws3 = Path(tempfile.mkdtemp(prefix="sixcoder-js-fail-"))
(ws3 / "package.json").write_text(json.dumps(
    {"name": "p", "version": "1.0.0",
     "scripts": {"test": "node -e \"process.exit(1)\""}}), encoding="utf-8")
(ws3 / "calc.js").write_text(main_js, encoding="utf-8")
(ws3 / "calc.test.js").write_text("process.exit(1);\n", encoding="utf-8")

RM.complete = fake_complete
try:
    res3 = CO.night(ws3, "1) calc.js：\n   - add(a, b) -> int：两数相加",
                    "mock-model", use_remote=True)
finally:
    RM.complete = orig
check("测试红 → ok=False", res3.ok is False,
      f"ok={res3.ok} verified={res3.verified}")
check("测试红 → verified=0（不冒充）", res3.verified == 0, str(res3.verified))
check("理由点名未计入验收通过", "未计入验收通过" in res3.reason, res3.reason[:200])

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
    raise SystemExit(1)
print(f"全部通过（{N} 条断言：多文件落盘 / 真编译 / 真测试 / 如实计入 / 失败不冒充）")
