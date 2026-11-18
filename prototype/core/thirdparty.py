"""第三方 provider 模式：**规范检查 + 替身场景**（2026-11-15，开发文档 §13.11）。

## 解决的痛点

支付、短信验证码、图形/人机验证、LLM、邮件 —— 这些**没法用普通探针测**：
真付钱、真发短信、真滑滑块都不现实。所以现状是**完全没测**，上线才发现回调幂等没做、
超时没处理、伪造签名也照收。

## 标准化流程（四件套，缺一不可）

```
① 接口抽象     app/providers/<kind>.py 里定义 Provider 接口
② 可注入       模块级 set_provider(fake) 或构造参数（测试才能塞替身）
③ 业务不直连   业务代码只 import 接口，**不许** import 厂商 SDK
④ 场景判据     成功 / 超时 / 重复（幂等）/ 非法跃迁 / 伪造签名
```

前三件是**静态可查**的（本模块 D8.1），第四件需要注入点（D8.2）。

## 为什么"业务不直连"是硬要求

业务代码里出现 `import stripe` / `from alipay import ...` 的那一刻，
这个能力就**永远不可测**（除非真付钱）。而且换厂商要改业务代码 —— 抽象本来就是为了这个。

## 只报"确定的厂商 SDK"

`urllib` / `requests` / `httpx` 太通用（可能调自己的服务），**不报**。
只报明确的厂商包名 —— **误报比漏报更毁信任**。
"""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

from .errorreport import Error

__all__ = ["VENDOR_SDKS", "PROVIDER_DIR", "check_provider_pattern",
           "run_scenarios", "FakeProvider", "SCENARIO_KINDS"]

PROVIDER_DIR = "app/providers"

# 类别 → 明确的三方 SDK 包名（**只收确定的厂商包**，不收通用 HTTP 客户端）
VENDOR_SDKS = {
    "payment": ("wechatpay", "wechatpy", "wxpay", "alipay", "alipay_sdk",
                "stripe", "paypalrestsdk"),
    "sms": ("aliyunsdkcore", "aliyunsdkdysmsapi", "tencentcloud", "twilio",
            "yunpian", "submail"),
    "captcha": ("recaptcha", "geetest", "tencentcaptcha", "aliyun_captcha",
                "dysmsapi"),
    "llm": ("openai", "anthropic", "zhipuai", "dashscope", "qianfan",
            "volcengine", "erniebot"),
    "mail": ("sendgrid", "mailgun", "postmark"),
}

# 场景类型（标准化：这五种覆盖了第三方集成的绝大多数事故）
SCENARIO_KINDS = ("success", "timeout", "replay", "illegal_transition", "bad_signature")


class FakeProvider:
    """替身基类：按 `stub` 配置返回，不碰真实网络。

    `stub` 形状示例（支付）：
        {"charge": {"status": "ok", "txn": "T1"},
         "refund": {"raise": "TimeoutError"}}
    方法名 → 返回值 dict；`raise` 键表示抛对应异常。
    """

    def __init__(self, stub: dict | None = None):
        self._stub = dict(stub or {})
        self.calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _call(*args, **kw):
            self.calls.append((name, args, kw))
            spec = self._stub.get(name) or {}
            if not isinstance(spec, dict):
                return spec
            exc = spec.get("raise")
            if exc:
                raise _mk_exc(str(exc))
            out = {k: v for k, v in spec.items() if k != "raise"}
            return out
        return _call


def _mk_exc(name: str) -> Exception:
    """按名字造一个异常（`Timeout` → `TimeoutError`）。"""
    table = {
        "Timeout": TimeoutError, "TimeoutError": TimeoutError,
        "ConnectionError": ConnectionError, "ValueError": ValueError,
        "RuntimeError": RuntimeError, "PermissionError": PermissionError,
    }
    return table.get(name, RuntimeError)(name)


