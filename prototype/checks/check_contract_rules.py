# -*- coding: utf-8 -*-
"""契约约束段判据（2026-11-15 用户定稿："契约改一下，让远端先写，本地补齐工程"）。

钉三件事：
1. 约束段**只讲领域边界**，不讲框架形态（"函数名要含路径末段"这类话不许回来）；
2. 每个目标栈都有对应的**补充事实**（只讲传输层怎么接，不讲业务怎么写）；
3. **幂等**（新旧两种标题都认），否则契约会被重复追加整段、前缀变长、缓存全丢。
"""
import inspect
import os
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))

from core import contract_rules as CR  # noqa: E402

FAILS = []


def ck(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (("  — " + str(detail)[:240]) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    print("[1] 通用规则：五类边界都在")
    gen = CR.GENERIC_RULES
    ck("说了「只写业务实现」", "只写**业务实现**" in gen)
    ck("禁止写路由/入口/settings", "不要" in gen and "路由注册" in gen and "settings" in gen)
    ck("禁止 import Web 框架", "import 任何 Web 框架" in gen)
    ck("输入输出是纯数据", "纯数据" in gen and "JSON 序列化" in gen)
    ck("错误用 ValueError（不返回状态码）", "ValueError" in gen and "HTTPException" in gen)
    ck("凭据只以 authorization: str 进入", "authorization: str" in gen)
    ck("命名逐字照契约（供本地对账）", "逐字" in gen)
    ck("docstring 要求（语义 + 边界）", "docstring" in gen)

    print()
    print("[2] 不许再出现框架形态约束（那是接线事实，属于本地）")
    banned = ["函数名要含路径末段", "请求体参数叫 `body`", "凭据参数必须叫"]
    for b in banned:
        ck("没有「%s」" % b, b not in gen, b)

    print()
    print("[3] 栈补充只讲传输层")
    for stack in ("fastapi", "django"):
        note = CR.STACK_NOTES.get(stack) or ""
        ck("%s 有补充" % stack, bool(note))
        ck("%s 说明了映射（422/4xx）" % stack, "422" in note or "4xx" in note, note[:80])
        ck("%s 不重复业务怎么写" % stack, "纯数据" not in note)
    d = CR.STACK_NOTES["django"]
    ck("Django 明确 models.py 例外（数据层可 import models）", "models.py" in d and "例外" in d)

    print()
    print("[4] apply 与幂等")
    a = CR.apply("# 契约正文\n", "django")
    ck("追加了约束段", "契约约束（引擎要求" in a)
    ck("带 Django 补充", "Django" in a)
    ck("第二次 apply 不重复", CR.apply(a, "django") == a)
    legacy = a.replace("契约约束", "接线约束")
    ck("旧标题文本也认（不重复追加）", CR.apply(legacy, "django") == legacy)
    ck("未知栈不炸、只给通用段", CR.apply("# x\n", "nope") and "通用" not in CR.apply("# x\n", "nope"))
    ck("is_present 覆盖新旧", CR.is_present(a) and CR.is_present(legacy))
    ck("describe() 可读", "条通用规则" in CR.describe(), CR.describe())

    print()
    print("[5] 接线：引擎真的用它")
    from core import auto
    src = inspect.getsource(auto._wire_constraints)
    ck("_wire_constraints 委托 contract_rules", "contract_rules" in src)
    ck("_wire_constraints 不再硬编码旧文案", "接线约束（引擎要求，必须遵守" not in src)
    ck("调用点带 stack_name（换栈能出对应补充）", "_stack().name" in src or "stack_name" in src)

    print()
    print("[6] 端到端：生成一份 Django 契约看长什么样")
    text = auto._wire_constraints("# 契约\n- create_topic(body: dict) -> dict：建草稿\n", "django")
    ck("正文保留在前", text.startswith("# 契约"))
    idx = text.find("契约约束（引擎要求")
    ck("约束段在正文之后（不插在中间打断契约）", idx > text.find("create_topic"), idx)
    ck("总长合理（别把契约撑爆）", 800 < len(text) < 6000, len(text))

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("ALL PASS（%d 项）" % 22)
    return 0


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    sys.exit(main())
