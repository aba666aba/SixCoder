"""第三个真 bug：`_verify` 在 company.py 里根本不存在（行级补丁那条路必炸）。

`_author_round` 里给 `patch.request_line_fix` 传的 verify 回调是
    verify=lambda: _verify(ws, {...}, scope=[rel])
而 company.py 里**没有 `_verify` 这个函数**（它是旧路径删掉时留下的引用）。
于是只要决策表选了"行级补丁"并且真的打完补丁要复验，就会 NameError。

修法：用 `iterate.inspect` 直接实现这一小段复验（只验本模块，判据全过才算过）。
"""
from pathlib import Path

p = Path("core/company.py")
s = p.read_text(encoding="utf-8")
n = 0

# 1) 在 _author_round 里加一个局部复验函数（紧跟 needs_work 定义之后）
old = """    def needs_work(path: str) -> bool:
        return needs_work_state(state, path)"""
new = """    def needs_work(path: str) -> bool:
        return needs_work_state(state, path)

    def _verify_module(path: str) -> dict:
        \"\"\"行级补丁打完后的复验：只验这一个模块，判据全过才算过。

        以前这里写的是 `_verify(...)`，而那个函数在旧路径删掉时一起没了 ——
        行级补丁这条路一旦真用上就 NameError（用静态未定义名字检查抓到的）。
        \"\"\"
        try:
            st = iterate.inspect(ws, {path: contracts.get(path)})
            cur = st.get(path) or {}
            total = int(cur.get("funcs") or 0)
            verified = int(cur.get("verified") or 0)
            ok = bool(total) and verified >= total
            fb = "；".join([str(x) for x in (cur.get("fails") or [])[:2]])
            if not ok and not fb:
                fb = f"仍差 {total - verified} 个函数没过判据"
            return {"ok": ok, "feedback": fb}
        except Exception as exc:                     # noqa: BLE001
            return {"ok": False, "feedback": f"复验出错：{type(exc).__name__}: {exc}"}"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

# 2) 把不存在的 _verify 换成新函数
old2 = """                        verify=lambda: _verify(ws, {**contracts, rel: contracts.get(rel)},
                                               scope=[rel]))"""
new2 = """                        verify=lambda: _verify_module(rel))"""
if old2 in s:
    s = s.replace(old2, new2, 1)
    n += 1

p.write_text(s, encoding="utf-8")
print("company.py 改动", n)
