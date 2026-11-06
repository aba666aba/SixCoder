"""自检：cli_run 的签名必须与引擎生成的判据一致。

真实事故（dev1106a，2026-11-06）：远端把契约里的 cli_run 写成 `-> int`，
但 core_groups() 生成的判据用 `cli_run(argv)[1]`（要求返回 (退出码, 文本)）。
引擎原来只在「契约里没有 cli_run 行」时才注入正确签名，于是矛盾被保留，
判据永远红，4 轮自愈全在重写同一个文件，桩模块一直没人管。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import auto  # noqa: E402

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    print("  OK  " + msg if cond else "  FAIL " + msg)
    if not cond:
        FAILS.append(msg)


BAD = """1) csvio.py：CSV 读写
   - import_csv(file_path: str) -> None：导入 CSV

2) cli_core.py：命令行核心
   - cli_run(argv: list[str]) -> int：参数解析和调度

示例（必须满足）：
（无）
"""
PROBES = [{"kind": "cli", "entry": "main.py",
           "args": ["import", "--file", "d.csv"],
           "files": {"d.csv": "a,b\n"}, "stdout_has": "导入成功"}]

out = auto.ensure_core_binding(BAD, "main.py", PROBES)
ok("-> tuple[int, str]" in out, "远端写错的 -> int 被强制换回 tuple[int, str]")
ok("-> int：" not in out and "-> int:" not in out, "契约里不再残留错误签名")
ok("[1]" in out, "探针生成的 cli_run(...)[1] 判据仍在（签名与判据一致）")

MIXED = BAD.replace("（无）", "cli_run(['import', '--file', 'd.csv']) == 0")
out3 = auto.ensure_core_binding(MIXED, "main.py", PROBES)
ok("cli_run(['import', '--file', 'd.csv']) == 0" not in out3,
   "与 tuple 签名矛盾的旧示例 == 0 被清掉")
ok("示例（必须满足）：" in out3 and "cli_run(" in out3,
   "清旧示例后仍注入了引擎自己的判据，不是删成空考卷")

GOOD = BAD.replace("-> int", "-> tuple[int, str]")
out2 = auto.ensure_core_binding(GOOD, "main.py", PROBES, fail_note="入口不存在")
ok("-> tuple[int, str]" in out2, "本来正确的签名不被改坏")
ok("上一轮实测失败" in out2, "失败备注仍会追加到正确签名上")

if FAILS:
    raise SystemExit(1)
print("全部通过（cli_run 签名与判据一致）")
