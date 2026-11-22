"""外部真链路半自动：人填一次 → 会话内缓存 → 引擎后续自动代跑。

解决哪条红线（承接通用差距讨论）：
  · 验证码 / 短信 OTP / 第三方登录密码 / 扫码确认 / 支付确认——判据与凭据都在别人
    服务器上，引擎拿不到，全人工又太碎。这里做**半自动**：把"必须人介入"的那一下
    压缩成一次输入，之后由引擎在同一会话里自动跑完余下步骤。
  · 诚实边界：**不是全自验**。第一次人工不可避免；本模块只是让这一次之后不再反复叫人。
    缓存只在**当前进程内存**，不落盘（凭据落盘是之前踩过的坑），因此：
      - 夜班 CLI 进程与 Web 界面进程内存不共享——跨进程回填由上层 emit/回填通道负责，
        本模块只提供 `put()` 接口给那条通道调用，不自己偷偷落盘；
      - 进程重启即失效（fail-closed：重启后需要人重新填，绝不假装还记得）。

安全约定：
  · 值只进内存 `_STORE`，**绝不写文件/日志/审计/对外视图**；审计只记"某命名空间某键
    被存/取/清"，不记值本身；
  · TTL 到期自动失效，`uses` 用完自动消费（OTP 一次、登录态可多次）；
  · 空值 / 非字符串 / 超长值拒绝，防误存垃圾与内存膨胀；
  · 命名空间与键做白名单字符校验，防上层拼出奇怪 key。
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,80}$")
_MAX_VALUE = 4096
_DEFAULT_TTL = 15 * 60.0          # 15 分钟；OTP 类应更短，由调用方覆盖
# 进程代次（2026-11-23）：缓存只在内存，重启即空。给前端一个可展示的标记，
# 让"为什么刚填的值没了"变成一句明确提示，而不是让人以为引擎忘了。
_BOOT_ID = uuid.uuid4().hex[:12]
_BOOT_AT = time.time()


class NeedHuman(Exception):
    """引擎跑到一个"必须人给"的坑，缓存里没有 → 抛此异常，请上层叫人填一次。

    携带 namespace/key/hint，UI 据此弹出输入框；人填完调 `put()` 后重跑。
    """
    def __init__(self, namespace: str, key: str, hint: str = ""):
        self.namespace = namespace
        self.key = key
        self.hint = hint
        super().__init__(f"需要人工提供一次：{namespace}/{key}" + (f"（{hint}）" if hint else ""))

    def as_dict(self) -> dict[str, str]:
        return {"namespace": self.namespace, "key": self.key, "hint": self.hint,
                "kind": "need_human"}


@dataclass
class _Entry:
    value: str
    expires: float
    left: int                      # 剩余可用次数；<=0 视为已消费
    created: float = field(default_factory=time.time)


_STORE: dict[tuple[str, str], _Entry] = {}
_LOCK = threading.RLock()
_AUDIT: list[dict] = []             # 内存审计（不落盘，值不进来）


def _norm(namespace: str, key: str) -> tuple[str, str]:
    ns, k = str(namespace or "").strip(), str(key or "").strip()
    if not _KEY_RE.match(ns) or not _KEY_RE.match(k):
        raise ValueError("命名空间/键含非法字符")
    return ns, k


def _audit(action: str, namespace: str, key: str) -> None:
    # 只记事件，不记值
    _AUDIT.append({"ts": round(time.time(), 3), "action": action,
                   "namespace": namespace, "key": key})


def put(namespace: str, key: str, value: str, *,
        ttl: float = _DEFAULT_TTL, uses: int = 1) -> dict[str, Any]:
    """人填一次的值存入会话缓存。uses=1（OTP 一次消费）/ >1（登录态可复用）。

    返回不含值的回执。value 为空/非字符串/超长拒绝。
    """
    ns, k = _norm(namespace, key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError("值不能为空")
    v = value.strip()
    if len(v) > _MAX_VALUE:
        raise ValueError("值过长")
    try:
        ttl = max(1.0, min(24 * 3600.0, float(ttl)))
        uses = max(1, min(10000, int(uses)))
    except (TypeError, ValueError):
        raise ValueError("ttl/uses 非法")
    with _LOCK:
        _STORE[(ns, k)] = _Entry(value=v, expires=time.time() + ttl, left=uses)
        _audit("put", ns, k)
    return {"namespace": ns, "key": k, "ttl": ttl, "uses": uses, "stored": True}


def has(namespace: str, key: str) -> bool:
    ns, k = _norm(namespace, key)
    with _LOCK:
        e = _STORE.get((ns, k))
        if e is None:
            return False
        if e.expires <= time.time() or e.left <= 0:
            _STORE.pop((ns, k), None)
            return False
        return True


def get(namespace: str, key: str, *, consume: bool = True) -> str:
    """引擎取值。默认取一次扣一次（OTP）；consume=False 用于可复用登录态。

    缺失 / 过期 / 已消费 → 抛 NeedHuman（fail-closed，绝不返回空串假装成功）。
    """
    ns, k = _norm(namespace, key)
    with _LOCK:
        e = _STORE.get((ns, k))
        if e is None or e.expires <= time.time() or e.left <= 0:
            _STORE.pop((ns, k), None)
            _audit("miss", ns, k)
            raise NeedHuman(ns, k)
        val = e.value
        if consume:
            e.left -= 1
            if e.left <= 0:
                _STORE.pop((ns, k), None)
        _audit("get", ns, k)
        return val


def require(namespace: str, key: str, *, hint: str = "", consume: bool = True) -> str:
    """`get()` 的人性化版：缺失时抛带 hint 的 NeedHuman，UI 可直接用。"""
    ns, k = _norm(namespace, key)
    if not has(ns, k):
        _audit("need", ns, k)
        raise NeedHuman(ns, k, hint)
    return get(ns, k, consume=consume)


def clear(namespace: str, key: str) -> bool:
    ns, k = _norm(namespace, key)
    with _LOCK:
        existed = _STORE.pop((ns, k), None) is not None
        _audit("clear", ns, k)
        return existed


def clear_all() -> int:
    with _LOCK:
        n = len(_STORE)
        _STORE.clear()
        _audit("clear_all", "*", "*")
        return n


def pending() -> list[dict[str, Any]]:
    """会话内已缓存项（**不含值**，只给命名空间/键/剩余次数/剩余秒）。"""
    now = time.time()
    with _LOCK:
        expired = [k for k, e in _STORE.items() if e.expires <= now or e.left <= 0]
        for k in expired:
            _STORE.pop(k, None)
        return [{"namespace": ns, "key": k, "uses_left": e.left,
                 "ttl_left": round(max(0.0, e.expires - now), 1)}
                for (ns, k), e in _STORE.items()]


def audit_log() -> list[dict]:
    """内存审计副本（不含值），给自检/UI 查看，不落盘。"""
    with _LOCK:
        return [dict(x) for x in _AUDIT]


def run_steps(steps: list[dict], executor: Callable[[dict, str], Any], *,
              namespace: str = "flow") -> dict[str, Any]:
    """半自动执行器：把"人填一次 + 后续自动"的步骤表跑完。

    每个 step：
      · `{ "kind":"human", "key":..., "hint":..., "slot":"<注入名>" }`
        从缓存取值（缺 → NeedHuman），把值以 `executor(step, value)` 交给外部动作；
      · `{ "kind":"auto", "action":... }` 直接 `executor(step, "")`。
    `executor` 由调用方注入（本模块不碰真实第三方，保持无副作用）；返回值并入结果。
    任一步抛 NeedHuman 时**立即停下**并回报，人补齐后可从该步继续（前面的 auto 步骤
    由调用方保证幂等）。
    """
    out: list[dict] = []
    for i, step in enumerate(steps or []):
        kind = str(step.get("kind") or "auto")
        try:
            if kind == "human":
                key = str(step.get("key") or f"step{i}")
                val = require(namespace, key, hint=str(step.get("hint") or ""))
                out.append({"i": i, "kind": kind, "ok": True,
                            "result": executor(step, val)})
            else:
                out.append({"i": i, "kind": "auto", "ok": True,
                            "result": executor(step, "")})
        except NeedHuman as nh:
            _audit("pause", namespace, str(step.get("key") or f"step{i}"))
            return {"ok": False, "paused_at": i, "need": nh.as_dict(), "steps": out}
        except Exception as exc:                          # noqa: BLE001
            out.append({"i": i, "kind": kind, "ok": False,
                        "error": f"{type(exc).__name__}: {exc}"})
            return {"ok": False, "paused_at": i, "error": out[-1]["error"], "steps": out}
    return {"ok": True, "steps": out}


def describe() -> dict[str, Any]:
    return {
        "storage": "memory-only",
        "persisted": False,
        "cross_process": False,
        "cross_process_note": "跨进程回填由上层 emit/回填通道调 put()；本模块不落盘",
        "default_ttl": _DEFAULT_TTL,
        "fail_closed_on_missing": True,
        "audit_excludes_values": True,
        "full_auto_claimed": False,
        "boot_id": _BOOT_ID,             # 本次进程代次；变了说明服务重启过
        "booted_at": round(_BOOT_AT, 1),
        "survives_restart": False,       # 诚实：重启后已填值全部失效，需重新提供
        "restart_hint": "服务重启后已填值会清空（不落盘是安全红线），重新填一次即可",
    }
