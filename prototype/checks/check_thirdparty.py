"""钉住「第三方 provider 模式」检测（2026-11-15，开发文档 §13.11）。

## 要证明的两件事

**D8.1（静态）**：业务代码直接 `import stripe` → 必须报 P1；
provider 模块自己 import SDK → **不报**（那是正确位置）。

**D8.2（场景）**：按 provider 模式写（有 `set_provider()` + 业务入口）→
能跑替身场景，且五种事故都能测：
成功 / 超时 / 重复（幂等）/ 状态不符 / 缺注入点。
"""
from __future__ import annotations

# 引擎不含项目身份（2026-11-19）：路径来自 --repo / SIXCODER_REPO / _logs/engine.json
import sys as _sys
_ENGINE = __import__('pathlib').Path(__file__).resolve().parents[1]
if str(_ENGINE) not in _sys.path:
    _sys.path.insert(0, str(_ENGINE))
from core.runtime_cfg import repo_root as _repo_root  # noqa: E402


import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import detectors, thirdparty  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name +
          (("  — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


# ── 违规产物：业务代码直连 SDK ──────────────────────────────────────────────
BAD_BIZ = """def pay(oid):
    import stripe
    return stripe.Charge.create(amount=1)
"""

# ── 合规产物：provider 抽象 + 注入点 ───────────────────────────────────────
PROVIDER = '''_impl = None


def set_provider(p):
    global _impl
    _impl = p


def get_provider():
    if _impl is None:
        raise RuntimeError("provider not set")
    return _impl
'''

ORDER = '''from app.providers import payment


def pay(oid):
    prov = payment.get_provider()
    r = prov.charge(oid)
    return {"status": "paid" if (r or {}).get("status") == "ok" else "pending"}
'''


def main() -> int:
    print("[1] D8.1 业务代码直连厂商 SDK → P1")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        (w / "app").mkdir()
        (w / "app" / "order.py").write_text(BAD_BIZ, encoding="utf-8")
        errs = thirdparty.check_provider_pattern(w)
        check("抓到 `import stripe`", any("stripe" in e.what for e in errs),
              str([e.what[:50] for e in errs]))
        check("级别是 P1（不可测 = 硬伤）",
              any(e.level == "P1" for e in errs), str([e.level for e in errs]))
        check("位置精确到行", any(":" in e.where for e in errs),
              str([e.where for e in errs]))
        check("缺 providers/ 目录时额外报 P2",
              any(e.level == "P2" and "no-provider-dir" in (e.symbol or "")
                  for e in errs), str([(e.level, e.symbol) for e in errs]))

        print("[2] provider 模块自己 import SDK → 不报（那是正确位置）")
        (w / "app" / "providers").mkdir()
        (w / "app" / "providers" / "__init__.py").write_text("", encoding="utf-8")
        (w / "app" / "providers" / "payment.py").write_text(
            "import stripe  # provider 里包 SDK 是对的\n", encoding="utf-8")
        errs2 = thirdparty.check_provider_pattern(w)
        check("排除 app/providers 后不再报 provider 自身",
              not any("providers/payment" in (e.where or "") for e in errs2),
              str([e.where for e in errs2]))
        check("但业务文件仍然报", any("app/order.py" in (e.where or "") for e in errs2),
              str([e.where for e in errs2]))

    print("[3] D8.2 场景：合规产物能跑替身，五种事故都测得到")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        for d in ("app", "app/providers", "app/services"):
            (w / d).mkdir(parents=True, exist_ok=True)
            (w / d / "__init__.py").write_text("", encoding="utf-8")
        (w / "app" / "providers" / "payment.py").write_text(PROVIDER, encoding="utf-8")
        (w / "app" / "services" / "order.py").write_text(ORDER, encoding="utf-8")

        probe = {
            "kind": "thirdparty", "provider": "payment",
            "module": "app.providers.payment", "entry": "app.services.order:pay",
            "scenarios": [
                {"name": "支付成功", "stub": {"charge": {"status": "ok"}},
                 "args": ["O1"], "expect": {"status": "paid"}},
                {"name": "第三方超时", "stub": {"charge": {"raise": "Timeout"}},
                 "args": ["O2"], "expect": {"raise": "Timeout"}},
                {"name": "重复回调幂等", "replay": 2,
                 "stub": {"charge": {"status": "ok"}},
                 "args": ["O3"], "expect": {"status": "paid"}},
                {"name": "状态不符（故意）", "stub": {"charge": {"status": "ok"}},
                 "args": ["O4"], "expect": {"status": "wrong"}},
            ]}
        errs = thirdparty.run_scenarios(w, probe)
        got = " ".join(e.what for e in errs)
        check("成功场景不报错", "支付成功" not in got, got[:200])
        check("超时场景按期望抛错 → 不报", "第三方超时" not in got, got[:200])
        check("重复 2 次按期望通过 → 不报", "重复回调幂等" not in got, got[:200])
        check("状态不符被报出来（P2）",
              "状态不符" in got and any(e.level == "P2" for e in errs), got[:200])
        check("报错带替身调用记录（证据）",
              any("替身调用记录" in (e.evidence or "") for e in errs),
              str([(e.evidence or "")[:60] for e in errs]))

        print("[4] 缺注入点 → P1（provider 模式的验收点）")
        bad_probe = dict(probe, module="", entry="")
        errs2 = thirdparty.run_scenarios(w, bad_probe)
        check("缺 module/entry → P1 说明不可测",
              any(e.level == "P1" and "no-injection" in (e.symbol or "") for e in errs2),
              str([(e.level, e.symbol, e.what[:40]) for e in errs2]))

        # 没有 set_provider 的 provider 也要报
        (w / "app" / "providers" / "noset.py").write_text("X = 1\n", encoding="utf-8")
        errs3 = thirdparty.run_scenarios(
            w, dict(probe, module="app.providers.noset"))
        check("provider 没有 set_provider() → P1",
              any("no-setter" in (e.symbol or "") for e in errs3),
              str([(e.level, e.symbol) for e in errs3]))

    print("[5] 现有产物不误报")
    real = _repo_root()
    if real.is_dir():
        errs = detectors.d8_thirdparty(real)
        check("真实产物 0 条（没有厂商 SDK 直连）", len(errs) == 0,
              str([(e.level, e.where, e.what[:50]) for e in errs[:3]]))
    else:
        print("  （跳过：产物目录不在）")

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


# 未配置项目路径时：这些自检依赖真实产物，明确 SKIP（不是 FAIL）
if __name__ == "__main__" and _repo_root() is None:
    print("SKIP：未配置项目路径（--repo / SIXCODER_REPO / _logs/engine.json），本自检需要真实产物仓")
    raise SystemExit(0)

if __name__ == "__main__":
    raise SystemExit(main())