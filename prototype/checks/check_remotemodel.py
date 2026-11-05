"""远端模型选择自检：右上角选了谁，夜班就真的只走谁。

用户口径："右上角的选择应该是改为远端模型用于区分"。
所以这里钉三件事：
  ① `remote.ladder(profile)` 能按档案过滤（整条 / 只 flash / 只 pro）；
  ② 档案没启用时过滤结果为空 —— 而不是偷偷回退到别的模型；
  ③ `company.night(..., remote_profile=...)` 把这个选择透传到阶梯与交付报告。

跑法: python check_remotemodel.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import remote, settings_store  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("远端模型选择自检")
    orig = settings_store.models()

    print("[1] 默认设置：整条阶梯 = flash 四档")
    alls = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder()]
    print(f"     整条：{alls}")
    check("整条阶梯非空", bool(alls))
    check("默认只有 flash（pro 未启用）", all(x.startswith("flash") for x in alls))

    print("[2] 按档案过滤")
    flash = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder("flash")]
    pro = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder("pro")]
    check("只 flash → 与整条一致（当前配置下）", flash == alls)
    check("只 pro（未启用）→ 空，不偷偷回退到 flash", pro == [], str(pro))
    check("不存在的档案 → 空", remote.ladder("nonexistent") == [])

    print("[3] 单模型语义：档案都能存，但阶梯只认 active_model")
    try:
        profs = settings_store.models()
        for p in profs:
            if p["id"] == "pro":
                p["enabled"] = True
        settings_store.save_models(profs)
        all2 = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder()]
        pro2 = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder("pro")]
        print(f"     整条 {len(all2)} 档：{all2}")
        check("打开 pro 也不会变成两条并排（仍然只有当前那一个模型）",
              len(all2) == 4 and all(x.startswith("flash") for x in all2), str(all2))
        check("按档案过滤：pro 未激活 → 空（不偷偷回退）", pro2 == [], str(pro2))
        settings_store.update_remote({"active_model": "pro"})
        all3 = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder()]
        check("切 active_model=pro → 整条阶梯换成 pro 的四档",
              len(all3) == 4 and all(x.startswith("pro") for x in all3), str(all3))
        check("切过去之后 flash 反而查不到", remote.ladder("flash") == [],
              str(remote.ladder("flash")))
    finally:
        settings_store.update_remote({"active_model": ""})
        settings_store.save_models(orig)          # 一定还原，别把用户配置改坏

    print("[4] 还原后配置没被改动")
    back = [f"{x['profile_id']}:{x['effort']}" for x in remote.ladder()]
    check("阶梯恢复原样", back == alls, f"{back} vs {alls}")

    print("[5] 调用链：night 接受这个参数且写进报告")
    src = (Path(__file__).resolve().parents[1] / "core" / "company.py").read_text(encoding="utf-8")
    check("night() 有 remote_profile 参数", "remote_profile: str = \"\"" in src)
    check("阶梯按档案取", "ladder(remote_profile)" in src)
    check("交付报告写明用的档案", "远端模型：只走" in src)
    srv = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    check("/v1/night 接受 remote_profile", "remote_profile: str = \"\"" in srv)
    check("服务端透传到 night", "remote_profile=req.remote_profile" in srv)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（按档案过滤 / 未启用不回退 / 配置不被改坏 / 调用链打通）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
