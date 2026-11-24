"""集成冒烟的导入口径自检（真实事故：模块 import 必然失败）。

事故（实测 2026-11-09）：集成冒烟用**模块短名**导入（batches），
而仓库布局是 app/api/batches.py → 正确导入名是 app.api.batches
→ ModuleNotFoundError: No module named 'batches'
→ 冒烟永远 100% 失败，"跨模块调用"从未真正跑过 → 业务流通无从验证。

跑法: python checks/check_flow_import.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core import flow, project  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    ws = Path(tempfile.mkdtemp())
    (ws / "app" / "api").mkdir(parents=True)
    (ws / "app" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "app" / "api" / "batches.py").write_text(
        "def create_batch(n: int) -> dict:\n"
        '    """建批次。"""\n'
        '    return {"n": n}\n', encoding="utf-8")
    (ws / "app" / "schemas.py").write_text(
        "def build_batch_create_request(n: int) -> dict:\n"
        '    """请求体。"""\n'
        '    return {"n": n}\n', encoding="utf-8")

    orig_map = project.build_map
    project.build_map = lambda contracts: {
        "modules": {
            "app/api/batches.py": {"module": "batches",
                                   "funcs": [{"name": "create_batch", "params": [{"type": "int"}],
                                              "owner": ""}]},
            "app/schemas.py": {"module": "schemas",
                               "funcs": [{"name": "build_batch_create_request",
                                          "params": [{"type": "int"}], "owner": ""}]},
        },
        "deps": {},
    }
    try:
        r = flow.integration_smoke(ws, {})
    finally:
        project.build_map = orig_map

    imports = r.get("imports") or {}
    check("模块 import 成功（用路径推出的点分名）",
          all(v == "ok" for v in imports.values()) and len(imports) == 2, str(imports))
    check("没有 ModuleNotFoundError",
          not any("ModuleNotFoundError" in str(v) for v in imports.values()), str(imports))
    check("跨模块调用真的跑了（不是 missing）",
          r.get("ok") is True, str(r.get("fails"))[:200])
    check("fails 为空", not (r.get("fails") or []), str(r.get("fails"))[:200])

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（按路径导入 / 跨模块调用真跑）")
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
