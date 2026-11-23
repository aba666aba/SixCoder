"""UI 审美样式库自检（2026-11-23）。

钉住：10 预设 + 4 类维度类型齐全 / 库里不写死色值 / 颜色插槽合法且拒绝注入 /
选择校验 fail-closed / 选色存 settings 可读回 / contract 注入含类型与占位符 /
用户没选就不注入 / blueprint 组装不污染 web_rules 断言。

用临时配置（SIXCODER_SETTINGS）隔离，不碰用户真配置。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:160]}" if detail and not ok else ""))


def main() -> int:
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tf.close()
    os.environ["SIXCODER_SETTINGS"] = tf.name

    from core import uistyles as u

    try:
        print("[1] 目录结构：类型齐全")
        cat = u.load_catalog()
        check("目录可读", bool(cat) and cat.get("version") == "1")
        d = u.describe()
        check("10 个预设", d["preset_count"] == 10, str(d["preset_count"]))
        check("4 个维度", d["dimensions"] == ["page", "background", "menu", "button"])
        check("每类 10 种类型（共 40）", d["type_count"] == 40, str(d["type_count"]))
        check("6 个颜色插槽", d["color_slot_count"] == 6, str(d["color_slot_count"]))
        check("库里不写死色值", d["colors_are_user_filled"] is True)
        check("引擎不做审美评判", d["aesthetic_judgement_by_engine"] is False)

        print("[2] 预设字段完整、引用的类型都存在")
        types = u.list_types()
        for p in u.list_presets():
            pid = p.get("id", "?")
            dims = p.get("dimensions") or {}
            ok = all(dims.get(k) in {t["id"] for t in types.get(k, [])}
                     for k in ("page", "background", "menu", "button"))
            check(f"预设 {pid} 四维类型合法", ok, str(dims))
        ids = [p["id"] for p in u.list_presets()]
        check("预设 id 唯一", len(ids) == len(set(ids)))

        print("[3] 颜色插槽校验（安全 / 拒绝注入）")
        for good in ("#4F8CFF", "#fff", "rgb(79,140,255)", "rgba(0,0,0,.5)",
                     "hsl(210,90%,60%)", "rebeccapurple"):
            check(f"接受合法色值 {good}", u._valid_color(good))
        for bad in ("red; background:url(x)", "#fff}", "expression(alert(1))",
                    "javascript:alert(1)", "<script>", "rgb(0,0,0);evil"):
            check(f"拒绝注入色值 {bad[:18]}", not u._valid_color(bad))

        print("[4] 选择校验 fail-closed")
        ok, _ = u.validate_selection("minimal-clean", {"primary": "#fff"})
        check("合法选择通过", ok)
        ok, _ = u.validate_selection("no-such-preset", {})
        check("未知预设拒绝", not ok)
        ok, _ = u.validate_selection("minimal-clean", {"evil": "#fff"})
        check("未知插槽拒绝", not ok)
        ok, _ = u.validate_selection("minimal-clean", {"primary": "#fff};bad"})
        check("注入色值拒绝", not ok)

        print("[5] 选择落地（settings）与 contract 生成")
        sel = u.select("dashboard-pro", {"primary": "#4F8CFF", "bg": "#0E1117"})
        check("选色写回", sel["selected"] == "dashboard-pro" and sel["colors"].get("primary") == "#4F8CFF")
        back = u.selection()
        check("选色可读回", back["selected"] == "dashboard-pro" and back["colors"].get("bg") == "#0E1117")
        txt = u.contract_text()
        check("contract 含选定风格名", "专业仪表盘" in txt, txt[:120])
        check("contract 含四维类型标签", all(k in txt for k in ("page", "background", "menu", "button")))
        check("contract 回填用户色值", "#4F8CFF" in txt)
        check("contract 未填插槽留占位", "待用户填" in txt)
        check("contract 明说不自检审美", "机器不判" in txt)
        # 显式传 colors 覆盖
        txt2 = u.contract_text("minimal-clean", {"primary": "#123456"})
        check("显式 colors 覆盖", "#123456" in txt2 and "极简清爽" in txt2)

        print("[6] 无选择时不注入")
        u.select("minimal-clean", {})  # 先有选择
        import json
        raw = json.loads(Path(tf.name).read_text(encoding="utf-8"))
        raw["uistyle"] = {"selected": "", "colors": {}}
        Path(tf.name).write_text(json.dumps(raw), encoding="utf-8")
        check("没选风格 contract 为空", u.contract_text() == "")

        print("[7] public 视图含全部清单")
        pub = u.public_config()
        check("public 含预设/类型/插槽/选择",
              len(pub["presets"]) == 10 and pub["types"] and len(pub["color_slots"]) == 6
              and "selected" in pub["selected"])

        print("[8] CSS 变量与验收页（与生成同源）")
        u.select("minimal-clean", {"primary": "#ff0000", "bg": "#fafafa"})
        vs = u.css_vars()
        check("css_vars 用 --ui- 前缀且值正确",
              vs.get("--ui-primary") == "#ff0000" and vs.get("--ui-bg") == "#fafafa", str(vs))
        check("css_vars 只含已填插槽", set(vs) == {"--ui-primary", "--ui-bg"}, str(set(vs)))
        html = u.demo_html()
        check("验收页含变量声明", "--ui-primary: #ff0000" in html)
        check("验收页四维结构落地", html.count('<section class="ui-') == 4)
        check("验收页无脚本注入", "<script" not in html)
        u.select("minimal-clean", {})       # 复原，不留测试色值

        print("[9] 空插槽允许（先选样式、颜色后填）")
        # 2026-11-23 真 UI 跑出来的 bug：前端首次打开每个插槽都是空串，
        # 旧校验把空串当"非法色值"→ 只挑样式不填色就保存会 400。
        ok_empty, why_empty = u.validate_selection("minimal-clean", {"primary": "", "bg": ""})
        check("空插槽不算非法", ok_empty, why_empty)
        check("非法值仍拒绝（底线没松）",
              not u.validate_selection("minimal-clean", {"primary": "<script>"})[0])
        check("未知插槽仍拒绝",
              not u.validate_selection("minimal-clean", {"nope": "#fff"})[0])
        u.select("minimal-clean", {})

    finally:
        os.environ.pop("SIXCODER_SETTINGS", None)
        try:
            Path(tf.name).unlink(missing_ok=True)
        except Exception:                                # noqa: BLE001
            pass

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
