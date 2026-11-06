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

import json
import shutil
import time
from pathlib import Path

from . import architect
from . import blueprint
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
CNY_PER_FUNC = blueprint.CNY_PER_FUNC   # 每函数花费口径只留一处定义（blueprint 里那份）
DEFAULT_SLICE_HOURS = 0.5      # 单切片时间预算（没指定时）
MAX_REPLANS = 6                # 一次运行最多重规划 6 次（再多说明计划本身站不住）
MAX_ITERATIONS = 400           # 循环硬闸：无论如何不许转不出来
SELFCHECK_STEPS = 3            # 片内自验最多几步（每步 = 一次远端 + 至多 2 条命令）


# ---------------------------------------------------------------- 小工具

def _usage_cny() -> float:
    """当前远端累计花费（用于按切片记账）。取不到就返回 0，绝不编数。"""
    try:
        from . import remote
        return float((remote.usage_summary("") or {}).get("cost_cny") or 0.0)
    except Exception:                                    # noqa: BLE001
        return 0.0


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
            if note and "上一轮实测失败" not in lines[core_at]:
                lines[core_at] = lines[core_at].rstrip("；; ") + f"；【上一轮实测失败】{note}"
    else:
        idx = max([len(heads) and heads[-1][1] or ""] and [0] or [0])
        num = (len(heads) + 1)
        lines += ["", f"{num}) {CORE_MODULE}：命令行核心（入口脚本由引擎生成，会调用这里的 cli_run）",
                  core_line, "", "示例（必须满足）：", "（无）", ""]
    exs = core_groups(probes)
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


def contract_for_slice(ws, sl: dict, brief: str, *, emit=None, refresh: bool = False,
                       entry: str = "", entry_example: str = "", fail_note: str = "",
                       prep: list[str] | None = None,
                       post: list[str] | None = None,
                       groups: list[list[str]] | None = None,
                       core_probes=None,
                       keep_modules: list[str] | None = None,
                       drop_modules: list[str] | None = None) -> dict:
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

def _default_runner(ws, brief: str, contract_path: str, budget_cny: float, *,
                    model: str = "", max_rounds: int = 40, hours: float = 0.0,
                    emit=None, slice_id: str = "") -> dict:
    """跑一轮夜班（内循环原样复用）。返回统一结构，方便编排层记账。"""
    from . import company
    if not model:
        try:
            from . import ollama
            model = ollama.pick_default() or ""
        except Exception:                                # noqa: BLE001
            model = ""
    before = _usage_cny()
    t0 = time.time()
    res = company.night(Path(ws), brief, model, hours=hours or DEFAULT_SLICE_HOURS,
                        max_rounds=max_rounds, emit=emit, contract_file=contract_path,
                        max_cny=budget_cny, scale_level="S")
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
            "cost_cny": round(max(0.0, _usage_cny() - before), 4),
            "delivery": str(getattr(res, "delivery_path", "") or "")}


# ---------------------------------------------------------------- 主循环

