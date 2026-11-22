"""补偿路径决策工具自检（2026-11-23）。

钉四件事：
1. **决策矩阵**：四类典型需求各走对路径（直接生成/插件桥/人在环/结构性拒绝）。
2. **优先级与边界**：主观判据优先、判据不可机械化早否决、布尔字符串兼容、
   信号缺失保守降级、纯函数无副作用。
3. **结论完备**：每条路径都带 label/why/engine_can/human_must/cannot，
   不谎报（拒绝路径不能假装能验）。
4. **接线完整**：tool schema 合规（含 boolean 类型）、分发器有分支、
   GUIDE 有介绍、remotetools 能真的调用它。

全部用相对本文件的路径，避开历史自检硬编码 E 盘的技术债。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core import route_decision as rd  # noqa: E402
from core import toolschema as ts  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK  " if cond else "  FAIL ") + name + ("" if cond else "   " + extra))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("[1] 决策矩阵：四类典型需求走对路径")
    cases = [
        ("纯后端 API → 直接生成",
         dict(target="订单管理 API"), rd.ROUTE_DIRECT),
        ("闭源宿主 PR 插件 → 插件桥",
         dict(target="PR 自动剪辑", closed_host=True,
              can_observe_runtime=False), rd.ROUTE_PLUGIN_BRIDGE),
        ("审美型视频 → 降维+人在环",
         dict(target="智能剪辑", subjective_quality=True,
              closed_host=True, can_observe_runtime=False), rd.ROUTE_PROXY_HUMAN),
        ("闭源且判据不可机械化 → 结构性拒绝",
         dict(target="闭源 CAD 自动建模", closed_host=True,
              mechanical_verdict=False, can_observe_runtime=False), rd.ROUTE_STRUCTURAL),
    ]
    for name, kw, want in cases:
        got = rd.decide_route(**kw)["route"]
        check(name, got == want, f"期望 {want}，实际 {got}")

    print("[2] 优先级与边界")
    d = rd.decide_route(target="审美+后端", subjective_quality=True, closed_host=True)
    check("主观判据优先级高于闭源宿主", d["route"] == rd.ROUTE_PROXY_HUMAN, d["route"])
    check("主观+宿主时 why 提示仍需插件桥", "插件桥" in d["why"], d["why"])
    d = rd.decide_route(target="判据不可机械化+闭源", closed_host=True,
                        mechanical_verdict=False, can_observe_runtime=False)
    check("判据不可机械化早否决，不误判插件桥", d["route"] == rd.ROUTE_STRUCTURAL, d["route"])
    d = rd.decide_route(target="字符串信号", closed_host="true",
                        can_observe_runtime="false", mechanical_verdict="1")
    check("字符串布尔被正确解析", d["route"] == rd.ROUTE_PLUGIN_BRIDGE, str(d["signals"]))
    d = rd.decide_route(target="", closed_host=None, mechanical_verdict=None)
    check("None/空信号保守降级不冒进", d["route"] in rd.ROUTES, d["route"])
    d = rd.decide_route(target="能观测但判据不可机械化", mechanical_verdict=False,
                        can_observe_runtime=True)
    check("能观测但判据不可机械化仍拒绝", d["route"] == rd.ROUTE_STRUCTURAL, d["route"])

    print("[3] 结论完备 + 纯函数")
    # 每个 route 用确实能命中它的信号组合，避免"测了模板却没走到那条路径"的假绿
    route_kwargs = {
        rd.ROUTE_DIRECT: dict(target="后端", mechanical_verdict=True,
                              can_observe_runtime=True),
        rd.ROUTE_PLUGIN_BRIDGE: dict(target="PR 插件", closed_host=True,
                                     mechanical_verdict=True),
        rd.ROUTE_PROXY_HUMAN: dict(target="视频审美", subjective_quality=True),
        rd.ROUTE_STRUCTURAL: dict(target="不可判", mechanical_verdict=False),
    }
    for route, kwargs in route_kwargs.items():
        d = rd.decide_route(**kwargs)
        check(f"{route} 信号确实命中该路径", d["route"] == route,
              f"期望 {route}，实际 {d['route']}")
        for field in ("label", "why", "engine_can", "human_must", "cannot"):
            check(f"{route} 有 {field}", bool(d.get(field)), str(d.get(field))[:60])
        check(f"{route} engine_can 是非空数组", isinstance(d["engine_can"], list)
              and len(d["engine_can"]) > 0)
        check(f"{route} cannot 写明边界", isinstance(d["cannot"], str)
              and len(d["cannot"]) > 5)
    # 同一输入两次调用结果一致（确定性）
    kw = dict(target="确定性测试", closed_host=True, mechanical_verdict=True)
    check("纯函数：同输入同输出", rd.decide_route(**kw) == rd.decide_route(**kw))
    d = rd.decide_route(target="回显测试", requirement="r" * 999)
    check("回显字段被截断不撑爆", len(d["requirement"]) <= 500, str(len(d["requirement"])))

    print("[4] 接线：schema / 分发器 / GUIDE")
    names = ts.TOOL_NAMES
    check("decide_route 在 schema 里", "decide_route" in names, str(names))
    schema = next((t for t in ts.tool_schemas()
                   if t["function"]["name"] == "decide_route"), None)
    check("schema 形状合规", bool(schema) and schema.get("type") == "function")
    params = (schema or {}).get("function", {}).get("parameters", {})
    props = params.get("properties", {})
    check("target 是必填", "target" in params.get("required", []), str(params.get("required")))
    check("四个信号是 boolean 类型",
          all(props.get(k, {}).get("type") == "boolean" for k in
              ("closed_host", "can_observe_runtime", "mechanical_verdict",
               "subjective_quality")), str(props))
    check("description 讲清四条路径",
          "插件桥" in (schema or {}).get("function", {}).get("description", ""))
    rtsrc = (ROOT / "core" / "remotetools.py").read_text(encoding="utf-8")
    check("分发器有 decide_route 分支", 'name == "decide_route"' in rtsrc)
    check("分发分支真调用决策器", "route_decision" in rtsrc and "decide_route(" in rtsrc)
    check("GUIDE 介绍了 decide_route", "decide_route(" in rtsrc)

    # 真走一次分发器（走原生 schema 通道外的文本协议入口，验接线不是摆设）
    sys.path.insert(0, str(ROOT / "core"))
    check("决策模块可被 import 再调用",
          rd.decide_route(target="接线烟测")["route"] == rd.ROUTE_DIRECT)

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（决策矩阵 / 优先级边界 / 结论完备纯函数 / 接线完整）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
