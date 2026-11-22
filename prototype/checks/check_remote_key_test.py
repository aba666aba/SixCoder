"""远端「按 key 测试连接」自检（2026-11-21 用户：保存 key 后没有测试按钮）。

钉住：
  1. `POST /v1/settings/remote/key-test` 真挂在 app 上；
  2. 用未保存的新 key 也能测（api_key 覆盖，不落盘）；
  3. 401 时返回可读原因，不抛 500；
  4. 界面在 key 详情页有「测试连接」，保存后留在详情页（不被踢回列表）；
  5. 全局 `/remote/test` 仍然保留。

跑法: python checks/check_remote_key_test.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
APP = PROTO / "web" / "src" / "App.vue"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {str(detail)[:240]}" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_keytest", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    print("[1] 路由")
    ck("注册 /v1/settings/remote/key-test", "/v1/settings/remote/key-test" in paths)
    ck("全局 /v1/settings/remote/test 还在", "/v1/settings/remote/test" in paths)

    print("[2] 按 key 测试逻辑")
    fake_prov = {
        "id": "deepseek", "base_url": "",
        "keys": [{"id": "k1", "api_key": "sk-saved-key-1234567890",
                  "models": [{"model": "deepseek-flash"}]}],
        "models": [],
    }
    from core import remote as remote_mod
    from core import settings_store
    real_providers = settings_store.providers
    real_post = remote_mod._post_once
    seen = {}

    def fake_providers():
        return [fake_prov]

    def fake_post(payload, base, key, timeout=300):
        seen.update(payload=payload, base=base, key=key)
        return {"choices": [{"message": {"content": "好"}}]}, "", 12

    settings_store.providers = fake_providers
    remote_mod._post_once = fake_post
    try:
        r = srv.test_remote_key(srv.RemoteKeyTestReq(provider="deepseek", key_id="k1"))
        ck("保存过的 key 能测通", r.get("ok") is True, r)
        ck("用的模型是 key 下第一个", seen.get("payload", {}).get("model") == "deepseek-flash", seen)
        ck("用的是官方 DeepSeek 地址", seen.get("base") == remote_mod.DEEPSEEK_BASE, seen.get("base"))
        ck("用的是已保存 key", seen.get("key") == "sk-saved-key-1234567890", seen.get("key"))

        seen.clear()
        r2 = srv.test_remote_key(srv.RemoteKeyTestReq(
            provider="deepseek", key_id="k1", api_key="sk-unsaved-new-key-1234567890"))
        ck("未保存的新 key 优先用于测试", seen.get("key") == "sk-unsaved-new-key-1234567890", seen)

        def fail_post(*a, **k):
            return None, "鉴权失败（HTTP 401）", 9
        remote_mod._post_once = fail_post
        r3 = srv.test_remote_key(srv.RemoteKeyTestReq(provider="deepseek", key_id="k1"))
        ck("401 返回 ok=False", r3.get("ok") is False, r3)
        ck("401 给出可读原因", "401" in (r3.get("detail") or ""), r3.get("detail"))
    finally:
        settings_store.providers = real_providers
        if real_post:
            remote_mod._post_once = real_post

    print("[3] 只在厂商 key 里填 key，状态也要算已配置")
    from core import settings_store
    real_get = settings_store.get_remote
    real_any = settings_store.any_configured_key
    real_keyfn = remote_mod._key
    try:
        settings_store.get_remote = lambda: {"enabled": True, "compat": "deepseek",
                                             "provider": "deepseek", "api_key": "",
                                             "model": "deepseek-flash"}
        settings_store.any_configured_key = lambda: True
        remote_mod._key = lambda: ""
        ok, why = remote_mod.available()
        ck("厂商 key 已配置 → available=True", ok is True, why)
        ck("不再误报未配置", "未配置" not in why, why)
    finally:
        settings_store.get_remote = real_get
        settings_store.any_configured_key = real_any
        remote_mod._key = real_keyfn

    print("[4] 界面")
    app = APP.read_text(encoding="utf-8")
    for token in ("testProviderKey", "remote/key-test", "测试连接", "keyTest.detail"):
        ck("界面有 " + token, token in app)
    ck("保存后保留当前 key 详情", "keepKeyId" in app)
    ck("保存后不直接踢回列表", "find((k) => k.id === keepKeyId)" in app)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
