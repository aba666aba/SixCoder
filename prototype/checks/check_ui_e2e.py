"""前端 UI 端到端自检（2026-11-23）：真起后端 + 真浏览器点按钮。

为什么需要：此前只核验了"接口字段对齐"，**没点过按钮**——本轮第一次真跑就抓到
一个真 bug（空颜色插槽被当成非法值，一进面板点保存就 400）。这个自检把三面板的
关键路径钉住：

  · 切 Tab 能渲染出对应面板（不是空白/错位）；
  · UI 审美：选预设 → 保存 → **真的写进 settings**；
  · 半自动：填值 → 存入 → 列表可见且**值不回显**；
  · 能力边界：插件适配与判据开放两份清单都在。

用临时 settings（不污染本机配置）、随机端口、headless 系统浏览器；跑完关服务。
浏览器不可用时如实 SKIP（不算通过），不假装验证过。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" :: {detail[:200]}" if detail and not ok else ""))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, sec: float = 45.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < sec:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.35)
    return False


def main() -> int:
    print("前端 UI 端到端（真起后端 / 真浏览器 / 真点按钮）")
    try:
        from core import browser as br
    except Exception as exc:                             # noqa: BLE001
        print(f"SKIP：导入浏览器模块失败（{type(exc).__name__}）")
        return 0
    avail, why = br.available()
    if not avail:
        print(f"SKIP：没有可用浏览器（{why}）——不算通过，只是没覆盖")
        return 0
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:                             # noqa: BLE001
        print(f"SKIP：playwright 不可用（{type(exc).__name__}）")
        return 0

    port = _free_port()
    tmp = Path(tempfile.mkdtemp(prefix="uie2e-")) / "settings.json"
    tmp.write_text("{}", encoding="utf-8")
    env = {**os.environ, "SIXCODER_SETTINGS": str(tmp)}
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "server.py"), "--port", str(port)],
        cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, env=env)
    try:
        if not _wait_port(port):
            check("后端启动", False, "45s 内没起来")
            return 1
        check("后端启动", True)

        ch, _ = br._try_channels()
        with sync_playwright() as pw:
            kw = {"headless": True}
            if ch:
                kw["channel"] = ch
            b = pw.chromium.launch(**kw)
            pg = b.new_page()
            net: list[tuple[int, str]] = []
            pg.on("response", lambda r: net.append((r.status, r.url))
                  if "/v1/" in r.url else None)
            pg.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle", timeout=30000)

            pg.click("text=帮助")
            pg.wait_for_timeout(600)
            tabs = pg.locator(".tab").all_inner_texts()
            for name in ("UI 审美", "半自动回填", "多机编排", "能力边界"):
                check(f"Tab 存在：{name}", name in tabs, str(tabs))

            # ① UI 审美：选预设 → 保存 → 真落盘
            pg.click(".tab:has-text('UI 审美')")
            pg.wait_for_timeout(600)
            pg.locator("div.panel[style*='cursor']").nth(0).click()
            pg.wait_for_timeout(250)
            pg.click("text=保存风格并用于生成")
            pg.wait_for_timeout(900)
            us = (json.loads(tmp.read_text(encoding="utf-8")) or {}).get("uistyle") or {}
            check("UI 审美保存写入 settings", bool(us.get("selected")), str(us))

            # ② 半自动：填值 → 存入 → 列表可见且不回显
            pg.click(".tab:has-text('半自动回填')")
            pg.wait_for_timeout(600)
            pg.locator("input[placeholder*='命名空间']").fill("__ui__")
            pg.locator("input[placeholder*='键']").fill("otp")
            pg.locator("input[placeholder*='值']").fill("987654")
            pg.click("button:has-text('存入')")
            pg.wait_for_timeout(900)
            body = pg.locator("body").inner_text()
            check("半自动存入后列表可见", "__ui__" in body and "otp" in body)
            check("值不回显（安全红线）", "987654" not in body)

            # ③ 能力边界：两份清单都在
            pg.click(".tab:has-text('能力边界')")
            pg.wait_for_timeout(800)
            body2 = pg.locator("body").inner_text()
            check("插件适配清单可见", "LibreOffice" in body2 and "Premiere" in body2)
            check("判据开放清单可见", "Figma" in body2 and "GitHub" in body2)

            # ④ 部署通道：填目标 → 保存 → 真落盘（凭据只存引用）
            pg.click(".tab:has-text('部署通道')")
            pg.wait_for_timeout(600)
            pg.locator("input[placeholder*='如 prod']").fill("e2ehost")
            pg.locator("input[placeholder*='host / IP']").fill("127.0.0.1")
            pg.locator("input[placeholder*='端口']").fill("2222")
            pg.locator("input[placeholder*='用户名']").fill("deployer")
            pg.locator("select:has(option[value='password'])").select_option("password")
            pg.wait_for_timeout(300)
            pg.locator("input[placeholder*='凭据引用']").fill("env:E2E_PW")
            pg.locator("textarea[placeholder*='git pull']").fill(
                "git pull\nsystemctl restart app")
            pg.click("button:has-text('保存目标')")
            pg.wait_for_timeout(1000)
            dcfg = (json.loads(tmp.read_text(encoding="utf-8")) or {}).get("deploy") or {}
            tg = next((t for t in (dcfg.get("targets") or []) if t.get("id") == "e2ehost"), {})
            check("部署目标写入 settings", bool(tg), str(dcfg)[:150])
            check("部署目标字段正确",
                  tg.get("host") == "127.0.0.1" and tg.get("port") == 2222
                  and tg.get("user") == "deployer" and tg.get("auth") == "password",
                  str({k: tg.get(k) for k in ("host", "port", "user", "auth")}))
            check("凭据只存引用（env:）", tg.get("credential") == "env:E2E_PW",
                  repr(tg.get("credential")))
            check("命令白名单整行保存",
                  tg.get("allow_commands") == ["git pull", "systemctl restart app"],
                  str(tg.get("allow_commands")))

            # ⑤ 安全红线：明文密码不许落盘（后端静默丢弃，界面仍提示已保存）
            pg.locator("input[placeholder*='如 prod']").fill("e2eplain")
            pg.locator("input[placeholder*='host / IP']").fill("10.0.0.9")
            pg.locator("input[placeholder*='用户名']").fill("root")
            pg.locator("select:has(option[value='password'])").select_option("password")
            pg.wait_for_timeout(300)
            pg.locator("input[placeholder*='凭据引用']").fill("hunter2-plaintext")
            pg.click("button:has-text('保存目标')")
            pg.wait_for_timeout(1000)
            raw_cfg = tmp.read_text(encoding="utf-8")
            dcfg2 = (json.loads(raw_cfg) or {}).get("deploy") or {}
            pt = next((t for t in (dcfg2.get("targets") or []) if t.get("id") == "e2eplain"), {})
            check("明文密码不落盘（安全红线）",
                  "hunter2" not in raw_cfg and not pt.get("credential"),
                  repr(pt.get("credential")))
            check("明文目标仍保存（只丢凭据不丢目标）", bool(pt), str(dcfg2)[:120])

            # ⑥ 多机编排：建计划 → 保存 → 步骤/目标真落盘
            pg.click(".tab:has-text('多机编排')")
            pg.wait_for_timeout(800)
            pg.locator("input[placeholder*='计划 id']").fill("e2e_rollout")
            pg.locator("label:has-text('e2ehost') input[type='checkbox']").check()
            pg.locator("textarea[rows='4']").fill(
                "git pull || 拉最新代码\nsystemctl restart app || 重启服务")
            pg.locator("input[placeholder*='健康检查命令']").fill("systemctl is-active app")
            pg.locator("textarea[rows='2']").fill("systemctl restart app")
            pg.click("button:has-text('保存计划')")
            pg.wait_for_timeout(1000)
            ocfg = (json.loads(tmp.read_text(encoding="utf-8")) or {}).get("orchestration") or {}
            plan = next((p for p in (ocfg.get("plans") or [])
                         if p.get("id") == "e2e_rollout"), {})
            check("编排计划写入 settings", bool(plan), str(ocfg)[:150])
            check("编排目标绑定正确", plan.get("targets") == ["e2ehost"],
                  str(plan.get("targets")))
            st = plan.get("steps") or []
            check("编排步骤解析（命令+原因）",
                  len(st) == 2 and st[0].get("command") == "git pull"
                  and st[0].get("reason") == "拉最新代码",
                  str(st)[:160])
            check("编排健康检查与回滚落盘",
                  plan.get("health_check") == "systemctl is-active app"
                  and plan.get("rollback") == ["systemctl restart app"],
                  str({k: plan.get(k) for k in ("health_check", "rollback")}))

            # ⑦ 体检：真跑链路走查，出结果（不强求全绿，只要求跑出结论）
            pg.click(".tab:has-text('体检')")
            pg.wait_for_timeout(700)
            pg.click("button:has-text('跑链路走查')")
            try:
                pg.wait_for_selector("text=/通过 \\d+\\/\\d+/", timeout=45000)
                body3 = pg.locator("body").inner_text()
                check("体检走查出结论", "通过" in body3 and "链路" in body3)
            except Exception:                            # noqa: BLE001
                check("体检走查出结论", False, "45s 内没出结果")

            bad = [f"{s} {u.split('/v1/')[-1]}" for s, u in net if s >= 400]
            check("全程无 4xx/5xx 业务请求", not bad, str(bad[:4]))
            b.close()
    except Exception as exc:                             # noqa: BLE001
        check("UI 跑通未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:                                # noqa: BLE001
            proc.kill()

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
