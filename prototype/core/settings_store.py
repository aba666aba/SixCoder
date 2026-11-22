"""设置存储：远端 API 的 base_url / model / api_key 可随时在设置里更新。

约定（对齐 DSH 的做法）：
- 配置文件只做"最后写入者生效"，每次请求重新读取 → 改设置不需要重启；
- 读取时永远不把 api_key 原样返回（只回掩码 + 是否已配置）；
- 环境变量 DEEPSEEK_API_KEY 优先级低于设置里的显式值？——反过来：设置里显式填了就用设置的，
  否则回落到环境变量，两者都没有就视为"远端不可用"。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from . import paths as _paths

# 配置放在引擎自己的 data/ 下。**打包时生成空白模板**（见
# packaging/build_installer.py::write_settings_template）：安装包只带
# `settings.template.json`，绝不带开发机的真实 `settings.json`。
# 允许用环境变量指向另一份配置 —— **自检必须用它**：
# 用户会在界面上改模型/价格/启用状态（`data/settings.json` 是本地配置，不进库），
# 自检若读真实配置，用户一改配置就有一批自检红，而且看起来像代码坏了
# （实测 2026-11-10 23:50：check_pricing / check_single_model / check_remote_profiles
#  在用户禁用 pro、改过价格之后全红；换一份干净配置立刻全绿 —— 是自检的问题，不是代码的）。
ENGINE_ROOT = Path(__file__).resolve().parent.parent
SETTINGS = Path(os.environ.get("SIXCODER_SETTINGS")
                or (ENGINE_ROOT / "data" / "settings.json"))
SETTINGS_TEMPLATE = ENGINE_ROOT / "data" / "settings.template.json"


def _is_installed_tree() -> bool:
    exe = Path(sys.executable).resolve()
    return exe.parent.name.lower() == "python" and exe.parent.parent.name.lower() == "resources"


# 覆盖安装会把安装目录整体换掉（NSIS 先删旧文件再解压），放在里面的 settings.json
# 会跟着消失 —— 用户看到的就是"装个新版本/重开一次，key 全没了"（2026-11-21 报障）。
# 配置本体仍按用户要求留在原地；打包态额外镜像一份到数据目录，
# 下次启动没找到本体时先恢复镜像，再退回空白模板。
MIRROR = (_paths.data_dir() / "settings.mirror.json"
          if _paths.is_packaged() and _is_installed_tree() else None)


def _mirror_write() -> None:
    """把当前配置同步到镜像（best-effort，失败不影响主流程）。"""
    if not MIRROR:
        return
    try:
        MIRROR.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SETTINGS, MIRROR)
    except OSError:
        pass


def _bootstrap_packaged_settings() -> None:
    """安装版启动：没有配置就先恢复镜像、再退回空白模板；有配置则刷新镜像。

    **这里绝不清任何 key**。旧实现有一段"升级后清一次 key"，标记写在安装目录里：
    首次安装拷完模板就 return 了、标记没写，用户填好 key 再启动一次命中清理分支，
    刚填的 key 被清空（2026-11-21 实测复现）。包里本来就不带 settings.json
    （打包硬闸保证），没有任何东西需要运行时清理，这段逻辑只剩误伤。
    """
    if not (_paths.is_packaged() and _is_installed_tree()):
        return
    if SETTINGS.exists():
        _mirror_write()
        return
    for src in (MIRROR, SETTINGS_TEMPLATE):
        if src and src.exists():
            try:
                SETTINGS.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, SETTINGS)
                return
            except OSError:
                continue


_bootstrap_packaged_settings()

# ---------------------------------------------------------------- 厂商预设
#
# 2026-11-09 用户口径：**厂商是一等公民**。
#   · 界面：点开「远端」→ 一个下拉（deepseek / glm / openai）+ 三个长方矩形；
#     矩形中间是大字厂商名，右侧偏右是「正在使用」的单选标记；点矩形弹出该厂商的配置。
#   · DeepSeek 与 GLM **开箱带默认模型**（含默认调用参数与思考等级），用户只填 apikey；
#     OpenAI **不带**默认模型，用户自己加。
#
# 每家的默认参数都来自**官方文档**（2026-11-09 核对），不是猜的：
#   · DeepSeek：https://api.deepseek.com —— reasoning_effort = none/low/high/max
#   · GLM：https://open.bigmodel.cn/api/paas/v4 —— thinking.type=enabled/disabled，
#     reasoning_effort = max/high/low（**没有 none**，GLM-5.3 起强制开启思考）
#   · OpenAI：https://api.openai.com/v1 —— 通用兼容，各模型思考能力不同
PROVIDER_IDS = ("deepseek", "glm", "openai")

PROVIDER_PRESETS: dict[str, dict] = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        # 官方支持的四档（none = 不思考）
        "efforts": ["none", "low", "high", "max"],
        "models": [
            {"id": "flash", "label": "DeepSeek Flash（主力）", "model": "deepseek-flash",
             "efforts": ["none", "low", "high", "max"], "max_output_tokens": 0,
             "temperature": 0.2, "priority": 10,
             "pricing": {"offpeak": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0},
                         "peak": {"in_hit": 0.04, "in_miss": 2.0, "out": 8.0}}},
            {"id": "pro", "label": "DeepSeek Pro（更强，默认不启用）",
             "model": "deepseek-v4-pro", "efforts": ["none", "low", "high", "max"],
             "max_output_tokens": 0, "temperature": 0.2, "priority": 20, "enabled": False,
             "pricing": {"offpeak": {"in_hit": 0.15, "in_miss": 4.5, "out": 13.5},
                         "peak": {"in_hit": 0.30, "in_miss": 9.0, "out": 27.0}}},
        ],
    },
    "glm": {
        "label": "GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # GLM 官方 reasoning_effort 只有 max/high/low；**没有 none**
        # （"不思考"要用 thinking.type=disabled，而 GLM-5.3 起已不支持禁用）。
        "efforts": ["low", "high", "max"],
        "models": [
            {"id": "glm-5.3", "label": "GLM-5.3（深度思考）", "model": "glm-5.3",
             "efforts": ["low", "high", "max"], "max_output_tokens": 131072,
             "temperature": 1.0, "priority": 10,
             "pricing": {"offpeak": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0},
                         "peak": {"in_hit": 0.04, "in_miss": 2.0, "out": 8.0}}},
            {"id": "glm-5.3-flash", "label": "GLM-5.3-Flash（轻快）", "model": "glm-5.3-flash",
             "efforts": ["low", "high", "max"], "max_output_tokens": 131072,
             "temperature": 1.0, "priority": 20,
             "pricing": {"offpeak": {"in_hit": 0.01, "in_miss": 0.5, "out": 2.0},
                         "peak": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0}}},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        # OpenAI 通用兼容：各模型思考能力不同，默认单档，用户自己配。
        "efforts": ["none"],
        # **故意不带默认模型**（用户口径：OpenAI 里面的默认模型没有，需要自己填写）
        "models": [],
    },
}

DEFAULTS = {
    "remote": {
        "enabled": True,
        "provider": "deepseek",
        # 空 = 跟随 provider（默认 deepseek）；设 "openai" 走 OpenAI 兼容格式。
        # 档案级 compat 优先于这里，方便一条档案接第三方端点、其余仍是 DS。
        "compat": "",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "api_key": "",
        # **真正的上限按 token 算**：官方 1M 上下文，输入+输出共用；整文件模式给 32K 输出，
        # 所以输入的安全上限约 950K tokens。字符上限只是辅助（字符→token 比率随内容漂）。
        "max_prompt_tokens": 950000,
        # 这两个数字原来拍的是 4000 / 14000（省钱口径）。官方文档口径（2026-10 页）：
        # 上下文 1M、输出最大 384K；max_tokens 不设时非思考默认 8K、思考 64K（max 档 128K）。
        # 8000 / 200000 只是我们自己的"单次安全上限"，不是模型上限。
        "max_output_tokens": 0,   # 0 = 自动（按任务规模算配额）
        "max_prompt_chars": 2000000,
        # 同一档连续失败几次才升到下一档（用户口径："低级模型多次无法解决才调下一层"）
        "escalate_after": 1,
        # 校准值（可手改）：思考预留 / 正文下限 / 单次上限 / 单模块预算 / 写代码思考封顶。
        # 这些数字**不该硬编码**——它们随模型与任务规模变化，由 core/calibration.py
        # 按真实账本自动重算（auto=True 时每轮收工后尝试一次）。
        "calibration": {
            "source": "defaults",
            "reasoning_reserve": {"low": 12000, "high": 24000, "max": 40000},
            "content_floor": 12000,
            "safety_cap": 128000,
            "module_budget_cny": 0.15,
            "code_effort_ceiling": "high",
            "auto": True,
            "min_samples": 20,
            "updated_at": "",
        },
        # 开发者模式的远端开关：默认**关**（本地模型自己写）。
        # 甲方模式强制开启；只读模式强制关闭 —— 见 core/policy.py 的 remote_allowed()。
        "dev_remote": False,
        # 省钱优先：第 0 档用**本地模型**（免费），本地做不出来再上远端八档。
        # 关掉它就是纯远端阶梯（快、但每单都烧 token）。
        # 甲方模式已经改成"整文件由远端写"，本地不再参与写代码（只做委托与机械验证）。
        # 这个开关保留给实验/开发者场景：打开后阶梯第 0 档是本地模型。
        "local_first": False,
        "thinking": "off",
        # ---------------------------------------------------------------
        # 模型档案（可增删改）：夜班按 priority 升序、每档再按 efforts 顺序组成阶梯。
        # 默认八档（用户指定）：flash:none → low → high → max → pro:none → low → high → max
        # 价格单位：人民币 / 百万 tokens（官方 2026-10 价目表，空闲时段为高峰的一半）
        "models": [
            {
                "id": "flash", "label": "DeepSeek V4.1 Flash", "model": "deepseek-flash",
                "base_url": "", "api_key": "", "enabled": True, "priority": 10,
                "efforts": ["none", "low", "high", "max"],
                # max_output_tokens=0 → 由 tokens.output_budget 按任务规模算（唯一真相源）
                "max_output_tokens": 0, "temperature": 0.2,
                "pricing": {
                    "offpeak": {"in_hit": 0.02, "in_miss": 1.0, "out": 4.0},
                    "peak": {"in_hit": 0.04, "in_miss": 2.0, "out": 8.0},
                },
            },
            {
                # pro 默认**关掉**：官方价 flash 高峰 2/8 元 vs pro 9/27 元（3~4 倍），
                # 而架构设计与整模块实现 flash 基本够用。要用就在界面把 enabled 打开。
                "id": "pro", "label": "DeepSeek V4 Pro（默认不启用）", "model": "deepseek-v4-pro",
                "base_url": "", "api_key": "", "enabled": False, "priority": 20,
                "efforts": ["none", "low", "high", "max"],
                "max_output_tokens": 0, "temperature": 0.2,
                "pricing": {
                    "offpeak": {"in_hit": 0.15, "in_miss": 4.5, "out": 13.5},
                    "peak": {"in_hit": 0.30, "in_miss": 9.0, "out": 27.0},
                },
            },
        ],
        # 价目表（美元 / 每百万 token）—— 旧字段，保留给老配置；新逻辑读 models[].pricing
        "pricing": {
            "deepseek-flash": {"in_miss": 0.28, "in_hit": 0.028, "out": 0.42,
                               "offpeak_ratio": 0.5},
            "deepseek-chat": {"in_miss": 0.27, "in_hit": 0.07, "out": 1.10,
                              "offpeak_ratio": 0.5},
        },
        # 高峰时段（官方：北京时间周一至周五 9:00-12:00、14:00-18:00，法定节假日除外；
        # 其余时段含周末全天为空闲，价格减半）
        "peak_window": {"ranges": ["09:00-12:00", "14:00-18:00"], "weekdays_only": True,
                        "tz": "Asia/Shanghai"},
        "offpeak_window": {"start": "00:30", "end": "08:30", "tz": "UTC"},
    }
}


def load() -> dict:
    data = json.loads(json.dumps(DEFAULTS))
    if SETTINGS.exists():
        try:
            raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
        except Exception:                              # noqa: BLE001
            raw = {}
        for k, v in (raw or {}).items():
            if isinstance(v, dict) and isinstance(data.get(k), dict):
                data[k].update(v)
            else:
                data[k] = v
    return data


def save(data: dict) -> None:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # 同步刷新镜像：覆盖安装后启动才能把这份配置恢复回来
    _mirror_write()


def get_remote() -> dict:
    return load().get("remote", {})


def _norm_pricing(raw: dict) -> dict:
    """价目表规范化：只保留合法数值字段，非法项丢弃（不让一个手滑把配置写坏）。"""
    out: dict = {}
    for model, spec in (raw or {}).items():
        if not isinstance(spec, dict):
            continue
        row = {}
        for k in ("in_miss", "in_hit", "out"):
            try:
                row[k] = max(0.0, float(spec.get(k)))
            except (TypeError, ValueError):
                pass
        try:
            ratio = float(spec.get("offpeak_ratio"))
            row["offpeak_ratio"] = min(1.0, max(0.0, ratio))
        except (TypeError, ValueError):
            pass
        if row:
            out[str(model)[:64]] = row
    return out


def _norm_window(raw: dict) -> dict:
    def hhmm(v: str, default: str) -> str:
        try:
            h, m = str(v).split(":")
            h, m = int(h), int(m)
            if 0 <= h < 24 and 0 <= m < 60:
                return f"{h:02d}:{m:02d}"
        except Exception:                              # noqa: BLE001
            pass
        return default
    raw = raw or {}
    return {"start": hhmm(raw.get("start", "00:30"), "00:30"),
            "end": hhmm(raw.get("end", "08:30"), "08:30"),
            "tz": str(raw.get("tz") or "UTC")[:16]}


def _norm_peak_window(raw: dict) -> dict:
    """高峰时段（is_offpeak 真正读的字段）：ranges / weekdays_only / tz。

    规范化到可判定形态，非法区间一律丢弃（不许界面手滑把时段写坏导致全天空闲，
    那样成本会静默算错）。空 ranges = 用默认两段。
    """
    raw = raw or {}
    ranges: list[str] = []
    for item in (raw.get("ranges") or []):
        try:
            a, b = str(item).split("-")
            ha, ma = (int(x) for x in a.strip().split(":"))
            hb, mb = (int(x) for x in b.strip().split(":"))
            if not (0 <= ha < 24 and 0 <= ma < 60 and 0 <= hb < 24 and 0 <= mb < 60):
                continue
            if hb * 60 + mb <= ha * 60 + ma:
                continue
            ranges.append(f"{ha:02d}:{ma:02d}-{hb:02d}:{mb:02d}")
        except (ValueError, AttributeError, TypeError):
            continue
    if not ranges:
        ranges = ["09:00-12:00", "14:00-18:00"]
    return {"ranges": ranges[:8], "weekdays_only": bool(raw.get("weekdays_only", True)),
            "tz": str(raw.get("tz") or "Asia/Shanghai")[:32]}


DEFAULT_PEAK_WINDOW = {"ranges": ["09:00-12:00", "14:00-18:00"],
                       "weekdays_only": True, "tz": "Asia/Shanghai"}


EFFORTS = ("none", "low", "high", "max")
EFFORT_ALIAS = {"off": "none", "minimal": "low", "medium": "high", "xhigh": "high",
                "disabled": "none", "enabled": "high", "": "none"}


def _norm_effort(v: str) -> str:
    """思考强度规范化：别名先映射，合法值原样保留，非法值当 none。"""
    key = str(v if v is not None else "").strip().lower()
    if key in EFFORTS:
        return key
    return EFFORT_ALIAS.get(key, "none")


def _norm_efforts(raw) -> list[str]:
    """思考档位列表：只保留 none/low/high/max，去重且按固定强度升序。"""
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    got = {_norm_effort(x) for x in (raw or [])}
    return [e for e in EFFORTS if e in got]


def _norm_model_profile(raw: dict) -> dict:
    """单个模型档案规范化。非法/越界一律收敛，绝不让手滑把配置写坏。"""
    raw = raw or {}

    def num(v, lo, hi, default):
        try:
            return max(lo, min(hi, type(default)(v)))
        except (TypeError, ValueError):
            return default

    mid = str(raw.get("id") or "").strip()[:32]
    model = str(raw.get("model") or "").strip()[:64]
    compat = str(raw.get("compat") or "").strip().lower()
    if compat not in ("", "deepseek", "openai"):
        compat = ""                  # 非法值收敛到「未设置」，交给下游回落，不猜协议
    # 档案没写协议时，按**模型名**定族：deepseek-* 一律是 DeepSeek 协议。
    # 不这么定的话，空 compat 会继承全局 compat（可能已被配成 openai），
    # 结果把 DS 的 reasoning_effort 丢掉、还继承别家端点的 base_url ——
    # 实测（2026-11-07）：DeepSeek key 被发到智谱端点，报 401，看起来像 key 失效。
    # 其它模型仍保持空 = 跟随全局，不擅自替用户拍板。
    if not compat and str(raw.get("model") or "").strip().lower().startswith("deepseek-"):
        compat = "deepseek"
    pricing = raw.get("pricing") or {}

    def price_block(b):
        b = b or {}
        return {"in_hit": num(b.get("in_hit"), 0.0, 1e4, 0.0),
                "in_miss": num(b.get("in_miss"), 0.0, 1e4, 0.0),
                "out": num(b.get("out"), 0.0, 1e4, 0.0)}

    # 工具能力声明（2026-11-10）：三态 true / false / ""（未设置 → 查本地能力表）。
    # 不支持原生 function calling 的模型（GLM 视觉模型、部分精简版）要显式关掉，
    # 否则请求硬带 tools → 端点 400 → 被当成"模型能力不足"反复重试。
    _st = str(raw.get("supports_tools") if raw.get("supports_tools") is not None
              else "").strip().lower()
    if _st in ("true", "1", "yes", "y", "支持", "on"):
        _st = True
    elif _st in ("false", "0", "no", "n", "不支持", "off"):
        _st = False
    else:
        _st = ""

    return {
        "id": mid or model,
        "label": str(raw.get("label") or mid or model)[:48],
        "model": model,
        "compat": compat,
        "base_url": str(raw.get("base_url") or "").strip()[:200],
        "api_key": str(raw.get("api_key") or "").strip(),
        "enabled": bool(raw.get("enabled", True)),
        "priority": num(raw.get("priority"), -9999, 9999.0, 10.0),
        "efforts": _norm_efforts(raw.get("efforts")) or ["none"],
        "supports_tools": _st,
        "max_output_tokens": num(raw.get("max_output_tokens"), 0, 393216, 0),
        "temperature": num(raw.get("temperature"), 0.0, 2.0, 0.2),
        "pricing": {"offpeak": price_block(pricing.get("offpeak")),
                    "peak": price_block(pricing.get("peak") or pricing.get("offpeak"))},
    }


def _norm_profiles(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        p = _norm_model_profile(item)
        if not p["model"]:
            continue
        if p["id"] in seen:
            continue
        seen.add(p["id"])
        out.append(p)
    out.sort(key=lambda p: (p["priority"], p["id"]))
    return out


# ---------------------------------------------------------------- 厂商（一等公民）

KEY_ROLES = ("writer", "reviewer1", "reviewer2")


def _norm_key_class(raw: dict, idx: int, provider_id: str = "") -> dict:
    """单个 key 类规范化（2026-11-18 多远端协作：厂商 → key → 模型 三层）。

    为什么要有 key 这一层（用户口径）：同一厂商可以有多把 key（不同账号/不同余额池），
    写作者与审核者**必须用不同的 key** —— 三方协商若走同一把 key，限流会把
    "审核者缺席"误判成"审核者反对"。key 类是审核者/写作者分离的最小单位。

    `role`（2026-11-18 晚，用户口径）：三个角色都可能是**同一个厂商的不同 key**，
    所以角色挂在 key 类上显式指派（writer / reviewer1 / reviewer2 / 空=不参与）。
    `current` 保留做兼容（= role 是 writer），读写两处必须同步，不许各说各话。
    """
    raw = raw or {}
    kid = str(raw.get("id") or ("k%d" % (idx + 1))).strip()[:32]
    models = []
    for m in _norm_profiles(raw.get("models")):
        m = dict(m)
        m["provider"] = provider_id
        models.append(m)
    role = str(raw.get("role") or "").strip().lower()
    if role not in KEY_ROLES:
        role = ""
    # 注意：**不在这里**把 current 派生成 writer —— 单个 key 看不到兄弟，
    # 旧配置的 current=True 会把用户刚指派给别的 key 的 writer 顶掉（实测踩过）。
    # 「无任何显式角色时按 current 派生」在 _norm_provider 里统一裁决。
    return {
        "id": kid,
        "label": str(raw.get("label") or kid)[:48],
        "api_key": str(raw.get("api_key") or "").strip(),
        "role": role,
        "current": bool(raw.get("current", False)),
        "models": models,
    }


def _norm_provider(raw: dict, pid: str = "") -> dict:
    """单个厂商档案规范化。非法值一律收敛，绝不让手滑把配置写坏。"""
    raw = raw or {}
    preset = PROVIDER_PRESETS.get(pid) or {}
    pid = str(raw.get("id") or pid or "").strip().lower()
    if pid not in PROVIDER_PRESETS:
        pid = "deepseek"
    label = str(raw.get("label") or preset.get("label") or pid)[:48]
    base_url = str(raw.get("base_url") or preset.get("base_url") or "")[:200].rstrip("/")
    key = str(raw.get("api_key") or "").strip()
    # 思考等级：该厂商**拥有**的档位（用户口径：只需填 apikey 与拥有的思考强度等级）
    effs = _norm_efforts(raw.get("efforts"))
    if not effs:
        effs = list(preset.get("efforts") or ["none"])
    # key 类层：显式 keys 优先；没有就**自动合成单个 key 类**
    # （旧配置/只填厂商级 key 的用户无感：一个 key 类 = 老行为）。
    raw_keys = raw.get("keys")
    keys = [_norm_key_class(k, i, pid)
            for i, k in enumerate(raw_keys) if isinstance(k, dict)] \
        if isinstance(raw_keys, list) else []
    # **只有"配置里根本没有 keys 字段"（老配置）才合成 k1**；显式给了空列表
    # = 用户把 key 全删了（2026-11-18 用户口径：一把也支持删，删了要真落地），
    # 这里再合成一把会让删除永远不生效。
    if not keys and raw_keys is None:
        keys = [_norm_key_class({"id": "k1", "label": "主 key", "api_key": key,
                                 "current": True,
                                 "models": raw.get("models")}, 0, pid)]
    # key 类内没填 key → 继承厂商级 key（填在厂商框上的那个）
    for k in keys:
        if not k["api_key"]:
            k["api_key"] = key
    # id 唯一 + **空位复用**（2026-11-18 晚：id 对用户隐藏，只在内部用；
    # 删掉一把 key 腾出的 k2 必须能被下一把自动复用，编号不许一路涨）。
    used_ids: set[str] = set()
    for k in keys:
        kid = str(k.get("id") or "")
        if not kid or kid in used_ids:
            i = 1
            while f"k{i}" in used_ids:
                i += 1
            k["id"] = f"k{i}"
            kid = k["id"]
        used_ids.add(kid)
    # 角色归一（2026-11-18 晚）：**显式角色优先**；全都没有角色时（旧配置）
    # 才把 current=True 的那把派生成 writer。然后保证 writer 唯一、reviewer 各至多一个，
    # current 与 role 永远同步（current ⇔ role=='writer'）。
    if keys and not any(k.get("role") for k in keys):
        for k in keys:
            if k.get("current"):
                k["role"] = "writer"
                break
    writers = [k for k in keys if k.get("role") == "writer"]
    if keys and not writers:
        keys[0]["role"] = "writer"
        writers = [keys[0]]
    seen_roles: set[str] = set()
    for k in keys:
        r = str(k.get("role") or "")
        if r == "writer" and k is not writers[0]:
            r = ""
        if r and r in seen_roles:
            r = ""
        k["role"] = r
        if r:
            seen_roles.add(r)
        k["current"] = (r == "writer")
    # 该厂商的模型档案：内嵌在厂商里（弹窗里直接编辑）。
    # 兼容旧口径：若 keys 里没有模型而厂商级 models 有，把厂商级 models 归到
    # 「正在使用」的 key 类（迁移不丢档案）。
    if keys and not any(k.get("models") for k in keys):
        for m in _norm_profiles(raw.get("models")):
            m = dict(m)
            m["provider"] = pid
            cur_k = next((k for k in keys if k.get("current")), keys[0])
            cur_k["models"].append(m)
    return {
        "id": pid,
        "label": label,
        "base_url": base_url,
        "api_key": key,
        "efforts": effs,
        "current": bool(raw.get("current", False)),
        "keys": keys,
        "models": [m for k in keys for m in (k.get("models") or [])],
    }


def providers() -> list[dict]:
    """三个厂商（deepseek / glm / openai）的当前配置。

    **厂商是真相源**；`models()` 由它派生。
    旧配置（只有 `remote.models[]`）在第一次读取时**自动迁移**：
      · compat=deepseek 的档案 → deepseek 厂商
      · compat=openai 且 base_url 含 bigmodel → glm 厂商
      · 其余 openai 兼容档案 → openai 厂商
    迁移不丢用户填过的 key / 参数 / 思考等级。
    """
    r = get_remote()
    raw = r.get("providers")
    if not isinstance(raw, list) or not raw:
        raw = _migrate_providers(r)
    out = [_norm_provider(p, str((p or {}).get("id") or "")) for p in raw]
    have = {p["id"] for p in out}
    for pid in PROVIDER_IDS:                 # 补齐缺的三家（用预设，不覆盖已有）
        if pid not in have:
            out.append(_norm_provider(dict(PROVIDER_PRESETS[pid]), pid))
    # 「正在使用」有且只有一个
    cur = [p for p in out if p.get("current")]
    if not cur:
        for p in out:
            if p["id"] == "deepseek":
                p["current"] = True
                break
    else:
        for p in out:
            p["current"] = (p["id"] == cur[0]["id"])
    order = {pid: i for i, pid in enumerate(PROVIDER_IDS)}
    out.sort(key=lambda p: order.get(p["id"], 99))
    return out


def _migrate_providers(r: dict) -> list[dict]:
    """旧 `remote.models[]` → 三家厂商（一次性迁移，保留用户填过的一切）。"""
    old = _norm_profiles((r or {}).get("models"))
    buckets: dict[str, list[dict]] = {pid: [] for pid in PROVIDER_IDS}
    for p in old:
        compat = str(p.get("compat") or "").strip().lower()
        base = str(p.get("base_url") or "").strip().lower()
        if compat == "deepseek":
            pid = "deepseek"
        elif "bigmodel" in base or "zhipu" in base or "z.ai" in base:
            pid = "glm"
        elif compat == "openai":
            pid = "openai"
        else:
            pid = "deepseek"
        p = dict(p)
        p["provider"] = pid
        buckets[pid].append(p)
    out = []
    for pid in PROVIDER_IDS:
        preset = PROVIDER_PRESETS[pid]
        prof_models = buckets[pid]
        # 预置模型 + 用户档案（用户档案在前，保留可编辑）
        merged = prof_models + [dict(m) for m in preset.get("models") or []]
        seen = set()
        uniq = []
        for m in merged:
            mid = str(m.get("id") or "")
            if mid and mid in seen:
                continue
            if mid:
                seen.add(mid)
            uniq.append(m)
        # 厂商**有预置模型**时，用户的老档案默认停用（但保留、可编辑、可再启用）：
        # 预置的是当前官方推荐 + 思考档齐全的模型，老档案往往是早期模型
        # （实测：老档案 glm-4-flash 只有 none 一档，而官方 glm-5.3 支持 low/high/max）。
        # 不停用的话 active_model 会挑中老档案 → 远端只剩一档思考。
        if preset.get("models"):
            for m in uniq[:len(prof_models)]:
                m["enabled"] = False
        out.append({
            "id": pid,
            "label": preset["label"],
            "base_url": preset["base_url"],
            "api_key": "",
            "efforts": list(preset.get("efforts") or ["none"]),
            "current": (pid == "deepseek"),
            "models": uniq,
        })
    # 老配置里的全局 key 归到「正在使用」的那家
    gkey = str((r or {}).get("api_key") or "").strip()
    if gkey:
        for p in out:
            if p.get("current"):
                p["api_key"] = gkey
    return out


def save_providers(provs: list[dict]) -> list[dict]:
    """整表写回厂商配置（界面的增/删/改最后都收敛到这里）。"""
    data = load()
    remote = data.setdefault("remote", {})
    clean = []
    for p in provs or []:
        pid = str((p or {}).get("id") or "").strip().lower()
        if pid not in PROVIDER_PRESETS:
            continue
        n = _norm_provider(p, pid)
        old_all = providers() if "providers" in remote else []
        # 厂商级 key 为空 → 保留原有（界面传空表示"不修改"）
        if not str((p or {}).get("api_key") or "").strip():
            old = next((x for x in old_all if x["id"] == pid), None)
            if old:
                n["api_key"] = old.get("api_key") or ""
        # key 类的 key 为空 → 同样保留原有（界面把 key 打码后回传，不能被清掉）
        old = next((x for x in old_all if x["id"] == pid), None)
        if old:
            old_keys = {k["id"]: k for k in (old.get("keys") or [])}
            for k in n.get("keys") or []:
                if not k.get("api_key") and k["id"] in old_keys:
                    k["api_key"] = old_keys[k["id"]].get("api_key") or ""
        clean.append(n)
    remote["providers"] = clean
    # 旧的平铺字段同步成「正在使用」那家的值（兼容老代码路径）
    cur = next((p for p in clean if p.get("current")), None)
    if cur:
        remote["provider"] = cur["id"]
        remote["compat"] = cur["id"]
        remote["base_url"] = cur.get("base_url") or ""
        remote["api_key"] = cur.get("api_key") or ""
        if cur.get("models"):
            first = cur["models"][0]
            remote["model"] = first.get("model") or ""
    save(data)
    return clean


def current_provider() -> dict:
    """正在使用的那个厂商。"""
    ps = providers()
    for p in ps:
        if p.get("current"):
            return p
    return ps[0] if ps else _norm_provider({}, "deepseek")


def provider_key(pid: str) -> str:
    """某个厂商的 key（优先「正在使用」的 key 类；模型级留空时继承它）。"""
    pid = str(pid or "").strip().lower()
    for p in providers():
        if p["id"] == pid:
            for k in p.get("keys") or []:
                if k.get("current") and str(k.get("api_key") or "").strip():
                    return str(k["api_key"])
            return str(p.get("api_key") or "").strip()
    return ""


def key_classes(pid: str) -> list[dict]:
    """某个厂商的全部 key 类（多远端协作按 key 分角色用）。"""
    for p in providers():
        if p["id"] == str(pid or "").strip().lower():
            return list(p.get("keys") or [])
    return []


def default_models_for(pid: str) -> list[dict]:
    """新 key 的**默认模型**（2026-11-18 晚用户口径：新增 key 不配模型，
    自动按厂商默认填好，用户点进去自己改）。

    来源顺序：厂商预置模型（PROVIDER_PRESETS）→ 该厂商现有任意 key 的模型。
    都没有就返回空（调用方允许空）。
    """
    pid = str(pid or "").strip().lower()
    preset = PROVIDER_PRESETS.get(pid) or {}
    ms = [dict(m) for m in (preset.get("models") or []) if isinstance(m, dict)]
    if not ms:
        for p in providers():
            if p["id"] != pid:
                continue
            for k in p.get("keys") or []:
                if k.get("models"):
                    ms = [dict(m) for m in k["models"]]
                    break
    out = []
    for m in ms:
        m = dict(m)
        m["provider"] = pid
        m["api_key"] = ""            # 模型级 key 留空 → 继承所属 key 类的 key
        out.append(m)
    return out


def models() -> list[dict]:
    """当前所有远端模型档案（按优先顺序）。**由厂商派生**。

    旧配置（只有 `remote.models[]`）会自动迁移到厂商层，迁移后这里返回的是
    迁移结果 —— 所以调用方不需要知道厂商层存在。
    """
    # 厂商层是真相源；providers() 内部已处理"旧配置 → 厂商"的迁移，
    # 所以这里**直接用 providers()**，不再看 settings 里有没有 providers 字段
    # （旧配置下那个字段不存在，但迁移结果才是真相）。
    out = []
    for p in providers():
        # key 类层（2026-11-18）：厂商 → key → 模型。key 的继承顺序：
        # **模型级 > key 类级 > 厂商级**，并盖上 key_id 章供角色解析用。
        pkey = str(p.get("api_key") or "").strip()
        for k in p.get("keys") or []:
            kkey = str(k.get("api_key") or "").strip() or pkey
            for m in k.get("models") or []:
                mm = dict(m)
                mm["provider"] = p["id"]
                mm["key_id"] = k["id"]
                mm["compat"] = p["id"] if p["id"] in ("deepseek", "glm") else "openai"
                if not mm.get("base_url"):
                    mm["base_url"] = p.get("base_url") or ""
                if not str(mm.get("api_key") or "").strip():
                    mm["api_key"] = kkey
                out.append(mm)
        # 兜底：某些老路径直接挂在厂商级 models 上（没归进 key 类）
        have = {id(m) for k in (p.get("keys") or []) for m in (k.get("models") or [])}
        for m in p.get("models") or []:
            if id(m) in have:
                continue
            mm = dict(m)
            mm["provider"] = p["id"]
            mm.setdefault("key_id", next((k["id"] for k in (p.get("keys") or [])
                                          if k.get("current")), "k1"))
            mm["compat"] = p["id"] if p["id"] in ("deepseek", "glm") else "openai"
            if not mm.get("base_url"):
                mm["base_url"] = p.get("base_url") or ""
            if not str(mm.get("api_key") or "").strip():
                mm["api_key"] = pkey or str(mm.get("api_key") or "")
            out.append(mm)
    if out:
        return out
    got = _norm_profiles(get_remote().get("models"))
    if got:
        return got
    return _norm_profiles(DEFAULTS["remote"]["models"])


def _bucket_models(profiles: list[dict]) -> dict[str, list[dict]]:
    """把模型档案按厂商分桶（口径与 `_migrate_providers` 完全一致）。

    抽成函数是为了让 `save_models()` 与一次性迁移共用同一份归类规则 ——
    两处规则漂移会出现"迁移时归 deepseek、保存后归 openai"这种静默错配。
    """
    buckets: dict[str, list[dict]] = {pid: [] for pid in PROVIDER_IDS}
    for p in profiles or []:
        compat = str((p or {}).get("compat") or "").strip().lower()
        base = str((p or {}).get("base_url") or "").strip().lower()
        if compat == "deepseek":
            pid = "deepseek"
        elif "bigmodel" in base or "zhipu" in base or "z.ai" in base:
            pid = "glm"
        elif compat == "openai":
            pid = "openai"
        else:
            pid = "deepseek"
        q = dict(p)
        q["provider"] = pid
        buckets[pid].append(q)
    return buckets


def save_models(profiles: list[dict]) -> list[dict]:
    """整表写回模型档案。返回规范化后的结果。

    2026-11-11 修真 bug：**厂商层 `providers` 才是 `models()` 的真相源**
    （11-09「厂商是一等公民」口径）。旧实现只写 `remote["models"]`，
    而 `models()` 由 `providers()` 派生 —— 写完立刻读不到：
      · 调用方以为"启用/停用模型"保存成功，实际阶梯没变；
      · 自检 `save_models()` 后断言 `ladder()` 变化 → 全批假红；
      · 更坏的是各脚本互相读脏配置，还会把测试档案写进用户真配置。
    这里同时写回厂商层，并保留旧字段（兼容旧配置回滚 / 老代码路径）。

    刻意**不套用** `_migrate_providers` 的"预置模型优先、停用老档案"逻辑：
    那是首次迁移的一次性策略；显式 `save_models` 是调用方的主权动作，
    它设的 enabled=True 不许被悄悄改回 False。
    """
    data = load()
    remote = data.setdefault("remote", {})
    clean = _norm_profiles(profiles)
    remote["models"] = clean               # 旧字段保留，向后兼容
    buckets = _bucket_models(clean)
    old = {str((p or {}).get("id") or ""): dict(p)
           for p in (remote.get("providers") or [])
           if isinstance(p, dict) and (p or {}).get("id")}
    out = []
    for pid in PROVIDER_IDS:
        p = dict(old.get(pid) or PROVIDER_PRESETS[pid])
        p["id"] = pid
        p["models"] = buckets.get(pid, [])
        out.append(p)
    remote["providers"] = out
    save(data)
    return clean


def active_model() -> dict | None:
    """运行期**唯一**使用的模型档案（单模型）。

    解析顺序（都可解释，绝不抛错）：
      1. `remote.active_model` 填的档案 id；
      2. 档案表里 `model` 字段等于平铺字段 `remote.model` 的那条
         （界面只填 base_url / api_key / model 三个框，所以这条是主路径）；
      3. 第一个 enabled 的档案；
      4. 都没有 → 用平铺字段临时合成一条（保持"只填链接参数就能用"）。
    返回 None 表示"一个可用的远端模型都没有"。
    """
    r = get_remote()
    # **只从「正在使用」的厂商里选**（2026-11-09 用户口径：厂商级单选"正在使用"，
    # 一次运行只用一个厂商；厂商内部再按 priority 选模型）。
    cur = current_provider()
    profs = [p for p in models() if str(p.get("provider") or "") == cur.get("id")] \
        or [p for p in models() if p.get("provider") == cur.get("id")] \
        or models()
    want_id = str(r.get("active_model") or "").strip()
    if want_id:
        for p in profs:
            # 显式 pin 的档案**不看 enabled**：enabled 只用于"自动解析"时挑选，
            # 用户明确点了"用这个"就该用它（否则会在界面上出现"点了没反应"）。
            if p["id"] == want_id:
                return p
    flat_model = str(r.get("model") or "").strip()
    if flat_model:
        for p in profs:
            if p.get("model") == flat_model and p.get("enabled", True):
                return p
    for p in profs:
        if p.get("enabled", True):
            return p
    if flat_model or str(cur.get("base_url") or "").strip() or str(cur.get("api_key") or "").strip():
        return _norm_model_profile({"id": flat_model or "flat", "label": flat_model or "平铺配置",
                                    "model": flat_model or "deepseek-flash",
                                    "compat": cur.get("id") or "deepseek",
                                    "base_url": cur.get("base_url") or "",
                                    "api_key": cur.get("api_key") or "",
                                    "enabled": True})
    return None


def ladder() -> list[dict]:
    """阶梯 = **当前这一个模型**的思考强度序列（none → low → high → max）。

    2026-11-05 用户口径：远端是"**单模型 + 自动切思考强度**"，不是"多模型 × 多强度"。
    所以其它档案虽然可以保存（各自带 base_url / api_key / model，方便随时切换），
    **不参与阶梯**：一次运行只用一个模型，只在这个模型内部换思考强度。

    `local_first=True` 时第 0 档仍是本地模型（它也是"一个模型"，同样不构成多模型）。
    """
    steps: list[dict] = []
    r = get_remote()
    default_compat = str(r.get("compat") or r.get("provider") or "deepseek").strip().lower()
    if r.get("local_first", True):
        # 第 0 档 = 本地模型（0 token 的手脚档，省 token）。本地连续失败到阈值才升到远端主力。
        steps.append({"profile_id": "local", "label": "本地模型", "model": "",
                      "effort": "-", "max_output_tokens": 0, "temperature": 0.2,
                      "compat": "deepseek", "base_url": "", "api_key": "", "pricing": None})
    p = active_model()
    if not p:
        return steps
    for eff in p.get("efforts") or ["none"]:
        steps.append({"profile_id": p["id"], "label": p["label"], "model": p["model"],
                      "effort": eff, "max_output_tokens": p["max_output_tokens"],
                      "temperature": p["temperature"],
                      "compat": p.get("compat") or default_compat,
                      "base_url": p["base_url"], "api_key": p["api_key"],
                      "pricing": p["pricing"]})
    return steps


def ladder_step(idx: int) -> dict | None:
    """取阶梯里第 idx 档（越界返回 None）。

    住在 settings_store 而不是调用方：阶梯是**设置层**的概念，
    规格合成（contractgen）与夜班编排（company）都只是它的使用者。
    （2026-11-05 从 contractgen 搬过来 —— 那边管契约，不该管阶梯。）
    """
    try:
        steps = ladder()
    except Exception:                                    # noqa: BLE001
        return None
    i = int(idx or 0)
    return steps[i] if 0 <= i < len(steps) else None


def single_effort() -> bool:
    """当前是否只有**一个思考强度** → 走「单档直通」分路。

    OpenAI 兼容端点（智谱 / 腾讯混元等）通常不认 reasoning_effort 的多档切换，
    档案只配一档时也一样。用户口径：这种情况直接走另一条分路，
    **不做思维强度提升和降阶**，因为只有一个强度可升可降。
    DS 默认多档阶梯仍走原来的升档 / 难度路由语义。
    """
    try:
        r = get_remote()
        p = active_model()
        if not p:
            return False
        effs = [str(e).strip().lower() for e in (p.get("efforts") or [])
                if str(e or "").strip()]
        compat = str(p.get("compat") or r.get("compat") or r.get("provider")
                     or "deepseek").strip().lower()
        return compat == "openai" or len(effs) <= 1
    except Exception:                                    # noqa: BLE001
        return False


def strongest_step_index() -> int:
    """阶梯里**最强的那一档**的下标：规格应该由它生成。

    为什么不是第 0 档：规格（接口/数据形状/错误语义/示例）决定后面每个模块的对错，
    是整条链上最高杠杆的一步；用最便宜的档位生成规格再固化，
    等于用最弱的算力做最重要的决定（旧行为，2026-11-05 改掉）。
    """
    try:
        steps = ladder()
    except Exception:                                    # noqa: BLE001
        return 0
    idx = [i for i, s in enumerate(steps) if str(s.get("model") or "").strip()]
    return idx[-1] if idx else 0


def update_remote(patch: dict) -> tuple[dict, list[str]]:
    """只写"真的变了"的字段；没变化就不落盘（打开界面/重复保存都不会改参数）。

    返回 ({新的 remote 配置}, [实际改动的字段名])。
    """
    data = load()
    remote = data.setdefault("remote", {})
    changed: list[str] = []
    for k, v in (patch or {}).items():
        if k in ("max_output_tokens", "max_prompt_chars"):
            try:
                v = int(v)
            except (TypeError, ValueError):
                continue
            v = max(256, min(4000000, v))
        elif k in ("local_first", "dev_remote"):
            v = bool(v)
        elif k == "escalate_after":
            try:
                v = max(1, min(9, int(v)))
            except (TypeError, ValueError):
                continue
        elif k == "models":
            v = _norm_profiles(v)
            if not v:
                continue
        elif k == "compat":
            # 只认三个值；非法/空一律收敛成「跟随默认」，绝不把不认识的协议写进配置
            v = str(v or "").strip().lower()
            if v not in ("deepseek", "openai"):
                v = ""
        elif k == "api_key":
            v = str(v or "").strip()
        elif k == "active_model":
            # 空 = 自动（按平铺 model / 第一个 enabled 解析）；填了就必须是存在的档案 id
            v = str(v or "").strip()
            if v and not any(p["id"] == v for p in models()):
                continue                              # 不存在的 id 直接忽略，别把配置写坏
        elif k == "pricing":
            v = _norm_pricing(v)
            if not v:
                continue
        elif k == "offpeak_window":
            v = _norm_window(v)
        elif k == "peak_window":
            v = _norm_peak_window(v)
        old = remote.get(k)
        if k == "configured_at":
            remote[k] = v
            continue
        if old == v:
            continue                              # 值没变 → 不动，也不标记
        remote[k] = v
        changed.append(k)
    if changed:
        save(data)
    return remote, changed


def any_configured_key() -> bool:
    """**当前正在使用的厂商**是否有任意一把 key（不只看平铺 remote.api_key）。

    2026-11-21 实测：只在厂商卡片里填 key 时，remote.available() 原来只查平铺
    api_key → 状态红点一直"未配置"，但实际调用走 provider key 是通的。
    只看 current provider：别的厂商有 key 不代表当前这条链路可用。
    """
    try:
        p = current_provider()
        if str(p.get("api_key") or "").strip():
            return True
        return any(str(k.get("api_key") or "").strip() for k in (p.get("keys") or []))
    except Exception:                                    # noqa: BLE001
        return False


def mask(key: str) -> str:
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}…{key[-4:]}"



def sanitize_profile(p: dict) -> dict:
    """模型档案脱敏：api_key 永远不放进对外视图（只给 bool + 掩码）。

    实测踩过：/v1/settings/remote 的响应里 
