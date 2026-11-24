"""全自主编排：一句话目标 → 蓝图 → 切片 → 逐片生成 → 覆盖观测 → 重规划 → 报告。

这是把外循环搬进产品的那一层。以前"下一轮做什么"是人手写需求文件决定的
（.cache/run_rounds.py 里 16 个文件），现在由 蓝图 + 覆盖状态机 决定：

    while 还有待办能力:
        覆盖 := 观测整个项目（真执行探针）
        if 全绿: 收工
        if 预算/时间到: 收工并报告缺口
        切片 := 拓扑序的第一片（已完成的、降级的都不进来）
        契约 := 这一片能力的考卷（远端最强档产出，按切片缓存）
        跑一轮夜班（内循环原样复用：体检→派工→开发→机械验收）
        重新观测 → 覆盖停滞 or 某能力反复失败 → 重规划（降级/补能力/重连依赖）

三条设计要点：
1. **循环条件是实测覆盖，不是轮数**。计划切错了，探针还是红的，下一轮自动补，
   不需要人发现 —— 这是"计划错误能被自我纠正"的唯一机制。
2. **内循环不重写**。每片都调用 `company.night(..., contract_file=...)`，
   用已经被 78 个自检钉住的那条流水线；这一层只负责"决定做什么、做完了没"。
3. **预算换范围**。单切片最多吃掉总预算的 15%，一处困难不许拖死整个项目；
   收集不到的预算就降级并写进报告，绝不静默缩水。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path

from . import architect
from . import blueprint
from . import budget_estimate as _be
from . import coverage as covmod
from . import frontier
from . import replan
from . import selfcheck_loop
from . import slicer

AUTONOMY_REPORT = "AUTONOMY.md"
SLICE_DIR = ".sixcoder/slices"
SLICE_BUDGET_SHARE = 0.15      # 单切片最多占**总预算**的比例（=一处困难不许拖死全局）
SLICE_BUDGET_FLOOR = 0.10      # 单切片最低额度：再低连一次契约合成都付不起
SLICE_RETRY_MARGIN = 1.6       # 按函数数估切片预算时的重试余量（实测一轮常要重写 1~2 次）
# 缩片地板：切片**永远不少于这么多函数**（2026-11-09 用户口径：完全相信远端）。
# 旧口径下限是 1 → 连续几片不过就退化成"一次一个函数"，碎片化到没有业务纵切，
# 永远做不出有难度的东西 —— 那是"用规模惩罚远端"，不是"调整"。
# 6 是个能容纳一次业务纵切的下限（如"创建→运行→归因"三个端点分布在 4 个模块）。
SLICE_SHRINK_FLOOR = 6
CNY_PER_FUNC = blueprint.CNY_PER_FUNC   # 每函数花费口径只留一处定义（blueprint 里那份）
DEFAULT_SLICE_HOURS = 0.5      # 历史常量（已无人使用，保留仅防外部引用报错）
# 2026-11-10（预算机制重设计）：片级时间预算（--hours / slice_hours）与片级/用户金额上限
# 已全部移除。唯一停机线 = 系统总预算 × 1.05（写在 .sixcoder/budget.json，用户不可改）。
# 切片大小只由 dosing 决定（见 night_auto 内循环），不再有任何片级时限或封顶。
MAX_REPLANS = 6                # 一次运行最多重规划 6 次（再多说明计划本身站不住）
MAX_CONTRACT_FAIL = 4          # 契约合成连续失败到几次就如实停（系统侧问题，不降级能力）
MAX_ITERATIONS = 400           # 循环硬闸：无论如何不许转不出来
SELFCHECK_STEPS = 3            # 片内自验最多几步（每步 = 一次远端 + 至多 2 条命令）


# ---------------------------------------------------------------- 小工具

def _global_spend_marker() -> float:
    """**全量历史账本**的当前读数 —— **只许做差**（after − before），不许当"本项目花费"。

    为什么必须把名字写清（2026-11-10 18:05 事故）：
      原名叫 `_usage_cny`，读的是 `usage_summary("")` = 所有项目 + 所有实验 +
      所有续跑的总和。停机线把它当"本项目已花"，于是项目只花 ¥31.01 却被判
      "超额 5%" 秒停，一轮活都没干。

      做差是安全的：`after - before` 会自动抵消掉历史基线，与"是哪套账"无关。
      **要绝对值的"本项目花了多少"，走 `budget_estimate.project_spent_cny(ws)`**
      （读 `.sixcoder/night_spent.json`，与交付报告同源）。
    """
    try:
        from . import remote
        return float((remote.usage_summary("") or {}).get("cost_cny") or 0.0)
    except Exception:                                    # noqa: BLE001
        return 0.0


# ------------------------------------------------------- 跨中断的实际花费累计
# `spent` 按 goal 落盘累计：夜班被中断后同命令续跑不会归零，实际花费能跨多次续跑累加
# （用于交付报告报总消费，以及给 UI 展示"系统预算剩余"）。这不是上限——上限机制已移除。
# 账按 goal 隔离：同一需求续跑累计；换了新需求才是新的一届夜班。
SPENT_FILE = ".sixcoder/night_spent.json"


def _spent_key(goal: str) -> str:
    return hashlib.sha1(str(goal or "").encode("utf-8")).hexdigest()[:16]


def _load_spent(ws: Path, goal: str) -> float:
    try:
        d = json.loads((Path(ws) / SPENT_FILE).read_text(encoding="utf-8"))
        if d.get("goal") == _spent_key(goal):
            return max(0.0, float(d.get("total_cny") or 0.0))
    except Exception:                                    # noqa: BLE001
        pass
    return 0.0


def _save_spent(ws: Path, goal: str, total: float) -> None:
    try:
        p = Path(ws) / SPENT_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"goal": _spent_key(goal),
                                 "total_cny": round(float(total), 4),
                                 "updated_at": time.time()}, ensure_ascii=False),
                     encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        pass


def sync_spent_from_ledger(ws: Path, goal: str) -> float:
    """把项目账本真值同步回 night_spent.json 镜像（2026-11-19）。

    背景：night_spent.json 按 goal 隔离，而夜班每轮拼契约文本会改变 goal，
    导致 key 漂移、这个"报告总消费"的镜像卡在旧值（实测卡在 7 天前的 ¥50.92，
    真值已 ¥123+）。停机不读它（读 project_ledger），但它撒谎会误导数出报告、
    patrol 监控和人工排查。

    做法：夜班开局时读 project_ledger.total()（明细派生的真值），用**当前 goal 的
    key** 写回镜像。只升不降——镜像低于真值时抬上来；真值取不到（None）时不动。
    返回写回后的镜像值。异常只吞不留痕于账本之外，绝不影响夜班主流程。

    不碰 goal 隔离语义（check_night_budget 钉住）：换需求仍是新一届，只是这一届
    开局先继承仓库累计的真值——与 "换需求≠换项目、额度按仓库" 的定稿一致。
    """
    try:
        from . import project_ledger as _pl
        true_val = _pl.total(ws)
    except Exception:                                    # noqa: BLE001
        true_val = None
    if true_val is None:
        return _load_spent(ws, goal)
    mirror = _load_spent(ws, goal)
    if float(true_val) > mirror + 1e-9:                  # 只升不降
        _save_spent(ws, goal, float(true_val))
        return float(true_val)
    return mirror


def _contract_funcs(text: str) -> int:
    from . import contractgen
    mods = contractgen.contract_mod.parse_requirement(text or "")
    return sum(len(getattr(c, "funcs", None) or []) for c in (mods or {}).values())


def slice_budget(total: float, left: float, n_slices: int, funcs: int = 0) -> float:
    """这一片给多少钱：按**要写的函数数**估，再受总额比例与余额夹住。

    为什么要按函数数（实测 2026-11-06，第 3 次真机）：15 个函数的一片只拿到 ¥0.10 的
    切片额度，而一轮真实写作要 ¥0.3~0.5 —— 结果"预算用尽"时覆盖 0/11，钱全烧在
    一片做不完的活上。**预算必须跟工作量对齐**，否则切片永远做不完。
    同时保留"总额比例"上限：一处困难不许吃掉全部预算。
    """
    if not total:
        return 0.0
    share = total * SLICE_BUDGET_SHARE
    fair = left / max(1, n_slices)
    need = 0.0
    if funcs:
        per = CNY_PER_FUNC
        try:
            from . import calibration as _cal
            per = float((_cal.current() or {}).get("cost_per_func_cny") or 0) or CNY_PER_FUNC
        except Exception:                                # noqa: BLE001
            per = CNY_PER_FUNC
        need = per * max(1, int(funcs)) * SLICE_RETRY_MARGIN
    cap = max(SLICE_BUDGET_FLOOR, min(share, max(fair, SLICE_BUDGET_FLOOR)), need)
    return round(max(0.0, min(cap, left)), 4)


# ---------------------------------------------------------------- 切片契约

ENTRY_FUNC_LINE = (
    "   - main(argv: list[str]) -> int：命令行入口；argv 不含脚本名，必须原样支持验收示例里的"
    "参数名（不许改成位置参数）；入口脚本必须以 "
    "`if __name__ == \"__main__\": sys.exit(main(sys.argv[1:]))` 收尾，"
    "否则真实命令行验收不成立（少这一行，函数级判据全绿而真验收全红）")


def _entry_block(lines: list[str], entry: str) -> tuple[int, int]:
    """返回入口模块的行区间 [start, end)（找不到返回 (-1, -1)）。"""
    import re as _re
    head_re = _re.compile(r"^\s*\d+\)\s*([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))")
    start = -1
    for i, raw in enumerate(lines):
        m = head_re.match(raw)
        if m and m.group(1).split("/")[-1].split("\\")[-1] == entry:
            start = i
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if head_re.match(lines[j]):
            end = j
            break
    return start, end


def ensure_entry_binding(text: str, entry: str, ex: str, fail_note: str = "",
                         prep: list[str] | None = None,
                         post: list[str] | None = None,
                         groups: list[list[str]] | None = None) -> str:
    """把**入口函数签名 + 那条调用示例 +（有失败时）失败证据**一起写进契约（机械插入）。

    三样都要，少一样就白干（实测 2026-11-05，踩了三次）：
      ① 只插示例 → props 按"模块里有没有这个符号"过滤，模块里没有 `main` → 示例被静默丢掉
         → 内循环判"没有可做的工单"，30 秒花 ¥0.06 收工，真验收永远红。
      ② 插了签名+示例，但入口脚本没写 `if __name__ == "__main__": sys.exit(main(sys.argv[1:]))`
         → 函数级全绿、真实命令行仍红。
      ③ 失败证据只写进"简报"没用：契约按片 id 缓存，重试时简报根本不参与合成
         → 作者看到的还是同一份说明，于是第二次跑的错和第一次一模一样。
         所以证据必须落到**函数的规范字段**里（author 的 prompt 里只有 describe()，
         而 describe() 只带函数级规范，模块 purpose 进不去）。
    """
    import re as _re
    if not text or not entry:
        return text
    text = align_entry_module(text, entry)
    fn_re = _re.compile(r"^\s*-\s+([A-Za-z_]\w*)\s*\(")
    lines = text.splitlines()
    start, end = _entry_block(lines, entry)
    if start < 0:
        return ensure_example(text, entry, ex, prep, post, groups)
    main_at = -1
    for j in range(start + 1, end):
        m = fn_re.match(lines[j])
        if m and m.group(1) == "main":
            main_at = j
            break
    if main_at < 0:
        lines.insert(start + 1, ENTRY_FUNC_LINE)
        main_at = start + 1
    note = (fail_note or "").strip().replace("\n", " ")[:200]
    if note and "上一轮实测失败" not in lines[main_at]:
        lines[main_at] = lines[main_at].rstrip("；; ") + f"；【上一轮实测失败】{note}"
    return ensure_example("\n".join(lines), entry, ex, prep, post, groups)


CORE_FN = "cli_run"
CORE_MODULE = "cli_core.py"
CORE_LINE = ("   - cli_run(argv: list[str]) -> tuple[int, str]：命令行核心；返回 (退出码, 要写到 stdout 的"
             "文本)。argv 与真实命令逐字一致（如 ['--file','d.csv','--group','team','--value','amount']，"
             "含 --top）；**不许打印**，只把文本返回；参数名不许改；入口脚本与 argparse 由引擎生成，"
             "你不需要写 main")

ENTRY_WRAPPER = '''\
"""命令行入口（**由引擎生成**，不要改）：真正的逻辑在 cli_run(argv) 里。

它返回 (退出码, 要写到 stdout 的文本)；本文件只负责找到它、打印文本、按退出码结束。
这样"命令行接线"就不再消耗远端调用，也不会因为一次写错整文件就被回滚成空桩。
"""
import importlib
import sys
from pathlib import Path

# 内置 Python 是 `._pth` 隔离模式：**连脚本所在目录都不会自动进 sys.path**，
# 不显式注入的话，下面 `importlib.import_module` 永远找不到同目录的 cli_core
# （2026-11-23 实测：表现为"仓库里没有 cli_run"→ 退出码 70 → 能力反复红 → 降级）。
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _find():
    here = Path(__file__).resolve().parent
    me = Path(__file__).stem
    for p in sorted(here.glob("*.py")):
        if p.stem in (me, "__init__") or p.stem.startswith("test_"):
            continue
        try:
            mod = importlib.import_module(p.stem)
        except Exception:
            continue
        fn = getattr(mod, "cli_run", None)
        if callable(fn):
            return fn
    return None


def main(argv: list[str]) -> int:
    fn = _find()
    if fn is None:
        print("引擎错误：仓库里没有 cli_run(argv)")
        return 70
    code, text = fn(list(argv))
    out = "" if text is None else str(text)
    if out:
        print(out, end="" if out.endswith("\\n") else "\\n")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

WRAPPER_MARK = "由引擎生成"


def core_groups(probes) -> list[list[str]]:
    """把 CLI 探针翻译成 `cli_run(argv)` 的断言组（**含造夹具**，确定性、不需要子进程）。

    每条探针一组：先把它自己的素材写成文件（按探针编号改名，防多条探针互相覆盖），
    再断言 `cli_run(argv)[0]`（退出码）与 `(期望文本 in cli_run(argv)[1])`（输出内容）。
    这样"命令行验收"就落成了纯函数判据 —— 引擎负责接线，远端只写逻辑。
    """
    if isinstance(probes, dict):
        probes = [probes]
    import json as _json
    out: list[list[str]] = []
    for i, p in enumerate(x for x in (probes or [])
                         if isinstance(x, dict) and str(x.get("kind") or "") == "cli"):
        files = p.get("files") or {}
        rename = {str(n): f"_p{i}_{str(n)}" for n in files}
        argv = [rename.get(str(a), a) for a in (p.get("args") or [])]
        grp = [f"open({rename[str(n)]!r}, 'w', encoding='utf-8').write({_json.dumps(str(c))})"
               for n, c in files.items()]
        call = f"cli_run({argv!r})"
        code = p.get("expect_exit")
        if code is not None:
            grp.append(f"{call}[0] == {int(code)}")
        has = p.get("stdout_has")
        for n in ([has] if isinstance(has, str) else list(has or [])):
            grp.append(f"({str(n)!r} in {call}[1]) == True")
        if len(grp) > len(files):        # 有断言才收（只造夹具不判等于没判）
            out.append(grp)
    return out


def core_examples(probes) -> list[str]:
    """兼容旧调用：摊平成一行一条的示例列表。"""
    return [ln for g in core_groups(probes) for ln in g]


def _drop_block(lines: list[str], path: str) -> list[str]:
    """从契约文本里整块删掉某个模块（模块标题行到下一个标题）。"""
    import re as _re
    head_re = _re.compile(r"^\s*\d+\)\s*([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))")
    want = str(path or "").replace("\\", "/").split("/")[-1]
    out: list[str] = []
    dropping = False
    for ln in lines:
        m = head_re.match(ln)
        if m:
            dropping = m.group(1).replace("\\", "/").split("/")[-1] == want
        if not dropping:
            out.append(ln)
    return out


def _rename_block(lines: list[str], path: str, new_name: str) -> list[str]:
    """把某个模块块的标题换成 new_name。"""
    import re as _re
    head_re = _re.compile(r"^(\s*\d+\)\s*)([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))(.*)$")
    want = str(path or "").replace("\\", "/").split("/")[-1]
    out: list[str] = []
    for ln in lines:
        m = head_re.match(ln)
        if m and m.group(2).replace("\\", "/").split("/")[-1] == want:
            ln = f"{m.group(1)}{new_name}{m.group(3)}"
        out.append(ln)
    return out


def _has_funcs(lines: list[str]) -> bool:
    import re as _re
    return any(_re.match(r"^\s*-\s+[A-Za-z_]\w*\s*\(", ln) for ln in lines)


def ensure_core_binding(text: str, entry: str, probes, fail_note: str = "") -> str:
    """契约里放 `cli_run(argv) -> (code, text)` + 由探针机械生成的断言。

    为什么不放 `main(argv)`（实测 run 11/12/13 三次）：远端写 argparse 接线 + 精确输出
    这件事反复失败，而失败会触发回归保护把整个文件回滚成 `raise NotImplementedError`
    —— CLI 能力因此成批降级。把入口降级成**确定性样板 + 一个纯函数**之后，
    远端只需要写"给 argv 返回文本"的纯逻辑，引擎负责接线与退出码。
    """
    import re as _re
    if not text or not entry:
        return text
    lines = text.splitlines()
    head_re = _re.compile(r"^\s*\d+\)\s*([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))")
    # **整个入口模块块删掉**：入口由引擎独占（整文件覆盖）。远端写在这里的任何东西
    # 都会消失 —— 实测 run 18：`cli_run` 写在入口里 → wrapper 覆盖后"退出码 70"；
    # 实测 mid1：`parse_args` 写在 jlq.py 里 → `AttributeError: module ... parse_args`。
    # cli_run 统一放到 `cli_core.py`（远端只在这个文件里写 CLI 逻辑）。
    lines = _drop_block(lines, entry)
    if not _has_funcs(lines):
        # 契约里只有入口一个模块时，"删掉"会让契约变空（解析出 0 个函数 → 整片失败）。
        # 这种情况改成**改名**：入口块直接变成 cli_core.py，内容不丢。
        lines = _rename_block(text.splitlines(), entry, CORE_MODULE)
    # **入口脚本与 main 都归引擎**：契约里任何地方出现 `main(...)` 都要删掉。
    # 实测（mid4）：cli_core 块里混进了 main → 远端写了 main → 那是引擎的活 → 判红被回滚
    # → cli_core.py 整份留成 `raise NotImplementedError` → 16 条 CLI 能力一起死。
    lines = [ln for ln in lines
             if not _re.match(r"^\s*-\s+main\s*\(", ln)
             and not (_re.search(r"\bmain\s*\(", ln) and "==" in ln)]
    note = (fail_note or "").strip().replace("\n", " ")[:200]
    core_line = CORE_LINE + (f"；【上一轮实测失败】{note}" if note else "")
    # 找 cli_core.py 那一块；没有就在末尾新建一块，把 cli_run 放进去
    heads = [(i, m.group(1)) for i, ln in enumerate(lines) for m in [head_re.match(ln)] if m]
    tgt = next((i for i, p in heads
                if p.replace("\\", "/").split("/")[-1] == CORE_MODULE), -1)
    if tgt >= 0:
        block_end = next((j for j, _p in heads if j > tgt), len(lines))
        core_at = next((i for i in range(tgt + 1, block_end)
                        if _re.match(r"^\s*-\s*cli_run\s*\(", lines[i])), -1)
        if core_at < 0:
            lines.insert(tgt + 1, core_line)
        else:
            # cli_run 的签名是「引擎 wrapper ↔ 纯函数」之间的硬约定：必须返回
            # (退出码, 文本)。远端常写成 -> int，而 core_groups() 生成的判据用
            # cli_run(...)[1]，两者矛盾 → 判据永远红。实测 dev1106a：4 轮自愈
            # 全在重写 cli_core.py，桩 csvio.py 一直没人管。签名不对就强制换回。
            cur = lines[core_at]
            if not _re.search(r"->\s*tuple\s*\[\s*int\s*,\s*str\s*\]", cur):
                cur = CORE_LINE
            if note and "上一轮实测失败" not in cur:
                cur = cur.rstrip("；; ") + f"；【上一轮实测失败】{note}"
            lines[core_at] = cur
    else:
        idx = max([len(heads) and heads[-1][1] or ""] and [0] or [0])
        num = (len(heads) + 1)
        lines += ["", f"{num}) {CORE_MODULE}：命令行核心（入口脚本由引擎生成，会调用这里的 cli_run）",
                  core_line, "", "示例（必须满足）：", "（无）", ""]
    exs = core_groups(probes)
    if exs:
        # 引擎完全接管 cli_run 的判据。远端自己写的旧示例（典型是
        # `cli_run(...) == 0`）与 tuple 签名矛盾，留着就是两套判据并存、永远判红
        # （实测 dev1106b：order_failed 里同时出现 `== 0` 与 `[1]` 断言）。
        # 只删示例行，保留以 "- cli_run(" 开头的签名行。
        lines = [ln for ln in lines
                 if not _re.search(r"\bcli_run\s*\(", ln)
                 or _re.match(r"^\s*-\s*cli_run\s*\(", ln)]
    text = "\n".join(lines)
    if exs:
        text = ensure_example(text, CORE_MODULE, "", groups=exs)
    return text


def write_entry_wrapper(ws, entry: str, force: bool = False) -> dict:
    """把入口脚本写成**引擎生成的接线**（已生成过就不动，避免覆盖手改）。"""
    p = Path(ws) / str(entry or "")
    if not entry or p.suffix != ".py":
        return {"ok": False, "wrote": False, "why": "没有可写的入口文件"}
    try:
        if p.exists() and not force:
            cur = p.read_text(encoding="utf-8-sig", errors="replace")
            if WRAPPER_MARK in cur and "cli_run" in cur:
                return {"ok": True, "wrote": False, "why": "已是引擎入口"}
        p.write_text(ENTRY_WRAPPER, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "wrote": False, "why": f"写不了：{exc}"}
    return {"ok": True, "wrote": True, "why": "入口接线由引擎生成（cli_run → 打印 → 退出码）"}


WEB_ENTRY_MARK = "由引擎生成（Web 入口）"


def _stack(ws=None):
    """当前生效的栈适配器（§20.7 D 档：栈适配层）。

    没有 `.sixcoder/stack.json` → fastapi → **与本次改动前逐字一致**。
    选栈是配置，不是猜：猜错栈 = 整轮白烧钱（§20.1 证据 1：
    需求写 Django、产物是 FastAPI —— 引擎当时没有"栈"这个概念）。
    """
    from . import stack as _st
    return _st.current(ws)


WEB_ENTRY = '''"""ASGI 应用入口（**由引擎生成，不要改**）。

