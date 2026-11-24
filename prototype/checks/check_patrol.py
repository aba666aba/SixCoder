"""巡检（patrol）自检：它在夜班收工后还会不会空转、判据会不会自己制造假异常。

2026-11-20 实测事故（本次的由来）：夜班 01:17 正常收工（覆盖 76/76），
`patrol.py --persist` 是 `while True`，**没有"夜班退场就下班"的出口**，
于是从 01:17 到 02:07 每分钟：
  · 对整个产物仓做一次全量编译 + 冒烟（纯浪费）
  · 报一次 `ANOMALY: 夜班进程消失` —— 夜班是正常收工，这是假异常
  · 往 patrol.log 刷了 8701 行

同时它判断"夜班活着"的方式是 `tasklist | count python.exe`（整机 python 数量），
与"本仓库夜班"毫无关系；夜班实际由 pythonw 拉起，这条路本来也数不到。

钉住五条：
  1. persist 模式必须有收工出口（连续 N 轮看不到本仓库夜班 → 退出并清 pid）；
  2. 夜班存活必须**按仓库精确匹配**（watchdog_night.py / cli.py night + repo 路径），
     不许再数整机 python；
  3. 不许硬编码解释器/仓库路径（安装版里 D:\\Python、E:\\AAATOM 都不存在）；
  4. 不许硬编码产物规模（`done=76` 是某个项目的数字，引擎不认识项目）；
  5. 界面起夜班时必须**同时起巡检**（删掉 START_NIGHT.bat 后这层不能丢）。

跑法: python checks/check_patrol.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
PATROL = PROTO / "tools" / "patrol.py"
SERVER = PROTO / "server.py"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:280]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    src = PATROL.read_text(encoding="utf-8")
    srv = SERVER.read_text(encoding="utf-8")

    print("[1] 收工出口：夜班退场后巡检必须自己下班")
    ck("有 IDLE_EXIT_ROUNDS 常量", "IDLE_EXIT_ROUNDS" in src)
    ck("persist 分支里有收工 return",
       re.search(r"if PERSIST and _idle >= IDLE_EXIT_ROUNDS:[\s\S]{0,400}?return 0", src) is not None)
    ck("收工时清自己的 pid 文件", "_clear_own_pid" in src)
    ck("退场会写日志（不是悄悄消失）", "巡检收工" in src or "均已退场" in src)

    print("[2] 夜班存活的判断方式")
    ck("有按仓库精确匹配的函数", "def _own_night_alive" in src)
    ck("认 watchdog_night.py", '"watchdog_night.py" in low' in src)
    ck("认 cli.py night", '"cli.py" in low and " night" in low' in src)
    ck("用 repo 路径过滤（不把别的项目算进来）", "if key and key not in low" in src)
    ck("不再数整机 python.exe", 'r.stdout.count("python.exe")' not in src)
    ck("没有残留 tasklist 计数调用", 'IMAGENAME eq python.exe' not in src)

    print("[3] 不硬编码解释器/仓库路径")
    ck("没有 D:/Python", "D:/Python" not in src)
    ck("没有 E:/AAATOM", "E:/AAATOM" not in src)
    ck("解释器用 sys.executable", "PY = sys.executable" in src)
    ck("prototype 路径按 __file__ 算", "PROTO = Path(__file__).resolve().parents[1]" in src)
    # 2026-11-23：整批自检曾硬编码 E:\AAATOM（迁到 F 盘后全线读不到源码、验证假绿）。
    # 把"checks 目录内不许出现失效盘符路径"钉成守护，防止迁移后再次退化。
    bad_checks = []
    for cf in sorted((PROTO / "checks").glob("check_*.py")):
        if cf.name == "check_patrol.py":   # 本守护脚本自身含断言字面量，跳过
            continue
        csrc = cf.read_text(encoding="utf-8", errors="ignore")
        if ("E:\\AAATOM" in csrc or "E:/AAATOM" in csrc or "E://AAATOM" in csrc):
            bad_checks.append(cf.name)
    ck("checks/ 内无失效盘符硬编码", not bad_checks, bad_checks[:5])

    print("[4] 不硬编码产物规模")
    ck("没有 /76", "/76" not in src)
    ck("state() 返回总数", "return s, d, g, total" in src)

    print("[5] 界面起夜班时同时起巡检")
    ck("detached 接口 spawn patrol", '"tools/patrol.py"' in srv and '"--persist"' in srv)
    ck("给巡检传了 SIXCODER_REPO", 'env["SIXCODER_REPO"]' in srv)
    ck("巡检起不来不拖垮夜班（try 包住）",
       re.search(r"patrol = \{\"started\": False\}[\s\S]{0,600}?except Exception", srv) is not None)
    ck("返回值里带 patrol 状态", '"patrol": patrol' in srv)

    print("[6] 语法与导入")
    import ast
    try:
        ast.parse(src)
        ck("patrol.py 语法通过", True)
    except SyntaxError as exc:
        ck("patrol.py 语法通过", False, str(exc))
    try:
        ast.parse(srv)
        ck("server.py 语法通过", True)
    except SyntaxError as exc:
        ck("server.py 语法通过", False, str(exc))

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
