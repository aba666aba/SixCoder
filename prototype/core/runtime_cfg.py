"""运行时配置：引擎本体**不认识任何具体项目**（2026-11-19 用户口径）。

引擎只提供能力（检测/生成/协商/判据修复），项目身份必须由**外部**提供：
    · 命令行 `--repo`（优先级最高）
    · 环境变量 `SIXCODER_REPO`
    · 引擎配置 `_logs/engine.json` 的 `{"repo": "<绝对路径>"}`（本机长期默认）
三级都没有 → 明确报错，绝不猜路径、绝不写死某个项目的名字。

为什么必须这样做：写死路径的引擎等于把"能跑第二个项目"这件事废掉；
而且自检会变成"必须先有那个仓才能跑"，换机器就红一片。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]      # prototype/
CFG_FILE = ENGINE_ROOT.parent / "_logs" / "engine.json"


def _from_file() -> str:
    try:
        raw = json.loads(CFG_FILE.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return ""
    return str(raw.get("repo") or "").strip()


def repo_root(explicit: str = "") -> Path | None:
    """当前项目根目录（None = 没配置）。优先级：显式参数 > env > 配置文件。"""
    for cand in (explicit, os.environ.get("SIXCODER_REPO", ""), _from_file()):
        s = str(cand or "").strip()
        if not s:
            continue
        p = Path(s)
        if p.is_dir():
            return p
    return None


def require_repo(explicit: str = "") -> Path:
    """拿不到项目路径就**明确报错**（不猜、不兜底到某个写死的仓）。"""
    p = repo_root(explicit)
    if p is None:
        raise SystemExit(
            "没有可用的项目路径。请任选一种方式指定：\n"
            "  1) 命令行加 --repo <项目根目录>\n"
            "  2) 设置环境变量 SIXCODER_REPO=<项目根目录>\n"
            "  3) 在 %s 写 {\"repo\": \"<项目根目录>\"}\n" % CFG_FILE)
    return p


def save_repo(ws) -> Path:
    """把项目路径记进引擎配置（供"--repo 一次、以后免填"用）。"""
    data = {}
    try:
        data = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:                                    # noqa: BLE001
        data = {}
    data["repo"] = str(Path(ws).resolve())
    CFG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return CFG_FILE
