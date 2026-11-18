"""依赖自举的"自己人 vs 第三方"自检（2026-11-07 真实夜班暴露的 bug）。

事故：夜班的每一片都打一行 `notice 依赖装不上：matching_core`，产物 requirements.txt
里也真写着 `matching_core` / `app`。真因是 `deps.scan()` 只排除了标准库 ——
工作区里的本地包（`app/`、`matching_core/`）从引擎所在目录 `find_spec` 必然找不到，
于是被当成"缺依赖"，去 `pip install matching_core`（PyPI 上没有这个包）：
  · 每次观测都多花一次必然失败的 pip（还拖慢观测）；
  · requirements.txt 被写脏 —— 用户照着 `pip install -r requirements.txt` 直接报错，
    而"装完能直接启动"是硬要求；
  · 依赖失败的噪音盖住了真正缺的第三方包。

判据：本地包不算依赖；真第三方照旧识别；标准库照旧排除。

跑法: python checks/check_deps_local.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import deps  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    ws = Path(tempfile.mkdtemp(prefix="dep_"))
    (ws / "app" / "services").mkdir(parents=True)
    (ws / "matching_core").mkdir()
    (ws / "app" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "app" / "services" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "matching_core" / "__init__.py").write_text("", encoding="utf-8")
    (ws / "matching_core" / "da.py").write_text(
        "def da_match():\n    return []\n", encoding="utf-8")
    (ws / "app" / "services" / "batches.py").write_text(
        "import json\nimport os\nfrom fastapi import APIRouter\n"
        "from matching_core.da import da_match\n"
        "from app.services.audit import log_event\n"
        "\n\ndef run():\n    return da_match()\n", encoding="utf-8")
    (ws / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")

    mods = deps.scan(ws)
    check("本地包 matching_core 不算第三方依赖", "matching_core" not in mods, str(sorted(mods)))
    check("本地包 app 不算第三方依赖", "app" not in mods, str(sorted(mods)))
    check("真第三方仍被识别", "fastapi" in mods, str(sorted(mods)))
    check("标准库仍被排除", "json" not in mods and "os" not in mods, str(sorted(mods)))

    req = deps.ensure(ws, install=False)
    text = (ws / "requirements.txt").read_text(encoding="utf-8")
    lines = {ln.strip() for ln in text.splitlines() if ln.strip()}
    check("requirements.txt 不写本地包",
          not ({"app", "matching_core"} & lines), text.replace("\n", "|"))
    check("requirements.txt 有 fastapi", "fastapi" in lines, text.replace("\n", "|"))
    check("ASGI 项目补了 uvicorn", "uvicorn" in lines, text.replace("\n", "|"))
    check("没有把本地包报成装不上",
          all(x.get("module") not in ("app", "matching_core")
              for x in (req.get("failed") or [])), str(req.get("failed"))[:120])

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（本地包不算依赖 / 真第三方照旧识别 / requirements 不写自己人）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
