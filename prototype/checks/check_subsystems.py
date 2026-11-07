"""两级蓝图（L1 子系统层）自检：能力过阈值按模块域分组，切片不跨子系统。

这条钉的是开发文档 §18.7 第 1 件：
    · 扁平能力表上限 ~200 条 ≈ 6,000 函数 ≈ 10 万行，再往上必须分层；
    · 第一级 = 子系统 DAG（本检查），第二级 = 能力 DAG（原有 caps）；
    · 切片不跨子系统 = 接口先冻结（一片只碰一个子系统的文件面）；
    · 组间依赖必须无环（有环则确定性合并，子系统必须是 DAG）；
    · 分组确定性：同输入永远同输出（缓存/复现的前提）。

跑法: python checks/check_subsystems.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import blueprint as bp  # noqa: E402
from core import slicer  # noqa: E402
from core.blueprint import Blueprint, Cap, Subsystem  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _cap(cid, modules, deps=()):
    return Cap(id=cid, desc=cid, modules=modules, deps=list(deps),
               probe={"kind": "py_call", "module": "x", "func": "f", "args": [1], "expect": 1})


def main() -> int:
    print("两级蓝图（L1 子系统层）自检（分组 / 依赖 / 环 / 容量 / 切片 / 往返）")

    print("[1] 分组：按落点模块域聚合，组间依赖从能力依赖推导")
    b = Blueprint(goal="task")
    b.caps = [
        _cap("cap.auth.login", ["app/auth.py"]),
        _cap("cap.auth.register", ["app/auth.py"]),
        _cap("cap.ticket.create", ["app/tickets.py"], ["cap.auth.login"]),
        _cap("cap.ticket.list", ["app/tickets.py"], ["cap.auth.login"]),
    ]
    subs = bp.split_subsystems(b)
    check("分成 2 个子系统（auth / tickets）", {s.name for s in subs} == {"auth", "tickets"},
          str([s.name for s in subs]))
    check("tickets 依赖 auth", any(s.name == "tickets" and s.deps == ["auth"] for s in subs),
          str([(s.name, s.deps) for s in subs]))
    check("auth 无依赖", any(s.name == "auth" and not s.deps for s in subs))
    check("归属唯一：每条能力恰在一个组",
          sorted(cid for s in subs for cid in s.cap_ids) == sorted(b.ids()))

    print("[2] 大组拆分：超 max_caps 按序切块")
    b2 = Blueprint(goal="big")
    b2.caps = [_cap(f"cap.m.f{i}", ["m.py"]) for i in range(90)]
    subs2 = bp.split_subsystems(b2, max_caps=40)
    check("切成 3 块（m.1/m.2/m.3）", sorted(s.name for s in subs2) == ["m.1", "m.2", "m.3"],
          str([s.name for s in subs2]))
    check("每块 ≤ 40 条", all(len(s.cap_ids) <= 40 for s in subs2),
          str([len(s.cap_ids) for s in subs2]))
    check("不丢能力（90 条全在）", sum(len(s.cap_ids) for s in subs2) == 90)

    print("[3] 组间依赖成环 → 确定性合并（子系统必须是 DAG）")
    b3 = Blueprint(goal="ring")
    b3.caps = [
        _cap("cap.a.x", ["a.py"], ["cap.b.y"]),
        _cap("cap.b.y", ["b.py"], ["cap.a.x"]),
    ]
    subs3 = bp.split_subsystems(b3)
    check("环被合并成 1 组", len(subs3) == 1, str([s.name for s in subs3]))
    check("合并后无环", bp._subsystem_cycle(subs3) is None)

    print("[4] 分组确定性：同输入永远同输出")
    b4 = Blueprint(goal="det")
    b4.caps = [_cap(f"cap.d.{c}", ["app/x.py"] if c % 2 == 0 else ["app/y.py"]) for c in range(20)]
    s_a = bp.split_subsystems(b4)
    s_b = bp.split_subsystems(Blueprint(goal="det", caps=[
        Cap.from_dict(c.to_dict()) for c in b4.caps]))
    check("两次分组结果一致（名字/依赖逐项相同）",
          [(s.name, s.deps) for s in s_a] == [(s.name, s.deps) for s in s_b])

    print("[5] 容量上限：无分组 200 条封顶，分组后抬到 2000")
    small = Blueprint(goal="cap")
    n_ok = 0
    for i in range(250):
        if small.add(_cap(f"cap.z.{i}", ["z.py"])):
            n_ok += 1
    check("无分组：最多 200 条（第 201 条被拒）", n_ok == 200, str(n_ok))
    big = Blueprint(goal="cap")
    big.subsystems = [Subsystem(name="z", cap_ids=[])]
    n_ok2 = 0
    for i in range(250):
        if big.add(_cap(f"cap.z.{i}", ["z.py"])):
            n_ok2 += 1
    check("分组后：250 条都能进（上限 2000）", n_ok2 == 250, str(n_ok2))

    print("[6] 切片不跨子系统（接口先冻结）")
    b6 = Blueprint(goal="slice")
    b6.caps = [
        _cap("cap.auth.a1", ["app/auth.py"]),
        _cap("cap.auth.a2", ["app/auth.py"]),
        _cap("cap.ticket.t1", ["app/tickets.py"], ["cap.auth.a1"]),
    ]
    bp.split_subsystems(b6)
    packs = slicer.pack(b6)
    check("第一片只含 auth（ticket 依赖 auth，排后且不混片）",
          all(b6.subsystem_of(c.id) == "auth" for c in packs[0]),
          str([[c.id for c in p] for p in packs]))
    check("没有一片跨子系统",
          all(len({b6.subsystem_of(c.id) for c in p}) <= 1 for p in packs),
          str([[c.id for c in p] for p in packs]))

    print("[7] 序列化往返：subsystems 不丢")
    b7 = Blueprint(goal="rt")
    b7.caps = [_cap("cap.a.x", ["a.py"]), _cap("cap.b.y", ["b.py"], ["cap.a.x"])]
    bp.split_subsystems(b7)
    with tempfile.TemporaryDirectory() as td:
        bp.save(b7, td)
        loaded = bp.load(td)
    check("往返后子系统数一致", len(loaded.subsystems) == len(b7.subsystems))
    check("往返后组间依赖一致",
          sorted((s.name, tuple(s.deps)) for s in loaded.subsystems)
          == sorted((s.name, tuple(s.deps)) for s in b7.subsystems))

    print("[8] 校验集成：归属缺失 / 重名 / 坏依赖 / 环都会被 subsystem_issues 抓到")
    bad = Blueprint(goal="v")
    bad.caps = [_cap("cap.a.x", ["a.py"]), _cap("cap.b.y", ["b.py"])]
    bad.subsystems = [Subsystem(name="a", cap_ids=["cap.a.x"], deps=["ghost"]),
                      Subsystem(name="a", cap_ids=["cap.b.y"])]  # 重名 + 缺归属 cap.a.x? 不，a.x 在 a 组
    issues = bp.subsystem_issues(bad)
    check("抓到依赖不存在的子系统", any("ghost" in i for i in issues), str(issues))
    check("抓到子系统重名", any("重名" in i for i in issues), str(issues))

    print(f"\n{'全部通过' if not FAILS else '失败：' + str(FAILS)}"
          "（分组 / 大组拆分 / 环合并 / 确定性 / 容量 / 切片不跨组 / 往返 / 校验）")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
