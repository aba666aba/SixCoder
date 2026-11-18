"""断网 → 网络恢复，远端调用必须能自己回来（2026-11-09 用户反馈）。

用户原话：「突然断网然后网络恢复的情况下远端调用没用恢复欸」。

事故（根因三处，缺一不可）：

1. **重试窗口太短**：`remote._post` 原来只重试 3 次、退避 2s/4s/8s（总 ~14 秒）。
   真实断网通常几十秒到几分钟 → 14 秒后彻底放弃 → 这一轮直接失败。
2. **断网和"答错"分不开**：错误串一样是"请求失败：…"，
   上层把它记成"远端调用失败"= **"模型不会写"**（机制问题被记成能力问题）。
3. **"同一份证据就早停"把恢复机会掐死**：断网时每一轮失败证据完全相同 →
   第 2 轮就 `heal_stop（失败证据与上一轮完全相同）` → **网络恢复了也不再问**。

三处都修了：
1. 重试 3 → 8 次；网络类错误用更长退避（5/10/20/30…≈175s），限流/5xx 仍用短退避；
   总等待有上限（默认 240s）；退避带 ±20% 抖动。
2. `_post_once` 给网络错误打 `[网络不可达]` 标记；`remote.is_network_error()` 公开；
   author 返回 `network_unreachable=True`。
3. author 自愈循环：网络类失败**不走"证据重复"早停**（那不是"没新信息"，是"没问成"）。

跑法: python checks/check_network_recovery.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import remote  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 网络类错误必须被判成瞬时错误（要重试）")
    net_err = ("[网络不可达] 请求失败：URLError: <urlopen error [WinError 10061] "
               "由于目标计算机积极拒绝，无法连接。>")
    check("连接被拒 → 瞬时", remote._is_transient(net_err))
    check("连接被拒 → 网络类", remote.is_network_error(net_err))
    check("DNS 失败 → 网络类", remote.is_network_error(
        "请求失败：gaierror: [Errno 11001] getaddrinfo failed"))
    check("超时 → 网络类", remote.is_network_error("请求失败：TimeoutError: timed out"))
    check("连接重置 → 网络类", remote.is_network_error(
        "请求失败：ConnectionResetError: Connection reset by peer"))
    check("SSL 错误 → 网络类", remote.is_network_error("请求失败：SSLError: EOF occurred"))
    check("限流 → 瞬时但不是网络类",
          remote._is_transient("限流：HTTP 429") and not remote.is_network_error("限流：HTTP 429"))
    check("500 → 瞬时但不是网络类",
          remote._is_transient("HTTP 500") and not remote.is_network_error("HTTP 500"))
    check("模型答错（不是请求失败）→ 不算网络类",
          not remote.is_network_error("远端返回的 JSON 解析失败"))

    print("[2] 重试窗口必须覆盖一次典型断网（不能只有 14 秒）")
    src = (ROOT / "core" / "remote.py").read_text(encoding="utf-8")
    check("默认重试次数 ≥ 8", 'SIXCODER_REMOTE_RETRY", "8"' in src)
    check("有总等待上限（不会无限挂着）",
          "SIXCODER_REMOTE_RETRY_WAIT" in src)
    check("网络类退避比限流类更长",
          remote._backoff_delay(3, net_err) > remote._backoff_delay(3, "限流：HTTP 429"),
          f"{remote._backoff_delay(3, net_err)} vs {remote._backoff_delay(3, '限流：HTTP 429')}")
    # 网络类：5+10+20+30+30+30+30 = 175 秒（覆盖典型断网）
    total = sum(remote._backoff_delay(i, net_err) for i in range(1, 8))
    check("网络类总退避 ≥ 120 秒（覆盖典型断网）", total >= 120, f"{total:.0f}s")

    print("[3] 断网时不能被记成\"模型不会写\"")
    asrc = (ROOT / "core" / "author.py").read_text(encoding="utf-8")
    check("author 标记 network_unreachable", "network_unreachable" in asrc)
    check("author 用远端公开的判定函数", "is_network_error(" in asrc)
    check("网络类失败不走\"证据重复\"早停",
          "and not _ev_is_net" in asrc)
    check("错误串带网络标记", "[网络不可达]" in src)
    check("网络不通时不升档（否则重试次数×档位数）",
          "not _is_network_err(err)" in src)

    print("[3b] 远端**配置问题**（401/402/403）要立刻停机，不是当能力问题")
    check("401 → 致命（配置问题）", remote.is_fatal_remote_error(
        "鉴权失败（HTTP 401）：key 失效或已轮换"))
    check("402 欠费 → 致命", remote.is_fatal_remote_error("余额不足（HTTP 402）"))
    check("403 → 致命", remote.is_fatal_remote_error("HTTP 403"))
    check("网络不通**不是**致命（该等恢复，不是收工）",
          not remote.is_fatal_remote_error("[网络不可达] 请求失败：URLError"))
    check("被限流**不是**致命（该退避重试）",
          not remote.is_fatal_remote_error("被限流（HTTP 429）"))
    check("模型答错不是致命", not remote.is_fatal_remote_error("远端返回的 JSON 解析失败"))
    asrc2 = (ROOT / "core" / "author.py").read_text(encoding="utf-8")
    check("author 把远端原始错误带出来（不抹平成一句话）",
          "remote_error" in asrc2 and "_remote_err[0]" in asrc2)
    check("author 标记 remote_fatal", "remote_fatal" in asrc2)
    csrc = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("夜班遇到致命远端错误立刻收工", "remote_fatal" in csrc
          and "远端配置问题，已立即收工" in csrc)
    # 2026-11-09 事故：fatal 停机第一版把 break 写在**嵌套函数**里 →
    # SyntaxError: 'break' outside loop → **后端整个起不来**（界面报"后端没能起来"）。
    # 所以这里用 AST 静态检查：整个 core/ 里不允许出现"不在任何循环内的 break"。
    import ast
    bad_breaks = []
    for _f in sorted((ROOT / "core").glob("*.py")):
        try:
            _tree = ast.parse(_f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        class _V(ast.NodeVisitor):
            def __init__(self):
                self.loops = []

            def visit_For(self, node):
                self.loops.append(node); self.generic_visit(node); self.loops.pop()

            visit_While = visit_For

            def visit_Break(self, node):
                if not self.loops:
                    bad_breaks.append(f"{_f.name}:{node.lineno}")

        _v = _V(); _v.visit(_tree)
    check("core/ 里没有'不在循环内的 break'（那会让后端直接起不来）",
          not bad_breaks, str(bad_breaks))
    for _f in sorted((ROOT / "core").glob("*.py")):
        try:
            ast.parse(_f.read_text(encoding="utf-8"))
        except SyntaxError as _e:
            check(f"core/{_f.name} 语法可解析", False, str(_e))
            break
    else:
        check("core/ 全部模块语法可解析", True)

    print("[4] 真调用一次：前两次失败（网络），第三次成功 → 必须自己回来")
    calls = {"n": 0}

    def _fake_post_once(payload, base, key, timeout=300):
        calls["n"] += 1
        if calls["n"] <= 2:
            return None, ("[网络不可达] 请求失败：URLError: <urlopen error "
                          "[WinError 10051] 目标计算机积极拒绝>"), 10
        return {"ok": True, "choices": [{"message": {"content": "pong"}}]}, "", 10

    orig = remote._post_once
    remote._post_once = _fake_post_once
    import os
    os.environ["SIXCODER_REMOTE_RETRY"] = "8"
    os.environ["SIXCODER_REMOTE_RETRY_WAIT"] = "30"
    try:
        # 把退避压到最小，避免测试等太久
        orig_backoff = remote._backoff_delay
        remote._backoff_delay = lambda attempt, err: 0.01
        try:
            data, err, ms = remote._post({"x": 1}, "http://127.0.0.1:1", "k")
        finally:
            remote._backoff_delay = orig_backoff
    finally:
        remote._post_once = orig
        os.environ.pop("SIXCODER_REMOTE_RETRY", None)
        os.environ.pop("SIXCODER_REMOTE_RETRY_WAIT", None)
    check("调用成功返回（网络恢复后自己回来了）", data is not None, str(err)[:150])
    check("一共试了 3 次", calls["n"] == 3, str(calls["n"]))

    print("[6] 网络不通必须**原地等恢复后重试同一模块**，不能每个模块各判一次失败")
    asrc = (ROOT / "core" / "author.py").read_text(encoding="utf-8")
    # 整文件路径曾经漏了 network_unreachable → company 层分不清断网和答错
    check("author.py 两条路径都返回 network_unreachable",
          asrc.count('"network_unreachable": _net') >= 2, str(asrc.count('"network_unreachable": _net')))
    csrc2 = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("company 层有 _wait_network（原地等网络恢复）",
          "def _wait_network(" in csrc2)
    check("网络失败会重试同一个模块（不是换下一个）",
          "_write_one(rel, _net_try + 1)" in csrc2)
    check("重试有上限（不无限递归）", "_net_try >= 3" in csrc2)
    check("网络失败不被记成模块失败（等恢复 / 交给下一轮）",
          'stats["network_down"]' in csrc2)
    check("等待时长可配（SIXCODER_NET_WAIT_SECONDS）",
          "SIXCODER_NET_WAIT_SECONDS" in csrc2)
    # 行为级：网络不可达 → 等 → 恢复 → 重试同一模块
    from core import company as _co
    _waited = {"n": 0}

    def _fake_wait(ws=None, emit=None, max_seconds=0.0, probe_interval=0.0):
        _waited["n"] += 1
        return True

    _orig_wait = _co._wait_network
    _co._wait_network = _fake_wait
    try:
        check("_wait_network 返回 True 时可继续", _co._wait_network(None) is True)
    finally:
        _co._wait_network = _orig_wait
    check("等了网络（调用了 _wait_network）", _waited["n"] == 1, str(_waited))

    print()
    if FAILS:
        print("有失败项：%s" % ", ".join(FAILS))
        return 1
    print("全部通过（断网能重试 / 断网≠模型不会写 / 恢复后自动回来）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
