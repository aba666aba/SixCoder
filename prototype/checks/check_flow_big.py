"""跨模块集成冒烟自检：唯一能证明"模块之间接得上"的机械证据。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import contract as cm, flow  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

REQ = """代码规模：S

1) models.py：
   - make(x: str) -> dict：返回 {"id": x}
2) service.py：
   - reg(db: dict, x: str) -> dict：写入并返回 {"ok": True}
3) app.py：
   - run(db: dict) -> dict：编排，返回 {"ok": True}

示例（必须满足）：
make("a") == {"id": "a"}
reg({}, "a") == {"ok": True}
run({}) == {"ok": True}
"""

GOOD = {
    "models.py": 'def make(x: str) -> dict:\n    return {"id": x}\n',
    "service.py": 'import models\n\n\ndef reg(db: dict, x: str) -> dict:\n'
                  '    db[x] = models.make(x)\n    return {"ok": True}\n',
    "app.py": 'import service\n\n\ndef run(db: dict) -> dict:\n'
              '    service.reg(db, "a")\n    return {"ok": True}\n',
}


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws() -> Path:
    import tempfile
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(root)))


def main() -> int:
    print("跨模块集成冒烟自检")
    cs = cm.parse_requirement(REQ)

    print("[1] 三个模块接得上 → 通过")
    ws = _ws()
    for name, src in GOOD.items():
        (ws / name).write_text(src, encoding="utf-8")
    r = flow.integration_smoke(ws, cs)
    check("集成冒烟通过", r["ok"], str(r["fails"])[:200])
    check("报告里说清了模块与函数数量", "集成冒烟：通过" in flow.report_lines(r)[0],
          flow.report_lines(r)[0])

    print("[2] 中间模块 import 断了 → 必须抓到")
    ws2 = _ws()
    for name, src in GOOD.items():
        (ws2 / name).write_text(src, encoding="utf-8")
    (ws2 / "service.py").write_text("import not_exist_mod\n\n\ndef reg(db: dict, x: str) -> dict:\n"
                                    "    return {}\n", encoding="utf-8")
    r2 = flow.integration_smoke(ws2, cs)
    check("import 断链被抓到", not r2["ok"], str(r2["fails"])[:160])
    check("指出是哪个模块", any("service" in f for f in r2["fails"]), str(r2["fails"])[:160])

    print("[3] 签名不匹配（缺参数）→ 必须抓到")
    ws3 = _ws()
    for name, src in GOOD.items():
        (ws3 / name).write_text(src, encoding="utf-8")
    (ws3 / "app.py").write_text("import service\n\n\ndef run(db: dict) -> dict:\n"
                               "    return service.reg(db)\n", encoding="utf-8")
    r3 = flow.integration_smoke(ws3, cs)
    check("签名不匹配被抓到", not r3["ok"], str(r3["fails"])[:160])

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}（通过 / 断链 / 签名不匹配）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
