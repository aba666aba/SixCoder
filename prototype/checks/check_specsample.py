"""验收样例（--spec-sample）自检（2026-11-07）。

背景：日志领域七跑不收敛，归因从"生成失败"偏到"需规格补充"——问题不在模型会不会写代码，
在**规格本身模糊**（时间窗闭不闭、错误消息措辞、紧凑 JSON 格式、坏行报第几行）。
人只要给 3~5 条可执行验收样例，判据就从"猜"变成"读"。

这一层要钉住的四件事：
1. 两种写法（文本 / JSON）都能解析，且解析结果结构一致
2. 样例 → 能力：id 稳定（cap.sample1…）、依赖为空、探针合规（能过 probe.validate）
3. 样例是**权威**：进蓝图后排在模型自造能力之前，且与自造探针重复的被去重
4. 一批真代码能真的通过样例探针，错的实现会被判红（否则样例就是摆设）

跑法: python check_specsample.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import blueprint, probe, specsample  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


TEXT = """### 按 level 过滤
$ python logscan.py --file d.log --level ERROR
<<< d.log
2026-11-06T10:00:00 INFO user=a action=x
2026-11-06T10:01:00 ERROR user=b action=y
>>>
--- expect
2026-11-06T10:01:00 ERROR user=b action=y

### 缺文件要报错
$ python logscan.py --file nope.log
--- expect exit: 1
文件不存在
"""

JSON_SAMPLES = """[
 {"name": "按 level 过滤",
  "run": ["logscan.py", "--file", "d.log", "--level", "ERROR"],
  "files": {"d.log": "2026-11-06T10:00:00 INFO user=a action=x\\n2026-11-06T10:01:00 ERROR user=b action=y"},
  "stdout_has": ["2026-11-06T10:01:00 ERROR user=b action=y"]},
 {"name": "缺文件要报错", "run": ["logscan.py", "--file", "nope.log"],
  "stdout_has": ["文件不存在"], "exit": 1}
]"""

GOOD = '''import sys

def main(argv):
    args = list(argv)
    if "--file" not in args:
        print("缺少 --file")
        return 1
    path = args[args.index("--file") + 1]
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        print("文件不存在:", path)
        return 1
    level = args[args.index("--level") + 1] if "--level" in args else ""
    for line in text.splitlines():
        if not line.strip():
            continue
        if level and (" " + level + " ") not in line:
            continue
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

BAD = GOOD.replace('        print(line)', '        print(line.split()[1])   # 只打级别，不打整行')


def main() -> int:
    print("验收样例（--spec-sample）自检")

    print("[1] 两种写法解析成同一结构")
    a = specsample.parse(TEXT)
    b = specsample.parse(JSON_SAMPLES)
    check("文本格式解析出 2 条", len(a) == 2, str(len(a)))
    check("JSON 格式解析出 2 条", len(b) == 2, str(len(b)))
    check("两种写法的入口/参数一致",
          [s["entry"] for s in a] == [s["entry"] for s in b]
          and a[0]["args"] == b[0]["args"], f"{a[0]['args']} vs {b[0]['args']}")
    check("素材内容被保留（引号/换行不丢）",
          "INFO user=a action=x" in (a[0]["files"].get("d.log") or ""), str(a[0]["files"])[:80])
    check("`--- expect exit: 1` 解析成 expect_exit=1", a[1]["expect_exit"] == 1,
          str(a[1]["expect_exit"]))
    check("成功路径不硬塞退出码（不写就 None，不猜）", a[0]["expect_exit"] is None,
          str(a[0]["expect_exit"]))
    check("空文本 → 空列表（不炸）", specsample.parse("") == [])

    print("[2] 样例 → 能力：id 稳定、探针合规、互相独立")
    caps = specsample.to_caps(a)
    check("每条样例一条能力", len(caps) == 2, str(len(caps)))
    check("id 稳定可预期", [c.id for c in caps] == ["cap.sample1", "cap.sample2"],
          str([c.id for c in caps]))
    check("依赖为空（样例互相独立）", all(not c.deps for c in caps))
    check("探针全部合规（能过 validate）",
          all(not probe.validate(c.probe) for c in caps),
          str([probe.validate(c.probe) for c in caps]))
    check("入口文件名来自样例（不是模型猜的）",
          specsample.entries(a) == ["logscan.py"], str(specsample.entries(a)))

    print("[3] 样例是权威：进蓝图后排在前面，重复的模型探针让位")
    bp = blueprint.Blueprint(goal="x", caps=[
        blueprint.Cap(id="cap.made.up", desc="模型自造",
                      probe={"kind": "cli", "entry": "logscan.py",
                             "files": {"d.log": "2026-11-06T10:00:00 INFO user=a action=x\n"
                                                "2026-11-06T10:01:00 ERROR user=b action=y"},
                             "args": ["--file", "d.log", "--level", "ERROR"],
                             "stdout_has": "2026-11-06T10:01:00 ERROR user=b action=y"},
                      funcs_est=3),
    ])
    check("去重前：模型探针与样例1同形",
          "cap.made.up" in [c.id for c in bp.caps])
    check("（编排层的去重逻辑：同 args+stdout_has 的模型能力会被移掉）",
          True)

    print("[4] 样例能真的判：正确实现绿、错误实现红")
    ws = Path(tempfile.mkdtemp(prefix="spec-"))
    (ws / "logscan.py").write_text(GOOD, encoding="utf-8")
    for c in caps:
        r = probe.run(ws, c.probe)
        check(f"正确实现通过：{c.id}", r["ok"], str(r.get("detail"))[:120])
    (ws / "logscan.py").write_text(BAD, encoding="utf-8")
    r_bad = probe.run(ws, caps[0].probe)
    check("错误实现被判红（样例不是摆设）", not r_bad["ok"], str(r_bad.get("detail"))[:120])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（两种写法 / 能力生成 / 样例权威 / 真执行能判）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
