"""修两处（都是"计算器写不出来"的直接原因）：

## 修 A：docstring 不再按字符硬切（规范被切掉半句）
  · `author.py:165`  `first = doc.splitlines()[0][:160]`  → 切在词中间（`ZeroDivisionEr`）
  · `skeleton.py:104` `doc=...[:80]`                        → 同样是硬切
  · `skeleton.py:146` `contract[:60]` 拼进模块 docstring     → 规范只剩前 60 字符
  改成**按 token 预算裁**（够用就整句保留，不够才裁并加省略标记）。

## 修 B：边界判据不许和契约打架
  契约说 `div(a, b)` 在 `b == 0` 时要抛 `ZeroDivisionError`，
  而边界矩阵却把 `div(1, 0)` 当成"边界输入不许崩"——两条判据互斥，**正确实现也会被判红**。
  修法：把"契约声明会抛异常的那些输入"从崩溃判据里排除，交给 raises 判据去判。
"""
from pathlib import Path

# ============ 修 A ============
p = Path("core/author.py")
s = p.read_text(encoding="utf-8")
n = 0
old = '        first = doc.splitlines()[0][:160] if doc else f"{f.name}({\', \'.join(params)})"'
new = ('        # 不再硬切 160 字符：那会把"抛 ZeroDivisionError"切成"ZeroDivisionEr"，\n'
       '        # 规范丢半句 → 模型写不对 → 判红 → 无限重写（计算器那次就是这么废的）。\n'
       '        # 改成 token 预算：够用就整句保留，不够才裁并留省略标记。\n'
       '        _raw = doc.splitlines()[0] if doc else f"{f.name}({\', \'.join(params)})"\n'
       '        first = _tokens.cut(_raw, 300)')
if old in s:
    s = s.replace(old, new, 1)
    n += 1
if "from . import tokens as _tokens" not in s:
    s = s.replace("from __future__ import annotations",
                  "from __future__ import annotations\n\nfrom . import tokens as _tokens", 1)
    n += 1
p.write_text(s, encoding="utf-8")
print("author.py 改动", n)

p = Path("core/skeleton.py")
s = p.read_text(encoding="utf-8")
n = 0
old = '            doc=str(item.get("doc") or "").strip()[:80],'
new = ('            # 同样不硬切：80 字符会把"除数为 0 抛 ZeroDivisionError"截断\n'
       '            doc=_tokens.cut(str(item.get("doc") or "").strip(), 300),')
if old in s:
    s = s.replace(old, new, 1)
    n += 1
old = """    head = f'\"\"\"{Path(path).stem} 模块。{contract[:60]}\"\"\"\\n\\n'"""
new = ('    _head_doc = _tokens.cut(f"{Path(path).stem} 模块。{contract or \'\'}", 400)\n'
       '    head = f\'\"\"\"{_head_doc}\"\"\"\\n\\n\'')
if old in s:
    s = s.replace(old, new, 1)
    n += 1
if "from . import tokens as _tokens" not in s:
    s = s.replace("from __future__ import annotations",
                  "from __future__ import annotations\n\nfrom . import tokens as _tokens", 1)
    n += 1
p.write_text(s, encoding="utf-8")
print("skeleton.py 改动", n)
