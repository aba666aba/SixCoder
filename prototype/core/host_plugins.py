"""闭源宿主插件适配清单：按**公开自动化文档**把常用办公软件接到引擎。

解决哪条红线（承接通用差距讨论）：
  · PR / Office / CAD 这类闭源宿主，引擎进不去进程，只能插件桥 + 验产物。
  · 这里按各家**公开文档**列出：桥接接口、可机械验的判据（能验到什么）、
    验不到的如实标 `human_in_loop=True`。**不宣称能自动操作宿主界面**。

诚实边界（必须说清，防谎报）：
  · "有公开自动化接口" ≠ "引擎能全自动"。能验的只是**产物**（文件存在/格式正确/
    内容可解析/扩展命令返回值），宿主内部的视觉状态、交互手感判据仍在人眼里。
  · 每个适配器都给 `verdict_source`（判据在谁手里）：`artifact`=产物可判、
    `extension_command`=扩展命令返回值可判、`human`=只能人判。
  · 没有本机二进制/宿主时，探针如实报"测不了"（fail-closed），绝不假装通过。

公开的自动化依据（2026-11-23 核对，均官方文档/权威来源）:
  · Microsoft Office — COM 自动化/VBA：learn.microsoft.com/office/vba/language/concepts/
    getting-started/understanding-automation；Excel 对象模型 API learn.microsoft.com/
    office/vba/api/overview/excel。判据：产物 .docx/.xlsx 可被 openpyxl/python-docx 解析。
  · LibreOffice — UNO bridge（Python）：help.libreoffice.org + UNO `--headless --accept=socket`；
    判据：headless 转换出的 PDF/目标格式存在且可打开（本模块给真探针路径）。
  · Visual Studio Code — Extension API：code.visualstudio.com/api/extension-guides/command；
    `registerCommand` + `executeCommand` 可返回结果 → 扩展命令返回值是可判据。
  · Adobe Premiere — CEP / ExtendScript（已在独立项目 PRAutoCut 落地）；判据：导出产物存在。
  · WPS Office — 兼容 VBA/JS 宏接口（公开文档），判据：产物可解析；宏执行状态多为人在环。
"""
from __future__ import annotations

from typing import Any

ADAPTERS: list[dict[str, Any]] = [
    {
        "id": "libreoffice", "label": "LibreOffice", "category": "办公套件",
        "bridge": "UNO bridge（Python uno 模块，--headless --accept=socket）",
        "docs": [
            "https://help.libreoffice.org/latest/en-US/text/sbasic/python/python_programming.html",
            "https://api.libreoffice.org/",
        ],
        "verdict_source": "artifact",
        "can_verify": [
            "headless 转换产物存在且非空（docx→pdf、xlsx→csv 等）",
            "产物可被标准库/第三方解析（PDF 头 %PDF、CSV 可读、zip 结构）",
            "转换命令 rc==0 且无异常日志",
        ],
        "cannot_verify": ["文档排版是否美观", "批注/修订是否被正确接受（语义层）"],
        "human_in_loop": False,
        "probe": {"kind": "cli", "start_cmd":
                  'soffice --headless --convert-to pdf --outdir <out> <in>',
                  "assert": "产物 <in>.pdf 存在且以 %PDF 开头"},
        "installed_check": ["soffice", "--version"],
    },
    {
        "id": "ms_office", "label": "Microsoft Office（Word/Excel）", "category": "办公套件",
        "bridge": "COM 自动化 / VBA（CreateObject、对象模型）",
        "docs": [
            "https://learn.microsoft.com/office/vba/language/concepts/getting-started/understanding-automation",
            "https://learn.microsoft.com/office/vba/api/overview/excel",
        ],
        "verdict_source": "artifact",
        "can_verify": [
            "产物 .docx/.xlsx 存在且可被 python-docx/openpyxl 解析",
            "Excel 单元格值/公式按约定写入且读回一致",
            "宏执行退出码 / COM 调用返回值",
        ],
        "cannot_verify": ["界面交互过程", "格式细节的视觉一致性", "弹窗对话框的处理"],
        "human_in_loop": False,
        "probe": {"kind": "py_call", "assert":
                  "openpyxl 打开 xlsx 读回约定单元格值；python-docx 读回约定段落"},
        "installed_check": [],   # Windows 上 Office 是否装了 COM 组件，运行时探
    },
    {
        "id": "vscode", "label": "Visual Studio Code", "category": "开发工具",
        "bridge": "Extension API：registerCommand / executeCommand（命令可返回值）",
        "docs": [
            "https://code.visualstudio.com/api/extension-guides/command",
            "https://code.visualstudio.com/api/references/vscode-api#commands.executeCommand",
        ],
        "verdict_source": "extension_command",
        "can_verify": [
            "扩展命令 executeCommand 的返回值（结构化结果）",
            "命令注册成功、命令面板可调用",
            "产物文件被扩展写入后的内容断言",
        ],
        "cannot_verify": ["webview 的视觉呈现", "用户交互手感", "主题配色好不好看"],
        "human_in_loop": False,
        "probe": {"kind": "cli", "start_cmd": "code --extensionDevelopmentPath <ext> --command <id>",
                  "assert": "命令返回值符合约定结构"},
        "installed_check": ["code", "--version"],
    },
    {
        "id": "premiere", "label": "Adobe Premiere Pro", "category": "创作工具",
        "bridge": "CEP 扩展 + ExtendScript（已在独立项目 PRAutoCut 落地）",
        "docs": [
            "https://developer.adobe.com/developer-distribution/creative-cloud/docs/guides/cep/",
            "https://developer.adobe.com/indesign-scripting/",
        ],
        "verdict_source": "artifact",
        "can_verify": [
            "导出视频文件存在、时长/分辨率/编码符合约定（ffprobe）",
            "字幕/关键帧数据文件被正确写入",
        ],
        "cannot_verify": ["剪辑节奏好不好", "转场是否顺滑", "画面审美"],
        "human_in_loop": True,   # 剪辑质量是审美判据，必须人看
        "probe": {"kind": "cli", "start_cmd": "ffprobe -v error -show-format <out>",
                  "assert": "导出文件存在且时长/分辨率符合约定"},
        "installed_check": [],
    },
    {
        "id": "wps", "label": "WPS Office", "category": "办公套件",
        "bridge": "VBA/JS 宏接口（兼容 Office 自动化子集）",
        "docs": ["https://solution.wps.cn/"],
        "verdict_source": "artifact",
        "can_verify": ["产物 .docx/.xlsx/.pptx 存在且可解析（同 Office 判据）"],
        "cannot_verify": ["宏执行过程的细节", "界面交互", "视觉一致性"],
        "human_in_loop": True,   # 宏兼容性不稳定，执行状态多靠人确认
        "probe": {"kind": "py_call", "assert": "产物可被 python-docx/openpyxl/python-pptx 解析"},
        "installed_check": [],
    },
]


