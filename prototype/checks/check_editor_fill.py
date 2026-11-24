"""编辑器「本地模型自动填充」自检（2026-11-20 用户口径）。

钉住五条：
  1. 接口真的挂上（`/v1/editor/fill`，不是"函数写了没接线"）；
  2. 语言按扩展名认（喂错语言比不喂更糟：模型会按 python 规矩写 vue）；
  3. 模型爱包的 ``` 围栏必须剥掉（否则整段粘进文件就是语法错误）；
  4. 空输入不浪费一次模型调用（本地 7B 一次十几秒，白跑很贵）；
  5. 没有本地模型时**明确说原因**，不抛 500（界面要能把话显示给用户）。

跑法: python checks/check_editor_fill.py
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:260]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_edfill", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()

    print("[1] 路由挂上了")
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    ck("注册 /v1/editor/fill", "/v1/editor/fill" in paths, sorted(paths)[-6:])

    print("[2] 语言识别")
    cases = [("a.py", "python"), ("App.vue", "vue"), ("x.tsx", "tsx"), ("m.go", "go"),
             ("q.sql", "sql"), ("conf.yml", "yaml"), ("Makefile", "text"), ("z.rs", "rust")]
    for path, want in cases:
        got = srv._editor_lang(path)
        ck(f"{path} → {want}", got == want, got)

    print("[3] 剥掉 markdown 围栏")
    ck("```python ... ``` 被剥",
       srv._strip_fence("```python\nprint(1)\n```") == "print(1)",
       srv._strip_fence("```python\nprint(1)\n```"))
    ck("无围栏原样保留",
       srv._strip_fence("print(1)") == "print(1)")
    ck("纯文本代码块也剥",
       srv._strip_fence("```\nx = 1\n```") == "x = 1")

    print("[4] 空输入不浪费调用")
    r = srv.editor_fill(srv.EditorFillReq(before="   ", selected="", path="a.py"))
    ck("空上下文返回 ok=False", r.get("ok") is False, r)
    ck("给了可读原因", bool(r.get("reason")), r.get("reason"))
    # 只有指令、没有上下文时不拒：用户在空文件里说"写一个快排"也算明确意图
    r1b = srv.editor_fill(srv.EditorFillReq(before="", selected="", instruction="写一个快排", path="a.py"))
    ck("只有指令时不判空（不误拒）", r1b.get("reason") != "光标前没有内容，也没写要求，没什么可生成的", r1b)

    print("[4b] 本地/远端开关（精修模式自己选）")
    req = srv.EditorFillReq()
    ck("默认 source=local", req.source == "local", req.source)
    ck("可选 remote", srv.EditorFillReq(source="remote").source == "remote")
    src = (PROTO / "server.py").read_text(encoding="utf-8")
    ck("远端分支存在（remote.complete）", "use_remote" in src and 'node_id="editor_fill"' in src)
    ck("远端返回里带 source 字段", '"source": "remote" if use_remote else "local"' in src)

    print("[5] 没有本地模型时给原因，不抛异常")
    import core.ollama as _ol
    real_alive = _ol.alive
    try:
        _ol.alive = lambda: False                          # 模拟 Ollama 没启动
        r2 = srv.editor_fill(srv.EditorFillReq(before="def f():\n    ", path="a.py"))
        ck("返回 ok=False", r2.get("ok") is False, r2)
        ck("原因里点明 Ollama", "Ollama" in str(r2.get("reason", "")), r2.get("reason"))
    except Exception as exc:                               # noqa: BLE001
        ck("没有本地模型时不抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        _ol.alive = real_alive

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
