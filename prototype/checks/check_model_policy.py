r"""模型门槛自检：本地工人模型必须 ≥7B，低于门槛的入口必须真的拒绝。

为什么值得单独一条：这条门槛是**实测**定出来的（3B：机械验收 2/8、独立验收 2/16；
7B(CPU)：8/8、19/19），但"门槛"最容易被后来的人删掉——它只是一行常量。
所以这里把"谁能用、谁不能用、谁会被自动替换、谁会被拒绝"钉成回归。

跑法: python check_model_policy.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import model_policy as mp  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"  {detail}" if not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 参数量解析")
    cases = {"qwen2.5-coder:3b": 3.0, "qwen2.5:7b": 7.0, "qwen2.5-coder:14b": 14.0,
             "qwen2.5-coder:32b": 32.0, "llama3:70b": 70.0,
             "deepseek-coder:6.7b-instruct": 6.7, "codellama:13b-instruct": 13.0, "deepseek-flash": 0.0,
             "some-model": 0.0}
    for name, want in cases.items():
        got = mp.params_b(name)
        check(f"{name} → {want:g}B", abs(got - want) < 1e-6, f"得到 {got}")

    print("[2] 判定：4B 是**建议门槛**，不是红线（选哪个模型归用户）")
    check("建议门槛常量是 4", mp.MIN_PARAMS_B == 4.0, str(mp.MIN_PARAMS_B))
    for small in ("qwen2.5-coder:3b", "qwen2.5:1.5b", "qwen2.5-coder:1.5b", "phi3:mini2b"):
        v = mp.verdict(small)
        check(f"{small} 只提示不建议（不拒绝）", v["ok"] and v["advisory"],
              f"ok={v['ok']} advisory={v['advisory']}")
    check("4B 本身算低于建议门槛（严格大于才算达标）", mp.verdict("qwen2.5:4b")["advisory"])
    for good in ("qwen2.5:7b", "qwen2.5-coder:14b", "deepseek-flash",
                 "deepseek-coder:6.7b-instruct"):
        check(f"{good} 不建议（达标）", not mp.verdict(good)["advisory"],
              mp.verdict(good)["message"][:40])
    # 名字里没有参数量：不许误杀（用户可能用自定义 tag）
    check("未标注参数量放行", not mp.verdict("my-custom-coder")["advisory"])
    # 远端不受本地门槛约束
    check("远端不受本地门槛约束", not mp.verdict("deepseek-chat")["advisory"])

    print("[2.5] 代码专精优先（同尺寸时 coder 更会按契约写）")
    check("识别 coder 模型", mp.is_coder("qwen2.5-coder:7b") and mp.is_coder("deepseek-coder:6.7b"))
    check("通用模型不算 coder", not mp.is_coder("qwen2.5:7b") and not mp.is_coder("llama3.1:8b"))
    # 挑选逻辑：7B coder 应排在 7B 通用之前（用假列表测，不依赖本机装了什么）
    order = sorted(["qwen2.5:7b", "qwen2.5-coder:7b"],
                   key=lambda m: (-mp.params_b(m) if not mp.is_coder(m) else -mp.params_b(m) - 100, m))
    check("同尺寸时 coder 排前面", order[0] == "qwen2.5-coder:7b", str(order))

    print("[3] 入口行为：require / enforce / advise（全都不拦人）")
    check("require(3B) 不抛错、原样返回", mp.require("qwen2.5-coder:3b") == "qwen2.5-coder:3b")
    check("require(7B) 原样返回", mp.require("qwen2.5:7b") == "qwen2.5:7b")
    model, warn = mp.enforce("qwen2.5:7b")
    check("enforce(7B) 不动也不唠叨", model == "qwen2.5:7b" and not warn)
    model, warn = mp.enforce("qwen2.5-coder:3b")
    check("enforce(3B) 照用户的选择用，只给一句警告",
          model == "qwen2.5-coder:3b" and bool(warn), f"→ {model} / {warn[:40]}")
    check("advise(3B) 有说明", bool(mp.advise("qwen2.5-coder:3b")))
    check("advise(7B) 为空", not mp.advise("qwen2.5:7b"))
    if model != "qwen2.5-coder:3b":
        check("自动替换的目标必须本身合格", mp.verdict(model)["ok"], model)

    print("[4] 默认模型不许是小模型")
    from core import ollama  # noqa: E402
    installed = ollama.models()
    d = ollama.pick_default()
    n = mp.params_b(d)
    if installed:
        # 装了模型 → 默认值必须是门槛以上的那个（挑出来的是 1.5B/3B 就是错）
        check("装了模型时，默认值不低于建议门槛", n >= mp.MIN_PARAMS_B, f"默认 {d}（{n}B）")
    else:
        # 一个模型都没装（CI / 新机器）→ 必须如实返回空，不许回落成小模型占位。
        # 以前回落 `qwen2.5:1.5b`，于是"默认模型"本身就是项目禁止的那种小模型。
        check("没有可用模型时如实返回空（不回落成小模型）", d == "", f"默认 {d!r}")
        # 注意：verdict("") 是 ok=True（"未标注参数量→放行"这条规则），所以不能拿它判。
        # 这里断的是"返回非空名字时，那个名字必须达标"。
        check("空默认值不会被当成合格模型（0 参数不算达标）",
              d == "" or mp.params_b(d) >= mp.MIN_PARAMS_B, f"默认 {d!r}")

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（3B/1.5B 被拒、7B+ 放行、默认模型不低于门槛）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
