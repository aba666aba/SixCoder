"""修一个真 bug（冒烟+探针抓到的）：契约函数整段消失时，夜班误判"已完成"。

现象（probe_missing_funcs.py 复现）：
    模块里只剩 `def placeholder()`，契约要求的 8 个函数一个都没有。
    inspect 报 funcs=4 / verified=0 / hard_failed=[] / stubs=[] / untested=[]
    → 夜班的 needs_work() 认为"没事要干" → 第 2 轮收工，
      收工理由却是"契约内函数已全部通过机械验收（没有可执行的工作单）"，与 0/8 自相矛盾。

    **这是循环层的假绿**：函数被删/改名 ≠ 已通过。危险在于交付报告会自相矛盾，
    而且永远不会再去补那些函数。

修法（两处）：
    ① iterate.inspect 增加 `missing`（契约函数在文件里不存在）并写进 fails；
    ② company.needs_work 把 missing 也算"要干活"，收工理由改成按真实数据说话。
"""
from pathlib import Path

# ---------- ① inspect：统计 missing ----------
p = Path("core/iterate.py")
s = p.read_text(encoding="utf-8")
n = 0
old = """        res = {"passed": passed_n, "total": total_n, "ok": not fails, "fails": fails}
        state[path] = {"exists": p.exists(), "symbols": have, "stubs": stubs,"""
new = """        # 契约函数"整段消失"（被删/改名）必须单独记账：
        # 它既不是桩、也不是判红，旧逻辑于是什么都没记 → 夜班以为没事可做（假绿）。
        missing = [f.name for f in c.funcs if f.name not in have]
        if missing:
            fails.append(f"契约函数缺失：{', '.join(missing)}（文件里根本没有这些函数）")
        res = {"passed": passed_n, "total": total_n, "ok": not fails, "fails": fails}
        state[path] = {"exists": p.exists(), "symbols": have, "stubs": stubs,
                       "missing": missing,"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1
p.write_text(s, encoding="utf-8")
print("iterate.py 改动", n)

# ---------- ② needs_work + 收工理由 ----------
p = Path("core/company.py")
s = p.read_text(encoding="utf-8")
n = 0
old = """    def needs_work(path: str) -> bool:
        st = state.get(path) or {}
        return bool(st.get("hard_failed")) or bool(st.get("untested")) \\
            or bool(st.get("stubs")) or not st.get("exists")"""
new = """    def needs_work(path: str) -> bool:
        st = state.get(path) or {}
        return bool(st.get("hard_failed")) or bool(st.get("untested")) \\
            or bool(st.get("stubs")) or not st.get("exists") \\
            or bool(st.get("missing")) or int(st.get("verified") or 0) < int(st.get("funcs") or 0)"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

old = """                if not stats["modules"]:
                    reason = "契约内函数已全部通过机械验收（没有可执行的工作单）\""""
new = """                if not stats["modules"]:
                    _v = sum(int((state.get(p) or {}).get("verified") or 0) for p in contracts)
                    _t = sum(int((state.get(p) or {}).get("funcs") or 0) for p in contracts)
                    if _v >= _t and _t:
                        reason = f"契约内函数已全部通过机械验收（{_v}/{_t}，没有可执行的工作单）"
                    else:
                        _why = []
                        for p2, s2 in state.items():
                            if s2.get("missing"):
                                _why.append(f"{p2} 缺 {', '.join(s2['missing'][:3])}")
                            elif s2.get("stubs"):
                                _why.append(f"{p2} 仍是桩 {', '.join(sorted(s2['stubs'])[:3])}")
                        reason = (f"没有可执行的工作单，但**验收只有 {_v}/{_t}**："
                                  + ("；".join(_why[:3]) or "详见交付报告"))"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1
p.write_text(s, encoding="utf-8")
print("company.py 改动", n)
