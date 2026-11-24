"""端点—前端覆盖自检（2026-11-23）。

为什么需要：这个项目已经出现 **5 次**"后端做完了、前端没入口"——
半自动回填 / 多机编排 / walkthrough / 运维单元 / 插件探针。
每次都是"功能在，用户点不到"，靠人记性去补必然还会漏第 6 次。

这道自检把规矩变成机器可判：
  · 扫 `server.py` 里所有 `/v1/*` 端点，对照 `App.vue` 的调用；
  · 覆盖不到、又不在白名单里的 → **判红**（新接口必须同时给出前端入口）；
  · 白名单分两类：`API_ONLY`（设计上只给工具/CLI，附理由）和
    `TODO_UI`（该接但还没接，打 WARN 不计失败——不掩盖，但也不假装已完成）。

判定要防误伤：前端用模板串拼 URL（`/v1/x/${id}`）时按**前缀**匹配；
白名单同理按前缀，最长匹配优先。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"
APP = ROOT / "web" / "src" / "App.vue"

PASS: list[str] = []
FAIL: list[str] = []

# —— 设计上只给工具/CLI/夜班用的端点（不是"忘了接"）——
# 每条都要写清"为什么界面不需要"，而不是"还没做"——后者属于 TODO_UI。
API_ONLY = {
    "/v1/fs/tree":
        "工具通道（探针/CLI 要递归+limit 的扁平树）；编辑器用 /v1/repo/tree 的懒加载单层树。"
        "两者跳过目录集合已对齐（2026-11-23 补 venv/env/target）",
    "/v1/remote/tool": "远端工具循环入口，由夜班/CLI 驱动（界面不该直接调工具循环）",
    "/v1/settings/remote/models/primary":
        "单独切主模型的快捷入口（CLI/API 用）；界面上切主模型走 POST /v1/settings/remote "
        "整体保存（见 loadRemote 的保存路径），两条路等价",
    # —— Git 只读接口：**产品决定不做界面**，不是"待接" ——
    # 用户明确要求移除编辑器里的 Git 按钮（避免误操作），所以没有分支/diff/历史视图；
    # 接口保留给 CLI 与脚本做只读查询（gitops 仍是夜班提交代码的通道）。
    "/v1/git/branches": "产品决定不做 Git 界面（用户要求移除编辑器 Git 按钮）；接口留给 CLI/脚本只读查询",
    "/v1/git/diff": "同上：无 diff 视图，接口留给 CLI/脚本",
    "/v1/git/log": "同上：无历史视图，接口留给 CLI/脚本",
    # 2026-11-23：`/v1/audit` 与 `/v1/docs/stage3` 已在**审阅模式右栏**接上界面（扫描 / 生成文档），
    # 所以从"仅 API"名单里撤掉——白名单只放"真的没接"的端点，接了还留着会掩盖问题。
    "/v1/audit/report":
        "审计报告的人读版（Markdown 文本）：界面右栏已直接用结构化的 /v1/audit 渲染，"
        "这份留给 CLI 输出与外部工具拉取，不重复接一次",
}

# —— 该接、但还没接（打 WARN，不计失败）——
# 2026-11-23：上一轮的 4 个已全部接上（探针建议 / 判据约定 / 验收覆盖声明 / 用量统计），
# 目前没有待接项。以后新增接口若一时接不了前端，登记在这里并写清原因。
TODO_UI: dict[str, str] = {}


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" :: {detail[:240]}" if detail and not ok else ""))


def _endpoints() -> set[str]:
    src = SERVER.read_text(encoding="utf-8")
    return set(re.findall(r'@app\.(?:get|post|put|delete)\("(/v1/[^"]+)"', src))


def _base(ep: str) -> str:
    """`/v1/x/{id}` → `/v1/x`；用于前缀比较。"""
    return ep.split("{")[0].rstrip("/")


def _covered(ep: str, ui: str) -> bool:
    base = _base(ep)
    return base in ui or f"{base}/" in ui


def _allow(table: dict, ep: str) -> str:
    for k in sorted(table, key=len, reverse=True):
        if ep == k or ep.startswith(k):
            return table[k]
    return ""


def main() -> int:
    print("端点—前端覆盖（防『后端做完、前端没入口』第 6 次）")
    eps = _endpoints()
    ui = APP.read_text(encoding="utf-8")
    check("扫到端点", bool(eps), str(len(eps)))

    missing: list[str] = []
    todo: list[tuple[str, str]] = []
    api_only = 0
    for ep in sorted(eps):
        if _covered(ep, ui):
            continue
        why_todo = _allow(TODO_UI, ep)
        if why_todo:
            todo.append((ep, why_todo))
            continue
        if _allow(API_ONLY, ep):
            api_only += 1
            continue
        missing.append(ep)

    print(f"  端点 {len(eps)}｜前端已接 {len(eps) - len(missing) - len(todo) - api_only}"
          f"｜仅 API {api_only}｜待接 {len(todo)}｜未登记 {len(missing)}")
    check("没有『未登记』的无入口端点", not missing, "；".join(missing[:8]))

    print("[2] 白名单本身要诚实")
    check("『仅 API』都写了理由", all(str(v).strip() for v in API_ONLY.values()))
    check("『待接』都写了说明", all(str(v).strip() for v in TODO_UI.values()))
    # 白名单不许收"其实有前端入口"的端点（防用白名单掩盖已接好的）
    stale = [k for k in list(API_ONLY) + list(TODO_UI) if _covered(k, ui)]
    check("白名单里没有已经接好的端点（避免掩盖）", not stale, "；".join(stale[:6]))

    if todo:
        print(f"\n  待接（不计失败，但请排期）：{len(todo)} 个")
        for ep, why in todo:
            print(f"    - {ep}：{why}")

    print()
    print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
    if FAIL:
        print("失败项：" + ", ".join(FAIL))
        return 1
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
