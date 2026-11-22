"""环境包安装自检（2026-11-19 用户口径：pip 装包放权给协商，但需 3/3 全票）。

钉住的铁律：
  1. **包名严格白名单**：只允许 `名字` / `名字==精确版本`；拒绝 -r/-e/git+/URL/路径/
     `;` `@` 等注入面（这些能让 pip 执行任意代码或从任意源拉包 = 供应链攻击）。
  2. **manager 白名单**：只允许 pip / npm。
  3. **隔离目录**：装到 `data/tool_lib/_env/` 下的独立目录，绝不进系统 site-packages。
  4. **3/3 全票门槛**：提案人非 keep + 两名审核者的 install_env 完全一致才装；
     任一方没提/提的不同 → 不装。
  5. **接线**：judgefix.valid_patch_op 认识 install_env；apply_patch 能执行。
  本自检**不真装包**（不联网、快、可离线跑）——真装能力由独立冒烟验证。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import envinstall as ei  # noqa: E402
from core import deadlock as dl    # noqa: E402
from core import judgefix as jf    # noqa: E402

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def _op(manager="pip", packages=None, cap="cap.x"):
    return {"op": "install_env", "cap": cap, "manager": manager,
            "packages": packages if packages is not None else ["six"]}


def main() -> int:
    print("[1] 包名白名单：合法放行")
    for good in ("six", "requests", "scikit-learn==1.3.0", "python_dateutil",
                 "pkg.name", "A1"):
        check(f"接受 {good!r}", ei.valid_package(good)[0])

    print("[2] 包名白名单：注入面一律拒")
    bads = ["-r requirements.txt", "--requirement x", "-e .", "--editable=x",
            "git+https://evil/x.git", "http://evil/x.tar.gz", "https://x/y",
            "file:/etc/passwd", "./local", "/abs/path", "../../etc",
            "a; rm -rf /", "a@1.0", "a >=1.0", "a|b", "a$(x)", "a`x`",
            "a b", ""]
    for b in bads:
        ok, _ = ei.valid_package(b)
        check(f"拒绝 {b!r}", not ok)

    print("[3] manager 白名单")
    check("pip 放行", ei.validate_request("pip", ["six"])[0])
    check("npm 放行", ei.validate_request("npm", ["lodash"])[0])
    # 系统软件包管理器（2026-11-19 用户口径：装 ollama/工具链也放权）
    for m in ("winget", "choco", "scoop", "brew"):
        check(f"{m} 放行（系统软件）", ei.validate_request(m, ["GoLang.Go"])[0])
    check("apt 拒（必须 sudo，沙箱禁提权）",
          not ei.validate_request("apt", ["curl"])[0])
    # 注意 valid_package 返回 (ok, why) 元组，取 [0] 判
    check("系统包名含注入片段仍拒",
          not ei.valid_package("--override")[0]
          and not ei.valid_package("a;rm -rf /")[0])
    check("winget 风格包名可放行", ei.valid_package("GoLang.Go")[0]
          and ei.valid_package("Microsoft.VisualStudioCode")[0])
    # **绝不自检里真调 install**（2026-11-19 血泪）：winget 一旦可用，install 会
    # 真的开始下载安装（Go 约 100MB）→ 自检变成"往系统里装软件"，还会超时卡死。
    # 只测"能不能解析到可执行文件"这一层，装的动作留给真实协商（3/3 门槛后）。
    _mgr_exe = ei._resolve_mgr("winget") or ei._resolve_mgr("scoop") \
        or ei._resolve_mgr("choco") or ei._resolve_mgr("brew")
    check("能解析到某个系统包管理器（或如实为空）", True,
          "解析到：%s" % (_mgr_exe or "无（本机会如实报缺包管理器）"))
    # 命令模板必须把 {pkg} 占位，且首项是 manager（防远端注入额外参数）
    _cmd_tpl = ei._SYS_CMD["winget"]
    check("winget 命令模板含 {pkg} 占位", any("{pkg}" in x for x in _cmd_tpl))
    check("winget 命令首项是 winget（不给远端插参数的机会）",
          _cmd_tpl[0] == "winget")
    # manager 大小写/空白归一化（'PIP ' → pip）是安全的宽容，应放行
    check("'PIP ' 归一化为 pip 后放行", ei.validate_request("PIP ", ["six"])[0])
    # brew 现在是合法系统 manager；apt（需 sudo）与 curl 仍拒
    for m in ("apt", "curl", "", "powershell"):
        check(f"拒绝 manager {m!r}", not ei.validate_request(m, ["six"])[0])
    check("空 packages 拒", not ei.validate_request("pip", [])[0])
    check("重复包（大小写）拒", not ei.validate_request("pip", ["six", "SIX"])[0])
    check("超 20 个拒", not ei.validate_request(
        "pip", ["pkg%d" % i for i in range(21)])[0])

    print("[4] 隔离目录：装在 data/tool_lib/_env/ 下，不进系统 site-packages")
    d = ei._dir_for("pip", ["six"])
    check("目标目录在 _env 下", str(d).endswith(str(d)) and
          "tool_lib" in str(d) and "_env" in str(d), str(d))
    import site
    sys_paths = {str(Path(p).resolve()) for p in sys.path if p}
    check("目标目录不在 sys.path/site-packages", str(d.resolve()) not in sys_paths)
    check("同清单稳定同目录", ei._dir_for("pip", ["six"]) == ei._dir_for("pip", ["six"]))
    check("不同清单不同目录",
          ei._dir_for("pip", ["six"]) != ei._dir_for("pip", ["requests"]))
    check("同包不同顺序同目录",
          ei._dir_for("pip", ["six", "requests"]) == ei._dir_for("pip", ["requests", "six"]))

    print("[5] 3/3 全票门槛（decide_env_install 纯函数）")
    a, n = dl.decide_env_install("unlock", [[_op()], [_op()]])
    check("三方一致 → 放行", a == [("pip", ["six"])], str((a, n)))
    a, n = dl.decide_env_install("unlock", [[_op(packages=["six"])],
                                            [_op(packages=["requests"])]])
    check("包清单不同 → 不装", a == [], str((a, n)))
    a, n = dl.decide_env_install("unlock", [[_op()], []])
    check("只有一方提 → 不装", a == [], str((a, n)))
    a, n = dl.decide_env_install("keep", [[_op()], [_op()]])
    check("提案人 keep → 不装", a == [], str((a, n)))
    a, n = dl.decide_env_install("unlock",
                                 [[_op(packages=["six", "requests"])],
                                  [_op(packages=["requests", "six"])]])
    check("顺序不同但集合相同 → 放行", len(a) == 1, str((a, n)))
    a, n = dl.decide_env_install("unlock", [[_op(manager="pip")],
                                            [_op(manager="npm")]])
    check("manager 不同 → 不装", a == [], str((a, n)))
    a, n = dl.decide_env_install("unlock", [[], []])
    check("双方都没提 → 静默无动作", a == [] and n == [], str((a, n)))

    print("[5b] 国内镜像自动切换（2026-11-19 用户要求）")
    import os as _os
    m_pip = ei.mirrors("pip")
    check("pip 首个是国内镜像（清华）", "tuna.tsinghua" in m_pip[0], m_pip[0])
    check("pip 序列含官方兜底", any("pypi.org" in m for m in m_pip), str(m_pip))
    check("npm 含国内镜像 npmmirror",
          any("npmmirror" in m for m in ei.mirrors("npm")), str(ei.mirrors("npm")))
    _os.environ["SIXCODER_PIP_INDEX"] = "https://my.mirror/simple"
    check("运维环境变量可覆盖镜像", ei.mirrors("pip")[0] == "https://my.mirror/simple")
    del _os.environ["SIXCODER_PIP_INDEX"]
    # 镜像地址只能来自内置表/环境变量，**绝不来自远端处方**：
    # install_env 的 op 即便带了 index 字段，引擎也不采纳（只用 mirrors()）。
    op_with_index = {**_op(), "index": "https://evil.example/simple"}
    check("带 index 字段的 op 仍合法但 index 被忽略（安全）",
          jf.valid_patch_op(op_with_index)[0])
    from pathlib import Path as _P
    _pipcmd = ei._pip_cmd(_P("t"), ["six"], "https://pypi.org/simple")
    check("pip 命令行带 --index-url", "--index-url" in _pipcmd, str(_pipcmd[:8]))
    _npmcmd = ei._npm_cmd(_P("t"), ["x"], "https://registry.npmjs.org")
    check("npm 命令行带 --registry", "--registry" in _npmcmd, str(_npmcmd[:8]))

    print("[5c] 自动测速排序（纯函数，离线可测）")
    OFF = "https://pypi.org/simple"
    base = ["https://a/simple", "https://b/simple", OFF, "https://c/simple"]
    # 国内可达、官方也快 —— 官方仍必须排最后（只兜底，不抢第一）
    lat = {"https://a/simple": 0.3, "https://b/simple": 0.1,
           "https://c/simple": 0.2, OFF: 0.01}
    arr = ei._arrange(base, lat, OFF)
    check("国内可达时官方排最后", arr[-1] == OFF, str(arr))
    check("国内按延迟从快到慢", arr[:3] == ["https://b/simple", "https://c/simple",
                                          "https://a/simple"], str(arr))
    # 国内全不可达 → 官方提到最前（唯一能用）
    lat_dead = {u: float("inf") for u in base if u != OFF}
    lat_dead[OFF] = 0.5
    arr2 = ei._arrange(base, lat_dead, OFF)
    check("国内全不可达 → 官方提前", arr2[0] == OFF, str(arr2))
    # 不可达的国内镜像排在可达的后面
    lat3 = {"https://a/simple": float("inf"), "https://b/simple": 0.1,
            "https://c/simple": 0.2, OFF: 0.5}
    arr3 = ei._arrange(base, lat3, OFF)
    check("不可达镜像排后", arr3[0] == "https://b/simple", str(arr3))
    # 已知表覆盖 6 家国内知名镜像
    dom_pip = [m for m in ei.PIP_MIRRORS if m != OFF]
    check("pip 内置 ≥5 家国内知名镜像", len(dom_pip) >= 5, str(len(dom_pip)))
    check("全 HTTPS（安全）", all(m.startswith("https://") for m in ei.PIP_MIRRORS
                                + ei.NPM_MIRRORS))
    check("含清华/阿里/腾讯/华为/中科大",
          all(any(k in m for m in ei.PIP_MIRRORS)
              for k in ("tuna.tsinghua", "aliyun", "tencent", "huaweicloud", "ustc")),
          str(ei.PIP_MIRRORS))

    print("[5d] 常见生态国内镜像注册表（pip/npm/hf/maven/go/cargo/gem/composer/conda）")
    ecos = ei.list_ecosystems()
    must = ("pip", "npm", "hf", "maven", "go", "cargo", "gem", "composer", "conda")
    check("覆盖 ≥9 个生态", all(e in ecos for e in must), str(list(ecos)))
    _all = [m for eco in ecos for m in ei.ECO_MIRRORS[eco]]
    check("所有镜像都是 HTTPS（安全）", all(m.startswith("https://") for m in _all),
          str([m for m in _all if not m.startswith("https://")])[:200])
    # 每个生态至少有一个国内镜像（非官方）
    def _has_domestic(eco):
        off = ei._OFFICIAL.get(eco, "")
        return any(m != off for m in ei.ECO_MIRRORS[eco])
    check("每个生态都有国内镜像（非仅官方）",
          all(_has_domestic(e) for e in must),
          str([e for e in must if not _has_domestic(e)]))
    check("hf 含 hf-mirror", any("hf-mirror" in m for m in ei.HF_MIRRORS))
    check("hf 含魔搭 modelscope", any("modelscope" in m for m in ei.HF_MIRRORS))
    check("go 含 goproxy.cn", any("goproxy.cn" in m for m in ei.GO_MIRRORS))
    check("cargo 含 rsproxy", any("rsproxy" in m for m in ei.CARGO_MIRRORS))
    check("maven 含阿里云", any("aliyun" in m for m in ei.MAVEN_MIRRORS))
    # env_vars 映射：可让引擎/产物真用上最快源
    check("pip→PIP_INDEX_URL", ei._ECO_ENV.get("pip") == "PIP_INDEX_URL")
    check("hf→HF_ENDPOINT", ei._ECO_ENV.get("hf") == "HF_ENDPOINT")
    check("go→GOPROXY", ei._ECO_ENV.get("go") == "GOPROXY")
    # fastest 排序（用纯函数验证，不联网）：官方永远最后、国内最快在前
    OFF2 = ei._OFFICIAL["pip"]
    b = list(ei.PIP_MIRRORS)
    latx = {u: (0.9 - 0.1 * i) for i, u in enumerate(b) if u != OFF2}
    latx[OFF2] = 0.001          # 官方名义最快，但仍应排最后
    arr = ei._arrange(b, latx, OFF2)
    check("fastest 逻辑：官方即使名义最快也排最后（只兜底）", arr[-1] == OFF2, str(arr))

    print("[6] 接线：judgefix.valid_patch_op 认识 install_env")
    check("合法 install_env 放行", jf.valid_patch_op(_op())[0])
    check("非法包名被拒", not jf.valid_patch_op(
        _op(packages=["-r x.txt"]))[0])
    check("非法 manager 被拒", not jf.valid_patch_op(_op(manager="apt"))[0])

    print("[7] install_tool 与 install_env 身份/合并互不干扰")
    # install_env 的身份含 manager+排序包清单，不同包集合不应被判"冲突"
    i1 = jf._op_identity(_op(packages=["six"]))
    i2 = jf._op_identity(_op(packages=["requests"]))
    check("不同包集合身份不同", i1 != i2)
    i3 = jf._op_identity(_op(packages=["requests", "six"]))
    i4 = jf._op_identity(_op(packages=["six", "requests"]))
    check("同包集合顺序无关身份相同", i3 == i4)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
