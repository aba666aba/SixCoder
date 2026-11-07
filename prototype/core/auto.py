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
import re
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
MAX_CONTRACT_FAIL = 4          # 契约合成连续失败到几次就如实停（系统侧问题，不降级能力）
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

# 引擎按蓝图填的模块清单；还没写出来的模块自动跳过（不影响已通过的能力）
_ROUTER_MODULES = {modules!r}

for _name in _ROUTER_MODULES:
    try:
        if importlib.util.find_spec(_name) is None:
            continue
    except (ImportError, ValueError):
        continue
    _mod = importlib.import_module(_name)
    _router = getattr(_mod, "router", None)
    if _router is not None:
        app.include_router(_router)
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


def _pick_func(path: str, funcs: dict[str, list[str]], method: str = "GET") -> str:
    """按 HTTP 语义选最像的**端点**函数（POST /tickets → create_ticket）。

    两个坑（都是实测踩的）：
      · 加权求和会让 `get_board_tickets` 凭一个 "get" 抢走 `GET /tickets`；
      · 只按动作词匹配会让 `create_connection(db_file)` 抢走 `POST /tickets`
        —— 它名字里有 "create"，但它是**数据库连接辅助函数**，接上去必然
        `TypeError: expected str, bytes or os.PathLike object`（2026-11-07）。
    所以：先按路径/方法的优先关键词组选出候选，再**剔除辅助函数**，最后优先资源词匹配。
    """
    p = str(path or "").lower()
    m = str(method or "GET").upper()
    groups: list[tuple[str, ...]] = []
    if "search" in p:
        groups.append(("search", "find", "query"))
    if "board" in p:
        groups.append(("board", "kanban"))
    if "dashboard" in p:
        groups.append(("dashboard", "stat", "summary"))
    if "login" in p:
        groups.append(("login", "signin"))
    if "register" in p:
        groups.append(("register", "signup"))
    if "comment" in p:
        groups.append(("comment",))
    if m == "POST":
        groups.append(("create", "add", "insert", "new"))
    elif m in ("PUT", "PATCH"):
        groups.append(("update", "edit", "change", "modify", "set"))
    elif m == "DELETE":
        groups.append(("delete", "remove", "drop"))
    else:
        groups.append(("list", "all", "index"))
        groups.append(("get", "detail", "read", "fetch"))

    res_tokens = [t for t in re.findall(r"[a-z]+", p)
                  if len(t) > 3 and t not in ("auth", "tickets", "users", "boards", "http")]
    res_singular = [t[:-1] if t.endswith("s") else t for t in res_tokens]

    def usable(name: str) -> bool:
        low = name.lower()
        return not any(h in low for h in HELPER_WORDS)

    for keys in groups:
        hit = [n for n in funcs if any(k in n.lower() for k in keys) and usable(n)]
        if not hit:
            continue
        # 资源词优先（ticket → create_ticket 而不是 create_user）
        for n in hit:
            if any(t in n.lower() for t in res_singular):
                return n
        return hit[0]
    return ""


