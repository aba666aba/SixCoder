"""安装包瘦身自检：**哪些包该剔除、哪些绝不能剔**（2026-11-23 真问题的回归）。

## 这条自检保护的是什么

实测 1.1.0 装包 `win-unpacked` **687 MB**，其中 `site-packages` 227 MB，里面混着：
    · `pip/`           10.9 MB —— 运行时不需要
    · `mypyc/`          5.8 MB —— **mypy 本体都没打进来，只残留了它的子包**
    · `huggingface_hub/` + `hf_xet/` —— tokenizers 的传递依赖，自家代码零引用
合计约 47 MB 纯无效载荷（连带的 tqdm/filelock/fsspec 一并剔掉）。

根因：`ensure_python()` 在便携 Python 已存在时直接 return，于是 `site-packages`
是**跨构建累积**的——上一次构建装过的包会一直留着，没有人清。

同时 `locales/` 有 **55 个语言包 46.6 MB**，而产品只有中文界面。

## 本自检钉住四件事

1. **剔除名单必须存在且生效**：`build_installer.PRUNE_PACKAGES` 覆盖上述包；
2. **匹配逻辑正确**：`pip` / `mypyc` / `huggingface_hub` / `hf_xet` 命中，
   `tokenizers` / `playwright` / `pytest` / `pydantic_core` **不**命中（这是关键，
   误剔任何一个都会让"判据变松"或"UI 验收消失"，且很难被发现）；
3. **`.dist-info` 一起剔**：只删包目录不删元数据，pip 视角里"包还在"，脏状态会传染；
4. **locales 已裁剪**：`electron-builder.json` 声明 `electronLanguages`，不再全量带 55 种。

跑法: python check_installer_slim.py
"""
from __future__ import annotations

import json
import runpy
import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]          # prototype/
ROOT = PROTO.parent
sys.path.insert(0, str(PROTO))

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


def main() -> int:
    print("安装包瘦身自检")
    bi = ROOT / "packaging" / "build_installer.py"
    check("打包脚本存在", bi.is_file(), str(bi))
    if not bi.is_file():
        print(f"\n有失败项：{', '.join(FAILS)}")
        return 1
    # 只取定义，不执行构建（run_name 不是 __main__）
    ns = runpy.run_path(str(bi), run_name="__not_main__")

    print("[1] 剔除名单覆盖已知无效载荷")
    prune = tuple(ns.get("PRUNE_PACKAGES") or ())
    check("PRUNE_PACKAGES 已定义且非空", bool(prune), str(prune))
    for pkg in ("mypy", "mypyc", "huggingface_hub", "hf_xet"):
        check(f"名单含 {pkg}", pkg in prune, str(prune))
    # pip 是**构建期工具**，必须在装完依赖、打完包之后单独删，
    # 放进 PRUNE_PACKAGES 会让下一次构建 `No module named pip` 直接挂（2026-11-23 踩过）
    check("pip 不在 PRUNE_PACKAGES（构建期要靠它）", "pip" not in prune, str(prune))
    check("有 remove_pip_after_build()", callable(ns.get("remove_pip_after_build")))

    print("[2] 匹配逻辑：该剔的剔、该留的留")
    base = ns.get("_pkg_base")
    check("_pkg_base 已定义", callable(base))
    if callable(base):
        want = {p.lower().replace("-", "_") for p in prune}

        def hit(name: str) -> bool:
            return base(name).lower().replace("-", "_") in want

        for n in ("mypyc", "mypy-2.4.1.dist-info",
                  "huggingface_hub", "hf_xet", "hf_xet-1.5.0.dist-info"):
            check(f"{n} 应被剔除", hit(n))
        # **这几个绝不能剔**：剔了会让判据变松/UI 验收消失/基线建不起来
        for n in ("tokenizers", "playwright", "pytest", "fastapi", "pydantic",
                  "pydantic_core", "libcst", "coverage", "psutil",
                  "pyinstaller", "PyInstaller-6.20.0.dist-info"):
            check(f"{n} 绝不能剔", not hit(n))
        # dist-info 必须剥掉版本号才能匹配（否则元数据残留，pip 视角脏）
        check("dist-info 去掉版本段", base("mypy-2.4.1.dist-info") == "mypy",
              base("mypy-2.4.1.dist-info"))
        check("带连字符的包名不被截断",
              base("typing-extensions-4.0.0.dist-info") == "typing-extensions",
              base("typing-extensions-4.0.0.dist-info"))

    print("[3] 构建流程真的会调用（不是定义了不用）")
    src = bi.read_text(encoding="utf-8", errors="replace")
    tail = src.split("def main(")[-1]
    check("main() 里调用 prune_packages()", "prune_packages()" in tail)
    check("main() 里调用 remove_pip_after_build()", "remove_pip_after_build()" in tail)
    # pip 必须在 build_installer() **之前**删：electron-builder 是复制
    # `shell/resources/python`，放打包之后删，源目录干净、装包里那份副本仍带 pip
    i_build = tail.find("exe = build_installer()")
    if i_build < 0:
        i_build = tail.find("build_installer()")
    i_rmpip = tail.find("remove_pip_after_build()")
    check("remove_pip 在 build_installer() 之前（否则装包里仍有 pip）",
          0 <= i_rmpip < i_build, f"rmpip@{i_rmpip} build@{i_build}")
    check("剔完复验核心 import", "bundled deps still OK" in src)
    check("剔完复验官方 tokenizer", "exact_tokenizer" in src)
    check("ensure_pip 用 -m pip 真探测（不只看 pip.exe）",
          '"-m", "pip", "--version"' in src)

    print("[4] locales 已按语言裁剪")
    eb = ROOT / "packaging" / "electron-builder.json"
    check("electron-builder.json 存在", eb.is_file(), str(eb))
    if eb.is_file():
        cfg = json.loads(eb.read_text(encoding="utf-8"))
        langs = cfg.get("electronLanguages")
        check("声明了 electronLanguages", isinstance(langs, list) and bool(langs), str(langs))
        if isinstance(langs, list):
            check("只留中文与英文", set(langs) <= {"zh-CN", "en-US"}, str(langs))
            check("不超过 3 种（防回退成全量）", len(langs) <= 3, str(langs))

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（剔除名单 / 匹配不误剔 / 真正生效 / 复验 / locales 裁剪）")
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
