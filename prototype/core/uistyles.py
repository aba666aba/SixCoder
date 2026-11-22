"""UI 审美样式库：把"好不好看"从引擎的不可判据，转成**用户显式选择**的可执行约定。

为什么需要（承接通用差距讨论）：
  · 审美是判据在人眼里的那类，引擎自己判不了——硬判就是谎报；
  · 工程上能做的是把审美**降维成有限个具名类型**（页面/背景/菜单/按钮各 10 种），
    让用户挑一个预设，库给出这段约定的可执行描述，注入生成链路；
  · **颜色一律用户自填**：库里不写死任何色值，只给语义插槽（primary/bg/surface/
    text/accent/border）。用户没填时生成规则里保留占位符，绝不代替用户拍板配色。

职责边界（不越界）：
  · 本模块只做"列举 + 选择 + 生成约定文本"，**不做审美评判、不自动挑样式**；
  · 选出来的约定经 `contract_text()` 交给提示词/探针链路，由既有 browser 探针验收
    DOM/CSS 是否落地（判据仍是机器可判的 DOM/CSS 变量，不是"好不好看"）。

存储：用户选择放 settings 的 `uistyle` 段（`{selected, colors}`）；预设库只读 catalog.json。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import settings_store as _settings

_ROOT = Path(__file__).resolve().parent.parent
_CATALOG = _ROOT / "data" / "uistyles" / "catalog.json"

# 颜色插槽值校验：接受 #RGB / #RRGGBB / rgb()/rgba() / hsl() / CSS 具名色。
# 严禁注入：不接受分号、花括号、url(、javascript:、反斜杠等（防 CSS/HTML 注入）。
_COLOR_RE = re.compile(
    r"^(?:#[0-9a-fA-F]{3,8}|rgb\(\s*[\d\s.,%]+\)|rgba\(\s*[\d\s.,%]+\)|"
    r"hsl\(\s*[\d\s.,%]+\)|hsla\(\s*[\d\s.,%]+\)|[a-zA-Z]{3,20})$"
)
_BAD_COLOR = re.compile(r"[;{}<>\\]|url\s*\(|javascript:|expression\s*\(", re.I)
_DIMENSIONS = ("page", "background", "menu", "button")


def catalog_path() -> Path:
    return _CATALOG


def load_catalog() -> dict[str, Any]:
    """读预设库；文件缺失/损坏时返回空结构（fail-closed，不给假预设）。"""
    if not _CATALOG.is_file():
        return {"version": "0", "dimensions": list(_DIMENSIONS),
                "color_slots": [], "types": {}, "presets": []}
    try:
        data = json.loads(_CATALOG.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:                                  # noqa: BLE001
        return {}


def list_presets() -> list[dict[str, Any]]:
    """全部预设（给 UI 列举）。颜色不含任何写死色值。"""
    return [p for p in (load_catalog().get("presets") or []) if isinstance(p, dict)]


def list_types(dimension: str = "") -> dict[str, list[dict]]:
    """类型清单（给 UI 展示"总共有哪些类型"）。dimension 空=全部。"""
    types = load_catalog().get("types") or {}
    if dimension:
        return {dimension: [t for t in (types.get(dimension) or []) if isinstance(t, dict)]}
    return {d: [t for t in (types.get(d) or []) if isinstance(t, dict)]
            for d in _DIMENSIONS if d in types}


def color_slots() -> list[dict[str, str]]:
    return [s for s in (load_catalog().get("color_slots") or []) if isinstance(s, dict)]


def get_preset(pid: str) -> dict[str, Any] | None:
    pid = str(pid or "").strip()
    for p in list_presets():
        if p.get("id") == pid:
            return p
    return None


def _valid_color(value: str) -> bool:
    v = str(value or "").strip()
    return bool(v) and bool(_COLOR_RE.match(v)) and not _BAD_COLOR.search(v)


def validate_selection(selected: str, colors: dict | None) -> tuple[bool, str]:
    """校验一次选择是否合法：预设存在、颜色值都是安全色值、无未知插槽。"""
    if not get_preset(selected):
        return False, "未选择有效预设"
    allowed = {s.get("key") for s in color_slots()}
    for k, v in (colors or {}).items():
        if k not in allowed:
            return False, f"未知颜色插槽：{k}"
        if not _valid_color(v):
            return False, f"颜色值不合法或含注入字符：{k}"
    return True, "ok"


def selection() -> dict[str, Any]:
    """当前用户选择（含 colors）。未选时返回 selected=''、colors={}。"""
    data = _settings.load()
    raw = data.get("uistyle") or {}
    if not isinstance(raw, dict):
        raw = {}
    colors = {k: str(v) for k, v in (raw.get("colors") or {}).items()
              if isinstance(k, str) and _valid_color(v)}
    return {"selected": str(raw.get("selected") or ""), "colors": colors}


def select(pid: str, colors: dict | None) -> dict[str, Any]:
    """保存用户选择。非法输入 fail-closed：抛 ValueError，不写坏配置。

    颜色为空是允许的（用户可先选样式再慢慢填色）——空插槽在 contract 里保留占位符。
    """
    ok, why = validate_selection(pid, colors or {})
    if not ok:
        raise ValueError(why)
    clean_colors = {str(k): str(v).strip() for k, v in (colors or {}).items()
                    if str(v or "").strip()}
    data = _settings.load()
    data["uistyle"] = {"selected": str(pid).strip(), "colors": clean_colors}
    _settings.save(data)
    return {"selected": pid, "colors": clean_colors}


def public_config() -> dict[str, Any]:
    """给 UI 的完整视图：预设清单 + 类型清单 + 插槽 + 当前选择。"""
    return {
        "presets": list_presets(),
        "types": list_types(),
        "dimensions": list(_DIMENSIONS),
        "color_slots": color_slots(),
        "selected": selection(),
    }


def _type_label(dimension: str, tid: str) -> str:
    for t in (load_catalog().get("types") or {}).get(dimension, []) or []:
        if isinstance(t, dict) and t.get("id") == tid:
            return f"{t.get('label') or tid}（{tid}）"
    return tid


def contract_text(pid: str = "", colors: dict | None = None) -> str:
    """把选择转成给远端/生成链路的**可执行风格约定**。

    未传 pid 时用当前选择；没选就返回空串（不擅自替用户定风格）。
    颜色未填的插槽输出 `待用户填` 占位符——绝不代替用户拍板。
    """
    sel = selection()
    pid = str(pid or sel.get("selected") or "").strip()
    preset = get_preset(pid)
    if not preset:
        return ""
    palette = dict(sel.get("colors") or {})
    if colors is not None:
        palette.update(colors)
    dims = preset.get("dimensions") or {}
    lines = [
        f"【用户选定 UI 风格：{preset.get('label') or pid}】",
        f"· 说明：{preset.get('description') or ''}",
    ]
    for d in _DIMENSIONS:
        tid = str(dims.get(d) or "")
        if tid:
            lines.append(f"· {d}：{_type_label(d, tid)}")
    lines.append("· 配色（用户自填；未填=待用户填，禁止引擎自行指定色值）：")
    for s in color_slots():
        key = str(s.get("key") or "")
        val = str(palette.get(key) or "").strip()
        lines.append(f"    - {key}({s.get('label') or key}): {val or '待用户填'}")
    lines.append("· 验收：用 browser 探针断言上述结构对应的 DOM/CSS 变量落地；"
                 "审美本身机器不判，勿声称'好看'。")
    return "\n".join(lines)


def css_vars(colors: dict | None = None) -> dict[str, str]:
    """把用户填的配色转成 CSS 自定义属性（`--ui-<key>`）。未填/非法的插槽不返回。

    生成链路与验收链路都用这一份映射，避免"验证的和生成的不是同一套变量名"。
    """
    sel = selection()
    palette = dict(sel.get("colors") or {})
    if colors is not None:
        palette.update({k: v for k, v in colors.items() if str(v or "").strip()})
    out: dict[str, str] = {}
    for s in color_slots():
        key = str(s.get("key") or "")
        val = str(palette.get(key) or "").strip()
        if key and val and _valid_color(val):
            out[f"--ui-{key}"] = val
    return out


def demo_html(colors: dict | None = None, preset_id: str = "") -> str:
    """生成一个最小 HTML：把选定风格的 CSS 变量与四维结构真落到 DOM。

    用途是**验收通道**，不是产品页：它证明"变量能写进 DOM 且浏览器读得到"，
    不证明"好看"（审美判据仍在人眼里）。无任何用户色值时给一个兜底色，
    保证通道本身仍可验（否则空值会让验收永远红，变成假失败）。
    """
    sel = selection()
    pid = str(preset_id or sel.get("selected") or "").strip()
    preset = get_preset(pid) or {}
    vars_ = css_vars(colors)
    if not vars_:
        vars_ = {"--ui-primary": "#3b82f6"}
    root = ";".join(f"{k}: {v}" for k, v in vars_.items())
    dims = preset.get("dimensions") or {}
    blocks = "".join(
        f'<section class="ui-{d}" data-kind="{dims.get(d) or "none"}">'
        f'<span>{d}</span></section>'
        for d in _DIMENSIONS)
    _ = preset.get("label") or pid or "custom"     # 占位，保持结构可读
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>:root{{{root}}}"
        ".ui-probe{color:var(--ui-text,#111);background:var(--ui-bg,#fff);"
        "padding:8px;font-family:sans-serif}</style></head><body>"
        f'<main class="ui-probe" id="ui-root">{blocks}</main></body></html>'
    )


def describe() -> dict[str, Any]:
    """能力声明（给自检/文档，诚实标注边界）。"""
    return {
        "preset_count": len(list_presets()),
        "dimensions": list(_DIMENSIONS),
        "type_count": sum(len(v) for v in list_types().values()),
        "color_slot_count": len(color_slots()),
        "colors_are_user_filled": True,          # 库里不写死任何色值，全靠用户填插槽
        "aesthetic_judgement_by_engine": False,
        "selection_stored_in": "settings.uistyle",
    }
