"""分级门禁"阻断 vs 只报告"自检：档位开关必须真的管用，且不许把跳过当通过。

这条钉的是"能力上限"里阻断式门禁那一格。以前的覆盖只有一句读源码字符串
（`"quality as _q" in arch`），根本没验证"档位真的会拦"。这里的判定口径：

    · S 档：覆盖率/改动规模这类门禁必须 `evidence_ref == "skipped"`（不阻断），
      而且 detail 要写清"不设地板"。**跳过不是通过**：ok=True 但证据是 skipped；
    · M 档：覆盖率实测低于 80% → 必须 ok=False（阻断）；达标 → ok=True；
    · M 档：改动规模超过软上限 → 必须阻断，并给出可追溯的豁免口
      （SIXCODER_DIFF_CAP_REASON）；写了理由 → 放行且理由进报告；
    · S 档：改动规模不设硬上限（小项目切碎反而增加成本）；
    · 测不了（unavailable/error）**不许**冒充通过：ok 可以为 True，但证据必须说明测不了。

跑法: python checks/check_gatepolicy.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import arch  # noqa: E402
from core import gates as _gates_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

MOD = '''def add(a: int, b: int) -> int:
    """两数相加（这个是唯一被测到的函数）。"""
    total = a + b
    return total


def mul(a: int, b: int) -> int:
    """乘法：没人测 → 这几行都没执行过，用来把覆盖率压到地板以下。"""
    out = a * b
    out = out + 0
    out = out - 0
    return out


def sub(a: int, b: int) -> int:
    """减法：没人测。"""
    out = a - b
    out = out * 1
    out = out + 0
    return out


def div(a: int, b: int) -> float:
    """除法：没人测。"""
    if b == 0:
        return 0.0
    out = a / b
    out = out * 1.0
    return out
'''

TEST = '''from m import add


def test_add():
    assert add(1, 2) == 3
'''

TEST_FULL = '''from m import add, div, mul, sub


def test_add():
    assert add(1, 2) == 3


def test_mul():
    assert mul(3, 4) == 12


def test_sub():
    assert sub(5, 2) == 3


def test_div():
    assert div(6, 3) == 2.0
    assert div(1, 0) == 0.0
'''


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws(files: dict) -> Path:
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    ws = Path(tempfile.mkdtemp(dir=str(root), prefix="gatepol_"))
    for name, src in files.items():
        (ws / name).write_text(src, encoding="utf-8")
    return ws


def _git(ws: Path, *args: str) -> int:
    p = subprocess.run(["git", *args], cwd=str(ws), capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode


def _git_repo(files: dict) -> Path:
    ws = _ws(files)
    _git(ws, "init", "-q")
    _git(ws, "add", "-A")
    _git(ws, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return ws


def main() -> int:
    print("分级门禁：阻断 vs 只报告")

    print("[1] S 档：覆盖率与改动规模不设地板 —— 是 skipped，不是'通过'")
    ws_s = _ws({"m.py": MOD, "test_m.py": TEST})
    cov_s = arch.gate_coverage(ws_s, level="S")
    check("S 档覆盖率不阻断（ok=True）", cov_s.ok is True, str(cov_s.detail))
    check("S 档覆盖率证据是 skipped（跳过≠通过）", cov_s.evidence_ref == "skipped",
          str(cov_s.evidence_ref))
    check("S 档说清为什么不设地板", "不设覆盖率地板" in str(cov_s.detail), str(cov_s.detail))
    dif_s = arch.gate_diff_size(ws_s, level="S")
    check("S 档改动规模也 skipped", dif_s.evidence_ref == "skipped", str(dif_s.evidence_ref))

    print("[2] M 档：覆盖率低于 80% 必须阻断，达标必须放行")
    cov_m = arch.gate_coverage(ws_s, level="M")
    check("M 档实测覆盖率确实低于地板", cov_m.ok is False, f"ok={cov_m.ok} {cov_m.detail}")
    check("阻断理由带实测数字", "%" in str(cov_m.evidence_ref), str(cov_m.evidence_ref))
    check("阻断结论来自引擎实测（不是拍脑袋）", cov_m.evidence_kind == "engine_assertion",
          str(cov_m.evidence_kind))
    ws_full = _ws({"m.py": MOD, "test_m.py": TEST_FULL})
    cov_ok = arch.gate_coverage(ws_full, level="M")
    check("覆盖率达标 → 放行", cov_ok.ok is True, str(cov_ok.detail))
    # 实测 2026-11-07：不指定 --source 时，运行环境注入的 sitecustomize.py
    # （834 行 / 10% 覆盖）被算进 TOTAL，把真实 100% 拉到 13% → M 档永远假红。
    # 覆盖率只该测客户仓库里的代码，不许被外部文件拖低。
    check("覆盖率达标时读数确实 ≥80%（不是靠跳过蒙混）",
          cov_ok.evidence_ref.replace("%", "").isdigit()
          and float(cov_ok.evidence_ref.replace("%", "")) >= 80,
          str(cov_ok.evidence_ref))
    arch_src = (Path(__file__).resolve().parent.parent / "core" / "arch.py").read_text(
        encoding="utf-8")
    check("覆盖率限定 --source 到工作区", "--source={source}" in arch_src)
    check("report 按工作区过滤", "--include={source}/*" in arch_src)

    print("[3] M 档：改动规模超软上限必须阻断，并留可追溯豁免口")
    ws_g = _git_repo({"m.py": MOD})
    # 必须改**已跟踪**文件：git diff 数不到未跟踪的新文件（第一版样例就踩了这个坑）
    (ws_g / "m.py").write_text(MOD + "\n\n" + "\n".join(f"V{i} = {i}" for i in range(700)) + "\n",
                               encoding="utf-8")
    dif_big = arch.gate_diff_size(ws_g, level="M")
    check("超上限 → 阻断", dif_big.ok is False, str(dif_big.detail))
    check("阻断话里给出两种改法（拆分 / 说明理由）",
          "拆分成几个小改动" in str(dif_big.detail) and "SIXCODER_DIFF_CAP_REASON" in str(dif_big.detail),
          str(dif_big.detail))
    os.environ["SIXCODER_DIFF_CAP_REASON"] = "自检：验证豁免口"
    try:
        dif_waived = arch.gate_diff_size(ws_g, level="M")
    finally:
        os.environ.pop("SIXCODER_DIFF_CAP_REASON", None)
    check("写了理由 → 放行（不是静默放过）", dif_waived.ok is True, str(dif_waived.detail))
    check("放行时理由进了报告", "已说明理由" in str(dif_waived.detail), str(dif_waived.detail))
    dif_small = arch.gate_diff_size(_git_repo({"m.py": MOD}), level="M")
    check("没有改动 → 不阻断（不许误杀）", dif_small.ok is True, str(dif_small.detail))

    print("[4] level_gates：一次跑完，跳过的门禁不许冒充证据")
    gates = arch.level_gates(ws_s, "S")
    check("S 档跑出多道门禁", len(gates) >= 4, str(len(gates)))
    check("每道门禁都有名字与证据类型",
          all(g.gate and g.evidence_kind for g in gates), "有空字段")
    skipped = [g for g in gates if g.evidence_ref == "skipped"]
    check("S 档确实有门禁被跳过（说明分档生效）", bool(skipped), str([g.gate for g in gates]))
    check("跳过项 ok 都是 True（跳过≠失败，但也≠通过）",
          all(g.ok for g in skipped), str([(g.gate, g.ok) for g in skipped]))
    unmeasurable = [g for g in gates if g.evidence_ref in ("unavailable", "error", "no-layers",
                                                           "no-mutants")]
    check("测不了的项不报假通过结论（ok=True 但证据写明测不了）",
          all(g.ok for g in unmeasurable) and all(str(g.detail).strip() for g in unmeasurable),
          str([(g.gate, g.evidence_ref) for g in unmeasurable]))

    # ---- 虚构接口闸门（2026-11-08 00:52 真机事故）----
    # 作者 `from app.models import install_body_capture` —— 这个函数根本不存在；
    # 旧闸门只看"本模块绑没绑定"，import 就算绑定 → 结构体检 4 关全过、verified 7/7，
    # 一到 HTTP 探针就 ImportError，能力永远 absent。
    import tempfile
    _gws = Path(tempfile.mkdtemp())
    (_gws / "pkg").mkdir()
    (_gws / "pkg/__init__.py").write_text("", encoding="utf-8")
    (_gws / "pkg/models.py").write_text("def real_fn() -> int:\n    return 1\n", encoding="utf-8")
    (_gws / "pkg/api.py").write_text(
        "from pkg.models import real_fn, ghost_fn\n\n\n"
        "def use() -> int:\n    return real_fn()\n", encoding="utf-8")
    (_gws / "pkg/ok.py").write_text(
        "from pkg.models import real_fn\n\n\ndef use() -> int:\n    return real_fn()\n", encoding="utf-8")
    (_gws / "pkg/degrade.py").write_text(
        "try:\n    from pkg.models import ghost_fn\nexcept ImportError:\n    ghost_fn = None\n\n\n"
        "def use():\n    return ghost_fn\n", encoding="utf-8")

    g_bad = _gates_mod.gate_structure(_gws, ["pkg/api.py"])
    check("虚构接口被拦下", not g_bad.ok and "ghost_fn" in g_bad.detail, g_bad.detail[:120])
    check("反馈给出两条出路（实现到目标模块 / 改用已有）",
          "实现到 pkg.models" in g_bad.detail, g_bad.detail[:160])
    g_ok = _gates_mod.gate_structure(_gws, ["pkg/ok.py"])
    check("真实符号不误报", g_ok.ok, g_ok.detail[:120])
    g_try = _gates_mod.gate_structure(_gws, ["pkg/degrade.py"])
    check("try/except ImportError 的降级导入放行", g_try.ok, g_try.detail[:120])

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（S 跳过 / M 阻断且达标放行 / 豁免可追溯 / 跳过不冒充通过 / 虚构接口拦下）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
