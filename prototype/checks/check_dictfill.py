r"""字段级填充自检：给"返回 dict"的函数，把任务缩小到"每个键一个表达式"。

为什么单开这条路：`summarize` 在 7 个样本里只过 2 次，失败不是"不会算"，
而是产出整段代码时口径写错 / 转义搞坏。契约已经把答案写死（keys + 真 call_eq 示例），
所以引擎渲染骨架、模型只填表达式，失败反馈也只指向一个键。

反例清单（每条都对应真实失败形态）：
  1. 模型写语句（`for ...:` / `return ...` / 多行）→ 必须拒绝，骨架只接受表达式；
  2. 少填某个键 → 必须拒绝（不能悄悄留 `__FILL_*__` 进产物）；
  3. 键名不在契约里 → 忽略，不影响其它键；
  4. 函数没有 keys / 返回不是 dict → 不走这条路（回到普通符号编辑）。

跑法：python check_dictfill.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import dictfill  # noqa: E402
from core.contract import FuncSig, Param  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def summarize_sig() -> FuncSig:
    return FuncSig(name="summarize", params=[Param(name="rows", type="list[dict]")],
                   returns="dict", keys=["count", "avg_age", "domains"])


def main() -> int:
    f = summarize_sig()
    print("[1] 适用性判断")
    check("返回 dict + 有 keys + 简单参数 → 支持", dictfill.supported(f))
    check("没有 keys → 不支持",
          not dictfill.supported(FuncSig(name="g", params=[Param("x", "str")], returns="dict")))
    check("返回 list → 不支持", not dictfill.supported(
        FuncSig(name="h", params=[Param("x", "str")], returns="list[dict]", keys=["a"])))
    check("参数是陌生类型 → 不支持", not dictfill.supported(
        FuncSig(name="k", params=[Param("c", "sqlite3.Connection")], returns="dict", keys=["a"])))

    print("[2] 骨架由引擎渲染（键来自契约，模型不用猜）")
    sk = dictfill.skeleton(f, "汇总统计")
    check("骨架含正确签名", "def summarize(rows: list[dict]) -> dict:" in sk, sk)
    check("骨架预置了全部键", all(f'"{k}": None' in sk for k in f.keys), sk)
    check("每个键一个填空位", all(f"__FILL_{k}__" in sk for k in f.keys), sk)
    check("骨架语法合法", _parses(sk))
    check("docstring 里的三引号被换掉（防转义事故）",
          '"""' not in sk.split("\n")[1] or sk.count('"""') == 2, sk)

    print("[3] 填表达式：正常路径")
    good = {"count": "len(rows)",
            "avg_age": "round(sum(r.get('age', 0) for r in rows) / len(rows), 1) if rows else 0",
            "domains": "{e.split('@')[-1]: 1 for e in (r.get('email', '') for r in rows) if '@' in e}"}
    code = dictfill.fill(sk, good)
    check("填完非空", bool(code), code[:80])
    check("没有残留占位符", "__FILL_" not in code)
    check("填完语法合法", _parses(code), code[:120])
    check("表达式真的能跑", _runs(code))

    print("[4] 反例：越界写法必须拒绝")
    bad_cases = [
        ("写语句（for）", {**good, "count": "for r in rows: pass"}),
        ("写 return", {**good, "count": "return len(rows)"}),
        ("真·多行表达式", {**good, "count": "len(rows)\n+ 1"}),
        ("少填一个键", {"count": "len(rows)"}),
        ("空填", {**good, "domains": "  "}),
    ]
    for name, fields in bad_cases:
        check(f"{name} → 拒绝", dictfill.fill(sk, fields) == "", name)
    check("尾部空白/换行可以容忍（不算多行）",
          bool(dictfill.fill(sk, {**good, "count": "len(rows)\n"})))
    extra = dictfill.fill(sk, {**good, "not_a_key": "1"})
    check("多给未知键 → 忽略但其余照填", bool(extra) and "__FILL_" not in extra)

    print("[5] 提示词里带上了契约与样例（模型知道口径）")
    prompt, sk2 = dictfill.build_prompt(f, "summarize(rows: list[dict]) -> dict：返回 count/avg_age/domains",
                                        "汇总统计",
                                        'summarize([{"age": 10, "email": "a@x.com"}]) == '
                                        '{"count": 1, "avg_age": 10.0, "domains": {"x.com": 1}}')
    check("提示词含键名模板", '"count": "<表达式>"' in prompt, prompt[:120])
    check("提示词含契约口径", "avg_age" in prompt or "count" in prompt)
    check("提示词含真实样例", "a@x.com" in prompt)
    check("提示词说明了可用变量", "rows" in prompt)
    # 空输入不许崩这条铁律必须一直在：summarize 的 avg_age 写成 sum(...)/len(...)
    # 就是在空列表上除零（实测 night2/night3 都栽在这），删了这条提示词等于把坑挖回来
    check("提示词写明空输入/None 不许崩", "空输入" in prompt and "None" in prompt)
    check("提示词给出了接住空值的写法", "if x else" in prompt or "or 默认值" in prompt)
    # 重试时用的"报错 → 改法"映射：窄、确定、不带猜测
    check("除零给出针对性改法", "分母" in dictfill.advice("ZeroDivisionError: division by zero"))
    check("缺键给出 .get 改法", ".get('age')" in dictfill.advice("KeyError: 'age'"))
    check("None 运算给出兜底改法", "None" in dictfill.advice("unsupported operand type(s) for +: 'int' and 'NoneType'"))
    check("未知报错也给一条通用改法", bool(dictfill.advice("weird")))

    print("[6] 解析模型输出：容忍围栏与杂质")
    check("裸 JSON 可解析", dictfill.parse('{"fields": {"count": "len(rows)"}}')
          == {"count": "len(rows)"})
    check("带围栏可解析",
          dictfill.parse('```json\n{"fields": {"count": "1"}}\n```') == {"count": "1"})
    check("垃圾输出 → 空", dictfill.parse("我觉得应该这样做：for r in rows…") == {})
    check("fields 不是对象 → 空", dictfill.parse('{"fields": "count=len(rows)"}') == {})

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（骨架由引擎给、只接受表达式、越界一律拒、缺键必拒）")
    return 0


def _parses(code: str) -> bool:
    import ast
    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _runs(code: str) -> bool:
    """把填好的函数真跑一遍样例，确认表达式不是"语法对但算不出来"。"""
    ns: dict = {}
    try:
        exec(code, ns)                             # noqa: S102
        got = ns["summarize"]([{"age": 10, "email": "a@x.com"},
                               {"age": 20, "email": "b@y.com"}])
        return isinstance(got, dict) and got.get("count") == 2 and "avg_age" in got
    except Exception:                              # noqa: BLE001
        return False


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
