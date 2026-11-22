"""闭环验收编排器：把"用户挑样式 + 半自动 + 插件适配 + 判据开放"收口成一张可跑验收单。

这是最后落地的一块（用户口径：先把其他做完，验证放最后做）。它**不重复实现**任何
探针/校验，而是调用前面各模块的真实入口，汇总成可机读的验收结果：

  · style        → uistyles.contract_text() 真生成 + catalog 结构校验；
  · human_once   → 真跑一次 put→get→消费 的内存闭环（不落盘、不回显）；
  · host_plugin  → 清单完整性 + 每适配器有公开文档/判据/人在环标注；
  · judgment     → judgment_sources.contract_text() 真生成 + 全 https；
  · checks       → 真跑指定 check_*.py 子进程（可选；钉住"能跑起来"）。

诚实边界（必须写进结果，防谎报）：
  · 这些验收覆盖的是**结构与落地**（约定生成对了、产物在、命令 rc、DOM/CSS 变量），
    不覆盖"好不好看 / 剪辑顺不顺"——那类判据仍在人眼里，结果里 `human_review`
    明确列出，不假装机判通过。
  · 每项默认红（fail-closed）：模块缺失/异常=不通过，绝不空白过关。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
_CHECKS = _ROOT / "checks"
_PY = Path(sys.executable)


def _safe(module: str, fn: str, *args, **kwargs) -> tuple[bool, Any, str]:
    """反射调用模块的纯函数；任何异常=失败（默认红）。"""
    try:
        mod = __import__(f"core.{module}", fromlist=[fn])
        f = getattr(mod, fn)
        return True, f(*args, **kwargs), ""
    except Exception as exc:                             # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"


def _browser_css_check(html: str, expect: list[dict], timeout: int = 20000) -> tuple[bool, str]:
    """把 HTML 写成临时页，用**真浏览器**打开并读计算样式。

    浏览器不可用 → 返回 False（fail-closed：拿不到信号就说拿不到，不假装通过）。
    注意 `timeout` 单位是**毫秒**（沿用 browser.visit 的口径）。
    """
    try:
        from core import browser as _browser
    except Exception as exc:                             # noqa: BLE001
        return False, f"浏览器模块不可用：{type(exc).__name__}"
    avail, why = _browser.available()
    if not avail:
        return False, f"没有可用浏览器，CSS 落地测不了（{why}）"
    import tempfile
    tmp: Path | None = None
    try:
        fd = tempfile.NamedTemporaryFile(mode="w", suffix=".html", delete=False,
                                        encoding="utf-8")
        fd.write(html)
        fd.close()
        tmp = Path(fd.name)
        v = _browser.visit(tmp.as_uri(), expect_dom=expect,
                           expect_no_errors=False, with_a11y=False, timeout=timeout)
        if v.error:
            return False, f"浏览器打开验收页失败：{v.error}"
        bad = [d for d in v.dom if not d.get("ok")]
        if bad:
            return False, "；".join(str(d.get("desc") or "?") for d in bad)[:300]
        return True, ""
    except Exception as exc:                             # noqa: BLE001
        return False, f"真跑验收异常：{type(exc).__name__}: {exc}"
    finally:
        if tmp is not None:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:                            # noqa: BLE001
                pass


def verify_style(pid: str = "") -> dict[str, Any]:
    ok, cat, err = _safe("uistyles", "public_config")
    presets = (cat or {}).get("presets", []) if ok else []
    sel = (cat or {}).get("selected") or {}
    selected = str(pid or sel.get("selected") or "").strip()
    human = "配色与美观仍由用户判断"
    # 用户没选风格时，contract 为空是**预期**（不注入），不判失败
    if not selected:
        return {"name": "style", "ok": ok and len(presets) == 10,
                "detail": (f"预设 {len(presets)} 个；用户未选风格（不注入，符合预期）"
                           if ok else err),
                "evidence": "", "human_review": human}
    ok2, txt, err2 = _safe("uistyles", "contract_text", selected)
    if not ok2 or not txt:
        return {"name": "style", "ok": False,
                "detail": f"风格约定未生成（风格 id 无效？）{err2}", "evidence": "",
                "human_review": human}
    # 真跑：生成最小验收页 → 真浏览器读 CSS 变量 → 与用户填的值比对
    ok3, html, err3 = _safe("uistyles", "demo_html", None, selected)
    ok4, vars_, err4 = _safe("uistyles", "css_vars", None)
    if not ok3 or not html:
        return {"name": "style", "ok": False,
                "detail": f"验收页生成失败：{err3}", "evidence": (txt or "")[:300],
                "human_review": human}
    expect_vars = dict(vars_ or {"--ui-primary": "#3b82f6"})
    expect = [{"selector": ":root", "style": expect_vars},
              {"selector": "main#ui-root", "visible": True}]
    ev, why = _browser_css_check(html, expect)
    passed = bool(ok and len(presets) == 10 and ev)
    return {"name": "style", "ok": passed,
            "detail": (f"风格约定已生成；真浏览器读到 {len(expect_vars)} 个 CSS 变量落地"
                       if passed else (why or "CSS 变量未按约定落地")),
            "evidence": (txt or "")[:300], "human_review": human}


def verify_human_once() -> dict[str, Any]:
    err = ""
    try:
        from core import human_once as h
        h.clear_all()
        h.put("__verify__", "otp", "123456", ttl=60, uses=1)
        v = h.get("__verify__", "otp")
        consumed = not h.has("__verify__", "otp")
        h.clear_all()
        ok = True
    except Exception as exc:                             # noqa: BLE001
        ok, v, consumed = False, "", False
        err = f"{type(exc).__name__}: {exc}"
    passed = ok and v == "123456" and consumed
    return {"name": "human_once", "ok": bool(passed),
            "detail": "人填一次→取用→消费闭环通过（值不回显）" if passed else (err or "闭环失败"),
            "evidence": "", "human_review": "真实验证码/OTP 仍需人提供一次"}


def verify_host_plugin(adapter_id: str = "") -> dict[str, Any]:
    ok, d, err = _safe("host_plugins", "describe")
    ok2, sug, err2 = _safe("host_plugins", "probe_suggestion", adapter_id or "libreoffice")
    passed = ok and ok2 and (d or {}).get("adapter_count", 0) >= 5 \
        and (sug or {}).get("ok") is True
    return {"name": "host_plugin", "ok": bool(passed),
            "detail": f"适配器 {(d or {}).get('adapter_count')} 个；探针建议已生成"
            if passed else f"{err or err2}",
            "evidence": (sug or {}).get("contract", "")[:400],
            "human_review": "premiere/wps 的剪辑/排版质量必须人看；机器只验产物"}


def verify_judgment(source_id: str = "") -> dict[str, Any]:
    ok, d, err = _safe("judgment_sources", "describe")
    ok2, txt, err2 = _safe("judgment_sources", "contract_text", source_id)
    passed = ok and ok2 and (d or {}).get("verified_count", 0) >= 5 \
        and (d or {}).get("all_docs_https") is True
    return {"name": "judgment", "ok": bool(passed),
            "detail": f"{(d or {}).get('verified_count')} 个平台判据开放（全 https 文档）"
            if passed else f"{err or err2}",
            "evidence": (txt or "")[:400],
            "human_review": "判据开放只搬读取权；整体美感/创意仍人判"}


def verify_check(script: str, timeout: int = 180) -> dict[str, Any]:
    """真跑一个自检脚本（子进程隔离），钉住'能力能跑起来'。"""
    name = Path(script).name
    if not name.startswith("check_") or not name.endswith(".py") or "/" in name or "\\" in name:
        return {"name": f"check:{script}", "ok": False,
                "detail": "脚本名非法（只接受 check_*.py）", "evidence": "",
                "human_review": ""}
    path = _CHECKS / name
    if not path.is_file():
        return {"name": f"check:{name}", "ok": False,
                "detail": "自检脚本不存在", "evidence": "", "human_review": ""}
    t0 = time.time()
    try:
        cp = subprocess.run([str(_PY), str(path)], cwd=str(_ROOT),
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=max(10, int(timeout)))
        out = ((cp.stdout or "") + (cp.stderr or "")).strip()
        tail = "\n".join(out.splitlines()[-4:])
        return {"name": f"check:{name}", "ok": cp.returncode == 0,
                "detail": f"rc={cp.returncode} ({round(time.time()-t0, 1)}s)",
                "evidence": tail[-600:], "human_review": ""}
    except subprocess.TimeoutExpired:
        return {"name": f"check:{name}", "ok": False,
                "detail": f"超时 {timeout}s", "evidence": "", "human_review": ""}
    except Exception as exc:                             # noqa: BLE001
        return {"name": f"check:{name}", "ok": False,
                "detail": f"{type(exc).__name__}: {exc}", "evidence": "",
                "human_review": ""}


def run_all(*, pid: str = "", source_id: str = "", adapter_id: str = "",
            checks: list[str] | None = None) -> dict[str, Any]:
    """跑一张完整验收单。每项默认红；汇总 ok = 所有项都过。"""
    items = [
        verify_style(pid),
        verify_human_once(),
        verify_host_plugin(adapter_id),
        verify_judgment(source_id),
    ]
    for s in (checks or []):
        items.append(verify_check(s))
    ok = all(i["ok"] for i in items)
    return {
        "ok": ok,
        "passed": sum(1 for i in items if i["ok"]),
        "total": len(items),
        "items": items,
        "human_review": sorted({i["human_review"] for i in items if i.get("human_review")}),
        "note": "机验只覆盖结构/产物/DOM/CSS；审美与真链路仍人在环，不谎报全自动",
    }


def describe() -> dict[str, Any]:
    return {
        "verifies": ["style", "human_once", "host_plugin", "judgment", "checks"],
        "fail_closed": True,
        "aesthetic_auto_verified": False,
        "note": "最后落地的验证功能：把四块能力的验收路径收口成一张可跑单",
    }
