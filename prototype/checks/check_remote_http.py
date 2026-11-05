"""远端设置端点的 HTTP 级验证：起服务后打真实请求（不是只看源码编译过）。

⚠️ **本检查会改写本机远端设置**（会临时覆盖 api_key）。
   所以它做两件事：开始前把现有 key 备份到内存，结束时**无条件恢复**；
   恢复失败会红字告警并以非 0 退出，绝不静默。
   事故记录：早先版本没有备份，把用户真实的 key 覆盖成测试值再清空，找不回来了 ——
   这条注释就是为了让后来的人不要再踩第二次。

用法:
    python server.py                     # 先起后端（默认 127.0.0.1:8760）
    python check_remote_http.py [base_url]
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8760"
FAILS: list[str] = []


def call(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def run_checks(original_key: str) -> None:
    r = call("/v1/settings/remote")
    check("GET 返回 remote/available/usage", {"remote", "available", "usage"} <= set(r),
          str(r)[:160])
    dump = json.dumps(r)
    check("GET 不含明文 key（含模型档案里也不许有 api_key）",
          "api_key" not in dump and (not original_key or original_key not in dump),
          dump[:200])
    print(f"    初始状态 available={r['available']}（不假设初始必定未配置）")

    r2 = call("/v1/settings/remote", "POST", {"api_key": "sk-fake-test-key-0005"})
    check("POST 设置 key 后可用", r2["available"] is True, str(r2)[:160])
    check("POST 回显为掩码",
          bool(r2["remote"].get("key_masked")) and "sk-fake-test-key-0005" not in json.dumps(r2),
          r2["remote"].get("key_masked", ""))
    check("来源标注 settings", r2["remote"]["key_source"] == "settings", str(r2["remote"])[:120])
    check("模型档案里不回 api_key",
          all("api_key" not in m for m in r2["remote"].get("models") or []),
          str((r2["remote"].get("models") or [{}])[0])[:120])

    r3a = call("/v1/settings/remote", "POST", {"api_key": ""})
    check("空 api_key 不会误删已有 key（防误删设计）",
          r3a["remote"].get("key_configured") is True, str(r3a["remote"].get("key_source")))

    r3 = call("/v1/settings/remote", "POST", {"clear_key": True})
    check("显式 clear_key 之后不可用", r3["available"] is False, str(r3.get("reason"))[:80])

    r4 = call("/v1/settings/remote", "POST", {"max_output_tokens": 2000})
    check("可改非密钥字段", r4["remote"]["max_output_tokens"] == 2000,
          str(r4["remote"].get("max_output_tokens")))


def _ensure_backend():
    """保证后端在跑：不通就自己起一个（跑完杀掉）。

    为什么让检查自带后端：否则它永远是 run_all 里的 skip ——
    "从来没跑过"和"跑过没事"在报告里长得一样，这是最容易自欺的一类。
    """
    import subprocess
    import time
    root = Path(__file__).resolve().parents[1]

    def alive() -> bool:
        try:
            with urllib.request.urlopen(BASE + "/v1/health", timeout=3) as r:
                return r.status == 200
        except Exception:                                # noqa: BLE001
            return False

    if alive():
        return None, "已有后端"
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=str(root),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        time.sleep(0.5)
        if alive():
            return proc, "本检查自启"
    return proc, "起不来"


def main() -> int:
    print(f"远端设置端点验证 @ {BASE}")
    proc, how = _ensure_backend()
    print(f"  后端来源：{how}")
    if how == "起不来":
        print("  后端起不来；先手动 `python server.py` 再跑本检查")
        if proc is not None:
            proc.terminate()
        return 2
    try:
        return _run()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:                            # noqa: BLE001
                proc.kill()


def _run() -> int:
    try:
        original_key = (call("/v1/settings/remote?reveal=1")["remote"] or {}).get("key_plain") or ""
    except Exception as exc:                                # noqa: BLE001
        print(f"  读不到现有 key（{type(exc).__name__}）")
        return 2
    if original_key:
        print(f"  已备份现有 key（{len(original_key)} 字符），检查结束后会恢复")
    else:
        print("  当前没有已配置的 key，检查不会覆盖任何东西")

    ok = True
    try:
        run_checks(original_key)
    except Exception as exc:                                # noqa: BLE001
        print(f"  检查过程中出错：{type(exc).__name__}: {exc}")
        ok = False
    finally:
        if original_key:
            try:
                back = call("/v1/settings/remote", "POST", {"api_key": original_key})
                restored = bool(back["remote"].get("key_configured"))
            except Exception:                              # noqa: BLE001
                restored = False
            print("  " + ("已恢复原有 key ✅" if restored else
                          "**恢复原有 key 失败 —— 请到 设置 → 远端模型 重新填一次** ❌"))
            if not restored:
                ok = False

    ok = ok and not FAILS
    print(f"\n{'全部通过' if ok else '有失败项'} → exit={0 if ok else 1}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
