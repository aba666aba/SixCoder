"""实验台撞出来的两类判据缺陷的回归测试。

1) **共享文件的示例必须按序执行**（实验台 file-io-cli）
   契约：write_csv("t_two.csv", …) → read_csv("t_two.csv") → summarize_csv("t_two.csv")
   逐函数各跑一遍时，read 跑在 write 之前/跑到另一个空目录 → FileNotFoundError →
   3 个函数全判红（两版正确实现都被打死，0/3 收工）。
   修法：识别"多函数共用同一个文件路径"的示例 → 合成一条 seq 判据按顺序跑，
   逐函数验收跳过这些示例。

2) **自相矛盾的契约不许自动修**（实验台 contradictory）
   契约写了两条互相打架的示例（同一组参数两个期望值）。旧行为：让远端"修契约"，
   远端删掉其中一条继续跑，最后还报"验收 1/1 通过" —— 系统替甲方选了答案，还花了钱。
   修法：这类矛盾直接拒绝开跑，一次远端调用都不许发生。

跑法: python check_seq_pipeline.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import company, contract as cm, contractcheck, iterate, props  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAILS: list[str] = []

FILE_IO = """代码规模：S

1) csvkit.py：CSV 读写（会真的读写磁盘文件）。
   - write_csv(path: str, rows: list[dict]) -> int：把行写成带表头的 CSV，返回写入行数；空 rows 只写表头返回 0
   - read_csv(path: str) -> list[dict]：读回 CSV，每行是列名到字符串值的字典，保持文件顺序
   - summarize_csv(path: str) -> dict：返回 {"rows": 行数, "columns": 表头列表}

示例（必须满足）：
write_csv("t_empty.csv", []) == 0
read_csv("t_empty.csv") == []
write_csv("t_two.csv", [{"a": "1"}, {"a": "3"}]) == 2
read_csv("t_two.csv") == [{"a": "1"}, {"a": "3"}]
summarize_csv("t_two.csv") == {"rows": 2, "columns": ["a"]}
"""

GOOD_IMPL = '''"""CSV 工具。"""
import csv


def write_csv(path: str, rows: list[dict]) -> int:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return len(rows)


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


def summarize_csv(path: str) -> dict:
    rows = read_csv(path)
    cols = list(rows[0].keys()) if rows else []
    return {"rows": len(rows), "columns": cols}
'''

BAD_IMPL = GOOD_IMPL.replace("return len(rows)", "return 0")

CONTRA = """代码规模：S

1) fee.py：手续费计算。
   - calc_fee(amount: float) -> float：按 1% 收费，保留 2 位小数

示例（必须满足）：
calc_fee(100.0) == 1.0
calc_fee(100.0) == 1.5
"""


def _sandbox_temp():
    import tempfile as _tf
    root = ROOT / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("实验台缺陷回归（共享文件按序判据 / 自相矛盾契约不许自动修）")
    cs = cm.parse_requirement(FILE_IO)
    c = cs["csvkit.py"]

    print("[1] 识别共享文件：逐函数要跳过、模块级要有 seq 判据")
    skip = props.shared_artifact_exprs(cs)
    check("识别到共享文件示例", len(skip) >= 4, str(sorted(skip)))
    seqs = props.file_pipeline_checks(cs)
    check("生成了按序判据", bool(seqs), str(seqs)[:200])
    check("按序判据覆盖 3 个函数",
          any(set(s["covers"]) == {"write_csv", "read_csv", "summarize_csv"} for s in seqs),
          str([s["covers"] for s in seqs]))
    per_func = props.from_contract(c, skip)
    check("逐函数判据里不再有共文件的示例",
          not [x for x in per_func if x["expr"] in skip], str([x["expr"] for x in per_func]))

    print("[2] 真跑：正确实现必须全绿（以前必然 0/3）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "csvkit.py").write_text(GOOD_IMPL, encoding="utf-8")
        st = iterate.inspect(ws, cs)["csvkit.py"]
        check("正确实现 3/3 通过", st["verified"] == 3 and not st["fails"],
              f"verified={st['verified']} fails={st['fails'][:1]}")
        check("没有把函数算成判红", not st["hard_failed"], str(st["hard_failed"]))
        leftover = sorted(p.name for p in ws.iterdir() if p.suffix == ".csv")
        check("判据没在仓库里留下示例生成的 csv", not leftover, str(leftover))

    print("[3] 反例：写错一个返回值必须被抓到（判据不能白给）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "csvkit.py").write_text(BAD_IMPL, encoding="utf-8")
        st = iterate.inspect(ws, cs)["csvkit.py"]
        check("坏实现被判红", bool(st["hard_failed"]), str(st["verified"]))
        check("理由指向 write_csv", any("write_csv" in str(f) for f in st["fails"]) or
              "write_csv" in str(st["hard_failed"]), str(st["fails"])[:160])

    print("[4] 自相矛盾的契约：拒绝开跑、不许调远端、理由要说清")
    hard = contractcheck.check(cm.parse_requirement(CONTRA))["hard"]
    check("前置自查抓到矛盾", bool(contractcheck.unfixable(hard)), str(hard)[:160])
    with _sandbox_temp() as td:
        ws = Path(td)
        before = {p.name for p in ws.iterdir()}
        res = company.night(ws, CONTRA, "qwen2.5-coder:7b", use_remote=True)
        after = {p.name for p in ws.iterdir()}
        check("没有写任何代码文件", not (after - before - {"AGENTS.md", "DELIVERY.md",
                                                          ".sixcoder", "TRACE.md",
                                                          ".mypy_cache", "__pycache__"}),
              str(after - before))
        check("理由是『要甲方拍板』而不是瞎猜", "拍板" in res.reason or "矛盾" in res.reason,
              res.reason[:160])
        check("没有验收通过", res.verified == 0, str(res.verified))
        check("交付报告照常生成（早上不能什么都没有）",
              (ws / "DELIVERY.md").exists())

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}（共享文件按序 / 矛盾拒绝 / 反例必红 / 不留垃圾）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
