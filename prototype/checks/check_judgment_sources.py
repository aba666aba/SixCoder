"""判据开放调研自检（2026-11-23）。

钉住：平台清单齐全且都 verified / 每个有 https 官方文档 + 可机读接口 + 可机判据 +
仍有人判声明 / 凭据走 human_once 或本机 / 不谎报通用 / contract 生成正确 /
只读副本不被改坏。
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


def main() -> int:
    from core import judgment_sources as js

    try:
        print("[1] 清单与能力声明")
        d = js.describe()
        check("5 个来源", d["source_count"] == 5, str(d["source_count"]))
        check("全部已核实", d["verified_count"] == 5, str(d["verified_count"]))
        check("全 https 文档", d["all_docs_https"] is True)
        check("不谎报通用", d["claim_universal"] is False)
        ids = {s["id"] for s in js.list_sources()}
        check("关键平台齐全", {"figma_rest", "github_api", "playwright_cdp",
                              "vscode_ext", "office_com"} <= ids, str(ids))

        print("[2] 每个来源字段完整")
        for s in js.list_sources():
            sid = s["id"]
            check(f"{sid} 有官方文档", str(s["docs"]).startswith("https://"))
            check(f"{sid} 有可机读接口", len(s["interfaces"]) >= 1)
            check(f"{sid} 有可机判据", len(s["verdicts"]) >= 1)
            check(f"{sid} 有解锁说明", bool(s["unlocks"]))
            check(f"{sid} 有仍人判声明", bool(s["still_human"]))
            check(f"{sid} 有凭据说明", bool(s["auth"]))

        print("[3] 凭据不落盘口径")
        for s in js.list_sources():
            auth = s["auth"]
            ok = ("human_once" in auth or "env" in auth or "外部凭据" in auth
                  or "本机" in auth)
            check(f"{s['id']} 凭据不落盘", ok, auth)
        for sid in ("figma_rest", "github_api"):
            check(f"{sid} 走人填/env", "human_once" in js.get(sid)["auth"])

        print("[4] contract 生成")
        full = js.contract_text()
        check("全量 contract 含 Figma", "Figma" in full)
        check("全量 contract 含文档链接", "https://" in full)
        check("全量 contract 含仍人判", "仍需人判" in full)
        check("全量 contract 含凭据", "凭据" in full)
        one = js.contract_text("github_api")
        check("单个来源 contract 只含 GitHub", "GitHub" in one and "Figma" not in one)
        check("未知来源 contract 为空", js.contract_text("nope") == "")

        print("[5] 只读副本")
        lst = js.list_sources()
        lst[0]["platform"] = "TAMPERED"
        check("改副本不影响常量", js.get("figma_rest")["platform"] != "TAMPERED")

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
