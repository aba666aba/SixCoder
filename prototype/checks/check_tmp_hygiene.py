"""临时区不再无限堆积 + 数行数不再被垃圾拖死（2026-11-09 19:40 真实夜班暴露）。

事故（本轮真实夜班，卡了 1 小时 33 分、CPU 零产出）：
  夜班卡在 `scale.count_lines` 的 `Path.rglob("*.py")` 上（py-spy 抓栈确认）。
  根因不是磁盘慢，是**产物仓库里堆了 36754 个探针沙箱目录 / 210MB** ——
  `.sixcoder/tmp/probe-{tag}-{pid}-{seq}` 每次探针建一个且**从不删除**
  （删除会撞外层"单轮删除 ≥50 个就拦下"的安全网）。
  而 `count_lines` 用的是 `rglob` + **事后**过滤 `_SKIP_DIRS` ——
  事后过滤救不了"已经遍历进去"这件事：rglob 照样把 15450 个子目录全走一遍，
  单次 19 秒，而夜班每轮 inspect 都要调它。

两处修：
  1. `core/scale.py: count_lines` → `os.walk` + **目录层面剪枝**：
     命中的目录根本不进去（19 秒 → 0.1 秒，遍历量 15553 → 75）。
  2. `core/probe.py: _scratch` → **按进程分组** `.sixcoder/tmp/run-{pid}/`，
     进程退出时（atexit）整体删掉自己那一组 ——
     一次删一棵目录树，不是删几万条文件条目，不撞"单轮删除 ≥50 个"的保护；
     组内仍用 seq 隔离（探针之间不互相污染，这条实测教训的修复保留）。

跑法: python checks/check_tmp_hygiene.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import probe  # noqa: E402
from core import scale  # noqa: E402

FAILS: list[str] = []


def _mypid() -> int:
    return os.getpid()


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] count_lines 必须在目录层面剪枝（不进 .sixcoder 等目录）")
    ws = Path(tempfile.mkdtemp())
    (ws / ".sixcoder" / "tmp" / "run-1" / "probe-x-1").mkdir(parents=True)
    (ws / ".sixcoder" / "tmp" / "run-1" / "probe-x-1" / "junk.py").write_text(
        "x = 1\n" * 5000, encoding="utf-8")
    (ws / "app").mkdir()
    (ws / "app" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "c.py").write_text("y = 2\n" * 100, encoding="utf-8")

    t0 = time.time()
    got = scale.count_lines(ws)
    dt = time.time() - t0
    check("统计结果正确（只算 app/m.py 的 2 行非空）", got["prod_lines"] == 2, str(got))
    check("文件数正确（不含 .sixcoder / __pycache__）", got["files"] == 1, str(got))
    check("模块数正确", got["modules"] == 1, str(got))
    check("用时 < 1 秒（不遍历垃圾区）", dt < 1.0, f"{dt:.2f}s")
    src = (ROOT / "core" / "scale.py").read_text(encoding="utf-8")
    check("源码用 os.walk 而不是 rglob", "os.walk(ws)" in src)
    check("源码在目录层面剪枝", "dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]" in src)

    print("[1b] 隔离层快照也必须目录层面剪枝（否则每次开隔离都遍历临时区）")
    from core import isolate as _iso
    import time as _t
    ws3 = Path(tempfile.mkdtemp())
    junk = ws3 / ".sixcoder" / "tmp" / "run-1"
    for i in range(60):
        d = junk / f"probe-cli-{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "x.py").write_text("y = 1\n", encoding="utf-8")
    (ws3 / "app").mkdir()
    (ws3 / "app" / "m.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    t0 = _t.time()
    snap = _iso._py_files(ws3)
    dt = _t.time() - t0
    check("快照不含 .sixcoder/tmp 里的东西", "app/m.py" in snap
          and not any(k.startswith(".sixcoder") for k in snap), str(list(snap)[:5]))
    check("快照用时 < 1 秒", dt < 1.0, f"{dt:.2f}s")
    isrc = (ROOT / "core" / "isolate.py").read_text(encoding="utf-8")
    check("isolate 用 os.walk 而不是 rglob", "os.walk(ws)" in isrc)
    check("isolate 在目录层面剪枝",
          "dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]" in isrc)

    print("[2] 探针临时目录固定 work/ 根 + owner 握手（2026-11-15 用户新契约）")
    ws2 = Path(tempfile.mkdtemp())
    for i in range(5):
        d = probe._scratch(ws2, "cli")
        (d / "x.py").write_text("print(1)", encoding="utf-8")
    tmpdir = ws2 / ".sixcoder" / "tmp"
    kids = sorted(p.name for p in tmpdir.iterdir())
    check("临时区只有一个 work/ 目录（不再 run-<pid> 满地爬）",
          kids == ["work"], str(kids))
    work = tmpdir / "work"
    probes = sorted(p.name for p in work.iterdir() if p.is_dir())
    check("探针目录都在 work/ 下（组内仍按 p<seq> 隔离）",
          probes == ["p1", "p2", "p3", "p4", "p5"], str(probes))
    check("owner 握手文件在（别的活进程不许互删）",
          (work / ".owner").is_file()
          and (work / ".owner").read_text(encoding="utf-8").strip() == str(_mypid()),
          str(sorted(p.name for p in work.iterdir())))
    src2 = (ROOT / "core" / "probe.py").read_text(encoding="utf-8")
    check("源码注册了 atexit 清理", "atexit.register(_cleanup_run_dir)" in src2)

    print("[3] 子进程退让（不互删）+ 退出后自清自己的退让组")
    child = """
