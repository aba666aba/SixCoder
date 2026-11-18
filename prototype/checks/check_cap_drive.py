"""按能力派单（B）：契约全过但端到端还红时，不许"没有可执行的工作"收工。

事故背景（2026-11-10 20:47 实测）：
    契约 16 模块 / 35 函数 → 已验收 **35/35**；而端到端能力只有 **28/84**。
    `needs_work` 一个不剩 → `_author_round` 返回空模块 → 收工 → 看门狗重启 → 再收工：
    **纯空转**，一个 token 也不产出。
    根因：剩的 56 条能力，所在模块**大多不在契约里**（app/analytics.py、
    app/api/adjustments.py…），契约层压根没有它们的函数 —— 两套验收口径谁也不认谁。

修法：`mods` 为空但 `cap_red` 非空 → 把红模块直接派单，
**按端点写、验收只认端到端探针**（`author_module(contract=None, verify_fn=探针)`）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import company as C                                    # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


def main() -> int:
    print("[1] 候选选取：确定性、限量、空集安全")
    # 2026-11-11 **口径变更**（期望过期，不是代码错）：
    # 旧口径是**纯字典序**。现在按**文件所有权**排序：业务层（app/、app/services/、
    # matching_core/）在前，接线层（app/api/）在后 —— 接线层是引擎拥有的文件，
    # 远端写进去会被冲掉，且必然撞上「承载已通过能力的文件不许整文件重写」的护栏
    # → 死锁（实测 56 条红灯里 52 条先派到接线层，表现为「修好 0 条，仍红 4 条」）。
    # 依据见开发文档 §11.4e；断言见 checks/check_file_ownership.py。
    # **确定性不变**：同一层内仍按名字序 → 同一批不会被随机挑走。
    check("排序确定（同层内按名字序，同一批不会随机挑）",
          C._cap_drive_candidates({"app/b.py", "app/a.py", "app/c.py"}, 2)
          == ["app/a.py", "app/b.py"],
          str(C._cap_drive_candidates({"app/b.py", "app/a.py", "app/c.py"}, 2)))
    check("业务层优先于接线层（所有权口径）",
          C._cap_drive_candidates({"app/api/z.py", "app/services/a.py"}, 1)
          == ["app/services/a.py"],
          str(C._cap_drive_candidates({"app/api/z.py", "app/services/a.py"}, 1)))
    check("limit 生效", len(C._cap_drive_candidates({"a", "b", "c"}, 1)) == 1)
    check("没有红模块 → 空（不许硬造工作）", C._cap_drive_candidates(set(), 3) == [])
    check("limit<=0 → 空（不派活）", C._cap_drive_candidates({"a"}, 0) == [])

    print("[2] 源码层钉子：这条通路必须真的接上")
    src = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(
        encoding="utf-8")
    check("mods 为空且有 cap_red 时会派单", "_cap_drive_candidates(cap_red, max_modules)" in src)
    check("发出 cap_drive 事件（可观测）", 'emit("cap_drive"' in src)
    check("派单模块走专用写入路径", "if rel in _cap_only:" in src)
    check("专用路径用 contract=None（这些模块没有契约）",
          "author_module(ws, rel, None, contracts" in src)
    check("验收钩子是端到端探针", "verify_fn=_cap_probe_verdict" in src)
    check("探针验收失败要如实报红", '"端到端仍未通过："' in src)
    check("派单结果进 stats（报告里看得见）", '"by": "cap-drive"' in src)
    check("失败会回滚隔离层", "sand.rollback()" in src)
    # 顺序：先算派单批，再回退到按能力派单，最后才收工
    i_need = src.find("mods = _pick_batch(_pool, cap_round, max_modules)")
    i_cap = src.find("_cap_drive_candidates(cap_red, max_modules)")
    i_ret = src.find('return {"modules": [], "ok": True}', i_need)
    check("顺序正确：契约工作 → 按能力派单 → 才允许收工",
          0 < i_need < i_cap < i_ret, f"{i_need}/{i_cap}/{i_ret}")

    # ── 轮转取批（2026-11-10 21:55 实测：整表旋转被依赖修正还原 → 饿死 3 个模块）──
    print("[N] 轮转取批：公平 + 批内保持依赖序")
    pool = ["m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8"]
    for r, want in enumerate([["m1", "m2", "m3"], ["m4", "m5", "m6"], ["m1", "m7", "m8"]]):
        check(f"第 {r} 轮取批 = {want}", C._pick_batch(pool, r, 3) == want,
              str(C._pick_batch(pool, r, 3)))
    covered = set()
    for r in range(3):
        covered |= set(C._pick_batch(pool, r, 3))
    check("轮转 3 轮覆盖全部 8 个模块（不饿死）", covered == set(pool), str(sorted(covered)))
    check("批内仍按池序（依赖序）返回",
          C._pick_batch(["a", "b", "c", "d", "e"], 1, 2) == ["c", "d"],
          str(C._pick_batch(["a", "b", "c", "d", "e"], 1, 2)))
    check("批大小 ≥ 池大小 → 返回全池", C._pick_batch(pool, 5, 99) == pool)
    check("空池 / 零批 → 空", C._pick_batch([], 3, 3) == [] and C._pick_batch(pool, 3, 0) == [])
    _big = C._pick_batch(pool, 9999, 3)
    check("轮号很大也不越界（长度对、元素都在池里）",
          len(_big) == 3 and set(_big) <= set(pool), str(_big))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)} 项：" + "；".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
