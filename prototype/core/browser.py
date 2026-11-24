"""浏览器检测（Playwright）：让引擎能**看见**界面，判据全部结构化、零 token。

## 为什么需要（2026-11-15，用户提的缺口）

D1–D6 只测代码与 API。**页面白屏、按钮点不动、JS 报错、图表空着** —— 这些后端全对、
D1–D6 全绿，但交付物用户根本用不了（任务书要求 ECharts 可视化 → 必然有界面）。

## 设计原则（三条，都是踩过的）

1. **不靠截图喂模型** —— 同一页面两次描述可能不同，且每页都烧 token。
   判据只用三样**结构化**信号：`console.error` / `pageerror` / DOM 断言 + a11y 树。
2. **失败必须带证据** —— 截图 + console 原文 + 失败处的 DOM 片段，
   直接进错误报告，远端据此就能改（和 D4 的"证据到行"同一标准）。
3. **不许硬依赖** —— 没装 playwright / 没有浏览器时，`available()` 返回原因，
   检测器记一条 P3（"UI 检测不可用"），**绝不因为环境缺东西就让整条检测链崩**。

## 浏览器通道

优先用**系统已有的** Edge / Chrome（Windows 自带 Edge，省 150MB 下载）；
都没有才回落到 playwright 自带的 chromium。探测结果缓存，只探一次。

## axe-core

可访问性扫描用 axe-core（注入执行，结果本地判定，零 token）。
`axe.min.js` 放在 `data/axe.min.js`；**取不到就跳过 a11y 扫描**（不阻塞主流程）。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["PageVisit", "available", "visit", "visit_electron", "axe_scan", "render_evidence",
           "CHANNELS", "DEFAULT_TIMEOUT"]

# 通道优先级：系统浏览器优先（不用下载），最后才是 google-chrome-stable 兜底
CHANNELS = ("msedge", "chrome", None)
DEFAULT_TIMEOUT = 20000          # ms
AXE_PATH = Path(__file__).resolve().parents[1] / "data" / "axe.min.js"

# **无害噪声**：浏览器自己发的、跟业务无关的请求。
# 实测（2026-11-15）：`favicon.ico` 404 会让每个页面都被判红 ——
# 真实项目大多没有 favicon，这种误报会让"告警疲劳"，**比漏报更毁信任**。
NOISE_URL_PARTS = ("favicon.ico", "/__nextjs", "hot-update",
                   "webpack-dev-server", "/@vite/", "/@react-refresh")


def _is_noise(url: str) -> bool:
    u = str(url or "").lower()
    return any(x in u for x in NOISE_URL_PARTS)


_CHANNEL_CACHE: dict = {}


def _try_channels():
    """返回 (可用通道, 说明)；都没有则 (None, 原因)。结果缓存。"""
    if "chan" in _CHANNEL_CACHE:
        return _CHANNEL_CACHE.get("chan"), _CHANNEL_CACHE.get("why", "")
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                             # noqa: BLE001
        _CHANNEL_CACHE.update({"chan": None,
                               "why": "playwright 未安装（pip install playwright）"})
        return None, _CHANNEL_CACHE["why"]
    why = ""
    try:
        with sync_playwright() as p:
            for ch in CHANNELS:
                try:
                    kw = {"headless": True}
                    if ch:
                        kw["channel"] = ch
                    b = p.chromium.launch(**kw)
                    b.close()
                    _CHANNEL_CACHE.update({"chan": ch})
                    return ch, ""
                except Exception as exc:                 # noqa: BLE001
                    why = "%s: %s" % (ch or "chromium", type(exc).__name__)
    except Exception as exc:                             # noqa: BLE001
        why = "%s: %s" % (type(exc).__name__, exc)
    _CHANNEL_CACHE.update({"chan": None, "why": why or "没有可用浏览器"})
    return None, _CHANNEL_CACHE["why"]


def available() -> tuple[bool, str]:
    """能不能做 UI 检测 → (能不能, 原因)。"""
    ch, why = _try_channels()
    return (ch is not None or _CHANNEL_CACHE.get("chan") is not None), why


@dataclass
class PageVisit:
    """一次页面访问的结果（全部可判定，无主观成分）。"""
    url: str
    ok: bool = False
    status: int = 0
    final_url: str = ""
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    dom: list[dict] = field(default_factory=list)      # 每条断言的结果
    a11y_violations: list[dict] = field(default_factory=list)
    a11y_tree: str = ""                                # 压缩后的可访问性树（前 N 行）
    screenshot: str = ""
    #: 视觉体检结果（2026-11-24）
    visual: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""                                    # 过程异常（不是页面错）

    def problems(self) -> list[str]:
        """把结果翻译成"哪儿不对"的一句话列表（空 = 这一步没问题）。"""
        out: list[str] = []
        if self.error:
            out.append("打开失败：%s" % self.error)
            return out
        if self.status and self.status >= 400:
            out.append("HTTP %d" % self.status)
        for e in self.page_errors[:3]:
            out.append("未捕获 JS 异常：%s" % e[:200])
        for e in self.console_errors[:3]:
            out.append("console.error：%s" % e[:200])
        for r in self.failed_requests[:3]:
            out.append("请求失败：%s" % r[:200])
        for d in self.dom:
            if not d.get("ok"):
                out.append("DOM 断言不通过：%s" % d.get("desc", ""))
        for v in self.a11y_violations[:3]:
            out.append("可访问性[%s]：%s" % (v.get("impact") or "?",
                                            v.get("help") or v.get("id") or ""))
        return out


def _aria_compact(page, max_lines: int = 60) -> str:
    """可访问性树（压缩）：只留 role + name + 关键状态。

    为什么用 a11y 而不是 DOM 全文：它对"按钮存在但 disabled""输入框没有 label"
    这类**界面缺陷**最敏感，而且**比 DOM 小一个数量级**（省 token、好判）。
    """
    try:
        snap = page.locator("body").aria_snapshot()
    except Exception:                                    # noqa: BLE001
        try:
            snap = str(page.accessibility.snapshot() or "")
        except Exception:                                # noqa: BLE001
            return ""
    lines = [ln.rstrip() for ln in str(snap or "").splitlines() if ln.strip()]
    return "\n".join(lines[:max_lines])


def _run_action(page, act: dict, timeout: int) -> None:
    """执行一个动作（结构固定，不自由发挥）。"""
    kind = str(act.get("do") or act.get("action") or "").lower()
    sel = act.get("selector") or ""
    if kind in ("goto", "open"):
        page.goto(str(act.get("url") or ""), wait_until="domcontentloaded",
                  timeout=timeout)
    elif kind in ("fill", "type"):
        page.fill(sel, str(act.get("value") or ""), timeout=timeout)
    elif kind == "click":
        page.click(sel, timeout=timeout)
    elif kind in ("dblclick", "doubleclick"):
        page.dblclick(sel, timeout=timeout)
    elif kind == "select":
        page.select_option(sel, str(act.get("value") or ""), timeout=timeout)
    elif kind == "press":
        page.press(sel or "body", str(act.get("key") or "Enter"), timeout=timeout)
    elif kind in ("wait", "wait_for"):
        if sel:
            page.wait_for_selector(sel, timeout=timeout)
        else:
            page.wait_for_timeout(int(act.get("ms") or 1000))
    elif kind == "reload":
        page.reload(wait_until="domcontentloaded", timeout=timeout)
    # 未知动作忽略（不猜）


def _check_dom(page, exp: dict, timeout: int) -> dict:
    """一条 DOM 断言：{selector, text?, visible?, count?, attr?{k,v}}。"""
    sel = str(exp.get("selector") or exp.get("css") or "")
    desc = "选择器 %s" % (sel or "?")
    res = {"desc": desc, "ok": False}
    if not sel:
        res["desc"] = "断言缺 selector"
        return res
    try:
        loc = page.locator(sel)
        cnt = loc.count()
        res["count"] = cnt
        if "count" in exp:
            want = int(exp["count"])
            res["ok"] = cnt == want
            res["desc"] = "%s 数量 %d（期望 %d）" % (sel, cnt, want)
            return res
        if cnt == 0:
            res["desc"] = "%s 不存在" % sel
            return res
        if "visible" in exp:
            vis = loc.first.is_visible()
            res["ok"] = bool(vis) == bool(exp["visible"])
            res["desc"] = "%s 可见性 %s（期望 %s）" % (sel, vis, exp["visible"])
            return res
        if "text" in exp:
            want = str(exp["text"])
            got = (loc.first.inner_text(timeout=timeout) or "").strip()
            res["ok"] = want in got
            res["desc"] = "%s 文本为 %r（期望包含 %r）" % (sel, got[:120], want[:60])
            return res
        if "attr" in exp and isinstance(exp["attr"], dict):
            ok = True
            detail = []
            for k, v in exp["attr"].items():
                got = loc.first.get_attribute(str(k))
                detail.append("%s=%r" % (k, got))
                if str(got) != str(v):
                    ok = False
            res["ok"] = ok
            res["desc"] = "%s 属性 %s" % (sel, ", ".join(detail))
            return res
        # 计算样式断言（2026-11-23）：{style: {"--ui-bg": "#fff", "color": "..."}}
        # 自定义属性用 getPropertyValue 读到的是**声明值原样**（不经 rgb 归一化），
        # 所以可以直接和用户填的色值做字符串比对；常规属性则是计算值。
        if "style" in exp and isinstance(exp["style"], dict):
            ok2 = True
            detail2 = []
            for prop, want in exp["style"].items():
                got = loc.first.evaluate(
                    "(el, p) => getComputedStyle(el).getPropertyValue(p)", str(prop))
                got = str(got if got is not None else "").strip()
                detail2.append("%s=%r" % (prop, got))
                if got.lower() != str(want).strip().lower():
                    ok2 = False
            res["ok"] = ok2
            res["desc"] = "%s 计算样式 %s" % (sel, ", ".join(detail2))
            return res
        # 只要求存在
        res["ok"] = True
        res["desc"] = "%s 存在（%d 个）" % (sel, cnt)
        return res
    except Exception as exc:                             # noqa: BLE001
        res["desc"] = "%s 断言异常：%s" % (sel, type(exc).__name__)
        return res


def axe_scan(page) -> list[dict]:
    """注入 axe-core 跑可访问性扫描 → violations（失败返回空，不阻塞）。"""
    if not AXE_PATH.is_file():
        return []
    try:
        page.add_script_tag(path=str(AXE_PATH))
        got = page.evaluate(
            "async () => { const r = await axe.run(document,"
            " {runOnly: {type: 'tag', values: ['wcag2a','wcag2aa']},"
            "  resultTypes: ['violations']});"
            " return (r.violations||[]).slice(0,10).map(v=>({"
            "   id: v.id, impact: v.impact, help: v.help,"
            "   nodes: (v.nodes||[]).length })); }")
        return list(got or [])
    except Exception:                                    # noqa: BLE001
        return []


def visit(url: str, *, actions=None, expect_dom=None, expect_url: str = "",
          expect_no_errors: bool = True, screenshot_dir=None,
          timeout: int = DEFAULT_TIMEOUT, with_a11y: bool = True,
          visual=None) -> PageVisit:
    """访问一个页面，跑动作与断言，收集证据。

    `actions`：动作列表（`[{do: goto|fill|click|select|press|wait, ...}]`）
    `expect_dom`：DOM 断言列表
    `expect_url`：期望最终 URL（子串匹配）
    `expect_no_errors`：要求无 JS 异常 / 无 console.error
    """
    t0 = time.time()
    visit_res = PageVisit(url=url)
    ch, why = _try_channels()
    if ch is None and _CHANNEL_CACHE.get("chan") is None:
        visit_res.error = "没有可用浏览器：%s" % (why or "unknown")
        return visit_res
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                             # noqa: BLE001
        visit_res.error = "playwright 未安装：%s" % exc
        return visit_res

    try:
        with sync_playwright() as p:
            kw = {"headless": True}
            _c = _CHANNEL_CACHE.get("chan")
            if _c:
                kw["channel"] = _c
            browser = p.chromium.launch(**kw)
            ctx = browser.new_context(ignore_https_errors=True,
                                      viewport={"width": 1440, "height": 900})
            page = ctx.new_page()

            def _on_console(msg):
                try:
                    if msg.type != "error":
                        return
                    txt = str(msg.text or "")
                    # "Failed to load resource" 是**网络失败的衍生消息**，
                    # 具体的 URL 与状态码已经记在 failed_requests 里；
                    # 留着它等于同一件事报两遍（且 favicon 那类噪声也会混进来）。
                    if "Failed to load resource" in txt:
                        return
                    visit_res.console_errors.append(txt[:300])
                except Exception:                        # noqa: BLE001
                    pass

            def _on_pageerror(err):
                visit_res.page_errors.append(str(err)[:300])

            def _on_failed(req):
                try:
                    if _is_noise(req.url):
                        return
                    visit_res.failed_requests.append(
                        "%s %s (%s)" % (req.method, req.url[:160],
                                        (req.failure or "")[:80]))
                except Exception:                        # noqa: BLE001
                    pass

            def _on_response(resp):
                try:
                    if _is_noise(resp.url):
                        return
                    if resp.status >= 400 and not resp.url.startswith("data:"):
                        visit_res.failed_requests.append(
                            "HTTP %d %s" % (resp.status, resp.url[:160]))
                except Exception:                        # noqa: BLE001
                    pass

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)
            page.on("requestfailed", _on_failed)
            page.on("response", _on_response)

            resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            visit_res.status = int(getattr(resp, "status", 0) or 0)
            for act in (actions or []):
                try:
                    _run_action(page, act, timeout)
                except Exception as exc:                 # noqa: BLE001
                    visit_res.dom.append(
                        {"desc": "动作 %s 失败：%s" % (act.get("do"),
                                                   type(exc).__name__),
                         "ok": False})
                    break
            # 给渲染留一点时间（ECharts 是异步画的）
            try:
                page.wait_for_timeout(600)
            except Exception:                            # noqa: BLE001
                pass
            visit_res.final_url = page.url
            for exp in (expect_dom or []):
                visit_res.dom.append(_check_dom(page, exp, timeout))
            if expect_url and expect_url not in page.url:
                visit_res.dom.append({"desc": "最终 URL 为 %s（期望包含 %s）"
                                              % (page.url, expect_url), "ok": False})
            if with_a11y:
                visit_res.a11y_violations = axe_scan(page)
                visit_res.a11y_tree = _aria_compact(page)
            # 视觉体检（2026-11-24）：DOM 断言说"元素存在"，但可能被遮住/看不见。
            # 用**确定性的浏览器 API**（不是截图喂模型），同一页面跑 100 次答案一样。
            # 放在 DOM 断言之后、截图之前 —— 先算结论，再留证据。
            if visual:
                try:
                    from . import visual as _vismod
                    _vr = _vismod.visual_check(
                        page,
                        not_covered=visual.get("expect_not_covered"),
                        visible=visual.get("expect_visible"),
                        contrast=visual.get("expect_contrast"))
                    visit_res.visual = _vr
                    if not _vr.get("ok", True):
                        visit_res.dom.append({
                            "desc": "视觉体检：" + _vismod.describe(_vr),
                            "ok": False, "visual": _vr})
                except Exception as exc:                 # noqa: BLE001
                    visit_res.visual = {"ok": False, "checked": 0,
                                        "note": "视觉体检异常：%s: %s"
                                                % (type(exc).__name__, exc)}
            # 截图（失败时也有，作为证据）
            if screenshot_dir:
                try:
                    d = Path(screenshot_dir)
                    d.mkdir(parents=True, exist_ok=True)
                    name = "ui-%d.png" % int(time.time() * 1000)
                    fp = d / name
                    page.screenshot(path=str(fp), full_page=True)
                    visit_res.screenshot = str(fp)
                except Exception:                        # noqa: BLE001
                    pass
            browser.close()
    except Exception as exc:                             # noqa: BLE001
        visit_res.error = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        visit_res.elapsed_ms = int((time.time() - t0) * 1000)
        return visit_res

    visit_res.elapsed_ms = int((time.time() - t0) * 1000)
    visit_res.ok = not visit_res.problems()
    return visit_res


def visit_electron(exe: str, *, app_dir: str, actions=None, expect_dom=None,
                   expect_title: str = "", timeout: int = DEFAULT_TIMEOUT,
                   ready_timeout: float = 45.0, with_a11y: bool = True) -> PageVisit:
    """真机 Electron UI 验收（2026-11-23，甜区收尾）。

    Playwright 的 Python 版没有 `_electron.launch`，但 Chromium 驱动支持
    `connect_over_cdp`。做法：以 `--remote-debugging-port=<port>` 启动 electron
    应用 → 轮询 CDP 端口就绪 → `connect_over_cdp` 连上第一个窗口 → 复用
    `visit()` 同款 DOM 断言 / 动作 / console+pageerror 收集 → 杀进程树。

    铁律（同 visit，**默认红**）：
    - electron 二进制不存在 / CDP 端口起不来 / 连不上 → 如实报"测不了"，绝不假装通过；
    - 没有实质断言（expect_dom / actions / expect_title 全空）→ 由 probe.validate 拒收；
    - 进程树无论成败都在 finally 里清掉，不留僵尸窗口。

    `exe`：electron 可执行文件绝对路径（由调用方从仓库 node_modules 或系统找）。
    `app_dir`：electron 应用的入口目录（含 package.json 的 main）。
    """
    import socket
    import subprocess as _sp
    import tempfile

    t0 = time.time()
    res = PageVisit(url=f"electron://{exe}")
    exe = str(exe or "")
    if not exe or not Path(exe).is_file():
        res.error = "electron 可执行文件不存在（如实说测不了）：%s" % (exe or "(空)")
        return res
    # 找一个空闲端口（bind 0 让 OS 分配，随即释放给 electron 用）
    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
    except OSError as exc:                               # noqa: BLE001
        res.error = "分配调试端口失败：%s" % exc
        return res

    logf = None
    log_path = ""
    proc = None
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                             # noqa: BLE001
        res.error = "playwright 未安装：%s" % exc
        return res

    try:
        _fd, log_path = tempfile.mkstemp(prefix="electron_", suffix=".log")
        os.close(_fd)
        logf = open(log_path, "w", encoding="utf-8", errors="replace")
        cmd = [exe, f"--remote-debugging-port={port}", "--no-sandbox", str(app_dir or ".")]
        try:
            proc = _sp.Popen(cmd, cwd=str(app_dir or "."), stdout=logf,
                             stderr=_sp.STDOUT)
        except Exception as exc:                          # noqa: BLE001
            res.error = "启动 electron 失败：%s: %s" % (type(exc).__name__, exc)
            return res

        # 轮询 CDP 端口就绪（electron 起窗口要几秒）
        deadline = time.time() + max(1.0, ready_timeout)
        ready = False
        while time.time() < deadline:
            try:
                ck = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                ck.close()
                ready = True
                break
            except OSError:
                if proc.poll() is not None:
                    break
                time.sleep(0.3)
        if not ready:
            tail = ""
            try:
                tail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-400:]
            except OSError:
                pass
            res.error = "electron %.0fs 内没起来 CDP 端口 %d：%s" % (ready_timeout, port, tail)
            return res

        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = None
            # 找第一个可用窗口（electron 首个 BrowserWindow）
            for cand in ctx.pages:
                if not cand.url.startswith("devtools://"):
                    page = cand
                    break
            if page is None and ctx.pages:
                page = ctx.pages[0]
            if page is None:
                res.error = "electron 已连上但没有可验收的窗口（BrowserWindow 未创建？）"
                browser.close()
                return res

            def _on_console(msg):
                try:
                    if msg.type == "error" and "Failed to load resource" not in str(msg.text or ""):
                        res.console_errors.append(str(msg.text)[:300])
                except Exception:                        # noqa: BLE001
                    pass

            def _on_pageerror(err):
                try:
                    res.page_errors.append(str(err)[:300])
                except Exception:                        # noqa: BLE001
                    pass

            page.on("console", _on_console)
            page.on("pageerror", _on_pageerror)

            for act in (actions or []):
                try:
                    _run_action(page, act, timeout)
                except Exception as exc:                 # noqa: BLE001
                    res.dom.append({"desc": "动作 %s 失败：%s" % (act.get("do"),
                                                               type(exc).__name__), "ok": False})
                    break
            try:
                page.wait_for_timeout(600)
            except Exception:                            # noqa: BLE001
                pass
            res.final_url = page.url
            for exp in (expect_dom or []):
                res.dom.append(_check_dom(page, exp, timeout))
            if expect_title:
                try:
                    actual = page.title()
                except Exception:                        # noqa: BLE001
                    actual = ""
                res.dom.append({"desc": "窗口标题 %r（期望含 %r）" % (actual, expect_title),
                                "ok": expect_title in actual})
            if with_a11y:
                res.a11y_violations = axe_scan(page)
                res.a11y_tree = _aria_compact(page)
            browser.close()
    except Exception as exc:                             # noqa: BLE001
        res.error = "%s: %s" % (type(exc).__name__, str(exc)[:300])
        res.elapsed_ms = int((time.time() - t0) * 1000)
        return res
    finally:
        # 无论成败都清进程树 + 日志文件，不留僵尸窗口
        if proc is not None:
            try:
                proc.terminate()
            except Exception:                            # noqa: BLE001
                pass
            try:
                proc.wait(timeout=3)
            except Exception:                            # noqa: BLE001
                try:
                    proc.kill()
                except Exception:                        # noqa: BLE001
                    pass
        if logf is not None:
            try:
                logf.close()
            except Exception:                            # noqa: BLE001
                pass
        if log_path:
            try:
                os.unlink(log_path)
            except OSError:
                pass

    res.elapsed_ms = int((time.time() - t0) * 1000)
    res.ok = not res.problems()
    return res


def render_evidence(v: PageVisit, limit: int = 6) -> str:
    """把一次访问翻译成**给远端看的证据块**（进错误报告）。"""
    lines = ["- URL：%s" % v.url]
    if v.final_url and v.final_url != v.url:
        lines.append("- 最终跳转：%s" % v.final_url)
    if v.status:
        lines.append("- HTTP：%d" % v.status)
    probs = v.problems()
    if probs:
        lines.append("- 问题：")
        for x in probs[:limit]:
            lines.append("    · %s" % x)
    if v.dom:
        lines.append("- DOM 断言：")
        for d in v.dom[:limit]:
            lines.append("    %s %s" % ("✓" if d.get("ok") else "✗", d.get("desc", "")))
    if v.a11y_tree:
        lines.append("- 可访问性树（前几行，用来判断元素是否存在/可用）：")
        for ln in v.a11y_tree.splitlines()[:8]:
            lines.append("    %s" % ln[:160])
    if v.screenshot:
        lines.append("- 截图：%s" % v.screenshot)
    return "\n".join(lines)
