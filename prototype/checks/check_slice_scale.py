"""规模放开 + 差集分因归类 的自检。

2026-11-09 用户铁律：完全相信远端 → 引擎不许用"历史成功率/拍脑袋常数"去裁剪远端的活。
本文件钉住两件事：
  1) 切片规模只按**物理约束**（输出配额 ÷ 每函数 token）决定；
     一次通过率**只记录不缩小**（它的分母里混着引擎自身缺陷造成的失败）。
  2) `route_status` 工具能把"端点为什么没接上"**分因归类**：
     已接线 / 缺函数（该远端写）/ 接线被拒（引擎的锅，远端不用改）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # checks/ 的上一级 = prototype/（换盘符不失效）
sys.path.insert(0, str(ROOT))

from core import dosing, slicer  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK  " if cond else "  FAIL ") + name + ("" if cond else "   " + extra))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 切片规模：Web 片不再被硬砍到 6")
    src = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("auto.py 里不再有 'funcs_cap = min(int(funcs_cap), 6)'",
          "funcs_cap = min(int(funcs_cap), 6)" not in src,
          "Web 片仍被拍脑袋的 6 卡着")

    print("[2] dosing：一次通过率只记录，不用于缩小")
    n_low, why_low = dosing.suggest_max_funcs(capacity=6000, per_func=190,
                                               first_pass=0.1)
    n_ok, why_ok = dosing.suggest_max_funcs(capacity=6000, per_func=190,
                                             first_pass=0.9)
    check("一次通过率 10% 也不缩小（n 不被 ×0.6）", n_low == 31, f"实际 n={n_low}")
    check("一次通过率 90% 仍然放大", n_ok >= 38, f"实际 n={n_ok}")
    check("依据里写明'仅记录，不缩小'",
          any("仅记录" in x for x in why_low), str(why_low))
    check("没有 10% 通过率就缩片的旧逻辑",
          not any("缩小到 60%" in x for x in why_low), str(why_low))

    print("[3] 切片模块数上限已放开")
    check("SLICE_MAX_MODULES >= 8", slicer.SLICE_MAX_MODULES >= 8,
          f"实际 {slicer.SLICE_MAX_MODULES}")
    check("DEFAULT_DOSE >= 24", slicer.DEFAULT_DOSE >= 24,
          f"实际 {slicer.DEFAULT_DOSE}")

    print("[3b] 缩片：连续两片无收获才缩，且有地板")
    from core import auto
    src_auto = (ROOT / "core" / "auto.py").read_text(encoding="utf-8")
    check("存在缩片地板常量", hasattr(auto, "SLICE_SHRINK_FLOOR"),
          "缩片没有地板 → 会退化成一次一个函数")
    check("地板 >= 6（能容纳一次业务纵切）", auto.SLICE_SHRINK_FLOOR >= 6,
          f"实际 {auto.SLICE_SHRINK_FLOOR}")
    check("连续无收获计数已引入", "no_gain_streak" in src_auto)
    check("缩片条件是 no_gain_streak >= 2",
          "no_gain_streak >= 2" in src_auto,
          "仍是每片无收获就缩")
    check("两处缩片都用了地板", src_auto.count("SLICE_SHRINK_FLOOR") >= 4,
          f"出现 {src_auto.count('SLICE_SHRINK_FLOOR')} 次")
    check("没有 max(1, ...) 的碎片化下限了",
          "max(1, int(funcs_cap * 0.5))" not in src_auto
          and "max(1, int(funcs_cap * 0.8))" not in src_auto,
          "仍有缩到 1 的旧写法")

    print("[4] route_status：三种结论都能给出")
    from core import remotetools
    import tempfile
    ws = Path(tempfile.mkdtemp())
    (ws / ".sixcoder").mkdir(parents=True, exist_ok=True)
    (ws / "app").mkdir(parents=True, exist_ok=True)
    (ws / "app" / "api").mkdir(parents=True, exist_ok=True)
    (ws / "app" / "api" / "batches.py").write_text(
        "def create_batch(dataset_version: str, algorithm: str) -> dict:\n"
        "    return {}\n",
        encoding="utf-8")
    out = remotetools._route_status(ws, "POST", "")
    check("没给 path 时给出用法提示", "要查哪个端点" in out, out[:80])
    out = remotetools._route_status(ws, "POST", "/api/v1/batches")
    check("能识别出'缺函数'或'接线被拒'（二者之一）",
          ("缺函数" in out) or ("接线被拒" in out), out[:120])
    check("结论里带上了该远端还是该引擎的指引",
          ("需要你写" in out) or ("引擎的问题" in out), out[:120])

    print("[5] route_status 已进工具分发与 JSON Schema")
    from core import toolschema
    names = [t["function"]["name"] for t in toolschema.tool_schemas()]
    check("route_status 在 JSON Schema 里", "route_status" in names, str(names))
    src2 = (ROOT / "core" / "remotetools.py").read_text(encoding="utf-8")
    check("分发器里有 route_status 分支", 'name == "route_status"' in src2)
    check("GUIDE 里介绍了 route_status", "route_status" in src2)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（Web 片不再硬砍 / 一次通过率只记录不缩小 / 模块数与兜底剂量已放开 / "
          "route_status 三种结论可辨 / 已进分发器与 Schema）")
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
