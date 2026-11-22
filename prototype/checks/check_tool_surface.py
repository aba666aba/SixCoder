"""工具面完备性自检：能力覆盖 + 官方接口规范 + 可观测性。

2026-11-09 用户问："这些工具够用吗？需不需要按 DS 官方接口补充更多工具或规范化？"
本文件把答案钉成断言：

1. **覆盖**：远端写码要做的每一类动作都有工具（读/看结构/搜/看依赖/试 import/
   查契约/查路由/跑测试/静态检查/看历史/跑片段/查全局需求/记事实）。
2. **官方接口规范**：schema 形状合规（type/function/name/description/parameters）、
   required 明确、description 说清输入输出。
3. **可观测性**：`tool_loop` 事件必须带**工具名**（不只带数量）——
   否则"远端到底调了什么、哪些失败"根本看不见，工具面就永远无法评估
   （实测 2026-11-09：事件里只有 tools=4，没有任何工具名）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # checks/ 的上一级 = prototype/（换盘符不失效）
sys.path.insert(0, str(ROOT))

from core import remotetools, toolschema  # noqa: E402

FAILS: list[str] = []

# 远端写码需要的动作 → 至少一个对应工具
REQUIRED_ACTIONS = {
    "读文件": "read_file",
    "看结构": "outline",
    "列文件": "list_files",
    "搜代码": "grep",
    "看依赖": "imports",
    "试 import": "check_import",
    "查契约": "contract_for",
    "查路由": "route_status",
    "跑测试": "run_tests",
    "静态检查": "static_analysis",
    "看文件历史": "git_history",
    "跑代码片段": "run_python",
    "查全局需求": "requirement_for",
    "记事实": "write_memory",
}


def check(name: str, cond: bool, extra: str = "") -> None:
    print(("  OK  " if cond else "  FAIL ") + name + ("" if cond else "   " + extra))
    if not cond:
        FAILS.append(name)


def main() -> int:
    schemas = toolschema.tool_schemas()
    names = [t["function"]["name"] for t in schemas]

    print("[1] 覆盖：远端写码需要的每类动作都有工具")
    for action, tool in REQUIRED_ACTIONS.items():
        check(f"{action} → {tool}", tool in names, str(names))
    check("工具总数 >= 14", len(names) >= 14, f"实际 {len(names)}")

    print("[2] 官方接口规范（OpenAI/DeepSeek function calling 形状）")
    for t in schemas:
        f = t.get("function") or {}
        nm = f.get("name")
        ok = (t.get("type") == "function" and isinstance(nm, str) and nm
              and isinstance(f.get("description"), str) and f["description"]
              and isinstance(f.get("parameters"), dict))
        check(f"{nm} 的 schema 合规", bool(ok), str(t)[:120])
    bad_req = [t["function"]["name"] for t in schemas
               if not isinstance((t["function"].get("parameters") or {}).get("required"), list)]
    check("每个工具都声明了 required 数组", not bad_req, str(bad_req))
    no_desc = [t["function"]["name"] for t in schemas
               if len(t["function"].get("description") or "") < 15]
    check("每个工具的 description 都说明了做什么", not no_desc, str(no_desc))

    print("[3] 分发器与实现一致（schema 里有 → 分发器里也得有）")
    src = (ROOT / "core" / "remotetools.py").read_text(encoding="utf-8")
    missing = [n for n in names if f'name == "{n}"' not in src]
    check("所有工具都在分发器里有分支", not missing, str(missing))
    # 三种实现形态都算合法：
    #   ① 本文件里的 `def _<name>`（自造工具）；
    #   ② 委托 core.tools 的 `_tools.<name>`（read_file / list_files 是这一类）；
    #   ③ 分发器分支里**内联**实现（run_tests / write_memory 是这一类）。
    # 判据统一为：分支存在，且该分支的函数体里有 return（真的会返回结果）。
    import re as _re
    missing_impl = []
    for n in names:
        m = _re.search(r'if name == "%s":\n((?:[ ]{16}.*\n)+)' % _re.escape(n), src)
        if not m:
            missing_impl.append(n + "(无分支)")
            continue
        body = m.group(1)
        if "return" not in body:
            missing_impl.append(n + "(分支不返回结果)")
    check("每个工具都有能返回结果的实现", not missing_impl, str(missing_impl))

    print("[4] 可观测性：tool_loop 事件必须带工具名（不只带数量）")
    check("事件载荷里有 names 字段", '"names"' in src,
          "只有 tools=数量 → 看不见远端调了什么、哪些失败")
    check("事件载荷里有 failed 字段", '"failed"' in src,
          "失败的工具看不出来 → 没法评估工具面")
    check("有 _tool_log 记录每次调用", "_tool_log" in src)
    check("GUIDE 里介绍了每个工具",
          all(n in src for n in names),
          str([n for n in names if n not in src]))

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（动作覆盖完整 / 官方 schema 合规 / 分发器与实现一致 / "
          "工具调用可观测）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
