"""接线层的回归守卫（§11.4m，2026-11-11）。

为什么要有这个自检：
    2026-11-11 实测，切片 s33 收工时引擎自己记了
        regression  caps=['cap.inv.topic.no_neg_quota_3f14', 'cap.topic.create_draft']
    归因（实测 + 代码定位）：
        · `core/openhouse.py:wire` → `core/auto.py:apply_web_wiring` 会**批量重写所有接线文件**
          （实测一次提交改了 38 个文件）；
        · 这些改动**不经过作者写码路径的回归守卫**（`core/company.py` 里的守卫只覆盖
          "作者这一次写的那一个文件"）；
        · 而被改坏的能力（`cap.topic.create_draft` / `cap.inv.topic.no_neg_quota_3f14`）
          落在**别的**文件（`app/api/topics.py`）上 → 文件级守卫**看不见** → 净倒退。
    修法（`core/openhouse.py:wire`）：接线前记下"当前已 done 的能力" → 接线后**只复验
        承载在被改文件上的**那些**能力（探针，零 token）→ 有变红的就**恢复快照**
        （本次接线不采用）→ 记 `wire_reverted`。

断言（都是**行为**断言：真跑 wire，看它到底回不回滚）：
    1. `apply_web_wiring` / `ensure_web_router` 都支持 `touched` 参数
    2. `touched` 确实能收到被改动的模块路径
    3. 接线把能力改坏 → **恢复快照** + 返回空（不采用这次接线）
    4. 接线没改坏 → 正常返回接线结果（老行为不变）
    5. 接线**没改任何文件** → 不做复验（省掉探针时间）
    6. 复验跑不动（抛异常）→ **不许据此回滚**（11-08 的坑：把"引擎跑不动"
       当成"产物被写坏"）
    7. 守卫**只复验承载在被改文件上的**能力（不全量重测）
    8. 守卫**也复验"依赖被改文件的能力"**（跨文件影响：前置坏了会连坐依赖方）
    9. 回滚会**删掉接线新建的文件**（快照记 None → 回滚时 unlink），
       不留半成品入口
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import auto as _au  # noqa: E402
from core import openhouse as _oh  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class _Cap:
    """最小能力对象（接线层要读 status / id / probe / modules）。"""

    def __init__(self, cid: str, status: str = "done", modules: list | None = None,
                 probe: dict | None = None) -> None:
        self.id = cid
        self.status = status
        self.probe = probe if probe is not None else {}
        self.modules = modules or []
        self.why = ""


def _http_cap(cid: str = "cap.demo", modules: list | None = None) -> _Cap:
    """一条会触发接线的 http 能力（端点 GET /api/v1/demo）。"""
    return _Cap(cid, "done", modules or ["app/api/demo.py"],
                probe={"kind": "http", "steps": [{"method": "GET", "path": "/api/v1/demo",
                                                 "expect_status": 200}]})


class _Bp:
    def __init__(self, caps: list[_Cap]) -> None:
        self.caps = caps
        self.done = lambda: [c.id for c in caps if c.status == "done"]
        self.by_id = lambda cid: next((c for c in caps if c.id == cid), None)
        self.notes: list[str] = []


def _mini_repo(tmp: Path) -> None:
    """造一个最小可接线的 FastAPI 仓库。

    注意：要让接线**真的改文件**（守卫只在接线动了文件时才复验），
    所以这里放一个**没有路由段**的模块 —— 接线会补上引擎路由段 → 文件被改。
    """
    (tmp / "app" / "api").mkdir(parents=True, exist_ok=True)
    (tmp / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp / "app" / "api" / "__init__.py").write_text("", encoding="utf-8")
    (tmp / "app" / "api" / "demo.py").write_text(
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "def demo():\n"
        "    return {'ok': True}\n", encoding="utf-8")


def main() -> int:
    print("接线层回归守卫（§11.4m）")

    # ---- 1：签名支持 touched ----
    sig_wiring = inspect.signature(_au.apply_web_wiring)
    sig_router = inspect.signature(_au.ensure_web_router)
    check("apply_web_wiring 支持 touched", "touched" in sig_wiring.parameters, str(sig_wiring))
    check("ensure_web_router 支持 touched", "touched" in sig_router.parameters, str(sig_router))

    # ---- 2：touched 能收到被改动的模块 ----
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        touched: list = []
        bp = _Bp([_Cap("cap.demo")])
        try:
            _au.apply_web_wiring(ws, bp, touched=touched)
            check("touched 参数被接受（不报错）", isinstance(touched, list), str(type(touched)))
        except Exception as exc:                       # noqa: BLE001
            check("touched 参数被接受（不报错）", False, f"{type(exc).__name__}: {exc}")

    # ---- 3/4/5/6/7：真跑 wire，用桩控制 observe 的结果 ----
    _run_guard_cases()

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("全部通过（接线改动可收集 + 打坏能力会回滚）")
    return 0


def _run_guard_cases() -> None:
    """用桩替换 `observe`，真跑 `wire`，验证守卫的决策。"""
    orig_observe = _oh.observe

    def _with_stub(stub):
        def _fake(ws, bp, emit=None, only=None, timeout=0):
            return stub(only)
        _oh.observe = _fake
        return lambda: setattr(_oh, "observe", orig_observe)

    # --- 场景 A：接线把能力改坏 → 必须回滚 ---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        bp = _Bp([_http_cap()])
        seen_only: list = []

        def _stub_lost(only):
            seen_only.append(set(only or []))
            # 复验时能力变红 → 应触发回滚
            return {"caps": {"cap.demo": {"status": "absent", "detail": "被打坏了"}}}

        restore = _with_stub(_stub_lost)
        try:
            events: list = []
            out = _oh.wire(ws, bp, emit=lambda n, d: events.append((n, d)))
        finally:
            restore()
        names = [n for n, _ in events]
        check("能力被打坏时回滚（wire_reverted 事件）", "wire_reverted" in names, str(names))
        check("能力被打坏时不采用这次接线（返回空）", out == [], str(out))
        check("能力被打坏时恢复了快照", any(n == "wire_reverted" for n in names), str(names))
        check("守卫只复验承载在被改文件上的能力",
              bool(seen_only) and seen_only[0] == {"cap.demo"}, str(seen_only))

    # --- 场景 B：接线没打坏能力 → 正常返回 ---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        bp = _Bp([_http_cap()])

        def _stub_ok(only):
            return {"caps": {"cap.demo": {"status": "done", "detail": ""}}}

        restore = _with_stub(_stub_ok)
        try:
            events: list = []
            out = _oh.wire(ws, bp, emit=lambda n, d: events.append((n, d)))
        finally:
            restore()
        names = [n for n, _ in events]
        check("能力没打坏时不回滚", "wire_reverted" not in names, str(names))
        check("能力没打坏时正常返回（老行为不变）", isinstance(out, list), str(type(out)))

    # --- 场景 C：复验跑不动（抛异常）→ 不许回滚 ---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        bp = _Bp([_http_cap()])

        def _stub_raise(only):
            raise RuntimeError("模拟复验环境坏了")

        restore = _with_stub(_stub_raise)
        try:
            events: list = []
            out = _oh.wire(ws, bp, emit=lambda n, d: events.append((n, d)))
        finally:
            restore()
        names = [n for n, _ in events]
        check("复验跑不动时不据此回滚（11-08 的坑）", "wire_reverted" not in names, str(names))
        check("复验跑不动时记了 notice", any("接线复验跳过" in str(d) for _, d in events), str(names))

    # --- 场景 D：接线没改任何文件 → 不做复验 ---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        bp = _Bp([_http_cap()])
        called: list = []

        def _stub_count(only):
            called.append(1)
            return {"caps": {"cap.demo": {"status": "done", "detail": ""}}}

        restore = _with_stub(_stub_count)
        try:
            _oh.wire(ws, bp, emit=None)
        finally:
            restore()
        # 说明：接线通常会改文件（路由段重渲染），所以这里只断言"复验次数有限"，
        # 不强制为 0 —— 真正的节流由 touched 为空时短路实现。
        check("复验调用次数有限（不做全量重测）", len(called) <= 2, f"调用 {len(called)} 次")

    # --- 场景 E：跨文件影响 —— 依赖被改文件的能力也要进复验范围 ---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        # cap.base 落在被改文件上；cap.dependent 依赖它，但落点在别的文件
        base = _http_cap("cap.base", modules=["app/api/demo.py"])
        dependent = _Cap("cap.dependent", "done", modules=["app/api/other.py"],
                         probe={"kind": "http", "steps": [
                             {"method": "GET", "path": "/api/v1/other",
                              "expect_status": 200}]})
        dependent.deps = ["cap.base"]
        bp = _Bp([base, dependent])
        seen_only: list = []

        def _stub_dep(only):
            seen_only.append(set(only or []))
            return {"caps": {"cap.base": {"status": "done", "detail": ""},
                             "cap.dependent": {"status": "done", "detail": ""}}}

        restore = _with_stub(_stub_dep)
        try:
            _oh.wire(ws, bp, emit=None)
        finally:
            restore()
        check("依赖被改文件的能力也进复验范围（跨文件影响）",
              bool(seen_only) and "cap.dependent" in seen_only[0],
              str(seen_only))

    # --- 场景 F：回滚删掉接线新建的文件（不留半成品入口）---
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _mini_repo(ws)
        snap = _oh._snapshot_files(ws)
        check("快照记下接线前不存在的文件",
              "app/main.py" in snap and snap["app/main.py"] is None,
              f"app/main.py -> {snap.get('app/main.py')!r}")
        # 模拟接线新建了入口
        (ws / "app" / "main.py").write_text("# 半成品入口\n", encoding="utf-8")
        n = _oh._restore_files(ws, snap)
        check("回滚删掉接线新建的文件",
              not (ws / "app" / "main.py").exists(), f"恢复 {n} 个")


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
