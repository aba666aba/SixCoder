"""check_wsrepair —— 修复优先机制的断言（2026-11-08 真机事故钉死）。

事故：llm 模块虚构接口（from-import 不存在的符号）→ `import app.main` 失败 →
全部 HTTP 探针红 → 观测把覆盖 5/76 清成 0/76；而当前片的契约里没有坏文件，
没人去修，夜班在毒化状态里烧钱。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import auto  # noqa: E402
from core import gates as gates_mod  # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK ' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        FAILS.append(name)
    return ok


def make_ws(files: dict) -> Path:
    ws = Path(tempfile.mkdtemp())
    for rel, content in files.items():
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return ws


HEALTHY_APP = {
    "app/__init__.py": "",
    "app/main.py": "from fastapi import FastAPI\n\napp = FastAPI()\n",
}
BROKEN_APP = {
    "app/__init__.py": "",
    "app/models.py": "def parse(x: dict) -> dict:\n    return x\n",
    "app/api.py": "from app.models import ghost_fn\n\n\ndef use():\n    return ghost_fn()\n",
    "app/main.py": "from fastapi import FastAPI\n\nimport app.api  # noqa: F401\n\napp = FastAPI()\n",
}


def main() -> int:
    print("[1] 健康工作区 / 根入口工作区 → 冒烟 None")
    ws1 = make_ws(HEALTHY_APP)
    check("健康入口冒烟通过", auto._asgi_smoke(ws1) is None)
    # 2026-11-08 口径升级（耦合点修复）：main.py 是合法入口形态，探测后会**真跑**
    # 冒烟并返回 None（成功）——旧版无视它才是 bug。
    ws2 = make_ws({"main.py": "print('hi')\n"})
    check("根 main.py 入口冒烟通过（探测口径）", auto._asgi_smoke(ws2) is None)
    ws2b = make_ws({"other.py": "x = 1\n"})
    check("真无入口才不适用", auto._asgi_smoke(ws2b) is None)

    print("[2] 破损工作区 → 冒烟给出证据（错误行 + 未解析符号清单）")
    ws3 = make_ws(BROKEN_APP)
    bad = auto._asgi_smoke(ws3)
    check("冒烟判定为坏", isinstance(bad, dict))
    check("错误摘要带 ImportError", "ImportError" in str(bad.get("error")), str(bad.get("error")))
    check("未解析清单抓到 ghost_fn 和 app.models",
          any(u.get("symbol") == "ghost_fn" for u in bad.get("unresolved") or []),
          str(bad.get("unresolved")))

    print("[3] 修复回路：假 runner 把文件修好 → workspace_repaired → True")
    events: list = []

    def _emit(kind, data=None):
        events.append((kind, data) if data is not None else kind)

    def good_runner(ws, brief, contract_path, **kw):
        # 模拟作者：把缺失的函数真正实现到 app.models（引擎只路由，写由作者做）
        (ws / "app/models.py").write_text(
            "def parse(x: dict) -> dict:\n    return x\n\n\n"
            "def ghost_fn() -> dict:\n    return {}\n", encoding="utf-8")
        events.append(("run", kw.get("slice_id")))
        return {"ok": True}

    ws4 = make_ws(BROKEN_APP)

    # 修复片的"契约合成"直连远端 LLM；自检不该打网络、也不该依赖本机远端配置。
    # 注入一个最小真实契约文件，只验"修复回路"本身（派单 → 复查 → 结论），
    # 与"契约合成质量"解耦（那是 company/contractgen 的职责）。
    _orig_cfs = auto.contract_for_slice

    def _fake_cfs(ws, sl, brief, **kw):
        q = Path(ws) / ".sixcoder" / "tmp" / "c_repair.md"
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text("# 契约：修复片\n\n- ghost_fn() -> dict\n", encoding="utf-8")
        return {"ok": True, "path": str(q)}

    auto.contract_for_slice = _fake_cfs
    ok = auto._repair_workspace(ws4, emit=_emit, runner=good_runner)
    auto.contract_for_slice = _orig_cfs
    check("修好后返回 True", ok is True)
    check("发出 workspace_repaired 事件",
          any(e[0] == "workspace_repaired" for e in events if isinstance(e, tuple)),
          str(events)[:160])
    check("修复片真的派给了执行器", any(e[0] == "run" for e in events if isinstance(e, tuple)),
          str(events)[:160])

    print("[4] 修不好的工作区 → 两轮后如实认输（False + still_broken）")

    def bad_runner(ws, brief, contract_path, **kw):
        return {"ok": True}          # 什么都不修

    ws5 = make_ws(BROKEN_APP)
    auto.contract_for_slice = _fake_cfs
    ok2 = auto._repair_workspace(ws5, emit=_emit, runner=bad_runner)
    auto.contract_for_slice = _orig_cfs
    check("两轮修不好返回 False", ok2 is False)
    check("发出 workspace_still_broken（事件流里有一次）",
          any(isinstance(e, tuple) and e[0] == "workspace_still_broken" for e in events),
          str([e[0] for e in events if isinstance(e, tuple)])[:160])

    print("[5] 提交时冒烟闸门 gate_import_smoke（company 写文件后调用）")
    ws6 = make_ws(HEALTHY_APP)
    g_ok = gates_mod.gate_import_smoke(ws6)
    check("健康入口冒烟 ok", g_ok.ok, g_ok.detail)
    g_bad = gates_mod.gate_import_smoke(ws3)
    check("坏入口冒烟红且带 ImportError", (not g_bad.ok) and "ImportError" in g_bad.detail,
          g_bad.detail[:100])
    g_na = gates_mod.gate_import_smoke(ws2)
    check("根 main.py 形态冒烟 ok（探测口径）", g_na.ok and "成功" in g_na.detail, g_na.detail)
    g_na2 = gates_mod.gate_import_smoke(ws2b)
    check("真无入口才不适用（ok=True）", g_na2.ok and "不适用" in g_na2.detail, g_na2.detail)

    print("[6] 导入期兼容判据（props 文本模式 → call_ok，必然失败才派得出单）")
    cs = __import__("core.props", fromlist=["from_requirement"]).from_requirement(
        "- install_body_capture() 无参调用必须成功（导入期兼容）。\n",
        {"install_body_capture"})
    check("无参调用判据生成 call_ok", bool(cs) and cs[0]["kind"] == "call_ok"
          and cs[0]["expr"] == "m.install_body_capture()", str(cs)[:120])
    check("TypeError 在允许崩溃表里（当前实现必判红）",
          "TypeError" in (cs[0].get("exc_ok") or []), str(cs[0].get("exc_ok")))

    print("[7] 接口对齐（engine_interface_fix）：签名缺默认值 + 无参调用点 → 引擎两行修")
    _MODELS_SRC = ("def install_body_capture(app: dict) -> dict:\n"
                   "    if not isinstance(app, dict):\n"
                   "        raise ValueError('app 必须是 dict')\n"
                   "    return dict(app)\n")
    ws7 = make_ws({"app/__init__.py": "",
                   "app/models.py": _MODELS_SRC,
                   "app/svc.py": ("from app.models import install_body_capture\n\n\n"
                                  "install_body_capture()\n"),
                   "app/main.py": "import app.svc\n"})
    evt: list = []
    fixed = auto._engine_interface_fix(ws7, "install_body_capture",
                                       emit=lambda k, d=None: evt.append(k))
    check("引擎实施了接口对齐", fixed is True and "engine_interface_fix" in evt, str(evt))
    r = auto._asgi_smoke(ws7)
    check("对齐后入口可导入", r is None, str(r)[:120])
    src = (ws7 / "app/models.py").read_text(encoding="utf-8")
    check("签名带默认值 + None 守卫", "app: dict = None" in src and "if app is None:" in src,
          src[:150])
    check("幂等：已有默认值不再动",
          auto._engine_interface_fix(ws7, "install_body_capture") is False)

    print()
    if FAILS:
        print(f"失败：{FAILS}")
        return 1
    print("全部通过（健康放行 / 破损带证据 / 修复回路复用执行器 / 修不好如实认输 / 提交冒烟三态）")
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
