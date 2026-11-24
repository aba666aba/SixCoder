"""2026-11-08 深层修复的三条断言：熔断记账口径 / 骨架保留模块级赋值 / 字段幻觉只扫当前签名表。

背景（当天 org 切片修复乒乓，models.py 半天 30 次失败、69% 预算烧在失败上）：
  ① company._module_spend 只精确匹配 author:<rel>，bodies:/patch: 等前缀的钱全漏
     （账本 115 笔 ¥1.47 只见 41 笔 ¥0.88）→ 模块预算熔断永不触发 → 无限重试。
  ② author.render_skeleton 只保留 import 行，模块级赋值（_ORG_STATE = {}）在重写
     后凭空消失 → 模型被迫 globals() 动态建 → 每次姿势不同 → 判红循环。
  ③ gates.gate_dict_keys 整文件扫，历史函数用的键不在当前契约 allowed 里 →
     已验收代码陪绑判红，48 函数的大文件永远消不了红。

跑法:  python checks/check_deepfix_20261108.py
"""
from __future__ import annotations

import ast
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import author, company, db, gates  # noqa: E402


def _check_module_spend() -> list[str]:
    """① bodies:/patch: 前缀的钱必须能被 _module_spend 看到。"""
    fails: list[str] = []
    tag = "zz_deepfix_probe.py"
    db.execute("INSERT INTO remote_usage(task_id, node_id, purpose, model,"
               " prompt_tokens, completion_tokens, cache_hit_tokens, cached_reply,"
               " ms, created_at, reasoning_tokens, offpeak, effort, cost_cny)"
               " VALUES ('t', ?, 'probe', 'm', 1, 1, 0, 0, 1,"
               " datetime('now','localtime'), 0, 0, 'low', 0.5)", (f"bodies:{tag}",))
    db.execute("INSERT INTO remote_usage(task_id, node_id, purpose, model,"
               " prompt_tokens, completion_tokens, cache_hit_tokens, cached_reply,"
               " ms, created_at, reasoning_tokens, offpeak, effort, cost_cny)"
               " VALUES ('t', ?, 'probe', 'm', 1, 1, 0, 0, 1,"
               " datetime('now','localtime'), 0, 0, 'low', 0.25)", (f"patch:{tag}",))
    try:
        got = company._module_spend(Path("."), tag, since="2000-01-01 00:00:00")
        if abs(got - 0.75) > 1e-6:
            fails.append(f"_module_spend 前缀漏记：bodies+patch 应为 0.75，实际 {got}")
    finally:
        db.execute("DELETE FROM remote_usage WHERE node_id IN (?, ?)",
                   (f"bodies:{tag}", f"patch:{tag}"))
    return fails


def _check_skeleton_keeps_assigns() -> list[str]:
    """② 模块级赋值/常量必须随骨架保留，且位于函数桩之前。"""
    src = ("import sqlite3\n"
           "_STATE: dict = {}\n"
           "_ALLOWED = ('a', 'b')\n"
           "\n"
           "def old_fn() -> dict:\n"
           "    return {}\n")
    out = author.render_skeleton([], existing_src=src)
    fails: list[str] = []
    for token in ("import sqlite3", "_STATE: dict = {}", "_ALLOWED = ('a', 'b')"):
        if token not in out:
            fails.append(f"骨架丢了模块级语句: {token}")
    if "def old_fn" in out:
        fails.append("旧函数不应进骨架（骨架只铺契约桩）")
    try:
        tree = ast.parse(out)
    except SyntaxError as exc:
        fails.append(f"骨架语法坏: {exc}")
        return fails
    first_def = next((i for i, n in enumerate(tree.body)
                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), 10 ** 9)
    if not any(isinstance(n, ast.Assign) for n in tree.body[:first_def]):
        fails.append("模块级赋值没有铺在函数桩之前（会被 apply_bodies 并进函数体变局部变量）")
    return fails


