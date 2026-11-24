"""八档阶梯自检：升档 / 回档 / 偶发失败不升档 / 提示词前缀稳定（省钱）。

三个必须钉死的行为（都是用户明确问过的）：

1. **升档**：同一档连续失败到阈值才升下一档，不是一失败就升级；
2. **回档**：解决之后立刻回到最低档，高档次不许被后面的单继承；
3. **省钱**：切档会重新付一次全价输入，所以
   · 偶发失败（限流/超时）不升档；
   · 提示词按"稳定→多变"排，让同模块的多张单共享前缀（命中价只有 1/50）。

跑法: python check_ladder.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import company, iterate  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Ladder:
    """company 里那段升档逻辑的等价模型（从代码里抽出来单独跑）。"""

    def __init__(self, steps: int, escalate_after: int):
        self.steps = steps
        self.after = escalate_after
        self.tier: dict[str, int] = {}
        self.fail: dict[str, int] = {}

    def fail_once(self, key: str) -> int:
        """记一次失败，返回记录之后的档位。"""
        lvl = self.tier.get(key, 0)
        self.fail[key] = self.fail.get(key, 0) + 1
        if self.fail[key] >= self.after and lvl + 1 < self.steps:
            self.tier[key] = lvl + 1
            self.fail[key] = 0
        return self.tier.get(key, 0)

    def succeed(self, key: str) -> None:
        lvl = self.tier.get(key, 0)
        if lvl > 0:
            self.tier[key] = 0            # 解决即回最低档
        self.fail[key] = 0


def main() -> int:
    print("八档阶梯自检")

    # 1) 升档：阈值 2 → 第一次失败不升，第二次失败才升
    lad = Ladder(steps=8, escalate_after=2)
    k = "clean.py::clean_row::implement"
    check("第一次失败还在第 1 档", lad.fail_once(k) == 0)
    check("第二次失败升到第 2 档", lad.fail_once(k) == 1)
    check("升级后失败计数清零（新档重新给机会）", lad.fail[k] == 0)

    # 2) 一路升到顶就停住（不越界）
    for _ in range(40):
        lad.fail_once(k)
    check("最高只到第 8 档，不会越界", lad.tier[k] == 7, str(lad.tier[k]))

    # 3) 回档：成功后立刻回到最低档
    lad.succeed(k)
    check("解决之后回到第 1 档", lad.tier[k] == 0, str(lad.tier[k]))

    # 4) 每张单各有各的档位：难题升档不会污染别的单
    lad2 = Ladder(steps=8, escalate_after=2)
    hard, easy = "a::x::implement", "b::y::implement"
    lad2.fail_once(hard); lad2.fail_once(hard)
    check("难题升到第 2 档", lad2.tier[hard] == 1)
    check("别的单仍在第 1 档", lad2.tier.get(easy, 0) == 0)

    # 5) 偶发失败（限流/超时）不参与升档
    print("[2] 偶发失败不该升档")
    for msg in ("被限流（HTTP 429）：稍后重试", "请求失败：TimeoutError: timed out",
                "远端调用失败：HTTP 503"):
        check(f"识别为偶发：{msg[:22]}", company._is_transient_fail(msg) is True, msg)
    for msg in ("契约判据未通过：summarize 得到 0，应为 1", "【签名】缺 rows", ""):
        check(f"不算偶发：{msg[:22] or '（空）'}", company._is_transient_fail(msg) is False, msg)

    # 6) 省钱：提示词必须是"稳定段在前、多变段在后"
    print("[3] 提示词前缀稳定（决定能不能吃缓存折扣）")
    p = iterate.EXEC_PROMPT
    i_src, i_target, i_order = (p.find("【同文件其它内容"), p.find("【目标符号现状"),
                                p.find("【工作单】"))
    check("同文件全文在目标符号之前", 0 <= i_src < i_target)
    check("工作单（每单都变）放在最后", i_order > i_target and i_order > 0)
    check("铁律在最前（全局共享前缀）", p.find("铁律：") < i_src)

    # 7) 真实源码钉死：company.py 三条写码路径**都必须有成功回落**（双向弹性）。
    #    背景 2026-11-10：函数体主路（author_bodies）只升不降——升档 11-09 补通后，
    #    模块升到 high/max 就锁死，简单模块也在用最贵档跑（实测 74% 花费在 max）。
    #    修复 = 对齐另两条路（工作单路 tier_reset / 整文件路"解决即回最低档"）。
    print("[4] 三条写码路径都有「成功回落」（防只升不降复发）")
    src = (Path(__file__).resolve().parent.parent / "core" / "company.py"
           ).read_text(encoding="utf-8")
    n_reset = src.count('emit("tier_reset"')
    check("工作单路与函数体主路都有 tier_reset 事件（≥2 处）", n_reset >= 2,
          f"count={n_reset}")
    check("整文件路：解决即回最低档", "tier_of[rel] = 0                     # 解决即回最低档" in src)
    # 函数体主路的回落特征：elif ok 分支里 tier_of[rel] = 0（带中文注释块）
    check("函数体主路：成功也回 tier 0",
          "emit(\"tier_reset\", {\"order\": f\"函数体 {rel}\"" in src)
    check("升档通道仍在（三条路共 ≥3 处升档写点）",
          src.count("tier_of[rel] = lvl + 1") + src.count("tier_of[okey(o)] = lvl + 1") >= 3)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（阈值升档 / 解决回档 / 单独立档 / 偶发不升 / 前缀稳定 / 三路回落）")
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
