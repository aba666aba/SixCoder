r"""一次跑完所有自检（新目录结构下的统一入口）。

用法（在 SixCoder\prototype 下）：
    python checks/run_all.py

跑完会清掉 prototype\.tmp\ 下的临时目录（可重建产物，留着只会堆垃圾、污染发布体检）。
注意：清理必须在 main() 里执行 —— 旧版写在 `raise SystemExit(main())` 之后，
是**永远不执行的死代码**，结果 .tmp 一路涨到 1607 个目录。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
TMP_ROOT = ROOT / ".tmp"


def _ensure_tmp() -> None:
    """子进程也写工作区内的临时目录（沙箱外不可写）。"""
    import os
    TMP_ROOT.mkdir(exist_ok=True)
    os.environ["TEMP"] = str(TMP_ROOT)
    os.environ["TMP"] = str(TMP_ROOT)


def _cleanup_tmp() -> None:
    """清掉 .tmp 下的临时目录（可重建产物，失败也不该拖垮自检）。"""
    if not TMP_ROOT.exists():
        return
    for d in TMP_ROOT.iterdir():
        try:
            shutil.rmtree(d) if d.is_dir() else d.unlink()
        except OSError:
            pass


def main() -> int:
    _ensure_tmp()
    scripts = sorted(p.name for p in HERE.glob("check_*.py"))
    failed: list[str] = []
    print(f"共 {len(scripts)} 个自检脚本\n")
    try:
        for name in scripts:
            if name == "check_remote_http.py":
                print(f"  skip  {name}（需要已启动的后端）")
                continue
            proc = subprocess.run([sys.executable, str(HERE / name)], cwd=str(ROOT),
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            tail = [ln for ln in (proc.stdout or "").splitlines()
                    if "全部通过" in ln or "有失败项" in ln]
            status = "PASS" if proc.returncode == 0 else "FAIL"
            print(f"  {status}  {name:<24} {tail[-1] if tail else ''}")
            if proc.returncode != 0:
                failed.append(name)
                for ln in (proc.stdout or "").splitlines():
                    if "FAIL" in ln:
                        print(f"        {ln.strip()[:110]}")
    finally:
        _cleanup_tmp()
    print(f"\n{'全部通过' if not failed else '有失败项: ' + ', '.join(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