def list_adapters() -> list[dict[str, Any]]:
    """全部适配器清单（给 UI / 自检）。都是只读副本，防外部改坏常量。"""
    import copy
    return copy.deepcopy(ADAPTERS)


def get(adapter_id: str) -> dict[str, Any] | None:
    for a in ADAPTERS:
        if a["id"] == str(adapter_id or "").strip().lower():
            import copy
            return copy.deepcopy(a)
    return None


def verifiable(adapter_id: str = "") -> list[dict[str, Any]]:
    """可机械验的判据清单（human_in_loop=False 的适配器）。"""
    items = [a for a in list_adapters() if not a["human_in_loop"]]
    return [a for a in items if not adapter_id or a["id"] == adapter_id]


def human_required() -> list[dict[str, Any]]:
    """必须人在环的清单——诚实列出，不藏。"""
    return [a for a in list_adapters() if a["human_in_loop"]]


def probe_suggestion(adapter_id: str, workdir: str = "") -> dict[str, Any]:
    """给远端的探针建议：把适配器的 probe 转成可直接用的文本约定。

    不含真执行（本模块无副作用）；真探针由 probe.py 在子进程跑，避免污染主进程。
    """
    a = get(adapter_id)
    if not a:
        return {"ok": False, "why": "未知适配器"}
    lines = [
        f"【{a['label']} 插件桥验收约定】",
        f"· 桥接：{a['bridge']}",
        f"· 判据归属：{a['verdict_source']}",
        "· 可机械判据：",
    ]
    lines += [f"    - {x}" for x in a["can_verify"]]
    if a["cannot_verify"]:
        lines.append("· 机器判不了（必须人在环）：")
        lines += [f"    - {x}" for x in a["cannot_verify"]]
    if a["human_in_loop"]:
        lines.append("· ⚠ 本适配器含审美/交互判据，验产物只覆盖机械部分，最终需人确认。")
    lines.append("· 公开文档：" + " / ".join(a["docs"]))
    return {"ok": True, "adapter": a["id"], "workdir": workdir,
            "probe": a["probe"], "contract": "\n".join(lines)}


def describe() -> dict[str, Any]:
    return {
        "adapter_count": len(ADAPTERS),
        "verifiable_count": len(verifiable()),
        "human_required": [a["id"] for a in human_required()],
        "claim_full_auto": False,
        "note": "只验产物/扩展命令返回值；宿主内部与审美判据仍在人眼里",
    }
