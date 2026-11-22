"""真机 Electron UI 探针自检（2026-11-23，甜区收尾）。

钉住：
  1. validate 拒收空壳（只判"能启动"过不了）；
  2. exe 不存在 → fail-closed（如实说测不了，不假装通过）；
  3. 真 DOM 断言路径：用真 chromium 加载测试页，验证 expect_dom/expect_title
     真断言通过、错误断言真失败（monkeypatch CDP 连接到真浏览器）；
  4. 进程树在 finally 里清理（不留僵尸）；
  5. run() 分发到 electron 分支 + describe() 正确。

monkeypatch 全部还原，且用的是本机已装 chromium（不下载、不连服务器）。
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:140]}" if detail and not ok else ""))


def main() -> int:
    from core import probe as pr
    from core import browser as br

    print("[1] validate：空壳探针被拒")
    check("electron 进入 PROBE_KINDS", "electron" in pr.PROBE_KINDS)
    issues_empty = pr.validate({"kind": "electron"})
    check("缺 exe 被拒", any("exe" in i for i in issues_empty), str(issues_empty))
    issues_noassert = pr.validate({"kind": "electron", "exe": "electron.exe"})
    check("只判能启动被拒", any("太弱" in i for i in issues_noassert), str(issues_noassert))
    good = {"kind": "electron", "exe": "electron.exe",
            "expect_dom": [{"selector": "h1", "text": "Hi"}]}
    check("合法 electron 探针通过校验", pr.validate(good) == [], str(pr.validate(good)))
    desc = pr.describe(good)
    check("describe 说清是 electron CDP", "electron" in desc and "DOM" in desc, desc)

    print("[2] exe 不存在 → fail-closed")
    r = pr._find_electron_exe(ROOT, {"exe": "no/such/electron.exe"})
    check("找不到的 exe 不解析成有效路径", r == "")
    res = br.visit_electron(str(ROOT / "no-such-electron.exe"), app_dir=str(ROOT))
    check("缺 exe 判失败", not res.ok)
    check("缺 exe 如实说测不了", "测不了" in (res.error or ""), res.error)
    run_r = pr.run(ROOT, {"kind": "electron", "exe": "no/such/electron.exe",
                          "expect_title": "X"})
    check("run 层也默认红（找不到 exe）", not run_r["ok"] and "测不了" in run_r["detail"],
          run_r["detail"])

    print("[3] 真 DOM 断言路径（真 chromium 冒充 CDP 连接）")
    # 造一个测试 HTML 页面
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False, encoding="utf-8")
    tmp.write("<!doctype html><html><head><title>TestApp Window</title></head>"
              "<body><h1 id='hero'>你好 Electron</h1><button id='go'>Go</button></body></html>")
    tmp.close()
    page_url = Path(tmp.name).as_uri()

    # 先填 channel 缓存（msedge），避免 patch 后 _try_channels 内部 launch 被递归替换
    br.available()
    cached_channel = br._CHANNEL_CACHE.get("chan")
    check("有可用系统浏览器通道（真断言前置）", bool(cached_channel), str(cached_channel))

    import playwright.sync_api as ps
    real_sync_playwright = ps.sync_playwright
    real_popen = br.subprocess.Popen if hasattr(br, "subprocess") else None
    # visit_electron 内部用的是函数级 import subprocess as _sp，patch 系统模块
    import subprocess as sp_mod
    real_sp_popen = sp_mod.Popen
    real_create_conn = socket.create_connection
    cleaned = {"terminate": 0, "kill": 0}

    class FakeProc:
        def poll(self):
            return None                      # 假装 electron 一直活着
        def terminate(self):
            cleaned["terminate"] += 1
        def wait(self, timeout=None):
            return 0
        def kill(self):
            cleaned["kill"] += 1

    def fake_popen(*a, **kw):
        return FakeProc()

    def fake_conn(addr, timeout=None):
        class Sock:
            def close(self):
                pass
        return Sock()

    class FakeSync:
        def __init__(self):
            self.real = real_sync_playwright()
        def __enter__(self):
            self.impl = self.real.__enter__()
            return self
        def __exit__(self, *a):
            return self.real.__exit__(*a)
        @property
        def chromium(self):
            outer = self
            class FakeChromium:
                def connect_over_cdp(self, url):
                    kw = {"headless": True}
                    if cached_channel:
                        kw["channel"] = cached_channel
                    b = outer.impl.chromium.launch(**kw)
                    ctx = b.new_context()
                    page = ctx.new_page()
                    page.goto(page_url)
                    return b
            return FakeChromium()

    # 给一个"存在"的假 exe（内容是空文件即可，Popen 被 patch 不会真执行它）
    fake_exe = tempfile.NamedTemporaryFile(suffix=".exe", delete=False)
    fake_exe.close()

    try:
        ps.sync_playwright = FakeSync
        sp_mod.Popen = fake_popen
        socket.create_connection = fake_conn

        # 正确断言：h1 文本 + 标题
        pv = br.visit_electron(fake_exe.name, app_dir=str(ROOT),
                               expect_dom=[{"selector": "#hero", "text": "你好"}],
                               expect_title="TestApp", with_a11y=False)
        check("真 DOM 断言（文本）通过", pv.ok and all(d["ok"] for d in pv.dom), str(pv.dom))
        check("断言条数 = dom + title = 2", len(pv.dom) == 2, str(pv.dom))
        check("无 console/pageerror", not pv.console_errors and not pv.page_errors,
              str(pv.console_errors) + str(pv.page_errors))

        # 错误断言：文本不符 → 必须判红
        pv2 = br.visit_electron(fake_exe.name, app_dir=str(ROOT),
                                expect_dom=[{"selector": "#hero", "text": "不存在的文本"}],
                                with_a11y=False)
        check("错误文本断言判红", not pv2.ok)
        check("错误断言给出证据", any("文本" in d.get("desc", "") for d in pv2.dom),
              str(pv2.dom))

        # 错误标题 → 必须判红
        pv3 = br.visit_electron(fake_exe.name, app_dir=str(ROOT),
                                expect_title="WrongTitle", with_a11y=False)
        check("错误标题断言判红", not pv3.ok)

        # 进程树清理（finally 一定 terminate）
        check("进程在 finally 里清理", cleaned["terminate"] >= 3, str(cleaned))
    finally:
        ps.sync_playwright = real_sync_playwright
        sp_mod.Popen = real_sp_popen
        socket.create_connection = real_create_conn
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        try:
            os.unlink(fake_exe.name)
        except OSError:
            pass

    print("[4] run() 分发到 electron 分支")
    # 用一个会失败的 exe 确认 run 走的是 electron executor（detail 带"electron 可执行文件"）
    rr = pr.run(ROOT, {"kind": "electron", "exe": "missing/electron.exe",
                       "expect_dom": [{"selector": "h1"}]}, timeout=5)
    check("run 返回 kind=electron", rr.get("kind") == "electron", str(rr))
    check("run 没走 cli 兜底", "py_call" not in rr.get("detail", "") and
          "缺 module" not in rr.get("detail", ""), rr.get("detail", ""))

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