为什么不让你写这个文件：路由是分模块长的（app/auth.py、app/tickets.py …），
每加一片能力就重写一次入口 → 上一片的路由被抹掉、"覆盖数"来回摆（CLI 入口踩过这个坑）。
所以入口只做一件事：把各模块里的 `router` 全部 include 进来。
你只需要在每个功能模块里写 `router = APIRouter()` 并挂路由。
"""
from __future__ import annotations

import importlib
import importlib.util

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="SixCoder App")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

# 静态资源（§20.4 产物文件 + §20.5）：app/static 存在就挂 /static ——
# 页面里引用的 css/js 要真能被服务到，"文件存在"才算数。没有该目录时零影响。
try:
    from fastapi.staticfiles import StaticFiles as _StaticFiles
    from pathlib import Path as _StaticPath
    _static_dir = _StaticPath(__file__).resolve().parent / "static"
    if _static_dir.is_dir():
        app.mount("/static", _StaticFiles(directory=str(_static_dir)), name="static")
except Exception:
    pass

# 引擎按蓝图填的模块清单；还没写出来的模块自动跳过（不影响已通过的能力）
_ROUTER_MODULES = {modules!r}

for _name in _ROUTER_MODULES:
    try:
        if importlib.util.find_spec(_name) is None:
            continue
    except (ImportError, ValueError):
        continue
    # **单个模块 import 失败不许炸掉整个 app**。
    # 实测 2026-11-10：`app/api/xxx.py` 里用了 router 却没定义（NameError）→
    # 入口 import 时抛异常 → 整个 app 起不来 → 探针报"没有可用的 ASGI 应用"
    # → 84 条能力一起红，引擎还以为"功能全没写"。
    # 坏模块跳过即可：它自己的端点 404，别的模块照常服务。
    try:
        _mod = importlib.import_module(_name)
    except Exception:
        continue
    _router = getattr(_mod, "router", None)
    if _router is not None:
        try:
            app.include_router(_router)
        except Exception:
            continue
'''


def _web_route_plan(bp, sl: dict, ctext: str) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    """切片 → {模块: [路由]} 与 {模块: {函数: [参数名]}}（都来自探针与契约，机械可得）。"""
    from . import contract as _cm
    parsed = _cm.parse_requirement(ctext) or {}
    fmap: dict[str, dict] = {}
    for mod, c in parsed.items():
        fmap[mod] = {f.name: [p.name for p in (f.params or [])] for f in (c.funcs or [])}
    plan: dict[str, list[dict]] = {}
    for cid in (sl.get("caps") or []):
        c = bp.by_id(cid)
        if c is None or str((c.probe or {}).get("kind") or "") != "http":
            continue
        mod = (c.modules or ["app/main.py"])[0]
        steps = (c.probe or {}).get("steps") or [{}]
        st = steps[-1] or {}
        names = list((fmap.get(mod) or {}).keys())
        route = {"method": str(st.get("method") or "GET").upper(),
                 "path": _fastapi_path(st.get("path") or "/"),
                 "func": names[0] if names else "",
                 "body_keys": list((st.get("json") or {}).keys()),
                 "body_sample": st.get("json")}
        stt = st.get("expect_status")
        if isinstance(stt, int):
            route["status"] = stt
        plan.setdefault(mod, []).append(route)
    return plan, fmap


def _fastapi_path(path: str) -> str:
    """`/tickets/${ticket_id}` → `/tickets/{ticket_id}`（探针用 ${} 传变量，路由用 {} 占位）。"""
    return re.sub(r"\$\{(\w+)\}", r"{\1}", str(path or ""))


def _module_signatures(src: str) -> dict[str, list[str]]:
    """从源码 AST 取 {函数名: [参数名]}（跳过下划线私有与引擎自己的 handler）。

    用 AST 而不是契约：路由接线发生在"作者可能刚把文件整篇重写"之后，
    契约里记的函数名不一定还是现在这个文件里的名字。
    """
    import ast as _ast
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return {}
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            name = node.name
            if name.startswith("_"):
                continue
            out[name] = [a.arg for a in node.args.args]
    return out


HELPER_WORDS = ("connection", "conn", "_db", "get_db", "cursor", "session", "close",
                "helper", "setup", "initialize", "engine", "migrate", "table")

# 方法-动词冲突（2026-11-08 实测）：`GET /analytics/directions` 抢到 `create_direction`、
# `GET /topics/{id}` 抢到 `submit_topic`、`GET /llm/candidates/{id}` 抢到 `create_candidates`。
# 一个 GET 探测器打到"创建/提交"函数上会改库状态，比 404 更糟（假绿或连锁 500）。
# 表里是"该动作在语义上绝不可能是这个 HTTP 方法"的组合，命中即否决该候选（不是降分）。
_METHOD_CONFLICT = {
    "GET": ("create", "add", "insert", "submit", "delete", "remove", "update", "patch",
            "set", "confirm", "publish", "offline", "reject", "adopt", "run"),
    "HEAD": ("create", "add", "insert", "submit", "delete", "remove", "update", "patch",
             "set", "confirm", "publish", "offline", "reject", "adopt", "run"),
    "DELETE": ("create", "add", "insert", "submit", "publish"),
    # PUT/PATCH 是"替换/改这一条"，不是"再建一条"：实测 2026-11-08
    # `PUT /llm/candidates/{id}` 接到 `create_candidates`（会真的多造候选）
    "PUT": ("create", "add", "insert", "submit"),
    "PATCH": ("create", "add", "insert", "submit"),
}

# 读语义动词：路径末段是 `{id}` 这类参数段（详情端点）时，优先选这些词开头的函数。
_READ_VERBS = ("get", "detail", "fetch", "read", "show", "view", "query", "find", "load")

# 构造器/解析器命名（build_*/parse_*/make_*）不是请求处理器：它们造数据，不读 header、
# 不进库。实测 2026-11-08：`GET /analytics/directions` 抓到
# `app/schemas.py::build_direction_request`、`GET /auth/me` 抓到 `build_me_response`，
# 接上去只会返回骨架字典 → 字段比对全红。**只看前缀**，避免误杀
# `validate_candidate_body`（它本身可能就是 `POST .../validate` 的合法端点）。
_NON_HANDLER_PREFIX = ("build", "parse", "make", "coerce", "format", "render", "normalize")

# 通用末段（不含资源信息）：`/auth/me`、`/people/students/me`、`/results/me` 都以 me 结尾，
# 只看末段会让 `auth_me` 把三条不同资源的接口全抢走（实测 2026-11-08）。
# 命中通用末段时，**必须再命中路径里的某个资源段**，否则不接。
_GENERIC_TAIL = {"me", "self", "mine", "current", "profile", "all", "detail", "list"}

# 鉴权资源段：这些路径下的 `me` 允许用 `current_user` 命名（REST 惯例）。
_AUTH_SEG = {"auth", "deps", "security", "session", "account", "accounts"}

# 路径末段别名（REST 惯例）：`/auth/me` 的正解通常叫 `get_current_user`。
_TAIL_ALIAS = {"me": ("whoami", "current_user", "currentuser"),
               "self": ("current_user", "currentuser")}


def _name_words(name: str) -> set[str]:
    """函数名 → 词集（`create_topic_endpoint` → {create, topic, endpoint}）。

    短资源段（`me`、`id`）必须按词匹配而不是子串：`"me" in "create_comment"` 为真，
    会再把详情端点接到写函数上（2026-11-08 实测）。
    """
    return {w for w in re.split(r"[^a-z0-9]+", str(name or "").lower()) if w}


def _route_shape(path: str) -> list[str]:
    """路径 → 形状（{param} 归一为 '{p}'，去空段）：/batches/{id}/run → ['batches','{p}','run']。"""
    return ["{p}" if seg.startswith("{") else seg.lower()
            for seg in str(path or "").split("/") if seg]


def _route_handler_from_src(src: str, method: str, path: str) -> str:
    """从产物源码**实测**这条路由挂的处理函数名（AST 级，不依赖命名约定）。

    为什么（2026-11-08 耦合点修复）：以前路由→函数全靠语义猜（动词组 + `*_endpoint`
    命名兜底），作者写 `run_batch(batch_id)`（无后缀）就接不上 → 404 → 能力被判
    "做不出来"。而作者既然把路由写出来了（`@router.post("/batches/{id}/run")`），
    装饰器下面挂的函数名就是**权威答案**，读它即可，不用猜。
    路径匹配容忍前缀差异（探针带 /api/v1，装饰器是相对 router 的路径）与参数名差异
    （/{id} 与 /{batch_id} 同一条路由）。
    """
    import ast as _ast
    want = _route_shape(path)
    if not want:
        return ""
    try:
        tree = _ast.parse(src or "")
    except SyntaxError:
        return ""
    hits: list[str] = []
    for node in _ast.walk(tree):
        if not (isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                and node.decorator_list):
            continue
        # 引擎自己生成的接线（`_engine_*`）不是权威：它可能正是上次接错的那条。
        # 若把它当权威，错映射会被自证并永远纠不回来（2026-11-08 实测 /auth/me）。
        if str(node.name).startswith("_"):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, _ast.Call) and isinstance(dec.func, _ast.Attribute)):
                continue
            if str(dec.func.attr).lower() != str(method or "GET").lower():
                continue
            if not (dec.args and isinstance(dec.args[0], _ast.Constant)
                    and isinstance(dec.args[0].value, str)):
                continue
            got = _route_shape(dec.args[0].value)
            if got and (got == want or (len(got) <= len(want) and want[-len(got):] == got)):
                hits.append(node.name)
                break
    if len(hits) == 1:
        return hits[0]
    if hits:
        # 多个同形路由（不同 router 各挂一条）：优先与路径资源词相关的，仍不定取首个
        res = [t for t in _route_shape(path) if t != "{p}"]
        for n in hits:
            if any(t in n.lower() for t in res):
                return n
        return hits[0]
    return ""


def _pick_func(path: str, funcs: dict[str, list[str]], method: str = "GET",
               src: str = "") -> str:
    """按路径选端点函数：**路径末段必须在函数名里出现**，否则不接。

    这条规则的来历（2026-11-08 连环事故）：旧的"关键词组 + 唯一 *成 _endpoint 兜底"
    会把一个模块里唯一的 `create_candidates` 接到 `/preferences`、`/appeals`、
    `/auth/login`…… 十几个完全无关的端点上，请求体和校验模型全错，几十条能力一起红。
    现在只认"名字里真有这个资源/动作"，接不上就返回空 → 端点不注册 → 404（诚实）。
    """
    p = str(path or "").lower()
    m = str(method or "GET").upper()
    if src:                                  # 产物里装饰器注册过的路由是权威
        got = _route_handler_from_src(src, m, path)
        if got:
            return got
    parts = [x for x in p.strip("/").split("/")
             if x and not x.startswith("{") and not x.startswith("$")]
    # 末段是参数段（/topics/{topic_id}）= 详情/操作端点，选函数时另行加权。
    # 注意：**不能按长度过滤 toks**。旧代码 `len(x) >= 3` 会把 `me`、`id` 这类短末段
    # 丢掉，last 退化成上一层资源段（/auth/me → auth），于是 `parse_authorization_header`
    # 靠 "auth" 子串抢走 `GET /auth/me`（2026-11-08 实测，返回空字符串）。
    _raw = [x for x in p.strip("/").split("/") if x]
    tail_is_param = bool(_raw) and (_raw[-1].startswith("{") or _raw[-1].startswith("$"))
    toks = [x for x in parts
            if x not in ("api", "rest", "internal")
            and not re.fullmatch(r"v\d+", x)]
    if not toks:
        # 根路径特例：`GET /` 没有资源段，靠"首页"词选函数。
        # 实测 2026-11-08：这里原来无条件 return ""，于是作者已写好的
        # `home_page_route()`（pages.py，返回 200 + HTML）永远接不上；而
        # apply_web_wiring 是**整块重建**，重建时把该模块旧引擎段清掉且不补回，
        # 已经 done 的 page.home / ui.index 直接被拉红（连续 10 轮探针 404）。
        if m not in ("GET", "HEAD"):
            return ""
        _home = ("home", "index", "root", "homepage", "landing")
        _hc = [(2 if re.search(r"_(route|endpoint)$", n.lower()) else 1, -len(n), n)
               for n in funcs if _name_words(n) & set(_home)]
        return max(_hc)[2] if _hc else ""
    last, res = toks[-1], toks[0]
    # 方法词：只当**加分**，不当入选理由（否则 create_* 会抢走所有 POST 端点）
    verb = {"POST": ("create", "add", "submit", "insert"),
            "PUT": ("update", "edit", "set", "confirm"),
            "PATCH": ("update", "edit", "set"),
            "DELETE": ("delete", "remove", "offline", "reject"),
            "GET": ("get", "list", "list_", "detail", "me")}.get(m, ())

    def forms(t: str) -> tuple[str, ...]:
        # 单数化要认 **es 结尾的复数**：只砍一个 's' 会把 'batches' 变成 'batche'
        # （不是 'batch'）→ 末段命中检查全灭 → `POST /api/v1/batches` 判定"没有匹配
        # 函数" → 引擎不接线 → 404 → 该能力永远 absent（实测 2026-11-09 14:45，
        # 夜班在同一片上反复重跑，gained=[]）。
        # 规则：sses/shes/ches/xes/zes 砍 'es'，其余以 s 结尾砍 's'。
        if t.endswith(("sses", "shes", "ches", "xes", "zes")):
            base = (t, t[:-2])
        elif t.endswith("s"):
            base = (t, t[:-1])
        else:
            base = (t, t + "s")
        return tuple(dict.fromkeys(base + _TAIL_ALIAS.get(t, ())))

    def tail_hit(n_low: str, words: set[str], t: str) -> bool:
        """末段是否命中函数名。短末段走词级，其余走子串（兼容 topics↔topic）。"""
        fs = forms(t)
        # 2026-11-10：**连字符/下划线路径段必须拆词**。
        # `quota-suggestions` 原样去函数名里找 substring 永远找不到 ——
        # 函数名是 `quota_suggestion`（下划线），于是这些端点一律"无匹配函数"
        # → 不接线 → 404。实测一批 20 个端点就是这么丢的。
        _parts = [p for p in re.split(r"[-_]+", str(t)) if len(p) >= 3]
        if len(_parts) > 1:
            # 复合末段：每个词都要在函数名里出现（quota-suggestions → quota + suggestion）
            return all(any(pp in n_low for pp in forms(x)) for x in _parts)
        if len(t) < 4:
            if words & set(fs):
                return True
            # 别名是复合词（current_user），词集里不会整体出现，按子串兜
            return any(len(f) >= 6 and f in n_low for f in fs)
        return any(f in n_low for f in fs)

    def usable(n: str) -> bool:
        return not any(h in n.lower() for h in HELPER_WORDS)

    def generic_ok(n_low: str, words: set[str], t: str) -> bool:
        """通用末段（me/self/all…）不提供资源信息，改由路径资源段决定是否可接。

        判据（2026-11-08 实测三条路由全被 deps.auth_me 抢走后定）：
          1. 函数名命中路径里的某个资源段        → 可接（get_student_profile ← /people/students/me）
          2. 鉴权上下文 + current_user 命名      → 可接（get_current_user ← /auth/me）
          3. 函数名除"动作词 + 通用词"外没有别的资源词 → 可接（get_me / me）
        其余否决：`auth_me` 不该接 `/people/students/me`、`/results/me`。
        """
        if t not in _GENERIC_TAIL:
            return True
        ctx = toks[:-1]
        if ctx and any(any(f in n_low for f in forms(x)) for x in ctx):
            return True
        if ctx and ctx[0] in _AUTH_SEG and ("current_user" in n_low or "currentuser" in n_low):
            return True
        return not (words - {t} - set(verb))

    cands: list[tuple[int, int, str]] = []
    conflicts = _METHOD_CONFLICT.get(m, ())
    for n in funcs:
        if not usable(n):
            continue
        low = n.lower()
        words = _name_words(low)
        # 构造器/解析器不是端点（build_direction_request / build_me_response 实例）
        if low.startswith(_NON_HANDLER_PREFIX):
            continue
        # 方法-动词冲突：GET 打到 create/submit/update 上不是"次优"，是**错**（会改库）
        if any(v in words for v in conflicts):
            continue
        # **路径末段必须命中**：只靠资源段会把 /auth/login 接到 parse_authorization_header 上
        if last in _GENERIC_TAIL:
            if not generic_ok(low, words, last):
                continue
        elif not tail_hit(low, words, last):
            continue
        score = 6
        if any(low.startswith(v) or ("_" + v) in low for v in verb):
            score += 2
        # 详情端点（末段是参数）优先读语义函数，别把 GET /topics/{id} 接到 list_topics
        if tail_is_param and any(w in words for w in _READ_VERBS):
            score += 3
        if re.search(r"_(endpoint|route)$", low):
            score += 1
        cands.append((score, -len(n), n))
    if not cands:
        return ""
    cands.sort(reverse=True)
    return cands[0][2]

def _module_for_path(ws, path: str) -> str:
    """按**单个路径**解析落点模块（文件必须已存在），解析不到返回空。

    2026-11-10：实现挪到 `core/openhouse.module_for_path` ——
    造物模式也要用同一份（能力红灯 → 工作单的映射），放一份避免漂移。
    本函数保留为转发器，老调用方不用改。
    """
    from . import openhouse as _oh
    return _oh.module_for_path(ws, path)


def _resolve_route_module(ws, cap) -> str:
    """按能力路径解析落点模块（文件必须已存在）。保留给旧调用方。"""
    import re as _re
    paths = [str((s or {}).get("path") or "") for s in ((cap.probe or {}).get("steps") or [])]
    skip = {"api", "rest", "internal"}
    seg = ""
    for raw in reversed([p for p in paths if p]):
        parts = [x for x in raw.strip("/").split("/")
                 if x and not x.startswith("$") and not x.startswith("{")]
        parts = [x for x in parts
                 if x.lower() not in skip and not _re.fullmatch(r"v\d+", x.lower())]
        if parts:
            seg = parts[0]
            break
    if not seg:
        return ""
    cands = [f"app/api/{seg}.py", f"app/{seg}.py",
             f"app/api/{seg}s.py", f"app/{seg}s.py"]
    for m in cands:
        if (Path(ws) / m).is_file():
            return m
    return ""


def _author_area(src: str) -> str:
    """只看作者写的部分（引擎段之前）。

    为什么必须剥掉引擎段（实测 2026-11-08）：`_pick_func(src=...)` 会读产物里
    装饰器注册的路由当权威映射，而**引擎段自己的旧装饰器**也在里面 —— 一旦某次
    接错（/auth/login → parse_authorization_header），下次还会读到这个错映射，
    永远纠不回来；同时正确端点会因为旧映射指向已删函数而被跳过（/auth/register 消失）。
    """
    if WEB_ROUTE_BEGIN in src:
        return src.split(WEB_ROUTE_BEGIN, 1)[0]
    return src


def _resource_of(path: str) -> str:
    """端点的资源段（跳过 api / v1..v9 / rest / 参数段）。"""
    import re as _re
    parts = [x for x in str(path or "").strip("/").split("/")
             if x and not x.startswith("{") and not x.startswith("$")]
    for x in parts:
        low = x.lower()
        if low in ("api", "rest", "internal") or _re.fullmatch(r"v\d+", low):
            continue
        return low
    return ""


def _candidate_modules(ws) -> list[str]:
    """接线候选模块（按优先级）。

    **必须包含 `matching_core`**（2026-11-10 实锤）：算法类端点
    （`/api/v1/algorithms/solve` → `solve_student_da`）的实现在算法包里，
    候选里没有它 → `_pick_func` 找不到函数 → 端点被跳过 → 端到端 404。
    实测一批 23 个端点全是这么丢的。

    **必须递归包含 `app/` 的所有子目录**（2026-11-11 19:20 实锤）：
    切片契约的落点被改到业务层 `app/services/*.py` 之后，候选集里**没有它**
    → 契约点名的函数永远找不到 → 引擎只能接 `app/api/*.py` 里的**旧函数**
    → 远端按契约新写的函数白写（实测 `create_topics` 写对了也不生效，
    `source` 永远 None）。**候选集与契约落点必须同步扩容** ——
    否则"所有权改对"反而让接线接不上。
    """
    # **由栈适配层决定**（2026-11-15）：Django 的代码在 `apps/<域>/*.py`，
    # 写死 `app/` 会让它一个候选都没有 → 契约点名的函数永远找不到 → 端点全 404。
    try:
        from . import stack as _st
        _ad = _st.from_workspace(ws)
    except Exception:                                    # noqa: BLE001
        _ad = None
    if _ad is not None and getattr(_ad, "name", "fastapi") != "fastapi":
        _out: list[str] = []
        for _g in _ad.module_globs:
            for _f in sorted(Path(ws).glob(_g)):
                _posix = _f.as_posix()
                if _f.name == "__init__.py" or "/migrations/" in _posix:
                    continue
                try:
                    _rel = str(_f.relative_to(Path(ws))).replace("\\", "/")
                except ValueError:
                    continue
                if _rel not in _out:
                    _out.append(_rel)
        return _out

    out: list[str] = []
    for base in ("app", "app/api", "matching_core"):
        d = Path(ws) / base
        if not d.is_dir():
            continue
        # `app` 用递归（覆盖 app/services/ 等子目录）；其余保持一层
        files = sorted(d.rglob("*.py")) if base == "app" else sorted(d.glob("*.py"))
        for f in files:
            if f.name == "__init__.py" or f.stem == "main":
                continue
            try:
                rel = str(f.relative_to(Path(ws))).replace("\\", "/")
            except ValueError:
                rel = f"{base}/{f.name}"
            if rel not in out:
                out.append(rel)
    return out


def _locate_route(ws, method: str, path: str, prefer: str = "",
                  prefer_mod: str = "", bkeys=None) -> tuple[str, str]:
    """给端点找 (模块, 函数)。找不到就返回 ("", "")。

    评分：模块名与资源段同名 > 包含关系 > 仅函数匹配；同分时优先路径解析出的模块。
    只在**该模块真有匹配函数**时才接线：宁可 404，也不接到语义无关的函数上
    （2026-11-08 实测：`/auth/login` 被接到 `topics.parse_create_topic_body`
    → 所有带登录前置的探针全报 `ValueError: title 不能为空`）。

    **prefer / prefer_mod = 契约点名**（2026-11-11，§11.4g）：
    契约文本里本来就写着"这个端点归哪个模块、哪个函数管"，那是**唯一权威** ——
    优先用它，别再靠字符串猜。实测不点名的代价：
    `GET /api/v1/people/teachers/me` 被猜到 **app/analytics.py** 的负载建议函数上
    → 返回 `[{'teacher':'','quota':0,'assigned':0,'load_ratio':0.0}]`
    → 端到端永远红，而正确实现就在 `app/api/people.py` 里。

    **prefer_mod 优先于 prefer**：点名函数**未必能当路由处理器**
    （实测 `teachers_me(account: dict)` 的 `account` 参数在路由层供给不了
    —— 路由层要的是 `authorization`）→ 硬接会"注册成功但语义是另一件事"，
    那比 404 更坏（引擎看不出真相，永远修不好）。
    所以：**模块用点名的，函数在那个模块里按规则挑**。
    """
    prefer = str(prefer or "").strip()
    prefer_mod = str(prefer_mod or "").strip()
    if prefer:
        # **契约点名的函数优先**（2026-11-11 19:05）：契约说这个端点归 `create_topics`，
        # 就该用它 —— 旧函数 `create_topic` 名字更短、按"名字像"排序会排到前面，
        # 于是路由一直指着旧实现（实测：`source` 永远 None，远端写了新函数也没用）。
        # 前提是**参数接得上**（`_wire_call` 同一套判据）——接不上再回落。
        _cands_mods = ([prefer_mod] if prefer_mod else []) + list(_candidate_modules(ws))
        for m in _cands_mods:
            try:
                src = (Path(ws) / m).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if not re.search(r"^\s*(?:async\s+)?def\s+%s\s*\(" % re.escape(prefer),
                             src, re.M):
                continue
            try:
                _head0 = _author_area(src)
                _fns0 = _module_signatures(_head0)
                _a0, _bad0 = _wire_call(_head0, prefer, path, set(bkeys or []),
                                       _fns0, method)
            except Exception:                            # noqa: BLE001
                _bad0 = ["?"]
            if not _bad0:
                return m, prefer
            break        # 点名函数在（但接不上）→ 回落 prefer_mod 的挑法
    if prefer_mod:
        try:
            src = (Path(ws) / prefer_mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            src = ""
        if src:
            # **只用作者区的签名**（与 `ensure_web_router` 渲染时同源）：
            # 引擎段里是引擎自己生成的路由处理器，不是业务函数；
            # 选落点时按全文选 → 选中引擎段的函数 → 渲染时判"缺函数"跳过
            # → 整条路由 404（实测 2026-11-11，POST /api/v1/topics）。
            _head = _author_area(src)
            fns = _module_signatures(_head)
            # 候选按"名字像不像这个端点"排，**再按参数能不能接上筛**
            # （复用 `_wire_call` 的同一套判据 —— 不重复造逻辑）。
            # 实测（2026-11-11）：`POST /api/v1/topics` 若按名字挑会挑到
            # `create_topic_route(body, user)`，而 `user` 在路由层供给不了
            # → 渲染时整条路由被拒 → 404；真正能接的 `create_topic(body)` 被漏掉。
            _cands = [n for n in (fns or {}) if not n.startswith("_")]
            _cands.sort(key=lambda n: (0 if _pick_func(path, {n: (fns or {}).get(n) or []},
                                                          method, src=src) == n else 1,
                                        len(n)))
            for nm in _cands:
                try:
                    _a, _bad = _wire_call(_head, nm, path, set(bkeys or []), fns, method)
                except Exception as exc:                 # noqa: BLE001
                    # **不许静默吞**（2026-11-11 血泪）：这里吞过一次 NameError
                    # → 所有候选被跳过 → 回落旧猜测 → 挑了参数接不上的函数
                    # → 整条路由 404，而日志里什么都没有。
                    if _DBG:
                        print("[wire] 候选 %s 判定异常：%s: %s"
                              % (nm, type(exc).__name__, exc))
                    continue
                if not _bad:
                    return prefer_mod, nm
    if prefer:
        # 契约点名：哪个模块里**真有**这个函数，就用它（不再打分、不再猜）。
        for m in _candidate_modules(ws):
            try:
                src = (Path(ws) / m).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if re.search(r"^\s*(?:async\s+)?def\s+%s\s*\(" % re.escape(prefer),
                         src, re.M):
                return m, prefer
    seg = _resource_of(path)
    primary = _module_for_path(ws, path)
    mods = ([primary] if primary else []) + [m for m in _candidate_modules(ws) if m != primary]
    best = (0, "", "")
    for m in mods:
        try:
            src = _author_area((Path(ws) / m).read_text(encoding="utf-8-sig", errors="replace"))
        except OSError:
            continue
        fn = _pick_func(path, _module_signatures(src), method, src=src)
        if not fn:
            continue
        stem = Path(m).stem.lower()
        score = 10
        if seg and stem == seg:
            score += 40
        elif seg and (seg in stem or stem in seg):
            score += 20
        if m == primary:
            score += 10
        if score > best[0]:
            best = (score, m, fn)
    return best[1], best[2]


def _func_exists(ws, name: str, exclude: str = "") -> bool:
    """产物里**真有这个函数**吗（排除了某个模块）。

    用于"契约点名优先"的判定：点名函数**真实存在**时，引擎才敢接管
    作者抢跑注册的路由 —— 否则接管了却接不上（11-10 铁律的教训）。
    """
    nm = str(name or "").strip()
    if not nm:
        return False
    pat = re.compile(r"^\s*(?:async\s+)?def\s+%s\s*\(" % re.escape(nm), re.M)
    for m in _candidate_modules(ws):
        if exclude and str(m) == str(exclude):
            continue
        try:
            src = (Path(ws) / m).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if pat.search(src):
            return True
    return False


_DBG = bool(__import__("os").environ.get("SIXCODER_DEBUG_WIRE"))


def _authored_routes(src: str, author_only: bool = True) -> list[tuple[str, str, str]]:
    """文件里**注册**的路由 → [(METHOD, path, 函数名)]。

    为什么不用 `_route_handler_from_src`：它只认自己认得的写法，
    实测漏掉了 `app/analytics.py` 里的
    `@router.get("/api/v1/people/teachers/me", response_model=None, status_code=200)`
    → 引擎以为没人注册 → 不摘 → FastAPI 里它抢先注册 → 探针打到猜错的那个。
    这里用 **AST 全扫**，不漏写法。

    `author_only=True` 只看**作者区**（WEB_ROUTE_BEGIN 之前）；
    `False` 看全文（含引擎段 —— 引擎自己渲染的路由也是"注册"，
    重复注册正是要摘的对象）。
    """
    import ast as _ast
    head = src.split(WEB_ROUTE_BEGIN, 1)[0] if (author_only and WEB_ROUTE_BEGIN in src) else src
    try:
        tree = _ast.parse(head)
    except SyntaxError:
        return []
    out: list[tuple[str, str, str]] = []
    for node in tree.body:
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, _ast.Call) or not isinstance(dec.func, _ast.Attribute):
                continue
            if getattr(dec.func.value, "id", "") not in ("router", "app"):
                continue
            if not dec.args or not isinstance(dec.args[0], _ast.Constant):
                continue
            if not isinstance(dec.args[0].value, str):
                continue
            out.append((dec.func.attr.upper(), _fastapi_path(dec.args[0].value),
                        node.name))
    return out


def _strip_engine_routes(src: str, owned: set) -> str:
    """从**引擎段**里摘掉 owned 指定的路由（作者区不动）。

    为什么必须有（2026-11-11 实测）：`ensure_web_router` 在"本轮无可接路由"时
    会**原样保留旧引擎段**（11-10 的保护：怕清了导致 404）——
    但那个端点可能**已经由别的模块接管**（本例：`teachers/me` 由
    `app/api/people.py` 接管），旧段里这份是**重复注册且实现错误**的
    （返回负载建议结构 load_ratio）→ FastAPI 里先 include 的抢先注册
    → 探针永远打到它 → 端到端永远红。
    """
    if WEB_ROUTE_BEGIN not in src:
        return src
    head, rest = src.split(WEB_ROUTE_BEGIN, 1)
    if WEB_ROUTE_END in rest:
        blk, tail = rest.split(WEB_ROUTE_END, 1)
    else:
        blk, tail = rest, ""
    blk2, _rm = _strip_author_routes(blk, owned)     # 复用同一套摘除逻辑
    return head + WEB_ROUTE_BEGIN + blk2 + WEB_ROUTE_END + tail


def apply_web_wiring(ws, bp, route_funcs: dict | None = None,
                     touched: list | None = None) -> list[str]:
    """按蓝图重建**全部**路由接线（纯函数式：注册表是输出，不参与输入）。

    为什么改成重建而不是增量合并（实测 2026-11-08 的连环事故）：
      · 只接最后一步 → 多步链的 `POST /datasets/seed` 永远 404；
      · 按"能力的模块"渲染 → `/auth/login` 被渲染进 `topics.py`，还用 topics 的
        请求体模型校验 → `ValueError: title 不能为空`，几十条能力一起红；
      · 增量合并 + 历史注册表 → 幽灵路由剪不掉，还会在两个模块之间来回抖。
    现在：收集端点 → 逐个端点选"真有匹配函数的那个模块" → 整块重渲染 → 注册表覆盖写。
    同一份输入永远得到同一份输出（幂等），端点不会同时出现在两个模块里。

    **接线层也会改坏产物**（2026-11-11 实测，§11.4m）：
      一次接线会**批量重写所有接线文件**（实测一次提交改了 38 个文件），
      而这些改动**不经过作者写码路径的回归守卫**（守卫只覆盖作者写的那一个文件）。
      → 接线把 `app/api/topics.py` 的路由换成引擎渲染版 → 该文件承载的
        `cap.topic.create_draft` / `cap.inv.topic.no_neg_quota_3f14` 一起变红
        → **引擎自己打坏已通过的能力**（实测切片 s33 的 `regression` 事件）。
      因此：接线改过的文件必须能被调用方**复验 + 回滚**。
      → `touched`：传一个 list 进来，被改动的模块路径会填进去
        （调用方据此复验；见 `core/company.py` 的 `_wiring_guard`）。
    """
    ws = Path(ws)
    touched_out = touched if isinstance(touched, list) else []
    # ① 收集端点（来自所有 http 能力的**正向**步骤；负向用例只判不接线）
    wanted: dict[tuple[str, str], dict] = {}
    _keysets: dict[tuple[str, str], set] = {}
    _types: dict[tuple[str, str], dict[str, set]] = {}
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        for st in ((c.probe or {}).get("steps") or []):
            st = st or {}
            path = _fastapi_path(st.get("path") or "")
            if not path:
                continue
            _stt = st.get("expect_status")
            if (isinstance(_stt, int) and _stt >= 400) or (
                    isinstance(_stt, list) and _stt and all(
                        isinstance(x, int) and x >= 400 for x in _stt)):
                continue
            k = (str(st.get("method") or "GET").upper(), path)
            _keysets.setdefault(k, set()).add(frozenset((st.get("json") or {}).keys()))
            for _fk, _fv in (st.get("json") or {}).items():
                _types.setdefault(k, {}).setdefault(_fk, set()).add(_py_type(_fv))
            cur = wanted.get(k)
            if cur is None:
                wanted[k] = st
                continue
            # 同端点多个样本：带状态码的优先，其次请求体字段多的优先
            if (isinstance(st.get("expect_status"), int)
                    and not isinstance(cur.get("expect_status"), int)):
                wanted[k] = st
            elif len((st.get("json") or {})) > len((cur.get("json") or {})):
                wanted[k] = st

    # ②.5 **作者自己注册的端点是权威**（2026-11-10 实锤）
    # 远端按提示词用 FastAPI 装饰器注册了正确的端点后，引擎若还"接管"它，
    # 会先把它摘掉（_strip_author_routes）再自己接 —— 而引擎靠字符串猜，
    # 在**异名**场景接不上（quota-suggestions ↔ quota_advice）→ 路由越接越少
    # （实测 web_routing routes 3→2→1→1）。
    # 所以：作者已注册的端点 → 引擎不接管、不摘除，只记进接线表（入口要 include 它）。
    #
    # 2026-11-11（§11.4g）**例外**：**契约点名可用且点名函数真实存在**时，
    # 作者的注册是"抢跑"（不是权威）—— 让引擎按点名接管。
    # 实测代价（不接管的代价）：甲方线早期把路由写进了引擎拥有的
    # `app/api/people.py`（`@router.get('/api/v1/people/teachers/me')` → 返回
    # `[{'teacher':'','quota':0,'assigned':0,'load_ratio':0.0}]` 的负载建议结构），
    # FastAPI 里它先注册 → 探针永远打到它 → 业务层写对了也轮不到 →
    # 端到端永远红、覆盖永远不涨。
    # 接管是安全的：`_strip_author_routes` 会摘掉作者的装饰器，
    # 引擎渲染段注册"契约点名 + 契约签名"的那一个。
    from . import routefuncs as _rfmod
    # 点名表：调用方传了就用它；没传就**兜底**从产物仓取（单一实现，零漏传 ——
    # 接线的调用点有 3 处，靠"每个调用点都记得传"必然漏一处，漏一处就退回字符串猜）。
    _rf = {str(k): str(v) for k, v in (route_funcs or {}).items()}
    if not _rf:
        try:
            _rf = _rfmod.from_workspace(ws)
        except Exception:                                # noqa: BLE001
            _rf = {}
    authored_by_mod: dict[str, list[tuple[str, str, str]]] = {}
    _engine_take: set[tuple[str, str]] = set()   # 引擎接管的端点（作者注册会被重渲染替换）
    _seen_paths: set[tuple[str, str]] = set()
    for _mod in _candidate_modules(ws):
        try:
            _src = (Path(ws) / _mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        _seen_paths: set[tuple[str, str]] = set()
        for (_m, _p) in list(wanted.keys()):
            # **用 `_route_handler_from_src`（不改成 AST 扫描）**：
            # 它认得的写法比 AST 宽（作者段与引擎段都能认），
            # 换成 AST 只扫作者区 → 引擎段里的注册被当成"没人注册"
            # → 引擎抢接并重渲染 → 请求体模型与作者实现对不上 → 422
            # （实测 2026-11-11：auth/login 从 200 变 422）。
            _fn = _route_handler_from_src(_src, _m, _p)
            if not _fn:
                continue
            authored_by_mod.setdefault(_mod, []).append((_m, _p, _fn))
            # **引擎能不能接管**（2026-11-11，§11.4g）——
            # 判据与渲染时**同一套**（`_wire_call`）：
            #   · 引擎接得上（参数能供给）→ **接管并重渲染**。
            #     远端自己注册的路由常常**没声明请求体模型**
            #     （实测 `router.add_api_route("/auth/login", login, …)`）
            #     → FastAPI 把 login(username, password) 当**查询参数**
            #     → 探针发 JSON body → 参数缺失 → **422**
            #     → 全线 POST 能力一起红。引擎渲染的版本带 Pydantic 模型 → 不会 422。
            #   · 引擎接不上（11-10 的顾虑：异名场景猜不准）→ **保留作者的**，
            #     不摘（摘了就是 404，比 422 更坏）。
            _bk: set[str] = set()
            _ks0 = _keysets.get((_m, _p)) or set()
            if _ks0:
                _bk = set.union(*[set(x) for x in _ks0])
            try:
                _ah = _author_area(_src)
                _afn = _module_signatures(_ah)
                _a2, _bad2 = _wire_call(_ah, _fn, _p, _bk, _afn, _m)
            except Exception:                            # noqa: BLE001
                _bad2 = ["?"]
            if not _bad2:
                _engine_take.add((_m, _p))     # → 端点留在 wanted，引擎接管
            else:
                wanted.pop((_m, _p), None)    # → 保留作者的注册

    # ②.6 **权威模块**（§11.4g）：点名表说这个端点归哪个模块 → 那个模块是权威；
    # **别的模块**里同路径的注册是"猜错的抢跑" → 摘掉。
    # 实测：`GET /api/v1/people/teachers/me` 在 **app/analytics.py**（猜错，返回
    # 负载建议结构 load_ratio）和 **app/api/people.py**（正确）里**各注册了一份**，
    # FastAPI 里先 include 的抢先注册 → 探针打到猜错的那份 → 端到端永远红。
    # 判据：**保留"含点名函数的模块"里的注册**，摘掉其它模块的同名注册。
    _auth_path: dict[str, str] = {}
    for _p, _fn in _rf.items():
        for _mod in _candidate_modules(ws):
            try:
                _src2 = (Path(ws) / _mod).read_text(encoding="utf-8-sig", errors="replace")
            except OSError:
                continue
            if re.search(r"^\s*(?:async\s+)?def\s+%s\s*\(" % re.escape(_fn), _src2, re.M):
                _auth_path.setdefault(_p, _mod)
                break
    # 作者注册的端点（AST 全扫，不漏写法）→ 权威模块 = 点名表说的模块
    _auth_mod: dict[tuple[str, str], str] = {}
    # **所有**模块里的注册（含引擎段 —— 引擎自己渲染的也是"注册"，
    # 重复注册正是要摘的对象）
    _reg_all: dict[tuple[str, str], str] = {}
    for _mod in _candidate_modules(ws):
        try:
            _src3 = (Path(ws) / _mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for (_v, _p, _fn) in _authored_routes(_src3, author_only=False):
            _reg_all.setdefault((_v, _p), _mod)
            if (_v, _p) not in _auth_mod:
                _auth_mod[(_v, _p)] = _mod
    for (_m, _p) in list(_auth_mod):
        _auth_mod[(_m, _p)] = _auth_path.get(_rfmod.normalize(_p),
                                              _auth_mod[(_m, _p)])

    # ② 选落点 + 组装
    per_mod: dict[str, list[dict]] = {}
    skipped: list[str] = []
    # **全局接管端点**（§11.4g）：被契约点名接管的端点，其**作者注册**
    # 要在**所有模块**里摘掉 —— 否则别的模块（如 app/analytics.py）
    # 抢先注册同一个路径，FastAPI 里它先注册 → 探针打到它 → 永远红。
    _owned_all: set[tuple[str, str]] = set()
    for (method, path), st in sorted(wanted.items()):
        # 契约点名的**函数**（第一优先）+ 点名的**模块**（次选）
        _prefer = _rf.get(_rfmod.normalize(path), "")
        _prefer_mod = _auth_path.get(_rfmod.normalize(path), "") or \
            _auth_mod.get((method, path), "")
        _ks0 = _keysets.get((method, path)) or set()
        _bkeys0 = set.union(*[set(x) for x in _ks0]) if _ks0 else set()
        mod, fn = _locate_route(ws, method, path, prefer=_prefer,
                                prefer_mod=_prefer_mod, bkeys=_bkeys0)
        if _DBG:
            print("[wire] %s %s prefer_mod=%r -> mod=%r fn=%r" % (method, path, _prefer_mod, mod, fn))
        if not mod:
            skipped.append(f"{method} {path}（没有匹配函数）")
            continue
        if _prefer and fn == _prefer:
            _owned_all.add((method, path))
        _ks = _keysets.get((method, path)) or set()
        _req = set.intersection(*[set(x) for x in _ks]) if _ks else set()
        _all = set.union(*[set(x) for x in _ks]) if _ks else set()
        # 同字段在不同样本里类型冲突（capacity 一会 int 一会 str）→ Any，
        # 否则 pydantic 按其中一个类型校验，另一个样本直接 422（实测 2026-11-08）
        _ty = {_fk: (next(iter(_tv)) if len(_tv) == 1 else "any")
               for _fk, _tv in (_types.get((method, path)) or {}).items()}
        route = {"method": method, "path": path, "func": fn,
                 "body_keys": sorted(_all),
                 "body_required": sorted(_req),
                 "body_types": _ty,
                 "body_sample": st.get("json")}
        if isinstance(st.get("expect_status"), int):
            route["status"] = int(st["expect_status"])
        per_mod.setdefault(mod, []).append(route)

    # 作者自己注册的端点 → 记进接线表（入口要 include 它），但不进引擎渲染段
    for _mod, _eps in authored_by_mod.items():
        for (_m, _p, _fn) in _eps:
            per_mod.setdefault(_mod, []).append(
                {"method": _m, "path": _p, "func": _fn,
                 "body_keys": [], "body_required": [], "body_types": {},
                 "authored": True})

    # ③ 整块重渲染（含"没有路由的模块"也要把旧引擎段清掉）
    done: list[str] = []
    for mod in _candidate_modules(ws):
        # **只渲染引擎接管的端点** —— 作者已注册的（authored）保留原样，
        # 引擎既不摘也不重复注册（否则异名场景下路由会被摘掉又接不上）。
        routes = [r for r in (per_mod.get(mod) or []) if not r.get("authored")]
        _auth_here = [r for r in (per_mod.get(mod) or []) if r.get("authored")]
        try:
            src = (Path(ws) / mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        # 摘掉**别的模块**里同路径的注册（它们是猜错的抢跑，§11.4g）
        # 权威模块 = 点名表说的模块；点名表没说 → 保留（11-10 铁律：别乱摘）
        _strip_extra: set[tuple[str, str]] = set()
        for (_v, _p2) in _reg_all:
            _auth = _auth_path.get(_rfmod.normalize(_p2), "")
            if _auth and _auth != mod:
                _strip_extra.add((_v, _p2))
        src, _removed = _strip_author_routes(
            src, {(str(r["method"]), str(r["path"])) for r in routes})
        src, _added = _ensure_imports(src)
        # 作者用了 @router.xxx 却没定义 router → 补一行样板（否则模块 import 就炸）
        src, _rfix = _ensure_router_decl(src)
        if _rfix:
            _added = list(_added) + ["router = APIRouter()"]
        src, _local = _ensure_local_imports(ws, src, mod)
        if _local:
            _added = list(_added) + _local
        if _removed or _added:
            try:
                (Path(ws) / mod).write_text(src, encoding="utf-8")
                if mod not in touched_out:
                    touched_out.append(mod)          # §11.4m：接线改动要能被复验/回滚
            except OSError:
                pass
            if _removed:
                done.append(f"{mod}: 摘掉作者自挂的 {', '.join(_removed[:3])}")
            if _added:
                done.append(f"{mod}: 补 import {', '.join(x.split()[-1] for x in _added[:3])}")
        res = ensure_web_router(ws, mod, routes, _module_signatures(_author_area(src)),
                                full=True, touched=touched_out)
        if res.get("wrote") and routes:
            done.append(f"{mod}:{len(routes)} 条路由")
        if _auth_here:
            done.append(f"{mod}:{len(_auth_here)} 条路由由作者自己注册（引擎不接管）")
        # 接线表要和**文件里真实注册的**一致：渲染时被跳过的（参数接不上）
        # 不许还留在表里 —— 否则 include 列表以为它注册了，实际是 404，
        # 排查时两边对不上（实测 2026-11-10：web_routes.json 记着
        # `GET /api/v1/topics/{topic_id}` → 实际文件里没有）。
        _rendered = set(str(x) for x in (res.get("rendered") or []))
        if routes and _rendered is not None:
            per_mod[mod] = [r for r in (per_mod.get(mod) or [])
                            if r.get("authored")
                            or f"{str(r.get('method') or '').upper()} {r.get('path')}" in _rendered]

    # ③.5 **摘掉重复注册**（2026-11-11，§11.4g）
    # 同一个路径在多个模块注册 → FastAPI 里先 include 的抢先注册 →
    # 探针打到猜错的那个（实测 `teachers/me` 在 `app/analytics.py` 也注册了一份，
    # 返回负载建议结构 load_ratio；而正确的是 `app/api/people.py` 那份）。
    #
    # **判据是"谁真的渲染出了这个路径"**，不是"谁在点名表里" ——
    # 渲染可能失败（参数接不上），那时旧路由还得留着（11-10 的保护）。
    # 顺序也不能反：先渲染、后摘除；摘早了会把唯一能用的路由弄丢
    # （实测 2026-11-11：POST /api/v1/topics 从 422 变 404）。
    _rendered_by: dict[str, str] = {}
    for _mod, _rs in per_mod.items():
        for _r in _rs:
            if _r.get("authored"):
                continue
            _rendered_by.setdefault(_rfmod.normalize(str(_r.get("path") or "")), _mod)
    for _mod in _candidate_modules(ws):
        _strip2: set[tuple[str, str]] = set()
        for (_v, _p) in _reg_all:
            _owner = _rendered_by.get(_rfmod.normalize(_p))
            if _owner and _owner != _mod:
                _strip2.add((_v, _p))
        # **引擎接管的端点**：作者自己注册的那份也要摘（引擎重渲染了同一个路径
        # → 留着就是重复注册 → 先注册的那份生效 → 请求体模型对不上 → 422）
        for (_v, _p) in _engine_take:
            _owner = _rendered_by.get(_rfmod.normalize(_p))
            if _owner and _owner == _mod:
                _strip2.add((_v, _p))
        if not _strip2:
            continue
        try:
            _s = (Path(ws) / _mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        _s2, _rm = _strip_author_routes(_s, _strip2)
        _s3 = _strip_engine_routes(_s2, _strip2)
        if _s3 != _s:
            try:
                (Path(ws) / _mod).write_text(_s3, encoding="utf-8")
                if _mod not in touched_out:
                    touched_out.append(_mod)          # §11.4m：接线改动要能被复验/回滚
                done.append("%s: 摘掉重复注册的 %s"
                            % (_mod, "、".join(str(x[1]) for x in sorted(_strip2)[:2])))
            except OSError:
                pass

    _save_routes(ws, per_mod)
    if skipped:
        done.append(f"跳过 {len(skipped)} 个端点（无匹配函数）：" + "；".join(skipped[:3]))
    return done


def ensure_web_build(ws, emit=None, timeout: int = 300) -> dict:
    """§20.5 B 档落点 1（最小闭环）：package.json 存在且声明 build → npm install + build。

    口径（安全第一）：
      · 没有 package.json → 零影响（大多数项目走不到这里）；
      · npm 不在 PATH → 事件留痕并跳过（不许因为缺工具炸掉整个夜班）；
      · node_modules 已在 → 跳过 install（幂等，重跑不重复下载）；
      · 构建失败不抛异常 —— 返回 {ran: True, steps: [...]}，由调用方发事件留痕。
    构建产物（dist/）由 WEB_ENTRY 的静态挂载对外服务（app/static 同理）。
    """
    import json as _json
    import shutil as _sh
    import subprocess as _sp
    ws = Path(ws)
    pkg = ws / "package.json"
    if not pkg.is_file():
        return {"ran": False, "why": "没有 package.json（不是前端工程项目）"}
    try:
        meta = _json.loads(pkg.read_text(encoding="utf-8-sig", errors="replace"))
    except Exception as exc:                             # noqa: BLE001
        return {"ran": False, "why": f"package.json 解析失败：{type(exc).__name__}"}
    scripts = (meta.get("scripts") or {}) if isinstance(meta, dict) else {}
    npm = _sh.which("npm") or _sh.which("npm.cmd")
    if not npm:
        return {"ran": False, "why": "npm 不在 PATH（需要 Node.js；装好后重跑）"}
    out: dict = {"ran": True, "steps": []}

    def _run(args: list[str], label: str) -> bool:
        try:
            r = _sp.run([npm, *args], cwd=str(ws), capture_output=True, text=True,
                        timeout=timeout, shell=False)
            ok = r.returncode == 0
            out["steps"].append({"stage": label, "ok": ok,
                                 "tail": "" if ok else (r.stdout or r.stderr or "")[-300:]})
            return ok
        except Exception as exc:                         # noqa: BLE001
            out["steps"].append({"stage": label, "ok": False,
                                 "tail": f"{type(exc).__name__}: {exc}"})
            return False

    if not (ws / "node_modules").is_dir():
        _run(["install", "--no-audit", "--no-fund"], "install")
    if str(scripts.get("build") or "").strip():
        _run(["run", "build"], "build")
    return out


def write_web_entry(ws, bp) -> dict:
    """生成/刷新 ASGI 入口 `app/main.py`（确定性、0 token、可溯源）。

    只写**引擎自己的文件**：其它 app/*.py 一律不碰。

    2026-11-10 关键修正：include 列表必须按**实际有路由的模块**来生成，
    不能只按能力表点名的模块。实测的连环错位：
      · 能力表按 `app/api/analytics.py` 规划落点；
      · 但造物模式的契约让远端写的是**业务层** `app/analytics.py`；
      · 接线（`apply_web_wiring`）只渲染**已存在**的文件 → 路由落到 `app/analytics.py`；
      · 入口却 include `app.api.analytics`（不存在）→ **一个路由都没注册** → 全线 404。
    所以这里以**接线产物**（`web_routes.json` 的模块键）为准，能力表只作补充。
    """
    mods = []
    st = _stack(ws)
    # ① 接线产物里的模块（真正带路由的）—— 权威来源
    try:
        _rf = Path(ws) / ".sixcoder" / "web_routes.json"
        if _rf.exists():
            for name in (json.loads(_rf.read_text(encoding="utf-8-sig")) or {}).keys():
                _m = str(name).strip()
                if not _m.endswith(".py") or _m == st.entry_path:
                    continue
                _mod = _m[:-3].replace("/", ".")
                if _mod.startswith(st.module_prefix) and _mod not in mods:
                    mods.append(_mod)
    except Exception:                                    # noqa: BLE001
        pass
    # ② 能力表点名的模块（补充；不存在的入口有 find_spec 保护，会被跳过）
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        for m in (c.modules or []):
            name = str(m or "").strip()
            if not name.endswith(".py") or name == st.entry_path:
                continue
            mod = name[:-3].replace("/", ".")
            if mod.startswith(st.module_prefix) and mod not in mods:
                mods.append(mod)
    # ②.5 **磁盘上真实定义了 router 的模块**（2026-11-10 实测）。
    #
    # 前面两个来源都会漏：接线表只记引擎接管的端点，能力表只记蓝图点名的模块。
    # 远端按提示词自己用 `@router.post` 注册了路由、而那个模块**没被能力表点名**
    # （实测 `app/api/datasets.py` 有 router、注册了 POST /api/v1/datasets/seed，
    #  却不在 include 列表里）→ 该端点永久 404 → 挂在这条链上的 8 条能力
    # （prefs/teacherpref/batch/result 全都靠 seed 造数据）一起红。
    #
    # 口径：**磁盘上注册了 router 就该被 include** —— 入口的作用就是把 router 都装上。
    try:
        for _glob in st.module_globs:
            for _py in sorted(Path(ws).glob(_glob)):
                if _py.name in ("main.py", "__init__.py"):
                    continue
                # Django 的 migrations/ 不是业务模块（2026-11-15）：
                # `apps/**/*.py` 会把 0001_initial.py 一起扫进来 → 被当成候选模块
                # → 把迁移文件里的函数当路由处理器 / 把迁移当入口 include。
                if "/migrations/" in _py.as_posix():
                    continue
                _rel = _py.relative_to(Path(ws)).as_posix()
                _mod = _rel[:-3].replace("/", ".")
                if not _mod.startswith(st.module_prefix) or _mod in mods:
                    continue
                # 只加**真的定义了 router** 的模块：include 一个没有 router 的模块
                # 不会出错（入口有 getattr 检查），但会让入口列表越来越长、越难看出问题。
                try:
                    _src = _py.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                if re.search(r"(?m)^router\s*=\s*" + re.escape(st.router_marker),
                             _src):
                    mods.append(_mod)
    except Exception:                                    # noqa: BLE001
        pass
    root = Path(ws) / st.entry_path
    try:
        root.parent.mkdir(parents=True, exist_ok=True)
        init = root.parent / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
        p = root
        body = st.render_entry(mods)
        if p.exists():
            cur = p.read_text(encoding="utf-8-sig", errors="replace")
            if cur.strip() == body.strip():
                return {"ok": True, "wrote": False, "why": "已是引擎 Web 入口",
                        "modules": sorted(mods)}
        p.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "wrote": False, "why": f"写不了：{exc}"}
    return {"ok": True, "wrote": True, "modules": sorted(mods),
            "why": f"Web 入口由引擎生成（include {len(mods)} 个 router 模块）"}


WEB_ROUTE_BEGIN = "# --- 引擎补的路由接线（本段由引擎生成，手改无效）---"
WEB_ROUTE_END = "# --- 路由接线结束 ---"


ROUTES_FILE = ".sixcoder/web_routes.json"


def _load_routes(ws) -> dict:
    import json as _json
    p = Path(ws) / ROUTES_FILE
    try:
        return _json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except Exception:                                    # noqa: BLE001
        return {}


def _save_routes(ws, reg: dict) -> None:
    import json as _json
    p = Path(ws) / ROUTES_FILE
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


# 名字 → 该补的 import（只放**无歧义**的；`Path`/`Response`/`status` 这类
# 既能来自 fastapi 又能来自 pathlib/starlette 的不猜，避免补错）
_AUTO_IMPORTS = {
    "HTTPException": "from fastapi import HTTPException",
    "APIRouter": "from fastapi import APIRouter",
    "Body": "from fastapi import Body",
    "Header": "from fastapi import Header",
    "Query": "from fastapi import Query",
    "Depends": "from fastapi import Depends",
    "JSONResponse": "from fastapi.responses import JSONResponse",
    "PlainTextResponse": "from fastapi.responses import PlainTextResponse",
    "HTMLResponse": "from fastapi.responses import HTMLResponse",
    "BaseModel": "from pydantic import BaseModel",
    "Field": "from pydantic import Field",
    "sqlite3": "import sqlite3",
    "json": "import json",
    "os": "import os",
    "re": "import re",
    "time": "import time",
    "uuid": "import uuid",
    "threading": "import threading",
    "logging": "import logging",
}


def _ensure_local_imports(ws, src: str, mod: str) -> tuple[str, list[str]]:
    """补**项目内跨模块**的 import（`_models_state` 定义在 app/models.py，却直接用）。

    为什么必须补（实测 2026-11-08）：作者写 `state = _models_state()` 时假定已经 import，
    但文件里一个 import 都没有；闸门是在**引擎事后改写之前**跑的，改写后没有重跑，
    于是带 NameError 的文件落盘 → 探针 500 → 能力永远 absent，还被记成"模型做不出来"。
    只在符号**唯一**定义于另一个模块时才补（多个同名定义就如实不猜）。
    """
    import ast as _ast
    from .gates import _bind_all as _bind
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return src, []
    # 自己算绑定集（gates._bind_all 口径不同，会把 Load 也算进去 → missing 恒为空）
    bound: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Name) and isinstance(node.ctx, (_ast.Store, _ast.Param)):
            bound.add(node.id)
        elif isinstance(node, _ast.arg):
            bound.add(node.arg)
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (_ast.Import, _ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
    used = {n.id for n in _ast.walk(tree)
            if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
    import builtins as _b
    missing = sorted(used - bound - set(dir(_b)))
    if not missing:
        return src, []
    index: dict[str, set] = {}
    root = Path(ws)
    for f in list((root / "app").rglob("*.py")) if (root / "app").is_dir() else []:
        rel = f.relative_to(root).as_posix()
        if rel == mod or f.name == "__init__.py":
            continue
        try:
            t = _ast.parse(f.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in t.body:
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                if not node.name.startswith("__"):
                    index.setdefault(node.name, set()).add(rel)
            elif isinstance(node, _ast.Assign):
                for tg in node.targets:
                    if isinstance(tg, _ast.Name):
                        index.setdefault(tg.id, set()).add(rel)
    adds: list[str] = []
    for name in missing:
        mods = index.get(name) or set()
        if len(mods) == 1:
            m = next(iter(mods))[:-3].replace("/", ".")
            adds.append(f"from {m} import {name}")
    if not adds:
        return src, []
    # 只插在**模块级**第一个非 import 语句之前：按"最后一个 import 行"插会插进函数体
    # （实测 2026-11-08：org.py 的 import 在函数里 → 插入后 IndentationError）
    pos = 0
    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            pos = max(pos, (node.end_lineno or node.lineno))
            continue
        if isinstance(node, _ast.Expr) and isinstance(getattr(node, "value", None), _ast.Constant):
            pos = max(pos, (node.end_lineno or node.lineno))     # 模块 docstring
            continue
        pos = (node.lineno or 1) - 1
        break
    lines = src.splitlines()
    out = lines[:pos] + adds + lines[pos:]
    return "\n".join(out) + ("\n" if src.endswith("\n") else ""), adds


def _ensure_router_decl(src: str) -> tuple[str, list[str]]:
    """作者用了 `@router.xxx` 却**没定义 router** 时，补上 `router = APIRouter()`。

    为什么必须由引擎补（实测 2026-11-10，毕设项目，一轮就烧掉 3 次远端返工）：
      远端按提示词写 `@router.post("/api/v1/xxx")`，
      却忘了写 `router = APIRouter()`（或者那句在 docstring 里、没真执行）。
      → 模块 import 就炸 `NameError: name 'router' is not defined`
      → 契约体检失败 → 远端返工（attempt=1/2/3 都在改同一件事）
      → 引擎接线后冒烟也炸 → `wire_reverted restored=32`（整轮回滚 32 个文件）
      → 该模块的所有能力一起红，而真因只是一行样板。

    口径：这是**确定性**信息（装饰器用了 router ⇒ 必须有 router 对象），
    不该让远端猜、不该让远端为此重写整个文件。

    只在作者区补（引擎段稍后整块重渲染，补进去的会被冲掉）。
    """
    import ast as _ast
    # Django 的 views.py 没有 router 对象（路由在 urls.py 的 path() 里），
    # 补一句 `router = APIRouter()` 只会让文件 import 就炸。
    try:
        if _stack().name != "fastapi":
            return src, []
    except Exception:                                    # noqa: BLE001
        pass
    head, tail = src, ""
    if WEB_ROUTE_BEGIN in src:
        head, tail = src.split(WEB_ROUTE_BEGIN, 1)
        tail = WEB_ROUTE_BEGIN + tail
    try:
        tree = _ast.parse(head)
    except SyntaxError:
        return src, []
    # 已经绑定了 router 就别动
    if "router" in _module_bindings(head):
        return src, []
    # 没用装饰器也不用 router → 不补（别给纯业务模块硬塞一个 router）
    uses_router = False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and getattr(node.value, "id", "") == "router":
            uses_router = True
            break
    if not uses_router:
        return src, []
    adds = ["from fastapi import APIRouter", "", "router = APIRouter()"]
    lines = head.splitlines()
    pos = 0
    for node in tree.body:
        if isinstance(node, (_ast.Import, _ast.ImportFrom)):
            pos = max(pos, (node.end_lineno or node.lineno))
            continue
        if isinstance(node, _ast.Expr) and isinstance(getattr(node, "value", None), _ast.Constant):
            pos = max(pos, (node.end_lineno or node.lineno))     # 模块 docstring
            continue
        # 带装饰器的函数：`node.lineno` 指向 **def 行**，装饰器在更上面。
        # 插到 def 行前会插进"装饰器与 def 之间" → 语法错误
        # （实测 2026-11-10：补 router 时插错位置，模块直接编译不过）。
        _dec = min([getattr(d, "lineno", node.lineno)
                    for d in getattr(node, "decorator_list", [])] or [node.lineno])
        pos = _dec - 1
        break
    out = lines[:pos] + adds + lines[pos:]
    return "\n".join(out) + ("\n" if head.endswith("\n") else "") + tail, ["router"]


def _ensure_imports(src: str, ws=None) -> tuple[str, list[str]]:
    """补上"用了但没 import"的名字（HTTPException 之类）。

    为什么要有（实测 2026-11-07）：作者在服务函数里 `raise HTTPException(...)`，
    却只 import 了 APIRouter/Body/Header → 请求时 `NameError`，整模块所有能力一起红，
    系统还以为是"能力做不出来"。缺哪个 import 是**确定性**信息，不该让模型猜。
    """
    import ast as _ast
    # 只在**作者区**里补：引擎段稍后会被整块重渲染，补进去的 import 会被冲掉
    # （实测 2026-11-07：HTTPException 补进了引擎段 → 渲染后消失 → 还是 NameError）
    head, tail = src, ""
    if WEB_ROUTE_BEGIN in src:
        head, tail = src.split(WEB_ROUTE_BEGIN, 1)
        tail = WEB_ROUTE_BEGIN + tail
    try:
        tree = _ast.parse(head)
    except SyntaxError:
        return src, []
    have: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            for a in node.names:
                have.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, _ast.ImportFrom):
            for a in node.names:
                have.add(a.asname or a.name)
    used = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name)}
    used |= {n.attr for n in _ast.walk(tree) if isinstance(n, _ast.Attribute)}
    # §20.7 接缝 5：import 补齐表由当前栈给（FastAPI 与旧 `_AUTO_IMPORTS` 同源，
    # 默认 ws=None 时也取 FastAPI → 老调用点零行为变化）。
    add = [stmt for name, stmt in _stack(ws).auto_import_table().items()
           if name in used and name not in have]
    if not add:
        return src, []
    lines = head.splitlines()
    last_import = 0
    for i, ln in enumerate(lines[:80], 1):
        t = ln.strip()
        if t.startswith("import ") or t.startswith("from "):
            last_import = i
    pos = last_import if last_import else 0
    out = lines[:pos] + add + lines[pos:]
    return "\n".join(out) + "\n" + tail, add


def _strip_author_routes(src: str, owned: set) -> tuple[str, list[str]]:
    """删掉作者自己写的、**引擎已经负责**的那些路由装饰器（保留函数本体）。

    为什么必须管（实测 2026-11-07）：作者会按"Web 项目常识"给服务函数挂
    `@router.post("/tickets", response_model=Dict[str, str])`，于是
      · 和引擎生成的路由**重复注册**，先注册的（作者的）生效 → 状态码/请求体都不对；
      · `response_model` 把 int 的 id 当 str 校验 → 500。
    引擎只为"自己有探针契约"的端点接管路由，作者多写的其它路由不动。
    """
    import ast as _ast
    if not owned:
        return src, []
    # 路径**归一**后比对（§11.4g）：owned 里可能是探针路径 `/api/v1/topics/999999`，
    # 作者装饰器里可能是 `/api/v1/topics/{topic_id}` —— 不归一就永远对不上，
    # 于是"引擎已接管的端点"在作者文件里留着一份抢先注册的路由。
    from . import routefuncs as _rfm_s
    try:
        _own = {(str(v).upper(), _rfm_s.normalize(p)) for (v, p) in owned}
    except Exception:                                # noqa: BLE001
        _own = {(str(v).upper(), str(p)) for (v, p) in owned}
    head, tail = src, ""
    if WEB_ROUTE_BEGIN in src:                    # 只处理作者区，引擎段稍后整块重渲染
        head, tail = src.split(WEB_ROUTE_BEGIN, 1)
        tail = WEB_ROUTE_BEGIN + tail
    try:
        tree = _ast.parse(head)
    except SyntaxError:
        return src, []
    drop: list[int] = []
    removed: list[str] = []
    for node in tree.body:
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, _ast.Call) or not isinstance(dec.func, _ast.Attribute):
                continue
            if getattr(dec.func.value, "id", "") not in ("router", "app"):
                continue
            verb = dec.func.attr.lower()
            if not dec.args or not isinstance(dec.args[0], _ast.Constant):
                continue
            if not isinstance(dec.args[0].value, str):
                continue
            path = _fastapi_path(dec.args[0].value)
            if (verb.upper(), _rfm_s.normalize(path)) in _own:
                drop.extend(range(dec.lineno, (dec.end_lineno or dec.lineno) + 1))
                removed.append(f"{verb.upper()} {path}")
    if not drop:
        return src, []
    lines = head.splitlines()
    for ln in sorted(set(drop), reverse=True):
        if 1 <= ln <= len(lines):
            del lines[ln - 1]
    return "\n".join(lines) + "\n" + tail, removed


def _py_type(v) -> str:
    """按样本值猜 pydantic 类型（用于生成请求体模型，让校验成为 FastAPI 的默认行为）。"""
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return "str"


def _strip_engine_leftovers(src: str) -> str:
    """删掉"被作者抄出引擎段"的残留：`_engine_*` 函数与 `_Body_*` 模型。

    实测（2026-11-07）：作者看到引擎生成的路由 handler，会把它当成模块的一部分
    **复制到自己的代码里**（还丢了 `Body/Header` 的 import）→ `NameError: Body`，
    整个 app 导入失败、15 条能力一起红。这些名字是引擎专用的（`_engine_`/`_Body_`），
    清掉不会伤到作者的东西。
    """
    import ast as _ast
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return src
    drop: list[tuple[int, int]] = []
    for node in tree.body:
        name = getattr(node, "name", "")
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            continue
        if not (name.startswith("_engine_") or name.startswith("_Body_")):
            continue
        start = min([getattr(d, "lineno", node.lineno) for d in
                     getattr(node, "decorator_list", [])] + [node.lineno])
        drop.append((start, node.end_lineno or node.lineno))
    if not drop:
        return src
    lines = src.splitlines()
    for a, b in reversed(sorted(drop)):
        del lines[a - 1:b]
    return "\n".join(lines) + "\n"


def _is_asgi_entry(module: str, ws=None) -> bool:
    """是不是 ASGI 入口。判定走 entrypoint 探测（写死的 app/main.py 永远算，
    给了 ws 再以实际入口为准）——入口不参与路由接线。"""
    from . import entrypoint as _ep
    return _ep.is_entry(module, ws)


def _module_bindings(src: str) -> set[str]:
    """模块级**真正绑定**的名字（import / 赋值 / def / class / try 里的 import）。

    为什么不能用正则：作者写 `router = APIRouter()` 时常常先把这句话写进模块 docstring
    当说明（实测 2026-11-07），`\\bAPIRouter\\b` 命中 docstring → 引擎以为已经 import 了
    → 生成的引擎段缺 `from fastapi import APIRouter` → 应用一 import 就 NameError，
    整片 Web 能力一起红，而系统还以为"能力做不出来"。
    """
    import ast as _ast
    try:
        tree = _ast.parse(src)
    except SyntaxError:
        return set()
    names: set[str] = set()

    def visit(nodes) -> None:
        for node in nodes:
            if isinstance(node, _ast.Import):
                for a in node.names:
                    names.add((a.asname or a.name).split(".")[0])
            elif isinstance(node, _ast.ImportFrom):
                for a in node.names:
                    if a.name != "*":
                        names.add(a.asname or a.name)
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                names.add(node.name)          # 不进去：函数内的局部名不算模块级绑定
            elif isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        names.add(t.id)
            elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
                names.add(node.target.id)
            elif isinstance(node, _ast.Try):
                visit(node.body)
                visit(node.orelse)
                visit(node.finalbody)
                for h in node.handlers:
                    visit(h.body)
            elif isinstance(node, _ast.If):
                visit(node.body)
                visit(node.orelse)
            elif isinstance(node, _ast.With):
                visit(node.body)

    visit(tree.body)
    return names


def _foreign_id_param(pn: str, path: str, method: str) -> bool:
    """这个 `*_id` 参数是外键/归属（不是主键）吗？

    实测 2026-11-09：`submit_topic(body, teacher_id)` 挂在 `POST /api/v1/topics` 上，
    路径里没有 id，旧规则一律 bad → 整条路由不接线 → 探针 404 → 主干能力反复失败
    被 descope → 一整夜 7 次"覆盖停滞"replan。

    判据（结构性的，不认业务词）：
      · 参数词干（teacher_id → teacher）== 路径资源（topics → topic）→ **主键**，必须真有值
      · 不相等、且是创建型 POST/PUT → **归属/外键**，可给确定性默认值（只记录归属，
        不会去操作一条不存在的记录）
    `batch_id` 对 `POST /batches` 是主键 → 仍然拒绝（保住 2026-11-07 那次修复）。
    """
    import re as _re
    stem = str(pn or "").lower()
    stem = stem[:-3] if stem.endswith("_id") else stem
    raw = [x for x in str(path or "").split("?", 1)[0].strip("/").split("/") if x]
    clean: list[str] = []
    param_at = -1
    for x in raw:
        if x.startswith("{") or x.startswith("$"):
            if param_at < 0:
                param_at = len(clean)
            continue
        low = x.lower()
        if low in ("api", "rest", "internal") or _re.fullmatch(r"v\d+", low):
            continue
        clean.append(low)
    # 有参数段时，资源是**参数段前面**那一段（/batches/{id}/run → batches），
    # 不是最后一段（run 是动作）。没有参数段时取最后一段。
    if param_at > 0:
        res = clean[param_at - 1]
    elif clean:
        res = clean[-1]
    else:
        res = ""
    # 去复数要覆盖 -ches（batches→batch），只去 s 会得到 batche → 主键被误判成外键
    if len(res) > 4 and res.endswith("ies"):
        res = res[:-3] + "y"
    elif len(res) > 4 and res.endswith(("ches", "shes", "sses", "xes", "zes")):
        res = res[:-2]
    elif len(res) > 3 and res.endswith("s") and not res.endswith(("ss", "us", "is")):
        res = res[:-1]
    if stem in ("", "id", "pk") or stem == res:
        return False
    return str(method or "").upper() in ("POST", "PUT", "PATCH")


def _block_routes(block: str) -> list[str]:
    """旧引擎段里**实际注册了**哪些路由（"METHOD /path" 形式）。

    用途：接线表要和文件里真实注册的路由一致（渲染失败的端点不许留在表里）。
    """
    out: list[str] = []
    for m in re.finditer(r"@router\.(\w+)\(\s*[\"']([^\"']+)[\"']", str(block or "")):
        _v, _p = m.group(1).upper(), _fastapi_path(m.group(2))
        _k = f"{_v} {_p}"
        if _p and _k not in out:
            out.append(_k)
    return out


def _func_param_defaults(head: str, fn: str) -> tuple[set[str], bool]:
    """函数签名里**哪些参数有默认值** + 是否收 **kwargs。

    用途：接线时判断"这个参数能不能不传"。
    能不传（有自己的默认值）→ 不传，让函数用自己的默认，比引擎塞空串诚实；
    不能不传且 body 里也没有 → 这条路由**不接**（404），别硬接出一个语义错误的路由。

    **注意**：FastAPI 的**声明式默认值**（`Body(...)` / `Header(...)` / `Form(...)`）
    不算"业务默认值" —— 它们是"这个参数从哪来"的声明，不传等于**把请求体整个丢掉**
    （实测 2026-11-11：`body: dict = Body(default=None)` 被当成可省略 →
    引擎调用 `login_account(authorization=...)` 不传 body → 远端收到 `body=None`
    → ValueError → **422** → 全线 POST 能力一起红）。
    这类参数由 `_fastapi_decl_params` 单独识别。
    """
    import ast as _ast
    try:
        tree = _ast.parse(str(head or ""))
    except SyntaxError:
        return set(), False
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if node.name != fn:
            continue
        defaults: set[str] = set()
        _args = list(node.args.args)
        _ndef = len(node.args.defaults)
        if _ndef:
            for a in _args[-_ndef:]:
                defaults.add(a.arg)
        return defaults, bool(node.args.kwarg)
    return set(), False


def _fastapi_decl_params(head: str, fn: str) -> tuple[set[str], set[str]]:
    """函数参数里，哪些是 **FastAPI 声明式**的（Body/Form/Header/Query…）。

    返回 (body 类参数, 其它声明类参数)。
    - body 类（Body/Form）：**必须传**整个请求体 —— 不传等于把请求体丢掉；
    - 其它（Header/Query/Path）：按引擎既有的规则供给（authorization 等）。

    为什么必须单独识别（2026-11-11 实测）：`body: dict = Body(default=None)`
    在"哪些参数有默认值"的眼里是**可省略**的 → 引擎不传 → 远端收到 `None`
    → 抛 ValueError → 422。**声明 ≠ 默认值。**
    """
    import ast as _ast
    body_set: set[str] = set()
    other_set: set[str] = set()
    try:
        tree = _ast.parse(str(head or ""))
    except SyntaxError:
        return body_set, other_set
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        if node.name != fn:
            continue
        _args = list(node.args.args)
        _ndef = len(node.args.defaults)
        for a, d in zip(_args[-_ndef:] if _ndef else [], node.args.defaults):
            _callee = None
            if isinstance(d, _ast.Call) and isinstance(d.func, _ast.Name):
                _callee = d.func.id
            elif isinstance(d, _ast.Attribute):
                _callee = d.attr
            if not _callee:
                continue
            if _callee in ("Body", "Form"):
                body_set.add(a.arg)
            elif _callee in ("Header", "Query", "Path", "Cookie", "FormData"):
                other_set.add(a.arg)
        break
    return body_set, other_set


def _engine_block_targets(block: str) -> list[str]:
    """旧引擎段里的 handler 调用了**哪些作者函数**（按出现顺序，去重）。

    用途：判断旧段是否**仍然有效**。引擎段的 handler 长这样：
        def _engine_post_api_v1_auth_register(...):
            ...
            return register_user(username=..., password=..., role=...)
    `register_user` 在作者区。作者还留着它 → 旧路由还能用；作者删了它 →
    保留旧段就会变成"请求时 NameError"，那时才该清掉。

    只认 `return <名字>(` 这一种形态：引擎段的 handler 一定是直接转调作者函数。
    """
    out: list[str] = []
    for m in re.finditer(r"(?m)^\s*return\s+([A-Za-z_]\w*)\s*\(", str(block or "")):
        _fn = m.group(1)
        if _fn and _fn not in out:
            out.append(_fn)
    return out


def _stack_route_spec(**kw):
    """把接线阶段算出的栈无关事实装成 `stack.RouteSpec`。

    延迟 import：避免 core/__init__.py 加载顺序上出现循环依赖。
    """
    from .stack import RouteSpec
    return RouteSpec(**kw)


def _wire_call(head: str, fn: str, path: str, bkeys: set, funcs: dict,
               method: str = "GET") -> tuple[list[str], list[str]]:
    """路由**怎么调**这个函数 → (args 片段, 接不上的参数名)。

    **单一实现**（2026-11-11，§11.4g）：`ensure_web_router`（渲染时）与
    `_locate_route`（选落点时）共用这一套判据。
    之前两处各写一套 → 选落点挑了 `create_topic_route(body, user)`
    （`user` 这个参数在路由层供给不了 → 渲染时整条路由被拒 → 404），
    而真正能接上的 `create_topic(body)` 根本没被选中 → 端到端永远红。

    `bad` 非空 = 这个函数**不能**当路由处理器（宁可 404，也不硬接 ——
    硬接会"注册成功但语义是另一件事"，比 404 更坏，引擎看不出真相）。
    """
    pvars = re.findall(r"\{(\w+)\}", path)
    nums = re.findall(r"/(\d+)(?=/|$)", path)
    _defaults, _has_kwargs = _func_param_defaults(head, fn)
    # FastAPI **声明式**参数（Body/Form/Header…）：声明 ≠ 默认值
    # （实测 2026-11-11：`body: dict = Body(default=None)` 被当成可省略 →
    # 引擎不传 body → 远端收到 None → ValueError → 422 → 全线 POST 一起红）
    _fa_body, _fa_other = _fastapi_decl_params(head, fn)
    args: list[str] = []
    bad: list[str] = []
    for pn in (funcs.get(fn) or []):
        if pn in pvars:
            args.append(f"{pn}=_to_int({pn})")
        elif pn in ("authorization", "token", "auth"):
            args.append(f"{pn}=authorization")
        elif pn in bkeys:
            args.append(f'{pn}=_b.get("{pn}")')
        elif pn in _fa_body:
            # **声明式请求体**：必须把整个 body 传过去
            args.append(f"{pn}=_b")
        elif pn in _defaults:
            # 函数自己给这个参数备了默认值 → **不传**。
            #
            # 旧口径塞空串（`pn=""`），实测 2026-11-10 被远端架构师当场点破：
            # 「引擎生成的路由接线把业务函数入参写死成空串/示例常量
            #   （如 publish_topic(current_status="", students="", …））」
            # → 路由"注册成功"但语义完全是另一件事：当前状态永远是空串、
            # 学生列表永远是空 → 业务判断全错 → 能力永远红，而引擎以为是"没写对"。
            # 让函数用自己的默认值，比塞一个假值**诚实得多**。
            continue
        elif pn.endswith("_id") or pn in ("id", "pk"):
            # 只接**路径里真有的** id：路径里有唯一占位符就用它，有数字段就用数字。
            # 都没有却硬塞 1（旧行为），等于拿一条不存在/不相干的数据去跑 —— 实测
            # 2026-11-07：`POST /api/v1/batches`（带 4 个请求体字段的创建路由）被接到
            # `run_batch_endpoint(batch_id=1)` 上，路由"注册成功"但语义完全是另一件事，
            # 失败报告还指着一个作者没写过的函数。宁可不接（404），也不硬接。
            if len(pvars) == 1:
                args.append(f"{pn}=_to_int({pvars[0]})")
            elif nums:
                args.append(f"{pn}={int(nums[0])}")
            elif _foreign_id_param(pn, path, method):
                # 外键/归属（teacher_id 之于 POST /topics）：给确定性默认值。
                # 只用于记录归属，不会拿不存在的记录去操作；不接就会 404。
                args.append(f"{pn}=1")
            else:
                bad.append(pn)
        elif pn in ("request", "payload", "data", "body"):
            # 收原始请求体的函数：传**整个已解析 body**（_b 在包装器里必有定义）。
            # 旧口径塞空串（实测 2026-11-08 13:10：create_candidates(request="")
            # → recover_request_body("") 为假 → ValueError，LLM 能力永远 absent）。
            args.append(f"{pn}=_b")
        elif _has_kwargs:
            # 函数收 **kwargs → 把整个 body 展开给它（它自己决定收哪些键）
            args.append(f"**{_b}")
        elif not any(k in pn.lower() for k in
                     ("file", "path", "url", "dir", "db", "conn")):
            # 兜底：**不接**这条路由。
            #
            # 旧口径塞空串（`pn=""`）—— 那等于"注册成功但语义是另一件事"：
            # 路由能通、数据全错，比 404 更坏（引擎看不出真相，永远修不好）。
            # 宁可 404（显式、指向"这条路没人写"），也不硬接。
            bad.append(pn)
        else:
            bad.append(pn)
    return args, bad


def ensure_web_router(ws, module: str, routes: list[dict], funcs: dict[str, list[str]],
                      full: bool = True, touched: list | None = None) -> dict:
    """把模块里的路由段**整块重渲染**成注册表的投影（确定性，不追加）。

    路由形状（方法/路径/状态码/请求体字段）完全由探针决定，是接线不是设计判断，
    所以交给引擎；作者只写服务层函数。

    三条硬约束（都踩过）：
      · 必须**剥掉所有**旧的引擎段再写：只剥第一段会让同一路由出现两次（2026-11-07）；
      · 静态路径排在 `{param}` 之前：否则 `/tickets/{id}` 会吃掉 `/tickets/search` → 405；
      · 参数接不上就不要接：硬接会拿 None 去调，报一个和作者无关的 TypeError。

    **touched**（§11.4m 新增）：传一个 list 进来，本模块被改写时把模块路径填进去
      —— 接线层的改动**不经过作者写码路径的回归守卫**，调用方要靠它复验。
    """
    _touched = touched if isinstance(touched, list) else None
    p = Path(ws) / module
    if not p.is_file():
        return {"ok": False, "wrote": False, "why": "模块还没写出来"}
    if _is_asgi_entry(module, ws):
        # 入口文件由 write_web_entry 整体生成（只 include 各模块的 router），**不是**路由
        # 模块。往它里面追加 `router = APIRouter()` 就是往 ASGI 入口里塞一个没有 import 的
        # 名字：`import app.main` 直接 NameError，所有 Web 能力一起红（实测 2026-11-07）。
        return {"ok": True, "wrote": False, "why": "入口文件由引擎整体生成，不挂路由段"}
    # 注意：routes 为空也要继续 —— 函数被作者删掉时，旧引擎段里还留着指向它们的
    # `@router.*`，不清掉就是"请求时 NameError"（实测 2026-11-07，6 条能力一起红）。
    try:
        src = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return {"ok": False, "wrote": False, "why": f"读不了：{exc}"}
    original = src

    old_blocks: list[str] = []
    while WEB_ROUTE_BEGIN in src:                        # 剥**干净**（可能有多段残留）
        _h, _rest = src.split(WEB_ROUTE_BEGIN, 1)
        if WEB_ROUTE_END in _rest:
            _blk, _rest = _rest.split(WEB_ROUTE_END, 1)
            old_blocks.append(_blk)
        else:
            old_blocks.append(_rest)
            _rest = ""
        src = _h + _rest
    src = _strip_engine_leftovers(src)
    head = src.rstrip()

    routes = sorted(routes, key=lambda r: (0 if "{" not in str(r.get("path") or "") else 1,
                                           -len(str(r.get("path") or "")),
                                           str(r.get("path") or "")))
    body: list[str] = []
    specs: list = []
    _ad = _stack(ws)                     # §20.7 D 档：按栈取渲染器（默认 fastapi）
    seen: set = set()
    n_routes = 0
    skipped: list[str] = []
    for r in routes:
        fn = str(r.get("func") or "")
        path = str(r.get("path") or "")
        method = str(r.get("method") or "GET").lower()
        if not fn or not path or (method, path) in seen:
            continue
        if not re.search(rf"^\s*def\s+{re.escape(fn)}\s*\(", head, re.M):
            skipped.append(f"{method} {path}（缺函数 {fn}）")
            continue
        pvars = re.findall(r"\{(\w+)\}", path)
        bkeys = set(r.get("body_keys") or [])
        args, bad = _wire_call(head, fn, path, bkeys, funcs, method)
        if bad:
            skipped.append(f"{method} {path}（参数无从提供：{','.join(bad)}）")
            continue
        seen.add((method, path))
        n_routes += 1
        name = re.sub(r"[^a-z0-9]+", "_", f"{method}_{path}".lower()).strip("_")
        # §20.7 接缝 2：把「绑哪个函数、实参怎么传、请求体字段」等**栈无关事实**
        # 装进 RouteSpec，交给栈适配器渲染。FastAPI 适配器的输出与改动前的内联
        # 渲染逐字一致（checks/check_stack.py 有逐字对比断言钉死）。
        spec = _stack_route_spec(method=method, path=path, func=fn, args=args,
                                 body_keys=sorted(bkeys),
                                 body_types=r.get("body_types") or {},
                                 body_required=r.get("body_required") or [],
                                 body_sample=r.get("body_sample") or {},
                                 status=r.get("status"), path_params=pvars, slug=name)
        specs.append(spec)
        body += _ad.render_route(spec)

    if not body:
        # 一条路由都接不上。
        #
        # **但旧引擎段不能就这么清掉**（实测 2026-11-10，毕设项目）：
        #   日志原文「app/api/rules.py（无可接路由，清掉旧引擎段；跳过 1 条）」
        #   → 那一轮 `_locate_route` 没能给某个端点找到匹配函数（异名场景：
        #     端点是 /api/v1/rules/preview，作者的函数叫 preview_rules）→
        #     引擎把该模块**整块路由清空** → 这个模块的所有端点当场 404 →
        #     观测全红 → 引擎以为是"功能没写" → 把工作单派回远端 → 远端改 →
        #     下一轮又被清空。**覆盖永远上不去，而每轮都在烧钱。**
        #
        # 口径：**旧路由只要还指向活着的函数，就保留**。
        # 旧段被清空只在该段确实已经失效（作者把函数删了）时才做 ——
        # 那时保留会变成"请求时 NameError"，同样是坏的。
        _old = "\n".join(old_blocks)
        _targets = _engine_block_targets(_old)
        _alive = [f for f in _targets
                  if re.search(rf"^\s*def\s+{re.escape(f)}\s*\(", head, re.M)]
        if _targets and len(_alive) == len(_targets):
            _keep = _old
            if "router" in _module_bindings(head):
                # 作者区已经有 router 了：旧段里那行 `router = APIRouter()` 会**重新赋值**，
                # 把作者自己注册的路由（挂在作者的 router 上）整个顶掉。
                # 去掉它，旧 handler 就挂到作者的 router 上，两边共存。
                _keep = re.sub(r"(?m)^router\s*=\s*APIRouter\(\)[ \t]*$", "", _keep)
            text = head + "\n\n" + WEB_ROUTE_BEGIN + _keep + WEB_ROUTE_END + "\n"
            try:
                p.write_text(text, encoding="utf-8")
                if _touched is not None and module not in _touched:
                    _touched.append(module)          # §11.4m
            except OSError as exc:
                return {"ok": False, "wrote": False, "why": f"写不了：{exc}"}
            return {"ok": True, "wrote": True, "routes": 0, "skipped": skipped,
                    "rendered": _block_routes(_keep),
                    "why": f"本轮没接上，但旧路由仍有效，保留 {len(_targets)} 条"}
        # 旧段确实失效了（作者把函数删了）→ 清掉，否则请求时 NameError。
        text = head + "\n"
        if text == original:
            return {"ok": True, "wrote": False, "why": "无可接路由（引擎段已干净）",
                    "routes": 0, "skipped": skipped}
        try:
            p.write_text(text, encoding="utf-8")
            if _touched is not None and module not in _touched:
                _touched.append(module)              # §11.4m
        except OSError as exc:
            return {"ok": False, "wrote": False, "why": f"写不了：{exc}"}
        return {"ok": True, "wrote": True, "routes": 0, "skipped": skipped,
                "why": f"无可接路由，清掉旧引擎段；跳过 {len(skipped)} 条"}

    block = [WEB_ROUTE_BEGIN]
    bound = _module_bindings(head)
    # §20.7 接缝 2：import 头 / router 声明由栈适配器给（FastAPI 与旧内联逐字一致）
    block += _ad.route_prelude(specs, bound)
    block += body + [WEB_ROUTE_END]
    text = head + "\n\n" + "\n".join(block) + "\n"
    if text == original:
        return {"ok": True, "wrote": False, "why": "路由段已是最新", "routes": n_routes,
                "skipped": skipped,
                "rendered": sorted(f"{m} {p}" for (m, p) in seen)}
    try:
        p.write_text(text, encoding="utf-8")
        if _touched is not None and module not in _touched:
            _touched.append(module)                  # §11.4m
    except OSError as exc:
        return {"ok": False, "wrote": False, "why": f"写不了：{exc}"}
    return {"ok": True, "wrote": True, "routes": n_routes, "skipped": skipped,
            "rendered": sorted(f"{m} {p}" for (m, p) in seen),
            "why": f"{n_routes} 条路由（静态优先）；跳过 {len(skipped)} 条"}


def ensure_entry_wiring(ws, entry: str) -> dict:
    """入口脚本必须能直接 `python entry.py …` 跑起来：缺 `__main__` 接线就机械补 1 行。

    为什么由引擎补（实测 2026-11-06，run 11）：远端把 `def main(argv)` 写对了，但**没写
    `if __name__ == "__main__"`** → 直接跑什么都不输出 → 6 条 CLI 能力反复失败被降级，
    而纯函数层 7/7 全绿。这是确定性的一行样板，让远端反复试是烧钱；本地助手该做的就是
    把它补上，并在报告里写清"引擎补了哪一行"（可溯源、不隐瞒）。
    """
    import ast as _ast
    p = Path(ws) / str(entry or "")
    if not p.is_file() or p.suffix != ".py":
        return {"ok": False, "patched": False, "why": f"入口不存在：{entry}"}
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return {"ok": False, "patched": False, "why": f"读不了：{exc}"}
    if "__name__" in text and "main(" in text:
        return {"ok": True, "patched": False, "why": "已有接线"}
    try:
        tree = _ast.parse(text)
    except SyntaxError as exc:
        return {"ok": False, "patched": False, "why": f"语法错误：{exc.msg}"}
    main_fn = next((n for n in tree.body
                    if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                    and n.name == "main"), None)
    if main_fn is None:
        return {"ok": False, "patched": False, "why": "没有模块级 main()"}
    nargs = len([a for a in main_fn.args.args if a.arg not in ("self", "cls")])
    call = "main(_sys.argv[1:])" if nargs >= 1 else "main()"
    tail = ("\n\n\nif __name__ == \"__main__\":\n"
            "    import sys as _sys\n\n"
            f"    raise SystemExit({call})\n")
    try:
        p.write_text(text.rstrip() + tail, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "patched": False, "why": f"写不了：{exc}"}
    return {"ok": True, "patched": True, "why": f"引擎补了 __main__ 接线（{call}）"}


def trim_contract(text: str, keep: list[str] | None = None, *,
                  drop: list[str] | None = None) -> str:
    """裁掉**不该出现在本片契约里**的模块。

    两种用法：
      · `keep=[...]`  只保留名单里的模块（激进，早先用在 CLI 片上）；
      · `drop=[...]`  只丢名单里的模块（保守，现在推荐）。

    为什么改成保守（实测 exp1b）：`keep` 白名单来自能力的 `modules` 提示，而那个提示
    常常只写了入口文件名 → 把真正要写的模块（logparse.py）**裁掉了**，远端永远写不出它，
    一片接一片空转。现在只丢"**已经通过**、且本片不需要"的模块：既避免重写把绿能力改坏，
    也不会删掉该写的活。
    """
    import re as _re
    if not text or (keep is None and drop is None):
        return text
    head_re = _re.compile(r"^\s*\d+\)\s*([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))")

    def base(p: str) -> str:
        return str(p or "").replace("\\", "/").split("/")[-1]

    drop_set = {base(d) for d in (drop or []) if d}
    keep_set = {base(k) for k in (keep or []) if k}
    out: list[str] = []
    dropping = False
    for ln in text.splitlines():
        m = head_re.match(ln)
        if m:
            name = base(m.group(1))
            dropping = (name in drop_set) if drop is not None else (name not in keep_set)
        if not dropping:
            out.append(ln)
    return "\n".join(out).strip()


def _contract_modules(text: str) -> list[str]:
    import re as _re
    head_re = _re.compile(r"^\s*\d+\)\s*([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))")
    out: list[str] = []
    for ln in (text or "").splitlines():
        m = head_re.match(ln)
        if m and m.group(1) not in out:
            out.append(m.group(1))
    return out


def align_probe_modules(bp, text: str) -> list[str]:
    """把 `py_call` 探针里的模块名**对齐到契约里实际写的模块路径**。

    实测（exp1f）：能力表写的是 `logscan_lib.parser`（目标句里有"库模块要拆开"），
    而契约/作者落的是根目录 `parser.py` → 探针永远 `ModuleNotFoundError`，
    同一条能力连着六跑全废。探针指哪、作者写哪，必须由同一份契约决定：
    按"文件名（stem）相同"做机械对齐，并记录改了什么（进备注，可溯源）。
    """
    mods = _contract_modules(text)
    by_stem = {Path(m).stem: m for m in mods}
    changed: list[str] = []
    for c in bp.caps:
        p = c.probe if isinstance(c.probe, dict) else {}
        if str(p.get("kind") or "") != "py_call":
            continue
        cur = str(p.get("module") or "")
        stem = cur.replace(".", "/").split("/")[-1]
        want_path = by_stem.get(stem)
        if not want_path:
            continue
        want = want_path.replace("\\", "/")
        if want.endswith(".py"):
            want = want[:-3]
        want = want.replace("/", ".")
        if want != cur:
            p["module"] = want
            changed.append(f"{c.id}: {cur} → {want}")
    return changed


def prep_lines(probe: dict) -> list[str]:
    """把探针的 `files` 素材变成示例里的**前置准备行**（造夹具）。

    为什么必须造夹具（实测 2026-11-06）：验收命令是 `csvstat.py --file sales.csv …`，
    而函数级判据在**空的临时目录**里跑 —— 没有 sales.csv，正确实现也会返回 1，
    于是"得到 1，应为 0"，两版都被判红。夹具必须由示例自己造出来。
    """
    import json as _json
    out: list[str] = []
    for name, content in ((probe or {}).get("files") or {}).items():
        out.append(f"open({_json.dumps(str(name))}, 'w', encoding='utf-8')"
                   f".write({_json.dumps(str(content))})")
    return out


def entry_block(probes) -> list[list[str]]:
    """把这一片**所有** CLI 探针变成**一组有序示例块**（每组自洽：造夹具→抓 stdout→跑 main→断言）。

    三条实测教训（2026-11-06）：
      ① 只断言 `main(...) == 0` 时，命令能跑但输出文本错的能力照样全绿
         → 必须连 stdout 一起断言（重定向到缓冲区再比包含关系）。
      ② 只注入**第一个** CLI 探针的示例时，作者按文档把 main 写完却撞上别的能力的期望
         → 这一片所有 CLI 探针都要注入（它们共用同一个 main）。
      ③ 多个探针的素材常重名（都叫 d.csv），后写的会覆盖先写的 → 素材按探针编号改名，
         argv 里对应的文件名一起改（文件名对被测程序是不透明的，语义不变）。
      ④ 每组的"恢复 stdout"必须紧跟在自己的 main 调用后面：重定向是嵌套的，
         两组的 enter 叠在一起时 main 的输出会进错缓冲区（实测踩到）。
    """
    if isinstance(probes, dict):
        probes = [probes]
    out: list[list[str]] = []
    for i, p in enumerate([x for x in (probes or [])
                           if isinstance(x, dict) and str(x.get("kind") or "") == "cli"]):
        files = p.get("files") or {}
        rename = {str(n): f"_p{i}_{str(n)}" for n in files}
        argv = [rename.get(str(a), a) for a in (p.get("args") or [])]
        code = p.get("expect_exit")
        has = p.get("stdout_has")
        needles = [has] if isinstance(has, str) else list(has or [])
        grp: list[str] = []
        for name, content in files.items():
            grp.append(f"open({rename[str(name)]!r}, 'w', encoding='utf-8')"
                       f".write({__import__('json').dumps(str(content))})")
        if needles:
            grp += [f"_buf{i} = __import__('io').StringIO()",
                    f"_rd{i} = __import__('contextlib').redirect_stdout(_buf{i})",
                    f"_rd{i}.__enter__()"]
        # 没写 expect_exit 就**不判退出码**（错误路径与成功路径都会存在），
        # 只把 main 调一次 —— argparse 拒绝参数会抛 SystemExit，照样判红。
        grp.append(f"main([{', '.join(repr(str(a)) for a in argv)}])"
                   + (f" == {int(code)}" if code is not None else ""))
        if needles:
            grp += [f"_rd{i}.__exit__(None, None, None)",
                    f"_txt{i} = _buf{i}.getvalue()"]
            grp += [f"({n!r} in _txt{i}) == True" for n in needles]
        out.append(grp)
    return out


def align_entry_module(text: str, entry: str) -> str:
    """契约里没有探针指定的入口文件时，把入口模块改名对齐。

    实测（2026-11-06 run 11）：探针用 `csvstat.py` 跑，契约却把入口造在 `main.py` 里
    → 真实命令行永远跑不通（文件不存在），函数级判据也判错了模块。名字不是创意空间：
    探针的 entry 就是硬约定，契约必须跟着它走。
    """
    import re as _re
    if not text or not entry:
        return text
    lines = text.splitlines()
    head_re = _re.compile(r"^(\s*)(\d+\)\s*)([\w./\\-]+\.(?:py|java|js|ts|go|rs|c|cpp|cs|kt|php|rb))(.*)$")
    heads = [(i, m) for i, ln in enumerate(lines) for m in [head_re.match(ln)] if m]
    if not heads or any(m.group(3).split("/")[-1].split("\\")[-1] == entry for _i, m in heads):
        return text
    # 约定：入口模块是"唯一含 main( 的那个"，否则是第一个模块
    target = heads[0][0]
    for i, m in heads:
        end = next((j for j, _m in heads if j > i), len(lines))
        if any("main(" in ln for ln in lines[i:end]):
            target = i
            break
    m = head_re.match(lines[target])
    if not m:
        return text
    old = m.group(3)
    lines[target] = f"{m.group(1)}{m.group(2)}{entry}{m.group(4)}"
    # 其它模块里对旧入口的 import/引用一并改掉（contract 文本里只可能出现名字）
    stem_old, stem_new = Path(old).stem, Path(entry).stem
    for i, ln in enumerate(lines):
        if i == target:
            continue
        if stem_old and stem_old in ln and ")" in ln:
            lines[i] = ln.replace(f"import {stem_old}", f"import {stem_new}")
    return "\n".join(lines)


def ensure_example(text: str, entry: str, ex: str, prep: list[str] | None = None,
                   post: list[str] | None = None,
                   groups: list[list[str]] | None = None) -> str:
    """把入口脚本的验收示例（夹具 + main 调用 + 输出断言）写进契约的"示例"段。

    为什么非做不可（实测 2026-11-05/06）：探针要求 `--file/--group/--value`，作者写成位置参数；
    契约里只有函数级示例 → 内循环判 20/20、真验收 0/8。**两套判据不一致时，
    唯一可靠的办法是让真验收那条调用出现在函数级判据里**（然后 props 会真的去执行它）。

    `groups`：多组完整示例（每条 CLI 探针一组，顺序即执行顺序）；给 `ex/prep/post`
    时自动合成一组（老调用方式仍然可用）。
    """
    if not text or not entry:
        return text
    if not groups:
        if not ex:
            return text
        groups = [list(prep or []) + [ex] + list(post or [])]
    wanted = [ln for g in groups for ln in g if ln]
    if not wanted or all(ln in text for ln in wanted):
        return text
    anchor = next((ln for ln in wanted if "main([" in ln), wanted[0])
    lines = text.splitlines()
    start, end = _entry_block(lines, entry)
    if start < 0:
        return text
    for j in range(start + 1, end):
        if lines[j].strip() == "示例（必须满足）：":
            nxt = j + 1
            missing = [ln for ln in wanted if ln not in "\n".join(lines[nxt:end])]
            if nxt < len(lines) and lines[nxt].strip() == "（无）" and anchor not in text:
                lines[nxt:nxt + 1] = wanted
            else:
                lines[nxt:nxt] = missing
            return "\n".join(lines)
    return text


def _render_e2e_chain(probes: list) -> list[str]:
    """把 http 探针的**端到端步骤链**渲染成契约文本（作者必须满足的运行时数据流）。

    为什么必须有（2026-11-08 run_student_da 实测）：`_append_web_routes` 只渲染
    **单条路由形状**，跨步骤的数据流只存在于探针里 —— `save: {"batch_id": "batch_id"}`
    意味着"上一步响应必须含 batch_id 字段，下一步路径要用它"，作者看不到这条，
    seed 响应就不写 batch_id → 探针第 3 步的 `${batch_id}` 断链 → 404 → 整条能力
    磨一下午。expect_json 的**值字面量**（"created"/2/0）也一并渲染：字段表判据只对
    键名，值漂移（实现自造 "finished" vs 契约 "created"）静默通过。
    """
    from . import slicer as _slicer
    out: list[str] = []
    for p in probes:
        if not isinstance(p, dict):
            continue
        steps = p.get("steps") or ([p] if p.get("path") else [])
        if len(steps) < 1:
            continue
        multi = len(steps) > 1
        for i, st in enumerate(steps, 1):
            if not isinstance(st, dict):
                continue
            method = str(st.get("method") or "GET").upper()
            path = str(st.get("path") or "")
            want = st.get("expect_status")
            head = f"第{i}步 {method} {path} → 期望 {want if want is not None else '2xx'}"
            lines = [head]
            ej = st.get("expect_json")
            if isinstance(ej, dict) and ej:
                kv = ", ".join(f"{k}={v!r}" for k, v in ej.items())
                lines.append(f"  响应 JSON 必须含（值字面量也算）：{kv}")
            for var, field in (st.get("save") or {}).items():
                lines.append(f"  ★ 响应里必须有字段 `{field}`（探针取它存成 {var}，"
                             f"后续步骤要用——响应里没有这个字段，端到端链路就断）")
            if multi:
                out.append("· " + lines[0])
                out.extend("  " + x for x in lines[1:])
            else:
                out.extend(lines)
        if multi:
            out.append("  ↑ 以上步骤是**同一条链**：前一步的 save 供后一步引用，"
                       "任何一步的数据流断了，整条能力都通不了")
    return out


def _append_web_routes(text: str, probes: list, keep_modules: list[str] | None = None,
                       fail_note: str = "", bp=None) -> str:
    """把 http 探针要求的路由**写进契约本体**（作者看得到的东西）。

    为什么不能只写在简报里（实测 2026-11-07）：作者上下文由契约构成，简报只用于
    契约合成。路由形状（方法/路径/请求体字段/状态码）只有探针知道，契约里没有，
    作者就只写服务层函数 → 探针真发请求时 404 → 整片红、反复重写同一个地方。

    fail_note 也必须进来：Web 项目没有 CLI 入口，`ensure_entry_binding` 会直接原样返回，
    于是"上一轮实测失败"这条证据**永远传不到作者**（实测：状态码 200/期望 201 连撞两轮）。
    """
    from . import slicer as _slicer
    if not text.strip():
        return text
    marker = "# 路由（探针会"
    if marker in text:                       # 上一轮加的块：整块换新（否则证据不会更新）
        text = text[:text.index(marker)].rstrip()
    specs = _slicer.web_route_spec(
        [p for p in probes if isinstance(p, dict)],
        _slicer.owned_routes(bp) if bp is not None else None,
        home=str((keep_modules or [""])[0] or ""))
    if not specs:
        return text
    mods = [str(m) for m in (keep_modules or []) if str(m).endswith(".py")] or ["（本片模块）"]
    # 路由归属按**文件所有权**说清（2026-11-11 治本，§11.4e）：
    # 业务层是远端拥有的文件 → 那里**只写服务层函数**，路由由引擎接线层按函数名挂；
    # 让远端在业务层也挂一个 `router = APIRouter()` → 与引擎的接线段**两个 router 实例**，
    # 路由注册到谁身上不确定 → 表现为"端点 404 / 字段不对"。
    try:
        from . import openhouse as _oh_own
        _own_wire = _oh_own.is_wiring_layer(mods[0])
    except Exception:                                    # noqa: BLE001
        _own_wire = False
    _write_note = (f"- 写法：在 {mods[0]} 里 `router = APIRouter()`，用 Pydantic 模型接收 JSON 请求体，"
                   "再调用上面的契约函数；**不要写 app/main.py**（引擎生成，router 会自动挂上）"
                   if _own_wire else
                   "- 写法：**只在上面对应的落点模块里写服务层函数**（纯函数 / 纯字典进出），"
                   "用 Pydantic 模型接收 JSON 请求体；**不要挂 `@router` 装饰器** —— "
                   "路由由引擎接线层按函数名自动挂；**不要写 app/main.py**（引擎生成）")
    block = ["", "# 路由（探针会**真的**发这些请求：路径 / 字段名 / 状态码逐字一致，不许改）",
             *[f"- {s}" for s in specs],
             _write_note]
    chain = _render_e2e_chain([p for p in probes if isinstance(p, dict)])
    if chain:
        block.append("# 端到端步骤链（探针按此顺序真的发请求；★ 标记的字段是数据流依赖，响应里必须真的有）")
        block.extend(chain)
    note = (fail_note or "").strip().replace("\n", " ")[:240]
    if note:
        block.append(f"- 【上一轮实测失败，这次必须改掉】{note}")
    return text.rstrip() + "\n" + "\n".join(block) + "\n"


def _sync_probe_expectations(bp, sl: dict, contract_text: str, emit=None) -> int:
    """把探针 `expect_json` 的字符串字段值与**切片契约的示例**对齐。

    为什么必须有（实测 2026-11-07）：探针是蓝图期生成的，契约是片期合成的，两者对
    "实现细节"（版本号、id 形状）各自拍板 —— 探针要 `small_2x2-s42`，契约写
    `small_2x2-v1`，代码按契约实现后探针必红，这条能力就永远 absent。

    口径：**契约是考卷，探针跟着考卷走**。只同步字符串字段；数字/布尔不动
    （数字错了是真 bug，字符串值是实现选择）。只动声明过的字段名，不新增字段。
    返回同步的字段数。
    """
    n = 0
    text = str(contract_text or "")
    if not text:
        return 0
    for cid in (sl.get("caps") or []):
        c = bp.by_id(cid) if hasattr(bp, "by_id") else None
        if c is None:
            continue
        probe = c.probe or {}
        if str(probe.get("kind") or "") != "http":
            continue
        for step in (probe.get("steps") or []):
            ej = step.get("expect_json")
            if not isinstance(ej, dict):
                continue
            for k, v in list(ej.items()):
                if not isinstance(v, str) or not v:
                    continue
                m = re.search(r'["\']%s["\']\s*[:\uFF1A]\s*["\']([^"\']+)["\']'
                              % re.escape(str(k)), text)
                if m and m.group(1) != v:
                    ej[k] = m.group(1)
                    n += 1
    if n and emit:
        emit("probe_synced", {"slice": sl.get("id"), "fields": n,
                              "note": "探针期望值与切片契约示例对齐（契约是考卷）"})
    return n


def _wire_constraints(text: str, stack_name: str = "") -> str:
    """契约文本末尾追加**通用约束段**（引擎本地生成，零 token）。

    2026-11-15 改写（用户定稿："契约改一下，尽量让远端先写，本地补齐工程"）：
    约束从"框架形态"（凭据叫什么、请求体叫什么、函数名像不像路径）改成
    **领域边界**（只写业务函数 / 纯数据进出 / 错误用 ValueError / 不 import 框架），
    具体文本在 `core/contract_rules.py`（单一实现，检查钉在那里）。

    为什么方向要改：形式约束是**接线事实**，换栈就作废，而且会让远端把业务函数
    写成"长得像视图"的东西（业务与传输层耦合）。旧口径的代价实测过：
    契约写 `topics_create(body, user)` → 路由层供给不了 user → 引擎判"接不上"
    → 探针打到旧实现 → `source` 永远 None（连写 8 轮白写）。

    幂等：已在文本里就不重复追加。
    """
    from . import contract_rules as _cr

    if _cr.is_present(text or ""):
        return text
    return _cr.apply(text or "", stack_name or _stack().name)


def contract_for_slice(ws, sl: dict, brief: str, *, emit=None, refresh: bool = False,
                       entry: str = "", entry_example: str = "", fail_note: str = "",
                       prep: list[str] | None = None,
                       post: list[str] | None = None,
                       groups: list[list[str]] | None = None,
                       core_probes=None,
                       keep_modules: list[str] | None = None,
                       drop_modules: list[str] | None = None,
                       web_probes: list | None = None, bp=None) -> dict:
    """这一片的考卷：远端最强档把"切片简报"翻译成可机判的契约，按切片 id 缓存。

    缓存不是为了省钱（虽然也省），是为了**可复现**：同一片重跑必须还是同一份考卷，
    否则"上次为什么没过"就查不清了。
    """
    # 裁剪白名单：入口 + **本片能力自己声明的模块**。只留入口会让库模块永远写不出来
    # （实测 run 17：混片里 CLI 和库能力同片，砍成只剩入口 → 库模块一个都没生成）。
    keep_mods = list(keep_modules or [])
    drop_mods = list(drop_modules or [])
    if entry and entry not in keep_mods:
        keep_mods.insert(0, entry)
    if core_probes is not None and CORE_MODULE not in keep_mods:
        keep_mods.append(CORE_MODULE)          # 核心模块必须留下，否则 cli_run 被裁掉
    d = Path(ws) / SLICE_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sl['id']}.txt"
    if p.exists() and not refresh:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
        if text.strip() and _contract_funcs(text) > 0:
            fixed = (trim_contract(ensure_core_binding(text, entry, core_probes, fail_note), drop=drop_mods)
                     if core_probes is not None else
                     ensure_entry_binding(text, entry, entry_example, fail_note, prep, post, groups))
            fixed = _append_web_routes(fixed, list(web_probes or []), keep_modules=keep_mods,
                                        fail_note=fail_note, bp=bp)
            fixed = _wire_constraints(fixed, _stack().name)
            if fixed != text:
                p.write_text(fixed, encoding="utf-8")
            return {"ok": True, "path": str(p), "text": fixed, "by": "cached"}
    from . import contractgen as _cg
    from . import dosing as _dosing
    from . import settings_store as _st
    step = None
    try:
        step = _st.ladder_step(_st.strongest_step_index())
    except Exception:                                    # noqa: BLE001
        step = None
    got = _cg.synthesize(brief, task_id=f"slice-{sl['id']}", step_index="strongest",
                         thinking="on", ambiguity="decide", budget=_dosing.context_for(ws, step))
    if not got.get("ok"):
        return {"ok": False, "problems": got.get("problems") or ["契约合成失败"],
                "need_human": bool(got.get("need_human"))}
    text = (trim_contract(ensure_core_binding(got.get("text") or "", entry, core_probes,
                                               fail_note), drop=drop_mods)
            if core_probes is not None else
            ensure_entry_binding(got.get("text") or "", entry, entry_example, fail_note,
                                 prep, post, groups))
    text = _append_web_routes(text, list(web_probes or []), keep_modules=keep_mods,
                               fail_note=fail_note, bp=bp)
    text = _wire_constraints(text, _stack().name)
    if _contract_funcs(text) == 0:
        # 只有模块名、没有函数签名的契约 = 没法验收 = 会静默零工作量（实测过的形态）
        return {"ok": False, "problems": ["切片契约解析出 0 个函数签名（只是文件清单）"]}
    p.write_text(text, encoding="utf-8")
    if emit:
        emit("slice_contract", {"slice": sl["id"], "funcs": _contract_funcs(text),
                                "path": str(p), "by": got.get("by") or "",
                                "entry_example": bool(entry_example)})
    return {"ok": True, "path": str(p), "text": text, "by": got.get("by") or "synthesized"}


# ---------------------------------------------------------------- 默认执行器

def _default_runner(ws, brief: str, contract_path: str, *,
                    model: str = "", max_rounds: int = 40,
                    emit=None, slice_id: str = "") -> dict:
    """跑一轮夜班（内循环原样复用）。返回统一结构，方便编排层记账。

    不再接收任何"片级/用户预算"参数——唯一停机线是系统总预算 5%（见 budget_estimate）。
    """
    from . import company
    if not model:
        try:
            from . import ollama
            model = ollama.pick_default() or ""
        except Exception:                                # noqa: BLE001
            model = ""
    before = _global_spend_marker()
    t0 = time.time()
    res = company.night(Path(ws), brief, model,
                        max_rounds=max_rounds, emit=emit, contract_file=contract_path,
                        scale_level="S")
    # 夜班会覆盖仓库根的 DELIVERY.md；每一片的证据必须单独留档，否则可溯源性当场丢掉
    if slice_id and getattr(res, "delivery_path", ""):
        try:
            src = Path(res.delivery_path)
            if src.exists():
                dst = Path(ws) / SLICE_DIR / f"{slice_id}.DELIVERY.md"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
        except Exception:                                # noqa: BLE001
            pass
    return {"ok": bool(getattr(res, "ok", False)),
            "reason": str(getattr(res, "reason", "") or ""),
            "verified": int(getattr(res, "verified", 0) or 0),
            "total": int(getattr(res, "total", 0) or 0),
            "spent_s": round(time.time() - t0, 1),
            "cost_cny": round(max(0.0, _global_spend_marker() - before), 4),
            "delivery": str(getattr(res, "delivery_path", "") or "")}


# ---------------------------------------------------------------- 主循环

def _target_lines(goal: str) -> int:
    """从目标里读"要多少行"（≥10000 行 / 目标 8000 行 / XL）。读不到就 0（不启用规模机制）。"""
    g = str(goal or "")
    m = re.search(r"(\d{3,6})\s*行", g)
    if m:
        return int(m.group(1))
    if re.search(r"\bXL\b", g, re.I):
        return 10000
    if re.search(r"\bL\b", g):
        return 4000
    return 0


def _maybe_split_subsystems(bp, ws, emit=None) -> bool:
    """两级蓝图（开发文档 §18.7 第 1 件）：能力数过阈值 → 按模块域分组。

    只在能力数 ≥ 阈值时分（重新）组；已有分组不会因为"能力暂时变少"被取消
    （分组是稳定的接口冻结，取消会破坏已做的切片边界）。返回是否真的重新分组。
    """
    if len(bp.caps) < blueprint.SUBSYS_TRIGGER_CAPS:
        return False
    blueprint.split_subsystems(bp)
    blueprint.save(bp, ws)
    if emit:
        emit("subsystems", {"groups": len(bp.subsystems), "caps": len(bp.caps),
                            "note": "两级蓝图：能力按模块域分组，切片不跨子系统"})
    return True


# ---------------------------------------------------------------- 修复优先

def _probe_guilty_files(ws, sl: dict, bp) -> list[str]:
    """探针 traceback 里指向的工作区文件 —— 运行时坏的文件往往不在本片契约里
    （实测 2026-11-08 10:27：SQLite 跨线程炸在 db 层，而 db 文件不在 s06 的
    模块清单 → 4 轮循环没有任何订单碰它，¥0.1/轮空烧）。"""
    out: list[str] = []
    ws_s = str(Path(ws))
    for cid in (sl.get("caps") or []):
        cap = bp.by_id(cid) if hasattr(bp, "by_id") else None
        why = str(getattr(cap, "why", "") or "")
        for m in re.finditer(r'File "([^"]+)", line', why):
            f = m.group(1).replace("\\\\", "/").replace("\\", "/")
            if f.startswith(ws_s):
                rel = f[len(ws_s):].lstrip("/")
                if rel.endswith(".py") and rel not in out:
                    out.append(rel)
        # HTTP 探针只留异常消息不留 traceback → 按异常特征定位嫌疑文件：
        # SQLite 跨线程（ProgrammingError/created in a thread）→ sqlite3.connect 所在文件
        if "SQLite" in why or "ProgrammingError" in why or "created in a thread" in why:
            for p in sorted(Path(ws).rglob("*.py")):
                if any(x in p.parts for x in (".sixcoder", ".git", "__pycache__")):
                    continue
                try:
                    t = p.read_text(encoding="utf-8-sig", errors="replace")
                except OSError:
                    continue
                if "sqlite3.connect" in t or "check_same_thread" in t:
                    rel = p.relative_to(ws).as_posix()
                    if rel not in out:
                        out.append(rel)
    return out


def _verified_authority(ws, sl: dict, bp) -> list[str]:
    """权威形状：已验收函数 ∪ 被已验收函数调用的函数，返回键（AST 全仓抽取）。"""
    import ast as _ast
    from . import project as _proj
    ws = Path(ws)
    # 第一遍：全仓收集 verified → 模块/函数 → 被它们调用的名字
    verified_map: dict[str, set[str]] = {}
    called_by_verified: set[str] = set()
    try:
        prog = _proj.load_progress(ws) or {}
    except Exception:                                    # noqa: BLE001
        prog = {}
    trees: dict[str, _ast.Module] = {}
    for mod, st in prog.items():
        names = set(st.get("verified") or []) if isinstance(st, dict) else set()
        if not names:
            continue
        p = ws / mod
        if not p.is_file():
            continue
        try:
            tree = _ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
        except SyntaxError:
            continue
        trees[mod] = tree
        verified_map[mod] = names
        for node in tree.body:
            if isinstance(node, _ast.FunctionDef) and node.name in names:
                for n in _ast.walk(node):
                    if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load):
                        called_by_verified.add(n.id)
    # 第二遍：全仓任何模块里，名字 ∈ verified 或被已验收函数调用的函数 → 抽返回键
    out: list[str] = []
    seen: set[tuple[str, str]] = set()
    for mod, tree in trees.items():
        pass
    for p in sorted(ws.rglob("*.py")):
        if any(x in p.parts for x in (".sixcoder", ".git", "__pycache__", ".venv", "venv")):
            continue
        rel = p.relative_to(ws).as_posix()
        if rel in trees:
            tree = trees[rel]
        else:
            try:
                tree = _ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
            except SyntaxError:
                continue
        for node in tree.body:
            if not isinstance(node, _ast.FunctionDef):
                continue
            # 权威边界（2026-11-08 11:40 再放宽）：**凡已存在的函数**，现有返回形状
            # 即权威 —— 契约要改它必须显式声明迁移。依据：_public_view 未验收、
            # 调用方也未验收，契约照样给它发明样例冤杀（batch_id 那次"合法变更"
            # 作废两天工作量，说明默认保护现有代码更安全）。
            is_verified = node.name in verified_map.get(rel, set())
            keys: set[str] = set()
            for n in _ast.walk(node):
                if isinstance(n, _ast.Return) and isinstance(n.value, _ast.Dict):
                    for k in n.value.keys:
                        if isinstance(k, _ast.Constant) and isinstance(k.value, str):
                            keys.add(k.value)
            if not keys:
                continue
            key = (rel, node.name)
            if key in seen:
                continue
            seen.add(key)
            tag = "（已验收）" if is_verified else "（现有代码）"
            out.append(f"- `{rel}::{node.name}`{tag} 返回键："
                       f"{sorted(keys)[:14]}（含这些键即可，字段可多不可少）")
    return out


def _asgi_smoke(ws) -> dict | None:
    """工作区冒烟：ASGI 入口还导得进来吗？

    None = 正常（或没有 `app/main.py` 形态的入口，冒烟不适用）；dict = 坏了（带证据）。
    为什么必须有（实测 2026-11-08 01:09）：llm 模块虚构接口让 `import app.main` 失败，
    所有 HTTP 探针全红，观测把覆盖 5/76 清成 0/76 —— 一个坏文件毒化整个工作区。
    """
    ws = Path(ws)
    # 入口不再写死 app/main.py（耦合点修复 2026-11-08）：判据端一律走探测，
    # 换布局（main.py 在根/src 下）观测链路不再哑掉。生成端约定不变。
    from . import entrypoint as _ep
    _erel = _ep.entry_rel(ws)
    if not _erel:
        return None
    _emod = _erel[:-3].replace("/", ".")
    import subprocess as _sp
    import sys as _sys
    try:
        # 内置 Python 是 `._pth` 隔离模式：`sys.path` 固定、不含 cwd、不读 PYTHONPATH，
        # `python -c "import app.main"` 一律 ModuleNotFoundError → 会误判"工作区入口坏了"
        # 并触发两轮修复后收工（2026-11-23 实测，同 gates.gate_import_smoke）。
        _code = f"import sys; sys.path.insert(0, {str(ws)!r}); import {_emod}"
        r = _sp.run([_sys.executable, "-c", _code], cwd=str(ws),
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120)
    except (_sp.TimeoutExpired, OSError) as exc:
        return {"error": f"冒烟执行失败：{type(exc).__name__}", "unresolved": [],
                "modules": []}
    if r.returncode == 0:
        return None
    try:
        from . import interfaces as _ifc
        un = _ifc.unresolved_imports(ws)
    except Exception:                              # noqa: BLE001
        un = []
    err = [ln for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
    return {"error": (err[-1] if err else "导入失败")[:220],
            "unresolved": un,
            "modules": sorted({u["module"] for u in un})}


def _call_sites(ws, symbol: str, limit: int = 8) -> list[str]:
    """符号在产物里的全部出现行（file:line: code）——修复简报的接地证据。

    为什么必须有（实测 2026-11-08 run14）：修复简报只写了符号名，契约合成器看着
    `install_body_capture` 这个名字**自己发明**了"请求体缓存中间件"语义，还把已验收的
    parse_candidate_request 拉进考卷按发明的字段表判 → 修复片两轮全败。调用点是
    唯一可信的语义来源：签名必须满足它们，不是满足想象力。
    """
    out: list[str] = []
    pat = re.compile(rf"\b{re.escape(symbol)}\b")
    for p in sorted(Path(ws).rglob("*.py")):
        if any(x in p.parts for x in (".sixcoder", ".git", "__pycache__", ".venv", "venv")):
            continue
        try:
            rel = p.relative_to(ws).as_posix()
            for i, ln in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if pat.search(ln):
                    out.append(f"{rel}:{i}: {ln.strip()[:110]}")
        except OSError:
            continue
        if len(out) >= limit:
            break
    return out


def _engine_interface_fix(ws, symbol: str, *, emit=None) -> bool:
    """接口对齐（确定性，零 token）：声明签名与调用点不匹配时，给函数补默认参数
    + None 守卫。与 linkfix 同类 —— 只动**签名与入口两行**，不写业务逻辑；
    复杂形态（多参/非 dict 注解）不碰，仍交作者。返回 True = 改了一个文件。
    """
    ws = Path(ws)
    for p in sorted(ws.rglob("*.py")):
        if any(x in p.parts for x in (".sixcoder", ".git", "__pycache__", ".venv", "venv")):
            continue
        try:
            txt = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        m = re.search(rf"^def {re.escape(symbol)}\(([^)\n]*)\)([^:\n]*):[ \t]*\n", txt, re.M)
        if not m:
            continue
        params = m.group(1)
        if "=" in params or "," in params:
            continue                      # 已有默认值 / 多参形态 → 交作者
        if ":dict" not in params.replace(" ", "").lower():
            continue                      # 只处理 dict 注解形态（本次事故的形态）
        pname = params.split(":")[0].strip()
        guard = (f"def {symbol}({pname}: dict = None){m.group(2)}:\n"
                 f"    if {pname} is None:\n        {pname} = {{}}\n")
        new_txt = txt[:m.start()] + guard + txt[m.end():]
        try:
            p.write_text(new_txt, encoding="utf-8")
        except OSError:
            continue
        if emit:
            emit("engine_interface_fix", {
                "file": str(p.relative_to(ws)).replace("\\", "/"), "symbol": symbol,
                "note": "接口对齐：签名补默认参数 + None 守卫（与 linkfix 同类，零 token）"})
        return True
    return False


def _seed_guilty_funcs(contract_path: Path, guilty: list[str], ws) -> bool:
    """罪魁文件在契约里没有函数条目 → 机械补一条基础设施函数条目。

    为什么必须有（实测 run19 10:52）：订单按**契约函数**派发；合成器把 models.py
    的修复写成纯文字说明（无 `- 函数` 行）→ 模块在契约里、订单为零 → 10/10 判据
    全绿照样空转。补的条目是基础设施规格（命名与线程安全要求写死），
    不是业务逻辑 —— 与 linkfix / engine_interface_fix 同类。
    """
    try:
        cp = Path(contract_path)
        txt = cp.read_text(encoding="utf-8-sig", errors="replace")
        if "引擎补的修复条目" in txt or not guilty:
            return False
        added = []
        n_sections = len(re.findall(r"^\d+\) ", txt, re.M))
        for g in guilty[:4]:
            gnorm = g.replace("\\", "/")
            # 该文件的契约小节里是否已有函数条目
            m = re.search(rf"^\d+\) {re.escape(gnorm)}：.*?(?=^\d+\) |\Z)", txt, re.M | re.S)
            if m and re.search(r"^\s+- \w+\(", m.group(0), re.M):
                continue
            stem = Path(gnorm).stem
            body = (Path(ws) / g)
            sqlite_fix = False
            try:
                sqlite_fix = body.is_file() and "sqlite3.connect" in body.read_text(
                    encoding="utf-8-sig", errors="replace")
            except OSError:
                pass
            n_sections += 1
            if sqlite_fix:
                added.append(
                    f"{n_sections}) {gnorm}：修复上片探针运行时崩溃（引擎补的修复条目）。\n"
                    f"   - init_database() -> None：把本文件的 SQLite 连接改为线程安全——"
                    f"connect(..., check_same_thread=False) 并用 threading.Lock 保护全部读写，"
                    f"或改为每请求新建连接；不改变既有函数的对外行为与签名；"
                    f"已有函数逻辑保持。\n")
            else:
                added.append(
                    f"{n_sections}) {gnorm}：修复上片探针运行时崩溃（引擎补的修复条目）。\n"
                    f"   - {stem}_repair() -> dict：按简报「上片探针运行时崩溃的文件」"
                    f"证据修复该文件的运行时缺陷；不改变既有函数的对外行为与签名。\n")
        # SQLite 类罪魁：文件在契约里已有函数条目时，补「子线程调用必须成功」判据
        # （判据在主线程全过、多线程才炸 → 不加这条就永远派不出单，实测 11:10）。
        appended = False
        for g in guilty[:4]:
            gnorm = g.replace("\\", "/")
            body = Path(ws) / g
            try:
                if not body.is_file() or "sqlite3.connect" not in body.read_text(
                        encoding="utf-8-sig", errors="replace"):
                    continue
            except OSError:
                continue
            if "线程安全判据（引擎机械追加）" in txt:
                break
            m = re.search(rf"^(\d+)\) {re.escape(gnorm)}：.*?(?=^\d+\) |\Z)",
                          txt, re.M | re.S)
            if not m:
                continue
            sec = m.group(0)
            fnames = re.findall(r"^\s+- (\w+)\(", sec, re.M)[:3]
            jlines = []
            for fn in fnames:
                ex = re.search(rf"{re.escape(fn)}\((\{{[^}}\n]*\}})\)\s*==", sec)
                if ex:
                    jlines.append(f"- 子线程调用 {fn}({ex.group(1)}) 必须成功。")
            if jlines:
                txt += ("\n\n## 线程安全判据（引擎机械追加）\n" + "\n".join(jlines) + "\n")
                appended = True
        if added or appended:
            cp.write_text(txt, encoding="utf-8")
            return True
    except OSError:
        pass
    return False


def _repair_brief(err: str, focus: list[str], sites: list[str],
                  frozen: list[str]) -> str:
    """修复片简报（纯函数：给 check_repair_brief 断言用）。

    两条硬禁令的来历（实测 2026-11-08 午后 org 切片修复乒乓，models.py 半小时
    内 8 次 bodies 重写全败）：
    · 模块级副作用：作者把 `state = _models_state()` 写在 import 期（api/org.py
      顶部），被依赖函数还没定义就执行 → import app.main 反复红 → 修复片反复
      清零 → 乒乓烧钱。import 期只许定义，业务初始化一律懒执行。
    · 全文件重排：修复片每次都整文件重写，无关函数也被动过 → 下次修复又清零。
      与本修复无关的既有函数必须逐字节保留。
    """
    brief = (
        "修复工作区阻塞：ASGI 入口 import 失败，在它修好之前其它任何工作都无法观测。\n"
        f"导入错误：{err}\n"
    )
    if focus and sites:
        brief += (
            f"问题符号：{', '.join(focus)}。\n"
            "**现有调用点（签名与语义必须满足它们，不要发明新语义）：**\n"
            + "\n".join(sites) + "\n"
            "**冻结清单（已存在且通过验收，本片不许改动其签名或行为）：** "
            + "、".join(frozen) + "\n"
            "注意：同一符号在多个模块都有定义时，**以调用方实际 import 的那份为准**——"
            "要么修那份，要么让调用方改 import 指向正确的那份；不要新建第三份。\n"
            "两条出路二选一：① 把缺失/不匹配的函数真正实现到被 import 的模块——"
            "严格按调用点的实参形状与返回值用法确定签名与语义；"
            "② 调用方改用已有函数。\n"
            "禁止再 import 任何不存在的东西；禁止改动冻结清单里的函数。\n"
            "**禁止模块级副作用调用**：import 期只许 import/定义/常量（"
            "`router = APIRouter()` 这类框架惯例除外）；任何业务初始化（读库、建表、"
            "读状态）都必须放进函数、在首次调用时懒执行——否则 import 就会炸。\n"
            "**禁止无关重排**：与本修复无关的既有函数逐字节保留（不重排、不改名、"
            "不重写），只新增缺失符号。\n"
            "完成的判据：`import app.main` 成功。"
        )
    else:
        brief += ("按上面的导入错误定位缺失的名字并补上；"
                  "禁止再 import 任何不存在的东西。判据：`import app.main` 成功。")
    return brief


def _mod_to_rel(ws, mod: str) -> str:
    """点分模块名 → 仓库相对路径（斜杠）；已是路径形态则原样。

    为什么必须有（2026-11-08 跨模块审查）：_asgi_smoke 的 modules 来自
    unresolved_imports 的 u["module"]（点分 `app.models`），而 call sites 给的是
    斜杠 rel（`app/models.py`）——两种形态混进 mods 后三连坏：save_progress 写出
    没人读的点分键（修复片清零失效 → run15"零订单"复发通道）、
    _top_level_defs 读不到路径（冻结清单空）、验收循环按斜杠 rel 查不到进度。
    用实际文件存在性消歧，落不到就原样返回（不瞎猜）。
    """
    m = str(mod or "").replace("\\", "/").strip()
    if not m or "/" in m:
        return m
    cand = m.replace(".", "/") + ".py"
    pkg = m.replace(".", "/") + "/__init__.py"
    if (Path(ws) / cand).is_file():
        return cand
    if (Path(ws) / pkg).is_file():
        return pkg
    return m


def _repair_workspace(ws, *, emit=None, runner=None) -> bool:
    """入口不可导入 → 合成"修复片"走一遍完整流水线（契约→作者→探针）。True=已恢复。

    引擎只**路由**（把坏文件和精确证据派给作者），不代写业务代码；
    修复片的契约/验收与普通片同一条链，预算照记账。
    runner 可注入（自检用）；默认走 _default_runner（与普通片同一执行器）。
    """
    bad = _asgi_smoke(ws)
    if bad is None:
        return True
    _run = runner or _default_runner
    from . import gates as _gg
    for attempt in (1, 2):
        missing = sorted({u["symbol"] for u in (bad.get("unresolved") or [])})
        mods = bad.get("modules") or []
        # 接口对齐先行（零 token）：签名与调用点不匹配的形态（ImportError 已消、
        # 剩 TypeError），引擎按 middleware 类兼容实现直接补默认参数 + None 守卫。
        # run17 实测：作者 4 轮整篇重写全被结构体检回滚（顺手发明未定义常量），
        # 而正确修复只是两行机械改动 —— 该引擎做的别让模型猜。
        if not missing:
            m = re.search(r"([A-Za-z_]\w*)\(\)", str(bad.get("error") or ""))
            if m:
                _engine_interface_fix(ws, m.group(1), emit=emit)
                bad = _asgi_smoke(ws)
                if bad is None:
                    if emit:
                        emit("workspace_repaired", {"attempt": attempt, "by": "engine_interface_fix"})
                    return True
        mods = bad.get("modules") or []
        # 符号不缺失、只是签名/调用不匹配时（ImportError 已消、剩 TypeError），从错误文本
        # 里提取出事函数（如 `TypeError: install_body_capture() missing ...`）——实测
        # 2026-11-08 run14：这类错误下 unresolved 为空，简报没证据，修复片两轮全败。
        if not missing:
            m = re.search(r"([A-Za-z_]\w*)\(\)", str(bad.get("error") or ""))
            if m:
                missing = [m.group(1)]
        focus = missing
        sites = [ln for s in focus for ln in _call_sites(ws, s)] if focus else []
        mods = sorted(set(mods) | {ln.split(":")[0] for ln in sites}) or mods
        mods = sorted({_mod_to_rel(ws, m) for m in mods} - {""})
        frozen = sorted({n for mdir in mods
                         for n in _gg._top_level_defs(Path(ws) / mdir) - {"__all__"}})[:20]
        sl_id = f"s-repair-{attempt}"
        brief = _repair_brief(str(bad.get("error") or ""), list(focus),
                              list(sites), list(frozen))
        if emit:
            emit("workspace_repair", {"attempt": attempt, "slice": sl_id,
                                      "missing": missing[:6], "modules": mods[:6]})
        # 强制重做：修复片涉及的模块**清掉旧的 verified 状态**。不清的后果（实测
        # 2026-11-08 run15）：函数级判据在 run13/14 已经"过"过、进度记成 verified，
        # 修复片断点续跑一看"全都验收过"→ 一张订单都不派 → 作者一行代码没写 →
        # 片结束冒烟照旧红，两轮空转。verified ≠ 集成健康，修复片必须重新验收。
        try:
            from . import project as _proj_r
            for _m in mods:
                _proj_r.save_progress(ws, _m, {"verified": [], "ok": False})
        except Exception as exc:                     # noqa: BLE001
            if emit:
                emit("notice", {"message": f"修复进度清零跳过：{type(exc).__name__}"})
        cg = contract_for_slice(ws, {"id": sl_id, "caps": []}, brief, emit=emit,
                                refresh=True)
        if cg.get("ok"):
            # 机械追加"无参调用必须成功"判据：这是**当前必然失败**的一条（工作区冒烟
            # 就是因为它红的），保证体检一定派单 —— 否则函数级判据全绿 + verified 齐全
            # → 零订单 → 修复片空转（实测 run15：作者一行代码没写，两轮"2/2、3/3"）。
            try:
                _cp = Path(cg["path"])
                _txt = _cp.read_text(encoding="utf-8-sig", errors="replace")
                _add = [f"- {s}() 无参调用必须成功（导入期兼容，调用点见简报）。"
                        for s in focus
                        if any(re.search(rf"\b{re.escape(s)}\(\)", ln) for ln in sites)]
                if _add and "导入期兼容" not in _txt:
                    _txt = (_txt.rstrip() + "\n\n## 导入期兼容判据（引擎机械追加）\n"
                            + "\n".join(_add) + "\n")
                # 声明签名与调用点对齐：无参调用点存在 → 声明的参数必须可省略（补默认值）。
                # 不补的后果（实测 run16）：contractcheck 判"示例参数个数 0 ≠ 声明 1"硬矛盾
                # → company.night 拒收契约 → 修复片零订单 → 空转收工。
                for _s in focus:
                    if not any(re.search(rf"\b{re.escape(_s)}\(\)", ln) for ln in sites):
                        continue
                    def _add_default(m: "re.Match") -> str:
                        inner = m.group(2)
                        parts = [p for p in inner.split(",") if p.strip()]
                        if not parts or all("=" in p for p in parts):
                            return m.group(0)
                        fixed = ",".join(p if "=" in p else f"{p} = None" for p in parts)
                        return f"{m.group(1)}{fixed}{m.group(3)}"
                    _txt = re.sub(rf"(-\s+{re.escape(_s)}\()([^)\n]*)(\))",
                                  _add_default, _txt)
                # 0 参示例行会绊倒 contractcheck 的 arity 检查（示例 0 参 vs 声明 1 参 →
                # 硬矛盾 → company 拒收契约 → 修复片零订单，实测 run16）。删掉示例行，
                # 无参要求保留在「导入期兼容判据」里 —— 那条由 props 求值（call_ok），
                # 不经过示例配对，照样逼作者把参数改成可省略。
                _keep = [ln for ln in _txt.splitlines()
                         if not re.match(rf"^\s*{re.escape('|'.join(focus))}\(\)\s*==", ln)]
                _txt = "\n".join(_keep) + "\n"
                _cp.write_text(_txt, encoding="utf-8")
            except OSError:
                pass
            _run(ws, brief, cg["path"], emit=emit, slice_id=sl_id)
        elif emit:
            emit("notice", {"message": "修复契约合成失败："
                            + str((cg.get("problems") or ["?"])[0])[:120]})
        bad = _asgi_smoke(ws)
        if bad is None:
            if emit:
                emit("workspace_repaired", {"attempt": attempt})
            return True
    if emit:
        emit("workspace_still_broken", {"error": (bad or {}).get("error")})
    return False


def night_auto(repo, goal: str = "", *,
               model: str = "", max_slices: int = 0, max_rounds: int = 40,
               do_audit: bool = True, fresh: bool = False,
               llm=None, runner=None, recon=None, emit=None, approve=None,
               spec_samples: str = "", arch: dict | None = None,
               selfcheck: bool = True) -> dict:
    """全自主夜班。只给一句话目标，其余由系统自己决定。

    **已冻结（2026-11-15）**：夜班主路径改成 `core/night.py`（判型 → 供料 →
    整仓会话施工 → 全量验证）。本函数是"切片式"旧路径（切片 + 每片契约 +
    片预算 + 片内自验），只在 `cli night` 收到架构问答类参数
    （`--clarify/--ask/--interview/--spec-sample`）时回落使用。
    **不要在这里加新功能**：加了也进不了主路径，只会让"两条路"继续分叉。
    自检 `checks/check_night_path.py` 会盯着"主路径不许再回到这里"。

    `arch` = {"questions": [...], "answers": {...}}：前期架构问答的结果（人给的安排）。
    有它就先并进目标、并把其中的验收样例当权威判据——人只在最贵的位置介入这一次。

    返回 {ok, reason, blueprint, coverage, ratios, spent_cny, slices, replans,
          history, report, descoped, blocked, gaps, next}
    """
    ws = Path(repo).resolve()
    # 全自主是从 0 生成：工作区允许还不存在（CLI --goal 模式与 API 直接调都会走这里）
    ws.mkdir(parents=True, exist_ok=True)
    # 2026-11-10：标记"本进程当前服务的产物仓"。`remote.complete` 有 20 多个调用方
    # 不传 workspace（architect/contractgen/blueprint/patch…），没有这个当前值，
    # 明细行的 project_id 就永远是空、项目账本也涨不上去（实测 19:44 一笔 plan 调用）。
    try:
        from . import project_ledger as _pl_cur
        _pl_cur.set_current(ws)
    except Exception:                                    # noqa: BLE001
        pass
    # 项目宪法：跑夜班前先落盘（幂等）。作者提示词把宪法当稳定前缀注入（吃前缀缓存）。
    try:
        from . import constitution as _const0
        _const0.ensure(ws)
    except Exception:                                    # noqa: BLE001
        pass
    t0 = time.time()
    llm = llm or blueprint._default_llm
    runner = runner or _default_runner
    recon = recon or covmod.run
    goal = (goal or "").strip()
    calls = 0

    # ── 阶段 1 的产物：架构安排并进目标 + 验收样例升级为权威判据 ──
    _arch_n = 0
    if arch:
        _qs = list((arch or {}).get("questions") or [])
        _as = dict((arch or {}).get("answers") or {})
        if _qs:
            goal = architect.merge_goal(goal, _qs, _as)
            _arch_n = sum(1 for q in _qs if str(_as.get(q["id"]) or "").strip())
            _sam = architect.samples_from(_qs, _as)
            if _sam.strip():
                spec_samples = (spec_samples + "\n\n" + _sam).strip() if (spec_samples or "").strip() else _sam
            if emit:
                emit("architecture", {"questions": len(_qs), "answered": _arch_n,
                                      "samples": _sam.count("### ")})

    bp = None if fresh else blueprint.load(ws)
    # 用户不再可设预算上限：续跑时若蓝图里残留 total_budget_cny，清零（系统预算改走 .sixcoder/budget.json）
    if bp is not None and bp.caps and bp.total_budget_cny:
        bp.total_budget_cny = 0.0
        blueprint.save(bp, ws)
        if emit:
            emit("notice", {"message": "蓝图总预算上限已清零（唯一停机线改由系统预算 .sixcoder/budget.json 控制）"})
    if bp is None or not bp.caps:
        if not goal:
            return _fail(ws, "既没有蓝图也没有目标：至少要给一句话")
        got = blueprint.derive(goal, budget_cny=0.0, llm=llm,
                               do_audit=do_audit, emit=emit, ws=ws)
        calls += int(got.get("calls") or 0)
        if not got.get("ok"):
            return _fail(ws, "蓝图不合格：" + "；".join((got.get("problems") or [])[:3]),
                         blueprint=got.get("blueprint"))
        bp = got["blueprint"]
        # 2026-11-10 夜：**不再"超预算砍范围"**（用户定稿：用户不可设任何上限，
        # 唯一停机线是"系统预算 ×1.05"，写在 .sixcoder/budget.json，由引擎每轮自查）。
        # 旧机制会从最不关键的能力开始砍，等于"按钱缩能力" —— 与"本地是远端的放大器"冲突。
        # 对应自检钉子：checks/check_auto.py「按钱缩范围机制已移除」。
        blueprint.save(bp, ws)
        # 两级蓝图：能力数过阈值 → 分组（切片不跨子系统）
        _maybe_split_subsystems(bp, ws, emit)
        if emit:
            emit("blueprint_ready", {"version": bp.version, "caps": len(bp.caps),
                                     "summary": blueprint.summary(bp)[:600]})
    elif goal and not bp.goal:
        bp.goal = goal
        blueprint.save(bp, ws)

    # 机制注入：鉴权拦截 / 页面可打开 / 数据真落盘 —— 这三类是"能上线"的硬要求，
    # 但远端模型经常"忘了做"，于是系统给绿灯、交付物却不能用（实测 2026-11-07）。
    # 这里是**引擎侧**补能力，不消耗远端能力。
    try:
        from . import shipgate as _ship0
        _inj = _ship0.inject_caps(bp, goal or bp.goal, ws=ws)
        if _inj:
            bp.notes.append("机制注入：" + "、".join(_inj))
            blueprint.save(bp, ws)
            if emit:
                try:
                    from . import blueprint as _bp_norm2
                    _bp_norm2.normalize_injected(bp, ws=ws)
                except Exception:                    # noqa: BLE001
                    pass
                emit("caps_injected", {"caps": _inj})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"机制注入跳过：{type(exc).__name__}: {exc}"[:160]})

    # 老蓝图（引擎分配落点模块之前生成的）可能 modules 是空的：
    # Web 能力必须补齐落点，否则作者无处落文件、契约合成会抄成 calc.py。
    if blueprint.fill_http_modules(bp, ws=ws):
        blueprint.save(bp, ws)

    # ── 人给的验收样例是**权威判据**：直接变成能力，排在最前，模型自造的探针让位 ──
    if (spec_samples or "").strip():
        try:
            from . import specsample as _ss
            _samples = _ss.parse(spec_samples)
        except Exception as exc:                    # noqa: BLE001
            _samples = []
            bp.notes.append(f"验收样例解析失败：{type(exc).__name__}")
        if _samples:
            _caps = _ss.to_caps(_samples)
            _sample_args = {json.dumps([c.probe.get("args"), c.probe.get("stdout_has")],
                                       ensure_ascii=False, sort_keys=True) for c in _caps}
            bp.caps = [c for c in bp.caps
                       if json.dumps([c.probe.get("args"), c.probe.get("stdout_has")],
                                     ensure_ascii=False, sort_keys=True) not in _sample_args]
            bp.caps = _caps + bp.caps
            _ent = (_ss.entries(_samples) or [""])[0]
            if _ent:
                bp.goal = (bp.goal + f"｜入口脚本固定为 {_ent}") if bp.goal else bp.goal
            bp.notes.append(f"验收样例 {len(_caps)} 条（人给的，权威）："
                           + "、".join(c.id for c in _caps))
            blueprint.save(bp, ws)
            if emit:
                emit("spec_samples", {"n": len(_caps), "entry": _ent})
    if approve is not None and not approve(bp):
        return _fail(ws, "蓝图等人确认（没开跑，一分钱没花）", blueprint=bp)

    # ---- 开工三件事（§8.2 / §8.4 / §8.3）：先看清现状，再锁接口，最后验扩展点 ----
    # 2026-11-10：这整段抽到 core/openhouse.prepare，与**造物模式共用一份实现** ——
    # 造物模式以前没有这段，于是写出的模块路由接不上、端到端红灯没人知道
    # （实测 84 项能力只过 13 项）。复制两份必然漂移，所以共用。
    try:
        from . import openhouse as _oh
        _oh.prepare(ws, bp, emit=emit, llm=llm)
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"开工段跳过：{type(exc).__name__}: {exc}"[:150]})

    # 起始观测前也要接线：上一轮作者可能自己给服务函数挂了路由（重复注册 / 错误的
    # response_model），也会把引擎段抹掉；不先接线，起始观测就会把"能过的能力"判红。
    # 2026-11-12 修复：原来这里**直调 apply_web_wiring**，绕过了 openhouse.wire 的
    # 快照 + 导入冒烟 + **接线回归守卫**（§11.4m.1）→ 接线批量重写打坏了别的能力时，
    # 这条线上**没有任何东西兜底**。改成走 openhouse.wire（它内部就是调
    # apply_web_wiring + 写入口 + 冒烟 + 回归复验 + 变红就恢复快照）。
    try:
        from . import openhouse as _oh_wire
        _w0 = _oh_wire.wire(ws, bp, emit=emit)
        if _w0 and emit:
            emit("web_routes", {"module": "（起始）", "routes": len(_w0),
                                "detail": "；".join(_w0[:4])})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"起始接线跳过：{type(exc).__name__}: {exc}"[:160]})

    cov = recon(bp, ws)
    covmod.record(ws, cov, "起始观测")
    bp = covmod.mark(bp, cov)
    blueprint.save(bp, ws)
    # Web 项目：入口必须由引擎先写好（作者只写功能模块，不写 app/main.py），
    # 否则每加一片就重写一次入口，上一片的路由会被抹掉。
    _web0 = write_web_entry(ws, bp)
    if _web0.get("wrote"):
        bp.notes.append(f"引擎生成 Web 入口：{_web0.get('why')}")
        if emit:
            emit("web_entry", {"modules": _web0.get("modules") or []})

    # 续跑时把中断前已花掉的钱带回来（实际花费账本，与任何上限无关）
    spent = _load_spent(ws, goal)
    # 2026-11-10：**显式对账**（读函数 `project_spent_cny` 不再写库，避免测试数据污染真账本）。
    # 旧计数器比账本大时把账本抬上来 —— 只升不降，宁保守也绝不少算（少算 = 停机线失效）。
    try:
        from . import project_ledger as _pl0
        _led = _pl0.total(ws)
        if _led is not None and spent > _led + 1e-9:
            _pl0.bump_to(ws, spent, "夜班启动对账（night_spent.json 比账本大）")
        elif _led is None and spent > 0:
            _pl0.seed(ws, spent, "首次运行补账（来源 night_spent.json）", count_run=False)
    except Exception:                                    # noqa: BLE001
        pass
    # 用明细重算一次（幂等 SET）：换版期间旧代码只写明细不累加账本，这里把差额补平。
    try:
        from . import project_ledger as _pl1
        _pl1.reconcile_from_details(ws)
    except Exception:                                    # noqa: BLE001
        pass
    if spent and emit:
        emit("budget_resumed", {"spent_cny": round(spent, 4)})
    hist: list[dict] = []
    fails: dict[str, int] = {}
    regressions: list[dict] = []
    contract_fail_streak = 0
    wasted = 0                 # 无效尝试：跑完一片后"未通过能力集合"没有净减
    no_gain_streak = 0         # **连续**无收获的片数（缩片的触发条件，见下）
    force_refresh = False      # 上一次无效 → 这次强制重合成契约（不吃旧缓存）
    best_done = 0              # 历史最好完成数（判断错误集合是否单调下降）
    slices_run = 0
    replans = 0
    selfchecks: list[dict] = []    # 片内自验的账（每一步修好了什么、卡在哪）
    reason = "循环到顶"
    # 修复优先：入口坏了先修它（最多 2 轮），修不好就如实收工 —— 继续跑=每片都在
    # 毒化状态里烧钱、每次观测都把覆盖清零（实测 2026-11-08 01:09，0/76）。
    _ws_stop = "" if _repair_workspace(ws, emit=emit) else \
        "工作区入口不可导入且两轮修复失败（workspace_still_broken）——如实收工"
    it = 0
    while True:
        if _ws_stop:
            reason = _ws_stop
            break
        it += 1
        if it > MAX_ITERATIONS:
            reason = f"循环次数到顶（{MAX_ITERATIONS}）"
            break
        pending = bp.pending()
        if not pending:
            # "待办清空" 有两种完全相反的含义，必须分清：全部通过 vs 全部降级。
            # 混在一起报"做完了"，就是把降级偷偷说成完成（最严重的那种谎）。
            n_done = len([c for c in bp.caps if c.status == "done"])
            if n_done == 0:
                reason = "一条能力都没通过（剩余能力已全部降级）——需要人工介入"
            else:
                reason = f"待办能力已清空：完成 {n_done} 条，其余见降级清单"
            break
        if max_slices and slices_run >= max_slices:
            reason = f"切片轮数到顶（{max_slices}）"
            break

        # 唯一停机机制：本项目总花费超过系统总预算 5% 自动停机
        # （预算写在 .sixcoder/budget.json，系统/运维设置，用户不可改）。
        # 传空 task_id 的语义（2026-11-10 修正，**别再改回去**）：
        #   空 task_id = "**本项目**"，由 budget_estimate 读 `.sixcoder/night_spent.json`
        #   计数器；**不是**全量历史账本。以前空 task_id 落到 usage_summary("")，
        #   那是所有项目+所有实验的总和，实测项目只花 ¥31.01 却被判超额秒停。
        if _be.system_budget_stopped(ws, ""):
            reason = "系统总预算超额 5%，自动收工（详见自主报告）"
            if emit:
                emit("system_budget_stop", {"action": "收工", "reason": reason})
            break

        # 切片大小由剩余能力决定（dosing 自适应；唯一停机线是系统总预算 5%，不再按钱收放）
        dose_now, _dose_why = slicer.dose(ws)
        funcs_cap = int(dose_now)
        # Web 能力**不再硬砍到 6**（2026-11-09 用户铁律：完全相信远端）。
        #
        # 旧口径的依据（实测 2026-11-07 s04）："一片 12 个函数作者根本写不完 → 整片被回滚"。
        # 但那条结论建立在**引擎自己造坏数据**的年代：
        #   · 多行签名被截断 → 写出来的文件解析不了；
        #   · 新函数被静默丢掉 → 远端写对了却没落盘；
        #   · 路由接线把 `batches` 判成"没有匹配函数" → 探测恒 404。
        # 这些引擎缺陷今天都已修（§9.14/§9.19/§9.22），"写不完"的真实原因不在远端。
        #
        # 现在切片大小只由 **dosing** 决定（输出配额 ÷ 每函数 token，再按一次通过率
        # 自适应放大/缩小）—— 那是**有实测依据**的自适应；6 是拍脑袋的常数。
        # 一次通过率低时 dosing 自己会缩小（`first_pass < 0.6 → ×0.6`），
        # 写不出来时缩片仍然有效，只是不再被一个与实测无关的常数卡着。
        # 结构依赖要在**每一轮**都补齐，不能只在 derive 里做：
        # 续跑加载的是已存蓝图，那里面只有"数据流"依赖（20/76），外键边一条都没有，
        # 于是第一片永远是 batch.run_student_da 这种中段能力（实测 2026-11-08）。
        # 推导是幂等的：补过一遍之后返回 0，不会反复写盘。
        if blueprint.derive_fk_deps(bp):
            blueprint.save(bp, ws)
        plan = slicer.plan(bp, max_funcs=funcs_cap)
        if not plan["slices"]:
            reason = "没有可做的切片：剩下的能力全部受阻（依赖被降级或不存在）"
            break
        sl = plan["slices"][0]
        brief = slicer.brief(sl, bp, ws=ws, done=bp.done())
        # 上片探针 traceback 指向的"罪魁文件"：强制并入本片契约（一次性消费）。
        # 它们不在本片模块清单里时，任何订单都不会碰它们 → 循环空烧（实测 10:27）。
        _prf = Path(ws) / ".sixcoder" / "probe_repair.json"
        _guilty: list[str] = []
        try:
            if _prf.is_file():
                _d0 = json.loads(_prf.read_text(encoding="utf-8"))
                _guilty = list(_d0.get("files") or [])
                _prf.unlink()
        except Exception:                                # noqa: BLE001
            _guilty = []
        if _guilty:
            brief += ("\n\n**上片探针运行时崩溃的文件（本片必须一并修复，traceback 证据）：**\n"
                      + "\n".join(f"- {_g}" for _g in _guilty[:6])
                      + "\n修复方向以 traceback 为准（如 SQLite 跨线程：连接处加 "
                      "check_same_thread=False 并配 threading.Lock，或每请求新建连接）。\n")
        # 已验收函数的权威形状：新契约不得为它们发明新样例（实测 2026-11-08 09:42，
        # run19 第 3 段：_public_view 已验收，下一切片契约却发明 code/id/name 样例 →
        # 已验收函数被新考卷判死 → 反复重写 → 覆盖停滞）。已验收 = 代码是权威，
        # 契约跟着代码走 —— 与"未验收时契约是考卷"互为镜像，边界就是验收状态。
        _auth_refresh = False
        try:
            _va = _verified_authority(ws, sl, bp)
            if _va:
                brief += ("\n\n**已验收函数的权威形状（契约样例必须与现有代码一致，"
                          "不得发明新字段；这些函数本体不许改动）：**\n" + "\n".join(_va))
            # 权威清单变化 → 契约缓存必须失效（实测 2026-11-08 12:52：s33 契约是
            # 权威机制上线前的缓存，污染样例还在考卷里，简报根本没被看见）。
            import hashlib as _hl
            _ahf = Path(ws) / ".sixcoder" / "authority_hash.json"
            try:
                _ahd = json.loads(_ahf.read_text(encoding="utf-8"))
            except Exception:                            # noqa: BLE001
                _ahd = {}
            _vh = _hl.md5("\n".join(_va).encode("utf-8")).hexdigest()
            _auth_refresh = _ahd.get(sl["id"]) != _vh
            _ahd[sl["id"]] = _vh
            _ahf.parent.mkdir(parents=True, exist_ok=True)
            _ahf.write_text(json.dumps(_ahd), encoding="utf-8")
            if _auth_refresh and emit:
                emit("authority_changed", {"slice": sl["id"], "entries": len(_va)})
        except Exception as exc:                         # noqa: BLE001
            emit("notice", {"message": f"权威形状提取跳过：{type(exc).__name__}"})
        probes = [bp.by_id(c).probe for c in sl["caps"] if bp.by_id(c)]
        entry = next((str((p or {}).get("entry") or "") for p in probes
                      if str((p or {}).get("kind") or "") == "cli"), "")
        cli_probes = [p for p in probes if str((p or {}).get("kind") or "") == "cli"]
        # **cli_run 是全项目共用的一根入口**：片与片之间会反复重写同一个文件。
        # 只带本片示例 → 重写就把上一片的行为丢掉（实测 mid2：覆盖 19→17 来回摆）。
        # 所以 CLI 契约必须带**全部** CLI 能力的示例（含已通过的），让 cli_run 只能越写越全。
        all_cli = [c.probe for c in bp.caps
                   if str((c.probe or {}).get("kind") or "") == "cli"
                   and c.status != "descoped"]
        _ex_groups = entry_block(all_cli)
        # 上一次这条能力是怎么红的 —— 必须进契约的函数规范字段（缓存命中时简报不参与合成，
        # 只写简报等于没写：实测第二轮重跑出的错和第一轮一模一样）
        fail_note = next((str(bp.by_id(c).why or "") for c in sl["caps"]
                          if bp.by_id(c) is not None and bp.by_id(c).why
                          and bp.by_id(c).status != "done"), "")
        # 只把**成果性证据**（探针怎么说）喂给作者；"连续 3 次尝试仍未通过→降级"这类决策
        # 叙述对作者毫无用处，反而污染契约合成（实测 run 12：CLI 片连续合成失败 3 次）。
        if any(k in fail_note for k in ("降级", "连续 3 次", "⚠ 被切片")):
            fail_note = ""
        if cli_probes and entry:
            _w = write_entry_wrapper(ws, entry)
            if _w.get("wrote"):
                bp.notes.append(f"引擎生成入口：{_w.get('why')}（{entry}）")
                if emit:
                    emit("wiring", {"entry": entry, "why": _w.get("why")})
        cg = contract_for_slice(ws, sl, brief, emit=emit, entry=entry, bp=bp,
                                refresh=force_refresh or bool(_guilty) or _auth_refresh,
                                entry_example=slicer.entry_example(probes),
                                fail_note=fail_note,
                                groups=_ex_groups,
                                # 只有**本片确实有 CLI 能力**时才走 cli_run 这条路：
                                # 否则 entry 为空，而 all_cli 非空 → 裁剪白名单里没有契约里的模块
                                # → 整份契约被裁成空的（"解析出 0 个函数"，实测 mid2 卡了 3 次）。
                                core_probes=(all_cli or None) if cli_probes else None,
                                keep_modules=[m for cid in sl["caps"]
                                              if bp.by_id(cid)
                                              for m in (bp.by_id(cid).modules or [])]
                                + _guilty,
                                # 只丢已通过且本片不需要的模块：裁剪的 modules 提示不可靠，
                                # 用白名单会把该写的模块裁掉（实测 exp1b：logparse.py 被裁 → 空转）
                                drop_modules=[m for c in bp.caps if c.status == "done"
                                              for m in (c.modules or [])
                                              if m not in {x for cid in sl["caps"] if bp.by_id(cid)
                                                           for x in (bp.by_id(cid).modules or [])}],
                                web_probes=probes)
        if cg.get("ok"):
            # §20.4 落点 1（接线）：原始需求里的【必须产出的文件】段 → 本片契约文本。
            # 为什么引擎插而不是指望合成契约的模型抄：contractgen 是模型翻译，散文段
            # 可能被改写/丢掉；判据又按它验收 → 段丢了 = 永远不验收 = 永远不做
            # （§20.1 证据 2 的机制化修复）。注入是幂等的（同 path 不重复）。
            try:
                from . import contract as _cm_inj
                _arts_goal = _cm_inj.parse_artifacts(goal or "")
            except Exception:                            # noqa: BLE001
                _arts_goal = []
            if _arts_goal:
                _sl_mods = set(sl.get("modules") or [])
                _arts_sl = [a for a in _arts_goal
                            if (getattr(a, "owner", "") or "") in _sl_mods
                            or not getattr(a, "owner", "")]
                if _arts_sl:
                    _fixed_txt = _cm_inj.inject_artifacts(str(cg.get("text") or ""),
                                                          _arts_sl)
                    if _fixed_txt != cg.get("text"):
                        cg["text"] = _fixed_txt
                        try:
                            Path(str(cg.get("path") or "")).write_text(_fixed_txt,
                                                                       encoding="utf-8")
                        except Exception:                # noqa: BLE001
                            pass
                        if emit:
                            emit("artifact_injected",
                                 {"slice": sl.get("id"),
                                  "files": [a.path for a in _arts_sl[:6]]})
            # §20.6 C 档落点 2/3/4（接线）：原始需求里的工程声明段 → 本片契约文本。
            # 与上面产物文件段同一条铁律：contractgen 是模型翻译，【数据库迁移】/
            # 【依赖锁定】/【服务编排】/【健康检查】/【可观测性】这类散文段会被
            # 改写或丢掉，段丢了 = 判据永远不验收 = 声明永远不做（§20.1 证据 2）。
            # 引擎确定性注入（幂等、无段则逐字返回原文），不吃 token、不靠模型。
            try:
                from . import enterprise as _ent
                _eng_txt = _ent.inject_sections(str(cg.get("text") or ""), goal or "")
                if _eng_txt != cg.get("text"):
                    cg["text"] = _eng_txt
                    try:
                        Path(str(cg.get("path") or "")).write_text(_eng_txt,
                                                                   encoding="utf-8")
                    except Exception:                    # noqa: BLE001
                        pass
                    if emit:
                        emit("engineering_injected",
                             {"slice": sl.get("id"),
                              "specs": [s.path for s in _ent.parse_sections(goal or "")][:6]})
            except Exception:                            # noqa: BLE001
                pass
            # 罪魁文件没有函数条目 → 机械补条目（否则模块在契约里、订单为零 → 空转）
            if _guilty and _seed_guilty_funcs(Path(cg["path"]), _guilty, ws) and emit:
                emit("guilty_funcs_seeded", {"files": _guilty[:4], "slice": sl["id"]})
            _aligned = align_probe_modules(bp, cg.get("text") or "")
            if _aligned:
                bp.notes.append("探针模块名对齐：" + "；".join(_aligned[:4]))
                blueprint.save(bp, ws)
                if emit:
                    emit("probe_aligned", {"changes": _aligned[:6]})
        if not cg.get("ok"):
            # 契约合成失败时**先缩小切片再试**，别急着把能力降级。
            # 实测（run 12）：一片 4 条 CLI 能力连续三次合成失败 → 4 条全被降级，
            # 而真正的原因往往只是"这一片太大/太杂，合成出来的契约不合规"。
            contract_fail_streak += 1
            # 同样有地板：缩片是为了让契约能合成出来，不是为了把活拆成碎片。
            funcs_cap = max(SLICE_SHRINK_FLOOR, int(funcs_cap * 0.5))
            # **不许记到 fails 上**（2026-11-07 实测）：fails 是"这条能力做不出来"的证据，
            # 而契约合成失败是**系统自己没把考卷出对**，跟能力难度无关。
            # 旧逻辑把它算进 fails → 3 次后 replan 把 4 条能力全标 descoped，
            # 整轮 9.1%、¥0.00 收工，报告写"连续 3 次尝试仍未通过"，把系统 bug 栽给需求。
            why = "契约合成失败：" + "；".join(cg.get("problems") or [])[:100]
            hist.append({"slice": sl["id"], "caps": sl["caps"], "cost_cny": 0.0,
                         "gained": [], "why": why[:160]})
            if emit:
                emit("slice_contract_failed", {"slice": sl["id"], "problems": (cg.get("problems") or [])[:3],
                                               "next_funcs_cap": funcs_cap,
                                               "streak": contract_fail_streak})
            if contract_fail_streak >= MAX_CONTRACT_FAIL:
                # 连着出不出考卷 = 系统侧问题。如实停下并说清是谁的问题，
                # 不降级任何能力（降级会污染蓝图，下一次重跑还得手工清）。
                reason = ("契约合成连续失败 %d 次（系统侧问题，不是能力做不出来）：%s"
                          % (contract_fail_streak,
                             "；".join((cg.get("problems") or [])[:2])[:200]))
                break
            continue
        contract_fail_streak = 0

        # 探针期望值与契约示例对齐（契约是考卷）：不同步则探针必红、能力永远 absent
        if _sync_probe_expectations(bp, sl, cg.get("text") or "", emit=emit):
            blueprint.save(bp, ws)

        # 唯一停机线 = 系统总预算（写在 .sixcoder/budget.json，用户不可改）。
        # 这里只取数给 UI 展示"系统预算剩余"，不再做任何片级封顶（无用户上限）。
        _sys_cap = _be.load_system_budget(ws)
        _sys_left = round(_sys_cap - spent, 2) if _sys_cap > 0 else 0.0
        green_before = set(bp.done())
        if emit:
            emit("slice_start", {"slice": sl["id"], "caps": sl["caps"], "funcs": sl["funcs"],
                                 "funcs_cap": funcs_cap,
                                 "system_budget_cny": _sys_cap,
                                 "system_budget_left": _sys_left, "contract": cg["path"],
                                 "why": frontier.why(bp, bp.by_id(sl["caps"][0]))
                                 if bp.by_id(sl["caps"][0]) else ""})
            # 接口变更通道（片开始）：契约若与开工时的锁定不一致，受影响模块先回待办
            # （这一片本来就要按新契约重写它们），然后重新锁定。片结束时只重锁、不清零。
            try:
                from . import interfaces as _ifc_s
                _ds = _ifc_s.apply_drift(ws, phase="start")
                if _ds.get("hard"):
                    emit("interface_drift", {"phase": "start", "reset": len(_ds["hard"]),
                                             "modules": [c["module"] for c in _ds["hard"]][:6]})
            except Exception as exc:                     # noqa: BLE001
                emit("notice", {"message": f"接口锁定跳过：{type(exc).__name__}"})
        # §20.5 B 档落点 1（最小闭环）：仓库里有 package.json 且声明了 build 时，
        # 写码前先 npm install + build。没有前端工程的项目零影响（没 package.json 直接跳过）；
        # npm 不在 PATH → 事件留痕并跳过（不许炸掉夜班）。
        try:
            _wb = ensure_web_build(ws)
            if _wb.get("ran") and emit:
                emit("web_build", _wb)
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"前端构建跳过：{type(exc).__name__}: {exc}"[:160]})
        res = runner(ws, brief, cg["path"], model=model, max_rounds=max_rounds,
                     emit=emit, slice_id=sl["id"])
        spent += float(res.get("cost_cny") or 0.0)
        _save_spent(ws, goal, spent)
        slices_run += 1
        # Web：**引擎补路由接线**（方法/路径/状态码/请求体都是探针给的，不是设计判断）。
        # 作者只写服务层函数；路由由引擎挂，避免"作者把 router 写丢 → 整片 404 → 振荡停机"。
        try:
            _ctext = cg.get("text") or Path(cg.get("path") or "").read_text(
                encoding="utf-8", errors="replace")
            _plan, _fmap = _web_route_plan(bp, sl, _ctext)
            for _mod, _routes in _plan.items():
                _wr = ensure_web_router(ws, _mod, _routes, _fmap.get(_mod) or {})
                if _wr.get("wrote"):
                    bp.notes.append(f"引擎路由接线：{_mod}（{_wr.get('why')}）")
                    if emit:
                        emit("web_routes", {"module": _mod, "routes": _wr.get("routes")})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"路由接线跳过：{type(exc).__name__}: {exc}"[:160]})
        # 本地助手该做的事：入口接线由**引擎**负责（确定性、0 token、可溯源）。
        # CLI 片跑完**强制**把入口恢复成引擎接线（远端整文件重写会把它盖掉）；
        # 其余情况只补一行 `__main__`（实测 run 11：不补 CLI 能力会成批被降级）。
        if cli_probes and entry:
            _w2 = write_entry_wrapper(ws, entry, force=True)
            bp.notes.append(f"引擎生成入口：{_w2.get('why')}（{entry}）")
            if emit:
                emit("wiring", {"entry": entry, "why": _w2.get("why")})
        elif entry:
            wiring = ensure_entry_wiring(ws, entry)
            if wiring.get("patched"):
                bp.notes.append(f"引擎补全：{wiring.get('why')}（{entry}）")
                if emit:
                    emit("wiring", {"entry": entry, "why": wiring.get("why")})
        else:
            # 纯 Web 片：刷新入口（新模块要 include 进来；已有模块的内容一律不碰）
            _web = write_web_entry(ws, bp)
            if _web.get("wrote"):
                bp.notes.append(f"引擎生成 Web 入口：{_web.get('why')}")
                if emit:
                    emit("web_entry", {"modules": _web.get("modules") or []})

        # 依赖自举：作者可能引入新的第三方包（jwt 之类）。缺包会让**整个 app 导入失败** →
        # 所有能力一起变红，系统会判"做不出来"。那不是代码错，是环境没跟上（实测 2026-11-07：
        # 6/9 一夜掉到 0/8，全因 PyJWT 没装）。所以每次测覆盖之前先扫 import、补装缺失。
        try:
            from . import deps as _deps
            _dep = _deps.ensure(ws)
            try:
                from . import shipgate as _ship_env
                _envw = _ship_env.ensure_env(ws)
                if _envw.get("wrote") and emit:
                    emit("env_ready", {"vars": _envw.get("names"), "wrote": _envw.get("wrote")})
            except Exception:                            # noqa: BLE001
                pass
            if _dep.get("installed") and emit:
                emit("deps_installed", {"pkgs": _dep["installed"], "req": _dep.get("req_file")})
            if _dep.get("failed") and emit:
                emit("notice", {"message": "依赖装不上：" + "、".join(
                    str(x.get("pkg")) for x in _dep["failed"][:3])})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"依赖自举跳过：{type(exc).__name__}: {exc}"[:160]})

        # 测覆盖之前**必须**把路由补齐：作者整篇重写、回归硬门禁回滚都会抹掉引擎段，
        # 少了路由的能力会以"404"被判成做不出来（实测 2026-11-07 update/delete）。
        # 2026-11-12 修复：原来这里**直调 apply_web_wiring**，绕过 openhouse.wire 的
        # 快照 + 导入冒烟 + **接线回归守卫**（§11.4m.1）→ 接线批量重写打坏了已通过
        # 能力时，这条线上没有任何东西兜底。改成走 openhouse.wire。
        try:
            from . import openhouse as _oh_wire2
            _wired = _oh_wire2.wire(ws, bp, emit=emit)
            if _wired and emit:
                emit("web_routes", {"module": "（全部）", "routes": len(_wired),
                                    "detail": "；".join(_wired[:4])})
        except Exception as exc:                         # noqa: BLE001
            if emit:
                emit("notice", {"message": f"路由补齐跳过：{type(exc).__name__}: {exc}"[:160]})

        cov = recon(bp, ws)
        covmod.record(ws, cov, f"切片 {sl['id']} 之后")
        bp = covmod.mark(bp, cov)
        # 溯源：这一片碰过的能力都记下片号（能力 → 片 → 契约文件 → 交付报告）。
        # 观测策略改成"全都测"之后，可能出现**没被单独排片、却已经通过**的能力
        # （比如核心函数写好后，几条 CLI 能力一次就过了）。它们也要有出处，
        # 否则报告里出现"完成但没有来源"，可溯源性就断了。
        for cid in sl["caps"]:
            c = bp.by_id(cid)
            if c is not None and not c.slice_id:
                c.slice_id = sl["id"]
        for c in bp.caps:
            if c.status == "done" and not c.slice_id:
                c.slice_id = f"{sl['id']}·观测"
        blueprint.save(bp, ws)
        # ── 片内自验：把"跑"接回作者手里（跑→看真输出→改，不跨片重来）────────
        # 实测动机：作者盲写、探针事后判红、整片重来 —— 一轮就是一次全赔。
        # 这里在**同一片内**给它 1~N 步：每步引擎先跑探针拿真实 stdout/退出码，
        # 作者可以 @RUN 自己验证，交回整份文件；改完重新观测。判定权仍在探针手里。
        if selfcheck:
            _red = []
            for cid in sl["caps"]:
                c = bp.by_id(cid)
                if c is not None and c.status != "done":
                    _red.append(c)
            if _red:
                _before = _global_spend_marker()
                fx = selfcheck_loop.repair(ws, _red, llm=llm, emit=emit,
                                           max_steps=SELFCHECK_STEPS)
                _sc_cost = round(max(0.0, _global_spend_marker() - _before), 4)
                spent += _sc_cost
                _save_spent(ws, goal, spent)
                if fx.get("changed"):
                    # 改过代码就必须重新观测：作者说"好了"不算数
                    cov = recon(bp, ws)
                    covmod.record(ws, cov, f"片内自验 {sl['id']}")
                    bp = covmod.mark(bp, cov)
                    for cid in sl["caps"]:
                        c2 = bp.by_id(cid)
                        if c2 is not None and not c2.slice_id:
                            c2.slice_id = sl["id"]
                    for c in bp.caps:
                        if c.status == "done" and not c.slice_id:
                            c.slice_id = f"{sl['id']}·自验"
                    blueprint.save(bp, ws)
                _sc = {"slice": sl["id"], "fixed": fx.get("fixed") or [],
                       "still": fx.get("still") or [], "steps": fx.get("steps") or 0,
                       "stop": fx.get("stop") or "", "changed": fx.get("changed") or [],
                       "cost_cny": _sc_cost}
                selfchecks.append(_sc)
                bp.notes.append(
                    f"片内自验 {sl['id']}：修好 {len(_sc['fixed'])} 条，仍红 {len(_sc['still'])} 条"
                    f"（{_sc['stop']}，{_sc['steps']} 步，¥{_sc_cost}）")
                if emit:
                    emit("selfcheck_done", _sc)
                blueprint.save(bp, ws)
        gained = [c for c in sl["caps"] if (bp.by_id(c) and bp.by_id(c).status == "done")]
        # 回归：这一片把**之前已经通过**的能力改坏了。整文件重写最容易犯这个错
        # （实测 run 4：第 2、3 片把第 1 片绿掉的 CLI 接线改坏，覆盖从 4 掉回 2）。
        # 必须显式记下来：写进备注、进报告、并让下一片的简报带命令去修。
        # ── 「每次调用必须有意义」的机械判定 ──────────────────────────────
        # 一次切片跑完，"未通过能力集合"必须净减；净减为 0 = 这次调用没意义。
        # 连续无效时不许重复同样的调用：强制重合成契约 + 缩小切片 + 补证据。
        if gained:
            force_refresh = False
            no_gain_streak = 0          # 有收获 → 连续计数清零
        else:
            wasted += 1
            # **不许因为"这次没收获"就换考卷。**
            # 同一片重跑必须用同一份考卷 —— 这是本文件自己写在缓存理由里的口径
            # （"缓存不是为了省钱，是为了可复现"）。旧实现一旦 waste 就 force_refresh，
            # 新考卷的语义随之改变（实测同一片三份考卷：`{"student","lecturer"}` →
            # `{"student_id","lecturer_id"}`），于是之前通过的模块全被判"退化→强制重写"，
            # 重写完仍不匹配新考卷 → 又没收获 → 又换考卷……自我强化的死亡螺旋。
            # 真正该变的是**打法与证据**（fail_note 已经带上了），不是考卷。
            # 考卷只有在结构不可用（合成失败 / 解析出 0 个函数）时才重做，那条路径自带 ok=False。
            # 缩片的触发条件：**连续两片无收获**才缩（2026-11-09 用户口径：
            # 「觉得写不出来了是不是需要调整」——调整要基于"确实卡住了"的证据，
            # 不是"这一片没过"就立刻缩小）。
            #
            # 旧口径（每片无收获就 ×0.8）的两个问题：
            #   1) **下限是 1** → 连续几片不过就退化成"一次一个函数"，
            #      碎片化到没有业务纵切，永远做不出有难度的东西；
            #   2) 单片失败的原因可能只是"这一片恰好难"或"引擎这一次的判据/接线有问题"
            #      （今天就发生过：路由接线把 batches 判成没有匹配函数 → 恒 404 → gained=[]），
            #      立刻缩片等于**用引擎的缺陷去惩罚远端的规模**。
            # 现在：连续两片无收获才缩，且有地板（SLICE_SHRINK_FLOOR）。
            no_gain_streak += 1
            if no_gain_streak >= 2:
                funcs_cap = max(SLICE_SHRINK_FLOOR, int(funcs_cap * 0.8))
            if emit:
                emit("slice_wasted", {"slice": sl["id"], "wasted": wasted,
                                      "funcs_cap": funcs_cap,
                                      "no_gain_streak": no_gain_streak,
                                      "note": ("连续两片无收获 → 缩片 + 换打法" if no_gain_streak >= 2
                                               else "这一片没有净收获：考卷保持不变，"
                                                    "换打法（连续两片无收获才缩片）")})
        best_done = max(best_done, len(bp.done()))
        lost = sorted(green_before - set(bp.done()))
        for cid in lost:
            c = bp.by_id(cid)
            if c is None:
                continue
            detail = str(c.why or "（原因见覆盖报告）")[:120]
            c.why = (f"⚠ 被切片 {sl['id']} 的改动改坏（之前是通的）：{detail}")[:200]
            regressions.append({"cap": cid, "slice": sl["id"], "detail": detail})
            # 备注是持久的（能力的 why 会在修好之后被清空），回归事实不能因此消失
            bp.notes.append(f"回归：{cid} 被切片 {sl['id']} 改坏（之前是通的）→ 重新做")
        if lost and emit:
            emit("regression", {"slice": sl["id"], "caps": lost})
        for cid in sl["caps"]:
            c = bp.by_id(cid)
            if c is not None and c.status == "done":
                fails.pop(cid, None)
            else:
                fails[cid] = fails.get(cid, 0) + 1
        hist.append({"slice": sl["id"], "caps": sl["caps"], "cost_cny": round(float(res.get("cost_cny") or 0.0), 4),
                     "gained": gained, "spent_s": res.get("spent_s"),
                     "why": (res.get("reason") or "")[:100]})
        if emit:
            emit("slice_done", {"slice": sl["id"], "gained": gained,
                                "left": sorted(set(sl["caps"]) - set(gained)),
                                "cost_cny": res.get("cost_cny"), "spent_cny": round(spent, 4),
                                "coverage": covmod.ratios(cov)})
            # git 记账：有净收获的片 = 一个可回滚的历史点（core/vcs.py 只 add+commit，
            # 永不 reset/rebase/删 .git —— 历史保护红线见该模块 docstring）。
            if gained:
                try:
                    from . import vcs as _vcs
                    _vc = _vcs.commit_slice(ws, sl_id=sl["id"], gained=gained, emit=emit)
                    if not _vc.get("ok") and emit:
                        emit("notice", {"message": f"git 提交跳过：{_vc.get('why')}"})
                except Exception as exc:             # noqa: BLE001
                    emit("notice", {"message": f"git 提交跳过：{type(exc).__name__}"})
            else:
                # 没收获：探针 traceback 指向的罪魁文件排队给下一片强制修复
                try:
                    _gfiles = _probe_guilty_files(ws, sl, bp)
                    if _gfiles:
                        (Path(ws) / ".sixcoder" / "probe_repair.json").write_text(
                            json.dumps({"slice": sl["id"], "files": _gfiles[:6]},
                                       ensure_ascii=False), encoding="utf-8")
                        if emit:
                            emit("probe_repair_queued", {"slice": sl["id"],
                                                         "files": _gfiles[:6]})
                except Exception as exc:             # noqa: BLE001
                    emit("notice", {"message": f"罪魁文件提取跳过：{type(exc).__name__}"})
            # 架构约束（fitness functions）：每片结束算一次，抓"这一片把边界改坏了"。
            # 没有 arch.json 的项目按路径推断 → 只报告不拦（推断值不足以判死）；
            # 有声明（骨架期生成）才当红线。
            try:
                from . import archrules as _ar
                _arch = _ar.check(ws)
                emit("arch_rules", {"slice": sl["id"], "ok": _arch["ok"],
                                    "source": _arch["spec_source"],
                                    "counts": _arch["counts"],
                                    "dup": _arch["metrics"]["dup"]["ratio"]})
            except Exception as exc:                     # noqa: BLE001
                emit("notice", {"message": f"架构约束跳过：{type(exc).__name__}"})
            # 接口清单（§8.4 变更通道）：第一次锁定；之后改签名/删函数 → 受影响模块回待办。
            # 只新增函数不算改接口（不清零），否则每加一个函数都要重验一遍。
            try:
                from . import interfaces as _ifc
                _dr = _ifc.apply_drift(ws, phase="end")
                if _dr.get("first_time"):
                    emit("interface_locked", {"modules": len(_ifc.load(ws))})
                elif not _dr.get("ok"):
                    emit("interface_drift", {
                        "resig": [f"{c['module']}::{','.join(c['resig'][:3])}"
                                  for c in _dr.get("hard") or []][:5],
                        "removed_mods": [c["module"] for c in _dr.get("hard") or []
                                         if c.get("removed")][:5],
                        "unresolved": [f"{u['module']}.{u['symbol']}"
                                       for u in _dr.get("unresolved") or []][:5],
                        "reset": len(_dr.get("hard") or [])})
            except Exception as exc:                     # noqa: BLE001
                emit("notice", {"message": f"接口清单跳过：{type(exc).__name__}"})

        stuck = [cid for cid, n in fails.items() if n >= replan.FAIL_DESCOPE]
        if covmod.stalled(ws, bp) or stuck:
            if replans >= MAX_REPLANS:
                reason = "重规划次数到顶"
                break
            bp, cov, calls, replans, halt = _do_replan(
                bp, cov, ws, llm, recon, fails, hist, replans, calls, emit,
                ("覆盖停滞" if covmod.stalled(ws, bp) else f"能力反复失败：{', '.join(stuck[:3])}"))
            if halt:
                reason = "自动策略已穷尽，需人工换方向"
                break

    ratios = covmod.ratios(cov)
    descoped = [c.id for c in bp.caps if c.status == "descoped"]
    blocked = [{"cap": c.id, "why": w} for c, w in frontier.blocked_caps(bp)]
    gaps = [c.id for c in bp.caps if c.status not in ("done", "descoped")]
    nxt = [(round(s, 3), c.id) for s, c in frontier.rank(bp)[:8]]
    # ok 的口径：**只有每条能力都真通过观测才算完成**。降级不是完成，
    # 受阻也不是完成 —— 报告里"全部通过"而实际有缺口，是最严重的失败形态。
    # 上线机制：README / 引擎生成的 pytest / 体检（全部引擎做，不占远端能力）。
    # 注意：ship_check 是 **Web 上线体检**（app.main / /docs / uvicorn / test_api.py），
    # 只对含 http 探针的项目生效；非 Web 项目（CLI/库/算法）跳过它，否则会误判
    # "测试通过"必红（没有 test_api.py）—— 实测 check_auto 的 CSV 工具 fixture 就这样
    # 被 ship_check 永久判成未完成。
    _is_web = any(str((c.probe or {}).get("kind") or "") == "http" for c in bp.caps)
    try:
        from . import shipgate as _ship
        try:
            _ship.ensure_env(ws)
            _ship.write_env_block(ws, bp)
        except Exception:                                # noqa: BLE001
            pass
        _docs = _ship.write_docs(ws, bp)
        if _is_web:
            _tests = _ship.write_tests(ws, bp)
            _ship_rep = _ship.ship_check(ws, bp)
        else:
            _tests = {}
            _ship_rep = {"ok": True, "rows": [{
                "name": "上线体检", "ok": True,
                "detail": "非 Web 项目，跳过 Web 上线体检（README / 依赖已生成）"}]}
        if emit:
            emit("ship_artifacts", {"docs": _docs.get("wrote"), "tests": _tests.get("tests") or 0,
                                    "path": _tests.get("path") or ""})
            for _row in _ship_rep["rows"]:
                emit("ship_check", _row)
    except Exception as exc:                             # noqa: BLE001
        _ship_rep = {"ok": False, "rows": [{"name": "上线体检", "ok": False,
                                            "detail": f"{type(exc).__name__}: {exc}"[:150]}]}
        if emit:
            emit("notice", {"message": f"上线体检失败：{type(exc).__name__}"})
    _ship_bad = [r["name"] for r in _ship_rep.get("rows") or [] if not r.get("ok")]
    if _ship_bad and not gaps:
        # 能力都过了但发布条件不齐：这是"看起来做完了、其实不能交付"，必须如实标成未完成
        reason = ("上线条件不齐：" + "、".join(_ship_bad[:3])) if not reason else                  (reason + "；上线条件不齐：" + "、".join(_ship_bad[:3]))

    res = {"ok": (not gaps and not blocked and not descoped and _ship_rep.get("ok")),
           "ship": _ship_rep, "reason": reason,
           "blueprint": bp, "coverage": cov,
           "ratios": ratios, "spent_cny": round(spent, 4), "elapsed_s": round(time.time() - t0, 1),
           "slices": slices_run, "replans": replans, "calls": calls, "history": hist,
           "descoped": descoped, "blocked": blocked, "gaps": gaps,
           "regressions": regressions,
           "wasted": wasted, "best_done": best_done,
           "selfchecks": selfchecks,
           "selfcheck_fixed": sum(len(s.get("fixed") or []) for s in selfchecks),
           "meaningful_rate": round(1.0 - wasted / max(1, slices_run), 3),
           "next": [{"cap": cid, "why": frontier.why(bp, bp.by_id(cid))}
                    for _s, cid in nxt if bp.by_id(cid)]}
    res["report"] = str(write_report(ws, bp, res))
    if emit:
        emit("autonomy_done", {"reason": reason, "coverage": ratios, "spent_cny": res["spent_cny"],
                               "slices": slices_run, "replans": replans,
                               "report": res["report"]})
    return res


def _stall_streak(hist: list[dict]) -> int:
    """尾部连续「覆盖停滞」重规划次数；遇到正常切片或其它原因即断。

    有进展就重置，这正是「人看一眼说别再这么跑了」的机械判据：
    连续 N 次重规划都没换来新增 done，才升级策略。
    """
    n = 0
    for h in reversed(hist or []):
        if h.get("kind") != "stall":
            break
        n += 1
    return n


def _do_replan(bp, cov, ws, llm, recon, fails, hist, replans, calls, emit, reason):
    """重规划一次并立刻重新观测（降级/补能力之后，覆盖必须重新算）。

    返回增加第 5 项 `halt`：策略阶梯判定「自动策略已穷尽」时，主循环停机请人换方向，
    而不是继续烧到第 6 次才报「重规划次数到顶」。
    """
    is_stall = str(reason or "").startswith("覆盖停滞")
    # +1 把**当前这次**也算进去：第 2 次停滞就进 focus，第 3 次进 escalate。
    streak = (_stall_streak(hist) + 1) if is_stall else 0
    rep = replan.replan(bp, cov=cov, ws=ws, llm=llm, reason=reason, fails=fails,
                        stall_streak=streak, emit=emit)
    calls += int(rep.get("calls") or 0)
    bp = rep["blueprint"]
    # 失败计数**不能整表清空**：清空之后，一条永远做不成的能力会在
    # "停滞→重规划→再失败→再停滞"的循环里重新从 1 数起，永远到不了降级阈值，
    # 最后只能靠 MAX_REPLANS 兜底（实测：6 次重规划后放弃，一条能力都没被降级）。
    # 只清掉"已经通过/已经降级/已经不在蓝图里"的条目。
    for cid in list(fails):
        c = bp.by_id(cid)
        if c is None or c.status in ("done", "descoped"):
            fails.pop(cid, None)
    cov = recon(bp, ws)
    covmod.record(ws, cov, f"重规划后观测（{reason[:20]}）")
    bp = covmod.mark(bp, cov)
    blueprint.save(bp, ws)
    # halt = 自动策略穷尽。停机前让远端**提出框架外方向**（只提方案，绝不自动执行），
    # 提案 append-only 落盘 .sixcoder/strategy_proposals.jsonl，由人裁决。
    # 提出 ≠ 拍板：policy.py 禁止的是远端替人拍板，不是远端提出。
    if rep.get("halt"):
        from . import strategy as _strat2
        _strat2.on_halt(
            ws, bp, emit=emit,
            facts={"done": sum(1 for c in bp.caps if c.status == "done"),
                   "total": len(bp.caps), "streak": streak})
    hist.append({"slice": f"replan-v{bp.version}", "caps": [], "cost_cny": 0.0, "gained": [],
                 "kind": "stall" if is_stall else "fail",
                 "strategy": rep.get("strategy") or "flat",
                 "why": f"重规划：{reason}；diff={rep.get('diff')}"[:160]})
    return bp, cov, calls, replans + 1, bool(rep.get("halt"))


def _fail(ws, reason: str, blueprint=None) -> dict:
    return {"ok": False, "reason": reason, "blueprint": blueprint, "coverage": {},
            "ratios": {"done": 0, "total": 0, "pct": 0.0}, "spent_cny": 0.0, "elapsed_s": 0.0,
            "slices": 0, "replans": 0, "calls": 0, "history": [], "descoped": [],
            "blocked": [], "gaps": [], "next": [], "report": "",
            "regressions": [], "wasted": 0, "best_done": 0, "meaningful_rate": 1.0}


# ---------------------------------------------------------------- 报告

def _cause(detail: str) -> str:
    """把失败证据归因：是**模型没写对**，还是**机制/环境**没到位。

    这条口径很重要（用户要的正是"最后只剩远端能力不足"）：如果报告里混着
    "缺依赖/导入失败"这类机制问题，那就不能算"能力不足"，得先修机制。
    """
    d = str(detail or "")
    if any(k in d for k in ("engine_error", "没有可用的 ASGI", "ModuleNotFound",
                            "ImportError", "SyntaxError", "缺依赖", "装不上",
                            "没有 httpx", "并发判据跑不起来")):
        return "机制/环境（先修引擎侧）"
    if "NotImplementedError" in d:
        return "模型未实现（函数是空桩）"
    if any(k in d for k in ("状态码", "响应里没有", "JSON 字段不符", "没有任何一个关键词",
                            "条记录", "并发", "条，实际")):
        return "模型实现不满足判据"
    if "抛异常" in d:
        return "模型实现抛异常"
    return "未分类"


def write_report(ws, bp, res: dict) -> Path:
    """AUTONOMY.md：目标、覆盖矩阵、缺口、每片账本、怎么续跑。

    这份文件是给人看的**唯一**入口：它必须能回答"做完了什么、缺什么、为什么缺、
    下一刀会砍在哪"。报告里出现"全部通过"而实际有缺口，是最严重的失败形态。
    """
    r = res.get("ratios") or {}
    cov = res.get("coverage") or {}
    caps = cov.get("caps") or {}
    from . import probe as _probe
    # 产物规模与**实测单价**：里程碑的硬指标是"≤¥0.15/100 行"，所以报告必须现算，
    # 不能只报总花费（总花费小可能是压根没写几行）。
    try:
        from . import scale as _scale
        _lines = _scale.count_lines(Path(ws))
    except Exception:                                    # noqa: BLE001
        _lines = {"prod_lines": 0, "modules": 0}
    _prod = int(_lines.get("prod_lines") or 0)
    _mods = _lines.get("modules") or 0
    try:
        _nmods = len(_mods)
    except TypeError:
        _nmods = int(_mods or 0)
    _spent = float(res.get("spent_cny") or 0.0)
    # 系统总预算（唯一停机线，写在 .sixcoder/budget.json，用户不可改）。
    _sys_cap = _be.load_system_budget(ws)
    _sys_left = round(_sys_cap - _spent, 2) if _sys_cap > 0 else 0.0
    _unit = round(_spent / _prod * 100, 3) if _prod else 0.0
    _left_funcs = sum(c.funcs for c in bp.caps if c.status not in ("done", "descoped"))
    _left_cost = round(_left_funcs * CNY_PER_FUNC * SLICE_RETRY_MARGIN, 2)
    lines = ["# 自主生成报告", "",
             f"目标：{bp.goal}", "",
             f"- 蓝图版本：v{bp.version}（能力 {len(bp.caps)} 条）",
             f"- 覆盖：**{r.get('done', 0)}/{r.get('total', 0)}（{r.get('pct', 0)}%）**",
             f"- 产物：{_prod} 行（{_nmods} 个模块）"
             f"｜**实测单价 ¥{_unit} / 100 行**" if _prod else "- 产物：0 行",
             f"- 花费：¥{res.get('spent_cny', 0):.2f}"
             + (f" / 系统总预算 ¥{_sys_cap:.2f}" if _sys_cap > 0 else ""),
             f"- 耗时：{res.get('elapsed_s', 0) / 60:.1f} 分钟｜切片 {res.get('slices', 0)} 轮"
             f"｜重规划 {res.get('replans', 0)} 次｜远端调用 {res.get('calls', 0)} 次",
             f"- 剩余：{_left_funcs} 个函数 ≈ 还要 ¥{_left_cost:.2f}"
             + (f"，系统预算还余 ¥{max(0.0, _sys_left):.2f}"
                if _sys_cap > 0 else ""),
             f"- 意义性：切片 {res.get('slices', 0)} 次里无效 {res.get('wasted', 0)} 次（有效调用率 {res.get('meaningful_rate', 1.0):.0%}）｜历史最好完成数 {res.get('best_done', 0)}",
             f"- 结束原因：{res.get('reason', '')}", ""]

    lines += ["## 覆盖矩阵", "",
              "| 状态 | 能力 | 内容 | 观测方式 | 依据 |", "|---|---|---|---|---|"]
    marks = {"done": "✓ 完成", "absent": "✗ 缺", "blocked": "⊘ 受阻",
             "descoped": "降级", "unsupported": "? 无法观测"}
    for c in bp.caps:
        row = caps.get(c.id) or {}
        st = row.get("status") or c.status
        detail = str(row.get("detail") or c.why or "")
        lines.append(f"| {marks.get(st, st)} | `{c.id}` | {c.desc[:40]} | "
                     f"{_probe.describe(c.probe)[:44]} | {detail[:52] or '-'} |")

    if [c for c in bp.caps if c.status == "descoped"]:
        lines += ["", "## 降级（主动放弃，附原因）", ""]
        for c in bp.caps:
            if c.status == "descoped":
                lines.append(f"- `{c.id}`：{c.why or '未说明'}")

    blocked = res.get("blocked") or []
    if blocked:
        lines += ["", "## 受阻（依赖被降级/不存在 → 永远轮不到）", ""]
        for b in blocked:
            lines.append(f"- `{b['cap']}`：{b['why']}")

    regs = res.get("regressions") or []
    if regs:
        lines += ["", "## 回归（后面的切片把已通过的能力改坏了）", "",
                  "| 能力 | 被哪片改坏 | 证据 |", "|---|---|---|"]
        for r in regs[-20:]:
            lines.append(f"| `{r['cap']}` | `{r.get('slice')}` | {str(r.get('detail'))[:60]} |")
        lines.append("")
        lines.append("> 这几条原本是通过的。下一轮会重新做它们；如果反复来回，"
                     "说明整文件重写太粗暴（应该改成补丁式修改）。")

    gaps = res.get("gaps") or []
    if gaps:
        from collections import Counter as _Counter
        _causes = {}
        for cid in gaps:
            _row = caps.get(cid) or {}
            _causes[cid] = _cause(_row.get("detail") or "")
        _cnt = _Counter(_causes.values())
        lines += ["", "## 缺口（还没通过的能力）", "",
                  "**归因汇总**：" + "；".join(f"{k} {v} 条" for k, v in _cnt.most_common()), ""]
        for cid in gaps:
            c = bp.by_id(cid)
            row = caps.get(cid) or {}
            lines.append(f"- `{cid}`：{row.get('detail') or (c.desc if c else '')}"
                         f"｜归因：{_causes.get(cid, '')}")
        _mech = _cnt.get("机制/环境（先修引擎侧）", 0)
        if _mech:
            lines += ["", f"> ⚠ 有 {_mech} 条是**机制/环境**问题（缺依赖、导入失败之类），"
                          "不该算成模型能力不足：先修引擎侧，再重跑。"]
        else:
            lines += ["", "> 这些缺口的归因都在模型侧（没写 / 写错 / 空桩），机制侧已无阻塞。"]

    ship = res.get("ship") or {}
    if ship.get("rows"):
        lines += ["", "## 上线体检（引擎跑的，与能力覆盖**分开**看）", "",
                  "> 能力覆盖只说明「每条能力都被观测过」；这一节回答「能不能交付」。", "",
                  "| 项 | 结果 | 说明 |", "|---|---|---|"]
        for row in ship["rows"]:
            lines.append(f"| {row.get('name')} | {'✓' if row.get('ok') else '✗'} | "
                         f"{str(row.get('detail') or '')[:72]} |")
        if not ship.get("ok"):
            lines += ["", "**结论：能力可能都过了，但发布条件不齐 —— 不能算交付完成。**"]

    nxt = res.get("next") or []
    if nxt:
        lines += ["", "## 下一步会先做这些（frontier 确定性排序）", ""]
        for i, row in enumerate(nxt[:6], start=1):
            lines.append(f"{i}. `{row['cap']}` —— {row.get('why', '')}")

    hist = res.get("history") or []
    if hist:
        lines += ["", "## 账本（每片花费与产出）", "",
                  "| 切片 | 能力 | 新增通过 | 花费 | 备注 |", "|---|---|---|---|---|"]
        for h in hist:
            lines.append(f"| `{h.get('slice')}` | {', '.join(h.get('caps') or [])[:44]} | "
                         f"{', '.join(h.get('gained') or []) or '-'} | "
                         f"¥{float(h.get('cost_cny') or 0):.3f} | {str(h.get('why') or '')[:40]} |")

    lines += ["", "## 怎么续跑", "",
              "```",
              f'sixcoder night --repo "{ws}" --goal "{bp.goal[:40]}"',
              "```",
              "",
              "> 续跑没有预算参数：花的钱实时记到账本，唯一停机线是系统总预算 × 1.05"
              "（写在 `.sixcoder/budget.json`，由系统/运维设置，用户不可改）。",
              "续跑会读 `.sixcoder/blueprint.json`：已经通过的能力不会重做，"
              "降级和缺口会继续被观测。", ""]
    p = Path(ws) / AUTONOMY_REPORT
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def describe(res: dict) -> str:
    """一行结论（CLI/GUI 共用）。"""
    r = res.get("ratios") or {}
    return (f"{'完成' if res.get('ok') else '未完成'}：覆盖 {r.get('done', 0)}/{r.get('total', 0)}"
            f"（{r.get('pct', 0)}%）｜¥{res.get('spent_cny', 0):.2f}"
            f"｜切片 {res.get('slices', 0)}｜{res.get('reason', '')}")


