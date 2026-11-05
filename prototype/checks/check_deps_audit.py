"""依赖审计自检：幻觉包 / 已知漏洞 / 缓存 / 离线如实说 / 分档。

全部离线可验证：网络函数是模块级可注入的，自检里替换成假实现，
所以**不需要联网**也能证明判据成立（这是"机制可行"而不是"我猜它能跑"）。

跑法: python check_deps_audit.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import deps_audit as da, quality  # noqa: E402

FAILS: list[str] = []


def _sandbox_temp():
    import tempfile as _tf
    root = Path(__file__).resolve().parents[1] / ".tmp"
    root.mkdir(exist_ok=True)
    return _tf.TemporaryDirectory(dir=str(root))


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("依赖审计自检")

    print("[1] 采集：跳过标准库与本仓库自己的模块")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "clean.py").write_text("import os\nimport sqlite3\nimport requests\n", encoding="utf-8")
        (ws / "store.py").write_text("from clean import normalize\n", encoding="utf-8")
        imports = da.extract_imports(ws)
        check("抓到第三方 requests", "requests" in imports, str(imports))
        check("标准库被跳过", "os" not in imports and "sqlite3" not in imports, str(imports))
        check("本仓库模块被跳过", "clean" not in imports, str(imports))
        check("记录出现在哪个文件", imports.get("requests") == ["clean.py"], str(imports))

    print("[2] 幻觉包：PyPI 查不到 → 点名，并给修法")
    orig_pypi, orig_osv = da.fetch_pypi, da.fetch_osv
    calls = {"pypi": 0, "osv": 0}

    def fake_pypi(name: str) -> dict:
        calls["pypi"] += 1
        if name in ("requests", "pytest"):
            return {"ok": True, "exists": True, "version": "1.0"}
        return {"ok": True, "exists": False, "version": ""}

    def fake_osv(name: str, version: str = "") -> dict:
        calls["osv"] += 1
        if name == "pytest":
            return {"ok": True, "vulns": [{"id": "GHSA-test", "summary": "测试用漏洞"}]}
        return {"ok": True, "vulns": []}

    da.fetch_pypi, da.fetch_osv = fake_pypi, fake_osv      # type: ignore
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "m.py").write_text(
                "import requests\nimport requests_html_parser\nimport pytest\n",
                encoding="utf-8")
            res = da.audit(ws)
            check("识别出幻觉包", [m["name"] for m in res["missing"]]
                  == ["requests_html_parser"], str(res["missing"]))
            check("识别出已知漏洞", res["vulns"] and res["vulns"][0]["id"] == "GHSA-test",
                  str(res["vulns"]))
            check("整体判为不 ok", res["ok"] is False)
            s = da.summarize(res)
            check("摘要点名幻觉包并给修法", "幻觉包" in s and "PyPI" in s, s)
            check("摘要点名漏洞", "漏洞" in s, s)

            print("[3] 缓存：第二次不再打网络")
            before = dict(calls)
            res2 = da.audit(ws)
            check("第二次没有新增网络调用",
                  calls["pypi"] == before["pypi"] and calls["osv"] == before["osv"],
                  f"{before} → {calls}")
            check("缓存文件已落盘", (ws / da.CACHE_FILE).exists())
            check("第二次结论一致", res2["missing"] == res["missing"])
    finally:
        da.fetch_pypi, da.fetch_osv = orig_pypi, orig_osv  # type: ignore

    print("[4] 离线/查不到 → 如实说'未核对'，绝不当作通过")
    def boom_pypi(name: str) -> dict:
        return {"ok": False, "exists": None, "error": "离线（未联网核对）"}

    def boom_osv(name: str, version: str = "") -> dict:
        return {"ok": False, "vulns": [], "error": "离线（未联网核对）"}

    da.fetch_pypi, da.fetch_osv = boom_pypi, boom_osv     # type: ignore
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "m.py").write_text("import requests\n", encoding="utf-8")
            res = da.audit(ws)
            check("记入未核对", res["unverified"], str(res["unverified"]))
            check("摘要里写清不算通过", "不算通过" in da.summarize(res), da.summarize(res))
            check("unverified 不导致 missing 误报", not res["missing"], str(res["missing"]))
    finally:
        da.fetch_pypi, da.fetch_osv = orig_pypi, orig_osv  # type: ignore

    print("[5] 分档：S 跳过；M 只报告；L+ 阻断")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "m.py").write_text("import requests_html_parser\n", encoding="utf-8")
        g_s = quality.gate_deps_audit(ws, "S")
        check("S 级跳过", g_s.evidence_ref == "skipped", g_s.evidence_ref)

    da.fetch_pypi = lambda name: {"ok": True, "exists": name != "bad_pkg", "version": "1"}
    da.fetch_osv = lambda name, version="": {"ok": True, "vulns": []}
    try:
        with _sandbox_temp() as td:
            ws = Path(td)
            (ws / "m.py").write_text("import bad_pkg\n", encoding="utf-8")
            g_m = quality.gate_deps_audit(ws, "M")
            check("M 级只报告（不阻断）", g_m.ok, f"{g_m.evidence_ref} / {g_m.detail[:60]}")
            g_l = quality.gate_deps_audit(ws, "L")
            check("L 级阻断", not g_l.ok, g_l.evidence_ref)
            check("阻断时给了修法", "删掉" in g_l.detail or "换成" in g_l.detail, g_l.detail[:100])
    finally:
        da.fetch_pypi, da.fetch_osv = orig_pypi, orig_osv  # type: ignore

    print("[6] 版本锁定：能从 requirements 读出来（查漏洞要用）")
    with _sandbox_temp() as td:
        ws = Path(td)
        (ws / "requirements.txt").write_text("requests==2.31.0\n# 注释\npytest>=7\n",
                                             encoding="utf-8")
        v = da.declared_versions(ws)
        check("读出 == 锁定版本", v.get("requests") == "2.31.0", str(v))
        check("非 == 的不算锁定", "pytest" not in v, str(v))

    print("[7] 已接进分级门禁与交付报告")
    q = (Path(__file__).resolve().parents[1] / "core" / "quality.py").read_text(encoding="utf-8")
    check("extra_gates 带上依赖审计", "gate_deps_audit(ws, level)" in q)
    comp = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("交付报告写依赖审计结论", "依赖审计（幻觉包/已知漏洞）" in comp)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（幻觉包/漏洞/缓存/离线如实/分档/版本锁定/接入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
