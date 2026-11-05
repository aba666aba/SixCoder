"""依赖审计升级：幻觉包核对 + 已知漏洞查询（L 级起硬约束）。

为什么必须要：
    ① **幻觉包**：模型会写出 `pip install requests-html-parser` 这类不存在的包名。
       这种代码在本地可能"看起来能跑"（只要没人装），装的时候才炸，而且报错完全误导。
    ② **已知漏洞**：加了依赖就等于扩大攻击面，`pip-audit` 只看已安装的，
       而我们需要看**代码里 import 的**。

数据源（都是公开 API，无需密钥）：
    · PyPI JSON API：`https://pypi.org/pypi/<name>/json` → 包是否存在
    · OSV API：`https://api.osv.dev/v1/query` → 该包该版本是否命中已知漏洞

三条纪律：
    · 结果落缓存（`.sixcoder/deps_cache.json`，默认 7 天），不重复打网络；
    · 没有网络/查不到时**如实说"未能核对"**，绝不当作"通过"；
    · 只报告"名字不存在""命中 CVE/OSV 条目"这类可核对的事实，不猜"这个包是否可信"。
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.request
from pathlib import Path

CACHE_FILE = ".sixcoder/deps_cache.json"
CACHE_TTL_S = 7 * 24 * 3600
_SKIP_DIRS = {".git", ".sixcoder", "__pycache__", "node_modules", ".venv", "venv",
              "dist", "build", ".mypy_cache", ".pytest_cache", ".tmp"}
_NET_TIMEOUT = 6


# ------------------------------------------------------------------ 采集
def _stdlib() -> set[str]:
    names = set(getattr(sys, "stdlib_module_names", ()))
    names |= {"typing", "dataclasses", "sqlite3", "pathlib", "asyncio"}
    return names


def local_modules(ws: Path) -> set[str]:
    ws = Path(ws)
    out: set[str] = set()
    for p in ws.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.relative_to(ws).parts):
            continue
        out.add(p.stem)
        out.add(p.parent.name)
    return out


def extract_imports(ws: Path) -> dict[str, list[str]]:
    """{第三方包名: [出现在哪些文件]} —— 跳过标准库与本仓库自己的模块。"""
    import ast
    ws = Path(ws)
    std = _stdlib()
    local = local_modules(ws)
    out: dict[str, list[str]] = {}
    for p in ws.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in p.relative_to(ws).parts):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        rel = str(p.relative_to(ws)).replace("\\", "/")
        for n in ast.walk(tree):
            names: list[str] = []
            if isinstance(n, ast.Import):
                names = [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                names = [n.module.split(".")[0]]
            for name in names:
                low = name.lower()
                if low in std or low in local or low == "core" or low in ("m",):
                    continue
                out.setdefault(name, [])
                if rel not in out[name]:
                    out[name].append(rel)
    return out


def declared_versions(ws: Path) -> dict[str, str]:
    """从 requirements*.txt / pyproject 里读锁定的版本（读不到就空）。"""
    ws = Path(ws)
    out: dict[str, str] = {}
    for f in list(ws.glob("requirements*.txt")):
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#")[0].strip()
            m = re.match(r"^([A-Za-z0-9_.\-]+)\s*==\s*([0-9][\w.\-]*)$", line)
            if m:
                out[m.group(1).lower()] = m.group(2)
    return out


# ------------------------------------------------------------------ 缓存
def _load_cache(ws: Path) -> dict:
    f = Path(ws) / CACHE_FILE
    if not f.exists():
        return {}
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def _save_cache(ws: Path, cache: dict) -> None:
    f = Path(ws) / CACHE_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")


def _fresh(entry: dict, ttl: int = CACHE_TTL_S) -> bool:
    try:
        return (time.time() - float(entry.get("at") or 0)) < ttl
    except Exception:                                    # noqa: BLE001
        return False


# ------------------------------------------------------------------ 网络（可注入，便于自检）
def fetch_pypi(name: str) -> dict:
    """查 PyPI 是否存在这个包。返回 {ok, exists, version, error}。"""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=_NET_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        return {"ok": True, "exists": True,
                "version": str((data.get("info") or {}).get("version") or "")}
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"ok": True, "exists": False, "version": ""}
        return {"ok": False, "exists": None, "error": f"HTTP {exc.code}"}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "exists": None, "error": type(exc).__name__}


def fetch_osv(name: str, version: str = "") -> dict:
    """查 OSV 已知漏洞。返回 {ok, vulns: [{id, summary}], error}。"""
    body: dict = {"package": {"name": name, "ecosystem": "PyPI"}}
    if version:
        body["version"] = version
    try:
        req = urllib.request.Request(
            "https://api.osv.dev/v1/query",
            data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as r:
            data = json.loads(r.read().decode())
        vulns = [{"id": v.get("id", ""), "summary": (v.get("summary") or "")[:120]}
                 for v in (data.get("vulns") or [])]
        return {"ok": True, "vulns": vulns}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "vulns": [], "error": type(exc).__name__}


# ------------------------------------------------------------------ 审计
def audit(ws: Path, use_network: bool = True) -> dict:
    """跑一遍依赖审计。返回 {imports, missing(幻觉包), vulns, unverified, ok}。"""
    ws = Path(ws)
    imports = extract_imports(ws)
    versions = declared_versions(ws)
    cache = _load_cache(ws)
    missing: list[dict] = []
    vulns: list[dict] = []
    unverified: list[dict] = []

    for name in sorted(imports):
        key = name.lower()
        entry = cache.get(key) or {}
        pypi = entry.get("pypi") or {}
        if not _fresh(pypi):
            if use_network:
                pypi = {**fetch_pypi(name), "at": time.time()}
            else:
                pypi = {"ok": False, "error": "离线（未联网核对）", "at": 0}
            entry["pypi"] = pypi
            cache[key] = entry
        if pypi.get("ok") and pypi.get("exists") is False:
            missing.append({"name": name, "files": imports[name][:3],
                            "why": "PyPI 上没有这个包（疑似模型编造的包名）"})
        elif not pypi.get("ok"):
            unverified.append({"name": name, "stage": "pypi",
                               "why": pypi.get("error") or "未知原因"})

        ver = versions.get(key, "")
        osv = entry.get("osv") or {}
        if not _fresh(osv):
            if use_network:
                osv = {**fetch_osv(name, ver), "at": time.time(), "version": ver}
            else:
                osv = {"ok": False, "error": "离线（未联网核对）", "at": 0}
            entry["osv"] = osv
            cache[key] = entry
        if osv.get("ok") and osv.get("vulns"):
            for v in osv["vulns"]:
                vulns.append({"name": name, "version": ver or "未锁定",
                              "id": v["id"], "summary": v["summary"]})
        elif not osv.get("ok"):
            unverified.append({"name": name, "stage": "osv",
                               "why": osv.get("error") or "未知原因"})

    _save_cache(ws, cache)
    return {"imports": sorted(imports), "count": len(imports),
            "missing": missing, "vulns": vulns, "unverified": unverified,
            "ok": not missing and not vulns, "network_used": bool(use_network)}


def summarize(res: dict, limit: int = 3) -> str:
    """一句话结论（进交付报告/事件），带修法。"""
    parts: list[str] = []
    if res.get("missing"):
        names = ", ".join(m["name"] for m in res["missing"][:limit])
        parts.append(f"**疑似幻觉包** {len(res['missing'])} 个（{names}）"
                     f" → 删掉或换成真实存在的包，PyPI 查不到就是装不上")
    if res.get("vulns"):
        first = res["vulns"][0]
        parts.append(f"**已知漏洞** {len(res['vulns'])} 条（{first['name']} {first['id']}）"
                     f" → 升级到修复版本，或在白名单里写清接受风险的理由")
    if res.get("unverified"):
        # 按**依赖名去重**数数：一个包要查 PyPI + OSV 两个来源，以前直接 len()
        # 于是"1 个依赖没核对"被写成"2 个依赖"，甲方以为多了一个包。
        names_u = sorted({str(x.get("name") or "") for x in res["unverified"] if x.get("name")})
        parts.append(f"未能核对 {len(names_u)} 个依赖（{', '.join(names_u[:limit])}；"
                     f"{res['unverified'][0]['why']}）—— 不算通过")
    if not parts:
        return f"依赖 {res.get('count', 0)} 个：PyPI 存在性核对通过、无已知漏洞"
    return "；".join(parts)
