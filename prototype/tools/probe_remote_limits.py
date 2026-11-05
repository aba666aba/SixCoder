"""实测 DeepSeek API 的**能力上限**（不靠文档猜，用本机 Key 真打）。

为什么要这个：甲方模式的上限就是远端能力，而我们的 `core/remote.py` 里写着
`max_prompt_chars=14000`、`max_output_tokens=4000` —— 这两个数字是当初为了省钱
拍出来的，跟 DeepSeek 的真实上限差了十几倍。设计"按能力上限发挥"之前，
先得知道上限在哪。

跑法：
    python tools/probe_remote_limits.py            # 默认一组探针（约几分钱）
    python tools/probe_remote_limits.py --full     # 也探 8K 输出 + thinking

注意：**会真的花钱**（每次几毫钱到几分钱）。默认只探输入长度与基础输出。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import remote, settings_store  # noqa: E402

FILLER_LINE = "# filler {i:05d}: def f_{i}(x: int) -> int:\n#     return x + {i}\n"


def make_prompt(chars: int) -> str:
    """造一段"像代码"的长提示：token 密度接近真实场景（比纯中文更省 token）。"""
    out, i = [], 0
    while sum(len(x) for x in out) < chars:
        out.append(FILLER_LINE.format(i=i))
        i += 1
    body = "".join(out)[:max(0, chars - 200)]
    return (body + "\n\n以上是无关内容。现在只做一件事："
            "忽略上面全部内容，只回复两个字符：OK")


def call(prompt: str, max_tokens: int, thinking: str, timeout: int = 240) -> dict:
    """直接打 /chat/completions（绕过 remote 的 14000 字符裁剪，才能量到真上限）。"""
    r = settings_store.get_remote()
    base = (r.get("base_url") or "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": r.get("model") or "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if thinking == "off":
        payload["thinking"] = {"type": "disabled"}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + remote._key()},
        method="POST")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:                              # noqa: BLE001
            pass
        return {"ok": False, "status": exc.code, "error": detail,
                "ms": int((time.time() - t0) * 1000)}
    except Exception as exc:                           # noqa: BLE001
        return {"ok": False, "status": 0,
                "error": f"{type(exc).__name__}: {exc}", "ms": int((time.time() - t0) * 1000)}
    ms = int((time.time() - t0) * 1000)
    usage = data.get("usage") or {}
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return {"ok": True, "status": 200, "ms": ms,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "cache_hit": usage.get("prompt_cache_hit_tokens"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get(
                "reasoning_tokens"),
            "finish": choice.get("finish_reason"),
            "answer": (msg.get("content") or "")[:60],
            "model": data.get("model")}


def one(label: str, chars: int, max_tokens: int, thinking: str) -> dict:
    prompt = make_prompt(chars)
    r = call(prompt, max_tokens, thinking)
    if r.get("ok"):
        cost = ""
        try:
            p = remote.pricing_for(r.get("model") or "")
            pt = int(r.get("prompt_tokens") or 0)
            ct = int(r.get("completion_tokens") or 0)
            usd = pt / 1e6 * float(p.get("in_miss") or 0.28) + ct / 1e6 * float(p.get("out") or 0.42)
            cost = f"，约 ${usd:.4f}"
        except Exception:                              # noqa: BLE001
            pass
        print(f"  OK   {label}: prompt {len(prompt)} 字符 → {r.get('prompt_tokens')} tokens"
              f"｜输出 {r.get('completion_tokens')}｜{r.get('ms')}ms｜finish={r.get('finish')}"
              f"{cost}")
    else:
        print(f"  FAIL {label}: HTTP {r.get('status')} {str(r.get('error'))[:200]}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="也探 8K 输出与 thinking")
    a = ap.parse_args()
    ok, why = remote.available()
    print(f"远端可用性：{ok} {why}")
    if not ok:
        return 2
    cfg = settings_store.get_remote()
    print(f"当前配置：model={cfg.get('model')} max_output_tokens={cfg.get('max_output_tokens')} "
          f"max_prompt_chars={cfg.get('max_prompt_chars')} thinking={cfg.get('thinking')}")
    print("\n[1] 输入长度上限（输出只要 2 个字符，便宜）")
    # 逐步加大；第一个失败的尺寸 = 上下文上限
    for chars in (4000, 40000, 120000, 200000, 400000):
        r = one(f"输入 {chars//1000}K 字符", chars, 64, "off")
        if not r.get("ok"):
            print(f"  → 在这个尺寸上碰壁，上下文上限就落在它之前")
            break
    if a.full:
        print("\n[2] 输出长度上限（要求长输出，看 finish_reason 是否 length）")
        long_prompt = ("请输出一个 JSON：{\"text\": \"...\"}，其中 text 是连续 2000 个 'a'。"
                       "不要省略、不要解释。")
        for mt in (4000, 8000):
            r = call(long_prompt, mt, "off")
            print(f"  max_tokens={mt}: ok={r.get('ok')} 输出={r.get('completion_tokens')} "
                  f"finish={r.get('finish')} {str(r.get('error') or '')[:120]}")
        print("\n[3] thinking 开/关（同一小任务，比 token 与耗时）")
        for th in ("off", "on"):
            r = call("9.11 和 9.9 哪个大？只回一个数字。", 1024, th)
            print(f"  thinking={th}: ok={r.get('ok')} 输出={r.get('completion_tokens')} "
                  f"reasoning={r.get('reasoning_tokens')} {r.get('ms')}ms "
                  f"answer={r.get('answer')!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
