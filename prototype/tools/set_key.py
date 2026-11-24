"""安全换 API key：**先验证能用，再落盘**；一次改全所有位置。

## 为什么需要它（2026-11-14）

DeepSeek 后台删掉旧 key 后，`settings.json` 里**4 个位置**会同时失效：
`remote.api_key` / `remote.models[i].api_key` / `remote.providers[0].api_key`。
手工改容易漏一处，漏了就出现"界面显示已换、实际还在用旧 key"的怪象。

**顺序很重要**：先拿新 key 真发一次最小请求（`max_tokens=1`，约 ¥0.0001），
**能用才写入**。这样绝不会把一个打不通的 key 写进配置，也就不会出现
"改完还是 401、然后怀疑代码有问题"。

## 用法

```bash
python tools/set_key.py sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
python tools/set_key.py --check            # 只测当前 key 通不通，不写
```

**不会打印完整 key**（只显示 `sk-abc…1234`）。
改之前自动备份成 `settings.json.bak-<时间戳>`。
"""
from __future__ import annotations

import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[1] / "data" / "settings.json"
BASE = "https://api.deepseek.com"
MODEL = "deepseek-flash"


def mask(k: str) -> str:
    k = str(k or "")
    return (k[:6] + "…" + k[-4:]) if len(k) > 12 else ("（空）" if not k else "***")


def probe(k: str, timeout: int = 30) -> tuple[bool, str]:
    """真发一次最小请求。返回 (通不通, 说明)。"""
    body = json.dumps({"model": MODEL,
                       "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1, "stream": False}).encode()
    req = urllib.request.Request(
        BASE + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + k, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
            u = d.get("usage") or {}
            return True, "HTTP 200，usage=%s" % json.dumps(u, ensure_ascii=False)
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", errors="replace")
        return False, "HTTP %d：%s" % (e.code, txt[:240])
    except Exception as exc:                             # noqa: BLE001
        return False, "%s：%s" % (type(exc).__name__, exc)


def _apply(d: dict, key: str) -> list[str]:
    """把新 key 写进所有位置，返回改过的路径（人类可读）。

    2026-11-18 起厂商是**三层结构**：providers[i].keys[j].api_key（key 类，
    多远端协作按 key 分角色）—— 写作者/审核者各用各的 key，所以这里只填
    「厂商级 + 正在使用的 key 类 + 平铺 models」，其余 key 类**留空**
    （等用户拿第二把 key 走 UI 或 --key-id 填，绝不能把同一把 key 填满
    所有角色 —— 那会让审核者与写作者共享限流池，协商机制失效）。
    """
    changed: list[str] = []
    r = d.setdefault("remote", {})
    if isinstance(r, dict):
        if r.get("api_key") != key:
            r["api_key"] = key
            changed.append("remote.api_key")
        for lst_name in ("models", "providers"):
            lst = r.get(lst_name)
            if not isinstance(lst, list):
                continue
            for i, m in enumerate(lst):
                if not isinstance(m, dict):
                    continue
                # 只同步"原本就填了 key"或"第 0 个主档案"的条目：
                # 空槽（占位的备用厂商）**不填**，免得把 DeepSeek 的 key
                # 塞到 GLM/OpenAI 的档案里，制造更隐蔽的错误。
                old = str(m.get("api_key") or "")
                if not old and i != 0:
                    continue
                if old != key:
                    m["api_key"] = key
                    changed.append("%s[%d].api_key" % (lst_name, i))
                # 厂商的 key 类层（多远端协作）：只填「正在使用」的那个类
                for k in (m.get("keys") or []):
                    if not isinstance(k, dict):
                        continue
                    kold = str(k.get("api_key") or "")
                    if k.get("current") and kold != key:
                        k["api_key"] = key
                        changed.append("%s[%d].keys[%s].api_key"
                                       % (lst_name, i, k.get("id")))
                    elif kold and kold != key and not k.get("current"):
                        # 非当前类原本有 key → 保留（那是审核者的 key，不是过期的）
                        pass
    return changed


def main(argv: list[str]) -> int:
    if not SETTINGS.is_file():
        print(f"找不到配置文件：{SETTINGS}")
        return 2
    d = json.loads(SETTINGS.read_text(encoding="utf-8"))
    cur = str(((d.get("remote") or {}).get("api_key")) or "")

    if "--check" in argv or len(argv) < 2:
        print(f"当前 key：{mask(cur)}（{len(cur)} 字符）")
        ok, why = probe(cur) if cur else (False, "配置里没有 key")
        print(("✓ 可用：" if ok else "✗ 不可用：") + why)
        if not ok:
            print("\n换 key：  python tools/set_key.py sk-你的新key")
        return 0 if ok else 1

    new = argv[1].strip()
    # 1) 格式先过一遍（省一次网络请求，也避免把复制残渣写进去）
    print(f"新 key：{mask(new)}（{len(new)} 字符）")
    if not new.startswith("sk-"):
        print("✗ 格式不对：应以 `sk-` 开头")
        return 2
    if len(new) != 35:
        print(f"✗ 长度不对：DeepSeek 的 key 是 35 字符（sk- + 32），实际 {len(new)}")
        print("  （多/少一般是复制时带进了空格或换行）")
        return 2
    # 2) 真发一次最小请求 —— **能用才写**
    print("正在用新 key 发一次最小请求（max_tokens=1）…")
    ok, why = probe(new)
    if not ok:
        print("✗ 新 key 也打不通，**没有写入**：" + why)
        return 1
    print("✓ " + why)
    # 3) 备份 + 写入
    bak = SETTINGS.with_suffix(".json.bak-%s" % time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(SETTINGS, bak)
    changed = _apply(d, new)
    SETTINGS.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ 已写入 {len(changed)} 处：" + (", ".join(changed) or "（本来就是新 key）"))
    print(f"  备份：{bak.name}")
    # 4) 复验（用**写进去的那份**再测一遍，确认不是改了别的文件）
    d2 = json.loads(SETTINGS.read_text(encoding="utf-8"))
    ok2, why2 = probe(str(((d2.get("remote") or {}).get("api_key")) or ""))
    print(("✓ 复验通过：" if ok2 else "✗ 复验失败：") + why2)
    if ok2:
        print("\n下一步：重启夜班（双击 START_NIGHT.bat），或先跑一次"
              " `python cli.py full --repo <仓库>` 看错误报告。")
    return 0 if ok2 else 1


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main(sys.argv))