# ─────────────────────────────────────────────────────────────────────────────
# 阶段①澄清循环（开发文档 §13.2）：多轮 / 单文档 / 多选 / 双闸门
# ─────────────────────────────────────────────────────────────────────────────

def clarify_design_doc_to_arch(doc) -> dict:
    """把澄清产出的 DesignDoc 转成 night_auto 已支持的 arch 格式。"""
    qs = [
        {"id": c.key, "q": c.question, "options": c.options, "why": "", "default": c.options[0]}
        for c in doc.choices
    ]
    ans = {c.key: c.picked for c in doc.choices if c.picked}
    return {"questions": qs, "answers": ans}


def _persist_stack_choice(ws, doc) -> dict:
    """把 DesignDoc 里 key == "stack" 的选择写进 .sixcoder/stack.json。

    返回 {"ok": bool, "stack": str, "path": str, "why": str}。

    识别规则（宽松但安全）：
      · 选项文本形如 "fastapi：说明" 或 "fastapi: 说明" -> 取冒号前的标识
      · 标识必须在引擎已支持的栈里（layerplan 注册表）
      · 形如 "不支持：..." 的选项**不落盘** —— 那是边界提示，不是可选技术栈
    """
    import json as _json_io
    import re as _re
    import time as _time_io
    picked = ""
    for c in (getattr(doc, "choices", None) or []):
        if str(getattr(c, "key", "")) != "stack":
            continue
        picked = str(getattr(c, "picked", "") or "").strip()
        break
    if not picked:
        return {"ok": False, "why": "澄清里没有选定技术栈（或用户没选）"}
    if picked.startswith("不支持"):
        return {"ok": False, "why": f"模型判定当前形态超出引擎能力：{picked}"}

    name = _re.split(r"[：:]", picked, maxsplit=1)[0].strip().lower()
    # 判定"引擎认不认识这个栈"，以 **layerplan 为准**（它决定生成骨架）。
    #
    # 为什么不用 stack.names()（2026-11-24 实测踩到）：
    #   stack._ADAPTERS 只有 fastapi/django —— 那是"有代码模板能渲染入口"的栈；
    #   cli / frontend 有生成计划和判据包，但还没有独立的 stack 适配器。
    #   于是用户选 cli -> 被判成"不认识的栈"，明明方案是支持的。
    #
    # 三个子系统各维护一份栈清单是 P0 留下的债（stack 2 / layerplan 4 / judges 3）。
    # 这里用并集判断"支持"，比任何单一来源都稳；真正的收敛留到后面单独一刀。
    known = set()
    try:
        from . import layerplan as _lp
        known |= {_p.stack for _p in _lp.default_registry().all() if getattr(_p, "stack", "")}
    except Exception:                                  # noqa: BLE001
        pass
    try:
        from . import stack as _stack_k
        known |= set(_stack_k.names())
    except Exception:                                  # noqa: BLE001
        known |= {"fastapi", "django"}
    # 短名 -> 生态包名的别名表（2026-11-24 打通时统一了两边命名）。
    # 为什么保留别名：澄清提示词、存量用户配置、测试里都还写着 cli / frontend，
    # 直接拒收会让它们全部报"不认识的栈"。别名表把两边接上。
    _ALIASES = {"cli": "cli_tool", "frontend": "frontend_spa"}
    if name in _ALIASES and _ALIASES[name] in known:
        name = _ALIASES[name]

    if name not in known:
        # **缺栈 -> 登记待协商请求**（2026-11-24：把 install_ecosystem 真正用起来）。
        #
        # 背景：协商机制早就支持 `install_ecosystem` 处方（deadlock.py:551），
        # 远端能现写一个 StackAdapter 装进生态库。但它的触发条件是"死局"
        # （反复回滚/振荡），而"缺栈"不表现为卡住 —— 引擎会按默认栈生成，
        # 判据全绿，从没触发过协商。实测：8 个生态包 installed_at 全是 None，
        # 审计里零条 install_ecosystem 记录。
        #
        # 这里补上真正的触发点：用户点名要的栈引擎没有 -> 登记请求，
        # 让协商阶段把它变成 install_ecosystem 处方去问远端。
        # **不拦截**（用户仍可继续用默认栈），只是让引擎知道"这里缺东西"。
        try:
            _req_dir = Path(ws) / ".sixcoder"
            _req_dir.mkdir(parents=True, exist_ok=True)
            _req = _req_dir / "pending_stacks.json"
            _items = []
            if _req.is_file():
                try:
                    _items = _json_io.loads(_req.read_text(encoding="utf-8")) or []
                except Exception:                    # noqa: BLE001
                    _items = []
            if name not in [str(x.get("stack") or "") for x in _items if isinstance(x, dict)]:
                _items.append({"stack": name,
                               "reason": "澄清时用户点名要这个栈，引擎没有",
                               "known": sorted(known),
                               "at": _time_io.strftime("%Y-%m-%d %H:%M:%S")})
                _req.write_text(_json_io.dumps(_items, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        except Exception:                            # noqa: BLE001
            pass                                     # 登记失败不该挡住澄清流程
        return {"ok": False, "stack": name, "pending_ecosystem": True,
                "why": (f"引擎还没有 {name!r} 这个栈（已知：{', '.join(sorted(known))}）。"
                        f"已登记为待协商请求：下次协商时会让远端尝试补全这个生态包。")}

    # 落盘：有适配器的走 write_choice（它会校验）；
    # 没有适配器但 layerplan 支持的（cli/frontend）直接写同一份 stack.json ——
    # 那本来就是"配置不是猜"的载体，谁读都认这个路径。
    from . import stack as _stack
    if name in set(_stack.names()):
        r = _stack.write_choice(ws, name)
        if not r.get("ok"):
            return {"ok": False, "why": str(r.get("why") or "写入失败")}
        return {"ok": True, "stack": name, "path": _stack.STACK_FILE}
    path = ws / ".sixcoder" / "stack.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json_io.dumps({"stack": name}, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return {"ok": True, "stack": name, "path": ".sixcoder/stack.json"}

def run_clarify_phase(ws, requirement: str, complete=None, answers: dict | None = None,
                      interactive: bool = True, force: bool = False) -> dict:
    """跑澄清循环：远端最强模型列全量待定项+多选 → 人类选 → 落进同一份设计文档（越改越细）
    → 直到双人闸门（人类满意 + AI 判足够）。产物转 arch 格式返回，并落盘：
      `.sixcoder/design_doc.md`（人读） + `.sixcoder/design_doc.json`（机读，二次跳过）。

    默认不破坏既有夜班：仅在 CLI `--clarify` 时调用。
    """
    from . import clarify as _clarify  # noqa: WPS433

    ws = Path(ws).resolve()
    (ws / ".sixcoder").mkdir(parents=True, exist_ok=True)
    json_path = ws / ".sixcoder" / "design_doc.json"
    md_path = ws / ".sixcoder" / "design_doc.md"

    if json_path.exists() and not force:
        import json as _json  # noqa: WPS433
        return _json.loads(json_path.read_text(encoding="utf-8"))

    complete = complete or _clarify.default_complete

    def human_answer(doc):
        sel: dict = {}
        for c in doc.open_choices():
            if answers and str(answers.get(c.key, "")) in c.options:
                sel[c.key] = str(answers[c.key])
            elif interactive:
                try:
                    line = input(f"  [{c.key}] {c.question}\n    选项 {c.options}\n    选择：").strip()
                except EOFError:
                    line = ""
                if line in c.options:
                    sel[c.key] = line
        satisfied = bool(answers) or (not doc.open_choices())
        return sel, satisfied

    doc = _clarify.clarify_loop(requirement, complete, human_answer, effort="max", max_rounds=8)
    md_path.write_text(doc.render(), encoding="utf-8")
    arch = clarify_design_doc_to_arch(doc)

    # 2026-11-24：把澄清里选定的技术栈落盘成 .sixcoder/stack.json。
    #
    # 为什么必须有这一步：clarify 的 choices 里第一项是 stack（提示词已强制），
    # 但以前选完只存进 design_doc.json，**没有写进 stack.json** ——
    # 于是后面的生成、判据、闸门全都在按默认 fastapi 猜。
    # 用户明明选了 CLI，生成侧仍然按 Web 三分层走，这是最隐蔽的一类错。
    #
    # 只认引擎支持的栈；"不支持：..." 这种选项不落盘（那是边界提示，不是选择）。
    try:
        _stack_pick = _persist_stack_choice(ws, doc)
    except Exception as _e:                            # noqa: BLE001
        _stack_pick = {"ok": False, "why": f"{type(_e).__name__}: {_e}"}
    if _stack_pick.get("ok"):
        print(f"> 技术栈已锁定：{_stack_pick.get('stack')}（{_stack_pick.get('path')}）")
    elif _stack_pick.get("why"):
        print(f"> 技术栈未锁定：{_stack_pick['why']}")
    import json as _json  # noqa: WPS433
    json_path.write_text(_json.dumps(arch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"> 设计文档已落盘：{md_path}")
    return arch


# ── 契约合成的成本归因（2026-11-15）────────────────────────────────────
# 契约合成是最贵的一步（thinking + 长输出），必须能和写码/修复分开记账，
# 否则"钱花在哪一步"永远是个谜（实测 1749 次调用 purpose 全是 handoff）。
def _attributed_contract(fn):
    import functools

    @functools.wraps(fn)
    def _wrap(ws, sl, brief, *args, **kwargs):
        try:
            from . import attribution as _at
            _ctx = _at.unit(phase="contract")
        except Exception:                                # noqa: BLE001
            return fn(ws, sl, brief, *args, **kwargs)
        with _ctx:
            return fn(ws, sl, brief, *args, **kwargs)

    return _wrap


contract_for_slice = _attributed_contract(contract_for_slice)
