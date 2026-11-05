"""把"这个模块还要不要干活"的判定提成模块级函数，便于自检与探针直接调用。

原来它是 _author_round 里的闭包，导致：
  · 探针只能自己抄一份逻辑（于是修了实现、探针还是旧结论）；
  · 也没法在自检里钉住"函数整段消失 → 仍要干活"这条。
"""
from pathlib import Path

p = Path("core/company.py")
s = p.read_text(encoding="utf-8")
n = 0

helper = '''

def needs_work_state(state: dict, path: str) -> bool:
    """这个模块还需不需要干活（唯一判定点，自检与探针都调它）。

    判定为"要干活"的情形：
      · 有判红函数 / 没判据函数 / 仍是桩
      · 文件不存在
      · **契约函数整段缺失**（被删或改名）—— 这一条以前漏了，
        导致"函数全没了"被当成"已完成"，收工理由还写着"全部通过验收"。
      · 验收数 < 契约函数数（兜底：只要不满分就还有活）
    """
    st = (state or {}).get(path) or {}
    return (bool(st.get("hard_failed")) or bool(st.get("untested"))
            or bool(st.get("stubs")) or not st.get("exists")
            or bool(st.get("missing"))
            or int(st.get("verified") or 0) < int(st.get("funcs") or 0))

'''
if "def needs_work_state" not in s:
    idx = s.find("def _author_round(")
    if idx > 0:
        s = s[:idx] + helper.strip() + "\n\n\n" + s[idx:]
        n += 1

# 内层闭包委托给模块级函数（保留名字，避免改动调用点）
old = """    def needs_work(path: str) -> bool:
        st = state.get(path) or {}
        return bool(st.get("hard_failed")) or bool(st.get("untested")) \\
            or bool(st.get("stubs")) or not st.get("exists") \\
            or bool(st.get("missing")) or int(st.get("verified") or 0) < int(st.get("funcs") or 0)"""
new = """    def needs_work(path: str) -> bool:
        return needs_work_state(state, path)"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

p.write_text(s, encoding="utf-8")
print("company.py 改动", n)

# 探针改成调用真实现
pp = Path("tools/probe_missing_funcs.py")
s2 = pp.read_text(encoding="utf-8")
old2 = """    def needs_work(path: str) -> bool:
        st = state.get(path) or {}
        return bool(st.get("hard_failed")) or bool(st.get("untested")) \\
            or bool(st.get("stubs")) or not st.get("exists")

    pending = [p for p in contracts if needs_work(p)]"""
new2 = """    from core import company
    pending = [p for p in contracts if company.needs_work_state(state, p)]"""
if old2 in s2:
    s2 = s2.replace(old2, new2, 1)
    pp.write_text(s2, encoding="utf-8")
    print("探针已改成调用真实现")
