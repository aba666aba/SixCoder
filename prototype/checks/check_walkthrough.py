"""端到端链路走查自检（2026-11-23）。

钉住：
  · 走查全绿且关键步骤齐全（样式/半自动/插件/判据/部署视图/编排视图）；
  · **只读/内存态**：跑完不改用户 settings、不残留 __walk__ 缓存、不连真实主机；
  · 与 verify 分工不混：describe 明确"不改配置/不连主机"；
  · 自检入口支持分批（--list / --batch），避免单进程过长被环境拦下。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" :: {detail[:200]}" if detail and not ok else ""))


def main() -> int:
    from core import walkthrough as w

    tf = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    tf.write("{}")
    tf.close()
    os.environ["SIXCODER_SETTINGS"] = tf.name
    try:
        print("[1] 能力声明")
        d = w.describe()
        check("声明不改用户配置", d["mutates_user_config"] is False)
        check("声明不连真实主机", d["connects_real_host"] is False)
        check("覆盖 7 项旅程", len(d["walks"]) == 7, str(d["walks"]))

        print("[2] 走查全绿 + 关键步骤齐全")
        r = w.walkthrough()
        bad = [s for s in r["steps"] if s["status"] == "fail"]
        check("走查通过", r["ok"] is True, str(bad))
        names = {s["name"] for s in r["steps"]}
        need = {"样式库可读", "半自动人填闭环", "插件适配清单", "判据开放清单",
                "部署目标视图", "多机编排视图"}
        check("关键步骤都在", need <= names, str(sorted(names)))
        check("未选风格时标 skip 而非 fail",
              all(s["status"] != "fail" for s in r["steps"] if "风格约定" in s["name"]))

        print("[3] 只读：不改用户 settings")
        raw = json.loads(Path(tf.name).read_text(encoding="utf-8"))
        check("settings 未被写入任何段", raw == {}, str(list(raw.keys())))

        print("[4] 内存态：不留 __walk__ 残留")
        from core import human_once as h
        left = [x for x in h.pending() if x["namespace"] == "__walk__"]
        check("__walk__ 命名空间已清空", left == [], str(left))

        print("[5] 不连真实主机：未启用也照常走查")
        from core import deploy as dp
        check("部署通道保持未启用", dp.public_config()["enabled"] is False)

        print("[6] 自检入口支持分批（B-6）")
        ra = subprocess.run([sys.executable, str(ROOT / "checks" / "run_all.py"), "--list"],
                            cwd=str(ROOT), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
        check("--list 列出脚本总数", ra.returncode == 0 and "个自检脚本" in ra.stdout,
              (ra.stdout or "")[:120])
        rb = subprocess.run([sys.executable, str(ROOT / "checks" / "run_all.py"),
                             "--batch", "99/99"], cwd=str(ROOT), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=60)
        check("--batch 越界批次为空且不报错", rb.returncode == 0, (rb.stdout or "")[:120])
        rc = subprocess.run([sys.executable, str(ROOT / "checks" / "run_all.py"),
                             "--only", "check_devdoc.py"], cwd=str(ROOT),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=180)
        check("--only 只跑指定脚本", rc.returncode == 0 and "check_devdoc.py" in rc.stdout,
              (rc.stdout or "")[:160])

    except Exception as exc:                             # noqa: BLE001
        check("自检未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        os.environ.pop("SIXCODER_SETTINGS", None)
        try:
            Path(tf.name).unlink(missing_ok=True)
        except Exception:                                # noqa: BLE001
            pass

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
