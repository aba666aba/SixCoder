"""夜班「需求协商」接口自检（2026-11-20 用户口径：需求要先问清楚再开工）。

钉住六条：
  1. 两个接口真的挂在 app 上（`/v1/night/clarify`、`/v1/night/clarify/status`）；
  2. 第一轮返回**待定项**，且此时 ready=False（还没问清就开工 = 整夜钱押在猜上）；
  3. 全部答完后 ready=True，并落盘 `night_spec.md`（夜班按这份规格开工）；
  4. 状态能从磁盘接上（关掉界面再打开不重复花钱）；
  5. 第一轮没给需求 → 400，不是 500、不是空跑；
  6. 三套界面的形态钉在源码里：夜班=对话框、开发者=编辑器内对话、只读=单按钮
     （防止以后又被合并回"一套面板打天下"）。

自检不调远端：`_clarify.default_complete` 换成替身，断言的是**我们的状态机**，
不是模型写得好不好（那个由夜班自己的端到端判据负责）。

跑法: python checks/check_clarify_api.py
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
APP = PROTO / "web" / "src" / "App.vue"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:280]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def _load_server():
    spec = importlib.util.spec_from_file_location("_srv_clarify", PROTO / "server.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def main() -> int:
    srv = _load_server()
    print("[1] 路由挂上了")
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    ck("POST /v1/night/clarify", "/v1/night/clarify" in paths)
    ck("GET /v1/night/clarify/status", "/v1/night/clarify/status" in paths)

    # 替身：模拟远端"先问 2 项，答完就够"
    calls = {"n": 0}

    def fake_complete(system: str, user: str, effort: str) -> str:
        calls["n"] += 1
        if "只回答 YES 或 NO" in user:
            return "YES"
        if "已有选择" in user:                            # 第二轮：不再追问
            return "好的，按已选内容深化：使用 SQLite 单文件存储。\n```choices\n[]\n```"
        return ("第 1 版思路：CLI + SQLite。\n```choices\n"
                '[{"key":"q1","question":"存储用什么？","options":["SQLite","CSV 文件"]},'
                '{"key":"q2","question":"金额怎么存？","options":["整数分","浮点"]}]\n```')

    real = srv._clarify.default_complete
    srv._clarify.default_complete = fake_complete
    ws = Path(tempfile.mkdtemp(prefix="clarify-"))
    try:
        print("[2] 第一轮：给出待定项，ready=False")
        r1 = srv.night_clarify(srv.ClarifyReq(repo=str(ws), requirement="做一个记账工具"))
        ck("ok", r1.get("ok") is True)
        ck("ready=False（还没问清）", r1.get("ready") is False, r1.get("ready"))
        ck("有待定项", len(r1.get("questions") or []) == 2, r1.get("questions"))
        ck("待定项带选项", all(q.get("options") for q in r1["questions"]))
        ck("有叙述文本", bool((r1.get("narrative") or "").strip()))
        ck("未生成 spec（还没确认）", not r1.get("spec_path"))
        ck("状态落盘", (ws / ".sixcoder" / "clarify.json").is_file())

        print("[3] 状态能从磁盘接上")
        st = srv.night_clarify_status(str(ws))
        ck("status exists", st.get("exists") is True)
        ck("status 里的待定项一致", len(st.get("questions") or []) == 2, st.get("questions"))
        ck("status ready=False", st.get("ready") is False)

        print("[4] 答完 → ready=True + 写 night_spec.md")
        ans = {"q1": "SQLite", "q2": "整数分"}
        r2 = srv.night_clarify(srv.ClarifyReq(repo=str(ws), requirement="做一个记账工具", answers=ans))
        ck("ready=True", r2.get("ready") is True, r2.get("ready"))
        ck("没有剩余待定项", not r2.get("questions"), r2.get("questions"))
        ck("答过的都记下来了", len(r2.get("answered") or []) == 2, r2.get("answered"))
        sp = r2.get("spec_path") or ""
        ck("生成了 spec 文件", bool(sp) and Path(sp).is_file(), sp)
        if sp and Path(sp).is_file():
            body = Path(sp).read_text(encoding="utf-8")
            ck("spec 里含需求与选择", "记账工具" in body and "SQLite" in body, body[:80])

        print("[5] 参数校验")
        ws2 = Path(tempfile.mkdtemp(prefix="clarify2-"))
        try:
            srv.night_clarify(srv.ClarifyReq(repo=str(ws2), requirement=""))
            ck("首轮没需求被拒", False, "居然没抛异常")
        except Exception as exc:                          # noqa: BLE001
            ck("首轮没需求被拒（400）", getattr(exc, "status_code", 0) == 400,
               f"{type(exc).__name__} {exc}")
        try:
            srv.night_clarify(srv.ClarifyReq(repo=str(ws2 / "__nope__"), requirement="x"))
            ck("坏仓库被拒", False)
        except Exception as exc:                          # noqa: BLE001
            ck("坏仓库被拒（400）", getattr(exc, "status_code", 0) == 400, exc)
        finally:
            shutil.rmtree(ws2, ignore_errors=True)

        print("[6] 三套界面形态（源码钉住）")
        src = APP.read_text(encoding="utf-8")
        ck("夜班模式有对话消息流", "chat-flow" in src or "chat-empty" in src)
        ck("夜班模式只有一个需求文本框", "clarify-input" in src)
        ck("待定项渲染成多选", "qitem" in src and "clarify.answers[q.key]" in src)
        ck("主按钮随状态切换（分析/开始夜班）", "clarify.ready" in src and "beginNight" not in src)
        ck("精修模式去掉常驻需求框", "dev-hint" in src)
        ck("编辑器里有悬浮对话框", "ed-chat" in src and "ed-chat-input" in src)
        ck("悬浮框有本地/远端开关", "setFillSource" in src and "edChat.source" in src)
        ck("审阅模式只有一个审查按钮", "isReadonlyMode" in src and "runReview" in src)
        ck("旧的「发送」残留块已删", src.count('@click="startTask"') <= 1,
           src.count('@click="startTask"'))
        print(f"  （替身共被调用 {calls['n']} 次）")
    finally:
        srv._clarify.default_complete = real
        shutil.rmtree(ws, ignore_errors=True)

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
