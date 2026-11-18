"""架构约束（fitness functions）自检。

六条判据都是确定性的，必须既能抓出真问题、又不在干净代码上误杀：
  F1 环 / F2 层级方向 / F3 SDP / F4 跨子系统 / F5 扇出 / F6 内聚（LCOM 近似）。

跑法: python checks/check_archrules.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import archrules  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(files: dict[str, str]) -> Path:
    ws = Path(tempfile.mkdtemp(prefix="ar_"))
    for rel, text in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return ws


def rules(res: dict) -> set[str]:
    return {v["rule"] for v in res["violations"]}


def main() -> int:
    print("[1] 干净分层：core ← application ← adapter 不该误杀")
    ws = make({
        "matching_core/__init__.py": "",
        "matching_core/da.py": "def da_match(x):\n    return x\n",
        "app/__init__.py": "",
        "app/svc.py": "from matching_core.da import da_match\n\n\ndef run(x):\n    return da_match(x)\n",
        "app/api.py": "from app.svc import run\n\n\ndef handle():\n    return run(1)\n",
    })
    r = archrules.check(ws)
    check("干净项目全绿", r["ok"], str(r["violations"])[:160])
    check("规格来源被标成推断", r["spec_source"] == "inferred", str(r["spec_source"]))

    print("[2] F1：依赖成环要红")
    ws2 = make({
        "app/__init__.py": "",
        "app/a.py": "from app.b import fb\n\n\ndef fa():\n    return fb()\n",
        "app/b.py": "from app.a import fa\n\n\ndef fb():\n    return fa()\n",
    })
    r2 = archrules.check(ws2)
    check("环被抓到", "F1" in rules(r2), str(r2["violations"])[:160])

    print("[3] F2：核心层依赖外层要红（方向反了）")
    ws3 = make({
        "matching_core/__init__.py": "",
        "matching_core/da.py": "from app.api import handle\n\n\ndef da():\n    return handle()\n",
        "app/__init__.py": "",
        "app/api.py": "def handle():\n    return 1\n",
    })
    r3 = archrules.check(ws3)
    check("core→adapter 被抓到", "F2" in rules(r3), str(r3["violations"])[:160])

    print("[4] F3：SDP（声明 stability 后才有判定）")
    ws4 = make({
        "app/__init__.py": "",
        "app/stable.py": "from app.volatile import f\n\n\ndef g():\n    return f()\n",
        "app/volatile.py": "def f():\n    return 1\n",
        ".sixcoder/arch.json": json.dumps({
            "modules": {
                "app/stable.py": {"layer": "application", "subsystem": "x", "stability": 0.1},
                "app/volatile.py": {"layer": "application", "subsystem": "x", "stability": 0.9},
            },
            "extension_points": []}, ensure_ascii=False),
    })
    r4 = archrules.check(ws4)
    check("SDP 违规被抓到", "F3" in rules(r4), str(r4["violations"])[:160])
    check("声明来源被标成 declared", r4["spec_source"] == "declared", str(r4["spec_source"]))

    ws4b = make({
        "app/__init__.py": "",
        "app/stable.py": "from app.volatile import f\n\n\ndef g():\n    return f()\n",
        "app/volatile.py": "def f():\n    return 1\n",
        ".sixcoder/arch.json": json.dumps({
            "modules": {
                "app/stable.py": {"layer": "application", "subsystem": "x", "stability": 0.1},
                "app/volatile.py": {"layer": "application", "subsystem": "x", "stability": 0.9},
            },
            "extension_points": [{"module": "app/stable.py", "invert_dependency": True}],
        }, ensure_ascii=False),
    })
    r4b = archrules.check(ws4b)
    check("扩展点的依赖倒置被豁免", "F3" not in rules(r4b), str(r4b["violations"])[:160])

    print("[5] F4：跨子系统边要红")
    ws5 = make({
        "orders/__init__.py": "",
        "orders/svc.py": "from billing.svc import charge\n\n\ndef place():\n    return charge()\n",
        "billing/__init__.py": "",
        "billing/svc.py": "def charge():\n    return 1\n",
    })
    r5 = archrules.check(ws5)
    check("跨子系统被抓到", "F4" in rules(r5), str(r5["violations"])[:160])

    print("[6] F5：扇出超限要红")
    files = {"app/__init__.py": ""}
    imports = []
    for i in range(9):
        files[f"app/m{i}.py"] = f"def f{i}():\n    return {i}\n"
        imports.append(f"from app.m{i} import f{i}")
    files["app/hub.py"] = "\n".join(imports) + "\n\n\ndef hub():\n    return 1\n"
    r6 = archrules.check(make(files))
    check("扇出超限被抓到", "F5" in rules(r6), str(r6["violations"])[:160])

    print("[7] F6：一个文件装两件事要红")
    ws7 = make({
        "app/__init__.py": "",
        "app/two.py": (
            "A = []\nB = []\n\n\n"
            "def a1():\n    A.append(1)\n\n\ndef a2():\n    A.append(2)\n\n\n"
            "def b1():\n    B.append(1)\n\n\ndef b2():\n    B.append(2)\n"),
    })
    r7 = archrules.check(ws7)
    check("两簇（LCOM=2）被抓到", "F6" in rules(r7), str(r7["violations"])[:160])
    check("簇数算对", r7["metrics"]["lcom"].get("app/two.py") == 2,
          str(r7["metrics"]["lcom"]))

    print("[8] 重复实现率（防「为解耦而复制粘贴」）")
    body = "def f{i}(x):\n    y = x + 1\n    y = y * 2\n    y = y - 3\n    y = y / 4\n    return y\n"
    ws8 = make({"app/__init__.py": "",
                "app/d1.py": body.format(i=1), "app/d2.py": body.format(i=2)})
    r8 = archrules.check(ws8)
    check("重复实现被算出来", r8["metrics"]["dup"]["ratio"] > 0, str(r8["metrics"]["dup"]))

    print("[9] 报告必须写明「规格是推断还是声明」")
    lines = "\n".join(archrules.report_lines(r))
    check("推断要标出来", "推断" in lines, lines[:120])
    lines4 = "\n".join(archrules.report_lines(r4))
    check("声明要标出来", "声明" in lines4, lines4[:120])

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（F1–F6 各自能红 / 干净代码不误杀 / 报告标明规格来源）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
