"""自检：契约声明会抛异常的输入，不许再被"崩溃判据"打红；docstring 不许被切断。

背景（「生成一个计算器」写不出来的直接原因）：
  ① `div(1, 0)` 既被契约要求抛 ZeroDivisionError，又被边界矩阵要求"不许崩"
     → 正确实现也红 → 6 轮同一状态 → 振荡检测收工，代码 0/5 全是桩；
  ② docstring 被硬切 160 字符 → "抛 ZeroDivisionError" 变成 "ZeroDivisionEr"。

跑法: python check_contract_conflict.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import author, contract as cm, iterate, props, skeleton  # noqa: E402

FAILS: list[str] = []

CALC_REQ = """1) calc.py：基础四则运算计算器。
   - add(a: float, b: float) -> float：两数相加
   - div(a: float, b: float) -> float：两数相除；除数为 0 抛 ZeroDivisionError

示例（必须满足）：
add(1, 2) == 3
div(6, 3) == 2.0
"""

CALC_SRC = '''"""calc 模块。"""


def add(a: float, b: float) -> float:
    return a + b


def div(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("div by zero")
    return a / b
'''


def _sandbox_temp():
    import tempfile as _tf
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


# 契约自己说"空串返回空串"、同时又声明"非法输入抛 ValueError" —— 文字探针若生成
# `from_hex("") 应当抛 ValueError`，就和示例正面打架（实验台 codec 因此 0/5）。
EMPTY_OK = """代码规模：S

1) codec.py：编码。
   - from_hex(s: str) -> str：把十六进制解回文本；长度是奇数或含非十六进制字符抛 ValueError；空串返回空串
   - to_hex(data: str) -> str：按 UTF-8 编码后转小写十六进制；空串返回空串

