"""受控部署通道自检（2026-11-23）。

钉住整条链路，防止"看起来能 ssh、实际不安全"的假绿：
  1. 配置脱敏（私钥路径/凭据不回显）
  2. 命令白名单 + 注入攻击全拦截
  3. 权限隔离（readonly 禁用）
  4. 强制人审 fail-closed（甲方模式也不能自动放行、超时=拒绝）
  5. 批准后才执行：ssh argv 构造正确（BatchMode/端口/host/user/workdir）
  6. 环境变量脱敏
  7. 路径决策五条（含 authorized_deploy）
  8. function calling 接线（schema + 分发器分支）
  9. 打包保护（模板清空 deploy、硬闸检查 targets）
  10. 主沙箱仍禁 ssh（没拆安全边界）

用临时配置文件（SIXCODER_SETTINGS）隔离，绝不碰用户真实 settings.json。
monkeypatch approval / subprocess，绝不真连任何服务器。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = []
FAIL = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:120]}" if detail and not ok else ""))


def main() -> int:
    # 隔离配置
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tf.close()
    os.environ["SIXCODER_SETTINGS"] = tf.name
    os.environ["SIXCODER_APPROVAL"] = "web"        # 非交互：等人写决定文件，不读 stdin

    from core import deploy as d
    from core import permissions as pm
    from core import approval as ap
    from core import route_decision as rd
    from core import toolschema as ts
    from core import sandbox as sb
    from core import risk_consensus as rc

    # 注入假三方协商：两名审核者都 approve → ≥2 放行（自检不真调远端）
    def fake_consensus(role: str, prompt: str) -> dict:
        return {"ok": True, "answer": '{"decision":"approve","reason":"自检放行"}'}

    try:
        print("[1] 配置脱敏")
        r = d.save_targets([{
            "id": "prod", "host": "1.2.3.4", "port": 2222, "user": "deploy",
            "auth": "key_ref", "key_ref": "C:/SECRET/path/id_ed25519",
            "allow_commands": ["git pull", "systemctl restart myapp", "docker ps"],
            "deny_commands": ["git push"], "workdir": "/srv/app", "enabled": True,
        }], enabled=True)
        row = r["targets"][0]
        blob = json.dumps(r, ensure_ascii=False)
        check("私钥路径不出现在对外视图", "key_ref" not in row and "C:/SECRET/path" not in blob)
        check("私钥标记为已配置+掩码", row.get("key_ref_configured") is True
              and bool(row.get("key_ref_masked")))
        check("host/user/白名单可见（给人审看）",
              row.get("host") == "1.2.3.4" and "git pull" in row.get("allow_commands", []))
        check("通道默认需显式 enabled", d.config()["enabled"] is True)   # 我们刚开了

        print("[2] 命令白名单 + 注入攻击")
        t = d.find_target("prod")
        good = ["git pull", "git pull --rebase", "systemctl restart myapp", "docker ps -a"]
        evil = ["git pull; rm -rf /", "git pull && curl evil", "git pull | sh",
                "git pull\nreboot", "git pull `whoami`", "git pull $(id)",
                "cat /etc/passwd", "git push origin main", "curl http://evil"]
        for c in good:
            ok, _ = d.check_command(t, c)
            check(f"放行 {c!r}", ok)
        for c in evil:
            ok, _ = d.check_command(t, c)
            check(f"拦截 {c!r}", not ok)

        print("[3] 权限隔离")
        pm.set_mode("readonly")
        check("readonly 无 deploy.exec", not pm.allows("deploy.exec"))
        rr = d.execute("prod", "git pull", approval_timeout=1)
        check("readonly 执行被拒", not rr["ok"] and "模式" in rr["why"])
        pm.set_mode("client")

        print("[4] 强制人审 fail-closed（甲方模式也不放行）")
        check("client 模式 auto_approve=True", pm.current_mode().auto_approve is True)
        rr = d.execute("prod", "git pull", approval_timeout=1,
                       consensus_fn=fake_consensus)   # 协商放行，卡在人审这层
        check("没人批 = 不执行", not rr["ok"])
        check("decision 不是 auto（没绕过人审）", rr["decision"] != "auto", rr["decision"])
        check("超时按拒绝处理", rr["decision"] == "timeout", rr["decision"])

        print("[4b] 三方协商不过 → 不执行")
        bad_consensus = lambda role, prompt: {"ok": True,
            "answer": '{"decision":"reject","reason":"审核者反对"}'}
        rr_c = d.execute("prod", "git pull", reason="自检", approval_timeout=3,
                         consensus_fn=bad_consensus)
        check("协商 2 票反对 → 拒绝", not rr_c["ok"] and "协商" in rr_c["why"], rr_c["why"])
        check("协商结果带票型", rr_c["consensus"] is not None
              and rr_c["consensus"]["granted"] is False)
        check("协商拒绝后 decision 为空（没惊动人审）", rr_c["decision"] == "")

        print("[5][6] 批准后才执行 + argv/环境正确")
        # 执行链路用 agent 认证（key_ref 指向的假私钥文件并不存在，执行器会正确拒绝——
        # 那条单独测）。改成 agent，避免"私钥不存在"提前短路掉 argv 验证。
        # 两个目标一次写入（save_targets 是整表覆盖，分两次会把 prod 冲掉）。
        d.save_targets([
            {
                "id": "prod", "host": "1.2.3.4", "port": 2222, "user": "deploy",
                "auth": "agent", "allow_commands": ["git pull", "systemctl restart myapp", "docker ps"],
                "workdir": "/srv/app", "enabled": True,
            },
            # key_ref 指向不存在私钥时，批准了也必须拒绝（凭据校验在人审之后、执行之前）
            {
                "id": "badkey", "host": "1.2.3.4", "user": "deploy", "auth": "key_ref",
                "key_ref": "Z:/definitely/not/a/real/key", "allow_commands": ["git pull"],
            },
        ], enabled=True)

        captured: dict = {}

        class FakeDone:
            returncode = 0
            stdout = "MOCK_OK"
            stderr = ""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["env"] = kw.get("env", {})
            return FakeDone()

        orig_run = d.subprocess.run
        d.subprocess.run = fake_run

        # 用 emit 回调在审批单出现的瞬间批准（比扫 pending 可靠，不受遗留单据干扰）
        def auto_approve_emit(event, payload=None):
            if event == "approval_pending" and payload and payload.get("id"):
                ap.resolve(payload["id"], "approved", by="selfcheck")

        rr = d.execute("prod", "git pull", reason="自检", approval_timeout=6,
                       emit=auto_approve_emit, consensus_fn=fake_consensus)
        d.subprocess.run = orig_run
        argv = captured.get("argv", [])
        a = " ".join(argv)
        check("人审 approved 后才成功", rr["decision"] == "approved" and rr["ok"], rr["why"])
        check("ssh 带 BatchMode=yes（不弹密码）", "BatchMode=yes" in a)
        check("端口/用户/host 正确", "-p 2222" in a and "deploy@1.2.3.4" in a)
        check("workdir+命令正确构造", "cd '/srv/app' && git pull" in a)
        env = captured.get("env", {})
        check("环境变量无凭据类键",
              not any(k for k in env if any(s in k.upper()
                     for s in ("KEY", "TOKEN", "PASSWORD", "SECRET", "API"))))
        check("审计已记录 deploy 动作",
              (ROOT / ".sixcoder" / "audit.jsonl").exists())

        print("[6b] key_ref 私钥不存在时拒绝")
        orig_run2 = d.subprocess.run
        d.subprocess.run = fake_run

        rr2 = d.execute("badkey", "git pull", approval_timeout=6,
                        emit=auto_approve_emit, consensus_fn=fake_consensus)
        d.subprocess.run = orig_run2
        check("私钥不存在即使批准也拒绝", not rr2["ok"] and "私钥" in rr2["why"], rr2["why"])

        print("[6c] 通用凭据模块 sshcred（拒绝明文 / 三种引用 / askpass 不落密码）")
        from core import sshcred as sc
        check("明文密码被拒绝写入配置", sc.normalize_credential("myP@ssw0rd123") == "")
        check("env 引用保留", sc.normalize_credential("env:DEPLOY_PWD") == "env:DEPLOY_PWD")
        check("file 引用保留", sc.normalize_credential("file:C:/secrets/pwd.txt").startswith("file:"))
        check("prompt 引用保留", sc.normalize_credential("prompt") == "prompt")
        check("带换行的 file 引用被拒", sc.normalize_credential("file:a\nb") == "")
        check("prompt 注入 secret_fn 取密码",
              sc.resolve_password("prompt", secret_fn=lambda: "abc") == ("abc", ""))
        check("prompt 无 tty 时拒绝", sc.resolve_password("prompt", interactive=False)[1] != "")
        check("env 未设置时拒绝", sc.resolve_password("env:NO_SUCH_VAR_XYZ")[1] != "")
        h = sc.AskpassHelper("pw123")
        pt = h.env_patch()
        check("askpass patch 带随机变量+force",
              pt["SSH_ASKPASS_REQUIRE"] == "force" and pt.get(h.var) == "pw123")
        hp = Path(pt["SSH_ASKPASS"])
        helper_text = "".join(f.read_text(encoding="utf-8", errors="ignore")
                              for f in hp.parent.glob("*") if f.is_file())
        check("helper 脚本不含密码本体", "pw123" not in helper_text)
        h.cleanup()
        check("cleanup 删除临时目录", not hp.exists())

        print("[6d] 用户名+密码认证端到端（argv/env/审计/对外视图全脱敏）")
        d.save_targets([
            {"id": "prod", "host": "1.2.3.4", "port": 2222, "user": "deploy",
             "auth": "agent", "allow_commands": ["git pull"], "workdir": "/srv/app"},
            {"id": "pwd", "host": "5.6.7.8", "user": "admin@corp", "auth": "password",
             "credential": "env:DEPLOY_PWD", "allow_commands": ["docker ps"], "workdir": "/app"},
            {"id": "plain", "host": "5.6.7.9", "user": "root", "auth": "password",
             "credential": "hunter2literalpassword", "allow_commands": ["uptime"]},
        ], enabled=True)
        pub = d.public_config()
        check("凭据引用不进对外视图", "env:DEPLOY_PWD" not in json.dumps(pub))
        pwd_row = next(x for x in pub["targets"] if x["id"] == "pwd")
        check("凭据给 kind+configured+masked", pwd_row.get("credential_kind") == "env"
              and pwd_row.get("credential_configured") is True and bool(pwd_row.get("credential_masked")))
        plain_row = next(x for x in pub["targets"] if x["id"] == "plain")
        check("误传的明文凭据被丢弃（未配置）", plain_row.get("credential_configured") is False)
        check("邮箱式用户名通过校验", bool(d._USER_RE.match("admin@corp")))

        os.environ["DEPLOY_PWD"] = "S3cr3t!P@ss"
        cap2: dict = {}

        def fake_run2(argv, **kw):
            cap2["argv"] = list(argv)
            env2 = dict(kw.get("env", {}))
            cap2["env"] = env2
            askp = env2.get("SSH_ASKPASS", "")
            if askp and Path(askp).is_file():
                cap2["helper_text"] = "".join(
                    f.read_text(encoding="utf-8", errors="ignore")
                    for f in Path(askp).parent.glob("*") if f.is_file())
            return FakeDone()

        orig3 = d.subprocess.run
        d.subprocess.run = fake_run2
        rr3 = d.execute("pwd", "docker ps", reason="自检", approval_timeout=6,
                        emit=auto_approve_emit, consensus_fn=fake_consensus)
        d.subprocess.run = orig3
        os.environ.pop("DEPLOY_PWD", None)
        a2 = " ".join(cap2.get("argv", []))
        ev2 = cap2.get("env", {})
        check("密码目标执行成功", rr3["ok"], rr3["why"])
        check("password 不带 BatchMode=yes（否则密码被禁用）",
              "BatchMode=yes" not in a2 and "BatchMode=no" in a2)
        check("密码不在 argv", "S3cr3t" not in a2 and "P@ss" not in a2)
        check("ssh 走 askpass（env 注入 + force）",
              ev2.get("SSH_ASKPASS") and ev2.get("SSH_ASKPASS_REQUIRE") == "force")
        check("askpass helper 脚本不含密码本体", "S3cr3t" not in cap2.get("helper_text", ""))
        check("helper 用完即删", not Path(ev2.get("SSH_ASKPASS", "Z:/none")).exists())
        check("密码不进返回值/审计", "S3cr3t" not in json.dumps(rr3))
        check("describe 声明支持密码认证", d.describe()["password_auth_supported"] is True)

        print("[7] 路径决策五条")
        cases = [
            (dict(target="订单 API"), "direct"),
            (dict(target="部署上线", deploy_authorized=True), "authorized_deploy"),
            (dict(target="PR 插件", closed_host=True, can_observe_runtime=False), "plugin_bridge"),
            (dict(target="视频审美", subjective_quality=True), "proxy_plus_human"),
            (dict(target="闭源 CAD", closed_host=True, mechanical_verdict=False), "structural_limit"),
            # 未授权就算提了部署也不冒进
            (dict(target="部署上线", deploy_authorized=False), "direct"),
        ]
        for kw, want in cases:
            got = rd.decide_route(**kw)["route"]
            check(f"{kw['target']} → {want}", got == want, f"got {got}")

        print("[8] function calling 接线")
        check("deploy_targets schema 存在", "deploy_targets" in ts.TOOL_NAMES)
        check("deploy_exec schema 存在", "deploy_exec" in ts.TOOL_NAMES)
        src = (ROOT / "core" / "remotetools.py").read_text(encoding="utf-8")
        check("分发器有 deploy_exec 分支", 'name == "deploy_exec"' in src)
        check("GUIDE 介绍部署工具", "deploy_exec(target, command, reason)" in src)
        check("decide_route 传 deploy_authorized", "deploy_authorized=attrs.get" in src)

        print("[9] 打包保护")
        bsrc = (ROOT.parent / "packaging" / "build_installer.py").read_text(encoding="utf-8")
        check("打包模板清空 deploy 段", "def _scrub_deploy" in bsrc and "_scrub_deploy(data)" in bsrc)
        check("硬闸检查部署目标", 'dep.get("targets")' in bsrc)

        print("[10] 主沙箱仍禁 ssh（没拆安全边界）")
        sb_src = (ROOT / "core" / "sandbox.py").read_text(encoding="utf-8")
        check("DENY_PATTERNS 仍含 ssh", "ssh|scp|sftp" in sb_src)
        check("ALLOW_HEADS 无 ssh", "ssh" not in sb_src.split("ALLOW_HEADS")[1].split("}")[0])
        check("部署能力声明凭据不落盘", d.describe()["credential_persisted"] is False)
        check("部署能力声明密码不进 argv/审计",
              d.describe()["password_never_in_argv"] is True
              and d.describe()["password_never_in_audit"] is True)
        check("部署能力声明强制人审", d.describe()["human_approval_forced"] is True)

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
    raise SystemExit(main())
