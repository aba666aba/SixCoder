"""写码成功必须记账（2026-11-11）。

背景（毕设项目实测 10:16 那轮）：
  引擎在**还有 56 项端到端红灯**时收工了，收工理由写
  「模块契约已全过，但端到端还有 56 项能力未通过（涉及模块 …）：没有可执行的工作」。

  但那一轮**其实写了东西** —— 日志里 `author_start path=app/models.py`、
  `patch_tools path=app/models.py` 都在，然后**没有** `structure_gate`/`order_done`，
  直接接了 `web_wiring` 和 `stop`。

  根因：`company.py` 的补丁分支里写着 `stats["modules"][-1]["line_patch"] = ...`，
  而**第一次写码就可能走补丁路径**（决策表判 ACT_PATCH_LINE）→
  此时 `stats["modules"]` 还是空的 → **IndexError** → 被外层 `except Exception` 吞掉
  → 补丁明明成功了却**没记账** → 轮末 `if not stats["modules"]` 判成"没有可执行的工作"
  → **收工**。

  这是"引擎把自己的记账 bug 读成'没活干'"的典型：症状是覆盖停滞，根因是引擎。

本自检钉住三条：
  ① 补丁路径不许直接索引 `stats["modules"][-1]`（空列表会 IndexError）
  ② 补丁成功必须**补记一条**到 stats["modules"]
  ③ 收工理由必须**自证**（本轮写了东西就不能说"没有可执行的工作"）
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

    print("[1] 补丁路径不许裸索引 stats[\"modules\"][-1]")
    # 扫**代码**（去掉注释行；注释里提到这个模式不算违规）
    _code = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    bad = re.findall(r'stats\["modules"\]\[-1\]\[', _code)
    check("没有裸的 stats[\"modules\"][-1][...] 写入", not bad,
          "仍有 %d 处 → 首次写码走补丁路径时会 IndexError" % len(bad))

    print("[2] 补丁成功必须补记一条")
    check("补丁分支里有补记（by: line-patch）",
          "line-patch" in src,
          "补丁成功没记账 → 引擎把自己的记账 bug 读成'没活干'")

    print("[3] 收工理由必须自证")
    check("收工理由里有'本轮写了 N 个模块'的自证",
          "本轮写了" in src, "")

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
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