def night_auto(repo, goal: str = "", *, budget_cny: float = 0.0, hours: float = 0.0,
               model: str = "", max_slices: int = 0, max_rounds: int = 40,
               slice_hours: float = 0.0, do_audit: bool = True, fresh: bool = False,
               llm=None, runner=None, recon=None, emit=None, approve=None,
               spec_samples: str = "", arch: dict | None = None,
               selfcheck: bool = True) -> dict:
    """全自主夜班。只给一句话目标（和预算），其余由系统自己决定。

    `arch` = {"questions": [...], "answers": {...}}：前期架构问答的结果（人给的安排）。
    有它就先并进目标、并把其中的验收样例当权威判据——人只在最贵的位置介入这一次。

    返回 {ok, reason, blueprint, coverage, ratios, spent_cny, slices, replans,
          history, report, descoped, blocked, gaps, next}
    """
    ws = Path(repo).resolve()
    t0 = time.time()
    llm = llm or blueprint._default_llm
    runner = runner or _default_runner
    recon = recon or covmod.run
    goal = (goal or "").strip()
    calls = 0
    start_cut: list[str] = []

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
    if bp is None or not bp.caps:
        if not goal:
            return _fail(ws, "既没有蓝图也没有目标：至少要给一句话")
        got = blueprint.derive(goal, budget_cny=budget_cny, llm=llm,
                               do_audit=do_audit, emit=emit, ws=ws)
        calls += int(got.get("calls") or 0)
        if not got.get("ok"):
            return _fail(ws, "蓝图不合格：" + "；".join((got.get("problems") or [])[:3]),
                         blueprint=got.get("blueprint"))
        bp = got["blueprint"]
        # 超预算就**主动砍范围**（只写在备注里没用：跑到一半钱没了，剩下全变缺口）
        cut = blueprint.fit_budget(bp)
        if cut:
            start_cut.extend(cut)
        blueprint.save(bp, ws)
        if emit:
            emit("blueprint_ready", {"version": bp.version, "caps": len(bp.caps),
                                     "summary": blueprint.summary(bp)[:600]})
    elif goal and not bp.goal:
        bp.goal = goal
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

    cov = recon(bp, ws)
    covmod.record(ws, cov, "起始观测")
    bp = covmod.mark(bp, cov)
    blueprint.save(bp, ws)

    spent = 0.0
    hist: list[dict] = []
    fails: dict[str, int] = {}
    regressions: list[dict] = []
    contract_fail_streak = 0
    wasted = 0                 # 无效尝试：跑完一片后"未通过能力集合"没有净减
    force_refresh = False      # 上一次无效 → 这次强制重合成契约（不吃旧缓存）
    best_done = 0              # 历史最好完成数（判断错误集合是否单调下降）
    slices_run = 0
    replans = 0
    selfchecks: list[dict] = []    # 片内自验的账（每一步修好了什么、卡在哪）
    reason = "循环到顶"
    it = 0
    while True:
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
        if budget_cny and spent >= budget_cny:
            reason = f"预算用尽（¥{spent:.2f} ≥ ¥{budget_cny:.2f}）"
            break
        if hours and (time.time() - t0) >= hours * 3600:
            reason = f"时间用尽（{hours}h）"
            break
        if max_slices and slices_run >= max_slices:
            reason = f"切片轮数到顶（{max_slices}）"
            break

        # 切片大小随"剩余的活"和"剩余的钱"一起收放：钱不够就切更小的片，
        # 而不是硬塞一个大片把钱烧完（实测第 3 次真机就是这样 0/11 收场的）。
        dose_now, _dose_why = slicer.dose(ws)
        funcs_cap = int(dose_now)
        if budget_cny:
            left_now = max(0.0, budget_cny - spent)
            per = CNY_PER_FUNC
            try:
                from . import calibration as _cal
                per = float((_cal.current() or {}).get("cost_per_func_cny") or 0) or CNY_PER_FUNC
            except Exception:                            # noqa: BLE001
                per = CNY_PER_FUNC
            affordable = max(1, int(left_now / max(1e-6, per * SLICE_RETRY_MARGIN)))
            funcs_cap = max(1, min(funcs_cap, affordable))
        plan = slicer.plan(bp, max_funcs=funcs_cap)
        if not plan["slices"]:
            reason = "没有可做的切片：剩下的能力全部受阻（依赖被降级或不存在）"
            break
        sl = plan["slices"][0]
        brief = slicer.brief(sl, bp, ws=ws, done=bp.done())
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
        cg = contract_for_slice(ws, sl, brief, emit=emit, entry=entry, refresh=force_refresh,
                                entry_example=slicer.entry_example(probes),
                                fail_note=fail_note,
                                groups=_ex_groups,
                                # 只有**本片确实有 CLI 能力**时才走 cli_run 这条路：
                                # 否则 entry 为空，而 all_cli 非空 → 裁剪白名单里没有契约里的模块
                                # → 整份契约被裁成空的（"解析出 0 个函数"，实测 mid2 卡了 3 次）。
                                core_probes=(all_cli or None) if cli_probes else None,
                                keep_modules=[m for cid in sl["caps"]
                                              if bp.by_id(cid)
                                              for m in (bp.by_id(cid).modules or [])],
                                # 只丢已通过且本片不需要的模块：裁剪的 modules 提示不可靠，
                                # 用白名单会把该写的模块裁掉（实测 exp1b：logparse.py 被裁 → 空转）
                                drop_modules=[m for c in bp.caps if c.status == "done"
                                              for m in (c.modules or [])
                                              if m not in {x for cid in sl["caps"] if bp.by_id(cid)
                                                           for x in (bp.by_id(cid).modules or [])}])
        if cg.get("ok"):
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
            funcs_cap = max(1, int(funcs_cap * 0.5)) if funcs_cap > 1 else 1
            for cid in sl["caps"]:
                fails[cid] = fails.get(cid, 0) + 1
            why = "契约合成失败：" + "；".join(cg.get("problems") or [])[:100]
            hist.append({"slice": sl["id"], "caps": sl["caps"], "cost_cny": 0.0,
                         "gained": [], "why": why[:160]})
            if emit:
                emit("slice_contract_failed", {"slice": sl["id"], "problems": (cg.get("problems") or [])[:3],
                                               "next_funcs_cap": funcs_cap,
                                               "streak": contract_fail_streak})
            if max(fails.values() or [0]) >= replan.FAIL_DESCOPE:
                if replans >= MAX_REPLANS:
                    reason = "重规划次数到顶（契约反复合成失败）"
                    break
                bp, cov, calls, replans = _do_replan(bp, cov, ws, llm, recon, fails, hist,
                                                     replans, calls, emit,
                                                     "契约连续合成失败")
            continue
        contract_fail_streak = 0

        left = (budget_cny - spent) if budget_cny else 0.0
        cap = slice_budget(budget_cny, left, len(plan["slices"]), sl.get("funcs") or 0)
        green_before = set(bp.done())
        if emit:
            emit("slice_start", {"slice": sl["id"], "caps": sl["caps"], "funcs": sl["funcs"],
                                 "budget_cny": cap, "funcs_cap": funcs_cap, "contract": cg["path"],
                                 "why": frontier.why(bp, bp.by_id(sl["caps"][0]))
                                 if bp.by_id(sl["caps"][0]) else ""})
        res = runner(ws, brief, cg["path"], cap, model=model, max_rounds=max_rounds,
                     hours=slice_hours, emit=emit, slice_id=sl["id"])
        spent += float(res.get("cost_cny") or 0.0)
        slices_run += 1
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
        if selfcheck and (not budget_cny or spent < budget_cny):
            _red = []
            for cid in sl["caps"]:
                c = bp.by_id(cid)
                if c is not None and c.status != "done":
                    _red.append(c)
            if _red:
                _before = _usage_cny()
                fx = selfcheck_loop.repair(ws, _red, llm=llm, emit=emit,
                                           max_steps=SELFCHECK_STEPS,
                                           budget_cny=budget_cny, cost_used=spent)
                _sc_cost = round(max(0.0, _usage_cny() - _before), 4)
                spent += _sc_cost
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
        else:
            wasted += 1
            force_refresh = True                        # 下次不吃旧契约缓存
            funcs_cap = max(1, int(funcs_cap * 0.8))    # 并缩小切片
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

        stuck = [cid for cid, n in fails.items() if n >= replan.FAIL_DESCOPE]
        if covmod.stalled(ws, bp) or stuck:
            if replans >= MAX_REPLANS:
                reason = "重规划次数到顶"
                break
            bp, cov, calls, replans = _do_replan(
                bp, cov, ws, llm, recon, fails, hist, replans, calls, emit,
                ("覆盖停滞" if covmod.stalled(ws, bp) else f"能力反复失败：{', '.join(stuck[:3])}"))

    ratios = covmod.ratios(cov)
    descoped = [c.id for c in bp.caps if c.status == "descoped"]
    blocked = [{"cap": c.id, "why": w} for c, w in frontier.blocked_caps(bp)]
    gaps = [c.id for c in bp.caps if c.status not in ("done", "descoped")]
    nxt = [(round(s, 3), c.id) for s, c in frontier.rank(bp)[:8]]
    # ok 的口径：**只有每条能力都真通过观测才算完成**。降级不是完成，
    # 受阻也不是完成 —— 报告里"全部通过"而实际有缺口，是最严重的失败形态。
    res = {"ok": (not gaps and not blocked and not descoped), "reason": reason,
           "blueprint": bp, "coverage": cov,
           "ratios": ratios, "spent_cny": round(spent, 4), "elapsed_s": round(time.time() - t0, 1),
           "slices": slices_run, "replans": replans, "calls": calls, "history": hist,
           "descoped": descoped, "blocked": blocked, "gaps": gaps,
           "budget_cut": start_cut, "regressions": regressions,
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


def _do_replan(bp, cov, ws, llm, recon, fails, hist, replans, calls, emit, reason):
    """重规划一次并立刻重新观测（降级/补能力之后，覆盖必须重新算）。"""
    rep = replan.replan(bp, cov=cov, ws=ws, llm=llm, reason=reason, fails=fails, emit=emit)
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
    blueprint.fit_budget(bp)          # 复评可能补了新能力，预算要重新收敛
    cov = recon(bp, ws)
    covmod.record(ws, cov, f"重规划后观测（{reason[:20]}）")
    bp = covmod.mark(bp, cov)
    blueprint.save(bp, ws)
    hist.append({"slice": f"replan-v{bp.version}", "caps": [], "cost_cny": 0.0, "gained": [],
                 "why": f"重规划：{reason}；diff={rep.get('diff')}"[:160]})
    return bp, cov, calls, replans + 1


def _fail(ws, reason: str, blueprint=None) -> dict:
    return {"ok": False, "reason": reason, "blueprint": blueprint, "coverage": {},
            "ratios": {"done": 0, "total": 0, "pct": 0.0}, "spent_cny": 0.0, "elapsed_s": 0.0,
            "slices": 0, "replans": 0, "calls": 0, "history": [], "descoped": [],
            "blocked": [], "gaps": [], "next": [], "report": "", "budget_cut": [],
            "regressions": [], "wasted": 0, "best_done": 0, "meaningful_rate": 1.0}


# ---------------------------------------------------------------- 报告

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
             + (f" / 预算 ¥{bp.total_budget_cny:.2f}" if bp.total_budget_cny else "（未设预算）"),
             f"- 耗时：{res.get('elapsed_s', 0) / 60:.1f} 分钟｜切片 {res.get('slices', 0)} 轮"
             f"｜重规划 {res.get('replans', 0)} 次｜远端调用 {res.get('calls', 0)} 次",
             f"- 剩余：{_left_funcs} 个函数 ≈ 还要 ¥{_left_cost:.2f}"
             + (f"，预算还够余 ¥{max(0.0, bp.total_budget_cny - _spent):.2f}"
                if bp.total_budget_cny else ""),
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
        lines += ["", "## 缺口（还没通过的能力）", ""]
        for cid in gaps:
            c = bp.by_id(cid)
            row = caps.get(cid) or {}
            lines.append(f"- `{cid}`：{row.get('detail') or (c.desc if c else '')}")

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
              f'sixcoder night --repo "{ws}" --goal "{bp.goal[:40]}" '
              f"--budget-cny {max(0.1, (bp.total_budget_cny or 0) - res.get('spent_cny', 0)):.2f}",
              "```",
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
