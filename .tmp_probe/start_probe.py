"""一次性诊断：启动安装版 Electron，捕获其输出并探测后端端口。

只为查清"静默安装后能否正常启动"，不参与产品逻辑。
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

D = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "SixCoder"
exe = D / "SixCoder.exe"

# 剔除大小写重复的代理变量：Node/Electron 构造环境块时会因
# HTTPS_PROXY 与 https_proxy 并存而报 “已添加项。字典中的关键字”
dup = [k for k in os.environ if k in ("https_proxy", "http_proxy", "no_proxy", "all_proxy")]
env = {k: v for k, v in os.environ.items() if k not in dup}
print(f"剔除的重复小写代理变量: {dup}")

p = subprocess.Popen([str(exe)], cwd=str(D), env=env,
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True, encoding="utf-8", errors="replace")
found, t0 = None, time.time()
while time.time() - t0 < 60:
    if p.poll() is not None:
        break
    for port in range(8760, 8772):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                found = port
                break
        except OSError:
            continue
    if found:
        break
    time.sleep(2)

print(f"退出码: {p.poll()} | 后端端口: {found} | 等待 {time.time() - t0:.0f}s")
if p.poll() is None:
    p.terminate()
try:
    out = p.communicate(timeout=10)[0]
except subprocess.TimeoutExpired:
    p.kill()
    out = p.communicate()[0]
print("--- 输出（末 2000 字）---")
print((out or "(空)")[-2000:])
sys.exit(0)
