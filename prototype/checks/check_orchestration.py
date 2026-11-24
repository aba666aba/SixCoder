"""多机编排自检（2026-11-23，甜区收尾）。

钉住：多台目标 / 每台每条命令过白名单 / 一次协商+一次人审覆盖整批 /
按序+并发两种策略 / 单机失败隔离 / 回滚 / 凭据不落盘 / 打包清理 / 主沙箱仍禁 ssh。

用临时配置（SIXCODER_SETTINGS）隔离，monkeypatch approval / subprocess，绝不真连服务器。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:120]}" if detail and not ok else ""))


def main() -> int:
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tf.close()
    os.environ["SIXCODER_SETTINGS"] = tf.name
    os.environ["SIXCODER_APPROVAL"] = "web"

    from core import orchestration as orch
    from core import deploy as d
    from core import approval as ap
    from core import permissions as pm

    def ok_consensus(role: str, prompt: str) -> dict:
        return {"ok": True, "answer": '{"decision":"approve","reason":"自检放行"}'}

    def reject_consensus(role: str, prompt: str) -> dict:
        return {"ok": True, "answer": '{"decision":"reject","reason":"审核者反对"}'}

    def approve_emit(event, payload=None):
        if event == "approval_pending" and payload and payload.get("id"):
            ap.resolve(payload["id"], "approved", by="selfcheck")

    class Done:
        def __init__(self, rc=0, out="MOCK"):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    orig_run = orch.subprocess.run

    def setup_targets():
        d.save_targets([
            {"id": "h1", "host": "1.2.3.4", "user": "deploy", "auth": "agent",
             "allow_commands": ["git pull", "systemctl restart app", "systemctl is-active app"],
             "workdir": "/srv/app", "enabled": True},
            {"id": "h2", "host": "5.6.7.8", "user": "deploy", "auth": "agent",
             "allow_commands": ["git pull", "systemctl restart app", "systemctl is-active app"],
             "workdir": "/srv/app", "enabled": True},
            {"id": "h3", "host": "9.10.11.12", "user": "deploy", "auth": "agent",
             "allow_commands": ["git pull", "systemctl restart app", "systemctl is-active app"],
             "workdir": "/srv/app", "enabled": True},
        ], enabled=True)

    def setup_plan(strategy="sequential", fail_fast=False):
        orch.save_plans([{
            "id": "rollout", "targets": ["h1", "h2", "h3"], "strategy": strategy,
            "fail_fast": fail_fast, "enabled": True,
            "steps": [
                {"command": "git pull", "reason": "拉代码", "on_fail": "abort"},
                {"command": "systemctl restart app", "reason": "重启", "on_fail": "abort"},
            ],
            "health_check": "systemctl is-active app",
            "rollback": ["systemctl restart app"],
            "note": "三机滚动重启",
        }], enabled=True)

    try:
        print("[1] 配置 + 每台每条命令过白名单")
        setup_targets()
        setup_plan()
        pub = orch.public_config()
        p = pub["plans"][0]
        check("编排计划已登记 3 台", len(p["host_details"]) == 3 and p["targets"] == ["h1", "h2", "h3"])
        check("计划对外无凭据字段", "credential" not in json.dumps(pub) and "password" not in json.dumps(pub))
        plan = orch.find_plan("rollout")
        tgs = [d.find_target(t) for t in plan["targets"]]
        ok, why = orch._validate_plan(plan, tgs)
        check("三台命令全在白名单", ok, why)
        bad = dict(plan)
        bad["steps"] = [{"command": "rm -rf /", "on_fail": "abort"}]
        check("越界命令整批拒绝", not orch._validate_plan(bad, tgs)[0])
        bad2 = dict(plan)
        bad2["rollback"] = ["curl http://evil"]
        check("回滚命令也要过白名单", not orch._validate_plan(bad2, tgs)[0])
        # 有一台的命令不在它自己白名单 → 整批拒
        tgs_bad = list(tgs)
        tgs_bad[1] = {**tgs[1], "allow_commands": ["git pull"]}
        check("任一台白名单不含命令则整批拒", not orch._validate_plan(plan, tgs_bad)[0])

        print("[2] 一次协商 + 一次人审覆盖整批")
        calls = {"consensus": 0, "approval": 0}
        real_decide, real_approval = orch._rc.decide, orch._approval.request

        def counted_decide(proposal, **kw):
            calls["consensus"] += 1
            return real_decide(proposal, **kw)

        def counted_request(*a, **kw):
            calls["approval"] += 1
            return real_approval(*a, **kw)

        orch._rc.decide = counted_decide
        orch._approval.request = counted_request

        calls_run = {"n": 0}

        def recorder_run(argv, **kw):
            calls_run["n"] += 1
            return Done(0, "MOCK_OK")

        orch.subprocess.run = recorder_run
        r = orch.execute("rollout", emit=approve_emit, consensus_fn=ok_consensus, timeout=6)
        check("编排执行成功", r["ok"], r["why"])
        check("协商只调用一次", calls["consensus"] == 1, str(calls))
        check("人审只调用一次", calls["approval"] == 1, str(calls))
        check("三台都执行了", len(r["hosts"]) == 3 and all(h["ok"] for h in r["hosts"]))
        check("每台 2 步 + 1 健康检查 = 3 条命令", calls_run["n"] == 9, str(calls_run))
        check("策略记录为 sequential", r["strategy"] == "sequential")
        check("每步审计落盘", (ROOT / ".sixcoder" / "audit.jsonl").exists())

        print("[3] 协商不过 / 人审不过 → 整批不执行")
        before = calls_run["n"]
        r2 = orch.execute("rollout", consensus_fn=reject_consensus, timeout=6)
        check("协商 2 票反对 → 拒绝", not r2["ok"] and "协商" in r2["why"], r2["why"])
        check("协商不过不执行任何命令", calls_run["n"] == before)
        check("协商不过 decision 为空", r2["decision"] == "")
        r3 = orch.execute("rollout", consensus_fn=ok_consensus, timeout=1)
        check("人审超时 → 拒绝", not r3["ok"] and r3["decision"] == "timeout", r3["why"])
        check("人审不过不执行任何命令", calls_run["n"] == before)

        print("[4] 单机失败隔离：h2 重启失败，h1/h3 不受影响")
        def h2_fail_run(argv, **kw):
            tip = argv[-2] if len(argv) >= 2 else ""      # user@host
            cmd = argv[-1]
            if "5.6.7.8" in tip and "restart" in cmd:
                return Done(1, "RESTART_FAIL")
            return Done(0, "MOCK")

        orch.subprocess.run = h2_fail_run
        setup_plan(strategy="sequential", fail_fast=False)
        r4 = orch.execute("rollout", emit=approve_emit, consensus_fn=ok_consensus, timeout=6)
        by = {h["target"]: h for h in r4["hosts"]}
        check("编排判定失败", not r4["ok"])
        check("h2 失败", not by["h2"]["ok"])
        check("h1/h3 仍成功（失败隔离）", by["h1"]["ok"] and by["h3"]["ok"])
        check("h2 触发回滚", len(by["h2"]["rollback"]) >= 1)
        check("失败原因列出故障机", "h2" in r4["why"], r4["why"])

        print("[5] fail_fast：首台失败即停后续")
        calls_ff = {"n": 0}
        def h1_fail_run(argv, **kw):
            calls_ff["n"] += 1
            tip = argv[-2] if len(argv) >= 2 else ""
            cmd = argv[-1]
            if "1.2.3.4" in tip and "restart" in cmd:
                return Done(1, "FAIL")
            return Done(0, "MOCK")
        orch.subprocess.run = h1_fail_run
        setup_plan(strategy="sequential", fail_fast=True)
        r5 = orch.execute("rollout", emit=approve_emit, consensus_fn=ok_consensus, timeout=6)
        check("fail_fast 只跑到故障机就停", len(r5["hosts"]) == 1, str(len(r5["hosts"])))
        check("fail_fast 首台失败", not r5["hosts"][0]["ok"])

        print("[6] 并发策略：三台都执行")
        def par_run(argv, **kw):
            return Done(0, "MOCK")
        orch.subprocess.run = par_run
        setup_plan(strategy="parallel")
        r6 = orch.execute("rollout", emit=approve_emit, consensus_fn=ok_consensus, timeout=6)
        check("并发策略执行成功", r6["ok"], r6["why"])
        check("并发策略记录正确", r6["strategy"] == "parallel")
        check("并发三台都执行", len(r6["hosts"]) == 3 and all(h["ok"] for h in r6["hosts"]))

        print("[7] 密码凭据：不落盘 / 不进 argv / 用完即焚")
        d.save_targets([
            {"id": "h1", "host": "1.2.3.4", "user": "deploy", "auth": "password",
             "credential": "prompt", "allow_commands": ["git pull"],
             "workdir": "/srv/app", "enabled": True},
            {"id": "h2", "host": "5.6.7.8", "user": "deploy", "auth": "password",
             "credential": "prompt", "allow_commands": ["git pull"],
             "workdir": "/srv/app", "enabled": True},
        ], enabled=True)
        orch.save_plans([{
            "id": "rollout", "targets": ["h1", "h2"], "strategy": "parallel",
            "steps": [{"command": "git pull", "on_fail": "abort"}],
            "enabled": True,
        }], enabled=True)
        cap = {"argvs": []}
        def cap_run(argv, **kw):
            cap["argvs"].append(argv)
            cap.setdefault("envs", []).append(dict(kw.get("env", {})))
            return Done(0, "ok")
        orch.subprocess.run = cap_run
        r7 = orch.execute("rollout", emit=approve_emit, consensus_fn=ok_consensus,
                          secret_fn=lambda: "OrchP@ss456", timeout=6)
        orch.subprocess.run = orig_run
        all_argv = " ".join(" ".join(a) for a in cap.get("argvs", []))
        check("密码目标执行成功", r7["ok"], r7["why"])
        check("密码不在 argv", "OrchP@ss" not in all_argv)
        check("每台 ssh 带 SSH_ASKPASS + force",
              all(e.get("SSH_ASKPASS") and e.get("SSH_ASKPASS_REQUIRE") == "force"
                  for e in cap.get("envs", [])))
        check("密码不进返回值/审计", "OrchP@ss" not in json.dumps(r7))

        print("[8] 打包清理 + 主沙箱仍禁 ssh + 能力声明")
        bsrc = (ROOT.parent / "packaging" / "build_installer.py").read_text(encoding="utf-8")
        check("打包模板清空 orchestration 段",
              '"orchestration" in obj' in bsrc and '"plans": []' in bsrc)
        check("打包硬闸检查编排计划", 'orch.get("plans")' in bsrc)
        sb = (ROOT / "core" / "sandbox.py").read_text(encoding="utf-8")
        check("主沙箱仍禁 ssh", "ssh|scp|sftp" in sb)
        capd = orch.describe()
        check("能力声明：一次放行覆盖整批", capd["approval_scope"] == "plan")
        check("能力声明：失败隔离 + 回滚 + 健康检查",
              capd["failure_isolation"] and capd["rollback_on_fail"] and capd["health_check"])
        check("能力声明：两种策略", set(capd["strategies"]) == {"sequential", "parallel"})

        print("[9] readonly 禁用")
        setup_targets()
        setup_plan()
        pm.set_mode("readonly")
        rr = orch.execute("rollout", consensus_fn=ok_consensus, timeout=6)
        check("readonly 无 deploy.exec → 拒绝", not rr["ok"] and "模式" in rr["why"], rr["why"])
        pm.set_mode("client")

        orch._rc.decide = real_decide
        orch._approval.request = real_approval
    finally:
        orch.subprocess.run = orig_run
        try:
            os.unlink(tf.name)
        except OSError:
            pass

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
