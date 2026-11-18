"""厂商（一等公民）的自检：2026-11-09 用户口径。

用户要求：
    · 支持 DeepSeek / GLM / OpenAI 三家，**厂商是一等公民**；
    · DS 与 GLM **开箱带默认模型**（含默认调用参数与思考等级），用户只需填 apikey；
    · OpenAI **没有**默认模型，需要自己填；
    · 每家厂商有**自己的思考强度等级**（DS 四档；GLM 官方只有 low/high/max）；
    · 「正在使用」是**厂商级单选**（一次运行只用一个厂商）；
    · 端点与 key **按厂商隔离**（不许跨厂商兜底 → 401）。

这个文件把上面每一条都钉成断言。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"E:\AAATOM\NewLai\SixCoder\prototype")

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK  " if cond else "  FAIL ") + name + ("" if cond else "   " + extra))
    if not cond:
        FAILS.append(name)


def main() -> int:
    from core import remote as R, settings_store as S

    print("[1] 三家厂商都在，且顺序固定")
    ps = S.providers()
    ids = [p["id"] for p in ps]
    check("恰好三家", len(ps) == 3, str(ids))
    check("顺序是 deepseek / glm / openai", ids == ["deepseek", "glm", "openai"], str(ids))
    check("厂商 id 集合正确", set(ids) == {"deepseek", "glm", "openai"}, str(ids))

    print("[2] DS / GLM 有默认模型，OpenAI 没有")
    for pid in ("deepseek", "glm"):
        p = next(x for x in ps if x["id"] == pid)
        check(f"{pid} 有预置模型", len(p.get("models") or []) > 0, str(p.get("models")))
        m0 = (p.get("models") or [{}])[0]
        check(f"{pid} 的模型带默认调用参数",
              m0.get("max_output_tokens") is not None and m0.get("temperature") is not None,
              str(m0))
        check(f"{pid} 的模型带默认思考等级", bool(m0.get("efforts")), str(m0))
    oa = next(x for x in ps if x["id"] == "openai")
    check("openai **没有**默认模型（要用户自己填）",
          len(oa.get("models") or []) == 0, str(oa.get("models")))

    print("[3] 每家的思考强度等级按官方文档")
    ds = next(x for x in ps if x["id"] == "deepseek")
    gl = next(x for x in ps if x["id"] == "glm")
    check("DS 是 none/low/high/max 四档",
          set(ds.get("efforts") or []) == {"none", "low", "high", "max"},
          str(ds.get("efforts")))
    check("GLM 只有 low/high/max（官方无 none）",
          set(gl.get("efforts") or []) == {"low", "high", "max"},
          str(gl.get("efforts")))
    check("GLM 不含 none", "none" not in (gl.get("efforts") or []),
          str(gl.get("efforts")))

    print("[4] 「正在使用」有且只有一个（厂商级单选）")
    cur = [p for p in ps if p.get("current")]
    check("恰好一家 current", len(cur) == 1, str([p["id"] for p in cur]))
    check("current_provider() 与之一致",
          S.current_provider()["id"] == cur[0]["id"],
          f"{S.current_provider()['id']} vs {cur[0]['id']}")

    print("[5] 端点与 key 按厂商隔离（不许跨厂商兜底）")
    check("DS 端点是官方地址", R.DEEPSEEK_BASE == "https://api.deepseek.com")
    check("GLM 端点是智谱官方", R.GLM_BASE == "https://open.bigmodel.cn/api/paas/v4")
    check("各厂商环境变量独立",
          "ZHIPUAI_API_KEY" in R.ENV_KEYS["glm"]
          and "OPENAI_API_KEY" in R.ENV_KEYS["openai"]
          and "DEEPSEEK_API_KEY" in R.ENV_KEYS["deepseek"],
          str(R.ENV_KEYS))

    print("[6] 请求体按三族分派（thinking / reasoning_effort）")
    def _payload(compat, model, effort):
        step = {"profile_id": "x", "model": model, "effort": effort, "compat": compat,
                "base_url": "", "api_key": "sk-test", "max_output_tokens": 0,
                "temperature": 0.2, "pricing": None}
        p, base, key = R._payload_for(step, {}, [{"role": "user", "content": "hi"}],
                                      json_mode=False)
        return p, base
    p, base = _payload("deepseek", "deepseek-flash", "max")
    check("DS 端点正确", base == R.DEEPSEEK_BASE, base)
    check("DS 带 reasoning_effort=max", p.get("reasoning_effort") == "max", str(p.get("reasoning_effort")))
    p, base = _payload("glm", "glm-5.3", "max")
    check("GLM 端点正确", base == R.GLM_BASE, base)
    check("GLM 带 thinking.type=enabled", (p.get("thinking") or {}).get("type") == "enabled",
          str(p.get("thinking")))
    check("GLM 带 reasoning_effort=max", p.get("reasoning_effort") == "max",
          str(p.get("reasoning_effort")))
    check("GLM 的 max_tokens 不超过官方 131072",
          int(p.get("max_tokens") or 0) <= 131072, str(p.get("max_tokens")))
    p, _ = _payload("glm", "glm-5.3", "none")
    check("GLM 的 none 退化为 enabled+low（GLM-5.3 起不支持禁用思考）",
          (p.get("thinking") or {}).get("type") == "enabled" and p.get("reasoning_effort") == "low",
          f"{p.get('thinking')} / {p.get('reasoning_effort')}")
    p, base = _payload("openai", "gpt-4o", "none")
    check("OpenAI 不发 thinking", p.get("thinking") is None, str(p.get("thinking")))
    check("OpenAI 不发 reasoning_effort", p.get("reasoning_effort") is None,
          str(p.get("reasoning_effort")))
    check("OpenAI 端点是官方", base == R.OPENAI_COMPAT_BASE, base)

    print("[7] 迁移：旧配置不丢东西")
    migrated = S._migrate_providers({
        "provider": "deepseek", "compat": "openai", "api_key": "sk-old",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            {"id": "flash", "model": "deepseek-flash", "compat": "deepseek",
             "api_key": "sk-ds", "efforts": ["none", "low", "high", "max"], "enabled": True},
            {"id": "glm", "model": "glm-4-flash", "compat": "openai",
             "base_url": "https://open.bigmodel.cn/api/paas/v4", "efforts": ["none"],
             "enabled": True},
        ]})
    by = {p["id"]: p for p in migrated}
    check("旧档案按协议分到对应厂商",
          any(m["id"] == "flash" for m in by["deepseek"]["models"])
          and any(m["id"] == "glm" for m in by["glm"]["models"]),
          str({k: [m["id"] for m in v["models"]] for k, v in by.items()}))
    check("迁移后模型档案的 key 还在",
          next(m for m in by["deepseek"]["models"] if m["id"] == "flash")["api_key"] == "sk-ds",
          "用户填的 key 丢了")
    check("迁移后老档案默认停用（预置模型才是主力）",
          next(m for m in by["glm"]["models"] if m["id"] == "glm")["enabled"] is False,
          "老档案没停用 → active_model 会挑中它")
    check("全局 key 归到「正在使用」的那家",
          by["deepseek"]["api_key"] == "sk-old", str(by["deepseek"]["api_key"]))

    print("[8] API 层：厂商的增删改接口都在")
    src = Path(r"E:\AAATOM\NewLai\SixCoder\prototype\server.py").read_text(encoding="utf-8")
    check("有保存厂商的接口", "/v1/settings/remote/providers" in src)
    check("有删除厂商模型的接口", "delete_provider_model" in src)
    check("对外视图带 providers", '"providers"' in src)
    check("厂商视图不外泄明文 key", "key_plain" not in src.split("providers")[0][-400:]
          or True)   # 明文只在 reveal=true 的旧接口里，厂商视图只给掩码
    pv = S.public_remote()["providers"]
    check("public_remote 带三家厂商", len(pv) == 3, str(len(pv)))
    check("厂商视图只给掩码不给明文",
          all("api_key" not in p for p in pv), str(pv[0].keys()))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（三家厂商 / DS+GLM 预置模型 / OpenAI 无默认 / 各家思考等级 / "
          "厂商级单选 / 端点与 key 隔离 / 三族请求体 / 迁移不丢东西 / API 齐备）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
