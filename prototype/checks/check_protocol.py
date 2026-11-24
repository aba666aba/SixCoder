"""本地↔远端协作协议自检：分工声明、输出格式、三档的远端开关、flash 优先。

钉住三件用户明确要求的事：
1. **给远端的提示词必须声明"本地有哪些机械工具"** —— 否则远端会把测试、报告、
   逐条核对也自己做了，白烧 token 而且做得更差（本地是确定性的）。
2. **远端开关按档位不同**：甲方强制开、只读强制关、开发者看设置开关。
3. **默认主力是 flash**（便宜且够用），pro 默认不启用。

跑法: python check_protocol.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import guards, iterate, permissions, policy, protocol, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("本地↔远端协作协议自检")
    orig = permissions.current_mode().id
    old_dev = bool(settings_store.get_remote().get("dev_remote", False))

    # 1) 协议内容：分工必须写明
    print("[1] 分工声明（远端不该做机械活）")
    blk = protocol.capabilities_block()
    check("声明了本地工具清单", "【本地工具（已具备，你不要重复做）】" in blk)
    for must_not in ("不要写 pytest 测试", "不要写验收报告", "不要解释你的代码",
                     "不要跑命令"):
        check(f"明确禁止：{must_not}", must_not in blk, blk[:120])
    check("声明了远端必须做的三件事", "决定模块划分" in blk and "写实现代码" in blk)
    check("本地能力清单覆盖测试与变异", "渲染 pytest" in blk and "变异" in blk)

    # 2) 输出格式：必须是可解析 JSON（不许解释性文本）
    print("[2] 输出格式（机器可解析）")
    for kind, token in (("files", '"files"'), ("edits", '"edits"'), ("plan", '"plan"'),
                        ("question", '"question"')):
        one = protocol.output_format(kind)
        check(f"{kind} 格式含 {token}", token in one, one[:80])
        check(f"{kind} 格式要求只输出 JSON", "只输出 JSON" in one)

    # 3) 协议确实进了给远端的提示词
    print("[3] 协议接进提示词")
    check("执行者提示词含占位符", "<<PROTOCOL>>" in iterate.EXEC_PROMPT, iterate.EXEC_PROMPT[:80])
    built = iterate._build_exec_prompt({"kind": "implement", "target": "a.py", "symbol": "f",
                                        "acceptance": "", "detail": ""},
                                       "f", "def f() -> int: pass", "source", plan="", extra="")
    check("渲染后的提示词带分工声明", "本地工具（已具备" in built, built[:120])
    check("渲染后没有占位符残留", "<<PROTOCOL>>" not in built)
    from core import author
    check("整文件模式提示词也带协议", "{protocol}" in author.AUTHOR_PROMPT)

    # 4) 三档的远端开关
    print("[4] 远端开关：甲方强制开 / 只读强制关 / 开发者可开关")
    permissions.set_mode("client")
    ok_c, why_c = policy.remote_allowed()
    check("造物模式强制开启", ok_c is True, why_c)
    check("造物模式远端调用自动放行", guards.gate_remote_call(".", 100).get("ok") is True)

    permissions.set_mode("readonly")
    check("审阅模式强制关闭", policy.remote_allowed()[0] is False)
    check("审阅模式远端调用被拒", guards.gate_remote_call(".", 100).get("ok") is False)

    permissions.set_mode("developer")
    settings_store.update_remote({"dev_remote": False})
    ok_d, why_d = policy.remote_allowed()
    check("精修模式默认关闭远端", ok_d is False, why_d)
    check("关闭时远端调用被拒且说明原因",
          guards.gate_remote_call(".", 100).get("ok") is False
          and "开关已关闭" in guards.gate_remote_call(".", 100).get("reason", ""))
    settings_store.update_remote({"dev_remote": True})
    check("打开开关后不再拦（走审批而不是直接拒）",
          policy.remote_allowed()[0] is True)
    settings_store.update_remote({"dev_remote": False})

    # 5) 默认主力是 flash，pro 不启用
    print("[5] 模型档位：主力 flash，pro 默认不启用")
    ladder = settings_store.ladder()
    models_used = [s["profile_id"] for s in ladder if s["model"]]
    check("阶梯里只有 flash（pro 默认关）", set(models_used) == {"flash"}, str(set(models_used)))
    pro = next((p for p in settings_store.models() if p["id"] == "pro"), None)
    check("pro 档案仍然存在但未启用（要用就打开）",
          pro is not None and pro["enabled"] is False, str(pro and pro["enabled"]))
    flash = next((p for p in settings_store.models() if p["id"] == "flash"), None)
    check("flash 覆盖四档思考强度",
          flash and flash["efforts"] == ["none", "low", "high", "max"], str(flash and flash["efforts"]))
    check("造物模式阶梯里没有本地档（代码只由远端写）",
          all(s["profile_id"] != "local" for s in ladder), str(ladder[:1]))

    settings_store.update_remote({"dev_remote": old_dev})
    permissions.set_mode(orig)
    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（分工声明 / JSON 输出 / 三档远端开关 / flash 主力）")
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