def apply_web_wiring(ws, bp) -> list[str]:
    """按蓝图把**所有** http 能力的路由补齐（幂等、确定性、顺序正确）。

    为什么要有注册表（实测 2026-11-07）：
      · 作者整文件重写 / 回归回滚会抹掉引擎段 → 路由消失 → 能力 404 被判"做不出来"；
      · 只追加不重排 → `/tickets/{ticket_id}` 排在 `/tickets/search` 前面 → 后者永远 405。
    所以引擎维护一份 `.sixcoder/web_routes.json`，每次**整块重渲染**（静态路径在前），
    模块里那段永远等于注册表的投影，不靠"记得追加"。
    """
    reg = _load_routes(ws)
    per_mod: dict[str, list[dict]] = {}
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        mod = (c.modules or [""])[0]
        p = Path(ws) / mod
        if not mod or not p.is_file():
            continue
        # 先占位：这个模块必须参与本次渲染，否则"函数全被删了"时旧引擎段永远清不掉。
        per_mod.setdefault(mod, [])
        last = (((c.probe or {}).get("steps") or [{}])[-1] or {})
        path = _fastapi_path(last.get("path") or "")
        if not path:
            continue
        # 负向用例（期望 4xx）只负责"判"，**不能定义路由**：否则它会用自己的请求体
        # 和错误状态码覆盖掉正向路由（实测 2026-11-07：POST /tickets 变成 200、
        # 请求体变成 __invalid__，整片全红）。
        _stt = last.get("expect_status")
        if (isinstance(_stt, int) and _stt >= 400) or (
                isinstance(_stt, list) and _stt and all(
                    isinstance(x, int) and x >= 400 for x in _stt)):
            continue
        try:
            src = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        funcs = _module_signatures(src)
        method = str(last.get("method") or "GET").upper()
        fn = _pick_func(path, funcs, method)
        if not fn:
            continue
        route = {"method": method, "path": path, "func": fn,
                 "body_keys": list((last.get("json") or {}).keys()),
                 "body_sample": last.get("json")}
        stt = last.get("expect_status")
        if isinstance(stt, int):
            route["status"] = stt
        per_mod.setdefault(mod, []).append(route)

    done: list[str] = []
    for mod, routes in per_mod.items():
        key = lambda r: f"{str(r.get('method')).upper()} {r.get('path')}"      # noqa: E731
        merged = {key(r): r for r in (reg.get(mod) or [])}
        for r in routes:
            k = key(r)
            old_r = merged.get(k)
            # 有状态码的版本优先：别让"没写 status"的同路径条目把 201 冲成 200
            if old_r and old_r.get("status") is not None and r.get("status") is None:
                continue
            merged[k] = r
        try:
            src = (Path(ws) / mod).read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        funcs_now = _module_signatures(src)
        # 函数已经不在了 → 这条路由必须从注册表里摘掉，不能留着变成 NameError
        reg[mod] = [r for r in merged.values()
                    if str(r.get("func")) and str(r.get("func")) in funcs_now]
        # **只有引擎真能接上的端点**才摘作者的路由：否则摘完引擎接不上，
        # 就变成 404（比重复注册更糟）。
        _owned = {(str(r.get("method") or "").upper(), str(r.get("path") or ""))
                  for r in reg[mod]}
        src, _removed = _strip_author_routes(src, _owned)
        src, _added = _ensure_imports(src)
        if _removed or _added:
            try:
                (Path(ws) / mod).write_text(src, encoding="utf-8")
            except OSError:
                pass
            if _removed:
                done.append(f"{mod}: 摘掉作者自挂的 {', '.join(_removed[:3])}")
            if _added:
                done.append(f"{mod}: 补 import {', '.join(x.split()[-1] for x in _added[:3])}")
        res = ensure_web_router(ws, mod, reg[mod], funcs_now, full=True)
        if res.get("wrote"):
            done.append(f"{mod}:{len(reg[mod])} 条路由（静态优先）")
    _save_routes(ws, reg)
    return done


def write_web_entry(ws, bp) -> dict:
    """生成/刷新 ASGI 入口 `app/main.py`（确定性、0 token、可溯源）。

    只写**引擎自己的文件**：其它 app/*.py 一律不碰。
    """
    mods = []
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        for m in (c.modules or []):
            name = str(m or "").strip()
            if not name.endswith(".py") or name == "app/main.py":
                continue
            mod = name[:-3].replace("/", ".")
            if mod.startswith("app.") and mod not in mods:
                mods.append(mod)
    root = Path(ws) / "app"
    try:
        root.mkdir(parents=True, exist_ok=True)
        init = root / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
        p = root / "main.py"
        body = WEB_ENTRY.replace("{modules!r}", repr(sorted(mods)))
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


