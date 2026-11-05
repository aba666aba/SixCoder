"""回放：`ab_bodies/store.py` 那两个真实缺陷，能否用"行级插入"修掉。

当时（2026-11-04 21:23）的实际情况：
  · store.py 整份**没有 import sqlite3**，但 `_new_memory_connection()` 里用了它；
  · `_row_values()` 用 `_PEOPLE_COLUMNS`，而文件里定义的是 `_FIELDS`。
原来的行级档结构上修不了这类"新增"（只能替换行，且提示词禁止引入依赖），
于是被迫升级成整文件重写（实测 9k~10k token 输出）。

本脚本不调用远端：只用真实的 buggy 文件 + 手工写死的两段最小补丁，
证明"打法上可行"，并把"输出 token 对比"算出来。
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import patch, tokens  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    src_path = ROOT / "projects" / "ab_bodies" / "store.py"
    if not src_path.exists():
        print(f"跳过：找不到回放样本 {src_path}")
        return 0
    code = src_path.read_text(encoding="utf-8")
    rows = code.splitlines()
    print("回放：ab_bodies/store.py 的两个真实缺陷")

    # 定位：用真实证据（e2e 的 traceback 原文）
    evidence = ("store.py:26: in upsert\nstore.py:93: in _row_values\n"
                "E   NameError: name '_PEOPLE_COLUMNS' is not defined")
    hint = patch.line_hint(evidence)
    print(f"  证据定位到第 {hint} 行；文件共 {len(rows)} 行")

    # 找到 _FIELDS 定义行（用它当 `_PEOPLE_COLUMNS` 的替代）+ 第 1 行（插 import）
    fields_line = next((i for i, ln in enumerate(rows, 1) if ln.startswith("_FIELDS")), 0)
    print(f"  文件里已有的常量定义在第 {fields_line} 行：{rows[fields_line - 1][:60]!r}")

    fix = [
        # ① 补 import（插在第 1 行之前）
        {"path": "store.py", "line": 1, "old": "", "new": "import sqlite3\n"},
        # ② 补别名常量（插在 _FIELDS 定义行之前，让 _row_values 里的名字有定义）
        {"path": "store.py", "line": fields_line, "old": "",
         "new": "_PEOPLE_COLUMNS = _FIELDS\n"},
    ]
    out_tokens = tokens.exact_count(
        '{"patches":[{"path":"store.py","line":1,"old":"","new":"import sqlite3\\n"},'
        '{"path":"store.py","line":%d,"old":"","new":"_PEOPLE_COLUMNS = _FIELDS\\n"}],'
        '"why":"补 import 与缺失的常量定义"}' % fields_line)
    whole_tokens = tokens.exact_count(
        '{"files":[{"path":"store.py","content":%s}]}' % __import__("json").dumps(code))

    res = patch.apply_patches(code, fix)
    ok_apply = res["ok"] and not res["rejected"]
    print(f"\n  补丁应用：{'成功' if ok_apply else '失败'}  {res['applied']}")
    if not ok_apply:
        print(f"  拒绝原因：{res['rejected']}")
        return 1
    try:
        compile(res["code"], "<patched>", "exec")
        compiles = True
    except SyntaxError as exc:
        compiles = False
        print(f"  语法错误：第 {exc.lineno} 行 {exc.msg}")
    print(f"  打完补丁能编译：{compiles}")

    # 关键判据：两个名字现在都有定义了（就是 gate_undef 抓的那两条）
    from core import gates
    tmp = ROOT / "projects" / "_replay_tmp"
    tmp.mkdir(exist_ok=True)
    (tmp / "store.py").write_text(res["code"], encoding="utf-8")
    before = gates.gate_undef(ROOT / "projects" / "ab_bodies", ["store.py"])
    after = gates.gate_undef(tmp, ["store.py"])
    print(f"  体检前：ok={before.ok}  {before.detail[:120]}")
    print(f"  体检后：ok={after.ok}  {after.detail[:120]}")
    for f in tmp.glob("*"):
        f.unlink()
    tmp.rmdir()

    print("\n  输出 token 对比（官方 tokenizer 精确数）：")
    print(f"    行级补丁（2 条插入）：{out_tokens} token")
    print(f"    整文件重写（当时实测）：约 {whole_tokens} token")
    if out_tokens and whole_tokens:
        print(f"    倍数：{whole_tokens / out_tokens:.0f}×")

    good = ok_apply and compiles and before.ok is False and after.ok is True
    print(f"\n结论：{'行级档可以修掉这类缺陷（不必升级到整文件）' if good else '行级档仍然不行'}")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
