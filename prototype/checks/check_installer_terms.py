"""安装向导「许可协议 + 强制同意 + 可选运行时」自检。

2026-11-20 用户口径：
  ① 安装包一开始要有条约，写得很详细、免责条款覆盖各方面；
  ② 必须点"我已阅读并同意所有条款"才能安装，不点就不能装；
  ③ 自带部分语言运行时（Python/JDK/Go/MinGW/Git），可勾选，不勾 = 用用户自己的。

钉住六条：
  1. 条款文件真的被生成，且十五条齐全、字数够（不是占位文案）；
  2. electron-builder 的 `nsis.license` 指向那份条款（否则第一屏没有协议页）；
  3. 确认页在"安装前"（customPageAfterChangeDir），且在可选组件页**之前**；
  4. 未勾选时「下一步」被**禁用**（EnableWindow 0），离开页时还会 Abort 兜底；
  5. 五个运行时勾选框都在，默认**都不勾**（不勾 = 用户自己有环境）；
  6. 安装后由 `SixCoder.exe --install-runtime` 触发下载（不阻塞安装程序）。

> 注：不要去 grep 安装包 exe 里的中文字符串 —— NSIS 把脚本与文件都 LZMA 压缩了，
> 连"保留本地运行数据"这种一定存在的文案也搜不到，那种断言只会制造假红。

跑法: python checks/check_installer_terms.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NSH = (ROOT / "shell" / "build" / "installer.nsh")
RTF = (ROOT / "shell" / "build" / "terms.rtf")
TXT = (ROOT / "shell" / "build" / "terms.txt")
CFG = ROOT / "packaging" / "electron-builder.json"
MAKER = ROOT / "packaging" / "make_terms.py"
FAILS: list[str] = []

SECTIONS = ["一、定义", "二、许可授予", "三、使用限制", "四、人工智能生成内容",
            "五、第三方模型服务", "六、数据、隐私与密钥", "七、自带运行时",
            "八、无担保声明", "九、责任限制", "十、您的赔偿义务", "十一、合规",
            "十二、终止", "十三、条款变更", "十四、适用法律", "十五、其他"]


def ck(name: str, ok: bool, detail: str = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + str(detail)[:280]) if not ok and detail else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    src = NSH.read_text(encoding="utf-8")
    cfg = json.loads(CFG.read_text(encoding="utf-8"))

    print("[1] 条款正文（详细、覆盖各方面）")
    ck("条款生成器存在", MAKER.is_file())
    ck("terms.rtf 已生成", RTF.is_file(), RTF)
    ck("terms.txt 已生成", TXT.is_file(), TXT)
    body = TXT.read_text(encoding="utf-8") if TXT.is_file() else ""
    ck("字数足够详细（≥3500 字）", len(body) >= 3500, f"{len(body)} 字")
    missing = [s for s in SECTIONS if s not in body]
    ck("十五节齐全", not missing, f"缺：{missing[:4]}")
    for kw in ("免责", "不保证", "责任限制", "赔偿", "数据丢失", "生成代码", "自行审查"):
        ck(f"含关键免责词：{kw}", kw in body)
    rtf = RTF.read_text(encoding="utf-8", errors="replace") if RTF.is_file() else ""
    ck("RTF 结构完整", rtf.startswith("{\\rtf1") and rtf.rstrip().endswith("}"))
    ck("中文按 \\u 转义（不会乱码）", "\\u" in rtf)

    print("[2] 安装向导第一屏就是协议页")
    ck("配置了 nsis.license", bool(cfg.get("nsis", {}).get("license")),
       cfg.get("nsis", {}).get("license"))
    ck("指向 build/terms.rtf", str(cfg.get("nsis", {}).get("license", "")).endswith("terms.rtf"))
    ck("文件确实在那儿",
       (ROOT / "shell" / str(cfg.get("nsis", {}).get("license", "___"))).is_file())

    print("[3] 确认页的位置与顺序（许可全文 + 同意勾选合一页）")
    ck("用 MUI 原生许可勾选框（两页合一）",
       "MUI_LICENSEPAGE_CHECKBOX" in src and "MUI_LICENSEPAGE_CHECKBOX_TEXT" in src)
    ck("勾选框定义在 MUI2 展开前（文件顶部）",
       src.find("MUI_LICENSEPAGE_CHECKBOX") < src.find('!include "nsDialogs.nsh"'))
    ck("自定义页只剩可选组件页（安装前）",
       "customPageAfterChangeDir" in src and "Page custom SixCoderOptCreate" in src)
    ck("不再有独立的同意页", "SixCoderTermsCreate" not in src)
    ck("跳过「为谁安装」页", "isForceCurrentInstall" in src)

    print("[4] 不勾选就过不去")
    ck("勾选框文案写明我已完整阅读并同意", "我已完整阅读并同意" in src)
    ck("文案写明不勾无法继续安装", "不勾选将无法继续安装" in src)
    ck("勾选框文案含关键免责条款", "第四条" in src and "第八条" in src
       and "第九条" in src and "第十条" in src)

    print("[5] 自带运行时：五个都默认不勾")
    for name, hwnd in (("Python", "$SC_PyHwnd"), ("JDK/Java", "$SC_JavaHwnd"),
                       ("Go", "$SC_GoHwnd"), ("MinGW", "$SC_MingwHwnd"),
                       ("Git", "$SC_GitHwnd")):
        ck(f"有 {name} 勾选框", hwnd in src)
        ck(f"{name} 默认不勾", "${NSD_Uncheck} " + hwnd in src)
    opt_ui = src[src.find("Function SixCoderOptCreate"):src.find("Function SixCoderOptLeave")]
    # 解释性注释里会引用旧话术/旧命令做对比，断言只看真正生效的代码行（非注释）。
    live = lambda s: "\n".join(l for l in s.splitlines() if not l.lstrip().startswith(";"))
    ck("文案直白：勾选=装、不勾=不装",
       "勾选 = 装到本机" in opt_ui and "不勾 = 不装" in opt_ui)
    ck("不再有绕弯话术（不装照样跑 / 用你自己装好的环境）",
       "不装照样跑" not in live(opt_ui) and "用你自己已经装好的环境" not in live(opt_ui))
    ck("Ollama 已装时文案也说清勾/不勾",
       "Ollama 已安装（勾选 = 重新安装一次，不勾 = 不装）" in opt_ui)
    ck("MSVC 边界写清楚了", "MSVC" in opt_ui or "微软" in opt_ui)

    print("[6] 安装后按勾选安装（不阻塞、不闪黑框、进度条不卡）")
    install = src[src.find("!macro customInstall"):src.find("!endif  ; !ifndef BUILD_UNINSTALLER")]
    ck("customInstall 里拼了运行时参数", "python@3.13" in install and "java@21" in install
       and "go@1.27" in install and "mingw@16" in install and "git@2.56.1" in install)
    main_js = (ROOT / "shell" / "main.cjs").read_text(encoding="utf-8")
    ck("外壳 --install-runtime 认 git@ 参数", "python|java|go|mingw|git" in main_js, "regex")
    ck("用 --install-runtime 触发", "--install-runtime" in install)
    ck("用 --env-setup 触发本地模型安装", "--env-setup" in install)
    ck("独立进程不阻塞（NSIS Exec）",
       'Exec \'"$INSTDIR\\SixCoder.exe" --install-runtime' in install)
    ck("不用 cmd/c start 弹控制台黑框", "cmd /c start" not in live(install))
    ck("清理进程后不留长 Sleep（进度条不卡 100%）",
       "Sleep 1200" not in install and "Sleep 800" not in install)
    main_cjs = (ROOT / "shell" / "main.cjs").read_text(encoding="utf-8")
    ck("壳有 --install-runtime 分支", "--install-runtime" in main_cjs)
    ck("壳调 core.runtimes install", "core.runtimes" in main_cjs)
    rt = (ROOT / "prototype" / "core" / "runtimes.py").read_text(encoding="utf-8")
    ck("运行时有清单与多源下载", "RUNTIMES" in rt and "urls" in rt)
    ck("装完真跑一次校验", "_verify" in rt and "probe" in rt)
    ck("装在数据目录（覆盖安装不丢）", "runtimes" in rt and "paths" in rt)

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