emote.models[i].api_key 带出来了 ——
    即使当时是空串，也意味着只要配过就会泄漏。这里是唯一出口，统一剥掉。
    """
    out = {k: v for k, v in (p or {}).items() if k != "api_key"}
    key = (p or {}).get("api_key") or ""
    out["key_configured"] = bool(key)
    out["key_masked"] = mask(key)
    return out


def public_remote(reveal: bool = False) -> dict:
    """给 UI/CLI 的视图：默认脱敏；reveal=True 时额外返回明文（仅本机回环接口可用）。"""
    r = get_remote()
    import os
    env_key = os.environ.get("DEEPSEEK_API_KEY", "")
    effective = r.get("api_key") or env_key
    out = {
        "enabled": bool(r.get("enabled", True)),
        "provider": r.get("provider", "deepseek"),
        "compat": r.get("compat") or r.get("provider") or "deepseek",
        "base_url": r.get("base_url", ""),
        "model": r.get("model", ""),
        "key_configured": bool(effective),
        "key_source": "settings" if r.get("api_key") else ("env" if env_key else ""),
        "key_masked": mask(effective),
        "max_output_tokens": int(r.get("max_output_tokens", 0) or 0),
        "max_prompt_chars": int(r.get("max_prompt_chars", 2000000)),
        "max_prompt_tokens": int(r.get("max_prompt_tokens", 950000)),
        "escalate_after": int(r.get("escalate_after", 2)),
        "active_model": str((active_model() or {}).get("id") or ""),
        "active_model_pinned": str(r.get("active_model") or ""),
        "local_first": bool(r.get("local_first", True)),
        "dev_remote": bool(r.get("dev_remote", False)),
        "configured_at": r.get("configured_at", ""),
        "key_plain": effective if reveal else "",
        "pricing": r.get("pricing") or {},
        "offpeak_window": r.get("offpeak_window") or {},
        "peak_window": r.get("peak_window") or {},
        # 模型档案 + 由它摊出来的阶梯（界面直接渲染这两块，不用自己算）
        # **api_key 一律剥掉**：实测接口曾把 models[i].api_key 带出来（当时是空串，
        # 但只要配过就会泄漏）。对外只给 key_configured + 掩码。
        "models": [sanitize_profile(p) for p in models()],
        # 厂商层（一等公民）：界面渲染"下拉 + 三个长方矩形 + 每家的模型列表"用这份。
        # api_key 一律只给掩码 + 是否已配置（绝不回明文）。
        "providers": [{
            "id": p["id"], "label": p["label"], "base_url": p["base_url"],
            "efforts": list(p.get("efforts") or []),
            "current": bool(p.get("current")),
            "key_configured": bool(p.get("api_key")),
            "key_masked": mask(p.get("api_key") or ""),
            "models": [sanitize_profile(m) for m in (p.get("models") or [])],
        } for p in providers()],
        "ladder": [{"profile_id": s["profile_id"], "model": s["model"],
                    "effort": s["effort"]} for s in ladder()],
    }
    return out
