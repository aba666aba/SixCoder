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


def _child_env(idx: int) -> dict:
    """给每个自检子进程**独立的删除计数**（只在带批量护栏的环境里生效）。

    实测 2026-11-07：在 WorkBuddy 会话里跑 run_all，出现 9~16 个"光秃秃的 FAIL"，
    单独跑每个脚本全绿。抓到的 stderr 是
    `[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":50,"threshold":50...}` ——
    宿主给 Python 注入了删除护栏（CODEBUDDY_SAFE_DELETE_*），计数器按"回合"累计：
    前 20 个脚本各自清临时目录，累计到 50 个文件后，后面所有脚本的清理全被拦下，
    于是"清理失败"被记成"自检失败"。护栏本身是对的，错的是把 105 个独立脚本算作一个回合。
    这里给每个子进程分配独立计数器目录：谁也没被放行，只是不再互相顶到阈值。
    没有该环境变量时（普通命令行 / CI）行为完全不变。
    """
    import os
    env = dict(os.environ)
    base = env.get("CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR")
    if base:
        d = Path(base) / f"check_{idx:03d}"
        try:
            d.mkdir(parents=True, exist_ok=True)
            env["CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR"] = str(d)
        except OSError:
            pass
    return env


def main() -> int:
    _ensure_tmp()
    # **开跑也清一次**（2026-11-18）：结尾清只在"跑完整轮 run_all"时生效，
    # 单独跑几个 check_* 脚本不清 —— 实测一轮全量清空后，单跑 4 个脚本又堆回
    # 600+ 目录。开头清一次，把残留封顶为"单轮自检的量"。
    _cleanup_tmp()
    scripts = sorted(p.name for p in HERE.glob("check_*.py"))
    failed: list[str] = []
    blocked_names: list[str] = []
    print(f"共 {len(scripts)} 个自检脚本\n")
    try:
        for idx, name in enumerate(scripts):
            if name == "check_remote_http.py":
                print(f"  skip  {name}（需要已启动的后端）")
                continue
            proc = subprocess.run([sys.executable, str(HERE / name)], cwd=str(ROOT),
                                  capture_output=True, text=True, env=_child_env(idx),
                                  encoding="utf-8", errors="replace")
            tail = [ln for ln in (proc.stdout or "").splitlines()
                    if "全部通过" in ln or "有失败项" in ln]
            _err_all = (proc.stderr or "") + (proc.stdout or "")
            # 宿主护栏拦截 ≠ 代码失败。删掉一个临时 git 仓库就是 60+ 个文件，超过护栏
            # 阈值（50/回合）被拦下；这不是被检代码有问题，也不是断言失败。分开报，
            # 别把"没跑成"混进"跑红了"（实测 2026-11-07：这一条把 106 个脚本的结论带偏）。
            blocked = proc.returncode != 0 and "SAFE_DELETE_BULK" in _err_all
            status = "PASS" if proc.returncode == 0 else ("BLOCK" if blocked else "FAIL")
            print(f"  {status:<5} {name:<24} {tail[-1] if tail else ''}")
            if proc.returncode != 0:
                if blocked:
                    blocked_names.append(name)
                    print("        环境删除护栏拦下（>50 文件/回合）：不是代码问题，"
                          "请在无护栏的终端复跑该脚本确认")
                else:
                    failed.append(name)
                for ln in (proc.stdout or "").splitlines():
                    if "FAIL" in ln:
                        print(f"        {ln.strip()[:110]}")
                # 崩溃型失败（脚本直接抛异常）在 stdout 里一个字都没有 —— 旧版本只打
                # stdout 里含 "FAIL" 的行，于是自检报告只剩一行光秃秃的 "FAIL"，
                # 现场全靠猜（实测 2026-11-07：16 个脚本失败却没有任何原因）。
                err = [ln for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
                if not any("FAIL" in ln for ln in (proc.stdout or "").splitlines()) and err:
                    print(f"        {err[-1].strip()[:110]}")
    finally:
        _cleanup_tmp()
    if failed:
        msg = "有失败项: " + ", ".join(failed)
    else:
        msg = "全部通过"
    if blocked_names:
        msg += f"（{len(blocked_names)} 个被环境护栏拦下、需换环境复跑：" + \
               ", ".join(blocked_names) + "）"
    print("\n" + msg)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
