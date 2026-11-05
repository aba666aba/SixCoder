"""干跑校验：不调模型，先确认每份实验契约**能生成判据**。

动机很直接：契约若解析不出示例/边界判据，真跑就是白烧钱——夜班会"验收 0/0 通过"
或整轮判红，而那不是模型的问题，是我的契约写坏了。这一步几秒钟，省掉整批无效实验。
"""
from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from core import contract as cm, contractcheck, props  # noqa: E402
from tools import lab_types  # noqa: E402

BAD: list[str] = []


def main() -> int:
    for t in lab_types.BATCH1 + lab_types.BATCH2 + lab_types.BATCH3 + lab_types.BATCH4 + lab_types.BATCH5:
        print(f"=== [{t['slug']}] {t['name']}")
        if t.get("expect") == "refuse":
            # 故意自相矛盾的契约：期望它**别生成判据**并被前置检查拦住，干跑只做提示
            cs0 = cm.parse_requirement(t["req"])
            r0 = contractcheck.check(cs0) if cs0 else {"ok": False, "hard": []}
            print(f"  前置自查 ok={r0.get('ok')} hard={str(r0.get('hard'))[:110]}")
            if r0.get("ok"):
                print("  FAIL 这份契约本该被判自相矛盾，却没查出来")
                BAD.append(f"{t['slug']}:contradiction-not-detected")
            print()
            continue
        cs = cm.parse_requirement(t["req"])
        if not cs:
            print("  FAIL 解析不出任何模块")
            BAD.append(t["slug"])
            continue
        for rel, c in cs.items():
            checks = props.from_contract(c)
            kinds: dict[str, int] = {}
            for ch in checks:
                kinds[ch.get("kind", "?")] = kinds.get(ch.get("kind", "?"), 0) + 1
            print(f"  {rel}: {len(c.funcs)} 个函数｜判据 {len(checks)} 条 {kinds}")
            if not checks:
                print("  FAIL 这个模块一条判据都生成不出来")
                BAD.append(f"{t['slug']}:{rel}")
            # 每个函数至少要有判据：示例，或**端到端覆盖**（连接类参数的函数不写字面量示例，
            # 空 dict 当连接是假示例 —— 实测会逼模型写静默跳过的防御性壳子）。
            covered = {nm for ch in checks for nm in (ch.get("covers") or [])}
            for f in c.funcs:
                n = sum(1 for ch in checks
                        if str(ch.get("expr", "")).startswith(f"m.{f.name}("))
                if n == 0 and f.name not in covered:
                    print(f"  FAIL {f.name} 没有任何判据（示例写错或签名不匹配）")
                    BAD.append(f"{t['slug']}:{rel}::{f.name}")
            # 示例右侧必须是字面量，否则 prop 比不了
            for ch in checks:
                if ch.get("kind") == "call_eq":
                    try:
                        ast.literal_eval(str(ch.get("expect")))
                    except Exception:                      # noqa: BLE001
                        print(f"  FAIL 期望值不是字面量：{ch.get('expect')!r}")
                        BAD.append(f"{t['slug']}:literal")
        # 边界矩阵：函数带类型注解才生成得出来
        for rel, c in cs.items():
            src = _stub_src(c)
            b = props.boundary_checks(src)
            if not b:
                print(f"  ! {rel} 生成不出边界判据（注解不全？仅提示，不阻塞）")
        print()
    print("干跑结论：" + ("全部可判" if not BAD else f"有 {len(BAD)} 处问题 {BAD[:8]}"))
    return 0 if not BAD else 1


def _stub_src(c) -> str:
    """按契约签名造一份桩源码，供边界矩阵判断能否生成判据。"""
    lines = []
    for f in c.funcs:
        params = ", ".join(f"{p.name}: {p.type or 'object'}" for p in f.params)
        lines.append(f"def {f.name}({params}) -> {f.returns or 'object'}:\n    raise ValueError('x')\n")
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
