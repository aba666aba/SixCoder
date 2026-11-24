"""视觉体检（2026-11-24）—— 用浏览器 API 做**确定性**的视觉级判据。

## 为什么需要

DOM 判据能证明"元素存在"，证明不了"用户看得见、点得到"：

    DOM 说：<button>提交</button> 存在        -> 绿
    实际：按钮被遮罩盖住了，用户点不到        -> 界面是坏的

    DOM 说：<h1>标题</h1> 存在                -> 绿
    实际：白底白字，什么也看不见              -> 界面是坏的

## 与"不靠截图喂模型"这条决策的关系

core/browser.py:10 写着「不靠截图喂模型 —— 同一页面两次描述可能不同，且每页都烧 token」。

那个决策反对的是"**让模型看图当判据**"（不稳定、不可复现、烧钱）。
本模块做的是另一件事：**用浏览器 API 算确定性的答案**。

     遮挡：    document.elementFromPoint(x, y) 返回谁？           -> 确定
     可见性：  getBoundingClientRect + visibility + opacity       -> 确定
     对比度：  按 WCAG 2.1 公式算 luminance 比值                   -> 确定

同一个页面跑 100 次，答案完全一样。**不烧 token，不引入不确定性。**

## 三项检查

1. **not_covered**：元素中心点被别的元素盖住
2. **visible**：元素真的在视口里、不是 0 尺寸、不是 visibility:hidden
3. **contrast**：文字与背景的对比度达到 WCAG AA（4.5:1，大字 3:1）
"""
from __future__ import annotations

from typing import Any

#: WCAG 2.1 AA 标准
CONTRAST_AA = 4.5
CONTRAST_AA_LARGE = 3.0        # 18pt+ 或 14pt+ 粗体的"大字"
LARGE_PX = 24                  # 24px 约等于 18pt


# 浏览器里跑的 JS：一次求值算完三项，不来回通信
_JS_VISUAL = r"""
(checks) => {
  const out = {covered: [], invisible: [], contrast: []};
  const parse = (c) => {
    const m = String(c||'').match(/rgba?\(([^)]+)\)/);
    if (!m) return null;
    const p = m[1].split(',').map(x => parseFloat(x.trim()));
    return {r: p[0]||0, g: p[1]||0, b: p[2]||0, a: p.length > 3 ? (p[3] ?? 1) : 1};
  };
  const lum = (c) => {
    const f = (v) => { v = v/255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); };
    return 0.2126*f(c.r) + 0.7152*f(c.g) + 0.0722*f(c.b);
  };
  const ratio = (a, b) => {
    const la = lum(a), lb = lum(b);
    return (Math.max(la, lb) + 0.05) / (Math.min(la, lb) + 0.05);
  };
  // 往上找第一个不透明的背景色（跳过 transparent）
  const bgOf = (el) => {
    let n = el;
    while (n && n !== document.documentElement) {
      const c = parse(getComputedStyle(n).backgroundColor);
      if (c && c.a > 0.05) return c;
      n = n.parentElement;
    }
    const bc = parse(getComputedStyle(document.body||document.documentElement).backgroundColor);
    return bc && bc.a > 0.05 ? bc : {r:255,g:255,b:255,a:1};
  };

  for (const sel of (checks.not_covered || [])) {
    const el = document.querySelector(sel);
    if (!el) { out.covered.push({sel, why: '元素不存在'}); continue; }
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) { out.covered.push({sel, why: '尺寸为 0'}); continue; }
    const cx = r.left + r.width/2, cy = r.top + r.height/2;
    if (cx < 0 || cy < 0 || cx > innerWidth || cy > innerHeight) {
      out.covered.push({sel, why: '中心点在视口外'}); continue;
    }
    const top = document.elementFromPoint(cx, cy);
    if (!top) { out.covered.push({sel, why: 'elementFromPoint 返回空'}); continue; }
    if (top !== el && !el.contains(top) && !top.contains(el)) {
      const t = top.tagName.toLowerCase() + (top.id ? '#'+top.id : '');
      out.covered.push({sel, why: '被 ' + t + ' 盖住'});
    }
  }

  for (const sel of (checks.visible || [])) {
    const el = document.querySelector(sel);
    if (!el) { out.invisible.push({sel, why: '元素不存在'}); continue; }
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    if (cs.visibility === 'hidden') { out.invisible.push({sel, why: 'visibility:hidden'}); continue; }
    if (parseFloat(cs.opacity) < 0.05) { out.invisible.push({sel, why: 'opacity≈0'}); continue; }
    if (cs.display === 'none') { out.invisible.push({sel, why: 'display:none'}); continue; }
    if (r.width <= 0 || r.height <= 0) { out.invisible.push({sel, why: '尺寸为 0'}); continue; }
    if (r.bottom < 0 || r.top > innerHeight || r.right < 0 || r.left > innerWidth) {
      out.invisible.push({sel, why: '不在视口内'}); continue;
    }
  }

  for (const item of (checks.contrast || [])) {
    const sel = item.sel, want = item.ratio || 4.5;
    const el = document.querySelector(sel);
    if (!el) { out.contrast.push({sel, ratio: 0, want, why: '元素不存在'}); continue; }
    const cs = getComputedStyle(el);
    const fg = parse(cs.color);
    const bg = bgOf(el);
    if (!fg) { out.contrast.push({sel, ratio: 0, want, why: '前景色解析失败'}); continue; }
    const rr = ratio(fg, bg);
    const px = parseFloat(cs.fontSize) || 16;
    const bold = (parseInt(cs.fontWeight, 10) || 400) >= 700;
    const effWant = (px >= 24 || (bold && px >= 18.66)) ? 3.0 : want;
    if (rr + 1e-6 < effWant) {
      out.contrast.push({sel, ratio: Math.round(rr*100)/100, want: effWant,
                         why: 'fg=' + cs.color + ' bg=rgb(' + bg.r + ',' + bg.g + ',' + bg.b + ')'});
    }
  }
  return out;
}
"""


