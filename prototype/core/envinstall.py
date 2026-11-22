"""环境包安装器：把 pip/npm 这类"装第三方包"的能力**隔离**地打开（2026-11-19 用户口径）。

## 为什么需要
用户定稿：**pip 这种环境安装也放权给协商**——但门槛是 **3/3 全票一致**
（写作者提案 + 两名独立审核者都主张装同一批包），普通判据修改只需 ≥2 票，
装包是更危险的动作，所以要更严的门槛。门槛判定在 core/deadlock.py，
本模块只管"拿到已决议的包清单后，安全地装上去"。

## 安全边界（宁可装不上，也不许装坏系统）
1. **隔离目录**：一律 `pip install --target <独立目录>` / `npm install --prefix <独立目录>`，
   每个包组合一个目录 `data/tool_lib/_env/<manager>_<hash>/`。**绝不装进系统 site-packages**。
   好处：不污染解释器、天然可回滚（删目录即卸载）、多个包集合互不干扰。
2. **包名严格白名单**：只接受 `名字` 或 `名字==精确版本`。拒绝 `-r/--requirement`、
   `-e/--editable`、`--index-url`、`git+`、`http(s)://`、`file:`、`@`、`;`、以 `./` `/` 开头的路径
   —— 这些能让 pip 执行任意代码或从任意源拉包，是供应链攻击面，一律拒。
3. **复验是裁判**：pip 返回 0 且目标目录里出现对应 `*.dist-info` 才算装好；
   失败/复验不过 → 整个目录删掉（不留半装），返回 errors。
4. **不静默吞异常**：每条失败都进 errors，调用方必须留痕。
5. **可审计**：装成功的目录里写 `INSTALL.json`（manager/packages/时间/输出摘要）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# 与 tool_lib 同源：data/ 是引擎运行期数据目录
ENV_ROOT = Path(__file__).resolve().parents[1] / "data" / "tool_lib" / "_env"

# 隔离安装的包管理器（可 --target 到独立目录，不污染系统）
MANAGERS = ("pip", "npm")
# **系统软件包管理器**（2026-11-19 用户口径：装 ollama/工具链这类也放权给协商）。
# 与 pip/npm 的根本差异：**装到系统、无法隔离、不便回滚** → 所以额外要求：
#   · 3/3 全票（同 install_env）；
#   · 包名严格白名单（防参数注入，远端的"包名"只能是一个名字）；
#   · 装完必须复验（list/show 或命令可用），并把命令与输出写审计日志；
#   · **不自动卸载**（回滚系统软件风险大于收益，交给运维）。
# apt 故意不收：它必须 sudo，而沙箱明令禁提权。
SYSTEM_MANAGERS = ("winget", "choco", "scoop", "brew")
ALL_MANAGERS = MANAGERS + SYSTEM_MANAGERS

# **国内知名镜像 + 官方兜底**（2026-11-19 用户要求：自动转国内镜像、安装时全自动
# 测速选最快、常见生态都要覆盖）。安全边界：这里只收**知名大厂/高校/社区镜像，
# 全部 HTTPS**；镜像地址只能来自本文件内置表 + 运维级环境变量，绝不接受远端
# 协商处方传入（否则远端可把包源指向任意地址 = 供应链攻击面）。
# 顺序=同等延迟下的兜底顺序；安装前会并发实测速按最快排序（见 rank_mirrors）。
PIP_MIRRORS = (
    "https://pypi.tuna.tsinghua.edu.cn/simple",       # 清华 TUNA
    "https://mirrors.aliyun.com/pypi/simple/",        # 阿里云
    "https://mirrors.cloud.tencent.com/pypi/simple",  # 腾讯云
    "https://repo.huaweicloud.com/repository/pypi/simple",  # 华为云
    "https://mirrors.ustc.edu.cn/pypi/simple",        # 中科大
    "https://mirrors.163.com/pypi/simple/",           # 网易
    "https://pypi.org/simple",                        # 官方兜底
)
NPM_MIRRORS = (
    "https://registry.npmmirror.com",                       # 淘宝 npmmirror
    "https://mirrors.cloud.tencent.com/npm/",               # 腾讯云
    "https://repo.huaweicloud.com/repository/npm/",         # 华为云
    "https://registry.npmjs.org",                           # 官方兜底
)
# HuggingFace 模型/数据集镜像：hf-mirror（社区镜像，广泛使用）+ 魔搭 ModelScope。
# 注意：魔搭不是 HF 的透明镜像，是独立平台；这里作为"国内可选源"并列，
# 具体能否服务某个 repo 要看资源是否存在——由调用方按需探测，引擎不假装一定有。
HF_MIRRORS = (
    "https://hf-mirror.com",            # HF 国内镜像（社区维护，常用）
    "https://www.modelscope.cn",        # 魔搭社区（阿里）
    "https://huggingface.co",           # 官方兜底
)
MAVEN_MIRRORS = (
    "https://maven.aliyun.com/repository/public",       # 阿里云
    "https://mirrors.cloud.tencent.com/nexus/repository/maven-public/",  # 腾讯云
    "https://repo.huaweicloud.com/repository/maven/",   # 华为云
    "https://repo1.maven.org/maven2/",                  # 官方兜底
)
GO_MIRRORS = (
    "https://goproxy.cn",               # 七牛
    "https://goproxy.io",               # 社区
    "https://mirrors.aliyun.com/goproxy/",   # 阿里云
    "https://proxy.golang.org",         # 官方兜底
)
CARGO_MIRRORS = (
    "https://rsproxy.cn/index/",        # 字节 rsproxy
    "https://mirrors.tuna.tsinghua.edu.cn/crates.io-index/",  # 清华
    "https://index.crates.io/",         # 官方兜底
)
GEM_MIRRORS = (
    "https://mirrors.tuna.tsinghua.edu.cn/rubygems/",   # 清华
    "https://mirrors.aliyun.com/rubygems/",             # 阿里云
    "https://rubygems.org",                             # 官方兜底
)
COMPOSER_MIRRORS = (
    "https://mirrors.aliyun.com/composer/",             # 阿里云
    "https://mirrors.cloud.tencent.com/composer/",      # 腾讯云
    "https://repo.packagist.org",                       # 官方兜底
)
CONDA_MIRRORS = (
    "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main/",  # 清华
    "https://mirrors.aliyun.com/anaconda/pkgs/main/",            # 阿里云
    "https://repo.anaconda.com/pkgs/main/",                      # 官方兜底
)
# 生态 → 镜像表（yarn/pnpm 与 npm 同源）。引擎据此"自动选择最快源"。
ECO_MIRRORS = {
    "pip": PIP_MIRRORS, "npm": NPM_MIRRORS, "yarn": NPM_MIRRORS,
    "pnpm": NPM_MIRRORS, "hf": HF_MIRRORS, "maven": MAVEN_MIRRORS,
    "go": GO_MIRRORS, "cargo": CARGO_MIRRORS, "gem": GEM_MIRRORS,
    "composer": COMPOSER_MIRRORS, "conda": CONDA_MIRRORS,
}
# 真能"装包"的生态（隔离安装实现在本模块）；其余生态只提供"最快源 + 环境变量"
INSTALLABLE = ("pip", "npm")
# 运维级覆盖（不是远端能传的）：一个镜像地址优先于内置表
_ENV_OVERRIDE = {"pip": "SIXCODER_PIP_INDEX", "npm": "SIXCODER_NPM_REGISTRY",
                 "hf": "SIXCODER_HF_ENDPOINT", "go": "SIXCODER_GOPROXY"}
# 官方源标识：只做兜底（国内镜像全不可达时才优先），不参与"国内最快"竞争
_OFFICIAL = {
    "pip": "https://pypi.org/simple", "npm": "https://registry.npmjs.org",
    "yarn": "https://registry.npmjs.org", "pnpm": "https://registry.npmjs.org",
    "hf": "https://huggingface.co", "maven": "https://repo1.maven.org/maven2/",
    "go": "https://proxy.golang.org", "cargo": "https://index.crates.io/",
    "gem": "https://rubygems.org", "composer": "https://repo.packagist.org",
    "conda": "https://repo.anaconda.com/pkgs/main/",
}
# 生态 → 生效该镜像要设置的环境变量（供引擎/产物真用上最快源）
_ECO_ENV = {"pip": "PIP_INDEX_URL", "hf": "HF_ENDPOINT", "go": "GOPROXY",
            "npm": "npm_config_registry", "yarn": "YARN_REGISTRY",
            "pnpm": "npm_config_registry", "composer": "COMPOSER_REPOSITORY"}

# 测速结果缓存（避免每次安装都测）：{manager: {order:[...], lat:{...}, at:ts}}
_RANK_CACHE = ENV_ROOT / ".mirror_rank.json"
_RANK_TTL = 6 * 3600          # 6 小时内复用测速结果
_PROBE_TIMEOUT = 3.0          # 单个镜像测速超时（秒）


def mirrors(manager: str) -> list[str]:
    """该生态的镜像**原始**序列（环境变量覆盖优先，其余内置表）。

    支持 ECO_MIRRORS 里的所有生态（pip/npm/yarn/pnpm/hf/maven/go/cargo/gem/
    composer/conda）。真正下载用的是 rank_mirrors()（测速后的顺序）。
    """
    mgr = str(manager or "").strip().lower()
    base = list(ECO_MIRRORS.get(mgr) or ())
    if not base:
        return []
    ov = os.environ.get(_ENV_OVERRIDE.get(mgr, ""), "").strip()
    if ov:
        return [ov] + [m for m in base if m != ov]
    return base


def _probe_latency(url: str, timeout: float = _PROBE_TIMEOUT) -> float:
    """测一个镜像的可达性与延迟（秒）；不可达/超时返回 inf。

    只做一次轻量 HEAD，拿到任何 HTTP 响应（含 3xx/4xx）就算通——我们测的是
    "网络到镜像可达且快"，不是"这个路径存在"。状态码 5xx 视为不可用。
    """
    import time as _t
    import urllib.request as _u
    t0 = _t.time()
    try:
        req = _u.Request(url, method="HEAD",
                         headers={"User-Agent": "sixcoder-mirror-probe"})
        with _u.urlopen(req, timeout=timeout) as r:
            if getattr(r, "status", 200) >= 500:
                return float("inf")
        return _t.time() - t0
    except Exception:                                     # noqa: BLE001
        return float("inf")


def _order_by_latency(base: list, lat: dict) -> list:
    """按延迟升序排序（纯函数、可单测）：不可达（inf）排后，同等延迟保持原序。"""
    idx = {u: i for i, u in enumerate(base)}
    return sorted(base, key=lambda u: (float(lat.get(u, float("inf"))), idx.get(u, 1 << 30)))


def _arrange(base: list, lat: dict, official: str) -> list:
    """国内镜像按测速排最快、官方源固定兜底（纯函数、可单测）。

    · 国内镜像全可达 → 官方排最后（只兜底，不跟国内抢第一，避免 HEAD 噪声
      让官方源插队，导致国内用户实际下载慢）；
    · 国内镜像全不可达 → 官方提到最前（那时它是唯一能用的）。
    """
    domestic = [u for u in base if u != official]
    order_dom = _order_by_latency(domestic, lat)
    dom_all_dead = (all(lat.get(u, float("inf")) == float("inf") for u in domestic)
                    if domestic else True)
    if official and official in base:
        return ([official] + order_dom) if dom_all_dead else (order_dom + [official])
    return order_dom


def _load_rank_cache() -> dict:
    try:
        return json.loads(_RANK_CACHE.read_text(encoding="utf-8"))
    except Exception:                                     # noqa: BLE001
        return {}


def _save_rank_cache(mgr: str, order: list, lat: dict) -> None:
    try:
        data = _load_rank_cache()
        data[mgr] = {"order": order,
                     "lat": {k: (None if v == float("inf") else round(v, 4))
                             for k, v in lat.items()},
                     "at": time.time()}
        ENV_ROOT.mkdir(parents=True, exist_ok=True)
        _RANK_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                               encoding="utf-8")
    except Exception:                                     # noqa: BLE001
        pass                                              # 缓存写失败不影响安装


def rank_mirrors(manager: str, *, force: bool = False,
                 timeout: float = _PROBE_TIMEOUT) -> list[str]:
    """**全自动测速**：并发探测所有镜像，按延迟从快到慢返回。

    规则：
      · 运维环境变量显式指定（SIXCODER_PIP_INDEX/NPM_REGISTRY）→ 固定排第一
        （明确指定优先于自动测速），其余测速排序；
      · 有 6 小时内缓存 → 直接用（避免每次安装都测速，省时间）；
      · 否则并发测速（总耗时 ≈ 单镜像超时，不是累加）。
    """
    mgr = str(manager or "").strip().lower()
    base = mirrors(mgr)
    if not base:
        return []
    ov = os.environ.get(_ENV_OVERRIDE.get(mgr, ""), "").strip()
    # 缓存命中（且缓存里的源仍在 base 里）
    if not force:
        row = (_load_rank_cache().get(mgr) or {})
        if row and time.time() - float(row.get("at") or 0) < _RANK_TTL:
            cached = [u for u in (row.get("order") or []) if u in base]
            if cached:
                rest = [u for u in base if u not in cached]
                order = cached + rest
                if ov and ov in order:
                    order = [ov] + [u for u in order if u != ov]
                return order
    # 并发测速
    lat: dict = {}
    try:
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=max(1, len(base))) as ex:
            futs = {ex.submit(_probe_latency, u, timeout): u for u in base}
            for f in _cf.as_completed(futs, timeout=timeout + 3):
                u = futs[f]
                try:
                    lat[u] = f.result()
                except Exception:                         # noqa: BLE001
                    lat[u] = float("inf")
    except Exception:                                     # noqa: BLE001
        # 测速整体失败（如无网络）→ 退回原始顺序，不让安装流程崩
        return base
    # **官方源只做兜底**（2026-11-19 用户口径：自动转国内、选最快）。
    order = _arrange(base, lat, _OFFICIAL.get(mgr, ""))
    if ov and ov in order:
        order = [ov] + [u for u in order if u != ov]
    _save_rank_cache(mgr, order, lat)
    return order

# 包名：字母数字开头，允许 . _ -；可选 ==精确版本。
# 刻意**只允许 == 精确版本**（不许 >=/~/< 这类范围），避免一次拉进一堆未知版本。
_PKG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}(==[A-Za-z0-9._-]{1,40})?$")
_DENY_SUBSTR = ("-r", "--requirement", "-e", "--editable", "--index",
                "git+", "http://", "https://", "file:", "@", ";", "&&", "|",
                ">", "<", "`", "$(", "/", "\\")


def valid_package(spec) -> tuple[bool, str]:
    """单个包规格是否安全。返回 (ok, 说明)。"""
    if not isinstance(spec, str) or not spec.strip():
        return False, "包名必须是非空字符串"
    s = spec.strip()
    if " " in s or "\t" in s:
        return False, "包名不能含空白：%r" % s
    low = s.lower()
    for bad in _DENY_SUBSTR:
        if bad in low:
            return False, "包名含危险片段 %r：%s" % (bad, s)
    if not _PKG_RE.match(s):
        return False, "包名格式非法（只允许 名字 或 名字==版本）：%s" % s
    return True, ""


def validate_request(manager, packages) -> tuple[bool, str]:
    """校验一次安装请求。返回 (ok, 说明)。"""
    if str(manager or "").strip().lower() not in ALL_MANAGERS:
        return False, "manager 只允许 %s：%r" % ("/".join(ALL_MANAGERS), manager)
    if not isinstance(packages, list) or not packages:
        return False, "packages 必须是非空数组"
    if len(packages) > 20:
        return False, "一次最多装 20 个包"
    seen = set()
    for p in packages:
        ok, why = valid_package(p)
        if not ok:
            return False, why
        key = str(p).strip().lower()
        if key in seen:
            return False, "重复包：%s" % p
        seen.add(key)
    return True, ""


def _norm(name: str) -> str:
    """PEP 503 规范化：小写、-/_/. 等价（用于 dist-info 比对）。"""
    return re.sub(r"[-_.]+", "-", str(name).split("==")[0].strip().lower())


# 包管理器常见安装路径回退（2026-11-19 实测：winget 装在 WindowsApps 但**不在 PATH**，
# `which winget` 失败 → 误报"本机没有"。这里按已知路径兜底，不要求用户改 PATH）。
_SYS_FALLBACK = {
    "winget": ["%LOCALAPPDATA%/Microsoft/WindowsApps/winget.exe",
               "C:/Program Files/WindowsApps/Microsoft.DesktopAppInstaller_*/winget.exe"],
    "scoop": ["%USERPROFILE%/scoop/shims/scoop.cmd"],
    "choco": ["C:/ProgramData/chocolatey/bin/choco.exe"],
}


def _resolve_mgr(mgr: str) -> str:
    """找包管理器可执行文件：先 PATH（which），失败再按常见安装路径回退。"""
    import glob as _glob
    import shutil as _sh
    p = _sh.which(mgr)
    if p:
        return p
    for raw in _SYS_FALLBACK.get(mgr, []):
        cand = os.path.expandvars(raw)
        if "*" in cand:
            hits = sorted(_glob.glob(cand), reverse=True)
            if hits:
                return hits[0]
        elif os.path.isfile(cand):
            return cand
    return ""


# 系统包管理器的安装/复验命令模板（{pkg} 由**已校验的包名**替换，远端不能注入参数）。
_SYS_CMD = {
    "winget": ["winget", "install", "--id", "{pkg}", "--silent",
               "--accept-package-agreements", "--accept-source-agreements",
               "--disable-interactivity"],
    "choco": ["choco", "install", "{pkg}", "-y", "--no-progress"],
    "scoop": ["scoop", "install", "{pkg}"],
    "brew": ["brew", "install", "{pkg}"],
}
_SYS_VERIFY = {
    "winget": ["winget", "list", "--id", "{pkg}", "--accept-source-agreements"],
    "choco": ["choco", "list", "--local-only", "{pkg}"],
    "scoop": ["scoop", "list", "{pkg}"],
    "brew": ["brew", "list", "{pkg}"],
}


def _install_system(mgr: str, pkgs: list, *, timeout: int = 900) -> dict:
    """系统软件安装（winget/choco/scoop/brew）：**装到系统、不可隔离**。

    与 pip/npm 的根本差异，所以安全设计不同：
      · 不可 --target 隔离 → 只能靠"3/3 全票 + 包名白名单 + 复验 + 审计"兜；
      · **失败不回滚**（卸系统软件的风险大于收益），只写审计日志交运维；
      · 包管理器本身不在 → 如实报"先手动装包管理器"，不假装能装。
    """
    exe = _resolve_mgr(mgr)
    if not exe:
        return {"ok": False, "manager": mgr, "dir": "",
                "errors": ["本机没有 %s（需先手动安装该包管理器；引擎不会自动装包管理器）"
                           % mgr]}
    ok_pkgs, errors, out_tail = [], [], {}
    for pkg in pkgs:
        # 第 0 项换成解析到的可执行文件（PATH 里没有时用完整路径）
        cmd = [x.replace("{pkg}", pkg) for x in _SYS_CMD[mgr]]
        cmd[0] = exe
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, encoding="utf-8",
                                errors="replace", cwd=str(ENV_ROOT))
        except subprocess.TimeoutExpired:
            errors.append("%s 装 %s 超时 >%ds" % (mgr, pkg, timeout))
            continue
        except Exception as exc:                         # noqa: BLE001
            errors.append("%s 装 %s 异常：%s: %s"
                          % (mgr, pkg, type(exc).__name__, exc))
            continue
        out = ((cp.stdout or "") + (cp.stderr or "")).strip()
        out_tail[pkg] = out[-400:]
        if cp.returncode != 0:
            errors.append("%s 装 %s 失败 rc=%s：%s"
                          % (mgr, pkg, cp.returncode, out[-300:]))
            continue
        # 复验：list 里能看到才算装上（不是只看安装命令 rc==0）
        vcmd = [x.replace("{pkg}", pkg) for x in _SYS_VERIFY[mgr]]
        vcmd[0] = exe
        try:
            vp = subprocess.run(vcmd, capture_output=True, text=True, timeout=120,
                                encoding="utf-8", errors="replace")
            vout = ((vp.stdout or "") + (vp.stderr or ""))
            if vp.returncode == 0 and pkg.lower() in vout.lower():
                ok_pkgs.append(pkg)
            else:
                errors.append("%s 装完复验失败：list 里没找到 %s" % (mgr, pkg))
        except Exception as exc:                         # noqa: BLE001
            errors.append("%s 复验异常：%s" % (mgr, exc))
    # 审计日志（系统变更必须留痕）
    try:
        ENV_ROOT.mkdir(parents=True, exist_ok=True)
        with open(ENV_ROOT / "system_installs.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"manager": mgr, "requested": pkgs,
                                "ok": ok_pkgs, "errors": errors,
                                "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                               ensure_ascii=False) + "\n")
    except Exception:                                    # noqa: BLE001
        pass
    return {"ok": bool(ok_pkgs) and not errors, "manager": mgr, "dir": "",
            "errors": errors, "output": str(out_tail)[:400],
            "verify": "list 确认：%s" % ",".join(ok_pkgs) if ok_pkgs else ""}


def _dir_for(manager: str, packages: list) -> Path:
    """每个包组合一个稳定目录（同样的包清单落在同一目录，幂等）。"""
    key = json.dumps({"m": str(manager).lower(),
                      "p": sorted(str(x).strip().lower() for x in packages)},
                     sort_keys=True, ensure_ascii=False)
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]
    return ENV_ROOT / ("%s_%s" % (str(manager).lower(), h))


def _pip_cmd(target: Path, packages: list, index_url: str) -> list[str]:
    return [sys.executable, "-m", "pip", "install", "--target", str(target),
            "--index-url", str(index_url),
            "--no-input", "--disable-pip-version-check", "--no-warn-script-location",
            *[str(p) for p in packages]]


def _npm_cmd(target: Path, packages: list, registry: str) -> list[str]:
    return ["npm", "install", "--prefix", str(target), "--registry", str(registry),
            "--no-audit", "--no-fund", "--loglevel", "error",
            *[str(p) for p in packages]]


def _verify(manager: str, target: Path, packages: list) -> tuple[bool, str]:
    """复验：目标目录里真的出现了这些包的标识（dist-info / package.json）。"""
    if manager == "pip":
        if not target.is_dir():
            return False, "目标目录不存在"
        dist = [d for d in target.iterdir() if d.is_dir() and d.name.endswith(".dist-info")]
        if not dist:
            return False, "目录里没有 .dist-info（pip 没真装上）"
        have = {_norm(d.name[:-len(".dist-info")]) for d in dist}
        # dist-info 名形如 name-version，去掉版本
        have = {re.sub(r"-\d.*$", "", h) for h in have}
        miss = [_norm(p) for p in packages if _norm(p) not in have]
        if miss:
            return False, "缺少包的 dist-info：%s" % ", ".join(miss)
        return True, "dist-info 齐全"
    # npm
    nm = target / "node_modules"
    if not nm.is_dir():
        return False, "node_modules 不存在"
    miss = []
    for p in packages:
        name = str(p).split("==")[0].strip()
        if not (nm / name / "package.json").is_file():
            miss.append(name)
    if miss:
        return False, "缺少包：%s" % ", ".join(miss)
    return True, "node_modules 齐全"


def installed(manager: str, packages: list) -> bool:
    d = _dir_for(manager, packages)
    if not (d / "INSTALL.json").is_file():
        return False
    ok, _ = _verify(str(manager).lower(), d, packages)
    return ok


def installed_paths() -> list[str]:
    """所有已装且复验通过的 **pip 包目录**（供产物/探针真用上这些包）。

    接在 `probe._env()` 里注入 PYTHONPATH —— 否则协商装好的包只是摆在磁盘上、
    产物根本 import 不到（"装完就摆着"等于没装）。npm 目录不进 PYTHONPATH，
    由产物自己的 NODE_PATH 处理，这里只返 pip 的 `_env/*` 目录。
    """
    out = []
    if not ENV_ROOT.is_dir():
        return out
    for d in sorted(ENV_ROOT.iterdir()):
        if not d.is_dir() or not (d / "INSTALL.json").is_file():
            continue
        try:
            m = json.loads((d / "INSTALL.json").read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            continue
        if str(m.get("manager")) != "pip":
            continue
        if _verify("pip", d, m.get("packages") or [])[0]:
            out.append(str(d))
    return out


def install(manager: str, packages: list, *, timeout: int = 300) -> dict:
    """隔离安装一批包。返回 {ok, dir, errors, verify, output}。失败整目录回滚。"""
    mgr = str(manager or "").strip().lower()
    ok, why = validate_request(mgr, packages)
    if not ok:
        return {"ok": False, "errors": [why], "dir": "", "verify": ""}
    pkgs = [str(p).strip() for p in packages]
    # **系统软件走独立通道**（口径差异见 SYSTEM_MANAGERS 注释：不可隔离/不自动卸载）。
    if mgr in SYSTEM_MANAGERS:
        return _install_system(mgr, pkgs, timeout=max(timeout, 300))
    target = _dir_for(mgr, pkgs)
    # 幂等：已装且复验通过 → 直接用
    if (target / "INSTALL.json").is_file():
        vok, vwhy = _verify(mgr, target, pkgs)
        if vok:
            _idxc = ""
            try:
                _idxc = str(json.loads((target / "INSTALL.json")
                                       .read_text(encoding="utf-8")).get("index") or "")
            except Exception:                             # noqa: BLE001
                pass
            return {"ok": True, "dir": str(target), "index": _idxc, "errors": [],
                    "verify": "already installed", "output": ""}
        # 登记过但复验不过 → 删掉重装
        shutil.rmtree(target, ignore_errors=True)
    ENV_ROOT.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    # **安装时就地实测速 → 按最快排序 → 逐个试**（2026-11-19 用户口径：
    # 测速是"用户安装的时候"的自动测速，不是启动时/定时）。force=True 每次实测，
    # 避免用陈旧缓存选到已经不快的源；测速本身并发，耗时 ≈ 单镜像超时。
    # 每次重试前清空目标目录，避免上一个镜像的半装残留影响复验。
    tried, last_err, out = [], "", ""
    for idx in rank_mirrors(mgr, force=True):
        tried.append(idx)
        shutil.rmtree(target, ignore_errors=True)
        cmd = (_pip_cmd(target, pkgs, idx) if mgr == "pip"
               else _npm_cmd(target, pkgs, idx))
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout, encoding="utf-8",
                                errors="replace", cwd=str(ENV_ROOT))
        except subprocess.TimeoutExpired:
            last_err = "%s 超时 >%ds" % (idx, timeout)
            continue
        except Exception as exc:                          # noqa: BLE001
            last_err = "%s 异常：%s: %s" % (idx, type(exc).__name__, exc)
            continue
        out = ((cp.stdout or "") + (cp.stderr or "")).strip()
        if cp.returncode != 0:
            last_err = "%s 失败(rc=%s)：%s" % (idx, cp.returncode, out[-400:])
            continue
        vok, vwhy = _verify(mgr, target, pkgs)
        if not vok:
            last_err = "%s 装完复验不过：%s" % (idx, vwhy)
            continue
        # 成功：记下用的镜像
        try:
            (target / "INSTALL.json").write_text(json.dumps({
                "manager": mgr, "packages": pkgs, "index": idx,
                "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "verify": vwhy, "output_tail": out[-500:]},
                ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as exc:                            # noqa: BLE001
            return {"ok": True, "dir": str(target), "index": idx,
                    "errors": ["写 INSTALL.json 失败：%s" % exc],
                    "verify": vwhy, "output": out[-400:]}
        return {"ok": True, "dir": str(target), "index": idx, "errors": [],
                "verify": vwhy, "output": out[-400:]}
    # 所有镜像都失败 → 整目录删除，如实报（不静默吞）
    shutil.rmtree(target, ignore_errors=True)
    return {"ok": False, "dir": "", "index": "", "verify": "",
            "errors": ["所有镜像均失败（尝试 %s）；最后一个错误：%s"
                       % (", ".join(tried), last_err[-600:])],
            "output": out[-300:]}


def fastest(eco: str, *, force: bool = True,
            timeout: float = _PROBE_TIMEOUT) -> str:
    """该生态当前**最快**的国内镜像地址（安装时实测速）。无可用则返回 ""。"""
    order = rank_mirrors(eco, force=force, timeout=timeout)
    return order[0] if order else ""


def env_vars(eco: str, *, force: bool = True) -> dict:
    """返回"让该生态用上最快国内镜像"所需的环境变量（供引擎/产物真用上）。

    例：pip → {"PIP_INDEX_URL": <最快源>}；hf → {"HF_ENDPOINT": <hf-mirror/魔搭>}；
    go → {"GOPROXY": <goproxy.cn ...>}。没有已知环境变量映射的生态返回 {}。
    """
    key = _ECO_ENV.get(str(eco or "").strip().lower())
    if not key:
        return {}
    url = fastest(eco, force=force)
    return {key: url} if url else {}


def list_ecosystems() -> dict:
    """引擎登记的所有生态及镜像数（供 UI / 自检 / 报告查看覆盖范围）。"""
    return {eco: {"count": len(urls), "official": _OFFICIAL.get(eco, ""),
                  "env": _ECO_ENV.get(eco, "")}
            for eco, urls in ECO_MIRRORS.items()}


def list_envs() -> list[dict]:
    """列出已隔离安装的包集合（读 INSTALL.json）。"""
    out = []
    if not ENV_ROOT.is_dir():
        return out
    for d in sorted(ENV_ROOT.iterdir()):
        p = d / "INSTALL.json"
        if not d.is_dir() or not p.is_file():
            continue
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except Exception:                                 # noqa: BLE001
            continue
        out.append({"manager": m.get("manager"), "packages": m.get("packages") or [],
                    "index": m.get("index", ""), "dir": str(d),
                    "installed_at": m.get("installed_at", "")})
    return out


def remove(manager: str, packages: list) -> dict:
    ok, why = validate_request(manager, packages)
    if not ok:
        return {"ok": False, "errors": [why]}
    d = _dir_for(str(manager).lower(), [str(p).strip() for p in packages])
    if not d.is_dir():
        return {"ok": False, "errors": ["未安装该包集合"]}
    try:
        shutil.rmtree(d)
        return {"ok": True, "errors": []}
    except OSError as exc:
        return {"ok": False, "errors": ["卸载失败：%s" % exc]}