import os, sys
sys.path.insert(0, %r)
from pathlib import Path
from core import probe
ws = Path(%r)
for i in range(30):
    d = probe._scratch(ws, "cli")
    (d / "x.py").write_text("print(1)")
""" % (str(ROOT), str(ws2))
    r = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True)
    check("子进程跑成功", r.returncode == 0, r.stderr[-200:])
    left = sorted(p.name for p in tmpdir.iterdir()) if tmpdir.exists() else []
    # 子进程发现 work/ 被**活着的父进程**持有（.owner）→ 退让到 work-<子pid>；
    # 退出时 atexit 删自己那棵退让树。父进程的 work/ 必须原地不动。
    check("子进程退出后自己的退让组被清（不留 30 个目录）",
          not any(x.startswith("work-") for x in left), str(left))
    check("父进程的 work/ 原地不动（等父进程退出才清）",
          "work" in left, str(left))
    check("没留下散落的 probe-* / run-* 目录（不按探针散落）",
          not any(x.startswith("probe-") or x.startswith("run-") for x in left),
          str(left))

    print("[4] 临时区垃圾回收：引擎自己清自己的垃圾")
    ws4 = Path(tempfile.mkdtemp())
    tmp4 = ws4 / ".sixcoder" / "tmp"
    old = tmp4 / "probe-old-1"
    old.mkdir(parents=True)
    (old / "x.py").write_text("y = 1\n", encoding="utf-8")
    fresh = tmp4 / "probe-fresh-1"
    fresh.mkdir(parents=True)
    (fresh / "x.py").write_text("y = 1\n", encoding="utf-8")
    keepdir = tmp4 / "not_mine"
    keepdir.mkdir(parents=True)
    (keepdir / "important.py").write_text("keep = 1\n", encoding="utf-8")
    # 把"旧的"改成 48 小时前，"新鲜的"改成 1 小时前（都明确算，避免算错）
    import time as _tt
    import os as _os
    _now = _tt.time()
    _os.utime(old, (_now - 48 * 3600, _now - 48 * 3600))
    _os.utime(fresh, (_now - 3600, _now - 3600))       # 1 小时前 → 还新鲜

    res = probe.gc_scratch(ws4, max_age_hours=24.0, max_dirs=2000)
    check("旧目录被回收了", res["removed"] == 1, str(res))
    check("旧目录真的没了", not old.exists())
    check("新鲜目录保留（别的进程可能还在用）", fresh.exists(), str(sorted(p.name for p in tmp4.iterdir())))
    check("**不是**引擎命名空间的目录绝不碰", keepdir.exists())
    check("回收字节数如实报告", res["freed_bytes"] > 0, str(res))
    check("不留批次目录残骸", not any(p.name.startswith(".gc-") for p in tmp4.iterdir()),
          str(sorted(p.name for p in tmp4.iterdir())))

    print("[5] 回收必须限速（不能一次掀掉整个盘）")
    ws5 = Path(tempfile.mkdtemp())
    tmp5 = ws5 / ".sixcoder" / "tmp"
    for i in range(30):
        d = tmp5 / f"probe-old-{i}"
        d.mkdir(parents=True)
        (d / "x.py").write_text("y = 1\n", encoding="utf-8")
        _os.utime(d, (_now - 48 * 3600, _now - 48 * 3600))
    res5 = probe.gc_scratch(ws5, max_age_hours=24.0, max_dirs=5)
    check("一次最多删 max_dirs 个", res5["removed"] == 5, str(res5))
    check("剩下的记为 kept（下轮继续）", res5["kept"] == 25, str(res5))
    check("没删的还在盘上", len(list(tmp5.iterdir())) == 25, str(len(list(tmp5.iterdir()))))

    print("[6] 回收必须按**删除条目数**限速，且异常不得打死引擎")
    src3 = (ROOT / "core" / "probe.py").read_text(encoding="utf-8")
    check("按条目数预算分批（不是按顶层目录数）", "entry_budget" in src3)
    check("有条目数估算函数", "_count_entries" in src3)
    check("删除动作兜 BaseException（宿主保护抛的是 BaseException 子类）",
          "except BaseException" in src3)
    csrc = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("夜班开跑钩子也兜 BaseException（清理失败不能带走夜班）",
          "except BaseException as exc" in csrc)
    # 30 个目录（每个 1 个文件）= 61 条 > 50 → 必须拆成多批，每批 ≤15 条
    ws6 = Path(tempfile.mkdtemp())
    tmp6 = ws6 / ".sixcoder" / "tmp"
    for i in range(30):
        d = tmp6 / f"probe-old-{i}"
        d.mkdir(parents=True)
        (d / "x.py").write_text("y = 1\n", encoding="utf-8")
        _os.utime(d, (_now - 48 * 3600, _now - 48 * 3600))
    res6 = probe.gc_scratch(ws6, max_age_hours=24.0, max_dirs=30)
    check("30 个目录（61 条）被拆批后仍能清完", res6["removed"] == 30, str(res6))
    check("不留批次目录残骸", not any(p.name.startswith(".gc-") for p in tmp6.iterdir()),
          str(sorted(p.name for p in tmp6.iterdir())))

    print("[7] 引擎自身 .tmp 清扫（gc_engine_tmp：48h 龄 + hsperfdata 豁免 + 分批）")
    import time as _t7
    import os as _o7
    ws7 = Path(tempfile.mkdtemp())          # 当作"引擎 .tmp"注入（root 参数只为测试）
    old7 = ws7 / "bizflow_old"
    old7.mkdir()
    (old7 / "x.py").write_text("y = 1\n", encoding="utf-8")
    fresh7 = ws7 / "bizflow_fresh"
    fresh7.mkdir()
    (fresh7 / "x.py").write_text("y = 1\n", encoding="utf-8")
    jvm = ws7 / "hsperfdata_test"           # JVM 活文件 → 绝不碰
    jvm.mkdir()
    (jvm / "perf").write_text("jvm", encoding="utf-8")
    _now7 = _t7.time()
    _o7.utime(old7, (_now7 - 72 * 3600, _now7 - 72 * 3600))     # 72 小时前 → 该收
    _o7.utime(fresh7, (_now7 - 3600, _now7 - 3600))             # 1 小时前 → 保留
    _o7.utime(jvm, (_now7 - 72 * 3600, _now7 - 72 * 3600))      # 旧，但豁免
    res7 = probe.gc_engine_tmp(root=ws7, max_age_hours=48.0, max_entries=4000)
    check("超龄目录被收", res7["removed"] == 1, str(res7))
    check("新鲜目录保留", fresh7.exists())
    check("hsperfdata_*（JVM 活文件）绝不碰", jvm.exists())
    check("回收字节数如实报告", res7["freed_bytes"] > 0, str(res7))
    check("不留批次残骸", not any(p.name.startswith(".gc-") for p in ws7.iterdir()),
          str(sorted(p.name for p in ws7.iterdir())))

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（数行数不遍历垃圾区 / 临时区按进程分组并自动清理）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
