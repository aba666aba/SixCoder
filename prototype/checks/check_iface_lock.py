"""接口冻结 / 变更通道自检（开发文档 §8.4）。

规矩：实现阶段**只读**接口清单；要改就得付出代价 ——
  改签名 / 删函数 → 受影响模块的"已验收"清零，必须重新验收；
  只是**新增**函数 → 不算改接口，不清零（否则每加一个函数就白干一遍）。

这条如果失效，`batch_id: int → str` 那种事就会重演：接口悄悄变了，
引擎却拿着两天前的"已验收"结论继续跑。

跑法: python checks/check_iface_lock.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import interfaces, project  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


SRC = "def run(batch_id: int) -> dict:\n    return {}\n"


def make() -> Path:
    ws = Path(tempfile.mkdtemp(prefix="il_"))
    (ws / "app").mkdir()
    (ws / "app/__init__.py").write_text("", encoding="utf-8")
    (ws / "app/svc.py").write_text(SRC, encoding="utf-8")
    project.save_progress(ws, "app/svc.py", {"verified": ["run"], "ok": True})
    return ws


def verified(ws: Path) -> list:
    return (project.load_progress(ws).get("app/svc.py") or {}).get("verified") or []


def main() -> int:
    ws = make()

    print("[1] 第一次：锁定接口清单")
    r = interfaces.apply_drift(ws)
    check("first_time=True", r["first_time"] is True, str(r))
    check("清单落盘", (ws / interfaces.IFACE_FILE).is_file())
    check("锁定不动进度", verified(ws) == ["run"], str(verified(ws)))

    print("[2] 只新增函数 → 不算改接口，不清零")
    (ws / "app/svc.py").write_text(SRC + "\n\ndef extra() -> int:\n    return 1\n",
                                   encoding="utf-8")
    r2 = interfaces.apply_drift(ws)
    check("新增不算 hard", r2["hard"] == [], str(r2["hard"]))
    check("已验收保留", verified(ws) == ["run"], str(verified(ws)))
    check("清单已重新锁定", "extra" in json.dumps(interfaces.load(ws), ensure_ascii=False))

    print("[3] 改签名 → 回待办（这就是 batch_id int→str 那次）")
    (ws / "app/svc.py").write_text("def run(batch_id: str) -> dict:\n    return {}\n\n\n"
                                   "def extra() -> int:\n    return 1\n", encoding="utf-8")
    r3 = interfaces.apply_drift(ws)
    check("识别为 hard（签名变更）", [c["module"] for c in r3["hard"]] == ["app/svc.py"],
          str(r3["hard"]))
    check("已验收被清零", verified(ws) == [], str(verified(ws)))
    check("进度里 ok 也置否", (project.load_progress(ws).get("app/svc.py") or {}).get("ok") is False)

    print("[4] 删函数 → 同样回待办")
    project.save_progress(ws, "app/svc.py", {"verified": ["run"], "ok": True})
    (ws / "app/svc.py").write_text("def run(batch_id: str) -> dict:\n    return {}\n",
                                   encoding="utf-8")
    r4 = interfaces.apply_drift(ws)
    check("removed 被抓到", r4["hard"] and r4["hard"][0]["removed"] == ["extra"], str(r4["hard"]))
    check("已验收再次清零", verified(ws) == [], str(verified(ws)))

    print("[4b] 片结束（phase=end）只重锁、绝不清零 —— 2026-11-07 真机教训："
          "verified=10/10 全过之后，end 阶段拿「与开工快照不同」去清零，把刚挣的进度当场作废")
    project.save_progress(ws, "app/svc.py", {"verified": ["run"], "ok": True})
    (ws / "app/svc.py").write_text("def run(batch_id: int, seed: int = 1) -> dict:\n    return {}\n",
                                   encoding="utf-8")
    r_end = interfaces.apply_drift(ws, phase="end")
    check("end 阶段不清零", verified(ws) == ["run"], str(verified(ws)))
    check("end 阶段仍会重锁", r_end.get("relocked") is True, str(r_end))
    check("end 阶段 hard 不外泄（留给下个片开始处理）", r_end["hard"] == [], str(r_end["hard"]))

    print("[5] 跨模块 import 指向不存在符号 → 一致性问题必须报出来")
    (ws / "app/api.py").write_text("from app.svc import run\nfrom app.svc import nope\n",
                                   encoding="utf-8")
    r5 = interfaces.apply_drift(ws)
    check("unresolved 被报出来", any(u["symbol"] == "nope" for u in r5["unresolved"]),
          str(r5["unresolved"]))
    check("整体判不一致", r5["ok"] is False)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（首锁 / 新增不清零 / 改签名删函数必清零 / end 只重锁 / 坏 import 报出来）")
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
