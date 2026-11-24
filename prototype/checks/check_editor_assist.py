"""精修模式「选中即改」自检（2026-11-20）。

钉住五条：
  1. `/v1/editor/assist` 真挂在 app 上（不是只定义函数）；
  2. explain / edit / test 三种动作的 prompt 各自带上正确约束，且都包含选中代码；
  3. 空选区不浪费模型调用，明确给原因；
  4. 结果不写盘：只返回 text，由前端 diff 预览后接受/拒绝；
  5. 界面真的有动作条、diff、接受/拒绝按钮（源码钉住，防回退成纯聊天）。

跑法: python checks/check_editor_assist.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
APP = PROTO / "web" / "src" / "App.vue"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {str(detail)[:240]}" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_edassist", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()
    paths = {getattr(r, "path", "") for r in srv.app.routes}

    print("[1] 路由挂上了")
    ck("注册 /v1/editor/assist", "/v1/editor/assist" in paths, sorted(paths)[-8:])

    print("[2] 三种动作的 prompt")
    seen: list[str] = []
    prompts: dict[str, str] = {}
    real = srv._editor_complete

    def fake(prompt: str, *, source: str, model: str, node_id: str = "") -> dict:
        seen.append(prompt)
        return {"ok": True, "text": "RESULT", "model": "fake", "ms": 1,
                "tokens": 3, "source": "local"}

    srv._editor_complete = fake
    try:
        code = "def add(a, b):\n    return a + b\n"
        for action in ("explain", "edit", "test"):
            seen.clear()
            r = srv.editor_assist(srv.EditorAssistReq(
                path="a.py", action=action, selected=code, before="# pre\n",
                after="# post\n", instruction="先校验再返回" if action == "edit" else ""))
            ck(f"{action} 返回 ok", r.get("ok") is True, r)
            ck(f"{action} 带上 action", r.get("action") == action, r.get("action"))
            ck(f"{action} 返回 text", r.get("text") == "RESULT", r.get("text"))
            p = seen[-1] if seen else ""
            prompts[action] = p
            ck(f"{action} prompt 含选中代码", "def add" in p)
            ck(f"{action} prompt 含文件路径", "a.py" in p)
        ck("explain 要求不改代码", "不要改代码" in prompts["explain"])
        ck("edit 只输出改写后的代码", "只输出**改写后的完整选区代码**" in prompts["edit"])
        ck("edit 带上用户意图", "先校验" in prompts["edit"])
        ck("test 要求 pytest", "pytest" in prompts["test"])
        ck("test 覆盖边界/异常", "边界" in prompts["test"] and "异常" in prompts["test"])
    finally:
        srv._editor_complete = real

    print("[3] 空选区不调用模型")
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("不该调用模型")

    srv._editor_complete = boom
    try:
        r = srv.editor_assist(srv.EditorAssistReq(path="a.py", action="edit", selected="   "))
        ck("空选区 ok=False", r.get("ok") is False, r)
        ck("空选区给原因", bool(r.get("reason")), r.get("reason"))
        ck("空选区没调用模型", called["n"] == 0, called["n"])
        r2 = srv.editor_assist(srv.EditorAssistReq(path="a.py", action="drop", selected="x=1"))
        ck("未知动作被拒", r2.get("ok") is False, r2)
    finally:
        srv._editor_complete = real

    print("[4] 结果不写盘（只返回 text）")
    src = (PROTO / "server.py").read_text(encoding="utf-8")
    seg = src[src.index("def editor_assist("):src.index("# ---------------------------------------------------------------- 精修模式：运行与调试")]
    ck("assist 段没有 tools.write_file", "write_file" not in seg)
    ck("assist 返回 text 而非 insertion", '"text": r["text"]' in seg)

    print("[5] 界面：动作条 / diff / 接受拒绝")
    app = APP.read_text(encoding="utf-8")
    for token in ("assist-bar", "openAssist('explain')", "openAssist('edit')",
                  "openAssist('test')", "openAssist('refs')", "assistDiff",
                  "acceptAssist", "assist-panel", "assist-refs"):
        ck("界面有 " + token, token in app)
    ck("选中事件接到 refreshSelection", '@select="refreshSelection"' in app
       and '@mouseup="refreshSelection"' in app)
    ck("接受前校验文件没被改过", "ed.text !== assist.baseText" in app)
    ck("审阅模式不许接受", "当前模式只读，不能改文件" in app)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
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
    sys.exit(main())
