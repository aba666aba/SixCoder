r"""三种模式自检：甲方模式 / 开发者模式 / 只读模式。

三条硬规则（用户明确要求的语义）：
  1. **甲方模式**：人工处置全部放行（不打扰人）、从 0 到 1 能跑通；
     但**只能改工作区内的文件，越界一律拒绝**。
  2. **开发者模式**：关键节点必须人工审核，**不会自动放行**；
     抛 needhuman，没人回答按拒绝处理（fail-closed），拒绝时该节点中止。
  3. **只读模式**：只能读，任何写入/执行/提交都被拒绝；用于成品意图分析与改进建议。

反例都在这里跑：越界路径、只读写入、开发者模式不批、开发者模式批准、超时。

跑法: python check_modes.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import approval, guards, permissions, sandbox  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 命名与档位（名字必须原样）")
    names = [m.name for m in permissions.MODES.values()]
    check("正好三个模式", len(names) == 3, str(names))
    check("名字是 甲方模式/开发者模式/只读模式", names == ["甲方模式", "开发者模式", "只读模式"],
          str(names))
    check("默认是甲方模式", permissions.DEFAULT_MODE == "client", permissions.DEFAULT_MODE)
    check("没有残留“完全授权”说法",
          "完全授权" not in "".join(m.desc for m in permissions.MODES.values()))

    print("[2] 能力矩阵")
    permissions.set_mode("客户端")                      # 非法名 → 回落默认
    check("非法模式名回落默认（不崩）", permissions.current_mode().id == "client")
    permissions.set_mode("developer")
    check("开发者模式能写文件", permissions.allows("file.write"))
    check("开发者模式能提交/推送", permissions.allows("git.commit") and permissions.allows("git.remote"))
    permissions.set_mode("readonly")
    for cap in ("file.write", "exec.run", "git.commit", "agent.run", "git.remote"):
        check(f"只读模式拒绝 {cap}", not permissions.allows(cap))
    check("只读模式仍能读", permissions.allows("file.read") and permissions.allows("review.run"))
    permissions.set_mode("client")
    check("甲方模式全能力放行", all(permissions.allows(c) for c in permissions.CAPS))

    # 开发者模式的远端调用受开关控制（默认关）：后面测审批要先打开
    try:
        from core import settings_store as _ss
        _old_dev = bool(_ss.get_remote().get("dev_remote", False))
        _ss.update_remote({"dev_remote": True})
    except Exception:                                    # noqa: BLE001
        _old_dev = None

    print("[3] 甲方模式：全部自动放行 + 工作区边界")
    check("甲方模式不需要人审", not permissions.needs_human("file.write"))
    ws = Path(tempfile.mkdtemp(prefix="mode_ws_"))
    (ws / "ok.py").write_text("x = 1\n", encoding="utf-8")
    g = guards.gate_file_write(ws, "ok.py", "x = 1\n", "x = 2\n")
    check("写工作区内文件直接放行", g["ok"] and g["decision"] == "auto", str(g))
    sb = sandbox.Sandbox(ws, mode="write")
    outside = str(ws.parent / "escape.py")
    blocked = False
    try:
        sb.jail(outside, write=True)
    except sandbox.SandboxError as exc:
        blocked = "越界" in str(exc)
    check("越界路径被拒（甲方模式也不例外）", blocked)
    blocked2 = False
    try:
        sb.jail("../../etc/passwd", write=True)
    except sandbox.SandboxError:
        blocked2 = True
    check("相对路径逃逸被拒", blocked2)

    print("[4] 开发者模式：关键节点必须人审（不会自动放行）")
    permissions.set_mode("developer")
    check("写文件节点需要人审", permissions.needs_human("file.write"))
    check("收尾交付也需要人审", permissions.needs_human("deliver"))

    # 4a 无人回答 → 超时按拒绝（fail-closed）
    t0 = time.time()
    r = guards.approval.request("file.write", "写 ok.py", "diff…", workspace=ws,
                                interactive=False, timeout=0.4)
    check("没人回答 → 拒绝（fail-closed）",
          (not r["ok"]) and r["decision"] == "timeout", str(r)[:120])
    check("超时确实等了（不是立刻放行）", time.time() - t0 >= 0.35, f"{time.time() - t0:.2f}s")

    # 4b 人工批准 → 放行
    # 只批准**本次新建**的单据：4a 的超时单据会残留在 pending() 里，旧写法
    # `items[0]` 可能批准错单据后线程就退出，导致 4b 自己没人批 → 偶发超时。
    # 线程持续到 request 返回（stop 事件），不再靠固定 2 秒循环碰运气。
    holder = {}
    before = {it["id"] for it in approval.pending()}
    stop = threading.Event()

    def approve_soon():
        while not stop.is_set():
            for it in approval.pending():
                if it["id"] not in before:
                    holder["id"] = it["id"]
                    approval.resolve(it["id"], "approved", by="test")
                    return
            time.sleep(0.01)

    th = threading.Thread(target=approve_soon, daemon=True)
    th.start()
    r2 = approval.request("file.write", "写 ok.py", "diff…", workspace=ws,
                          interactive=False, timeout=5)
    stop.set()
    th.join(timeout=1)
    check("人工批准后放行", r2["ok"] and r2["decision"] == "approved", str(r2)[:120])
    check("单据被回填了", bool(holder.get("id")), str(holder))

    # 4c 人工拒绝 → 该节点中止
    before2 = {it["id"] for it in approval.pending()}
    stop2 = threading.Event()

    def reject_soon():
        while not stop2.is_set():
            for it in approval.pending():
                if it["id"] not in before2:
                    approval.resolve(it["id"], "rejected", by="test")
                    return
            time.sleep(0.01)

    th2 = threading.Thread(target=reject_soon, daemon=True)
    th2.start()
    r3 = approval.request("deliver", "标记可交付", "8/8", workspace=ws,
                          interactive=False, timeout=5)
    stop2.set()
    th2.join(timeout=1)
    check("人工拒绝 → 不放行", (not r3["ok"]) and r3["decision"] == "rejected", str(r3)[:120])
    check("审计日志落了盘", (ws / ".sixcoder" / "audit.jsonl").exists())

    print("[5] 只读模式：没有写权限，也走不到写节点")
    permissions.set_mode("readonly")
    check("只读模式不需要人审（因为没有可批的写操作）", not permissions.needs_human("file.write"))
    ro = sandbox.Sandbox(ws, mode="readonly")
    denied = False
    try:
        ro.jail("ok.py", write=True)
    except sandbox.SandboxError as exc:
        denied = "只读" in str(exc)
    check("只读沙箱拒绝写入", denied)
    denied_cmd = False
    try:
        ro.check_cmd("python -c \"print(1)\"")
    except sandbox.SandboxError:
        denied_cmd = True
    check("只读沙箱拒绝执行命令", denied_cmd)

    print("[6] 收尾：恢复默认模式")
    permissions.set_mode("client")
    check("恢复为甲方模式", permissions.current_mode().id == "client")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（三模式语义、工作区边界、needhuman 的批准/拒绝/超时都成立）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
