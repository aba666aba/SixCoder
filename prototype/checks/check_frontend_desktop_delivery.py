"""前端 SPA / 桌面的「交付链路」自检（2026-11-23 用户补全需求）。

背景（真实缺口，不是设计想象）：
  生态包 frontend_spa/electron 在开发目录里早就写好、check_ecosystem.py 全绿，
  但**用户装的安装版根本用不上**，两处断点：
    1. packaging/electron-builder.json 的 filter 明确排除了 `data/ecosystem/**`
       → 安装包里没有这些包 → cli.py night 的 load_all() 加载不到 → 退回只有
       FastAPI/Django 两栈，前端/桌面项目仍按后端处理（全绿却全错）。
    2. requirements.txt 没有 playwright → core/browser.py 第一行 import 就失败
       → browser 探针不可用 → 前端 SPA 的 UI 验收闭环（它唯一能用的探针）断了。
  本自检钉住整条链路：打包配置 → 依赖清单 → 包加载 → 识别 → 模板 → 生成器。

不跑 npm build / 不启浏览器（慢且依赖网络/系统），那由独立冒烟验证过。
跑法: python checks/check_frontend_desktop_delivery.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT.parent / "packaging"
sys.path.insert(0, str(ROOT))

from core import ecosystem as eco  # noqa: E402
from core import stack  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 打包配置：生态包必须随安装包分发（上轮最大的断点）")
    cfg_path = PKG / "electron-builder.json"
    check("electron-builder.json 存在", cfg_path.is_file())
    if cfg_path.is_file():
        cfg_text = cfg_path.read_text(encoding="utf-8")
        try:
            cfg = json.loads(cfg_text)
            filt = []
            for res in cfg.get("extraResources", []):
                filt += res.get("filter", [])
            check("filter 不再排除 data/ecosystem/**",
                  not any("ecosystem" in str(x) for x in filt),
                  str([x for x in filt if "ecosystem" in x]))
            check("prototype 整包作为 extraResources 分发",
                  any(r.get("from") == "../prototype" for r in cfg.get("extraResources", [])))
        except json.JSONDecodeError as exc:
            check("electron-builder.json 可解析", False, str(exc))

    print("[2] 依赖清单：browser 探针要有 playwright（前端验收唯一探针）")
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    pw_lines = [l for l in req.splitlines()
                if l.strip().lower().startswith("playwright")]
    check("requirements.txt 含 playwright", bool(pw_lines), str(pw_lines))
    check("playwright 锁了精确版本（闸门要求 ==）",
          any("==" in l for l in pw_lines), str(pw_lines))

    print("[3] 打包后 import 自检也带 playwright（防装上了但用不了）")
    build = (PKG / "build_installer.py").read_text(encoding="utf-8")
    check("install_deps 自检 import playwright.sync_api",
          "playwright.sync_api" in build)

    print("[4] 运行时加载：load_all 能把三个包装进栈表")
    loaded = eco.load_all()
    check("load_all 无加载失败", not loaded.get("failed"), str(loaded.get("failed")))
    for n in ("frontend_spa", "electron", "microservice"):
        check(f"加载了 {n}", n in loaded.get("loaded", []), str(loaded.get("loaded")))
    names = stack.names()
    check("frontend_spa 注册进 stack", "frontend_spa" in names, str(names))
    check("electron 注册进 stack", "electron" in names, str(names))

    print("[5] 识别：按仓库实物能判出前端/桌面栈（不误判成 fastapi）")
    import tempfile
    ws_spa = Path(tempfile.mkdtemp())
    (ws_spa / "package.json").write_text('{"name":"x"}', encoding="utf-8")
    (ws_spa / "index.html").write_text("<html></html>", encoding="utf-8")
    (ws_spa / "vite.config.js").write_text("export default {}", encoding="utf-8")
    check("前端 SPA 仓判 frontend_spa", stack.detect(ws_spa) == "frontend_spa",
          stack.detect(ws_spa))
    ws_el = Path(tempfile.mkdtemp())
    (ws_el / "electron").mkdir()
    (ws_el / "electron" / "main.cjs").write_text(
        "new BrowserWindow({})", encoding="utf-8")
    check("Electron 仓判 electron", stack.detect(ws_el) == "electron",
          stack.detect(ws_el))
    ws_py = Path(tempfile.mkdtemp())
    (ws_py / "app").mkdir()
    (ws_py / "app" / "main.py").write_text(
        "from fastapi import FastAPI\napp=FastAPI()", encoding="utf-8")
    check("普通 FastAPI 仓不被生态包抢判", stack.detect(ws_py) == "fastapi",
          stack.detect(ws_py))

    print("[6] 模板：前端/桌面骨架齐全（引擎复制 templates/）")
    for n, must in [
        ("frontend_spa", ["package.json", "index.html", "vite.config.js",
                          "src/main.jsx", "src/App.jsx"]),
        ("electron", ["package.json", "electron/main.cjs",
                      "src/renderer/main.jsx"]),
    ]:
        td = eco.pack_dir(n) / "templates"
        for rel in must:
            check(f"{n}/templates/{rel} 存在", (td / rel).is_file())

    print("[7] 生成器：adapter 能产前端页、能读路由")
    a_spa, why = eco.load_adapter("frontend_spa")
    check("frontend_spa adapter 可加载", a_spa is not None, why)
    if a_spa is not None:
        class _S:
            path = "/about"
        made = a_spa.render_route(_S())
        check("render_route 产出 .jsx 页面",
              bool(made) and made[0][0].endswith(".jsx"), str(made))
        check("parse_routes 能读 JSX <Route>",
              "/x" in a_spa.parse_routes('<Route path="/x" element={<X/>}/>'))
        check("asgi_app=False（走 browser 探针不走 http）", a_spa.asgi_app() is False)
        check("web_rules 引导 browser 探针", "browser" in a_spa.web_rules())

    a_el, why2 = eco.load_adapter("electron")
    check("electron adapter 可加载", a_el is not None, why2)
    if a_el is not None:
        check("electron 识别主进程源码",
              a_el.is_app_source("app.whenReady(); new BrowserWindow({})") == "electron-main")
        check("electron asgi_app=False", a_el.asgi_app() is False)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（打包含生态包 + playwright 依赖 + 加载 + 识别 + 模板 + 生成）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
