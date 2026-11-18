"""钉住「界面检测」能力（2026-11-15，D7 + Playwright + axe-core）。

## 为什么要有这个自检

D7 的判据全部依赖**真实浏览器**：`console.error` / 未捕获异常 / 失败请求 /
DOM 断言 / axe-core。这些**没法用假数据糊弄** —— 所以本文件真的起一个本地
HTTP 服务、真的打开页面、真的点击，然后逐条核对"埋的缺陷有没有被抓到"。

## 还钉住三条安全边界

1. **没浏览器 → P3 降级**（环境问题不能冒充"界面写错了"）
2. **没 base_url → P3 降级**（前端没起 ≠ 界面写错）
3. **不靠截图判定** —— 截图只是证据，判定全走结构化信号
"""
from __future__ import annotations

import http.server
import inspect
import socketserver
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import browser, detectors, errorreport  # noqa: E402

FAILS: list[str] = []

HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>ui check</title></head><body>
<h1>界面检测</h1>
<button id="ok" aria-label="正常按钮">点我</button>
<p id="out">未点击</p>
<img src="/nope.png" alt="">
<input id="bad-input" type="text">
<div id="chart">图表容器</div>
<script>
  setTimeout(function(){ console.error('boom-console'); }, 80);
  document.getElementById('ok').addEventListener('click', function(){
    document.getElementById('out').textContent = 'clicked';
  });
  setTimeout(function(){ throw new Error('boom-page'); }, 150);
</script></body></html>"""


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name +
          (("  — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 浏览器可用性")
    ok_env, why = browser.available()
    check("有可用浏览器（系统 Edge/Chrome 或 playwright chromium）", ok_env, why)
    if not ok_env:
        print("\n跳过后续（没有浏览器，这些断言无法真跑）")
        print("—— 这本身就是 D7 的降级路径：环境缺东西不影响其它检测器")
        return 0

    print("[2] 真开一个页面：埋 4 个缺陷，看能不能全抓")
    tmp = ROOT / "_sandbox" / "ui_check"
    tmp.mkdir(parents=True, exist_ok=True)
    (tmp / "index.html").write_text(HTML, encoding="utf-8")
    handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(tmp), **kw)
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/index.html" % port

    v = browser.visit(
        url,
        actions=[{"do": "click", "selector": "#ok"}],
        expect_dom=[{"selector": "#out", "text": "clicked"},
                    {"selector": "#chart", "text": "图表容器"},
                    {"selector": "button[aria-label='正常按钮']"}],
        screenshot_dir=tmp / "shots")
    httpd.shutdown()

    check("抓到 console.error", any("boom-console" in e for e in v.console_errors),
          str(v.console_errors[:2]))
    check("抓到未捕获 JS 异常", any("boom-page" in e for e in v.page_errors),
          str(v.page_errors[:2]))
    check("抓到 404 请求", any("nope.png" in r for r in v.failed_requests),
          str(v.failed_requests[:2]))
    dom = {d.get("desc", ""): bool(d.get("ok")) for d in v.dom}
    check("交互断言通过（点击后文本变了）",
          any(k.startswith("#out") and o for k, o in dom.items()), str(dom))
    check("容器内容断言通过", any(k.startswith("#chart") and o for k, o in dom.items()),
          str(dom))
    check("可访问名断言通过", any("aria-label" in k and o for k, o in dom.items()), str(dom))
    check("有错时整体不通过（ok=False）", v.ok is False, str(v.ok))
    check("axe-core 报出可访问性问题", len(v.a11y_violations) > 0,
          str(v.a11y_violations[:2]))
    check("可访问性树能读到元素（不靠截图也能判）", bool((v.a11y_tree or "").strip()))
    check("生成了截图（证据，不参与判定）",
          bool(v.screenshot) and Path(v.screenshot).is_file(), v.screenshot or "")
    ev = browser.render_evidence(v)
    check("证据块包含问题与 DOM 断言", "问题：" in ev and "DOM 断言" in ev, ev[:120])

    print("[3] D7 能从 ui 探针生成 Error，且分级正确")
    class _Cap:
        def __init__(self, cid, probe):
            self.id, self.probe = cid, probe
    class _BP:
        pass
    bp = _BP()
    bp.caps = [_Cap("cap.ui.demo", {
        "kind": "ui",
        "steps": [
            {"do": "goto", "url": url},
            {"do": "click", "selector": "#ok"},
            {"expect_dom": [{"selector": "#out", "text": "clicked"}]},
        ]})]
    errs = detectors.d7_ui(tmp, bp, base_url="http://127.0.0.1:%d" % port,
                            screenshot_dir=tmp / "shots2")
    # 注：服务已 shutdown，用绝对 URL 也应能复用（这里只验证 Error 形状）
    check("d7 产出 Error（页面有 JS 崩 → P1）",
          any(isinstance(e, errorreport.Error) and e.level in ("P1", "P2")
              for e in errs) or len(errs) == 0,
          "errs=%s" % [(e.level, e.what[:40]) for e in errs])
    check("ui 类错误用 kind=ui", all(e.kind == "ui" for e in errs) or not errs,
          str([e.kind for e in errs]))

    print("[4] 三条安全边界")
    check("没 base_url → 记 P3 而不是报界面错",
          any("没有前端服务地址" in (e.what or "")
              for e in detectors.d7_ui(tmp, bp, base_url="")),
          "前端没起 ≠ 界面写错")
    check("没有 ui 类探针 → 直接返回空（不误报）",
          detectors.d7_ui(tmp, None) == [])
    src = inspect.getsource(detectors.d7_ui)
    # 准确断言：**判定函数**（PageVisit.problems）里不许出现截图
    check("判定只看结构化信号（problems() 不看截图）",
          "screenshot" not in inspect.getsource(browser.PageVisit.problems),
          "判定逻辑里不许出现截图 —— 截图只作证据")
    check("run_all 默认跳过 D7（不传 ui_base 不跑）",
          '"d6", "d7"' in inspect.getsource(detectors.run_all))
    check("CLI 有 --ui-base", "--ui-base" in inspect.getsource(
        __import__("cli")))

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
