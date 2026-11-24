"""交互动作 + 录屏测试（2026-11-24）。

## 背景

用户问："sixcoder 可以模拟点击拖动等操作吗，如果有这个工具远端又能截屏、
录屏看界面还能模拟一些电脑操作来测试"

## 已有 vs 新增

  已有：click / dblclick / fill / press / select / wait / goto / reload
  新增：drag（选择器 + 坐标两种）/ hover / rightclick / scroll /
        check / uncheck / focus / upload
  新增：Recorder（动作前后截帧，事后可喂给模型看）

## 录屏为什么不用 Playwright 的 video

Playwright 的 record_video 只对**它自己启动**的浏览器有效。
我们测 Electron 是 connect_over_cdp **连进**已有进程，拿不到那个 context。
所以自己截帧。

## 为什么不用后台线程（我第一版踩的坑）

第一版开线程定时 screenshot -> 卡死。
Playwright 同步 API 绑定创建它的线程（greenlet），跨线程调用会死锁。
改成提交式：在主线程的动作前后各截一帧。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "prototype"))

from core import browser   # noqa: E402

pytest.importorskip("playwright")


PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>交互测试</title><style>
body { margin:0; background:#fff; font-family:sans-serif; }
#box { width:100px; height:100px; background:#4a90d9; position:absolute;
       left:20px; top:20px; color:#fff; text-align:center; line-height:100px; }
#zone { width:200px; height:150px; border:2px solid #999; position:absolute;
        left:400px; top:200px; display:flex; align-items:center; justify-content:center; }
#btn { position:absolute; left:20px; top:300px; padding:8px 16px; }
#out { position:absolute; left:20px; top:360px; }
</style></head><body>
<div id="box">拖</div><div id="zone">放这</div>
<button id="btn">点我</button><div id="out">未点</div>
<script>
let dg=false,sx=0,sy=0,ol=20,ot=20;
const c=document.getElementById("box"),z=document.getElementById("zone");
c.addEventListener("pointerdown", e=>{
  dg=true; sx=e.clientX; sy=e.clientY;
  ol=parseInt(c.style.left||"20"); ot=parseInt(c.style.top||"20");
  c.setPointerCapture(e.pointerId);
});
c.addEventListener("pointermove", e=>{
  if(!dg) return;
  c.style.left=(ol+e.clientX-sx)+"px";
  c.style.top=(ot+e.clientY-sy)+"px";
});
c.addEventListener("pointerup", e=>{
  if(!dg) return; dg=false;
  try{ c.releasePointerCapture(e.pointerId); }catch(_){}
  const r=c.getBoundingClientRect(), zr=z.getBoundingClientRect();
  if(r.left<zr.right && r.right>zr.left && r.top<zr.bottom && r.bottom>zr.top){
    z.dataset.dropped="yes"; z.textContent="已放入";
  }
});
document.getElementById("btn").onclick=()=>{
  document.getElementById("out").textContent="被点了";
};
</script></body></html>"""


def _url() -> str:
    ws = Path(tempfile.mkdtemp(prefix="ix-"))
    p = ws / "index.html"
    p.write_text(PAGE, encoding="utf-8")
    return "file:///" + p.as_posix()


def _page(b):
    page = b.new_page(viewport={"width": 800, "height": 600})
    page.goto(_url())
    page.wait_for_timeout(200)
    return page


def test_click_works():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        browser._run_action(page, {"do": "click", "selector": "#btn"}, 5000)
        page.wait_for_timeout(200)
        assert page.locator("#out").inner_text() == "被点了"
        b.close()


def test_hover_works():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        browser._run_action(page, {"do": "hover", "selector": "#zone"}, 5000)
        b.close()


def test_drag_to_selector():
    """拖到选择器：**必须真的移动了元素**。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        before = page.locator("#box").bounding_box()
        browser._run_action(page, {"do": "drag", "selector": "#box", "to": "#zone"}, 5000)
        page.wait_for_timeout(300)
        after = page.locator("#box").bounding_box()
        assert after["x"] != before["x"], "拖拽没有移动元素"
        dropped = page.evaluate("() => document.getElementById('zone').dataset.dropped || ''")
        assert dropped == "yes", "没有触发放下的处理"
        b.close()


def test_drag_to_coordinates():
    """拖到绝对坐标。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        before = page.locator("#box").bounding_box()
        browser._run_action(page, {"do": "drag", "selector": "#box", "x": 480, "y": 260}, 5000)
        page.wait_for_timeout(300)
        after = page.locator("#box").bounding_box()
        assert after["x"] != before["x"], "坐标拖拽没有移动元素"
        b.close()


def test_unknown_action_is_ignored():
    """未知动作不抛异常（不猜）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        browser._run_action(page, {"do": "nonexistent_action"}, 3000)
        b.close()


def test_recorder_captures_frames():
    """录屏：动作序列每一步前后都有帧。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        rec = browser.Recorder(page, max_frames=20)
        actions = [{"do": "click", "selector": "#btn"},
                   {"do": "hover", "selector": "#zone"},
                   {"do": "drag", "selector": "#box", "to": "#zone"}]
        results = rec.run(page, actions, 5000)
        page.wait_for_timeout(200)
        assert len(rec.frames) >= 4, "帧数太少：%d" % len(rec.frames)
        for fr in rec.frames:
            assert Path(fr["path"]).is_file(), "帧文件不存在"
            assert "t" in fr and "note" in fr
        assert all(r["ok"] for r in results), results
        rec.close()
        b.close()


def test_recorder_records_failed_action():
    """失败的动作也要记录（便于定位）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        rec = browser.Recorder(page, max_frames=10)
        results = rec.run(page, [{"do": "click", "selector": "#nope"}], 1000)
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["why"], "失败要说原因"
        rec.close()
        b.close()


def test_recorder_cleanup():
    """默认清理：不留临时文件。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        rec = browser.Recorder(page, max_frames=5)
        rec.shot("t1")
        rec.shot("t2")
        d = rec._dir
        assert d.is_dir()
        assert len(list(d.glob("*.png"))) == 2
        rec.close()
        assert not d.exists(), "关闭后不该留目录"
        b.close()


def test_recorder_keep_files_when_cleanup_false():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        rec = browser.Recorder(page, max_frames=5, cleanup=False)
        rec.shot("keep")
        d = rec._dir
        rec.close()
        try:
            assert d.is_dir(), "cleanup=False 应保留"
            assert len(list(d.glob("*.png"))) == 1
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)
        b.close()


def test_recorder_data_urls():
    """帧能转 data URL（喂给模型）。"""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = _page(b)
        rec = browser.Recorder(page, max_frames=5)
        rec.shot("a")
        urls = rec.data_urls(limit=2)
        assert urls, "转不出 data URL"
        assert urls[0]["data_url"].startswith("data:image/png;base64,")
        rec.close()
        b.close()
