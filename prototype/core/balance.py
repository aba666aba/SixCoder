"""官方余额查询（DeepSeek `GET /user/balance`）。

**为什么不用自己算**（2026-11-11 用户指出）：
引擎自己累加的"已花费"（`remote_usage` + `project_ledger` + `night_spent.json`）
是**三个口径**、还会因为迁移/补账/口径切换而互相打架 ——
实测同一天就出现三个数：账本 ¥38.68 / `night_spent` ¥33.40 / 文档 ¥31.01。
而**余额是账号的客观事实**，官方接口直接给：

    GET https://api.deepseek.com/user/balance
    Authorization: Bearer <key>
    → {"is_available": true,
       "balance_infos": [{"currency": "CNY",
                          "total_balance": "110.00",
                          "granted_balance": "10.00",
                          "topped_up_balance": "100.00"}]}

所以口径改成：
  · **"还剩多少" → 问官方**（本模块）
  · **"已经花了多少" → 引擎账本**（历史事实，报告用；与官方对账，而不是替代官方）

**不编数**：查不到就如实说"查不到"，绝不拿估算冒充余额。

只对 **DeepSeek 官方端点**生效（用户把 base_url 改成兼容端点时，
余额是那个服务商的账，查 DeepSeek 官方没有意义 → 如实说明）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BALANCE_PATH = "/user/balance"
TIMEOUT = 20.0


def _ds_base_url(base_url: str = "") -> str:
    """取 DeepSeek 官方 base_url（只有官方端点才有 /user/balance）。"""
    if base_url:
        return str(base_url).rstrip("/")
    try:
        from . import settings_store
        r = settings_store.get_remote() or {}
    except Exception:                                    # noqa: BLE001
        r = {}
    return str(r.get("base_url") or "https://api.deepseek.com").rstrip("/")


def _key() -> str:
    """设置里的显式 key > 环境变量（与 core/remote.py 同口径，不跨厂商兜底）。"""
    try:
        from . import settings_store
        r = settings_store.get_remote() or {}
    except Exception:                                    # noqa: BLE001
        r = {}
    k = str(r.get("api_key") or "").strip()
    if k:
        return k
    return str(os.environ.get("DEEPSEEK_API_KEY") or "").strip()


def is_official(base_url: str = "") -> bool:
    """这个 base_url 是不是 DeepSeek 官方（决定能不能查余额）。"""
    u = _ds_base_url(base_url).lower()
    return "api.deepseek.com" in u


def query(base_url: str = "", api_key: str = "", timeout: float = TIMEOUT) -> dict:
    """查官方余额。返回 {"ok", "total", "granted", "topped_up", "currency", "available", "detail"}。

    **查不到就说查不到**（ok=False + detail），绝不返回估算值。
    """
    out: dict[str, Any] = {"ok": False, "total": None, "granted": None,
                           "topped_up": None, "currency": "", "available": None,
                           "detail": ""}
    if not is_official(base_url):
        out["detail"] = ("当前 base_url 不是 DeepSeek 官方端点"
                         f"（{_ds_base_url(base_url)}），余额查不了"
                         "—— 那是别的服务商的账")
        return out
    key = str(api_key or "").strip() or _key()
    if not key:
        out["detail"] = "没有 API key（设置 → 远端模型 → 填 apikey，或设 DEEPSEEK_API_KEY）"
        return out
    url = _ds_base_url(base_url) + BALANCE_PATH
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:                                # noqa: BLE001
            pass
        out["detail"] = f"官方接口返回 HTTP {exc.code}" + (f"：{body}" if body else "")
        return out
    except Exception as exc:                             # noqa: BLE001
        out["detail"] = f"查不到官方余额：{type(exc).__name__}: {exc}"[:160]
        return out
    try:
        data = json.loads(raw)
    except Exception:                                    # noqa: BLE001
        out["detail"] = "官方余额返回不可解析：" + raw[:160]
        return out
    infos = data.get("balance_infos") or []
    if not infos:
        out["detail"] = "官方余额返回里没有 balance_infos"
        return out
    info = infos[0] or {}
    out["ok"] = True
    out["available"] = bool(data.get("is_available"))
    out["currency"] = str(info.get("currency") or "")
    for k, fld in (("total", "total_balance"), ("granted", "granted_balance"),
                   ("topped_up", "topped_up_balance")):
        try:
            out[k] = float(info.get(fld))
        except (TypeError, ValueError):
            out[k] = None
    out["detail"] = "官方余额"
    return out


def snapshot() -> dict:
    """带引擎口径的快照：官方余额 + 引擎自算花费（两者对账，不互相替代）。"""
    bal = query()
    snap: dict[str, Any] = {"balance": bal}
    try:
        from . import budget_estimate as _be
        from pathlib import Path
        snap["engine_spent_cny"] = round(_be.project_spent_cny(Path(".")), 4)
    except Exception:                                    # noqa: BLE001
        snap["engine_spent_cny"] = None
    return snap


def balance_line() -> str:
    """一行摘要，给报告/日志用。查不到就如实说查不到。"""
    b = query()
    if not b.get("ok"):
        return "官方余额：查不到（%s）" % (b.get("detail") or "未知原因")
    cur = b.get("currency") or "CNY"
    tot = b.get("total")
    txt = "官方余额：%s" % ("%.2f %s" % (tot, cur) if tot is not None else "未知 %s" % cur)
    if b.get("granted") is not None:
        txt += "（赠金 %.2f + 充值 %.2f）" % (b["granted"], b.get("topped_up") or 0.0)
    if b.get("available") is False:
        txt += " **余额不足，API 不可用**"
    return txt


# ── 三账分离（2026-11-11 用户定稿）──────────────────────────────────────
#   ① 项目需要的预算 = 引擎估算（预测值）→ 充值决策
#   ② 实际花费       = 运行时记账（事实）  → 报告
#   ③ 余额           = 官方接口（事实）    → 硬约束
# 三者**不许互相替代**：①−② 不是余额（①是估算）；③ 不是 ②（受充值/赠金影响）。

RECHARGED_KEY = "recharged_cny"


def load_recharged(ws=None) -> float | None:
    """读用户登记的**充值总额**（`.sixcoder/budget.json` 的 `recharged_cny`）。

    没有登记 = None（**不编数**：不能拿"引擎自算花费 + 当前余额"反推充值额，
    因为那样等于默认账本没漏记，而今天的实测差额恰恰说明账本会漏）。
    """
    if ws is None:
        return None
    try:
        p = Path(ws) / ".sixcoder" / "budget.json"
        if not p.exists():
            return None
        d = json.loads(p.read_text(encoding="utf-8-sig"))
        v = d.get(RECHARGED_KEY)
        if v is None or v == "":
            return None
        return float(v)
    except Exception:                                    # noqa: BLE001
        return None


def reconcile(ws=None, bal: dict | None = None) -> dict:
    """三账对账：充值总额 − 实际花费 − 余额 = **账外消费**。

    只报差额，**不改账**（账是历史事实，差额是事实的差）。
    任何一个量取不到就如实说取不到，不拿估算冒充。
    """
    b = bal if bal is not None else query()
    out: dict[str, Any] = {
        "balance_ok": bool(b.get("ok")),
        "balance_cny": b.get("total"),
        "balance_detail": b.get("detail"),
        "recharged_cny": load_recharged(ws),
        "engine_spent_cny": None,
        "outside_spend_cny": None,
        "note": "",
    }
    if ws is not None:
        try:
            from . import budget_estimate as _be
            out["engine_spent_cny"] = round(_be.project_spent_cny(Path(ws)), 4)
        except Exception as exc:                         # noqa: BLE001
            out["note"] = "引擎花费取不到：%s" % type(exc).__name__
    rc = out.get("recharged_cny")
    sp = out.get("engine_spent_cny")
    balv = out.get("balance_cny")
    if rc is not None and sp is not None and balv is not None:
        out["outside_spend_cny"] = round(rc - sp - balv, 4)
        if out["outside_spend_cny"] > 0.5:
            out["note"] = ("差额 ¥%.2f = 账外消费（其他会话/其他 key/其他项目/手工调用），"
                           "或引擎账本漏记" % out["outside_spend_cny"])
        elif out["outside_spend_cny"] < -0.5:
            out["note"] = ("差额 ¥%.2f = 账本比官方多记，可能是赠金/多项目口径混入"
                           % abs(out["outside_spend_cny"]))
        else:
            out["note"] = "对账一致（差额 ≤ ¥0.5）"
    else:
        miss = [k for k, v in (("充值总额", rc), ("引擎花费", sp), ("余额", balv)) if v is None]
        out["note"] = "对不了账，缺：" + "、".join(miss) + "（缺哪个就登记哪个，不编数）"
    return out


def balance_exhausted(bal: dict | None = None) -> tuple[bool, str]:
    """余额闸：**官方余额耗尽** → 该停。

    返回 (是否该停, 原因)。

    口径（2026-11-11 用户定稿"双闸并行"）：
      · 余额是**官方事实** → 余额为 0 / `is_available=false` 就该停，
        不该由"自算花费"这种估算决定能否继续花钱。
      · **查不到 ≠ 余额为零** —— 网络失败、key 失效、端点不是官方
        一律按"取不到"处理，**绝不误停**（与既有口径一致）。
    """
    b = bal if bal is not None else query()
    if not b.get("ok"):
        return False, ""                       # 取不到 → 不误停
    if b.get("available") is False:
        return True, "官方余额不可用（is_available=false）"
    tot = b.get("total")
    if tot is not None and float(tot) <= 0:
        return True, "官方余额已耗尽（%s %s）" % (tot, b.get("currency") or "")
    return False, ""


if __name__ == "__main__":                             # pragma: no cover
    print(json.dumps(query(), ensure_ascii=False, indent=2))