# ── D8.1 规范符合性（静态，零 token）───────────────────────────────────────
def check_provider_pattern(ws, *, kinds=None, exclude=("app/providers",)) -> list[Error]:
    """业务代码里**直接 import 厂商 SDK** → P1（该能力从此不可测）。

    `exclude`：provider 模块自己当然要 import SDK —— 那是对的，不报。
    """
    ws = Path(ws)
    out: list[Error] = []
    want = set(kinds) if kinds else set(VENDOR_SDKS)
    # 反向表：包名 → 类别
    pkg2kind: dict[str, str] = {}
    for kind, pkgs in VENDOR_SDKS.items():
        if kind not in want:
            continue
        for p in pkgs:
            pkg2kind[p] = kind

    mods: list[str] = []
    for base in ("app", "matching_core"):
        d = ws / base
        if not d.is_dir():
            continue
        for f in sorted(d.rglob("*.py")):
            rel = str(f.relative_to(ws)).replace("\\", "/")
            if any(rel.startswith(e) for e in (exclude or ())):
                continue
            mods.append(rel)

    provider_dir_exists = (ws / PROVIDER_DIR).is_dir()
    for rel in mods:
        try:
            src = (ws / rel).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        hit: list[tuple[str, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    top = a.name.split(".")[0]
                    if top in pkg2kind:
                        hit.append((top, getattr(node, "lineno", 0), pkg2kind[top]))
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top in pkg2kind:
                    hit.append((top, getattr(node, "lineno", 0), pkg2kind[top]))
        for pkg, lineno, kind in hit:
            out.append(Error(
                "P1", "thirdparty", "%s:%d" % (rel, lineno),
                "业务代码直接 import 厂商 SDK `%s`（%s）—— 该能力从此**不可测**："
                "测它要真付钱/真发短信，换厂商还要改业务代码。"
                "应改为 `%s/%s.py` 里的接口 + 注入点"
                % (pkg, kind, PROVIDER_DIR, kind),
                evidence=("该行：import %s；标准做法：业务只 import 接口，"
                          "由 provider 模块包 SDK" % pkg),
                symbol="%s:%s" % (kind, pkg)))
        if hit and not provider_dir_exists:
            out.append(Error(
                "P2", "thirdparty", rel,
                "没有 `%s/` 目录 —— 三方调用没有抽象层，测试无法注入替身"
                % PROVIDER_DIR,
                symbol="no-provider-dir"))
    return out


# ── D8.2 场景执行（有注入点才跑）───────────────────────────────────────────
def run_scenarios(ws, probe: dict, *, only=None) -> list[Error]:
    """跑 `probe.kind=="thirdparty"` 里的场景（**必须能注入替身**）。

    probe 形状：
        {"kind": "thirdparty", "provider": "payment",
         "module": "app.providers.payment",        # 注入点所在模块
         "entry": "app.services.order:pay",        # 业务入口（模块:函数）
         "scenarios": [{"name": "...", "kind": "success",
                        "args": [...], "stub": {...}, "expect": {...}}]}

    没有 `module`/`entry` → 报 P1"缺注入点或业务入口"（这就是 provider 模式的验收点）。
    """
    ws = Path(ws)
    out: list[Error] = []
    kind = str(probe.get("provider") or "thirdparty")
    mod_name = str(probe.get("module") or "")
    entry = str(probe.get("entry") or "")
    items = [s for s in (probe.get("scenarios") or []) if isinstance(s, dict)]
    if not items:
        return out
    if not mod_name or not entry:
        return [Error("P1", "thirdparty", "%s/%s" % (mod_name or "?", entry or "?"),
                      "缺注入点（module）或业务入口（entry）—— 第三方能力无法测；"
                      "按 provider 模式应提供 `set_provider()` 与可调用的业务函数",
                      symbol="%s:no-injection" % kind)]

    # 业务入口：`pkg.mod:func`
    if ":" not in entry:
        return [Error("P1", "thirdparty", entry, "业务入口格式应为 `模块:函数`",
                      symbol="%s:bad-entry" % kind)]
    emod, efunc = entry.split(":", 1)
    try:
        import sys
        if str(ws) not in sys.path:
            sys.path.insert(0, str(ws))
        prov = importlib.import_module(mod_name)
        biz = importlib.import_module(emod)
        fn = getattr(biz, efunc)
    except Exception as exc:                             # noqa: BLE001
        return [Error("P1", "thirdparty", entry,
                      "导入失败：%s: %s" % (type(exc).__name__, exc),
                      symbol="%s:import" % kind)]

    setter = getattr(prov, "set_provider", None)
    if not callable(setter):
        return [Error("P1", "thirdparty", mod_name,
                      "provider 模块没有 `set_provider()` —— 测试无法注入替身；"
                      "这是 provider 模式的硬要求",
                      symbol="%s:no-setter" % kind)]

    for sc in items:
        name = str(sc.get("name") or sc.get("kind") or "scenario")
        fake = FakeProvider(sc.get("stub") or {})
        try:
            setter(fake)
        except Exception as exc:                         # noqa: BLE001
            out.append(Error("P1", "thirdparty", mod_name,
                             "%s：注入替身失败（%s）" % (name, type(exc).__name__),
                             symbol="%s:%s" % (kind, name)))
            continue
        args = list(sc.get("args") or [])
        reps = max(1, int(sc.get("replay") or 1))
        last = None
        try:
            for _ in range(reps):
                last = fn(*args)
        except Exception as exc:                         # noqa: BLE001
            err_name = type(exc).__name__
            want = (sc.get("expect") or {}).get("raise")
            if want and str(want) in err_name:
                continue                                  # 期望抛错 → 通过
            out.append(Error("P2", "thirdparty", entry,
                             "%s：业务函数抛 %s（未预期）" % (name, err_name),
                             evidence=str(exc)[:300],
                             symbol="%s:%s" % (kind, name)))
            continue
        exp = sc.get("expect") or {}
        for k, v in exp.items():
            if k == "raise":
                continue
            got = None
            if isinstance(last, dict):
                got = last.get(k)
            elif hasattr(last, k):
                got = getattr(last, k)
            if got != v:
                out.append(Error("P2", "thirdparty", entry,
                                 "%s：期望 %s=%r，实际 %r" % (name, k, v, got),
                                 evidence=("替身调用记录：%s"
                                           % (fake.calls[:3] or "（未被调用）")),
                                 symbol="%s:%s:%s" % (kind, name, k)))
    try:
        setter(None)                                     # 还原
    except Exception:                                    # noqa: BLE001
        pass
    return out
