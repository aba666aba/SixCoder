"""启动探针自检：health 必须快（2026-11-20 真实故障）。

用户装完打开只有背景：外壳 800ms 探针打 `/v1/health`，但 health 里串行查了多次
Ollama（没启动时每次等 2s 超时），单次 health 要 10s → 60 次探针全部超时 →
界面永远停在"后端未就绪"。

钉住：
  1. `/v1/health` 不查模型列表（不调用 ollama.models / 不拖超时）；
  2. health 在 Ollama 不存在时 <1s 返回，且 app=sixcoder；
  3. 模型列表拆到 `/v1/models`，界面加载后异步补；
  4. 源码里 health 不含 `ollama.models()`。

跑法: python checks/check_health_fast.py
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
APP = PROTO / "web" / "src" / "App.vue"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {str(detail)[:220]}" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_health", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()
    src = (PROTO / "server.py").read_text(encoding="utf-8")
    seg = src[src.index("def health("):src.index("def _model_policy_view(")]

    print("[1] health 不查模型列表")
    ck("health 段没有 ollama.models()", "ollama.models()" not in seg)
    ck("health 段没有 pick_default()", "pick_default()" not in seg)
    ck("health 用短超时 alive", "alive(timeout=0.3)" in seg)

    print("[2] health 在 Ollama 不可用时也要 <1s")
    real_alive = srv.ollama.alive
    real_models = srv.ollama.models

    def slow_alive(timeout: float = 2.0) -> bool:
        time.sleep(0.05)                                  # 模拟 localhost 快速拒绝
        return False

    def boom_models(*a, **k):
        raise AssertionError("health 不该调用 models()")

    srv.ollama.alive = slow_alive
    srv.ollama.models = boom_models
    try:
        t0 = time.time()
        r = srv.health()
        dt = time.time() - t0
        ck("health 返回 ok", r.get("ok") is True, r)
        ck("health 带 app 标识", r.get("app") == "sixcoder", r.get("app"))
        ck("health 不抛异常", True)
        ck("health <1s", dt < 1.0, f"{dt:.3f}s")
        ck("health 的 models 为空（由 /v1/models 补）", r.get("models") == [], r.get("models"))
    finally:
        srv.ollama.alive = real_alive
        srv.ollama.models = real_models

    print("[3] 模型列表仍有独立接口")
    paths = {getattr(x, "path", "") for x in srv.app.routes}
    ck("注册 /v1/models", "/v1/models" in paths, sorted(paths)[-8:])

    print("[4] 界面异步补模型列表")
    app = APP.read_text(encoding="utf-8")
    ck("loadHealth 调 loadModels", "loadModels()" in app)
    ck("loadModels 请求 /v1/models", "api('/v1/models')" in app)
    ck("loadHealth 不 await 模型列表", "await loadModels()" not in app)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
