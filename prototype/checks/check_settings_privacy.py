"""隐私自检：安装包绝不能夹带开发机的 settings.json / api_key（2026-11-20 事故）。

用户口径（2026-11-21）：远端配置回到原来的 `prototype/data/settings.json`，
但**每次打包必须换成空白模板**。

钉住：
  1. 开发/运行统一读 `prototype/data/settings.json`（原位置）；
  2. electron-builder filter 排除本机 settings，再由 extraResources 放入
     `packaging/settings.template.json` 作为 `prototype/data/settings.json`；
  3. 打包脚本每次生成模板并清空所有密钥字段（`_scrub_secrets`）；
  4. 成品硬闸：settings 里任何非空 api_key/secret/token 都让构建失败；
  5. 默认值里没有真实 key；
  6. 运行时**不清用户 key**，且覆盖安装后能从镜像恢复（2026-11-21 报障）。

跑法: python checks/check_settings_privacy.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTO = ROOT / "prototype"
sys.path.insert(0, str(PROTO))
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  {str(detail)[:220]}" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    src = (PROTO / "core" / "settings_store.py").read_text(encoding="utf-8")

    print("[1] 原位置：prototype/data/settings.json")
    ck("默认路径是 ENGINE_ROOT/data/settings.json",
       'ENGINE_ROOT / "data" / "settings.json"' in src)
    ck("配置本体不切到 data_dir",
       'or (ENGINE_ROOT / "data" / "settings.json"))' in src)
    ck("支持 SIXCODER_SETTINGS 覆盖（自检用）", 'os.environ.get("SIXCODER_SETTINGS")' in src)

    with tempfile.TemporaryDirectory() as td:
        env = dict(os.environ)
        env["SIXCODER_SETTINGS"] = str(Path(td) / "s.json")
        code = ("import sys; sys.path.insert(0, r'%s'); "
                "from core import settings_store as s; print(s.SETTINGS)") % str(PROTO)
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        got = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        want = env["SIXCODER_SETTINGS"]
        ck("环境变量覆盖生效", got == want, f"{got} != {want}\n{r.stderr[-300:]}")

    print("[2] 打包：排除本机 settings，放入空白模板")
    cfg = json.loads((ROOT / "packaging" / "electron-builder.json").read_text(encoding="utf-8"))
    flt = cfg["extraResources"][1]["filter"]
    for pat in ("!data/settings.json", "!data/toolchains.json",
                "!data/tool_lib/**", "!data/scratch/**"):
        ck("filter 有 " + pat, pat in flt)
    # 生态包**必须进安装版**：否则装好的软件认不出 frontend_spa/electron/spring_boot 等栈，
    # 等于功能缺失。早期为缩小体积排除了它，2026-11-23 已改为随包分发。
    ck("生态包不被排除（装好的软件要能用）", "!data/ecosystem/**" not in flt, flt)
    ck("仍保留 data/axe.min.js", "!data/axe.min.js" not in flt)
    extras = cfg["extraResources"]
    ck("extraResources 放的是模板文件（不覆盖用户 settings.json）", any(
        str(x.get("from", "")).endswith("settings.template.json")
        and x.get("to") == "prototype/data/settings.template.json" for x in extras), extras)

    build = (ROOT / "packaging" / "build_installer.py").read_text(encoding="utf-8")
    ck("打包脚本每次生成模板", "write_settings_template()" in build)
    ck("模板会清空密钥字段", "_scrub_secrets" in build and "SECRET_KEYS" in build)
    ck("成品硬闸拒绝非空 key", "仍有非空密钥字段" in build)
    ck("安装版首次启动从模板拷配置", "SETTINGS_TEMPLATE" in src and "copyfile" in src)
    ck("运行时不再清用户 key（旧标记逻辑已删）",
       "_KEY_CLEAR_MARKER" not in src and "_scrub_keys" not in src)
    ck("打包态有配置镜像", "settings.mirror.json" in src and "MIRROR" in src)
    ck("保存时刷新镜像", "_mirror_write()" in src.split("def save(")[-1][:400])

    tpl = ROOT / "packaging" / "settings.template.json"
    if tpl.is_file():
        obj = json.loads(tpl.read_text(encoding="utf-8"))
        leaked = []
        def walk(x):
            if isinstance(x, dict):
                for k, v in x.items():
                    if str(k).lower() in {"api_key", "apikey", "secret", "token", "password"} \
                            and isinstance(v, str) and v.strip():
                        leaked.append(k)
                    else:
                        walk(v)
            elif isinstance(x, list):
                for item in x:
                    walk(item)
        walk(obj)
        ck("模板里没有非空 key", not leaked, leaked)
    else:
        print("  SKIP 模板尚未生成（打包时生成）")

    print("[3] 安装版启动：不清 key、覆盖安装能恢复")
    from core import settings_store as ss
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        old = (ss.SETTINGS, ss.SETTINGS_TEMPLATE, ss.MIRROR,
               ss._paths.is_packaged, ss._is_installed_tree)
        try:
            ss.SETTINGS = td / "install" / "data" / "settings.json"
            ss.SETTINGS_TEMPLATE = td / "install" / "data" / "settings.template.json"
            ss.MIRROR = td / "home" / "data" / "settings.mirror.json"
            ss._paths.is_packaged = lambda: True
            ss._is_installed_tree = lambda: True
            ss.SETTINGS_TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
            ss.SETTINGS_TEMPLATE.write_text(json.dumps({"remote": {"api_key": ""}}),
                                            encoding="utf-8")

            ss._bootstrap_packaged_settings()          # 首次安装启动
            ck("首次启动生成配置", ss.SETTINGS.exists())

            ss.save({"remote": {"api_key": "sk-user-real-key-1234567890",
                                "provider": "deepseek"}})   # 用户填 key
            ck("保存后镜像已写入", ss.MIRROR.exists())

            ss._bootstrap_packaged_settings()          # 重启软件（自启动）
            got = json.loads(ss.SETTINGS.read_text(encoding="utf-8"))
            ck("重启一次 key 还在（旧实现会清掉）",
               got["remote"]["api_key"] == "sk-user-real-key-1234567890", got)
            ck("结构没被改写", got["remote"].get("provider") == "deepseek", got)

            # 模拟覆盖安装：NSIS 把安装目录换掉，settings.json 消失
            ss.SETTINGS.unlink()
            ss._bootstrap_packaged_settings()
            ck("覆盖安装后从镜像恢复 key",
               json.loads(ss.SETTINGS.read_text(encoding="utf-8"))["remote"]["api_key"]
               == "sk-user-real-key-1234567890")
            ck("镜像本身在数据目录、不在安装目录",
               "home" in str(ss.MIRROR) and "install" not in str(ss.MIRROR), ss.MIRROR)

            # 用户主动清空 key 后，镜像也要跟着空 —— 不能把清掉的 key 又恢复回来
            ss.save({"remote": {"api_key": ""}})
            ss.SETTINGS.unlink()
            ss._bootstrap_packaged_settings()
            ck("主动清空后不会被镜像复活",
               json.loads(ss.SETTINGS.read_text(encoding="utf-8"))["remote"]["api_key"] == "")

            # 开发态不写镜像
            ss._paths.is_packaged = lambda: False
            ss.MIRROR = None
            ss.SETTINGS.unlink()
            ss._bootstrap_packaged_settings()
            ck("开发态不生成配置/镜像", not ss.SETTINGS.exists())
        finally:
            (ss.SETTINGS, ss.SETTINGS_TEMPLATE, ss.MIRROR,
             ss._paths.is_packaged, ss._is_installed_tree) = old

    print("[4] 默认值不含真实 key")
    defaults = src[src.index("DEFAULTS = {"):src.index("def load()")]
    ck("DEFAULTS 里 api_key 都是空串",
       defaults.count('"api_key": ""') >= 2, defaults.count('"api_key": ""'))
    ck("没有 sk- 开头的硬编码 key", "sk-" not in defaults)

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
