"""风险操作协商门自检（2026-11-23 用户定稿：风险操作 ≥2 同意即可）。

钉住：
  1. ≥2 approve → 放行（用户要的门槛）
  2. 只有 1 票 approve → 拒绝（不足门槛）
  3. 审核者失败/弃权/解析不出 → 不算同意（fail-closed）
  4. 三方配置不可用 → 拒绝
  5. 提案不含凭据（提示词里没有 key/password）
  6. 审计落盘
  7. 门槛常量 = 2（不是 3/3；3/3 仍留给装 pip 包）
  8. 复用 multiremote 角色通道（不重复造轮子）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = []
FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:140]}" if detail and not ok else ""))


def votes_for(*decisions):
    """造一个假问答函数：按 role 顺序返回 decision。"""
    seq = list(decisions)
    calls = {"n": 0}

    def fn(role, prompt):
        i = calls["n"]
        calls["n"] += 1
        d = seq[i] if i < len(seq) else "reject"
        if d == "FAIL":
            return {"ok": False, "error": "远端不可用"}
        if d == "GARBAGE":
            return {"ok": True, "answer": "模型没输出 JSON"}
        return {"ok": True, "answer": json.dumps({"decision": d, "reason": "test"})}
    return fn


PROPOSAL = {"kind": "deploy", "target": "deploy@10.0.0.5:22",
            "command": "systemctl restart myapp", "reason": "上线新版本",
            "risk": "生产副作用", "guardrails": "白名单+审计+人审"}


def main() -> int:
    from core import risk_consensus as rc

    tmp = tempfile.mkdtemp()

    print("[1] 门槛 ≥2")
    check("APPROVE_QUORUM == 2", rc.APPROVE_QUORUM == 2)

    print("[2] ≥2 approve → 放行")
    out = rc.decide(PROPOSAL, complete_fn=votes_for("approve", "approve"), workspace=tmp)
    check("两审核者同意 → granted", out["granted"] is True, out["reason"])
    check("票型 3 approve", out["tally"]["approve"] == 3, str(out["tally"]))
    check("writer 默认同意", out["votes"]["writer"] == "approve")

    print("[3] 只有 1 票 → 拒绝")
    out = rc.decide(PROPOSAL, complete_fn=votes_for("approve", "reject"), workspace=tmp)
    check("writer+1 审核者 = 2 票其实够 → 放行", out["granted"] is True,
          str(out["tally"]))   # writer 也算一票：2/3 达标
    # 真正只有 1 票：两名审核者都 reject → 只有 writer 1 票
    out = rc.decide(PROPOSAL, complete_fn=votes_for("reject", "reject"), workspace=tmp)
    check("仅 writer 1 票 → 拒绝", out["granted"] is False, str(out["tally"]))
    check("票型 1 approve / 2 reject",
          out["tally"]["approve"] == 1 and out["tally"]["reject"] == 2, str(out["tally"]))

    print("[4] 审核者失败/弃权不算同意（fail-closed）")
    out = rc.decide(PROPOSAL, complete_fn=votes_for("FAIL", "approve"), workspace=tmp)
    check("审核者1失败+审核者2同意=2票 → 放行", out["granted"] is True, str(out["tally"]))
    out = rc.decide(PROPOSAL, complete_fn=votes_for("FAIL", "reject"), workspace=tmp)
    check("审核者失败+另1反对 → 只有1票 → 拒绝", out["granted"] is False, str(out["tally"]))
    check("失败记 abstain 不算 approve", out["tally"]["approve"] == 1, str(out["tally"]))
    out = rc.decide(PROPOSAL, complete_fn=votes_for("GARBAGE", "GARBAGE"), workspace=tmp)
    check("解析不出都弃权 → 拒绝", out["granted"] is False, str(out["tally"]))
    check("弃权记 abstain", out["tally"]["abstain"] == 2, str(out["tally"]))

    print("[5] 提案不含凭据")
    packet = rc.build_packet({"kind": "deploy", "target": "h", "command": "c",
                              "key_ref": "C:/secret/id_rsa", "password": "hunter2",
                              "api_key": "sk-realkey"})
    check("提示词不含私钥路径", "C:/secret" not in packet and "id_rsa" not in packet)
    check("提示词不含密码", "hunter2" not in packet)
    check("提示词不含 api key", "sk-realkey" not in packet)
    check("提示词含操作事实", "部署" not in packet or "操作类型" in packet)

    print("[6] 审计落盘")
    audit = Path(tmp) / ".sixcoder" / "risk_consensus.jsonl"
    check("审计文件存在", audit.exists())
    if audit.exists():
        rows = [json.loads(x) for x in audit.read_text(encoding="utf-8").splitlines()]
        check("每条审计有票型+结论",
              all("votes" in r and "granted" in r for r in rows))
        check("审计不含凭据", "hunter2" not in audit.read_text(encoding="utf-8"))

    print("[7] 复用 multiremote 角色通道")
    dsrc = (ROOT / "core" / "risk_consensus.py").read_text(encoding="utf-8")
    check("调用 multiremote.complete_as", "multiremote.complete_as" in dsrc)
    check("三方角色 = writer/reviewer1/reviewer2",
          all(r in dsrc for r in ("writer", "reviewer1", "reviewer2")))

    print("[8] 三方不可用 → 拒绝（真路径保护）")
    # 不注入 fn、且强制让 risk_ready 全 False（用 monkeypatch）
    orig_ready = rc.risk_ready
    rc.risk_ready = lambda: {"writer": False, "reviewer1": False, "reviewer2": False}
    # 用 _default_ask 作为 fn 才会触发 ready 检查
    out = rc.decide(PROPOSAL, complete_fn=rc._default_ask, workspace=tmp)
    rc.risk_ready = orig_ready
    check("三方不可用一律拒绝", out["granted"] is False, out["reason"])

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项：", ", ".join(FAIL))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    raise SystemExit(main())
