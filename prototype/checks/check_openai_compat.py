"""自检：默认 DeepSeek、可选 OpenAI 兼容格式（0 花费、不发一次真远端）。

钉住的事：
1. 什么都不配 → 协议仍是 deepseek，payload 逐字保持原样（thinking + reasoning_effort）
2. compat=openai → 按智谱官方格式：thinking 开关、不发 reasoning_effort /
   response_format，max_tokens 夹到 65536
3. 档案级 compat 优先于全局，非法值收敛为「未设置」而不是乱猜协议
4. available()：默认 / openai 放行，未知协议明确拒绝（不静默回落）
5. ladder / public_remote 把 compat 带出来，设置填了才真的生效
6. 端到端：complete() 真正交给 _post 的 payload 也是 OpenAI 格式

跑法: python checks/check_openai_compat.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import guards, remote, settings_store as st, tokens  # noqa: E402

FAILS: list[str] = []


def ok(cond: bool, msg: str) -> None:
    if cond:
        print("  OK  ", msg)
    else:
        FAILS.append(msg)
        print("  FAIL", msg)


TMP = Path(tempfile.mkdtemp(prefix="sixcoder_compat_"))
_saved = {"settings": st.SETTINGS, "key": remote._key, "post": remote._post,
          "guard": guards.gate_remote_call, "needs": tokens.needs_compression,
          "observe": tokens.observe, "cost": remote.cost_cny}
_POSTED: list[dict] = []


def set_remote(**kw) -> None:
    st.SETTINGS.write_text(json.dumps({"remote": kw}, ensure_ascii=False),
                           encoding="utf-8")


def payload(effort="max", *, r=None, step=None, json_mode=True) -> dict:
    base_step = {"model": "some-model", "effort": effort, "temperature": 0.2,
                 "max_output_tokens": 0, "base_url": "", "api_key": ""}
    base_step.update(step or {})
    p, _, _ = remote._payload_for(base_step, r or {}, [{"role": "user", "content": "x"}],
                                  json_mode)
    return p


try:
    st.SETTINGS = TMP / "settings.json"
    remote._key = lambda: "fake-key"      # 只为过 available()，不读真配置
    guards.gate_remote_call = lambda *a, **k: {"ok": True}
    tokens.needs_compression = lambda t: (False, {})
    tokens.observe = lambda *a, **k: None
    remote.cost_cny = lambda *a, **k: 0.0

    print("[1] 默认必须是 DeepSeek，且行为一字不改")
    st.SETTINGS.unlink(missing_ok=True)
    ok(remote.compat_of(r=st.get_remote()) == "deepseek", "未配置时协议 = deepseek")
    p = payload("max", r=st.get_remote())
    ok(p.get("thinking") == {"type": "enabled"} and p.get("reasoning_effort") == "max",
       "max 档仍发 thinking + reasoning_effort=max")
    p0 = payload("none", r=st.get_remote())
    ok(p0.get("thinking") == {"type": "disabled"} and "reasoning_effort" not in p0,
       "none 档仍发 thinking=disabled、不发 reasoning_effort")
    ok(p.get("response_format") == {"type": "json_object"},
       "json_mode 仍发 response_format")
    ok(bool(remote.available()[0]), "默认 available() 放行")

    print("[2] compat=openai 走 OpenAI 格式")
    set_remote(compat="openai", api_key="fake")
    r = st.get_remote()
    ok(remote.compat_of(r=r) == "openai", "全局 compat 生效")
    po = payload("max", r=r)
    ok(po.get("thinking") == {"type": "enabled"}, "max 档发 thinking=enabled")
    ok("reasoning_effort" not in po, "兼容端点不认 reasoning_effort，一律不发")
    ok(payload("low", r=r).get("thinking") == {"type": "enabled"},
       "low 档同样开思考")
    pn = payload("none", r=r)
    ok(pn.get("thinking") == {"type": "disabled"}
       and "reasoning_effort" not in pn, "none 档 thinking=disabled")
    ok("response_format" not in po, "按官方格式不发 response_format")
    ok(1 <= int(po.get("max_tokens") or 0) <= 65536,
       "max_tokens 夹到 65536（DS 的 33 万会 400）")
    ok(bool(remote.available()[0]), "openai 协议 available() 放行")

    print("[3] 档案级优先 / 非法值收敛")
    ok("reasoning_effort" not in payload("max", r={"compat": "deepseek"},
                                         step={"compat": "openai"}),
       "档案 compat=openai 覆盖全局 deepseek")
    ok(payload("max", r={"compat": "openai"},
               step={"compat": "deepseek"}).get("reasoning_effort") == "max",
       "档案 compat=deepseek 覆盖全局 openai")
    prof = st._norm_model_profile({"id": "x", "model": "m", "compat": "openai"})
    ok(prof.get("compat") == "openai", "档案规范化保留合法 compat")
    ok(st._norm_model_profile({"id": "x", "model": "m",
                               "compat": "anthropic"}).get("compat") == "",
       "非法 compat 收敛为「未设置」，不猜协议")

    print("[4] ladder / 对外视图把 compat 带出来")
    set_remote(compat="openai", api_key="fake", local_first=False,
               models=[{"id": "o", "model": "gpt-4o", "efforts": ["none", "high"]}])
    steps = st.ladder()
    ok(len(steps) >= 1 and all(s.get("compat") == "openai" for s in steps),
       "全局 compat 传进阶梯每一档")
    ok(st.public_remote().get("compat") == "openai", "对外视图暴露 compat")
    set_remote(compat="deepseek", api_key="fake", local_first=False,
               models=[{"id": "o", "model": "gpt-4o", "compat": "openai",
                        "efforts": ["none"]},
                       {"id": "d", "model": "deepseek-flash", "efforts": ["none"]}],
               active_model="o")
    ok(all(s.get("compat") == "openai" for s in st.ladder()),
       "选中的第三方档案走 openai，其余档案不受影响")

    print("[5] 未知协议明确拒绝，不静默回落")
    set_remote(compat="anthropic", api_key="fake")
    ok(not remote.available()[0] and "不支持" in remote.available()[1],
       "未知 compat 被 available() 拒绝并说明")

    print("[6] 端到端：complete() 真正发出去的也是 OpenAI 格式")
    set_remote(compat="openai", api_key="fake", local_first=False,
               models=[{"id": "o", "model": "gpt-4o", "compat": "openai",
                        "efforts": ["high"]}])

    def fake_post(pl, base, key, timeout=300):
        _POSTED.append(pl)
        return ({"choices": [{"message": {"content": "ok"},
                             "finish_reason": "stop"}],
                 # 正文要 >32 token，否则会命中 complete() 既有的「配额被思考吃光 →
                 # 放大配额重发一次」逻辑，假远端就会收到两次请求
                 "usage": {"prompt_tokens": 100, "completion_tokens": 100}}, "", 1)

    remote._post = fake_post
    res = remote.complete("hello", task_id="check", node_id="compat")
    ok(bool(res.get("ok")) and len(_POSTED) == 1, "假远端收到一次请求")
    sent = _POSTED[0]
    ok(sent.get("thinking") == {"type": "enabled"}
       and "reasoning_effort" not in sent
       and 1 <= int(sent.get("max_tokens") or 0) <= 65536,
       "实际请求体符合官方格式（thinking / 65536 / 无 reasoning_effort）")
    ok(sent.get("model") == "gpt-4o", "实际请求用档案里的模型名")

    print("[7] 短回复不触发「配额被吃光」放大重试")
    _POSTED.clear()

    def short_post(pl, base, key, timeout=300):
        _POSTED.append(pl)
        return ({"choices": [{"message": {"content": "好"},
                             "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 2,
                           "completion_tokens_details": {"reasoning_tokens": 0}}}, "", 1)

    remote._post = short_post
    res2 = remote.complete("回复一个字：好", task_id="check", node_id="short")
    ok(bool(res2.get("ok")) and len(_POSTED) == 1,
       "无 reasoning 的 2-token 短回复只发一次，不放大配额")

    print("[8] 单档直通：不升档 / 不降阶 / 不按难度路由")
    ok(st.single_effort() is True, "OpenAI 兼容端点判为单档")
    from core import effort as _eff
    ok(_eff.pick([{"effort": "none"}], "max") == 0, "effort.pick 单档直接返回 0")
    _POSTED.clear()
    remote._post = fake_post
    r3 = remote.complete("x", task_id="check", node_id="single",
                         step_index=3, thinking="on")
    ok(bool(r3.get("ok")) and len(_POSTED) == 1
       and _POSTED[0].get("model") == "gpt-4o",
       "忽略 step_index / thinking，只用唯一档发一次")
    _POSTED.clear()

    def transient(pl, base, key, timeout=300):
        _POSTED.append(pl)
        return None, "HTTP 429 限流", 1

    remote._post = transient
    r4 = remote.complete("x", task_id="check", node_id="single429")
    ok(not r4.get("ok") and len(_POSTED) == 1,
       "瞬态错误不换档重试，只发一次并如实返回")
    remote._post = fake_post

    set_remote(api_key="fake", local_first=False,
               models=[{"id": "ds", "model": "deepseek-flash",
                        "efforts": ["none", "low", "high", "max"]}])
    ok(st.single_effort() is False, "DS 四档阶梯仍走原来的升档 / 难度路由")

finally:
    st.SETTINGS = _saved["settings"]
    remote._key = _saved["key"]
    remote._post = _saved["post"]
    guards.gate_remote_call = _saved["guard"]
    tokens.needs_compression = _saved["needs"]
    tokens.observe = _saved["observe"]
    remote.cost_cny = _saved["cost"]
    shutil.rmtree(TMP, ignore_errors=True)

if FAILS:
    print("\n".join("FAIL " + m for m in FAILS))
    raise SystemExit(1)
print("全部通过（默认 deepseek / 可选 openai / 档案级覆盖 / 端到端请求体）")
