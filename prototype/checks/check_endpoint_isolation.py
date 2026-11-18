"""端点/协议按族隔离自检（2026-11-07 根治）。

事故：用户把**全局**远端配成智谱（openai 兼容，`https://open.bigmodel.cn/...`），
而 DeepSeek 档案自己没填 base_url。旧回落链 `档案 → 全局 → 官方默认` 让
`deepseek-flash` + DeepSeek key 被发到智谱端点 → HTTP 401，错误文案还一口咬定
"Key 无效或已被轮换"，把端点错配误诊成 key 失效。

根治两处：
  1. `settings_store._norm_model_profile`：档案没写 compat 时按模型名定族，
     `deepseek-*` 一律 deepseek（不再继承全局 openai）。
  2. `remote._payload_for`：端点与 key 都按协议族隔离，别家的全局配置不兜底。

跑法: python checks/check_endpoint_isolation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remote, settings_store  # noqa: E402
from core.settings_store import SETTINGS as SETTINGS_FILE  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def endpoint_for(profile: dict, r: dict) -> tuple[dict, str, str]:
    """用真实档案跑一遍 ladder → step → payload 组装（不发请求）。"""
    step = settings_store.ladder_from(profile) if hasattr(settings_store, "ladder_from") else None
    if step is None:
        step = {"profile_id": profile["id"], "model": profile["model"],
                "effort": (profile.get("efforts") or ["none"])[0],
                "max_output_tokens": profile.get("max_output_tokens", 0),
                "temperature": profile.get("temperature", 0.2),
                "compat": profile.get("compat") or "",
                "base_url": profile.get("base_url") or "",
                "api_key": profile.get("api_key") or "",
                "pricing": profile.get("pricing") or {}}
    return remote._payload_for(step, r, [{"role": "user", "content": "hi"}], False)


def main() -> int:
    print("端点/协议按族隔离自检")
    original = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    restored = False
    try:
        # 模拟事故现场：全局是智谱（openai 兼容），DeepSeek 档案啥都没填
        r = {"compat": "openai", "model": "glm-4-flash",
             "base_url": "https://open.bigmodel.cn/api/paas/v4",
             "api_key": "sk-ZHIPU-GLOBAL-KEY", "provider": "deepseek"}
        ds = settings_store._norm_model_profile({
            "id": "flash", "model": "deepseek-flash", "api_key": "sk-DEEPSEEK-PROFILE-KEY",
            "efforts": ["none", "low", "high", "max"],
        })
        oa = settings_store._norm_model_profile({
            "id": "glm", "model": "glm-4-flash", "compat": "openai",
            "efforts": ["none"],
        })

        print("[1] 空 compat 按模型名定族")
        check("deepseek-* 档案判为 deepseek", ds["compat"] == "deepseek", str(ds["compat"]))
        check("非 DS 模型保持空（跟随全局）", oa["compat"] == "openai", str(oa["compat"]))
        check("显式 compat 不被模型名覆盖",
              settings_store._norm_model_profile(
                  {"id": "x", "model": "deepseek-x", "compat": "openai"})["compat"] == "openai")

        print("[2] DeepSeek 档案不继承别家全局端点")
        payload, base, key = endpoint_for(ds, r)
        check("端点用 DeepSeek 官方地址", base == remote.DEEPSEEK_BASE, base)
        check("不是智谱地址", "bigmodel" not in base, base)
        check("用档案自己的 key（不是全局智谱 key）",
              key == "sk-DEEPSEEK-PROFILE-KEY", key)
        check("协议未被当成 openai（保留 DS 参数格式）",
              payload.get("thinking") == {"type": "disabled"}
              and "reasoning_effort" not in payload, str(payload))

        print("[3] OpenAI 兼容档案仍继承全局端点与 key")
        p2, b2, k2 = endpoint_for(oa, r)
        check("继承全局智谱地址", b2 == r["base_url"], b2)
        check("无档案 key 时继承全局 key", k2 == "sk-ZHIPU-GLOBAL-KEY", k2)
        check("payload 走 OpenAI 兼容格式（不塞 thinking / reasoning_effort）",
              "thinking" not in p2 and "reasoning_effort" not in p2, str(p2))

        print("[4] 档案显式填的网址永远优先")
        ds_explicit = {**ds, "base_url": "https://my-proxy.example.com/v1"}
        _, b3, _ = endpoint_for(ds_explicit, r)
        check("自定义端点不被官方默认覆盖", b3 == "https://my-proxy.example.com/v1", b3)

        print("[5] 四档思考强度仍按 DS 格式逐级切换")
        for eff in ("none", "low", "high", "max"):
            step = {"profile_id": "flash", "model": "deepseek-flash", "effort": eff,
                    "compat": "deepseek", "base_url": "", "api_key": "sk-DEEPSEEK-PROFILE-KEY",
                    "max_output_tokens": 0, "temperature": 0.2}
            p, _, _ = remote._payload_for(step, r, [{"role": "user", "content": "hi"}], False)
            if eff == "none":
                check("none 档关闭思考", p.get("thinking") == {"type": "disabled"}, str(p.get("thinking")))
            else:
                check(f"{eff} 档带 reasoning_effort={eff}",
                      p.get("reasoning_effort") == eff
                      and p.get("thinking") == {"type": "enabled"}, str(p.get("reasoning_effort")))

        print("[6] 401 文案不再把端点错配误诊成 key 失效")
        class FakeErr(Exception):
            code = 401
            def read(self):
                return b""
        hint = remote.http_error_hint(FakeErr())
        check("提示核对端点/协议/key 是否同一家", "端点" in hint and "协议" in hint, hint[:120])
        check("不再一口咬定 Key 无效", "Key 无效或已被轮换" not in hint, hint[:120])
    finally:
        try:
            if original is None:
                if SETTINGS_FILE.exists():
                    SETTINGS_FILE.unlink()
            else:
                SETTINGS_FILE.write_bytes(original)
            restored = True
        except Exception as exc:                       # noqa: BLE001
            print(f"  FAIL  恢复 settings.json 失败：{type(exc).__name__}: {exc}")
    check("本机设置已无条件恢复", restored)
    check("恢复后内容与原始一致",
          (SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None) == original)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（按族定协议 / 端点与 key 不串味 / 四档格式 / 401 文案不误导）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
