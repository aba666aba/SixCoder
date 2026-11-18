"""远端模型档案 / 闲时设置自检（2026-11-07 界面重构配套）。

三个真实 bug 各留一条断言，防止它们回来：
  1. `/v1/settings/remote/models` 曾直接返回 `models()`（含**明文 api_key**），
     界面"点开看配置"会把 key 原样带出来 → 现在必须脱敏，只给 bool + 掩码。
  2. 界面曾把闲时窗口写进 `offpeak_window`（旧字段），而 `is_offpeak()` 读的是
     `peak_window` → 填了不生效，成本静默算错 → 现在必须能写进真正被读的字段。
  3. OpenAI 兼容端点不认 `reasoning_effort` 多档切换 → 界面必须把它固定成单档。

⚠️ 本检查会临时改写本机 remote 设置：开始前备份 `data/settings.json` 原始字节，
   `finally` 里**无条件恢复**；恢复失败直接判红（绝不静默丢用户配置）。
   事故记录见 `check_remote_http.py` 顶部：早先没备份，把真实 key 覆盖后找不回来了。

跑法: python checks/check_remote_profiles.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 隔离：自检不许碰用户真配置（开发文档铁律：自检必须走独立 SIXCODER_SETTINGS）。
import os as _os
import tempfile as _tf
_os.environ["SIXCODER_SETTINGS"] = str(Path(_tf.mkdtemp(prefix="sixcoder_rp_")) / "settings.json")

from core import remote, settings_store  # noqa: E402
from core.settings_store import SETTINGS as SETTINGS_FILE  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("远端模型档案 / 闲时设置自检")

    # ---------------------------------------------------------- 备份（必须无条件恢复）
    original = SETTINGS_FILE.read_bytes() if SETTINGS_FILE.exists() else None
    restored = False
    # 隔离配置里没有 key → 下面「运行期仍能读到明文 key / available()」会假红；
    # 这里放一个隔离用的假 key（只在本检查自己的临时配置里，不碰用户真配置）。
    settings_store.update_remote({"api_key": "sk-ISOLATED-TEST-KEY-1234567890"})
    try:
        # [1] 档案接口脱敏：GET 与 POST/DELETE 响应都不带明文 key
        print("[1] 模型档案端点不回显明文 key")
        from server import (get_model_profiles, upsert_model_profile, delete_model_profile,
                            ModelProfileReq, RemoteSettingsReq, set_remote_settings)
        settings_store.update_remote({"models": [
            {"id": "t-probe", "model": "probe-model", "api_key": "sk-SUPER-SECRET-42",
             "efforts": ["none"], "compat": "openai"},
            {"id": "t-keep", "model": "keep-model", "efforts": ["none"]},
        ]})
        write = upsert_model_profile(ModelProfileReq(id="t-probe", label="探针"))
        check("POST 增改响应不含明文 key", "sk-SUPER-SECRET-42" not in str(write), str(write)[:160])
        check("POST 响应不含 api_key 字段",
              all("api_key" not in p for p in write.get("models", [])), str(write)[:160])
        check("POST 响应的 ladder 不带 key",
              all("api_key" not in s for s in write.get("ladder", [])), str(write.get("ladder"))[:160])
        view = get_model_profiles()
        profs = view["models"]
        check("端点返回了档案", len(profs) >= 1, str(profs)[:120])
        blob = str(view)
        check("不含明文 api_key 字段", all("api_key" not in p for p in profs), str(profs)[:160])
        check("不含明文 key 值", "sk-SUPER-SECRET-42" not in blob, blob[:160])
        hit = next((p for p in profs if p["id"] == "t-probe"), {})
        check("给出 key_configured=True", hit.get("key_configured") is True, str(hit)[:120])
        check("给出掩码而非原文", bool(hit.get("key_masked")) and "SECRET" not in str(hit.get("key_masked")),
              str(hit.get("key_masked")))
        drop = delete_model_profile("t-probe")
        check("DELETE 响应不含明文 key", "sk-SUPER-SECRET-42" not in str(drop), str(drop)[:160])

        # 明文仍在落盘文件里、运行期仍能读到 —— 脱敏只作用于对外响应，不影响联通
        raw = settings_store.load()["remote"]["models"]
        check("落盘档案不含已删除的测试项", all(p["id"] != "t-probe" for p in raw), str(raw)[:120])
        check("运行期仍能读到全局明文 key", len(remote._key()) > 10, remote._key()[:6])
        check("available() 判定可用", remote.available()[0] is True, str(remote.available()))

        # [2] 闲时窗口：写进真正被 is_offpeak 读的字段，且判定随之变化
        print("[2] 闲时时段写进 peak_window 且真的生效")
        rep = set_remote_settings(RemoteSettingsReq(peak_window={
            "ranges": ["00:00-23:59"], "weekdays_only": False, "tz": "Asia/Shanghai"}))
        check("接口接受 peak_window", bool(rep.get("ok")), str(rep)[:120])
        saved = settings_store.get_remote().get("peak_window") or {}
        check("落盘的是 peak_window.ranges", saved.get("ranges") == ["00:00-23:59"], str(saved))
        check("全天高峰 → is_offpeak=False", remote.is_offpeak() is False, str(saved))
        # 空 ranges 收敛到默认两段，不许把配置写坏导致"全天空闲、成本静默减半"
        norm = settings_store._norm_peak_window({"ranges": ["坏区间", "25:99-26:99"], "tz": ""})
        check("非法区间被丢弃并回落默认", norm["ranges"] == ["09:00-12:00", "14:00-18:00"], str(norm))
        check("tz 有兜底", norm["tz"] == "Asia/Shanghai", str(norm))

        # [3] OpenAI 兼容 = 单档；DeepSeek = 四档
        print("[3] 两类协议的档位约束")
        oa = settings_store._norm_model_profile({
            "id": "t-oa", "model": "glm-4-flash", "compat": "openai",
            "efforts": ["none", "low", "high", "max"],
        })
        ds = settings_store._norm_model_profile({
            "id": "t-ds", "model": "deepseek-flash", "compat": "deepseek",
            "efforts": ["none", "low", "high", "max"],
        })
        check("后端能识别 openai 协议", oa["compat"] == "openai", str(oa["compat"]))
        check("DeepSeek 四档保留", ds["efforts"] == ["none", "low", "high", "max"], str(ds["efforts"]))
        # 界面负责把 OpenAI 压成单档：断言前端产物里确实有这段约束
        app = (ROOT / "web" / "src" / "App.vue").read_text(encoding="utf-8")
        check("前端对 OpenAI 强制单档",
              "efforts: isOpenAI(p) ? ['none'] : (p.efforts || [])" in app)
        check("前端提供两类模型分组", "DeepSeek" in app and "OpenAI 兼容" in app)
        check("前端保存时写的是 peak_window（不是死字段 offpeak_window）",
              "peak_window: {" in app and "offpeak_window: { ...windowForm }" not in app)
        # 2026-11-09 厂商一等公民重构：价格不再有独立内联输入框，而是随模型档案对象
        # 一起透传到保存（saveProviderDraft:272 / saveProfile:889 都带 pricing）；
        # 后端六格规范化见 [4]。保留这条是为了防止「保存时把价格静默清空」。
        check("前端保存模型档案时仍透传 pricing（价格不静默丢失）",
              "pricing: m.pricing || {}" in app and "pricing: p.pricing" in app)

        # [4] 价格两类都可改（闲时 + 高峰六格），且规范化不丢
        print("[4] 价格六格可改且往返无损")
        want = {"offpeak": {"in_miss": 1.5, "in_hit": 0.03, "out": 4.5},
                "peak": {"in_miss": 3.0, "in_hit": 0.06, "out": 9.0}}
        got = settings_store._norm_model_profile({"id": "t-pz", "model": "m", "pricing": want})["pricing"]
        check("闲时三格往返一致",
              all(abs(got["offpeak"][k] - want["offpeak"][k]) < 1e-9 for k in ("in_miss", "in_hit", "out")),
              str(got))
        check("高峰三格往返一致（不再被写死 ×2）",
              all(abs(got["peak"][k] - want["peak"][k]) < 1e-9 for k in ("in_miss", "in_hit", "out")),
              str(got))
    finally:
        # ------------------------------------------------------ 无条件恢复
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
    print("全部通过（key 脱敏 / 闲时时段生效 / 两类档位约束 / 价格六格可改 / 设置无污染）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
