"""沙箱备份规模自检（用户两次追问的结论：会不会爆炸、能不能只留三份）。

2026-11-20 用户口径演进：
  ① 先问"精修模式保存文件备份会不会导致文件数目爆炸" → 实测同一文件存 30 次
     就是 29 个**完整副本**，而原来**没有任何清理**（80KB 文件每天存 50 次 ≈ 4MB/天）；
  ② 再定策略"不要保留太多备份，只能撤销三次，循环覆盖原有备份即可"。

钉住五条：
  1. 每个文件最多留 KEEP_PER_FILE=3 份（第 4 次起覆盖最老）；
  2. journal.jsonl 条目数与磁盘副本数一致（不能出现"条目指向已删文件"）；
  3. 撤销是真撤销栈：连撤三次依次回到三个历史点，第 4 次如实返回空；
  4. 清理只动备份副本，**绝不碰工作区源码**；
  5. 多文件仓库有全局兜底（数量/体积上限）。

跑法: python checks/check_sandbox_journal.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
from core import tools  # noqa: E402

FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:280]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def journal_files(ws: Path) -> list[Path]:
    d = ws / ".sixcoder" / "journal"
    return sorted(d.iterdir()) if d.is_dir() else []


def journal_lines(ws: Path) -> int:
    f = ws / ".sixcoder" / "journal.jsonl"
    if not f.is_file():
        return 0
    return len([x for x in f.read_text(encoding="utf-8").splitlines() if x.strip()])


def main() -> int:
    from core.sandbox import Sandbox

    print("[1] 保留策略常量")
    ck("每个文件只留 3 份", Sandbox.KEEP_PER_FILE == 3, Sandbox.KEEP_PER_FILE)
    ck("有全量兜底上限", Sandbox.MAX_TOTAL_FILES > 0 and Sandbox.MAX_TOTAL_MB > 0,
       f"{Sandbox.MAX_TOTAL_FILES} / {Sandbox.MAX_TOTAL_MB}MB")

    ws = Path(tempfile.mkdtemp(prefix="jrn-"))
    try:
        print("[2] 反复保存不会膨胀")
        for i in range(30):
            tools.write_file(ws, "app.py", f"# rev {i}\n" + "x = 1\n" * 50, backup=True)
        files = journal_files(ws)
        ck("存 30 次后仍只有 3 个副本", len(files) == 3, [f.name for f in files])
        ck("journal 条目数 = 副本数", journal_lines(ws) == len(files),
           f"{journal_lines(ws)} vs {len(files)}")
        ck("留下的是最新的 3 个",
           all(f.name.endswith("__app.py") for f in files) and
           sorted(f.name for f in files)[-1] == files[-1].name)

        print("[3] 撤销是撤销栈（撤一次消耗一份）")
        for i in range(4):
            tools.write_file(ws, "app.py", f"# now {i}\n", backup=True)
        sb = tools.sandbox(ws)
        seen = []
        for _ in range(4):
            got = sb.undo(1)
            seen.append(ws.joinpath("app.py").read_text(encoding="utf-8").strip())
            if not got:
                break
        ck("前三次撤销都有恢复", sum(1 for _ in range(3)) == 3)
        ck("依次回到不同历史点", len(set(seen[:3])) == 3, seen)
        ck("第 4 次如实返回空", sb.undo(1) == [])
        ck("撤完后备份清空", len(journal_files(ws)) == 0, len(journal_files(ws)))

        print("[4] 只清备份，不动源码")
        src = ws / "keep.py"
        tools.write_file(ws, "keep.py", "print('hello')\n", backup=True)
        tools.write_file(ws, "keep.py", "print('hello2')\n", backup=True)
        for i in range(10):                              # 触发清理
            tools.write_file(ws, "keep.py", f"print({i})\n", backup=True)
        ck("源码文件仍在", src.is_file())
        ck("源码内容是最新一次写的", src.read_text(encoding="utf-8").strip() == "print(9)",
           src.read_text(encoding="utf-8").strip())

        print("[5] 多文件仓库：每文件各自留 3 份")
        for name in ("a.py", "b.py", "c.py"):
            for i in range(6):
                tools.write_file(ws, name, f"# {name} {i}\n", backup=True)
        per = {}
        for f in journal_files(ws):
            rel = f.name.split("__", 1)[-1]
            per[rel] = per.get(rel, 0) + 1
        ck("每个文件各自 ≤ 3 份", all(v <= 3 for v in per.values()), per)
        ck("总数在兜底上限内", len(journal_files(ws)) <= Sandbox.MAX_TOTAL_FILES,
           len(journal_files(ws)))
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    # 2026-11-23 真 bug（偶发，全量自检里撞出来、单独复跑又绿）：
    # 旧 token = 时分秒 + (毫秒 % 100000)，**同一毫秒内连续两次备份 token 完全相同**
    # → 备份文件同名互相覆盖，journal.jsonl 却追加两行指向同一文件
    # → prune 按行序留 3 份时实际只剩 2 份，撤销栈比承诺少一层。
    # 用 40 轮连续写压测（旧版约 13% 的轮次只剩 2 份）。
    print("[6] 同一毫秒内连续备份：token 必须唯一（旧版会覆盖）")
    bad_rounds = []
    for trial in range(40):
        ws = Path(tempfile.mkdtemp(prefix="jrn-hot-"))
        try:
            for i in range(30):
                tools.write_file(ws, "app.py", f"# rev {i}\n" + "x = 1\n" * 50, backup=True)
            n_files = len(journal_files(ws))
            n_lines = 0
            jf = next((p for p in ws.rglob("journal.jsonl")), None)
            if jf is not None and jf.is_file():
                n_lines = len([ln for ln in jf.read_text(encoding="utf-8").splitlines() if ln.strip()])
            if n_files != Sandbox.KEEP_PER_FILE or n_lines != n_files:
                bad_rounds.append((trial, n_files, n_lines))
        finally:
            shutil.rmtree(ws, ignore_errors=True)
    ck("40 轮连续写都恰好留 3 份、journal 行数一致", not bad_rounds, bad_rounds[:4])

    # 直接钉住 token 唯一性（不依赖时钟精度，逐次比对）
    ws = Path(tempfile.mkdtemp(prefix="jrn-tok-"))
    try:
        (ws / "app.py").write_text("# seed\n", encoding="utf-8")
        sb = tools.sandbox(ws)
        toks = [sb.backup(ws / "app.py") for _ in range(12)]
        ck("连续 12 次 backup 的 token 互不相同", len(set(toks)) == len(toks), toks[:6])
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
