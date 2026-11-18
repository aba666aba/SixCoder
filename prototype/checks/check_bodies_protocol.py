"""作者输出协议：围栏代码（不是 JSON 字符串）—— 2026-11-09。

背景：旧协议要求把函数体塞进 JSON 字符串
（`{"bodies":{"f":"def f():\\n    ..."}}`），缩进和换行全靠手写转义。
Flash 反复在 `'(' was never closed` 上卡死。**代码写在围栏里是模型的训练原生形态，
塞进 JSON 是反模式** —— 同样模型、同样契约，只换输出协议。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import author  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def main() -> int:
    print("[1] 围栏里取函数体")
    txt = (
        "说明文字\n\n```python\n"
        "# ===== create_topic_draft =====\n"
        "if not isinstance(title, str) or not title.strip():\n"
        '    raise ValueError("title 不能为空")\n'
        "draft = {\n"
        '    "title": title.strip(),\n'
        '    "quota": normalize_quota(quota),\n'
        "}\n"
        "return draft\n"
        "# ===== helper:_coerce =====\n"
        "def _coerce(v):\n"
        "    return int(v) if v is not None else 0\n"
        "```\n\n结尾文字\n"
    )
    bodies, helpers = author.parse_fenced_bodies(txt)
    check("取到一个函数体", list(bodies) == ["create_topic_draft"], str(list(bodies)))
    body = bodies.get("create_topic_draft", "")
    check("函数体保留内部缩进", "    raise ValueError" in body, repr(body[:60]))
    check("函数体不含 def 行", not body.startswith("def "), repr(body[:40]))
    check("多行字典没被压平", '"quota": normalize_quota(quota),' in body, repr(body))
    check("helper 拿到完整 def",
          helpers.get("_coerce", "").startswith("def _coerce(v):"), str(helpers)[:80])

    print("\n[2] 模型多写了整段 def 也要能认")
    txt2 = ("```python\n# ===== foo =====\n"
            "def foo(a):\n    if a < 0:\n        raise ValueError('neg')\n    return a * 2\n```\n")
    b2, _ = author.parse_fenced_bodies(txt2)
    check("剥掉 def 行只留函数体", b2.get("foo", "").startswith("if a < 0:"),
          repr(b2.get("foo", "")[:40]))
    check("缩进仍然正确", "    raise ValueError('neg')" in b2.get("foo", ""),
          repr(b2.get("foo", "")))

    print("\n[3] 回退与容错")
    check("旧 JSON 仍能解析",
          "bodies" in (author._parse_json('{"bodies":{"f":"return 1"}}') or {}))
    check("没有围栏时不崩", author.parse_fenced_bodies("就是一段话") == ({}, {}))
    check("空输入不崩", author.parse_fenced_bodies("") == ({}, {}))

    print("\n[4] 骨架带现有实现（作者能看到别人长什么样）")
    class _P:
        def __init__(self, name, type=""):
            self.name, self.type = name, type

    class _F:
        def __init__(self, name, params=None):
            self.name, self.params, self.returns, self.doc = name, params or [], "", ""

    existing = (
        "def alpha(x):\n"
        '    """已有实现"""\n'
        "    return x + 1\n\n"
        "def beta(y):\n"
        '    """还没做"""\n'
        "    raise NotImplementedError\n"
    )
    sk = author.render_skeleton([_F("alpha", [_P("x")]), _F("beta")],
                                existing_src=existing, include_bodies=True)
    check("已有实现进了骨架", "return x + 1" in sk or "x + 1" in sk, sk[:200])
    check("没实现的那条仍是桩", "raise NotImplementedError" in sk, sk[:200])
    sk2 = author.render_skeleton([_F("alpha", [_P("x")]), _F("beta")], existing_src=existing)
    check("默认不含实现（旧行为）", "x + 1" not in sk2, sk2[:160])
    # 签名变了就不能复用旧体（旧体的变量名会和新签名打架）
    sk3 = author.render_skeleton([_F("alpha", [_P("y")])], existing_src=existing,
                                 include_bodies=True)
    check("签名变更时不给旧体", "x + 1" not in sk3, sk3[:200])
    check("并且给了提示", "签名已按契约变更" in sk3, sk3[:200])

    print("[5] prompt 能渲染（花括号必须转义，否则 format 直接 KeyError）")
    import re as _re
    try:
        _rendered = author.BODIES_PROMPT.format(
            protocol="p", file_block="f", skeleton_block="s",
            contract_block="c", plan_block="", evidence_block="",
            # 2026-11-10：模板新增 {format_example} 占位符（真实调用在 author.py:915
            # 已传），断言没同步 → 误报 KeyError。补上。
            format_example="e")
        _ok_fmt = True
    except Exception as _e:                              # noqa: BLE001
        _rendered, _ok_fmt = f"{type(_e).__name__}: {_e}", False
    check("BODIES_PROMPT 可 format", _ok_fmt, str(_rendered)[:100])
    check("渲染后没有残留占位符",
          not _re.findall(r"\{[a-z_]+\}", str(_rendered)),
          str(_re.findall(r"\{[a-z_]+\}", str(_rendered)))[:100])
    check("全文块真的进了 prompt", "f" in str(_rendered), "file_block 没渲染")

    print()
    if FAILS:
        print("有失败项：" + ", ".join(FAILS))
        return 1
    print("全部通过（围栏协议 / 多写 def / 回退 / 骨架带实现）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
