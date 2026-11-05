"""两处收尾：
  ① author.py 的 `_tokens` 只加在了函数里，模块级没有 → render_skeleton 里 NameError；
  ② `props.from_contract` 对 **float 参数**一个 raises 探针都不生成
     （只在 int/object/str 时才造 "-1"/"0"），于是 `div(a, b)` 契约里写的
     "除数为 0 抛 ZeroDivisionError" 根本没被判过 —— 而边界矩阵又要求它"不许崩"。
     一边不判 raises、一边判崩溃，就是这次计算器写不出来的死结。
"""
from pathlib import Path

# ---------- ① author.py 模块级导入 ----------
p = Path("core/author.py")
s = p.read_text(encoding="utf-8")
if "\nfrom . import tokens as _tokens\n" not in s:
    s = s.replace("from pathlib import Path\n\nAUTHOR_PROMPT",
                  "from pathlib import Path\n\nfrom . import tokens as _tokens\n\nAUTHOR_PROMPT", 1)
    p.write_text(s, encoding="utf-8")
    print("author.py：补模块级 _tokens 导入")
else:
    print("author.py：已有序")

# ---------- ② from_contract 对 float 也造 raises 探针 ----------
p = Path("core/props.py")
s = p.read_text(encoding="utf-8")
old = '''            if not probes:
                base = ["-1", "0"] if any(p.type in ("int", "object", "str") for p in f.params) else []
                probes = [x for x in (_probe_literal(b, ptype) for b in base) if x]'''
new = '''            if not probes:
                # 以前只在 int/object/str 时造探针，**float 参数一个都不造** ——
                # 于是 div(a: float, b: float) 契约声明的 ZeroDivisionError 从来没被判过，
                # 而边界矩阵却要求 div(1, 0) "不许崩"：一边不判、一边误判，正确实现必红。
                base = ["-1", "0"] if f.params else []
                probes = [x for x in (_probe_literal(b, ptype) for b in base) if x]'''
if old in s:
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    print("props.py：float 参数也造 raises 探针")
else:
    print("props.py：锚点没找到")
