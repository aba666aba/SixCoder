"""腐烂分自检：分数会随腐烂信号上升，且到阈值就把打法换成"整模块重刷"。

跑法: python check_rot.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import rot  # noqa: E402

FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


CLEAN = '''"""干净模块。"""


def add(a: int, b: int) -> int:
    return a + b
'''

ROTTEN = '''"""烂掉的模块。"""
import os


def dup(a):
    return a


def dup(a):
    return a


def messy(x=[]):
    # TODO 先这样
    try:
        return x
    except:
        pass


def messy2(x):
    # HACK 临时绕过
    try:
        return x
    except:
        pass
'''


def main() -> int:
    print("腐烂分自检")

    print("[1] 干净模块 → 0 分、打法 patch")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "clean.py").write_text(CLEAN, encoding="utf-8")
        info = rot.score_module(ws, "clean.py", "S")
        check("分数为 0", info["score"] == 0, str(info["score"]))
        check("打法是 patch", info["action"] == "patch", info["action"])
        check("没有依据条目", not info["reasons"], str(info["reasons"]))

    print("[2] 烂模块 → 分数显著上升且打法变 rebuild")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "messy.py").write_text(ROTTEN, encoding="utf-8")
        info = rot.score_module(ws, "messy.py", "S")
        print(f"     分数 {info['score']}｜打法 {info['action']}｜依据 {info['reasons']}")
        check("分数 ≥ 阈值 60", info["score"] >= rot.THRESHOLD_REBUILD, str(info["score"]))
        check("打法是 rebuild", info["action"] == "rebuild", info["action"])
        check("识别出临时方案", any("临时方案" in r for r in info["reasons"]), str(info["reasons"]))
        check("识别出重复定义", any("重复定义" in r for r in info["reasons"]), str(info["reasons"]))
        check("识别出 lint", any("lint" in r for r in info["reasons"]), str(info["reasons"]))
        rebuild, info2 = rot.should_rebuild(ws, "messy.py", "S")
        check("should_rebuild 返回 True", rebuild, str(info2["action"]))

    print("[3] 分数是单调的：信号越多分越高")
    with _sandbox_temp() as td:
        ws = Path(td)
        base = 'def f():\n    return 1\n'
        (ws / "m.py").write_text(base, encoding="utf-8")
        s0 = rot.score_module(ws, "m.py", "S")["score"]
        (ws / "m.py").write_text(base + "def dup():\n    return 1\n\n\ndef dup():\n    return 2\n",
                                 encoding="utf-8")
        s1 = rot.score_module(ws, "m.py", "S")["score"]
        (ws / "m.py").write_text(base + "def dup():\n    return 1\n\n\ndef dup():\n    return 2\n"
                                 + "# TODO 临时\n# HACK 绕过\n", encoding="utf-8")
        s2 = rot.score_module(ws, "m.py", "S")["score"]
        check("加重复定义后分数上升", s1 > s0, f"{s0} → {s1}")
        check("再加临时标记后继续上升", s2 > s1, f"{s1} → {s2}")

    print("[4] 阈值分档：30 以下是 patch，30–59 是 patch_careful，60+ 是 rebuild")
    check("阈值常量正确", rot.THRESHOLD_REBUILD == 60 and rot.THRESHOLD_CAREFUL == 30,
          f"{rot.THRESHOLD_CAREFUL}/{rot.THRESHOLD_REBUILD}")
    # 只加 3 个临时标记 → 36 分 → patch_careful
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "m.py").write_text("# TODO a\n# TODO b\n# TODO c\ndef f():\n    return 1\n",
                                 encoding="utf-8")
        info = rot.score_module(ws, "m.py", "S")
        check("3 个临时标记 → patch_careful", info["action"] == "patch_careful",
              f"{info['score']} / {info['action']}")

    print("[5] 依赖异常：被很多模块依赖 / 有环 → 加分")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "core_mod.py").write_text("X = 1\n", encoding="utf-8")
        for i in range(3):
            (ws / f"user{i}.py").write_text("from core_mod import X\n", encoding="utf-8")
        info = rot.score_module(ws, "core_mod.py", "S")
        check("被 3 个模块依赖 → 有依赖异常依据",
              any("依赖" in r for r in info["reasons"]), str(info["reasons"]))
        (ws / "core_mod.py").write_text("from user0 import X\n", encoding="utf-8")
        info2 = rot.score_module(ws, "core_mod.py", "S")
        check("双向依赖 → 环被识别", any("环" in r for r in info2["reasons"]),
              str(info2["reasons"]))
        check("有环比没环分高", info2["score"] > info["score"], f"{info['score']}→{info2['score']}")

    print("[6] 报告小节：列出分数与打法，并点名该重刷的模块")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "clean.py").write_text(CLEAN, encoding="utf-8")
        (ws / "messy.py").write_text(ROTTEN, encoding="utf-8")
        rmap = rot.scores(ws, ["clean.py", "messy.py"], "S")
        lines = rot.report_lines(rmap)
        txt = "\n".join(lines)
        check("有标题", "模块腐烂分" in txt, txt[:80])
        check("列出两个模块", "clean.py" in txt and "messy.py" in txt)
        check("高点名建议重刷", "整模块重刷" in txt, txt[-120:])

    print("[7] 已接进夜班")
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("开跑算一次腐烂分", "_rot0.scores" in comp and "rot_scores" in comp)
    check("决策表动作可被腐烂分覆盖", "rot_rebuild" in comp and "should_rebuild" in comp)
    check("交付报告有腐烂分小节", "_rot2.report_lines" in comp)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（干净=0 分 / 烂模块=重刷 / 单调递增 / 分档 / 依赖异常 / 报告与接入）")
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