def visual_check(page, *, not_covered: list | None = None,
                 visible: list | None = None,
                 contrast: list | None = None) -> dict:
    """在已打开的 page 上跑视觉体检。

    返回 {"ok": bool, "covered": [...], "invisible": [...], "contrast": [...],
          "checked": int}。**不抛异常**：查不了算失败，并写清原因。
    """
    checks: dict[str, Any] = {
        "not_covered": list(not_covered or []),
        "visible": list(visible or []),
        "contrast": [c if isinstance(c, dict) else {"sel": str(c), "ratio": CONTRAST_AA}
                     for c in (contrast or [])],
    }
    total = (len(checks["not_covered"]) + len(checks["visible"])
             + len(checks["contrast"]))
    if total == 0:
        return {"ok": True, "checked": 0, "covered": [], "invisible": [],
                "contrast": [], "note": "没有视觉检查项"}
    try:
        got = page.evaluate(_JS_VISUAL, checks)
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "checked": total, "covered": [], "invisible": [],
                "contrast": [],
                "note": "视觉体检执行失败：%s: %s" % (type(exc).__name__, exc)}
    got = got if isinstance(got, dict) else {}
    covered = list(got.get("covered") or [])
    invisible = list(got.get("invisible") or [])
    bad_contrast = list(got.get("contrast") or [])
    return {"ok": not (covered or invisible or bad_contrast),
            "checked": total, "covered": covered, "invisible": invisible,
            "contrast": bad_contrast}


def describe(result: dict) -> str:
    """把体检结果写成一句人话（进证据/失败原因）。"""
    if result.get("ok"):
        return "视觉体检通过（%d 项）" % int(result.get("checked") or 0)
    bits = []
    for row in (result.get("covered") or [])[:5]:
        bits.append("%s 被遮挡（%s）" % (row.get("sel"), row.get("why")))
    for row in (result.get("invisible") or [])[:5]:
        bits.append("%s 不可见（%s）" % (row.get("sel"), row.get("why")))
    for row in (result.get("contrast") or [])[:5]:
        bits.append("%s 对比度 %.2f < %.1f（%s）"
                    % (row.get("sel"), float(row.get("ratio") or 0),
                       float(row.get("want") or CONTRAST_AA), row.get("why")))
    if not bits:
        bits.append(str(result.get("note") or "视觉体检未通过"))
    return "；".join(bits)
