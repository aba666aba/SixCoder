"""剩下三件事一起修：
  #3 隔离层降级不能再静默（要进交付报告）
  #4 腐烂分覆盖"函数级重写"（不只是行级补丁）
  再把 workorder.py 里进提示词的 plan[:1200] 改成 token 预算（刚裁决时发现的）
"""
from pathlib import Path

# ---------- workorder：进提示词的计划改 token ----------
p = Path("core/workorder.py")
s = p.read_text(encoding="utf-8")
if "return _tokens.cut(plan.strip(), 1500)" not in s:
    s = s.replace('    return plan.strip()[:1200]',
                  '    return _tokens.cut(plan.strip(), 1500)')
    if "from . import tokens as _tokens" not in s:
        s = s.replace("from __future__ import annotations",
                      "from __future__ import annotations\n\nfrom . import tokens as _tokens", 1)
    p.write_text(s, encoding="utf-8")
    print("workorder：计划文本改成 token 预算")

# ---------- #3 + #4：company ----------
p = Path("core/company.py")
s = p.read_text(encoding="utf-8")
n = 0

# ① 隔离层降级记进 stats（不再只发一个 notice）
old = """        except Exception as exc:                     # noqa: BLE001
            sand = None
            if emit:
                emit("notice", {"message": f"隔离层开启失败（继续但用旧回滚）：{type(exc).__name__}"})"""
new = """        except Exception as exc:                     # noqa: BLE001
            sand = None
            # 降级**必须留痕**：既要发事件，也要进交付报告（不许静默降级）
            stats.setdefault("isolate_degraded", []).append(
                {"path": rel, "reason": f"{type(exc).__name__}: {exc}"[:120]})
            if emit:
                emit("notice", {"message": f"隔离层开启失败（继续但用旧回滚）：{type(exc).__name__}"})"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

# ② 腐烂分也覆盖"函数级重写"
old = """                    if rebuild and d.action in (_decision.ACT_PATCH_LINE,):"""
new = """                    # 腐烂分覆盖：行级补丁 **和** 函数级重写都算"打补丁"
                    if rebuild and d.action in (_decision.ACT_PATCH_LINE,
                                                _decision.ACT_REWRITE_FUNC):"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

# ③ 降级信息跨轮汇总并进 scope
old = '                                     "rot": rot_map, "doctor": doctor_res,'
new = ('                                     "rot": rot_map, "doctor": doctor_res,\n'
       '                                     "isolate_degraded": iso_degraded,')
if old in s:
    s = s.replace(old, new, 1)
    n += 1

# ④ 累加器：放在循环前
old = """    osc_tracker = _osc.Tracker()"""
new = """    osc_tracker = _osc.Tracker()
    iso_degraded: list[dict] = []          # 隔离层降级记录（进交付报告）"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

# ⑤ 每轮把 stats 里的降级记录并进累加器
old = """                if stats.get("system_issues"):"""
new = """                if stats.get("isolate_degraded"):
                    iso_degraded.extend(stats["isolate_degraded"])
                if stats.get("system_issues"):"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

# ⑥ 交付报告：降级小节
old = """    try:
        from . import doctor as _doctor2"""
new = """    _deg = (scope or {}).get("isolate_degraded") or []
    if _deg:
        lines += ["", "## 运行期降级（不许静默，逐条列在这里）", ""]
        for d in _deg[:10]:
            lines.append(f"- `{d.get('path')}`：隔离层没能开启（{d.get('reason')}）"
                         f" → 这一轮用旧的单文件回滚，**不是**整层隔离")
        lines.append("")
        lines.append("> 影响：失败时只能还原那一个模块，顺带改到的其它文件可能残留。"
                     "L/XL 级要求整层隔离，出现降级应当人工看一次。")
    try:
        from . import doctor as _doctor2"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

p.write_text(s, encoding="utf-8")
print("company.py 改动", n)
