"""#28 界面构建产物的机械验证（替代"人工点一下"）。

诚实说明：这不是浏览器点击测试，而是**对构建产物的确定性检查** ——
能确认"该消失的消失了、该出现的出现了、帮助里该写的写了"，
不能确认"点下去真的有反应"（那需要真浏览器/Playwright）。

检查项：
  1) 已删除的文案不得出现在 dist 里：原型 demo / 判定权分离 / 目标文件（输入框）/ 
     甲方模式 = 夜班 / 只读（不改任何文件）/ 怎么写需求
  2) 必须存在的：规模下拉（S/M/L/XL 四档标签）、远端模型下拉、帮助里的规模说明表、
     「远端仓库地址」、帮助入口
  3) 帮助里必须写明每档"管什么/不管什么"（含"只挡基本错误"那句）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # prototype/
DIST = ROOT / "web" / "dist" / "assets"
FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def bundle_text() -> str:
    if not DIST.exists():
        return ""
    parts = []
    for f in sorted(DIST.glob("*.js")) + sorted(DIST.glob("*.css")):
        parts.append(f.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def main() -> int:
    print("界面构建产物自检")
    txt = bundle_text()
    if not txt:
        print("  FAIL 没找到 web/dist/assets（先 npm run build）")
        return 1

    print("[1] 已按你要求删掉的文案不得残留")
    gone = {
        "原型 demo": "原型 demo",
        "判定权分离": "判定权分离",
        "目标文件输入框占位": "目标文件（可留空）",
        "甲方模式 = 夜班 标签": "甲方模式 = 夜班",
        "只读（不改任何文件）": "只读（不改任何文件）",
        "怎么写需求 按钮": "怎么写需求",
    }
    for label, needle in gone.items():
        check(f"{label} 已移除", needle not in txt, f"还残留：{needle}")

    print("[2] 该有的必须在")
    must = {
        "规模下拉四档": ["S 级", "M 级", "L 级", "XL 级"],
        "远端模型下拉": ["远端模型"],
        "帮助入口": ["帮助"],
        "远端仓库地址": ["远端仓库地址"],
    }
    for label, needles in must.items():
        check(f"{label} 存在", all(n in txt for n in needles),
              f"缺：{[n for n in needles if n not in txt]}")

    print("[3] 帮助里写清每档管什么/不管什么")
    hints = {
        "S 级说明": "只挡基本错误",
        "M 级说明": "架构依赖方向测试",
        "L 级说明": "变更行变异",
        "XL 级说明": "持续架构健康度",
        "目标文件在哪写": "在需求里写清文件名",
        "规模影响阈值": "12/4/60",
    }
    for label, needle in hints.items():
        check(f"{label} 在帮助里", needle in txt, f"缺：{needle}")

    print("[4] 关键交互状态存在（不是死界面）")
    for label, needle in {"夜班按钮": "开始夜班", "预算输入": "预算", "刷新进度": "刷新进度"}.items():
        check(f"{label} 存在", needle in txt, f"缺：{needle}")
    check("规模下拉绑定状态变量（有 v-model 的编译痕迹不现实，改查字符串常量）",
          "scaleTiers" in txt or "S 级 · < 10 万行" in txt, "找不到规模档位定义")

    print("[5] 构建产物清单（记录用）")
    files = sorted(DIST.glob("*"))
    for f in files:
        print(f"     {f.name}  {f.stat().st_size // 1024} KB")
    meta = ROOT / "web" / "dist" / "index.html"
    if meta.exists():
        html = meta.read_text(encoding="utf-8", errors="replace")
        check("index.html 引用了最新 bundle", any(f.name in html for f in files), "")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        print("说明：本检查只验证构建产物内容，不能替代真实浏览器点击测试。")
        return 1
    print("全部通过（删掉的文案没残留 / 该有的控件都在 / 帮助写清了分级说明）")
    print("说明：这是对构建产物的机械检查，不是浏览器点击测试。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