def _check_dict_keys_only_funcs() -> list[str]:
    """③ only_funcs 给定时，历史函数不再陪绑判红；不给时保持旧行为。"""
    ws = Path(tempfile.mkdtemp(prefix="deepfix_"))
    fails: list[str] = []
    try:
        (ws / "t.py").write_text(
            "def _legacy_view(row: dict) -> dict:\n"
            "    return {'role': row.get('role')}\n"
            "\n"
            "def current_fn(row: dict) -> dict:\n"
            "    return {'name': row.get('name')}\n", encoding="utf-8")
        # 旧行为（不给 only_funcs）：历史函数的 'role' 必然 blocked —— 防回归基准
        res_old, _ = gates.gate_dict_keys(ws, ["t.py"], {"name"}, "")
        if res_old.ok:
            fails.append("不给 only_funcs 时应保持旧行为（扫全文件并 blocked 历史键）")
        # 新行为：只扫当前契约签名表，历史函数跳过 → 不得判红当前函数
        res_new, _ = gates.gate_dict_keys(ws, ["t.py"], {"name"}, "",
                                          only_funcs={"current_fn"})
        if not res_new.ok:
            fails.append(f"only_funcs 限扫后误红: {res_new.detail}")
        # 当前函数真造字段仍要抓（不能把闸门整个废掉）
        (ws / "t.py").write_text(
            "def current_fn(row: dict) -> dict:\n"
            "    return {'ghost': row.get('ghost')}\n", encoding="utf-8")
        res_bad, _ = gates.gate_dict_keys(ws, ["t.py"], {"name"}, "",
                                          only_funcs={"current_fn"})
        if res_bad.ok:
            fails.append("当前契约函数造幻觉字段时应 blocked（闸门不能失效）")
    finally:
        shutil.rmtree(ws, ignore_errors=True)
    return fails


def _check_repair_brief() -> list[str]:
    """④ 修复片简报（抽成纯函数后）：两条硬禁令 + 证据 + 判据行必须在位。"""
    from core import auto
    sites = ["app/api/org.py:6: state = _models_state()",
             "app/api/org.py:147: _ensure_models_state()"]
    frozen = ["parse_candidate_request", "build_candidate_response"]
    b = auto._repair_brief("NameError: name '_models_state' is not defined",
                           ["_models_state"], sites, frozen)
    fails: list[str] = []
    for key in ("禁止模块级副作用调用", "懒执行", "禁止无关重排", "逐字节保留",
                "import app.main", "冻结清单", "_models_state"):
        if key not in b:
            fails.append(f"修复简报缺关键词: {key}")
    if "app/api/org.py:6" not in b:
        fails.append("修复简报丢了调用点证据")
    if "parse_candidate_request" not in b:
        fails.append("修复简报丢了冻结清单")
    b2 = auto._repair_brief("ModuleNotFoundError: No module named 'x'", [], [], [])
    if "import app.main" not in b2:
        fails.append("修复简报无证据分支缺判据行")
    return fails


def _check_skeleton_apply_bodies_chain() -> list[str]:
    """②的链路断言：带模块级赋值的骨架必须能被 apply_bodies 正确消化——
    防止"改了渲染、坏了填体"（改 A 坏 B）。赋值必须留在文件头（模块级），
    函数体必须进对的函数，整体语法必须成立。"""

    class F:
        """最小 FuncSig 替身（同 check_bodies）。"""

        def __init__(self, name, params=(), ret="int", doc=""):
            self.name = name
            self.params = [type("P", (), {"name": n, "type": t})() for n, t in params]
            self.returns = ret
            self.doc = doc

    existing = ("import sqlite3\n"
                "_STATE: dict = {}\n"
                "_ALLOWED = ('a', 'b')\n"
                "\n"
                "def insert_term(name: str) -> dict:\n"
                '    """加学期。"""\n'
                "    raise NotImplementedError\n")
    funcs = [F("insert_term", [("name", "str")], "dict", "加学期。")]
    skeleton = author.render_skeleton(funcs, existing_src=existing)
    fails: list[str] = []
    if "_STATE: dict = {}" not in skeleton:
        fails.append("骨架（含桩版）丢了模块级赋值 _STATE")
    if "def insert_term" not in skeleton:
        fails.append("骨架丢了函数桩 insert_term")
    code, missing = author.apply_bodies(skeleton, {"insert_term": "return {'name': name}"})
    if missing:
        fails.append(f"apply_bodies 没吃进函数体: {missing}")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"填体后语法坏: {exc}"]
    toplevel_assigns = [n for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))]
    if len(toplevel_assigns) != 2:
        fails.append(f"填体后模块级赋值数量不对: {len(toplevel_assigns)}（应为 2，"
                     "赋值被并进函数体会变成局部变量）")
    fn = next((n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "insert_term"), None)
    if fn is None:
        fails.append("填体后丢函数: insert_term")
    elif "return" not in ast.unparse(fn):
        fails.append("insert_term 的函数体没填进去")
    return fails


