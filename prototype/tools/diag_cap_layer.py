"""能力诊断：区分"没写"与"写了但接不上"（2026-11-12 实测产物仓得出）。

背景：某产物仓实测 5.8 小时 —— 29 次写码成功、函数级判据全过
（`progress.json` 里 verified 逐个增长），但**覆盖率 done 23/84 纹丝不动**。
逐条跑 absent 能力的探针，发现失败原因**不是"函数没写"**，而是三类接线/契约问题：

  ① **404 路由未命中** —— 处理函数写了，但没挂到 app 的 include_router 上
     （或挂在别的模块 → 探针按路径找不到）
  ② **422 参数校验失败** —— 路由在，但请求体字段名/类型与实现的 Pydantic 模型对不上
  ③ **响应字段不符** —— 状态码对，但返回 JSON 的键名与探针期望的不一致
     （实测 `{'status': 'pending'}` vs 期望 `status`+其它键）

这三类**都不是"让远端再写一遍函数"能解决的** —— 再写一遍大概率还是同样错，
于是夜班在同一批能力上空转（这就是"done 零增长"的真因）。

本脚本把这些失败原因**归类**，让人一眼看出"卡在哪一层"：
  · 端到端失败原因分布（404 / 422 / 字段不符 / 其它）
  · 有多少能力是"函数级已验证、但端到端没通" ← **接不上**的规模
  · 列出最该修的若干条（含失败原因摘要）

只读诊断：不改产物、不改引擎。
跑法: python tools/diag_cap_layer.py [产物仓路径]
"""
from __future__ import annotations

import json
import pathlib
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import blueprint as bp_mod, probe  # noqa: E402


def classify(detail: str) -> str:
    d = str(detail or "")
    if "404" in d and "路由未命中" in d:
        return "404_路由未命中"
    if "422" in d:
        return "422_参数校验失败"
    if "状态码" in d and "（期望" in d:
        return "状态码不符"
    if "JSON 字段不符" in d:
        return "响应字段不符"
    if "超时" in d:
        return "超时"
    if not d:
        return "无详情"
    return "其它"


def main() -> int:
    ws = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                      else r"F:\WorkSpace\biyeshejishuangxuan")
    bp = bp_mod.load(ws)
    done = bp.done()
    pend = [c for c in bp.caps if str(getattr(c, "status", "")) != "done"]

    # 函数级已验证的模块（progress.json）
    prog_path = ws / ".sixcoder" / "progress.json"
    verified_mods: set[str] = set()
    if prog_path.is_file():
        try:
            prog = json.loads(prog_path.read_text(encoding="utf-8"))
            for rel, info in (prog or {}).items():
                if (info or {}).get("verified"):
                    verified_mods.add(rel)
        except Exception:  # noqa: BLE001
            pass

    kinds: Counter[str] = Counter()
    rows: list[tuple[str, str, str]] = []
    written_but_unwired = 0
    checked = 0
    for c in pend[:40]:                     # 抽前 40 条，够看出分布
        pr = getattr(c, "probe", None)
        if not pr:
            continue
        checked += 1
        try:
            r = probe.run(ws, pr, timeout=120)
        except Exception as exc:  # noqa: BLE001
            r = {"ok": False, "detail": f"探针异常 {type(exc).__name__}: {exc}"[:200]}
        det = str(r.get("detail") or "")
        k = classify(det)
        kinds[k] += 1
        # 该能力的落点里，有没有"函数级已验证"的？
        has_verified = any(m in verified_mods for m in (c.modules or []))
        if has_verified and not r.get("ok"):
            written_but_unwired += 1
        rows.append((c.id, k, det[:150]))

    print("=" * 74)
    print(f"产物仓：{ws}")
    print(f"能力总数 {len(bp.caps)} | done {len(done)} | 待办 {len(pend)}")
    print(f"本轮抽检 {checked} 条未完成能力")
    print()
    print("── 端到端失败原因分布 ──")
    for k, v in kinds.most_common():
        print(f"   {k:<22} {v}")
    print()
    print(f"── 其中「落点已有函数级通过的 verified、但端到端没通」= {written_but_unwired} 条")
    print("   （这批就是「写对了但接不上」—— 重复派单写函数治不了）")
    print()
    print("── 明细（前 20 条）──")
    for cid, k, det in rows[:20]:
        print(f"   {cid:<42} {k}")
    print()
    print("判读口径：")
    print("   · 404_路由未命中 多的 → 接线层问题（引擎该修，不是让远端重写）")
    print("   · 422/字段不符 多的 → 契约与实现不一致（判据与契约该对齐）")
    print("   · 其它多           → 才可能是真正的「还没写」")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
