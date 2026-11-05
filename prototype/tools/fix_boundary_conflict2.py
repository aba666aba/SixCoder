"""收口修 B（按"能判就判、不能判就如实说"的纪律）：

① `props.from_contract` 回到"只在能导出处生成 raises"：
   我上一步给 float 参数造 `m.div(0)` 是**错的** —— div 有两个参数，
   `m.div(0)` 会抛 TypeError 而不是契约要的 ZeroDivisionError，判据本身是坏判据。

② 冲突的真解在边界矩阵这一侧：
   只要函数**声明了会抛异常**，它的数值参数取 0 时就不能再要求"不许崩"。
   `iterate.inspect` 把这些 0 组合从崩溃判据里剔除（判据数量不减，只是不误判）。

③ 契约没给可判示例时，如实提示要补示例，而不是拿坏判据冒充判过。
"""
from pathlib import Path

# ---------- ① 撤回错误的 float 探针 ----------
p = Path("core/props.py")
s = p.read_text(encoding="utf-8")
old = '''            if not probes:
                # 以前只在 int/object/str 时造探针，**float 参数一个都不造** ——
                # 于是 div(a: float, b: float) 契约声明的 ZeroDivisionError 从来没被判过，
                # 而边界矩阵却要求 div(1, 0) "不许崩"：一边不判、一边误判，正确实现必红。
                base = ["-1", "0"] if f.params else []
                probes = [x for x in (_probe_literal(b, ptype) for b in base) if x]'''
new = '''            if not probes:
                # 只在"单参数函数"上造默认探针：多参数函数（如 div(a, b)）里
                # `m.div(0)` 会变成缺参 TypeError，反而生成一条**坏判据**。
                # 多参数的情况下契约必须自己给可判示例，否则宁可不判（见 inspect 的提示）。
                base = ["-1", "0"] if (len(f.params) == 1 and
                                       f.params[0].type in ("int", "object", "str")) else []
                probes = [x for x in (_probe_literal(b, ptype) for b in base) if x]'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("props.py：撤回 float 单参探针")
else:
    print("props.py：锚点没找到（可能已复原）")

# ---------- ② inspect：声明了异常的函数，数值参数取 0 不算"崩溃" ----------
p = Path("core/iterate.py")
s = p.read_text(encoding="utf-8")
old = '''            _must_raise: dict[str, set[str]] = {}
            for _f in c.funcs:
                if not _f.raises:
                    continue
                _ptype = (_f.params[0].type if _f.params else "")
                _probes = props._invalid_probes(_f.name, getattr(c, "raw", "") or "", _ptype) or []
                if _probes:
                    _must_raise[_f.name] = {str(x).strip() for x in _probes}'''
new = '''            _must_raise: dict[str, set[str]] = {}
            for _f in c.funcs:
                if not _f.raises:
                    continue
                # 声明了会抛异常的函数：数值参数取 0 时**不许**再要求"不崩"。
                # 否则 div(1, 0) 既要抛 ZeroDivisionError、又被要求不崩 —— 正确实现必红
                # （计算器那次 6 轮同一状态就是这么来的）。
                if any(str(getattr(p, "type", "")).lower().endswith(
                        ("int", "float", "object")) for p in (_f.params or [])):
                    _must_raise[_f.name] = {"0", "0.0", "0.00"}
                _ptype = (_f.params[0].type if _f.params else "")
                _probes = props._invalid_probes(_f.name, getattr(c, "raw", "") or "", _ptype) or []
                if _probes:
                    _must_raise.setdefault(_f.name, set())
                    _must_raise[_f.name] |= {str(x).strip() for x in _probes}'''
if old in s:
    s = s.replace(old, new, 1)
    print("iterate.py：改为按'声明了异常'排除 0 组合")
else:
    print("iterate.py：锚点没找到")

# ---------- ③ 契约缺可判示例时如实提示 ----------
old2 = '''            sub = sub + [x for x in boundary if str(x.get("expr", "")).startswith(f"m.{f.name}(")]'''
new2 = '''            sub = sub + [x for x in boundary if str(x.get("expr", "")).startswith(f"m.{f.name}(")]
            # 契约声明了会抛异常，但没有任何 raises 判据（多参数函数造不出安全探针）→ 如实提示
            if f.raises and not any(x.get("kind") == "raises" and
                                    str(x.get("expr", "")).startswith(f"m.{f.name}(")
                                    for x in sub):
                _note = (f"{f.name} 声明会抛 {'/'.join(f.raises)}，但契约没给出可判输入；"
                         f"建议补一条示例（例：{f.name}(1, 0) 应当抛 {f.raises[0]}）")
                if _note not in fails:
                    fails.append(_note)'''
if old2 in s:
    s = s.replace(old2, new2, 1)
    print("iterate.py：补'契约缺可判示例'提示")
else:
    print("iterate.py：sub 锚点没找到")

p.write_text(s, encoding="utf-8")
