"""视觉体检测试（2026-11-24）。

## 背景：用户提出"动态运行界面录屏后看看"

项目里有一条明确决策（core/browser.py:10）：
  「不靠截图喂模型 —— 同一页面两次描述可能不同，且每页都烧 token」

但 DOM 判据证明不了"用户看得见、点得到"：
  · 按钮被遮罩盖住 -> DOM 说存在，实际点不到
  · 白底白字        -> DOM 说存在，实际看不见

## 分两层解决

B 层（本文件测的）：**确定性视觉检查**
  · 遮挡：elementFromPoint -> 确定
  · 可见性：getBoundingClientRect + visibility + opacity -> 确定
  · 对比度：WCAG 2.1 luminance 公式 -> 确定
  同一个页面跑 100 次答案一样，不烧 token。

C 层（另见 test_visual_llm.py）：截图交给模型当**兜底**，不参与 pass/fail。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import browser, probe, visual   # noqa: E402

pytest.importorskip("playwright")


HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>视觉测试</title><style>
body { margin:0; background:#ffffff; font-family:sans-serif; }
#covered { position:absolute; left:20px; top:100px; width:120px; height:40px; }
#mask { position:absolute; left:0; top:80px; width:300px; height:80px;
        background:rgba(0,0,0,.9); z-index:10; }
#low { color:#eeeeee; background:#ffffff; font-size:16px; }
#good { color:#000000; background:#ffffff; font-size:16px; }
#hidden { visibility:hidden; }
#zero { width:0; height:0; overflow:hidden; }
</style></head><body>
<button id="ok">普通按钮</button>
<button id="covered">被盖住的按钮</button>
<div id="mask"></div>
<p id="low">低对比度文字</p>
<p id="good">高对比度文字</p>
<p id="hidden">隐藏文字</p>
<span id="zero"></span>
</body></html>"""


def _page_url() -> str:
    ws = Path(tempfile.mkdtemp(prefix="vis-"))
    p = ws / "index.html"
    p.write_text(HTML, encoding="utf-8")
    return "file:///" + str(p).replace("\\", "/")


def test_visual_spec_parses_probe_fields():
    """探针里的视觉字段要能被正确解析（字符串/列表/字典都要认）。"""
    sp = probe._visual_spec({
        "expect_not_covered": "#a",
        "expect_visible": ["#b", "#c"],
        "expect_contrast": {"sel": "#d", "ratio": 4.5}})
    assert sp["expect_not_covered"] == ["#a"]
    assert sp["expect_visible"] == ["#b", "#c"]
    assert sp["expect_contrast"] == [{"sel": "#d", "ratio": 4.5}]
    assert probe._visual_spec({}) == {}, "没字段时不能返回垃圾"


def test_visible_element_passes():
    pv = browser.visit(_page_url(), visual={"expect_visible": ["#ok"],
                                            "expect_not_covered": ["#ok"]})
    assert pv.visual.get("ok") is True, pv.visual
    assert pv.visual.get("checked") == 2


def test_covered_element_detected():
    """DOM 说按钮存在，但被遮罩盖住 -> 必须抓到。"""
    pv = browser.visit(_page_url(), visual={"expect_not_covered": ["#covered"]})
    assert pv.visual.get("ok") is False
    covered = pv.visual.get("covered") or []
    assert covered, pv.visual
    assert "mask" in str(covered[0].get("why")), covered


def test_low_contrast_detected():
    """白底白字 -> 对比度不足，必须抓到。"""
    pv = browser.visit(_page_url(),
                       visual={"expect_contrast": [{"sel": "#low", "ratio": 4.5}]})
    assert pv.visual.get("ok") is False
    rows = pv.visual.get("contrast") or []
    assert rows and float(rows[0].get("ratio") or 0) < 4.5, rows


def test_good_contrast_passes():
    pv = browser.visit(_page_url(),
                       visual={"expect_contrast": [{"sel": "#good", "ratio": 4.5}]})
    assert pv.visual.get("ok") is True, pv.visual


def test_hidden_element_detected():
    pv = browser.visit(_page_url(), visual={"expect_visible": ["#hidden"]})
    assert pv.visual.get("ok") is False
    assert "visibility" in str(pv.visual.get("invisible")), pv.visual


def test_zero_size_detected():
    pv = browser.visit(_page_url(), visual={"expect_visible": ["#zero"]})
    assert pv.visual.get("ok") is False
    assert "尺寸" in str(pv.visual.get("invisible")), pv.visual


def test_missing_element_detected():
    pv = browser.visit(_page_url(), visual={"expect_visible": ["#nope"]})
    assert pv.visual.get("ok") is False
    assert "不存在" in str(pv.visual.get("invisible")), pv.visual


def test_no_visual_spec_keeps_old_behavior():
    """不传 visual 时行为完全不变（向后兼容）。"""
    pv = browser.visit(_page_url())
    assert pv.visual == {}, "没要求就别跑视觉检查"


def test_describe_is_human_readable():
    r = visual.visual_check.__doc__ is not None
    assert r
    txt = visual.describe({"ok": True, "checked": 3})
    assert "通过" in txt
    txt2 = visual.describe({"ok": False, "covered": [{"sel": "#a", "why": "被 div 盖住"}]})
    assert "遮挡" in txt2 and "#a" in txt2
