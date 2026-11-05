"""自审：不猜，数出来。

只统计**可机械判定**的东西，给"这套系统现在有多少问题"一个可核对的清单。
不做质量评价，不下结论——数字摆出来自己看。
"""
import io
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # SixCoder/
PROTO = ROOT / "prototype"
CORE = PROTO / "core"
CHECKS = PROTO / "checks"
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")


def pyfiles(d):
    return sorted(p for p in d.rglob("*.py") if "__pycache__" not in p.parts)


def loc(p):
    return len(p.read_text(encoding="utf-8", errors="replace").splitlines())


print("=" * 72)
print("1. 代码规模")
core = pyfiles(CORE)
print(f"core/ {len(core)} 个文件，{sum(loc(p) for p in core)} 行")
print("最大的 5 个：")
for p in sorted(core, key=loc, reverse=True)[:5]:
    print(f"  {loc(p):5d}  {p.name}")
chk = pyfiles(CHECKS)
print(f"checks/ {len(chk)} 个文件，{sum(loc(p) for p in chk)} 行")

print("=" * 72)
print("2. 已知的高风险模式（原地数）")
pats = {
    "吞异常 except Exception: pass": re.compile(
        r"except Exception[^\n]*:\s*(?:# noqa: BLE001)?\s*\n\s*pass\b"),
    "TODO/FIXME/XXX/HACK": re.compile(r"#.*\b(TODO|FIXME|XXX|HACK)\b"),
    "硬编码盘符路径": re.compile(r"['\"][A-Za-z]:[\\/]"),
    "裸 except:": re.compile(r"except\s*:"),
}
for name, pat in pats.items():
    hits = []
    for p in core + pyfiles(PROTO / "tools"):
        txt = p.read_text(encoding="utf-8", errors="replace")
        for m in pat.finditer(txt):
            ln = txt[:m.start()].count("\n") + 1
            hits.append(f"{p.relative_to(PROTO)}:{ln}")
    print(f"  {name}: {len(hits)} 处")
    for h in hits[:6]:
        print(f"      {h}")

# 「喂给模型的文本被硬切」是已经咬过两次的缺陷类（docstring 切 60/80 字→语义漂移）。
# 展示/日志里的 [:N] 无害，所以分开数。
print("  文本硬切 [:N]：区分「进提示词/交付物」与「只用于展示」")
feed_pat = re.compile(r"\[:\s*\d{2,6}\s*\]")
feed_kw = ("doc", "prompt", "contract", "requirement", "spec", "raw", "model_plan",
           "evidence", "last", "context", "brief")
feed, show = 0, 0
feed_hits = []
for p in core:
    for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        for m in feed_pat.finditer(line):
            head = line[:m.start()].lower()
            if any(k in head for k in feed_kw):
                feed += 1
                if len(feed_hits) < 8:
                    feed_hits.append(f"{p.name}:{i}  {line.strip()[:90]}")
            else:
                show += 1
print(f"      进提示词/契约/交付物：{feed} 处｜只用于展示/日志：{show} 处")
for h in feed_hits:
    print(f"      {h}")

print("=" * 72)
print("3. 检查里有多少是「真跑」的（会不会调模型/起进程）")
real, unit = [], []
for p in chk:
    if p.name in ("run_all.py", "clean_tmp_tool.py", "compare_runs_tool.py",
                  "seed_invariants_tool.py"):
        continue
    if not p.name.startswith("check_"):
        continue
    txt = p.read_text(encoding="utf-8", errors="replace")
    (real if ("ollama" in txt or "subprocess" in txt or "company.night" in txt) else unit).append(p.name)
print(f"  check_*.py 共 {len(real) + len(unit)} 个：真跑/牵涉进程 {len(real)}，纯单元 {len(unit)}")

print("=" * 72)
print("4. 标准是否真的被执行（门禁 × 档位）")
sys.path.insert(0, str(PROTO))
try:
    from core import scale as S
    gates = ("architecture", "coverage", "mutation", "dep_whitelist",
             "protected_zones", "typecheck", "lint", "deps_audit")
    print("  " + "档位  ".ljust(6) + "  ".join(g[:12].ljust(12) for g in gates))
    for lv in ("S", "M", "L", "XL"):
        row = [str(S.gate_mode(g, lv)).ljust(12) for g in gates]   # 注意参数序是 (gate, level)
        print("  " + lv.ljust(6) + "  ".join(row))
except Exception as exc:                                  # noqa: BLE001
    print(f"  （读不到 scale：{type(exc).__name__}: {exc}）")

print("=" * 72)
print("5. 台账（docs/25 分级规则 30 条）")
doc = ROOT / "docs" / "25-机制清单与实施状态.md"
if doc.exists():
    t = doc.read_text(encoding="utf-8", errors="replace")
    for ln in t.splitlines()[:6]:
        if "共 30 条" in ln:
            print("  " + ln.strip())
    print(f"  行内标记：✅ {t.count('✅')}｜⚠️ {t.count('⚠️')}｜❌ {t.count('❌')}")
    for ln in t.splitlines():
        if "❌" in ln and ln.strip().startswith("|"):
            print("  " + ln.strip()[:150])
else:
    print("  （找不到 docs/25）")

