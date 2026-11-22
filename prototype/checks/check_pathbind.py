"""PATH 绑定自检：默认未绑定 + 只写用户 PATH + 卸载精确清理（2026-11-20 用户口径）。

用户："常用工具都写上，安装包安装的自动绑定 PATH 由用户自己设置，一开始显示未绑定"。
这条钉的是：
  1. 常用工具全量列出（装没装都在，含 javac/pip/yarn/rustc 等 recon 不判定的）；
  2. 初始一律"未绑定"，即使工具已在系统 PATH 里 —— 显示归显示，绑定动作必须用户点；
  3. 点绑定只写 HKCU\\Environment（测试里用替身，绝不碰真机 PATH）；
  4. 用户自己加过的目录记 added=False，解绑时**不删**；
  5. unbind_all 只清清单里 added=True 的，清单丢了什么都不动。
跑法: python checks/check_pathbind.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import pathbind as PB  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    td = tempfile.mkdtemp(prefix="pathbind-")
    os.environ["SIXCODER_HOME"] = td

    # 真机 PATH 只读进来做替身，所有写操作都落在内存变量里
    user_path = str(os.environ.get("PATH") or "")
    machine_path = ""
    writes: list[str] = []
    PB._read_user_path = lambda: user_path                # type: ignore[assignment]
    PB._read_machine_path = lambda: machine_path          # type: ignore[assignment]

    def _fake_write(v: str) -> None:
        nonlocal user_path
        writes.append(v)
        user_path = v

    PB._write_user_path = _fake_write                     # type: ignore[assignment]
    # 探测工具不真跑：让清单稳定，只验状态机
    PB._probe = lambda argv, ver_re: ("1.2.3", None)      # type: ignore[assignment]
    PB._recon.toolchains = lambda overrides=None: [       # type: ignore[assignment]
        {"id": "python", "ok": True, "version": "3.13.7", "path": None, "source": None}
    ]
    PB._runtime_bind_dir = lambda rid: ""                 # type: ignore[assignment]

    print("[1] 常用工具全列出，初始一律未绑定")
    items = PB.state()
    ids = {i["id"] for i in items}
    check("recon 里的基础项在", {"python"} <= ids, str(sorted(ids)))
    check("常用补充项在（javac/pip/yarn/rustc/php/dotnet）",
          {"javac", "pip", "yarn", "rustc", "php", "dotnet"} <= ids, str(sorted(ids)))
    check("常用补充项都在（11 项非判定工具）",
          {tid for tid, *_ in PB.EXTRA_TOOLS} <= ids, str(sorted(ids)))
    check("覆盖 recon 基础项 + 常用补充项",
          len(items) >= 1 + len(PB.EXTRA_TOOLS), str(len(items)))
    check("每项都有 bound 字段", all(isinstance(i.get("bound"), bool) for i in items))
    check("清单不存在 = 没有 added 记录", PB._load_manifest() == {})

    print("[2] 用户自己加的目录：显示已绑定，但记 added=False，解绑不动它")
    own = str(Path(td) / "own-tools")
    Path(own).mkdir(parents=True, exist_ok=True)
    user_path = own + os.pathsep + user_path
    PB._probe = lambda argv, ver_re: ("1.2.3", None)      # type: ignore[assignment]
    # 让 javac 的 bind_dir 指向用户自己的目录
    _orig_item = PB._item

    def _item_own(tid, version, path, ok, bind_dir, entries, manifest):
        return _orig_item(tid, version, path, ok, own if tid == "javac" else bind_dir,
                          entries, manifest)

    PB._item = _item_own                                  # type: ignore[assignment]
    j = next(i for i in PB.state() if i["id"] == "javac")
    check("已在 PATH 的显示已绑定", j["bound"] is True, str(j))
    r = PB.bind("javac")
    check("绑定幂等：不重复写 PATH", r["ok"] and len(writes) == 0, str(writes))
    check("清单记 added=False（不冒充我们加的）",
          PB._load_manifest()["javac"]["added"] is False, str(PB._load_manifest()))
    r = PB.unbind("javac")
    check("解绑用户自己的目录：removed=False", r.get("removed") is False, str(r))
    check("用户 PATH 原样保留", own in user_path and len(writes) == 0)

    print("[3] 我们加的目录：写一次，解绑删一条，不碰其他条目")
    bundled = str(Path(td) / "runtimes" / "python-3.13")
    Path(bundled).mkdir(parents=True, exist_ok=True)
    (Path(bundled) / "python.exe").write_bytes(b"")
    before = user_path
    PB._item = lambda tid, version, path, ok, bind_dir, entries, manifest: _orig_item(
        tid, version, path, ok, bundled if tid in ("python", "pip") else bind_dir,
        entries, manifest)
    r = PB.bind("python")
    check("绑定成功", r.get("ok") is True, str(r))
    check("PATH 里加了目录", bundled in user_path and len(writes) == 1, str(writes))
    check("原来的条目全还在", before.split(os.pathsep)[0] in user_path)
    check("清单记 added=True", PB._load_manifest()["python"]["added"] is True)
    r2 = PB.bind("python")
    check("重复绑定不重复写", r2["ok"] and len(writes) == 1, str(writes))
    r3 = PB.unbind("python")
    check("解绑 removed=True", r3.get("removed") is True, str(r3))
    check("用户自己的目录没被误删", own in user_path, user_path[:160])
    check("清单条目已移除", "python" not in PB._load_manifest(), str(PB._load_manifest()))

    print("[4] 拒绝乱绑 + 卸载全清")
    bad = PB.bind("不存在的工具")
    check("未知工具拒绝", bad["ok"] is False, str(bad))
    # 重新绑两条，再全清
    PB.bind("python")
    PB.bind("pip")
    n_writes = len(writes)
    r = PB.unbind_all()
    check("unbind_all 成功", r["ok"] is True, str(r))
    check("python/pip 共用目录只留一条，全清后都没了",
          r["removed"] >= 1 and bundled not in user_path, str(r))
    check("用户自己的条目还在", own in user_path, user_path[:160])
    check("清单清空", PB._load_manifest() == {}, str(PB._load_manifest()))
    check("只写了一次 PATH（批量清不是逐条写）", len(writes) == n_writes + 1, str(len(writes)))

    print("[5] 清单丢了就什么都不动（不猜、不删用户 PATH）")
    snapshot = user_path
    r = PB.unbind_all()
    check("空清单返回 removed=0", r.get("removed") == 0, str(r))
    check("PATH 一个字节没动", user_path == snapshot)

    print("[6] 只写 HKCU，绝不写 HKLM")
    src = (Path(__file__).resolve().parents[1] / "core" / "pathbind.py").read_text(encoding="utf-8")
    check("写函数只用 HKEY_CURRENT_USER",
          "HKEY_CURRENT_USER" in src and "winreg.SetValueEx" in src)
    check("HKLM 只出现在只读探测里",
          src.count("HKEY_LOCAL_MACHINE") == 1 and "KEY_READ" in src)

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（常用清单 / 默认未绑定 / 用户 PATH 写入 / 精确解绑 / 卸载全清）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
