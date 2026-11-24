"""验证"提示词去转义"修复：契约 docstring 带 dict 字面量时，工人不再被转义搞崩。

反例来源（实测，night4/5/6 反复出现）：契约写 `返回 {"count": 人数, ...}`，
模型把这段抄进 JSON 的 code 字段时要把 `\"\"\"` 与内部 `"` 双重转义，7B 必崩，
输出被截断成 `... 返回 {` → ParserSyntaxError → 整轮白跑。

本脚本只做**离线可判**的部分（不依赖模型、不联网）：
  1. `_prompt_safe`：喂给模型的上下文里不再出现裸三引号/docstring 内双引号；
  2. `_repair_code`：给定"被截断的 docstring"代码，必须能机械救回可解析版本；
  3. 端到端：把坏代码喂给 symedit，看抢救后能否成功替换。

用法：python tools/verify_promptsafe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import iterate, symedit  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


POISON = '''def summarize(rows: list[dict]) -> dict:
    """summarize：返回 {"count": 人数, "avg_age": 平均年龄}"""
    return {}
'''

TRUNCATED = '''def summarize(rows: list[dict]) -> dict:
    """summarize(rows: list[dict]) -> dict：返回 {'''


def main() -> int:
    print("[1] 上下文净化（_prompt_safe）")
    safe = iterate._prompt_safe(POISON)
    check("不再出现裸三引号", '"""' not in safe, safe)
    check("docstring 里的双引号已转单引号", "'count'" in safe and '"count"' not in safe, safe)
    check("仍然是可解析的 python", _parses(safe), safe)
    check("函数签名没被破坏", "def summarize(rows: list[dict]) -> dict:" in safe)
    check("空输入不炸", iterate._prompt_safe("") == "")
    check("语法本身就坏的输入不炸（原样返回兜底）", isinstance(iterate._prompt_safe("def f(:"), str))

    print("[2] 确定性抢救（_repair_code）")
    cands = iterate._repair_code(TRUNCATED)
    check("至少给出一版候选", len(cands) >= 1, str(cands)[:120])
    ok = [c for c in cands if _parses(c)]
    check("有一版可解析", bool(ok), str(cands)[:200])
    if ok:
        check("抢救版保留了函数名", "def summarize" in ok[0], ok[0][:120])

    print("[3] 端到端：坏代码经抢救能落盘")
    base = "def summarize(rows: list[dict]) -> dict:\n    raise NotImplementedError\n"
    r = symedit.apply_edits(base, [{"op": "replace", "symbol": "summarize",
                                    "code": TRUNCATED}])
    check("直接渲染确实会失败（复现原 bug）", not r.ok, str(r.reason))
    salvaged = None
    for cand in iterate._repair_code(TRUNCATED):
        r2 = symedit.apply_edits(base, [{"op": "replace", "symbol": "summarize", "code": cand}])
        if r2.ok:
            salvaged = r2.new_source
            break
    check("抢救后能渲染成功", salvaged is not None)
    if salvaged:
        check("落盘内容可解析", _parses(salvaged), salvaged[:160])
        check("函数还在文件里", "def summarize" in salvaged)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（转义问题在上下文侧被消除，且坏输出有确定性抢救）")
    return 0


def _parses(text: str) -> bool:
    import ast
    try:
        ast.parse(text)
        return True
    except SyntaxError:
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
