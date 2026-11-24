"""骨架/函数体/局部重写的**确定性**自检（不调模型）。

为什么必须单开：粒度过细的那条路实测更差（6/8、¥0.35），日志里反复出现
"回归：clean.py 已验收数 2→1，已回滚"。那说明**只重写一个函数时把别的函数弄坏了**。
这种 bug 必须用确定性测试钉死，不能靠在夜班里烧钱试。

三条不变量：
    A. 局部重写（只给部分函数体）→ 其余函数的源码必须**逐字不变**；
    B. 局部重写（走 _merge_skeleton）→ 其余函数同样逐字不变；
    C. 骨架渲染 + 填体后：模块能编译、缩进正确、空行/注释不丢内容。

跑法: python check_bodies.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import author  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class F:
    """最小 FuncSig 替身。"""

    def __init__(self, name: str, params=(), ret="int", doc=""):
        self.name = name
        self.params = [type("P", (), {"name": n, "type": t})() for n, t in params]
        self.returns = ret
        self.doc = doc


SRC = '''"""模块文档。"""

import math


def alpha(x: int) -> int:
    """alpha 的描述里带引号 "2024-01-02"，必须能活下来。"""
    if x < 0:
        raise ValueError("负")
    return x + 1


def beta(rows: list[dict]) -> dict:
    """beta。"""
    out = {}
    for r in rows:
        if r.get("age"):
            out[r.get("id")] = r["age"]
    return out


def _helper(v: int) -> int:
    """辅助函数不该被动到。"""
    return v * 2
'''


def blocks_of(src: str) -> dict[str, str]:
    """把源码按顶格 def 切成 {函数名: 该函数源码}。"""
    import re
    lines = src.splitlines()
    idx = [i for i, ln in enumerate(lines) if ln.startswith("def ")]
    out = {}
    for k, s0 in enumerate(idx):
        e0 = idx[k + 1] if k + 1 < len(idx) else len(lines)
        m = re.match(r"def\s+(\w+)", lines[s0])
        out[m.group(1)] = "\n".join(lines[s0:e0]).rstrip()
    return out


def main() -> int:
    print("骨架 / 函数体 / 局部重写 自检")
    funcs = [F("alpha", [("x", "int")], "int", "alpha 的描述"),
             F("beta", [("rows", "list[dict]")], "dict", "beta 的描述"),
             F("_helper", [("v", "int")], "int", "辅助")]

    print("[1] 骨架能编译、签名正确")
    sk = author.render_skeleton(funcs)
    try:
        compile(sk, "<sk>", "exec")
        ok = True
    except SyntaxError:
        ok = False
    check("骨架可编译", ok)
    check("签名带注解", "def alpha(x: int) -> int:" in sk, sk[:80])
    check("占位体是 NotImplementedError", "raise NotImplementedError" in sk)
    check("描述里的双引号被消毒（不出现四引号结尾）", '""""' not in sk)

    print("[2] 全新填充：只填 alpha，其它留桩")
    code, missing = author.apply_bodies(sk, {"alpha": "return x + 1"})
    check("alpha 被填", "return x + 1" in code)
    check("beta 仍是桩", "raise NotImplementedError" in blocks_of(code)["beta"])
    check("未填清单正确", set(missing) == {"beta", "_helper"}, str(missing))
    compile(code, "<c>", "exec")

    print("[3] 局部重写（不变量 A）：其余函数逐字不变")
    before = blocks_of(SRC)
    # 模拟"只重写 beta"：在已有实现上替换 beta 的函数体
    src_no_beta = SRC.replace(before["beta"], "def beta(rows: list[dict]) -> dict:\n"
                                            '    """beta。"""\n    raise NotImplementedError')
    code2, _ = author.apply_bodies(src_no_beta, {"beta": "return {'count': len(rows)}"},
                                   stub_missing=False)
    after = blocks_of(code2)
    check("alpha 逐字未变", after.get("alpha") == before["alpha"], str(after.get("alpha"))[:80])
    check("_helper 逐字未变", after.get("_helper") == before["_helper"])
    check("beta 换成了新体", "return {'count': len(rows)}" in after.get("beta", ""))
    compile(code2, "<c2>", "exec")

    print("[4] 局部重写（走 _merge_skeleton，不变量 B）")
    merged = author._merge_skeleton(SRC, funcs, ["beta"])
    code3, _ = author.apply_bodies(merged, {"beta": "return {k: v for k, v in (rows[0] or {}).items()}"},
                                   stub_missing=False)
    after3 = blocks_of(code3)
    check("alpha 逐字未变", after3.get("alpha") == before["alpha"], str(after3.get("alpha"))[:80])
    check("_helper 逐字未变", after3.get("_helper") == before["_helper"])
    check("模块头（docstring/import）保留",
          code3.startswith('"""模块文档。"""') and "import math" in code3, code3[:60])
    try:
        compile(code3, "<c3>", "exec")
        ok3 = True
    except SyntaxError as exc:
        ok3 = False
        print("     编译错误：", exc)
    check("合并后可编译", ok3)

    print("[5] 缩进与空行处理（不变量 C）")
    body = "if x:\n    return 1\n\n# 注释行\nreturn 0"
    lines = author._indent_body(body)
    check("每行都缩进 4 格", all(x == "" or x.startswith("    ") for x in lines), str(lines))
    check("空行保持为空", "" in lines)
    check("注释行也缩进", any(x.strip().startswith("#") for x in lines))
    check("已经缩进过的体不会被双重缩进",
          author._indent_body("    return 1")[0] == "    return 1",
          str(author._indent_body("    return 1")))

    print("[6] 坏函数体能被点名")
    blame = author._blame_body(funcs, {"alpha": "return (1", "beta": "return {}"})
    check("只有坏的那个被点名", len(blame) == 1 and blame[0].startswith("alpha"), str(blame))
    check("好函数体不被点名", author._blame_body(funcs, {"beta": "return {}"}) == [])

    print("[7] 回归：模型少给函数体时，其它函数**不许被擦成桩**（真实事故）")
    # 事故现场：clean.py 的 clean_row 被擦成 NotImplementedError，已验收数 2→1。
    # 触发条件：本轮要"重写全部"（only=[]）且模型少返回了几个函数体。
    targets = ["alpha", "beta"]                      # 本轮目标：两个函数
    # 走真正的入口 rebuild_partial：目标被替换，模型没给的**还原成原实现**
    only_alpha, missing7 = author.rebuild_partial(SRC, funcs, targets,
                                                  {"alpha": "return x + 2"})
    after7 = blocks_of(only_alpha)
    check("缺失清单里列出了模型没给的 beta", "beta" in missing7, str(missing7))
    check("模型只给 alpha 时，beta 保持原实现（不是桩）",
          "raise NotImplementedError" not in after7.get("beta", ""),
          after7.get("beta", "")[:120])
    check("alpha 被更新", "return x + 2" in after7.get("alpha", ""))
    check("_helper 不动", after7.get("_helper") == before["_helper"])
    try:
        compile(only_alpha, "<c7>", "exec")
        ok7 = True
    except SyntaxError as exc:
        ok7 = False
        print("     编译错误：", exc)
    check("结果可编译", ok7)

    print("[8] 多行签名 + 多行 docstring：合并后必须仍可编译（真实事故）")
    # 事故现场：app/models.py:303 `create_topic_draft_record(` 的签名跨 7 行，
    # _merge_skeleton 只留了 `def ...(` 这一行 → 括号永不闭合 →
    # 日志里连续两轮 `第 303 行语法错误：'(' was never closed`，而 _blame_body
    # 查不出任何问题函数（函数体本身都是好的）→ 整文件重写 → 同一处再犯。
    SRC8 = ('"""模块文档。"""\n'
            'import math\n'
            '\n'
            '\n'
            'def wide(\n'
            '    a: int,\n'
            '    b: str = "x",\n'
            ') -> dict:\n'
            '    """wide 的说明\n'
            '    第二行也属于 docstring。\n'
            '    """\n'
            '    return {"a": a}\n'
            '\n'
            '\n'
            'def keep(a: int) -> int:\n'
            '    return a\n')
    funcs8 = [F("wide"), F("keep")]
    merged8 = author._merge_skeleton(SRC8, funcs8, ["wide"])
    check("多行签名的每一行都保留", '    b: str = "x",' in merged8, merged8[:200])
    check("多行 docstring 完整保留", "第二行也属于 docstring。" in merged8)
    try:
        compile(merged8, "<c8>", "exec")
        ok8 = True
        err8 = ""
    except SyntaxError as exc:
        ok8 = False
        err8 = str(exc)
    check("合并后可编译", ok8, err8)
    code8, _ = author.apply_bodies(merged8, {"wide": "return {'a': a}"}, stub_missing=False)
    check("填体后签名仍是多行", '    b: str = "x",' in code8, code8[:200])
    check("填体后函数体是新的", "return {'a': a}" in code8)
    try:
        compile(code8, "<c8b>", "exec")
        ok8b = True
        err8b = ""
    except SyntaxError as exc:
        ok8b = False
        err8b = str(exc)
    check("填体后可编译", ok8b, err8b)

    print("[9] 失败后重贴文件：下一轮必须看见刚被判红的那一份")
    # 事故现象：夜班日志里连续 `decision 跨函数/说不清位置，整文件重写一次`。
    # 根因：引擎手里的"文件现状"在失败后从不更新 —— file_block（喂远端的全文）、
    # existing（合并基线）、skeleton（骨架）三样都停在动手前的旧版。
    # 后果：远端看不到自己刚写坏的那份，只能凭记忆改 → 整文件重写；
    #       合并基线也是旧的 → 上一轮已经写对的部分被丢掉重来。
    import tempfile as _tf
    from core import remote as _remote_mod
    _ws = Path(_tf.mkdtemp())
    (_ws / "app" / "api").mkdir(parents=True)
    _ws.joinpath("app", "api", "auth.py").write_text(
        "def alpha(x: int) -> int:\n    return 0\n\n"
        "def beta(y: int) -> int:\n    return 0\n", encoding="utf-8")
    _prompts: list[str] = []

    def _fake_complete(prompt, **kw):
        if not kw.get("node_id"):
            # 诊断角色（只读查证）：写作轮带 node_id=bodies:<path>，诊断轮没有
            return {"ok": True, "answer": "诊断结论：beta 体不满足契约", "usage": {}}
        _prompts.append(prompt)
        if len(_prompts) == 1:
            body = ("# ===== alpha =====\nreturn 'A_V1'\n"
                    "# ===== beta =====\nreturn 'B_V1'\n")
        else:
            body = "# ===== beta =====\nreturn 'B_V2'\n"
        return {"ok": True, "answer": "```python\n" + body + "```\n", "usage": {}}

    _orig_complete, _orig_verify = _remote_mod.complete, author._verify
    _remote_mod.complete = _fake_complete
    _vcount = {"n": 0}

    def _fake_verify(w, c, scope=None):
        _vcount["n"] += 1
        if _vcount["n"] == 1:
            return {"ok": False, "feedback": "beta 不满足契约"}
        return {"ok": True, "feedback": ""}

    author._verify = _fake_verify
    try:
        _res = author.author_bodies(_ws, "app/api/auth.py",
                                    type("C", (), {"funcs": [F("alpha"), F("beta")]})(),
                                    {}, attempts=2, emit=None)
    finally:
        _remote_mod.complete, author._verify = _orig_complete, _orig_verify
    check("两次尝试都走了远端", len(_prompts) == 2, str(len(_prompts)))
    check("第 2 轮 prompt 里有第 1 轮刚写的那份（MARK: A_V1/B_V1）",
          len(_prompts) > 1 and "A_V1" in _prompts[1], (_prompts[1][:120] if len(_prompts) > 1 else ""))
    _final = _ws.joinpath("app", "api", "auth.py").read_text(encoding="utf-8")
    check("最终文件用上了第 2 轮的新体（B_V2）", "B_V2" in _final, _final[:160])
    check("第 1 轮已写对的部分没被丢掉（A_V1 仍在）", "A_V1" in _final, _final[:160])

    print("[10] 新目标函数必须被加进骨架（真实事故：引擎静默丢函数）")
    # 真机实测 2026-11-09：远端交回了**完全正确**的函数体
    # （parse_fenced_bodies → ['normalize_quota']），但 _merge_skeleton 只保留
    # **文件里已有**的函数 → 新函数不在骨架里 → apply_bodies 只能替换已有 def
    # → 新函数被静默丢掉 → 判据报"契约函数缺失：<名>（文件里根本没有这些函数）"。
    # 夜班日志里 26 次"契约函数缺失"就是这么来的 —— **不是模型不会写**。
    src_new = ("def existing_helper(x: int) -> int:\n"
                '    """已经写好的函数，别动它。"""\n'
                "    return x * 3\n")
    fns_new = [F("normalize_quota"), F("existing_helper")]
    code_new, miss_new = author.rebuild_partial(
        src_new, fns_new, ["normalize_quota"],
        {"normalize_quota": "if quota <= 0:\n    raise ValueError('x')\nreturn quota"})
    check("新函数真的进了代码", "def normalize_quota(" in code_new, code_new[-200:])
    check("新函数的实现被填进去了", "raise ValueError('x')" in code_new, code_new[-200:])
    check("既有的函数没被改坏", "return x * 3" in code_new, code_new[:200])
    try:
        compile(code_new, "<c10>", "exec")
        ok10 = True
        err10 = ""
    except SyntaxError as exc:
        ok10 = False
        err10 = str(exc)
    check("结果可编译", ok10, err10)

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    # ── 落盘后立刻补确定性 import（2026-11-10 23:10 实测）───────────────────
    # 事故：作者整文件重写冲掉了引擎补的 `from fastapi import Header`，
    # 文件带 `NameError: Header` 落盘 → 整模块判红 → 记成"模型写不出"，白跑几轮。
    print("[N] 落盘后补 import（缺 Header/HTTPException 这类确定性信息，不该让远端猜）")
    import tempfile as _tf
    from core import author as _au
    with _tf.TemporaryDirectory() as _td:
        _ws = Path(_td)
        _p = _ws / "api_mod.py"
        _p.write_text("from fastapi import APIRouter\n\nrouter = APIRouter()\n\n"
                      "def get_me(authorization: str = Header(default='')):\n"
                      "    raise HTTPException(status_code=401)\n", encoding="utf-8")
        _evs = []
        _au._fix_imports_after_write(_ws, _p, lambda k, v: _evs.append(v), "chk")
        _out = _p.read_text(encoding="utf-8")
        check("补上 from fastapi import Header", "from fastapi import Header" in _out)
        check("补上 HTTPException", "from fastapi import HTTPException" in _out)
        _au._fix_imports_after_write(_ws, _p, lambda k, v: _evs.append(v), "chk2")
        check("幂等（第二次不再改写）", len(_evs) == 1, f"{len(_evs)} 次改写")
    _asrc = (Path(__file__).resolve().parents[1] / "core" / "author.py").read_text(encoding="utf-8")
    check("两条写作路径都接了它", _asrc.count("_fix_imports_after_write(") >= 3,
          str(_asrc.count("_fix_imports_after_write(")))

    print("全部通过（骨架可编译 / 局部重写不动别人 / 缩进正确 / 坏体点名 / 多行签名 / 失败后重贴文件 / 落盘补 import）")
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
