"""承载已通过能力的文件不许"跳过"，只许"只补缺函数"（2026-11-11）。

背景（毕设项目实测死结）：
  `app/api/topics.py` 承载多条已通过能力，同时**缺函数**（切片 s10-9712636b 要求它
  新增 `create_topic_route` / `get_topic_route`）。
  旧口径在这里**直接跳过**（"承载已通过能力且缺函数 → 禁止整文件重写"），
  于是：缺函数 → 该文件要**加**新函数 → 正确的做法是"只回函数体"（bodies），
  整文件重写才是危险的那个动作；旧口径把"整文件重写有风险"读成了"这个文件不能碰"
  → 跳过 → 远端没写 → 红灯一直在 → 下一轮还是跳过 → **空转**
  （notes 连续出现「片内自验：修好 0 条，仍红 4 条（空转，1 步，¥0.03）」）。

本自检钉住三条：
  ① 承载已通过能力 + 不缺函数 → 一律 bodies（老规则，不许改回整文件）
  ② 承载已通过能力 + **缺函数** → 也必须 bodies，**不许跳过**
  ③ 跳过这条路已经删掉：源码里不许再出现"受保护…跳过"的早退
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import company  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    import inspect
    import re

    src = inspect.getsource(company)

    print("[1] 源码层：'受保护…跳过'的早退已经删掉")
    check("不再有 '禁止整文件重写' 的早退反馈",
          "禁止整文件重写" not in src,
          "旧口径还在 → 缺函数的文件仍然被跳过 → 死锁")
    check("不再有 '不做整文件重写（跳过）' 的提示",
          "不做整文件重写（跳过）" not in src, "")

    print("[2] 源码层：缺函数时强制 bodies")
    m = re.search(r"_carries_done and _missing_now.{0,4000}?auto\s*=\s*\"bodies\"", src, re.S)
    check("缺函数分支里强制 auto = bodies", bool(m),
          "没找到：缺函数的文件可能又被跳过了")
    check("缺函数分支里不再有 return 早退",
          not re.search(r"_carries_done and _missing_now.{0,1200}?return\s*\{", src, re.S),
          "早退 = 跳过 = 死锁")

    print("[3] 源码层：理由写清了（为什么不再跳过）")
    check("注释提到 bodies", "bodies" in src, "")
    check("注释提到旧口径是死锁/空转",
          ("死锁" in src) or ("空转" in src), "")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
