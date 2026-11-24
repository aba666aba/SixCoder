"""闭源宿主插件适配清单自检（2026-11-23）。

钉住：常用办公软件适配器齐全 / 每个都有公开文档链接 + 可验判据 + 人在环标注 /
文档 URL 真实指向官方文档域名 / 不谎报全自动 / probe_suggestion 只生成约定不执行 /
人在环清单诚实（premiere/wps）/ 只读副本不被外部改坏。
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
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:160]}" if detail and not ok else ""))


OFFICIAL_DOMAINS = ("learn.microsoft.com", "code.visualstudio.com",
                    "help.libreoffice.org", "api.libreoffice.org",
                    "developer.adobe.com", "solution.wps.cn")


def main() -> int:
    from core import host_plugins as hp

    try:
        print("[1] 适配器清单")
        d = hp.describe()
        check("5 个适配器", d["adapter_count"] == 5, str(d["adapter_count"]))
        check("不谎报全自动", d["claim_full_auto"] is False)
        check("有可机械验的", d["verifiable_count"] >= 3, str(d["verifiable_count"]))
        ids = {a["id"] for a in hp.list_adapters()}
        check("常用软件齐全", {"libreoffice", "ms_office", "vscode", "premiere", "wps"} <= ids, str(ids))

        print("[2] 每个适配器字段完整")
        for a in hp.list_adapters():
            aid = a["id"]
            check(f"{aid} 有桥接方式", bool(a["bridge"]))
            check(f"{aid} 有公开文档", len(a["docs"]) >= 1)
            check(f"{aid} 有可验判据", len(a["can_verify"]) >= 1)
            check(f"{aid} 有不可验声明", isinstance(a["cannot_verify"], list))
            check(f"{aid} 有判据归属", a["verdict_source"] in
                  ("artifact", "extension_command", "human"))
            check(f"{aid} 有探针建议", bool(a["probe"]))

        print("[3] 文档链接指向官方域名")
        for a in hp.list_adapters():
            ok = any(u.startswith("https://") and any(dom in u for dom in OFFICIAL_DOMAINS)
                     for u in a["docs"])
            check(f"{a['id']} 官方文档域名", ok, str(a["docs"]))

        print("[4] 人在环诚实标注")
        hr = {a["id"] for a in hp.human_required()}
        check("premiere 标人在环（剪辑质量）", "premiere" in hr)
        check("wps 标人在环（宏兼容）", "wps" in hr)
        check("libreoffice/vscode 可机械验",
              "libreoffice" not in hp.describe()["human_required"]
              and "vscode" not in hp.describe()["human_required"])

        print("[5] probe_suggestion 只生成约定不执行")
        sug = hp.probe_suggestion("libreoffice", "C:/tmp/demo")
        check("生成成功", sug["ok"] is True)
        check("含桥接说明", "UNO" in sug["contract"])
        check("含可验判据", "headless 转换产物" in sug["contract"])
        check("含文档链接", "https://" in sug["contract"])
        check("带 workdir", sug["workdir"] == "C:/tmp/demo")
        check("不可验部分被列出", "文档排版是否美观" in sug["contract"])  # 诚实列出人判部分
        bad = hp.probe_suggestion("nonexistent")
        check("未知适配器 fail-closed", bad["ok"] is False)

        print("[6] 只读副本不被改坏")
        lst = hp.list_adapters()
        lst[0]["label"] = "TAMPERED"
        check("外部改副本不影响常量", hp.get("libreoffice")["label"] != "TAMPERED")
        got = hp.get("MS_Office")
        check("get 大小写不敏感", got is not None and got["id"] == "ms_office")

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