def _ensure_imports(src: str) -> tuple[str, list[str]]:
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
    add = [stmt for name, stmt in _AUTO_IMPORTS.items()
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
            if (verb.upper(), path) in owned:
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


def ensure_web_router(ws, module: str, routes: list[dict], funcs: dict[str, list[str]],
                      full: bool = True) -> dict:
    """把模块里的路由段**整块重渲染**成注册表的投影（确定性，不追加）。

    路由形状（方法/路径/状态码/请求体字段）完全由探针决定，是接线不是设计判断，
    所以交给引擎；作者只写服务层函数。

    三条硬约束（都踩过）：
      · 必须**剥掉所有**旧的引擎段再写：只剥第一段会让同一路由出现两次（2026-11-07）；
      · 静态路径排在 `{param}` 之前：否则 `/tickets/{id}` 会吃掉 `/tickets/search` → 405；
      · 参数接不上就不要接：硬接会拿 None 去调，报一个和作者无关的 TypeError。
    """
    p = Path(ws) / module
    if not p.is_file():
        return {"ok": False, "wrote": False, "why": "模块还没写出来"}
    # 注意：routes 为空也要继续 —— 函数被作者删掉时，旧引擎段里还留着指向它们的
    # `@router.*`，不清掉就是"请求时 NameError"（实测 2026-11-07，6 条能力一起红）。
    try:
        src = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return {"ok": False, "wrote": False, "why": f"读不了：{exc}"}

    while WEB_ROUTE_BEGIN in src:                        # 剥**干净**（可能有多段残留）
        _h, _rest = src.split(WEB_ROUTE_BEGIN, 1)
        _rest = _rest.split(WEB_ROUTE_END, 1)[1] if WEB_ROUTE_END in _rest else ""
        src = _h + _rest
    src = _strip_engine_leftovers(src)
    head = src.rstrip()

    routes = sorted(routes, key=lambda r: (0 if "{" not in str(r.get("path") or "") else 1,
                                           -len(str(r.get("path") or "")),
                                           str(r.get("path") or "")))
    body: list[str] = []
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
        nums = re.findall(r"/(\d+)(?=/|$)", path)
        args: list[str] = []
        bad: list[str] = []
        for pn in (funcs.get(fn) or []):
            if pn in pvars:
                args.append(f"{pn}=_to_int({pn})")
            elif pn in ("authorization", "token", "auth"):
                args.append(f"{pn}=authorization")
            elif pn in bkeys:
                args.append(f'{pn}=_b.get("{pn}")')
            elif pn.endswith("_id") or pn in ("id", "pk"):
                args.append(f"{pn}={int(nums[0]) if nums else 1}")
            elif not any(k in pn.lower() for k in
                         ("file", "path", "url", "dir", "db", "conn")):
                args.append(f'{pn}=""')
            else:
                bad.append(pn)
        if bad:
            skipped.append(f"{method} {path}（参数无从提供：{','.join(bad)}）")
            continue
        seen.add((method, path))
        n_routes += 1
        name = re.sub(r"[^a-z0-9]+", "_", f"{method}_{path}".lower()).strip("_")
        model = ""
        if bkeys:
            fname = f"_Body_{name}"
            model = f"class {fname}(BaseModel):\n" + "\n".join(
                f"    {k}: {_py_type((r.get('body_sample') or {}).get(k))}" for k in sorted(bkeys))
            body_arg = f"body: {fname}"
        else:
            body_arg = "body: dict = Body(default=None)"
        extra = [f"{v}: str" for v in pvars] + [body_arg]
        st = r.get("status")
        kw = f", status_code={int(st)}" if isinstance(st, int) else ""
        if model:
            body += ["", model]
        body += ["", f'@router.{method}("{path}", response_model=None{kw})',
                 f"def _engine_{name}({', '.join(extra)},",
                 "                   authorization: str = Header(default=\"\")):",
                 ("    _b = body.model_dump()" if bkeys else
                  "    _b = body if isinstance(body, dict) else {}"),
                 f"    return {fn}({', '.join(args)})"]

    block = [WEB_ROUTE_BEGIN]
    if any(r.get("body_keys") for r in routes) and not re.search(r"\bBaseModel\b", head):
        block.append("from pydantic import BaseModel")
    for name in ("APIRouter", "Body", "Header"):
        if not re.search(rf"\b{name}\b", head):
            block.append(f"from fastapi import {name}")
    if not re.search(r"^\s*router\s*=\s*APIRouter\(", head, re.M):
        block.append("router = APIRouter()")
    if "_to_int" not in head:
        block += ["", "def _to_int(v):",
                  "    try:", "        return int(v)",
                  "    except (TypeError, ValueError):", "        return v"]
    block += body + [WEB_ROUTE_END]
    text = head + "\n\n" + "\n".join(block) + "\n"
    if text == p.read_text(encoding="utf-8-sig", errors="replace"):
        return {"ok": True, "wrote": False, "why": "路由段已是最新", "routes": n_routes,
                "skipped": skipped}
    try:
        p.write_text(text, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "wrote": False, "why": f"写不了：{exc}"}
    return {"ok": True, "wrote": True, "routes": n_routes, "skipped": skipped,
            "why": f"{n_routes} 条路由（静态优先）；跳过 {len(skipped)} 条"}


def write_web_entry(ws, bp) -> dict:
    """生成/刷新 ASGI 入口 `app/main.py`（确定性、0 token、可溯源）。

    只写**引擎自己的文件**：其它 app/*.py 一律不碰。
    """
    mods = []
    for c in bp.caps:
        if str((c.probe or {}).get("kind") or "") != "http":
            continue
        for m in (c.modules or []):
            name = str(m or "").strip()
            if not name.endswith(".py") or name == "app/main.py":
                continue
            mod = name[:-3].replace("/", ".")
            if mod.startswith("app.") and mod not in mods:
                mods.append(mod)
    root = Path(ws) / "app"
    try:
        root.mkdir(parents=True, exist_ok=True)
        init = root / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
        p = root / "main.py"
        body = WEB_ENTRY.replace("{modules!r}", repr(sorted(mods)))
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


def _append_web_routes(text: str, probes: list, keep_modules: list[str] | None = None,
                       fail_note: str = "") -> str:
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
    specs = _slicer.web_route_spec([p for p in probes if isinstance(p, dict)])
    if not specs:
        return text
    mods = [str(m) for m in (keep_modules or []) if str(m).endswith(".py")] or ["（本片模块）"]
    block = ["", "# 路由（探针会**真的**发这些请求：路径 / 字段名 / 状态码逐字一致，不许改）",
             *[f"- {s}" for s in specs],
             f"- 写法：在 {mods[0]} 里 `router = APIRouter()`，用 Pydantic 模型接收 JSON 请求体，"
             "再调用上面的契约函数；**不要写 app/main.py**（引擎生成，router 会自动挂上）"]
    note = (fail_note or "").strip().replace("\n", " ")[:240]
    if note:
        block.append(f"- 【上一轮实测失败，这次必须改掉】{note}")
    return text.rstrip() + "\n" + "\n".join(block) + "\n"


def contract_for_slice(ws, sl: dict, brief: str, *, emit=None, refresh: bool = False,
                       entry: str = "", entry_example: str = "", fail_note: str = "",
                       prep: list[str] | None = None,
                       post: list[str] | None = None,
                       groups: list[list[str]] | None = None,
                       core_probes=None,
                       keep_modules: list[str] | None = None,
                       drop_modules: list[str] | None = None,
                       web_probes: list | None = None) -> dict:
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
                                        fail_note=fail_note)
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
                               fail_note=fail_note)
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
    # 全自主是从 0 生成：工作区允许还不存在（CLI --goal 模式与 API 直接调都会走这里）
    ws.mkdir(parents=True, exist_ok=True)
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
        _inj = _ship0.inject_caps(bp, goal or bp.goal)
        if _inj:
            bp.notes.append("机制注入：" + "、".join(_inj))
            blueprint.save(bp, ws)
            if emit:
                emit("caps_injected", {"caps": _inj})
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"机制注入跳过：{type(exc).__name__}: {exc}"[:160]})

    # 老蓝图（引擎分配落点模块之前生成的）可能 modules 是空的：
    # Web 能力必须补齐落点，否则作者无处落文件、契约合成会抄成 calc.py。
    if blueprint.fill_http_modules(bp):
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

    try:
        from . import deps as _deps0
        _dep0 = _deps0.ensure(ws)
        if _dep0.get("installed") and emit:
            emit("deps_installed", {"pkgs": _dep0["installed"], "req": _dep0.get("req_file")})
    except Exception:                                    # noqa: BLE001
        pass
    # 规模机制：目标写着"XL / ≥N 行"时，做完一轮**不能报 100% 收工** ——
    # 要按行数缺口继续要新能力。实测 2026-11-07：目标 10000 行，蓝图 9 条能力，
    # 做完就报 100%，交付物 400 行。规模不是模型能力问题，是规划机制缺了一环。
    try:
        _target = _target_lines(bp.goal)
        if _target:
            from . import scale as _scale0
            _prod0 = int((_scale0.count_lines(ws) or {}).get("prod_lines") or 0)
            _all_done = all(c.status == "done" for c in bp.caps)
            if _all_done and _prod0 < _target * 0.8:
                _need = max(2, min(8, int((_target - _prod0) / 120) or 2))
                _got = blueprint.extend(bp, bp.goal, need=_need, llm=llm, ws=ws)
                if _got.get("added"):
                    bp.notes.append(f"规模扩产：+{len(_got['added'])} 条能力（目标 {_target} 行，"
                                    f"现有 {_prod0} 行）")
                    if emit:
                        emit("blueprint_extended", {"added": _got["added"], "target": _target,
                                                    "lines": _prod0})
                    # 扩产后能力涨了，重新分组（保证新能力也有子系统归属）
                    _maybe_split_subsystems(bp, ws, emit)
    except Exception as exc:                             # noqa: BLE001
        if emit:
            emit("notice", {"message": f"规模扩产跳过：{type(exc).__name__}: {exc}"[:150]})

    # 起始观测前也要接线：上一轮作者可能自己给服务函数挂了路由（重复注册 / 错误的
    # response_model），也会把引擎段抹掉；不先接线，起始观测就会把"能过的能力"判红。
    try:
        _w0 = apply_web_wiring(ws, bp)
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
        # Web 能力比纯函数重（路由、Pydantic 模型、DB 访问都要写），
        # 一片 12 个函数作者根本写不完 → 整片被回滚（实测 2026-11-07 s04）。
        if any(str((c.probe or {}).get("kind") or "") == "http" for c in bp.caps):
            funcs_cap = min(int(funcs_cap), 6)
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
                                                           for x in (bp.by_id(cid).modules or [])}],
                                web_probes=probes)
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
        try:
            _wired = apply_web_wiring(ws, bp)
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
    import json as _json  # noqa: WPS433
    json_path.write_text(_json.dumps(arch, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"> 设计文档已落盘：{md_path}")
    return arch

