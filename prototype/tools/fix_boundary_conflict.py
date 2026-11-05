"""修 B：边界判据不许和契约打架。

契约说 `div(a, b)` 在 `b == 0` 时要抛 `ZeroDivisionError`；
而边界矩阵把 `div(1, 0)` 当成"边界输入不许崩"来判 —— 两条判据互斥，
**正确实现也会被判红**，于是整夜原地打转（计算器那次 6 轮同一状态）。

改法：
  1. `props.boundary_checks(..., must_raise=None)`：传入 `{函数名: {必须抛异常的实参字面量}}`，
     命中这些字面量的组合**不再进"不许崩"判据**；
  2. `iterate.inspect` 从契约（`f.raises` + `props._invalid_probes`）算出这个映射并传进去；
  3. 那些输入本身仍会被 `from_contract` 的 `raises` 判据覆盖 —— 从"判崩溃"变成"判抛对异常"，
     判据不是变少，而是变准。
"""
from pathlib import Path

# ---------- 1) boundary_checks 支持 must_raise ----------
p = Path("core/props.py")
s = p.read_text(encoding="utf-8")
n = 0

old = """def boundary_checks(source: str, exclude: set[str] | None = None,
                    hard: bool | None = None) -> list[dict]:"""
new = """def boundary_checks(source: str, exclude: set[str] | None = None,
                    hard: bool | None = None,
                    must_raise: dict[str, set[str]] | None = None) -> list[dict]:"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1

old = """        seen_val: set[str] = set()
        for combo, why in probes:"""
new = """        seen_val: set[str] = set()
        # 契约声明"这些实参必须抛异常"→ 它们不能再用"不许崩"来判。
        # 否则 div(1, 0) 既被要求抛 ZeroDivisionError、又被要求不崩，正确实现也红。
        forbid = {x.strip() for x in ((must_raise or {}).get(node.name) or set())}
        for combo, why in probes:
            if any(str(a).strip() in forbid for a in combo):
                continue"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1
p.write_text(s, encoding="utf-8")
print("props.py 改动", n)

# ---------- 2) inspect 算 must_raise 并传下去 ----------
p = Path("core/iterate.py")
s = p.read_text(encoding="utf-8")
n = 0
old = """        try:
            boundary = props.boundary_checks(src, exclude=stubs)
        except Exception:                            # noqa: BLE001
            boundary = []"""
new = """        try:
            # 契约声明了会抛异常的输入，要从"不许崩"判据里剔除（改由 raises 判据覆盖）。
            # 不这么做就是两条判据互斥：div(1,0) 既要抛 ZeroDivisionError 又不许崩。
            _must_raise: dict[str, set[str]] = {}
            for _f in c.funcs:
                if not _f.raises:
                    continue
                _ptype = (_f.params[0].type if _f.params else "")
                _probes = props._invalid_probes(_f.name, getattr(c, "raw", "") or "", _ptype) or []
                if _probes:
                    _must_raise[_f.name] = {str(x).strip() for x in _probes}
            boundary = props.boundary_checks(src, exclude=stubs, must_raise=_must_raise)
        except Exception:                            # noqa: BLE001
            boundary = []"""
if old in s:
    s = s.replace(old, new, 1)
    n += 1
p.write_text(s, encoding="utf-8")
print("iterate.py 改动", n)
