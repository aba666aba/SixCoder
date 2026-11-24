"""免责条款版本号同步测试（2026-11-24）。

## 背景

用户指出："是免责条款有版本号"。

实测发现：packaging/make_terms.py 里硬编码 `版本 1.1.0`，
而当时软件版本已经是 1.2.1 —— 安装向导第一屏显示的免责声明写着过时版本。
每次发版还要人记得手改，迟早忘。

## 修法

条款正文改成占位符 {VERSION} / {DATE}，
由 terms_text() 从 shell/package.json 读版本、取构建当天日期填入。
唯一版本来源 = package.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

import make_terms   # noqa: E402


def _pkg_version() -> str:
    return str(json.loads((ROOT / "shell" / "package.json").read_text(encoding="utf-8"))["version"])


def test_version_comes_from_package_json():
    """条款版本号必须等于 package.json 的 version。"""
    got = make_terms._app_version()
    assert got == _pkg_version(), "条款版本 %r != package.json %r" % (got, _pkg_version())


def test_no_hardcoded_version_in_terms():
    """条款模板里不能再有写死的 x.y.z。"""
    import re
    tpl = make_terms.TERMS
    # 模板里允许有 "1.1"、"1.2" 这类条款编号（一、定义 下的 1.1/1.2），
    # 但不允许出现三段的 x.y.z 版本号
    bad = re.findall(r"版本\s*\d+\.\d+\.\d+", tpl)
    assert not bad, "条款里还有写死的版本号：%s" % bad


def test_render_substitutes_placeholders():
    """渲染后不能残留占位符。"""
    body = make_terms.terms_text()
    assert "{VERSION}" not in body
    assert "{DATE}" not in body
    assert _pkg_version() in body, "渲染后应包含当前版本号"


def test_render_contains_date():
    """渲染后日期要填上（构建当天）。"""
    import time
    body = make_terms.terms_text()
    assert time.strftime("%Y") in body
    assert "年" in body and "月" in body and "日" in body


def test_unknown_version_falls_back():
    """读不到 package.json 时不能崩，退回 unknown。"""
    orig = make_terms._SHELL_PKG
    try:
        make_terms._SHELL_PKG = Path("/nonexistent/package.json")
        assert make_terms._app_version() == "unknown"
    finally:
        make_terms._SHELL_PKG = orig
