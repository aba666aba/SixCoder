"""一次性诊断：用 CDP 连安装版窗口，看它到底显示了什么（错误页会直接说明原因）。"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

D = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "SixCoder"
exe = D / "SixCoder.exe"
CDP = 9223

# 干净环境：剔除任何大小写形式的 proxy 变量（本机环境块里存在重复键）
env = {k: v for k, v in os.environ.items() if "proxy" not in k.lower()}

p = subprocess.Popen([str(exe), f"--remote-debugging-port={CDP}"], cwd=str(D), env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

up, t0 = False, time.time()
while time.time() - t0 < 40:
    if p.poll() is not None:
        print(f"进程提前退出，码={p.poll()}")
        break
    try:
        with socket.create_connection(("127.0.0.1", CDP), timeout=0.6):
            up = True
            break
    except OSError:
        time.sleep(1)
print(f"CDP 端口 {CDP} 可用: {up} | 进程存活: {p.poll() is None}")

if up:
    sys.path.insert(0, str(Path(r"F:\WorkSpace\SixCoder") / "prototype"))
    from core import browser as br
    ch, _ = br._try_channels()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        kw = {"headless": True}
        if ch:
            kw["channel"] = ch
        b = pw.chromium.launch(**kw)
        try:
            b2 = b.new_context().new_page()
            b2.goto("about:blank")
        except Exception:
            pass
        # 连 Electron 自己的 CDP
        eb = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP}")
        print(f"连接成功，上下文 {len(eb.contexts)} 个")
        for ci, ctx in enumerate(eb.contexts):
            for pi, page in enumerate(ctx.pages):
                try:
                    title = page.title()
                except Exception:
                    title = "?"
                try:
                    body = page.locator("body").inner_text()[:900]
                except Exception as e:
                    body = f"(读不到 body: {type(e).__name__})"
                print(f"\n--- ctx{ci} page{pi} title={title!r} url={page.url} ---")
                print(body.replace("\n", "\n    "))
        b.close()

if p.poll() is None:
    p.terminate()
time.sleep(1)
print("\n诊断结束")
