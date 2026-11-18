"""钉住「界面监督」（2026-11-15，开发文档 §13.10）。

## 这个自检要证明什么

1. **判据六种情形都对**：无数据 / 健康 / 瞬时 / **持续故障** / 恢复 / 变慢
2. **连续失败才告警** —— 单次失败只记瞬时（网络抖动很常见，误报会让人不再看告警）
3. **台账能回答"哪条旅程坏了"** —— 每轮按探针记通过/失败
4. **两条降级**：没 ui 探针 → `no_ui`（项目没界面，不是错）；没 base_url → `no_base`
5. **真开浏览器跑三轮** —— 坏页面应判 persistent，换好页面后应判 recovered
"""
from __future__ import annotations

import http.server
import json
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import browser, uiwatch  # noqa: E402

FAILS: list[str] = []

BAD_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>watcher</title></head><body><h1>监督测试</h1>
<button id="ok" aria-label="按钮">点我</button><p id="out">未点击</p>
<script>
  setTimeout(function(){ console.error('watch-boom'); }, 60);
  document.getElementById('ok').addEventListener('click', function(){
    document.getElementById('out').textContent = 'clicked'; });
</script></body></html>"""

GOOD_HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>watcher</title></head><body><h1>监督测试</h1>
<button id="ok" aria-label="按钮">点我</button><p id="out">未点击</p>
<script>
  document.getElementById('ok').addEventListener('click', function(){
    document.getElementById('out').textContent = 'clicked'; });
</script></body></html>"""


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name +
          (("  — " + detail) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def _mk_bp(cids):
    class _C:
        def __init__(self, cid):
            self.id = cid
            self.probe = {"kind": "ui", "steps": [
                {"do": "goto", "url": "/"},
                {"do": "click", "selector": "#ok"},
                {"expect_dom": [{"selector": "#out", "text": "clicked"}]}]}
    class _BP:
        pass
    bp = _BP()
    bp.caps = [_C(c) for c in cids]
    return bp


def main() -> int:
    print("[1] 判据六种情形（纯函数，不跑浏览器）")
    cases = [
        ("无数据", [], "no_data"),
        ("健康", [{"ok": True, "elapsed_ms": 100}], "healthy"),
        ("瞬时", [{"ok": True, "elapsed_ms": 100}, {"ok": False, "elapsed_ms": 110}],
         "transient"),
        ("持续故障", [{"ok": False, "elapsed_ms": 100}] * 3, "persistent"),
        ("恢复", [{"ok": False, "elapsed_ms": 100}, {"ok": True, "elapsed_ms": 120}],
         "recovered"),
    ]
    for name, recs, want in cases:
        got = uiwatch.judge(recs)["action"]
        check("%s → %s" % (name, want), got == want, "实际 %s" % got)
    slow = uiwatch.judge([{"ok": True, "elapsed_ms": 100},
                          {"ok": True, "elapsed_ms": 100},
                          {"ok": True, "elapsed_ms": 900}])
    check("变慢 → slow=True", slow["slow"] is True, str(slow))
    only_once = uiwatch.judge([{"ok": False, "elapsed_ms": 100}])
    check("单轮失败不算持续（streak<3）", only_once["action"] == "transient",
          str(only_once))

    print("[2] 两条降级路径（不许环境问题冒充代码问题）")
    with tempfile.TemporaryDirectory() as td:
        w = Path(td)
        r1 = uiwatch.watch(w, _mk_bp([]), base_url="http://x")
        check("没 ui 探针 → no_ui", r1["action"] == "no_ui", str(r1))
        r2 = uiwatch.watch(w, _mk_bp(["cap.ui.a"]), base_url="")
        check("没 base_url → no_base", r2["action"] == "no_base", str(r2))

    ok_env, why = browser.available()
    if not ok_env:
        print("\n[3] 跳过真跑：浏览器不可用（%s）" % (why or "?"))
        print("—— 这本身就是降级路径：环境缺东西不影响判据部分")
    else:
        print("[3] 真跑：坏页面连跑 3 轮 → 应判 persistent")
        with tempfile.TemporaryDirectory() as td:
            w = Path(td)
            site = w / "site"
            site.mkdir()
            (site / "index.html").write_text(BAD_HTML, encoding="utf-8")
            handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(
                *a, directory=str(site), **kw)
            httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % port
            bp = _mk_bp(["cap.ui.watch"])

            v = uiwatch.watch(w, bp, base_url=base, rounds=3, interval_s=0)
            recs = uiwatch.load_records(w)
            check("跑了 3 轮并记了 3 条台账", len(recs) == 3, str(len(recs)))
            check("坏页面 → 判 persistent", v["action"] == "persistent", str(v))
            check("台账里能看出是哪条探针坏",
                  any((x.get("per") or [{}])[0].get("cap") == "cap.ui.watch"
                      for x in recs), str(recs[-1])[:200] if recs else "空")
            check("台账每条都带失败原因",
                  bool(((recs[-1].get("per") or [{}])[0].get("what") or "")),
                  str(recs[-1])[:200] if recs else "空")

            print("[4] 换成好页面再跑 1 轮 → 应判 recovered")
            (site / "index.html").write_text(GOOD_HTML, encoding="utf-8")
            v2 = uiwatch.watch(w, bp, base_url=base, rounds=1, interval_s=0)
            check("修好后 → 判 recovered", v2["action"] == "recovered", str(v2))
            check("台账累计 4 条（历史保留）", len(uiwatch.load_records(w)) == 4,
                  str(len(uiwatch.load_records(w))))
            httpd.shutdown()

            txt = uiwatch.render_history(w, limit=10)
            check("摘要能读出通过/失败轮数", "✓" in txt and "✗" in txt, txt[:200])

    print()
    if FAILS:
        print("FAILED: %d 项 —— " % len(FAILS) + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
