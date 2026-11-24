"""实验：远端「关闭思考」到底省钱还是更费钱？

问题（甲方问的）：关闭 thinking 省 completion，但会不会因为方案质量差而反复更多次，
实际更贵、更慢？

做法：造一个**真实会失败、且失败原因只在实现文件**的工作区（缺 divide，测试本身是好的），
用真实的接力包（handoff.build_packet）分别以 thinking=off / on 各调一次远端，
再走真实的 handoff.apply_remote（本地渲染 + 全部闸门）。对比：
  是否一次过 · prompt/completion/reasoning tokens · 耗时 · 费用 · 写出的编辑数

用法：python tools/exp_thinking.py
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PROTO = HERE.parent.parent
sys.path.insert(0, str(PROTO))

from core import agent, db, handoff, remote, settings_store  # noqa: E402

CALC = '''"""极简计算模块。"""


def add(a: int, b: int) -> int:
    """返回两数之和。"""
    return a + b


def sub(a: int, b: int) -> int:
    """返回两数之差。"""
    return a - b
'''

TEST = '''import pytest

from calc import add, sub, divide


def test_add():
    assert add(2, 3) == 5


def test_sub():
    assert sub(5, 3) == 2


def test_divide_ok():
    assert divide(6, 3) == 2.0


def test_divide_zero():
    with pytest.raises(ValueError):
        divide(1, 0)
'''

REQ = ('给 calc.py 增加 divide(a, b) 函数：返回 a/b（浮点），当 b 为 0 时抛 '
       'ValueError("除数不能为 0")。并补上对应的 pytest 测试。')

CONTRACT = 'divide(a: float, b: float) -> float：返回 a/b；b 为 0 时抛 ValueError'


def prepare(task_id: str) -> Path:
    ws = agent.WORKSPACES / task_id
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "calc.py").write_text(CALC, encoding="utf-8")
    (ws / "test_calc.py").write_text(TEST, encoding="utf-8")
    db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    db.execute("DELETE FROM nodes WHERE task_id=?", (task_id,))
    db.execute("DELETE FROM events WHERE task_id=?", (task_id,))
    for table in ("gates", "gate_runs", "specs"):
        try:
            db.execute(f"DELETE FROM {table} WHERE task_id=?", (task_id,))
        except Exception:                                # noqa: BLE001
            pass
    db.execute("INSERT INTO tasks(id,repo,workspace,requirement,target,state,model,created_at,"
               "updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
               (task_id, str(ws), str(ws), REQ, "calc.py", "running", "qwen2.5:7b",
                db.now(), db.now()))
    db.execute("INSERT INTO nodes(id,task_id,seq,kind,path,contract,status,attempts,created_at)"
               " VALUES(?,?,?,?,?,?,?,?,?)",
               (f"{task_id}-n1", task_id, 1, "impl", "calc.py", CONTRACT, "stuck", 3, db.now()))
    db.execute("UPDATE nodes SET last_error=? WHERE id=?",
               ("【G5 测试】ImportError: cannot import name 'divide' from 'calc'", f"{task_id}-n1"))
    return ws


def run_once(thinking: str, tag: str) -> dict:
    task_id = f"t-exp-{tag}"
    ws = prepare(task_id)
    old = settings_store.get_remote().get("thinking")
    settings_store.update_remote({"thinking": thinking})
    try:
        packet = handoff.build_packet(task_id, f"{task_id}-n1")
        md = packet["markdown"]
        t0 = time.time()
        r = remote.complete(md, task_id=task_id, node_id=f"{task_id}-n1", workspace=str(ws))
        ms = int((time.time() - t0) * 1000)
        if not r.get("ok"):
            return {"tag": tag, "ok": False, "error": r.get("error"), "ms": ms}
        ar = handoff.apply_remote(task_id, f"{task_id}-n1", r.get("answer") or "")
        usage = r.get("usage") or {}
        return {"tag": tag, "ok": bool(ar.get("ok")), "reason": str(ar.get("reason") or "")[:200],
                "applied": ar.get("applied"), "ms": ms,
                "prompt": usage.get("prompt_tokens"), "completion": usage.get("completion_tokens"),
                "answer_chars": len(r.get("answer") or ""),
                "packet_chars": len(md)}
    finally:
        settings_store.update_remote({"thinking": old or "off"})


def main() -> int:
    ok, why = remote.available()
    if not ok:
        print("远端不可用：", why)
        return 1
    print(f"模型：{settings_store.get_remote().get('model')}｜远端可用\n")

    rows = []
    for thinking, tag in (("off", "off"), ("on", "on")):
        print(f"--- thinking={thinking} ---")
        r = run_once(thinking, tag)
        rows.append(r)
        print("  结果:", "通过" if r.get("ok") else "未通过", "|", r.get("reason") or r.get("error"))
        print(f"  耗时 {r.get('ms')}ms｜prompt {r.get('prompt')}｜completion {r.get('completion')}"
              f"｜回答 {r.get('answer_chars')} 字符｜包体 {r.get('packet_chars')} 字符")

    print("\n=== 对比 ===")
    base = None
    for r in rows:
        cost = None
        try:
            import sys as _s
            _s.path.insert(0, str(PROTO))
            from core import remote as _rm
            cost = _rm.price_of(thinking="off") if False else None
        except Exception:
            pass
        print(f"{r['tag']:>4}: 通过={r.get('ok')} 耗时={r.get('ms')}ms "
              f"completion={r.get('completion')} 回答={r.get('answer_chars')}字符")
    print("\n用量总账:", json.dumps({k: v for k, v in remote.usage_summary().items()
                                     if k in ("calls", "prompt_tokens", "completion_tokens",
                                              "reasoning_tokens", "cost_usd")},
                                    ensure_ascii=False))
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