示例（必须满足）：
from_hex("") == ""
from_hex("61") == "a"
to_hex("") == ""
to_hex("a") == "61"
"""


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("契约↔判据冲突自检")
    contracts = cm.parse_requirement(CALC_REQ)
    c = contracts.get("calc.py") or next(iter(contracts.values()))
    div = c.by_name("div")
    print(f"[0] 契约：{[f.name for f in c.funcs]}｜div.raises={div.raises if div else '?'}")

    print("[1] 边界判据：声明了异常的函数，数值参数取 0 不再算'崩溃'")
    checks = props.boundary_checks(CALC_SRC, must_raise={"div": {"0", "0.0", "0.00"}})
    div_zero = [x for x in checks if "m.div(" in x["expr"] and ",0" in x["expr"].replace(" ", "")]
    check("div(*, 0) 已被剔除", not div_zero, str(div_zero))
    check("其它边界组合仍在（不是把判据删光）", len(checks) >= 2, f"{len(checks)} 条")

    print("[2] raises 判据不造假：多参数函数不生成缺参探针")
    fake = [x for x in props.from_contract(c) if x.get("kind") == "raises"]
    check("没有 m.div(0) 这种缺参坏判据",
          not any(str(x.get("expr", "")).replace(" ", "") in ("m.div(0)", "m.div(-1)")
                  for x in fake), str(fake))

    print("[2.5] 生成的 raises 探针不许与契约示例打架（实验台 codec 因此 0/5）")
    c_empty = cm.parse_requirement(EMPTY_OK)
    mod_empty = list(c_empty.values())[0]
    checks_empty = props.from_contract(mod_empty)
    ex_exprs = {x["expr"] for x in checks_empty if x.get("kind") == "call_eq"}
    raised = {x["expr"] for x in checks_empty if x.get("kind") == "raises"}
    check("空串是合法输入时不再生成 from_hex('') 必抛错探针",
          'm.from_hex("")' not in raised, str(sorted(raised)))
    check("没有任何探针与示例同表达式", not (ex_exprs & raised), str(sorted(ex_exprs & raised)))
    check("非法输入探针仍然保留（去掉的只是冲突那条）",
          any("m.from_hex(" in r and r != 'm.from_hex("")' for r in raised), str(sorted(raised)))

    print("[3] inspect 端到端：正确的 div 不再被判红，并提示补示例")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "calc.py").write_text(CALC_SRC, encoding="utf-8")
        st = iterate.inspect(ws, {"calc.py": c})
        cur = st.get("calc.py") or {}
        fails = " ".join(str(x) for x in (cur.get("fails") or []))
        print(f"     verified={cur.get('verified')}/{cur.get('funcs')}｜fails={fails[:150]}")
        check("div 没被判红", "div" not in (cur.get("hard_failed") or []),
              str(cur.get("hard_failed")))
        check("add 与 div 都验收通过", cur.get("verified") == 2, str(cur.get("verified_names")))
        notes = " ".join(str(x) for x in (cur.get("notes") or []))
        check("提示'契约缺可判示例'（不是拿坏判据冒充判过）",
              "可判输入" in notes or "补一条示例" in notes, notes[:160] or fails[:160])
        check("提示没有混进 fails（否则整个模块会被判红）",
              "可判输入" not in fails, fails[:160])

    print("[4] docstring 不再被硬切")
    long_spec = ("div(a: float, b: float) -> float：两数相除；除数为 0 抛 ZeroDivisionError；"
                 "其余情况返回 a/b 的浮点结果")

    class _P:
        name, type, default = "b", "float", None

    class _F:
        name, params, returns, doc = "div", [_P()], "float", long_spec

    sk = author.render_skeleton([_F()])
    check("author.render_skeleton 保留完整异常名", "ZeroDivisionError" in sk, sk[:160])
    check("没有 'ZeroDivisionEr\"\"\"' 这种断头", 'ZeroDivisionEr"""' not in sk, sk[:160])
    old_sk = skeleton.render_skeleton("calc.py", [skeleton.Sig("div", [_P()], "float", long_spec)],
                                      "基础四则运算计算器：只覆盖加减乘除；" * 6)
    check("skeleton.render_skeleton 的 doc 也完整", "ZeroDivisionError" in old_sk, old_sk[:160])
    head = old_sk.split('"""')[1] if '"""' in old_sk else ""
    check("模块 docstring 不再兜头截断（> 60 字符）", len(head) > 60, f"{len(head)}：{head[:60]}")

    print("[5] 返回键只从「返回…」段取；advisory 判据不许渲染成测试断言")
    # 实测（实验台 search-index）：说明先写"每篇文档形如 {id,text}"再写"返回 {index,docs}"，
    # 旧逻辑把 id/text 也当返回键 → 渲染出的测试必红 → 模块验收 3/3 却一个测试文件都没有。
    index_contract = """代码规模：M

1) index.py：建索引。
   - build(docs: list[dict]) -> dict：每篇文档形如 {"id": 1, "text": "x"}；返回 {"index": {词: [id]}, "docs": {id: text}}

示例（必须满足）：
build([{"id": 1, "text": "a b"}]) == {"index": {"a": [1], "b": [1]}, "docs": {1: "a b"}}
"""
    ci = cm.parse_requirement(index_contract)
    mod_i = list(ci.values())[0]
    build = [f for f in mod_i.funcs if f.name == "build"][0]
    check("返回键只含返回段里的键", set(build.keys) == {"index", "docs"}, str(build.keys))
    checks_i = props.from_contract(mod_i)
    keys_chk = [x for x in checks_i if x.get("kind") == "keys"]
    check("键判据不再要求入参键",
          all("id" not in (x.get("keys") or []) and "text" not in (x.get("keys") or [])
              for x in keys_chk), str([x.get("keys") for x in keys_chk]))
    rendered = props.render_pytest("index", checks_i)
    check("advisory 判据不渲染成断言（写进测试就是假红）", "只提示" not in rendered,
          rendered[:200])
    check("硬判据仍然渲染成断言", "assert" in rendered, rendered[:120])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（冲突输入不再误判 / 不造假判据 / 缺示例如实提示 / docstring 完整 / 返回键与渲染）")
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