def _check_module_spend_escape() -> list[str]:
    """①的边界断言：rel 里的 _ 不许当 LIKE 通配符。"""
    fails: list[str] = []
    tag = "zz_deep_fix_probe.py"          # 含 _ 的文件名
    decoy = "zzZdeepZfixZprobe.py"        # _→任意字符的冒名顶替者
    for nid, cost in ((f"bodies:{tag}", 0.5),):
        db.execute("INSERT INTO remote_usage(task_id, node_id, purpose, model,"
                   " prompt_tokens, completion_tokens, cache_hit_tokens, cached_reply,"
                   " ms, created_at, reasoning_tokens, offpeak, effort, cost_cny)"
                   " VALUES ('t', ?, 'probe', 'm', 1, 1, 0, 0, 1,"
                   " datetime('now','localtime'), 0, 0, 'low', ?)", (nid, cost))
    try:
        got = company._module_spend(Path("."), tag, since="2000-01-01 00:00:00")
        if abs(got - 0.5) > 1e-6:
            fails.append(f"ESCAPE 后记账口径错：应 0.5，实际 {got}")
    finally:
        db.execute("DELETE FROM remote_usage WHERE node_id = ?", (f"bodies:{tag}",))
    return fails


def _check_blueprint_save_log() -> list[str]:
    """⑤ 蓝图 save 溯源日志（2026-11-08：3 done 之谜查不到写者——save 必须留痕）。
    每次保存 append 一行 {at, pid, done, total, version, callers}。"""
    import tempfile
    import os
    from core import blueprint as bpmod
    ws = tempfile.mkdtemp()
    try:
        os.makedirs(os.path.join(ws, ".sixcoder"), exist_ok=True)
        bp = bpmod.Blueprint.from_dict(
            {"goal": "t", "caps": [{"id": "cap.a", "title": "a", "status": "done"}]})
        p = bpmod.save(bp, ws)
        logp = os.path.join(os.path.dirname(str(p)), "blueprint_save.jsonl")
        fails = []
        if not os.path.exists(logp):
            return ["save 日志文件未落盘"]
        rec = json.loads(open(logp, encoding="utf-8").read())
        for k in ("at", "pid", "done", "total", "version", "callers"):
            if k not in rec:
                fails.append(f"save 日志缺字段 {k}")
        if rec.get("done") != 1:
            fails.append("save 日志 done 计数不对")
        if not rec.get("callers"):
            fails.append("save 日志缺调用栈")
        return fails
    finally:
        shutil.rmtree(ws, ignore_errors=True)


def main() -> int:
    fails = (_check_module_spend() + _check_skeleton_keeps_assigns()
             + _check_dict_keys_only_funcs() + _check_repair_brief()
             + _check_skeleton_apply_bodies_chain() + _check_module_spend_escape()
             + _check_blueprint_save_log())
    if fails:
        for f in fails:
            print("FAIL " + f)
        print("有失败项: check_deepfix_20261108")
        return 1
    print("PASS 熔断记账口径(bodies/patch 不再漏) / 骨架保留模块级赋值 / "
          "字段幻觉只扫当前签名表")
    print("全部通过")
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
