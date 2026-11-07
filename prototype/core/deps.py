"""产物依赖的自举：扫描 import → 补装缺失包 → 写 requirements.txt。

为什么必须有（实测 2026-11-07）：作者按目标写了 `import jwt`（JWT 鉴权本来就要求），
但环境里没装 PyJWT → 整个 `app.main` 导入失败 → **9 条能力全部变红**，
系统判"做不出来"，一轮覆盖从 6/9 掉到 0/8。这不是代码错，是环境没跟上，
而且项目连 requirements.txt 都没有（"pip install -r requirements.txt 后可直接启动"是硬要求）。

口径：
  · 只认**顶层第三方 import**（stdlib 白名单排除），不猜框架；
  · 包名映射只覆盖常见坑（jwt→PyJWT 这类名字对不上的），其余按模块名直接装；
  · 装不上就如实返回失败原因，**绝不假装装好了**（探针会继续红，但原因写得清楚）。
"""
from __future__ import annotations

import subprocess
import sys

from pathlib import Path

# 模块名 → pip 包名（只列名字对不上的那些；其余默认同名）
PIP_NAME = {
    "jwt": "PyJWT",
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "serial": "pyserial",
    "OpenSSL": "pyOpenSSL",
    "jose": "python-jose",
    "multipart": "python-multipart",
    "pydantic_settings": "pydantic-settings",
    "dateutil": "python-dateutil",
}

# 标准库（常见的一批够了；剩下的靠"装不上就算了"兜底，不会误伤）
STDLIB = set("""
__future__ abc argparse ast asyncio base64 binascii bisect builtins calendar cgi cmd codecs
collections colorsys concurrent configparser contextlib copy csv ctypes dataclasses datetime
decimal difflib dis email enum errno faulthandler filecmp fileinput fnmatch fractions ftplib
functools gc getpass gettext glob gzip hashlib heapq hmac html http imaplib importlib inspect
io ipaddress itertools json keyword linecache locale logging lzma mailbox math mimetypes mmap
multiprocessing numbers operator os pathlib pickle pkgutil platform plistlib pprint profile pty
queue quopri random re reprlib secrets select selectors shelve shlex shutil signal site smtplib
socket socketserver sqlite3 ssl stat statistics string stringprep struct subprocess sys sysconfig
tarfile tempfile textwrap threading time timeit token tokenize traceback tracemalloc types
typing unicodedata unittest urllib uuid venv warnings wave weakref webbrowser xml xmlrpc zipfile
zlib uuid dataclasses
""".split())

REQ_FILE = "requirements.txt"


def _scan_file(p: Path) -> set[str]:
    import ast
    try:
        tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return set()
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:                      # 相对导入 = 自己的包，不算依赖
                continue
            if node.module:
                mods.add(node.module.split(".")[0])
    return mods


def scan(ws) -> set[str]:
    """产物里用到的**第三方**顶层模块（排除自己包、标准库）。"""
    ws = Path(ws)
    roots = [p for p in ws.iterdir() if p.is_dir() and p.name not in
             (".sixcoder", ".git", "__pycache__", ".pytest_cache", ".mypy_cache", "tests", "venv", ".venv")]
    mods: set[str] = set()
    for root in roots:
        for p in root.rglob("*.py"):
            mods |= _scan_file(p)
    for p in ws.glob("*.py"):
        mods |= _scan_file(p)
    return {m for m in mods if m not in STDLIB and not m.startswith("_")}


def installable(mods: set[str]) -> list[str]:
    return sorted(PIP_NAME.get(m, m) for m in mods)


def missing(mods: set[str]) -> set[str]:
    """哪些模块当前 import 不进来（用同一个解释器探，不靠 pip list 猜）。"""
    import importlib.util
    out: set[str] = set()
    for m in mods:
        try:
            if importlib.util.find_spec(m) is None:
                out.add(m)
        except (ImportError, ValueError):
            out.add(m)
    return out


def _is_asgi(ws: Path) -> bool:
    """项目里有没有 ASGI 应用（决定 requirements 要不要带 uvicorn）。"""
    for p in Path(ws).rglob("*.py"):
        if ".sixcoder" in p.parts:
            continue
        try:
            t = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if "FastAPI(" in t or "Starlette(" in t:
            return True
    return False


def ensure(ws, *, install: bool = True, timeout: int = 180) -> dict:
    """扫依赖 → 补装缺失 → 写 requirements.txt。返回 {missing, installed, failed, req_file}。

    只装在**当前解释器**里（探针用的就是它），不碰用户的全局环境假设。
    """
    ws = Path(ws)
    mods = scan(ws)
    lack = missing(mods)
    installed: list[str] = []
    failed: list[dict] = []
    if lack and install:
        for mod in sorted(lack):
            pkg = PIP_NAME.get(mod, mod)
            try:
                proc = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                                       "--disable-pip-version-check", pkg],
                                      capture_output=True, text=True, timeout=timeout,
                                      encoding="utf-8", errors="replace")
                if proc.returncode == 0:
                    installed.append(pkg)
                else:
                    failed.append({"module": mod, "pkg": pkg,
                                   "why": (proc.stderr or proc.stdout or "")[-200:]})
            except Exception as exc:                 # noqa: BLE001
                failed.append({"module": mod, "pkg": pkg, "why": f"{type(exc).__name__}: {exc}"[:200]})
    req = ws / REQ_FILE
    try:
        want = installable(mods)
        # ASGI 应用必须能 `uvicorn app.main:app` 起来：uvicorn 不会被 import，
        # 所以扫 import 扫不到它，得按"项目里有 FastAPI()"这条事实补上。
        if _is_asgi(ws) and "uvicorn" not in {w.lower() for w in want}:
            want = sorted(set(want) | {"fastapi", "uvicorn"})
        if want:
            req.write_text("\n".join(want) + "\n", encoding="utf-8")
    except OSError:
        pass
    return {"modules": sorted(mods), "missing": sorted(lack), "installed": installed,
            "failed": failed, "req_file": str(req) if req.exists() else "",
            "ok": not missing(mods)}
