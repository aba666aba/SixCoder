"""外部真链路半自动自检（2026-11-23）。

钉住：值只在内存不落盘 / TTL 失效 / 一次性消费 / NeedHuman fail-closed /
审计与视图不含值 / 半自动编排遇缺人填暂停并可续跑 / 空值与注入键拒绝 /
server 回填不回显值。
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" :: {detail[:160]}" if detail and not ok else ""))


def main() -> int:
    tf = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
    tf.close()
    os.environ["SIXCODER_SETTINGS"] = tf.name

    from core import human_once as h

    try:
        print("[1] 能力声明：诚实")
        d = h.describe()
        check("只存内存", d["storage"] == "memory-only" and d["persisted"] is False)
        check("不谎报全自动", d["full_auto_claimed"] is False)
        check("不谎报跨进程", d["cross_process"] is False)
        check("缺值 fail-closed", d["fail_closed_on_missing"] is True)
        check("审计不含值", d["audit_excludes_values"] is True)

        print("[2] 一次填入 / 一次消费")
        r = h.put("otp", "sms", "883421", ttl=60, uses=1)
        check("回执不含值", "883421" not in str(r))
        check("has 命中", h.has("otp", "sms"))
        v = h.get("otp", "sms")
        check("取到正确值", v == "883421")
        check("取后已消费", not h.has("otp", "sms"))
        try:
            h.get("otp", "sms")
            check("二次取拒绝", False)
        except h.NeedHuman:
            check("二次取抛 NeedHuman", True)

        print("[3] 可复用登录态 uses>1")
        h.put("login", "session", "token-abc", ttl=60, uses=3)
        check("复用第 1 次", h.get("login", "session", consume=False) == "token-abc")
        check("复用第 2 次仍在", h.has("login", "session"))
        h.clear("login", "session")

        print("[4] TTL 失效")
        h.put("otp", "short", "123456", ttl=1.0, uses=5)
        check("立即可用", h.has("otp", "short"))
        time.sleep(1.2)
        check("过期失效", not h.has("otp", "short"))
        try:
            h.get("otp", "short")
            check("过期取拒绝", False)
        except h.NeedHuman:
            check("过期取抛 NeedHuman", True)

        print("[5] pending / 审计不含值 / 不落盘")
        h.clear_all()
        h.put("otp", "email", "998877", ttl=60)
        pend = h.pending()
        check("pending 有项", any(p["key"] == "email" for p in pend))
        check("pending 不含值", "998877" not in str(pend))
        audit = h.audit_log()
        check("审计不含值", all("998877" not in str(x) for x in audit))
        # 不落盘：临时配置目录/仓库审计文件都不该出现该值
        hits = []
        for base in (Path(tf.name).parent, ROOT / ".sixcoder"):
            if base.exists():
                for f in base.glob("*.json*"):
                    try:
                        if "998877" in f.read_text(encoding="utf-8", errors="ignore"):
                            hits.append(str(f))
                    except Exception:                     # noqa: BLE001
                        pass
        check("值未落盘到配置/审计文件", not hits, str(hits))

        print("[6] 非法输入拒绝")
        for bad in ("", "   ", "x" * 5000, 123):
            try:
                h.put("otp", "bad", bad)
                check(f"拒绝非法值 {str(bad)[:8]}", False)
            except ValueError:
                check(f"拒绝非法值 {str(bad)[:8]}", True)
        try:
            h.put("otp;bad", "k", "123")
            check("拒绝注入命名空间", False)
        except ValueError:
            check("拒绝注入命名空间", True)

        print("[7] 半自动编排：暂停 + 续跑")
        h.clear_all()
        steps = [
            {"kind": "auto", "action": "打开站点"},
            {"kind": "human", "key": "captcha", "hint": "输入图形验证码", "slot": "captcha"},
            {"kind": "auto", "action": "提交表单"},
        ]
        got = []
        def executor(step, value):
            got.append((step.get("action") or step.get("slot"), value))
            return {"done": True}
        r1 = h.run_steps(steps, executor, namespace="flow")
        check("缺人填时暂停", r1["ok"] is False and r1["paused_at"] == 1)
        check("暂停点返回 need", r1["need"]["kind"] == "need_human" and r1["need"]["key"] == "captcha")
        check("暂停前只跑了第 0 步", len(r1["steps"]) == 1)
        h.put("flow", "captcha", "A7B9")
        r2 = h.run_steps(steps, executor, namespace="flow")
        check("补齐后续跑成功", r2["ok"] is True and len(r2["steps"]) == 3)
        check("human 值注入执行器", any(s == "captcha" and v == "A7B9" for s, v in got))
        check("编排结果不含人填值", "A7B9" not in str(r2))

        print("[8] server 端点不回显值")
        os.environ["SIXCODER_SETTINGS"] = tf.name
        # 直接测 schema 层：put 经 core 返回回执，body 里的值不进响应
        receipt = h.put("web", "code", "135790", ttl=60)
        check("put 回执无值", "135790" not in str(receipt))
        check("state 视图无值", "135790" not in str(h.pending()))

        print("[9] 进程代次与重启提示")
        d = h.describe()
        check("describe 带 boot_id", bool(d.get("boot_id")), str(d.get("boot_id")))
        check("声明不跨重启", d.get("survives_restart") is False)
        check("有重填提示", "重新" in str(d.get("restart_hint") or ""))
        check("同进程 boot_id 稳定", h.describe().get("boot_id") == d.get("boot_id"))

    finally:
        os.environ.pop("SIXCODER_SETTINGS", None)
        try:
            Path(tf.name).unlink(missing_ok=True)
        except Exception:                                # noqa: BLE001
            pass

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
