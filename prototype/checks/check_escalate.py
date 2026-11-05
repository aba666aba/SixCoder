r"""远端升级自检：本地连续失败后必须**先问远端**，再决定是否判 stuck。

背景（实测）：`agent.py` 这条单任务链路（就是「发送」走的路）本地 7B 拿不出补丁时
只重试 3 次就判 stuck，没有夜班链路里的远端兜底。补上之后必须能证明：

  1. 本地失败 → 会打包并调用远端（事件 `escalate(stage=remote)`）；
  2. 远端方案能过本地闸门 → 节点 done，标注 by=remote（远端只出方案，判定仍归本地）；
  3. 远端也失败 → 节点 stuck，且失败原因里**写明远端试过了**（不能让人以为是没试）；
  4. 远端不可用 → 不假装升级，明确跳过原因；
  5. 开关关掉（SIXCODER_REMOTE_ESCALATE=0）→ 完全不碰远端。

这里不打真实网络：用 monkeypatch 替掉 remote.available/complete 与 handoff.apply_remote，
只验证"升级阶梯"这段控制流。真实远端的效果在 docs/16 有成本与命中记录。

跑法：python check_escalate.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import agent, handoff, remote  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


class Capture:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, kind, payload):
        self.events.append((kind, payload))

    def has(self, kind, **match):
        for k, p in self.events:
            if k != kind:
                continue
            if all(p.get(mk) == mv for mk, mv in match.items()):
                return True
        return False

    def kinds(self):
        return [k for k, _ in self.events]


def run_escalation(monkey_remote: dict, remote_ok: bool = True) -> tuple[Capture, dict]:
    """跑一遍 _run_node 的尾部升级逻辑（用一个只会失败的本地产出器）。"""
    cap = Capture()

    # 本地：让它必然失败（不产生编辑）
    orig_generate = agent._generate

    def boom(*_a, **_k):
        raise agent.tools.ToolError("补丁应用失败：符号级编辑失败：没有编辑")

    agent._generate = boom
    orig_avail, orig_complete = remote.available, remote.complete
    orig_apply = handoff.apply_remote
    orig_packet = handoff.build_packet
    orig_budget = agent.budget.plan

    try:
        if remote_ok:
            remote.available = lambda: (True, "")
            remote.complete = lambda prompt, **kw: {"ok": True, "answer": "```json\n{\"edits\":[]}\n```",
                                                    "usage": {}}
        else:
            remote.available = lambda: (False, "远端未配置 Key")
        handoff.build_packet = lambda task_id, nid: {"markdown": "# 远端协助请求（自检）",
                                                     "json": {}}
        handoff.apply_remote = lambda task_id, nid, answer: monkey_remote

        class CheapBudget:
            attempts = 1
            temps = [0.15]
            use_strong = False
            why = "自检：只跑一轮"

        agent.budget.plan = lambda *a, **k: CheapBudget()

        ws = Path(tempfile.mkdtemp(prefix="esc_ws_"))
        (ws / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        node = {"nid": "n-esc-1", "seq": 1, "kind": "impl", "path": "calc.py",
                "contract": "add(a, b) -> int", "mode": "symbol", "task_id": "t-esc"}
        res = agent._run_node(ws, "t-esc", node, "qwen2.5:7b", [], cap)
    finally:
        agent._generate = orig_generate
        remote.available, remote.complete = orig_avail, orig_complete
        handoff.apply_remote = orig_apply
        handoff.build_packet = orig_packet
        agent.budget.plan = orig_budget
    return cap, res


def main() -> int:
    print("[1] 远端可用且方案通过本地闸门 → 节点应 done(by=remote)")
    cap, res = run_escalation({"ok": True, "applied": ["calc.py"], "reason": ""})
    check("发起了远端升级", cap.has("escalate", stage="remote"), str(cap.kinds()))
    check("节点判通过", bool(res.get("ok")), str(res)[:160])
    check("标明是远端协助", res.get("by") == "remote", str(res)[:120])
    check("事件里也有 node_done(by=remote)", cap.has("node_done", by="remote"), str(cap.kinds()))

    print("[2] 远端方案没过闸门 → stuck，但原因里写明试过远端")
    cap2, res2 = run_escalation({"ok": False, "reason": "远端方案语法闸门未通过"})
    check("节点判 stuck", not res2.get("ok"), str(res2)[:120])
    check("原因里含远端信息", "远端" in str(res2.get("reason")), str(res2.get("reason"))[:160])
    check("node_failed 标记 remote_tried", cap2.has("node_failed", remote_tried=True),
          str(cap2.kinds()))

    print("[3] 远端不可用 → 不假装升级，明确跳过")
    cap3, res3 = run_escalation({}, remote_ok=False)
    check("节点仍是 stuck", not res3.get("ok"))
    check("没有 escalate(remote) 事件", not cap3.has("escalate", stage="remote"),
          str(cap3.kinds()))
    check("原因里写明跳过原因", "远端升级跳过" in str(res3.get("reason")),
          str(res3.get("reason"))[:160])

    print("[4] 开关关闭 → 完全不碰远端")
    os.environ["SIXCODER_REMOTE_ESCALATE"] = "0"
    try:
        check("开关读到 0", not agent._remote_escalation_enabled())
        cap4, res4 = run_escalation({"ok": True})
        check("没有远端事件", not cap4.has("escalate", stage="remote"), str(cap4.kinds()))
        check("节点 stuck", not res4.get("ok"))
    finally:
        os.environ.pop("SIXCODER_REMOTE_ESCALATE", None)
    check("开关恢复默认开启", agent._remote_escalation_enabled())

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（先问远端、通过算通过、不通过写明试过、不可用/关闭都不假装）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
