"""远端协助自检（不联网）：设置读写与脱敏、可用性判定、包体裁剪、token 节省逻辑。

跑法: python check_remote.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import remote, settings_store  # noqa: E402


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {extra}" if extra and not cond else ""))
    return cond


def main() -> int:
    ok = True
    # 用临时设置文件，避免污染真实配置
    tmp = Path(tempfile.mkdtemp()) / "settings.json"
    settings_store.SETTINGS = tmp

    print("远端协助自检")

    # 1) 没配 key → 不可用（本地路径不受影响）
    os.environ.pop("DEEPSEEK_API_KEY", None)
    ok &= check("无 key 时远端不可用", remote.available()[0] is False,
                str(remote.available()))
    ok &= check("不可用时说明原因", "api key" in remote.available()[1].lower()
                or "key" in remote.available()[1], remote.available()[1])

    # 2) 设置里写 key → 立即可用，且回显脱敏
    settings_store.update_remote({"api_key": "sk-fake-test-key-0001", "model": "deepseek-chat"})
    view = settings_store.public_remote()
    ok &= check("写入后可用", remote.available()[0] is True, str(remote.available()))
    ok &= check("明文不回显", "sk-fake-test-key-0001" not in str(view), str(view))
    ok &= check("回显为掩码", "…" in view["key_masked"], view["key_masked"])
    ok &= check("来源标注 settings", view["key_source"] == "settings", str(view))
    ok &= check("key 不落明文到 public 视图", set(view) >= {"key_configured", "key_masked"})

    # 3) 随时更新：改 key 后下一次读取就是新值
    settings_store.update_remote({"api_key": "sk-fake-test-key-0002"})
    ok &= check("可随时更新 key", settings_store.public_remote()["key_masked"].endswith("0002"),
                settings_store.public_remote()["key_masked"])

    # 4) 关闭开关 → 不可用
    settings_store.update_remote({"enabled": False})
    ok &= check("可关闭远端", remote.available()[0] is False)
    settings_store.update_remote({"enabled": True})

    # 5) 环境变量兜底（设置里清空时用 env）
    settings_store.update_remote({"api_key": ""})
    os.environ["DEEPSEEK_API_KEY"] = "sk-fake-env-key-0003"
    view2 = settings_store.public_remote()
    ok &= check("env 兜底", view2["key_source"] == "env" and view2["key_configured"], str(view2))
    os.environ.pop("DEEPSEEK_API_KEY", None)

    # 6) 包体裁剪：超限时保留头尾（头里有输出格式，尾里有最近失败证据）
    big = "# 头\n" + "x" * 20000 + "\n最近失败：ImportError\n"
    trimmed = remote._trim(big, 2000)
    ok &= check("超长包被裁剪", len(trimmed) < 2200, str(len(trimmed)))
    ok &= check("裁剪保留头部格式段", trimmed.startswith("# 头"))
    ok &= check("裁剪保留尾部失败证据", "ImportError" in trimmed)
    ok &= check("未超限时原样返回", remote._trim("短包", 100) == "短包")

    # 7) 复用去重键：同一份证据 → 同一 key（省 token 的核心）
    k1 = remote._cache_key("packet-A", "deepseek-chat")
    k2 = remote._cache_key("packet-A", "deepseek-chat")
    k3 = remote._cache_key("packet-B", "deepseek-chat")
    ok &= check("相同证据生成相同去重键", k1 == k2 and k1 != k3)

    # 8) 用量统计结构
    s = remote.usage_summary("")
    ok &= check("用量统计字段齐全",
                {"calls", "prompt_tokens", "cache_hit_rate", "cached_replies"} <= set(s), str(s))

    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
