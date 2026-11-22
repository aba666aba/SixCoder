"""真实产物回环体检：把产物仓库里的模块喂一遍合并/回填路径，必须仍可编译。

为什么单开：`_merge_skeleton` 曾经只保留 `def` 的**物理首行**当桩头，
多行签名（`def f(\n    a: int,\n) -> dict:`）被截断 → 括号永不闭合 →
夜班连续两轮报 `第 303 行语法错误：'(' was never closed`，而每个函数体单独都合法，
`_blame_body` 一个函数都点不出来 → 判成"说不清位置" → 整文件重写 → 同一处再犯。

产物仓库不存在（本机没这个盘/目录）时跳过，不算失败。
跑法: python checks/check_real_artifacts.py
"""
from __future__ import annotations

import ast
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import author  # noqa: E402

PRODUCTS = [
    r'F:\WorkSpace\biyeshejishuangxuan',
    # 旧盘符下的产物仓已随项目迁移移除；保留失效路径只会让守护误报。
]

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class F:
    def __init__(self, n: str):
        self.name = n


def main() -> int:
    roots = [r for r in PRODUCTS if Path(r).is_dir()]
    if not roots:
        print("SKIP：产物仓库不在本机（%s），跳过真实产物回环体检" % ", ".join(PRODUCTS))
        return 0

    total = 0
    bad = []
    for root in roots:
        for fp in glob.glob(str(Path(root) / '**' / '*.py'), recursive=True):
            try:
                src = Path(fp).read_text(encoding='utf-8')
                tree = ast.parse(src, fp)
            except Exception:
                continue
            fns = [n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            if not fns:
                continue
            total += 1
            funcs = [F(n) for n in fns]
            try:
                # 场景 C：模型给全了函数体（真实夜班路径）→ 不触发还原
                code, _ = author.rebuild_partial(src, funcs, fns, {n: "return None" for n in fns})
                compile(code, fp, 'exec')
            except SyntaxError as exc:
                bad.append((fp, "line %s %s" % (exc.lineno, exc.msg)))
            except Exception as exc:                          # noqa: BLE001
                bad.append((fp, "%s: %s" % (type(exc).__name__, exc)))

    print("[真实产物] 模块数=%d | 合并/回填后不可编译=%d" % (total, len(bad)))
    for fp, why in bad[:10]:
        print("   %s | %s" % (fp, why))
    check("所有真实模块合并后仍可编译", not bad, "%d 个不可编译" % len(bad))

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（真实产物合并后可编译）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
