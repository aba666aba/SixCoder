"""声明式运维单元自检（2026-11-23）。

钉住：整组白名单 / 一次协商+一次人审覆盖整组 / 按序执行 / 健康检查 /
失败回滚 / 凭据不落盘 / 打包清理 / 主沙箱仍禁 ssh。

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

    from core import ops as o
    from core import deploy as d
    from core import approval as ap
    from core import permissions as pm

    def ok_consensus(role: str, prompt: str) -> dict:
        return {"ok": True, "answer": '{"decision":"approve","reason":"自检放行"}'}

    def reject_consensus(role: str, prompt: str) -> dict:
        return {"ok": True, "answer": '{"decision":"reject","reason":"审核者反对"}'}

    try:
        print("[1] 配置 + 整组白名单")
        d.save_targets([{
            "id": "prod", "host": "1.2.3.4", "user": "deploy", "auth": "agent",
            "allow_commands": ["git pull", "systemctl restart myapp",
                               "systemctl is-active myapp", "docker ps"],
            "workdir": "/srv/app", "enabled": True,
        }], enabled=True)
        o.save_units([{
            "id": "restart", "target": "prod", "enabled": True,
            "steps": [
                {"command": "git pull", "reason": "拉代码", "on_fail": "abort"},
                {"command": "systemctl restart myapp", "reason": "重启", "on_fail": "abort"},
            ],
            "health_check": "systemctl is-active myapp",
            "rollback": ["systemctl restart myapp"],
            "note": "重启应用",
        }], enabled=True)
        pub = o.public_config()
        u = pub["units"][0]
        check("运维单元已登记", u["id"] == "restart" and u["target_host"] == "1.2.3.4")
        check("单元对外无凭据字段", "credential" not in json.dumps(pub) and "password" not in json.dumps(pub))
        unit = o.find_unit("restart")
        ok, why = o._validate_all(d.find_target("prod"), unit)
        check("整组命令全在白名单", ok, why)
        bad = dict(unit)
        bad["steps"] = [{"command": "rm -rf /", "on_fail": "abort"}]
        ok2, why2 = o._validate_all(d.find_target("prod"), bad)
        check("越界命令整组拒绝", not ok2, why2)
        # 回滚命令也要过白名单
        bad2 = dict(unit)
        bad2["rollback"] = ["curl http://evil"]
        check("回滚命令也要过白名单", not o._validate_all(d.find_target("prod"), bad2)[0])

        print("[2] 一次协商 + 一次人审覆盖整组")
        calls = {"consensus": 0, "approval": 0}
        real_decide = o._rc.decide
        real_approval = o._approval.request

        def counted_decide(proposal, **kw):
            calls["consensus"] += 1
            return real_decide(proposal, **kw)

        def counted_request(*a, **kw):
            calls["approval"] += 1
            return real_approval(*a, **kw)

        o._rc.decide = counted_decide
        o._approval.request = counted_request

        results: list[dict] = []

        class Done:
            def __init__(self, rc=0, out="MOCK"):
                self.returncode, self.stdout, self.stderr = rc, out, ""

        def fake_run(argv, **kw):
            cmd = argv[-1]
            rc = 1 if "is-active" in cmd and "FAILHEALTH" in json.dumps(kw) else 0
            results.append({"cmd": cmd, "argv": argv, "env": kw.get("env", {})})
            return Done(rc, "MOCK_OK")

        orig_run = o.subprocess.run
        o.subprocess.run = fake_run

        def approve_emit(event, payload=None):
            if event == "approval_pending" and payload and payload.get("id"):
                ap.resolve(payload["id"], "approved", by="selfcheck")

        r = o.execute("restart", emit=approve_emit, consensus_fn=ok_consensus, timeout=6)
        o.subprocess.run = orig_run
        check("运维单元执行成功", r["ok"], r["why"])
        check("协商只调用一次", calls["consensus"] == 1, str(calls))
        check("人审只调用一次", calls["approval"] == 1, str(calls))
        check("步骤按序执行 2 条", len(r["steps"]) == 2)
        check("健康检查执行了", r["health"] is not None and r["health"]["ok"])
        check("成功时不跑回滚", len(r["rollback"]) == 0)
        check("每步审计落盘", (ROOT / ".sixcoder" / "audit.jsonl").exists())

        print("[3] 协商不过 / 人审不过 → 整组不执行")
        before = len(results)
        r2 = o.execute("restart", consensus_fn=reject_consensus, timeout=6)
        check("协商 2 票反对 → 拒绝", not r2["ok"] and "协商" in r2["why"], r2["why"])
        check("协商不过不执行任何命令", len(results) == before)
        check("协商不过 decision 为空（没惊动人审）", r2["decision"] == "")
        r3 = o.execute("restart", consensus_fn=ok_consensus, timeout=1)  # 没人批
        check("人审超时 → 拒绝", not r3["ok"] and r3["decision"] == "timeout", r3["why"])

        print("[4] 健康检查失败 → 自动回滚")
        o.subprocess.run = lambda argv, **kw: Done(0, "ok")
        # 让健康检查 rc=1：用 argv 判断 is-active 那条
        def health_fail_run(argv, **kw):
            rc = 1 if "is-active" in argv[-1] else 0
            return Done(rc, "INACTIVE" if rc else "MOCK")
        o.subprocess.run = health_fail_run
        r4 = o.execute("restart", emit=approve_emit, consensus_fn=ok_consensus, timeout=6)
        o.subprocess.run = orig_run
        check("健康检查失败 → 单元判失败", not r4["ok"])
        check("自动执行回滚", len(r4["rollback"]) >= 1 and r4["rollback"][0]["command"] == "systemctl restart myapp")
        check("失败原因说明已回滚", "回滚" in r4["why"], r4["why"])

        print("[5] 密码凭据：不落盘 / 不进 argv / 用完即焚")
        o.subprocess.run = fake_run
        # 给目标换成 password + prompt（一次性密码随 secret_fn 注入）
        d.save_targets([{
            "id": "prod", "host": "1.2.3.4", "user": "deploy", "auth": "password",
            "credential": "prompt",
            "allow_commands": ["git pull", "systemctl restart myapp", "systemctl is-active myapp"],
            "workdir": "/srv/app", "enabled": True,
        }], enabled=True)
        o.save_units([{
            "id": "restart", "target": "prod", "steps": [
                {"command": "git pull", "on_fail": "abort"}],
            "health_check": "", "rollback": [],
        }], enabled=True)
        cap: dict = {}

        def cap_run(argv, **kw):
            cap["argv"] = argv
            cap["env"] = dict(kw.get("env", {}))
            return Done(0, "ok")

        o.subprocess.run = cap_run
        r5 = o.execute("restart", emit=approve_emit, consensus_fn=ok_consensus,
                       secret_fn=lambda: "OpsP@ss123", timeout=6)
        o.subprocess.run = orig_run
        a = " ".join(cap.get("argv", []))
        check("密码目标执行成功", r5["ok"], r5["why"])
        check("密码不在 argv", "OpsP@ss" not in a)
        check("ssh 带 SSH_ASKPASS + force",
              cap.get("env", {}).get("SSH_ASKPASS") and
              cap.get("env", {}).get("SSH_ASKPASS_REQUIRE") == "force")
        check("密码不进返回值/审计", "OpsP@ss" not in json.dumps(r5))

        print("[6] 打包清理 + 主沙箱仍禁 ssh")
        bsrc = (ROOT.parent / "packaging" / "build_installer.py").read_text(encoding="utf-8")
        check("打包模板清空 ops 段", '"ops" in obj' in bsrc and '"units": []' in bsrc)
        check("打包硬闸检查运维单元", 'op.get("units")' in bsrc)
        sb = (ROOT / "core" / "sandbox.py").read_text(encoding="utf-8")
        check("主沙箱仍禁 ssh", "ssh|scp|sftp" in sb)
        capd = o.describe()
        check("能力声明：一次放行覆盖整组", capd["approval_scope"] == "unit")
        check("能力声明：自动回滚 + 健康检查", capd["rollback_on_fail"] and capd["health_check"])

        print("[7] readonly 禁用")
        pm.set_mode("readonly")
        rr = o.execute("restart", consensus_fn=ok_consensus, timeout=6)
        check("readonly 无 deploy.exec → 拒绝", not rr["ok"] and "模式" in rr["why"], rr["why"])
        pm.set_mode("client")

        # 恢复被 monkeypatch 的函数（避免影响同进程其它自检）
        o._rc.decide = real_decide
        o._approval.request = real_approval
    finally:
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
