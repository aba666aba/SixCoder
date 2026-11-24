"""闭环验收编排器自检（2026-11-23，最后落地的功能）。

钉住：run_all 汇总四块能力 + 真跑自检子进程 / 全绿汇总 ok=True /
任一失败汇总 ok=False（fail-closed） / 非法 check 名拒绝 / 脚本不存在拒绝 /
人审声明非空且不谎报机判审美 / capability 诚实。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:200]}" if detail and not ok else ""))


def main() -> int:
    from core import verify as v

    try:
        print("[1] 能力声明")
        d = v.describe()
        check("覆盖五类验收", len(d["verifies"]) == 5, str(d["verifies"]))
        check("fail-closed", d["fail_closed"] is True)
        check("不谎报机判审美", d["aesthetic_auto_verified"] is False)

        print("[2] 四类能力验收真跑")
        r = v.run_all()
        check("run_all 返回 ok", r["ok"] is True, str(r.get("items")))
        check("4 项全过", r["passed"] == 4 and r["total"] == 4, f"{r['passed']}/{r['total']}")
        names = {i["name"] for i in r["items"]}
        check("四块都在单里", {"style", "human_once", "host_plugin", "judgment"} <= names, str(names))
        check("人审声明非空", len(r["human_review"]) >= 3, str(r["human_review"]))
        check("注明不谎报", "不谎报全自动" in r["note"])

        print("[3] 指定风格/来源/适配器仍可跑")
        r2 = v.run_all(pid="minimal-clean", adapter_id="vscode", source_id="github_api")
        check("指定参数全过", r2["ok"] is True, str(r2.get("items")))
        style = next(i for i in r2["items"] if i["name"] == "style")
        check("风格约定含所选", "极简清爽" in style["evidence"], style["evidence"][:120])

        print("[4] 真跑自检子进程")
        r3 = v.run_all(checks=["check_uistyles.py"])
        chk = next(i for i in r3["items"] if i["name"] == "check:check_uistyles.py")
        check("子进程真跑通过", chk["ok"] is True and "rc=0" in chk["detail"], chk["detail"])
        check("带运行耗时", "s)" in chk["detail"], chk["detail"])

        print("[5] fail-closed：非法/不存在的 check")
        bad1 = v.verify_check("../evil.py")
        check("非法名拒绝", bad1["ok"] is False, bad1["detail"])
        bad2 = v.verify_check("check_no_such_thing.py")
        check("不存在的脚本拒绝", bad2["ok"] is False, bad2["detail"])
        r4 = v.run_all(checks=["check_no_such_thing.py"])
        check("一项失败则汇总红", r4["ok"] is False, f"{r4['passed']}/{r4['total']}")

        print("[6] 未知风格 id 不误判全绿")
        r5 = v.run_all(pid="__no_such_style__")
        style5 = next(i for i in r5["items"] if i["name"] == "style")
        check("未知风格判红", style5["ok"] is False, style5["detail"])

        print("[7] 样式验收真跑浏览器读 CSS（B-5）")
        from core import uistyles as _u
        html = _u.demo_html(colors={"primary": "#ff0000"})
        vs = _u.css_vars({"primary": "#ff0000"})
        css_ok, css_why = v._browser_css_check(html, [
            {"selector": ":root", "style": vs},
            {"selector": "main#ui-root", "visible": True}])
        check("真浏览器读到用户填的 CSS 变量", css_ok, css_why)
        # 反例：颜色不符必须判红，证明不是"能打开就绿"的空壳
        bad_ok, _ = v._browser_css_check(
            _u.demo_html(colors={"primary": "#ff0000"}),
            [{"selector": ":root", "style": {"--ui-primary": "#00ff00"}}])
        check("颜色不符判红（非空壳）", bad_ok is False)

    except Exception as exc:                             # noqa: BLE001
        check("自检未抛异常", False, f"{type(exc).__name__}: {exc}")

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
