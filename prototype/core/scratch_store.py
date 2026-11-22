"""引擎本地临时区归档（2026-11-19 用户口径）。

## 为什么要有这个模块

产物仓的 `.sixcoder/tmp` 是引擎探针的临时区。它有两个问题：

1. **不该留在用户仓库里**：实测堆到 2583 条 / 8.9MB，全是引擎自己的空目录壳与
   运行脚本 —— 用户 clone 或备份仓库时会一起带上；
2. **直接删也不合用户期望**：万一要排查"某一夜探针到底跑了什么"，删掉就没了。

用户定稿的两句话：
    "保存到引擎本地作为备用，不是有删除仓库记录功能，如果删除就顺便删除备份的临时目录"

所以设计成：**收尾时把有内容的临时项搬到引擎本地归档（备用），空壳直接删**；
**删除任务/仓库记录时连带删掉该仓库的归档**（不留孤儿）。

## 生命周期

    .sixcoder/tmp（产物仓，本轮产生）
        │  收尾：有内容 → move 到归档；空壳 → 删
        ▼
    prototype/data/scratch/<仓库指纹>/<时间戳>/（引擎本地，备用）
        │  超过 keep_rounds 次 → 删最老的（本地也不能无限涨）
        ▼
    （可选）删除任务/仓库记录 → purge_repo 整个指纹目录删除

## 硬约束

* 只处理**引擎自己的命名空间**（`probe-*` / `run-*` / `work*`），用户的文件一律不碰；
* 归档用 `shutil.move`（产物仓与引擎常在不同盘，`os.rename` 会抛 EXDEV）；
* 任何失败都只记录、不抛 —— 清理绝不改夜班退出码。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

# 引擎自己的临时目录命名空间（与 core/probe.py 的 _SCRATCH_PREFIXES 保持一致）
SCRATCH_PREFIXES = ("probe-", "run-", "work")

# 归档根：prototype/data/scratch（已 gitignore，不随仓库分发）
SCRATCH_ROOT = Path(__file__).resolve().parent.parent / "data" / "scratch"

# 每个仓库最多保留几轮归档（超出删最老的）—— 与"备用可查"和"本地不涨"的折中
DEFAULT_KEEP_ROUNDS = 5


def repo_key(repo) -> str:
    """仓库稳定指纹：路径归一后取 sha1 前 16 位。

    为什么不用仓库名：不同目录可能同名（`.../a/project` 与 `.../b/project`），
    归档会串到一起。路径归一（小写、正斜杠、去尾斜杠）保证同一仓库永远同一个 key。

    归一的三个坑（都实测过）：
      · Windows 的 `\\\\?\\` 长路径前缀 → 必须剥掉，否则同一仓两个指纹；
      · 反斜杠与正斜杠混用 → 统一成正斜杠；
      · 多余的分隔符（`F://workspace`）→ 折叠成单斜杠，否则 `f://a` ≠ `f:/a`。

    注：结果与引擎既有的"项目指纹"口径一致（同一仓库得到同一个 16 位值）。
    """
    p = str(repo or "").strip()
    if p.startswith("\\\\?\\"):
        p = p[4:]
    p = p.replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return hashlib.sha1(p.rstrip("/").lower().encode("utf-8")).hexdigest()[:16]


def archive_root(repo) -> Path:
    """该仓库在引擎本地的归档根目录（可能不存在）。"""
    return SCRATCH_ROOT / repo_key(repo)


def _tree_has_content(p: Path) -> bool:
    """目录里是否真的有东西（文件或子目录）。

    空目录壳是历史主要残留（实测 2200+ 个），搬走毫无意义 —— 直接删。
    只有**有内容**的才值得占本地空间留作备用。
    """
    try:
        with __import__("os").scandir(p) as it:
            return any(True for _ in it)
    except OSError:
        return False


def archive_scratch(ws, keep_rounds: int = DEFAULT_KEEP_ROUNDS,
                    stamp: str = "", max_items: int = 200,
                    deadline_s: float = 45.0) -> dict:
    """把产物仓 `.sixcoder/tmp` 里**有内容**的引擎临时项移到引擎本地归档。

    ## 为什么**不在这里删空壳**（2026-11-19 实测踩坑）

    第一版把所有空目录壳都 `shutil.rmtree` 掉，结果 2262 条跑到一半就被 SIGTERM：
    宿主环境把 `shutil.rmtree` 包装成了"每次删除前起 node 子进程做批量删除保护检查"，
    **逐个删 = 每个目录一次 node 启动**（这是 `probe.gc_scratch` 注释里早就写过的血泪）。
    正确手法是 rename 归集后整棵 rmtree（每批只触发一次检查）——那正是
    `gc_scratch` 干的事，所以空壳**交给它**，这里只负责搬有内容的东西（数量极少：
    实测 893 个目录里只有 2 个非空）。

    ## 时限

    `deadline_s`（默认 45s）+ `max_items`（默认 200）双保险：收尾清理绝不能
    让用户等，超时就停手，剩下的下轮再搬。

    返回 {"moved", "moved_bytes", "left_empty", "kept", "skipped_by_limit",
          "errors", "dest"}
    """
    out = {"moved": 0, "moved_bytes": 0, "left_empty": 0, "kept": 0,
           "skipped_by_limit": 0, "errors": [], "dest": ""}
    tmp = Path(ws) / ".sixcoder" / "tmp"
    if not tmp.is_dir():
        return out

    ts = stamp or time.strftime("%Y%m%d-%H%M%S")
    dest = archive_root(ws) / ts
    t_end = time.time() + max(1.0, float(deadline_s))

    for child in sorted(tmp.iterdir(), key=lambda p: p.name):
        name = child.name
        # 引擎自己生成的东西（全部由引擎每轮重写或诊断产生，用户不会往这里放）：
        #   · 目录：探针沙箱（probe-*/run-*/work*）
        #   · `run_<pid>_<n>.py`：旧实现留下的运行脚本
        #   · `_xxx_run.py`：flow/props 每次运行**重新生成**的 runner（实测重写而非复用）
        #   · `*.broken_*`：整文件重写失败时的备份（排查证据，实测没有任何代码读它）
        #   · `.coverage`：coverage.py 产物
        is_ns = (
            name.startswith(SCRATCH_PREFIXES)
            or (name.startswith("run_") and name.endswith(".py"))
            or (name.startswith("_") and name.endswith("_run.py"))
            or ".broken_" in name
            or "_broken_" in name        # 旧命名（topics_broken_1955.py）同样是坏文件备份
            or name == ".coverage"
        )
        if not is_ns:
            out["kept"] += 1                      # 不认识的：一律不碰
            continue

        if time.time() > t_end or out["moved"] >= max_items:
            out["skipped_by_limit"] += 1
            continue

        try:
            if child.is_dir() and not _tree_has_content(child):
                out["left_empty"] += 1            # 空壳留给 gc_scratch（rename 批处理）
                continue
            size = _dir_size(child) if child.is_dir() else child.stat().st_size
            if size <= 0 and child.is_file():
                out["left_empty"] += 1
                continue
            dest.mkdir(parents=True, exist_ok=True)
            shutil.move(str(child), str(dest / name))   # 跨盘安全（copy+del）
            out["moved"] += 1
            out["moved_bytes"] += size
        except BaseException as exc:              # noqa: BLE001
            out["errors"].append("%s: %s" % (name, type(exc).__name__))
            out["kept"] += 1

    if out["moved"]:
        out["dest"] = str(dest)
        _prune_rounds(ws, keep_rounds)
        try:
            (dest / ".manifest.json").write_text(json.dumps({
                "repo": str(ws), "at": ts,
                "moved": out["moved"], "bytes": out["moved_bytes"],
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    elif dest.is_dir() and not any(dest.iterdir()):
        shutil.rmtree(dest, ignore_errors=True)
    return out


def _dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _prune_rounds(ws, keep: int) -> int:
    """每个仓库只保留最近 keep 轮归档（目录名是时间戳，字典序即时间序）。"""
    root = archive_root(ws)
    if keep <= 0 or not root.is_dir():
        return 0
    try:
        rounds = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return 0
    drop = rounds[:-keep] if len(rounds) > keep else []
    for p in drop:
        shutil.rmtree(p, ignore_errors=True)
    return len(drop)


def purge_repo(repo) -> dict:
    """删除该仓库在引擎本地的全部归档（删除任务/仓库记录时调用）。

    为什么绑在"删记录"上：归档是**为这个仓库**留的备用证据。记录都删了，
    这些目录就成了没人认得、也没人会看的孤儿 —— 顺手清掉，不留暗账。
    """
    root = archive_root(repo)
    out = {"purged": False, "freed_bytes": 0, "path": str(root)}
    if not root.is_dir():
        return out
    size = _dir_size(root)
    shutil.rmtree(root, ignore_errors=True)
    out["purged"] = not root.exists()
    out["freed_bytes"] = size if out["purged"] else 0
    return out


def stats(repo) -> dict:
    """该仓库归档现状（给 UI/诊断看：几轮、多大）。"""
    root = archive_root(repo)
    if not root.is_dir():
        return {"rounds": 0, "bytes": 0, "path": str(root)}
    rounds = [p for p in root.iterdir() if p.is_dir()]
    return {"rounds": len(rounds), "bytes": _dir_size(root), "path": str(root)}
