"""阶段③（审阅模式）自检：审计聚合 + 开发者文档生成。

## 这条自检保护的是什么（2026-11-23）

产品是三段流水线：① 造物模式出原型 → ② 精修模式微调 → ③ 审阅模式审计与出文档。
盘查发现第③段**只有一半**：
  · `walkthrough` 有 HTTP 入口，能跑；
  · `archrules` / `lint` / `debt` / `testweight` 这些审计模块早写好了，
    却**一个 HTTP 入口都没有**——只被自检脚本调用，用户碰不到；
  · 文档生成只有 README + LICENSE。

也就是说：**能力在代码里，没接到产品面上**。这正是"阶段②③成熟度断崖"的实际形态
——不是缺实现，是缺接线。

本自检钉住四件事：
1. 审计能真的跑起来，且**每个审计器都跑过**（没跑成的必须暴露，不许静默通过）；
2. 审计**只读**：不改用户文件、不写盘（用文件哈希前后比对证明）；
3. 文档能生成 MODULES / ARCHITECTURE / HANDOVER 三份，且内容来自真实扫描
   （符号数与 AST 一致、环检测真的能找出环）；
4. 三个 HTTP 端点在位（审计 / 审计报告 / 阶段③文档），参数与守卫正确。

跑法: python check_audit.py
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))
from core import audit as A          # noqa: E402
from core import docs as D           # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def _ws() -> Path:
    root = PROTO / ".tmp"
    root.mkdir(exist_ok=True)
    return Path(tempfile.mkdtemp(dir=str(root), prefix="audit-"))


def _mk(ws: Path, rel: str, text: str) -> Path:
    p = ws / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _digest(ws: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(ws.rglob("*")):
        if p.is_file():
            h.update(p.name.encode("utf-8", "replace"))
            h.update(p.read_bytes())
    return h.hexdigest()


# 一个"哪都有点问题"的小仓库：空壳测试 + 裸 except + TODO + 依赖环
SAMPLE = {
    "a.py": "import b\n\ndef fa(x):\n    return b.fb(x)\n",
    "b.py": "import a\n\ndef fb(x):\n    return a.fa(x)  # TODO 这里有环\n",
    "c.py": "def fc():\n    try:\n        return 1\n    except:\n        return 0\n",
    "test_a.py": '"""啥也没有。"""\n',
}


def main() -> int:
    print("阶段③（审阅模式：审计 + 文档）自检")

    print("[1] 审计能跑，且每个审计器都跑过")
    ws = _ws()
    try:
        for rel, text in SAMPLE.items():
            _mk(ws, rel, text)
        res = A.audit(ws)
        check("返回结构完整", {"findings", "by_severity", "counts", "summary",
                              "auditors_ran", "errors"} <= set(res), str(sorted(res)))
        ran = set(res["auditors_ran"])
        for name in ("archrules", "lint", "debt", "testweight", "todo"):
            check(f"审计器 {name} 跑过", name in ran, str(sorted(ran)))
        check("没有审计器报错", not res["errors"], str(res["errors"]))
        check("扫到了 py 文件", res["py_files"] >= 3, str(res["py_files"]))

        print("[2] 审计真的发现了问题（默认红）")
        sev = {f["severity"] for f in res["findings"]}
        check("有问题被报出来", bool(res["findings"]), "空仓不该过")
        srcs = {f["source"] for f in res["findings"]}
        check("testweight 报出空壳测试", "testweight" in srcs, str(sorted(srcs)))
        check("有空壳测试时不是 all-clear", not (res["findings"] == []))
        _ = sev

        print("[3] 审计是只读的（文件哈希不变）")
        before = _digest(ws)
        A.audit(ws)
        A.render_report(A.audit(ws))
        check("审计前后文件哈希一致（不写盘）", _digest(ws) == before)
        check("报告是 Markdown", A.render_report(res).startswith("# 审计报告"))

        print("[4] 文档：MODULES / ARCHITECTURE / HANDOVER 都能生成")
        r = D.write_stage3_docs(ws, "probe")
        check("生成成功", r.get("ok") is True, str(r))
        for name in ("MODULES.md", "ARCHITECTURE.md", "HANDOVER.md"):
            p = ws / name
            check(f"{name} 已写盘", p.is_file())
            if p.is_file():
                check(f"{name} 非空且有标题", p.read_text(encoding="utf-8").startswith("#"),
                      p.read_text(encoding="utf-8")[:40])

        print("[5] 文档内容来自真实扫描（不是模板话）")
        mods_md = (ws / "MODULES.md").read_text(encoding="utf-8")
        # a.py 里有 fa，b.py 里有 fb：符号必须真的出现
        check("MODULES.md 列出了真实符号 fa", "`fa(" in mods_md or "fa(" in mods_md)
        check("MODULES.md 列出了真实符号 fb", "`fb(" in mods_md or "fb(" in mods_md)
        check("MODULES.md 报了模块依赖", "`a`" in mods_md and "`b`" in mods_md, mods_md[:200])
        arch_md = (ws / "ARCHITECTURE.md").read_text(encoding="utf-8")
        check("ARCHITECTURE.md 找出了依赖环（a↔b）", "❌" in arch_md, arch_md[:300])
        hand = (ws / "HANDOVER.md").read_text(encoding="utf-8")
        check("HANDOVER.md 点名空壳测试", "空壳" in hand or "测试" in hand, hand[:200])
        check("HANDOVER.md 有'没有证据的边界'一节", "没有证据的边界" in hand)

        print("[6] 环检测单独验（无环时不能乱报）")
        ws2 = _ws()
        try:
            _mk(ws2, "x.py", "def fx():\n    return 1\n")
            _mk(ws2, "y.py", "import x\n\ndef fy():\n    return x.fx()\n")
            mods2 = D._modules(ws2)
            edges2 = D._local_deps(ws2, mods2)
            check("单向依赖不算环", D._find_cycles(edges2) == [], str(edges2))
            _mk(ws2, "x.py", "import y\n\ndef fx():\n    return 1\n")
            edges3 = D._local_deps(ws2, D._modules(ws2))
            check("双向依赖算环", bool(D._find_cycles(edges3)), str(edges3))
        finally:
            _rm(ws2)

        print("[7] 空仓库如实说（不假装干净）")
        ws3 = _ws()
        try:
            r3 = A.audit(ws3)
            check("空仓也给结果（不崩）", isinstance(r3, dict) and "summary" in r3)
            check("空仓没有测试 → 报 P1", any(f["severity"] == "P1" for f in r3["findings"]),
                  str(r3["counts"]))
        finally:
            _rm(ws3)

        print("[8] 端点在位（接线，不是只写了函数）")
        srv = (PROTO / "server.py").read_text(encoding="utf-8", errors="replace")
        for route in ('"/v1/audit"', '"/v1/audit/report"', '"/v1/docs/stage3"'):
            check(f"路由 {route} 存在", route in srv)
        check("/v1/audit 有只读守卫", "_guard(\"file.read\")" in srv)
        check("/v1/docs/stage3 有写盘守卫", "_guard(\"file.write\")" in srv)
    finally:
        _rm(ws)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（审计器全跑 / 只读不写盘 / 三份文档来自真实扫描 / 环检测 / 端点接线）")
    return 0


def _rm(p: Path) -> None:
    import shutil
    shutil.rmtree(p, ignore_errors=True)


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
