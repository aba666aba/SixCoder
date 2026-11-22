"""判据开放调研：查公开开发文档，找"判据在谁手里"能往引擎这边搬的平台。

这是通用差距的第四块尝试（前几轮结论：左边判据在引擎手里的已补满，右边在外部）。
这里不再空谈"希望平台开放"，而是把**已经开放了可判据接口**的平台列成可执行清单：

  · 平台/宿主；
  · 公开文档的**确切接口**（端点/API）；
  · 能拿到什么判据（可机读字段）；
  · 接入方式（谁来调、凭据从哪来）；
  · 搬过来之后，原先"判不了"的那类软件里，**哪一部分变成可判**。

诚实边界：
  · "判据开放"只搬走判据的**读取权**，不等于能自动操作平台；
  · 需要 token/授权的部分，沿用 human_once（人填一次）或 deploy 的凭据引用，
    不落盘、不进审计；
  · 没被证实开放的，一律标 `verified=False`，不写进"可用"清单。

2026-11-23 核对的公开文档（都给确切 URL）:
  · Figma REST API — GET /v1/files/:key 返回 Node JSON + styles + components；
    GET /v1/images/:key 导出图片 — develop ers.figma.com/docs/rest-api/file-endpoints；
  · GitHub REST/GraphQL API — 可查询 commit/check-run/status，判 CI 是否绿；
  · Playwright CDP — connect_over_cdp 可连真浏览器/-electron 读 DOM；
  · VS Code Extension API — executeCommand 返回结构化结果（见 host_plugins）；
  · LibreOffice UNO — headless 转换产物可判（见 host_plugins）。
"""
from __future__ import annotations

from typing import Any

SOURCES: list[dict[str, Any]] = [
    {
        "id": "figma_rest", "platform": "Figma", "category": "设计",
        "verified": True,
        "docs": "https://developers.figma.com/docs/rest-api/file-endpoints/",
        "interfaces": [
            "GET /v1/files/:key → JSON Node 树 + styles + components",
            "GET /v1/files/:key/nodes?ids= → 指定节点子树",
            "GET /v1/images/:key?ids=&format=svg → 节点渲染 URL",
        ],
        "verdicts": [
            "设计稿的色值/字号/间距可从 styles 字段机读",
            "组件名/尺寸/布局属性可机读，可与生成代码的 CSS 变量比对",
        ],
        "unlocks": "审美的一部分（设计 token 一致性）从'人判'变成'机判'——比对生成产物与设计稿 token",
        "auth": "X-Figma-Token（走 human_once 人填一次 / env 引用，不落盘）",
        "still_human": "整体美感、创意质量仍在人眼里",
    },
    {
        "id": "github_api", "platform": "GitHub", "category": "研发",
        "verified": True,
        "docs": "https://docs.github.com/en/rest/checks/runs",
        "interfaces": [
            "GET /repos/{owner}/{repo}/commits/{ref}/check-runs → CI 检查结论",
            "GET /repos/{owner}/{repo}/statuses/{ref} → 提交状态",
        ],
        "verdicts": [
            "CI 是否通过（conclusion=success）可机判",
            "构建产物/检查数量可机读",
        ],
        "unlocks": "外部真链路里的'CI 绿不绿'从人看变成机判（配合半自动补 token）",
        "auth": "Bearer token（human_once / env 引用）",
        "still_human": "代码评审意见是否合理仍是人判",
    },
    {
        "id": "playwright_cdp", "platform": "Chromium/ Electron", "category": "运行时",
        "verified": True,
        "docs": "https://playwright.dev/docs/api/class-browser#browser-connect-over-cdp",
        "interfaces": [
            "connect_over_cdp('http://127.0.0.1:<port>') → 连真浏览器/electron 窗口",
            "Page.content() / locator → DOM 与 CSS 计算值可机读",
        ],
        "verdicts": [
            "DOM 结构/CSS 变量/计算样式可机判",
            "窗口标题/可见性可机判（已在 electron 探针落地）",
        ],
        "unlocks": "宿主 UI 的一部分（结构/样式落地）可机判，不必进宿主进程",
        "auth": "本机调试端口，无需外部凭据",
        "still_human": "好不好看仍是审美判据",
    },
    {
        "id": "vscode_ext", "platform": "Visual Studio Code", "category": "开发工具",
        "verified": True,
        "docs": "https://code.visualstudio.com/api/references/vscode-api#commands.executeCommand",
        "interfaces": ["executeCommand(id, ...args) → 命令 handler 的结构化返回值"],
        "verdicts": ["扩展命令返回值可机判", "扩展写入的文件内容可机判"],
        "unlocks": "编辑器内自动化的结果可机判（见 host_plugins 的 vscode 适配器）",
        "auth": "扩展开发环境，无外部凭据",
        "still_human": "webview 视觉与交互手感",
    },
    {
        "id": "office_com", "platform": "Microsoft Office", "category": "办公",
        "verified": True,
        "docs": "https://learn.microsoft.com/office/vba/language/concepts/getting-started/understanding-automation",
        "interfaces": ["COM 自动化：CreateObject/GetObject → 文档对象可读写"],
        "verdicts": ["产物 .docx/.xlsx 内容可机读", "单元格值/段落可比对"],
        "unlocks": "办公文档的正确性从人看变成机判（见 host_plugins 的 ms_office）",
        "auth": "本机 COM，无需外部凭据",
        "still_human": "排版视觉、批注语义",
    },
]


def list_sources() -> list[dict[str, Any]]:
    import copy
    return copy.deepcopy(SOURCES)


def verified_sources() -> list[dict[str, Any]]:
    return [s for s in list_sources() if s["verified"]]


def get(source_id: str) -> dict[str, Any] | None:
    return next((s for s in list_sources() if s["id"] == str(source_id or "").strip().lower()), None)


def contract_text(source_id: str = "") -> str:
    """给远端的'判据开放'说明：哪些原本判不了的，现在能机读到什么。"""
    srcs = [get(source_id)] if source_id else verified_sources()
    srcs = [s for s in srcs if s]
    if not srcs:
        return ""
    lines: list[str] = []
    for s in srcs:
        lines.append(f"【判据开放：{s['platform']}（{s['category']}）】")
        lines.append(f"· 公开文档：{s['docs']}")
        lines.append("· 可机读接口：")
        lines += [f"    - {x}" for x in s["interfaces"]]
        lines.append("· 可机判据：")
        lines += [f"    - {x}" for x in s["verdicts"]]
        lines.append(f"· 搬过来后：{s['unlocks']}")
        lines.append(f"· 仍需人判：{s['still_human']}")
        lines.append(f"· 凭据：{s['auth']}")
    return "\n".join(lines)


def describe() -> dict[str, Any]:
    return {
        "source_count": len(SOURCES),
        "verified_count": len(verified_sources()),
        "all_docs_https": all(str(s["docs"]).startswith("https://") for s in SOURCES),
        "claim_universal": False,
        "note": "判据开放只搬读取权；动态凭据走 human_once，仍有人眼判据搬不动",
    }
