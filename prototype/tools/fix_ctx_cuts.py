"""把"进模型上下文的字符截断"改成 token 预算（剩下的待裁决里最该改的一类）。

原则（和之前 patch.py[:12000]、author 的 [:4000] 同一批）：
    **进入模型输入的文本按 token 预算裁，不按字符数拍脑袋**；
    事件/报告里的展示字段可以按字符裁（那只影响可读性）。
"""
from pathlib import Path

EDITS = [
    # (文件, 旧, 新, 说明)
    ("core/contextpack.py",
     'sec_full.append(f"### {m[\'path\']}::{name}\\n```python\\n{code.strip()[:6000]}\\n```")',
     'sec_full.append(f"### {m[\'path\']}::{name}\\n```python\\n{_tokens.cut(code.strip(), 6000)}\\n```")',
     "焦点源码进上下文：按 token"),
    ("core/contextpack.py",
     'parts += ["# 现有测试状态", tests[:3000], ""]',
     'parts += ["# 现有测试状态", _tokens.cut(tests, 2500), ""]',
     "测试状态进上下文：按 token"),
    ("core/contract.py",
     'lines.append(f" 规范：{body[:400]}")',
     'lines.append(f" 规范：{_cm_tokens.cut(body, 500)}")',
     "契约规范进 prompt：按 token"),
    ("core/dictfill.py",
     'contract=(contract or "")[:900], skeleton=sk,',
     'contract=_tokens.cut(contract or "", 1200), skeleton=sk,',
     "契约进 prompt：按 token"),
    ("core/dictfill.py",
     'examples=(examples or "（契约没给样例，仍按类型口径写）")[:600])',
     'examples=_tokens.cut(examples or "（契约没给样例，仍按类型口径写）", 800))',
     "示例进 prompt：按 token"),
    ("core/iterate.py",
     'lines.append((requirement or "")[:2000])',
     'lines.append(_tokens_mod.cut(requirement or "", 2500))',
     "需求进 prompt：按 token"),
    ("core/iterate.py",
     'lines.append(plan.strip()[:2000])',
     'lines.append(_tokens_mod.cut(plan.strip(), 2500))',
     "实计方案进 prompt：按 token"),
    ("core/handoff.py",
     '{str(last_fail)[:1200]}',
     '{_tokens_h.cut(str(last_fail), 1500)}',
     "接力包的失败证据：按 token"),
]

# 需要补 import 的文件
IMPORTS = {
    "core/contextpack.py": ("_tokens", "from . import tokens as _tokens"),
    "core/contract.py": ("_cm_tokens", "from . import tokens as _cm_tokens"),
    "core/dictfill.py": ("_tokens", "from . import tokens as _tokens"),
    "core/iterate.py": ("_tokens_mod", "from . import tokens as _tokens_mod"),
    "core/handoff.py": ("_tokens_h", "from . import tokens as _tokens_h"),
}

changed = 0
for rel, old, new, why in EDITS:
    p = Path(rel)
    if not p.exists():
        print(f"跳过（不存在）：{rel}")
        continue
    s = p.read_text(encoding="utf-8")
    if old not in s:
        print(f"跳过（已改或不匹配）：{rel} — {why}")
        continue
    s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")
    changed += 1
    print(f"改：{rel} — {why}")

for rel, (name, imp) in IMPORTS.items():
    p = Path(rel)
    if not p.exists():
        continue
    s = p.read_text(encoding="utf-8")
    if f"def " not in s or f"as {name}" in s:
        continue
    # 插到 __future__ 之后；没有就在第一个 import 之前
    if "from __future__ import annotations" in s:
        s = s.replace("from __future__ import annotations",
                      f"from __future__ import annotations\n\n{imp}", 1)
    else:
        idx = s.find("\nimport ")
        s = s[:idx] + f"\n{imp}" + s[idx:] if idx > 0 else f"{imp}\n" + s
    p.write_text(s, encoding="utf-8")
    print(f"补 import：{rel}")

print(f"\n共改 {changed} 处")
