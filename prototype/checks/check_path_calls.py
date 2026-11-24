"""自检：夜班处理路径上的**每个调用点**都能真的绑上参数，且没被同名实现顶替。

为什么要这条（2026-11-15 用户要求"仔细检查调用正确、不会被其他函数劫持"）：

静态"函数存在"证明不了三件事：
  ① 实参名和形参名对不上 → TypeError（或被 `**kwargs` 静默吞掉，症状是参数丢失）
  ② 同名函数被后来的定义/导入覆盖（模块里后定义者胜；`from . import x` 也会覆盖）
  ③ 包装器（`_attributed` 之类）改变了调用约定（只收位置参数、不吃关键字）

这里用 `inspect.signature().bind()` 逐个调用点**真的绑一遍**：
绑不上就是运行时崩溃；`__module__` 不对就是被顶替；`**kwargs` 兜底要显式豁免。

跑法: python checks/check_path_calls.py
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE.parent
sys.path.insert(0, str(PROTO))

from core import author, company, converge, django_wire, errorreport      # noqa: E402
from core import fullgen, iterate, night, openhouse, remote, stack, wholerepo  # noqa: E402

FAILS: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  PASS ", name)
    else:
        print("  FAIL ", name, "|", str(detail)[:280])
        FAILS.append(name)


def _binds(fn, *args, **kwargs) -> tuple[bool, str]:
    """真绑一次：既能发现 TypeError，也能发现"形参里根本没这个关键字"。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        return False, f"取不到签名：{exc}"
    try:
        sig.bind(*args, **kwargs)
        return True, "ok"
    except TypeError as exc:
        return False, str(exc)


def _has_var_kw(fn) -> bool:
    try:
        return any(p.kind is p.VAR_KEYWORD
                   for p in inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False


def main() -> int:
    print("[1] 链条上的函数都解析到**自己的模块**（不是被同名顶替）")
    for mod, name, want in ((night, "run", "core.night"),
                            (company, "night", "core.company"),
                            (company, "_night_inner", "core.company"),
                            (fullgen, "decide_mode", "core.fullgen"),
                            (fullgen, "generate_contract_by_domain", "core.fullgen"),
                            (fullgen, "_lock_contract", "core.fullgen"),
                            (fullgen, "repair", "core.fullgen"),
                            (wholerepo, "session", "core.wholerepo"),
                            (wholerepo, "report_from_state", "core.wholerepo"),
                            (wholerepo, "collect", "core.wholerepo"),
                            (wholerepo, "apply", "core.wholerepo"),
                            (openhouse, "observe", "core.openhouse"),
                            (openhouse, "wire", "core.openhouse"),
                            (django_wire, "wire", "core.django_wire"),
                            (django_wire, "check_project", "core.django_wire"),
                            (author, "author_module", "core.author"),
                            (author, "author_modules", "core.author"),
                            (remote, "complete", "core.remote"),
                            (converge, "judge", "core.converge"),
                            (iterate, "inspect", "core.iterate"),
                            (stack, "activate", "core.stack")):
        fn = getattr(mod, name, None)
        got = getattr(fn, "__module__", "?")
        # 包装器：__wrapped__ 指向原模块也算对（functools.wraps 会保留）
        wrapped = getattr(getattr(fn, "__wrapped__", None), "__module__", None)
        ck(f"{want}.{name} 没被顶替", callable(fn) and want in (got, wrapped),
           f"实际来自 {got}（__wrapped__={wrapped}）")

    print()
    print("[2] 每个调用点用**源码里的实参**真绑一遍")
    cases = [
        ("cli → night.run", night.run,
         ("F:/ws", "目标"), dict(model="m", max_rounds=6, fresh=False, emit=None,
                                remote_profile="", plan="", refresh_contract=False)),
        ("cli → fullgen.run", fullgen.run,
         ("F:/ws",), dict(emit=None, max_rounds=6, detect_only=True, from_scratch=False,
                          max_files=6, per_file=False, refresh_contract=False,
                          mode="auto", ui_base="", only_e2e=None)),
        ("fullgen.run → repair", fullgen.repair,
         ("F:/ws", None, {}), dict(emit=None, max_files=6, whole=True,
                                   round_no=1, total_rounds=6, diagnose_only=False)),
        ("night.run → company.night", company.night,
         ("F:/ws", "需求", "m"), dict(max_rounds=6, emit=None, plan="",
                                      contract_file="x.txt", remote_profile="",
                                      scale_level="S")),
        ("night.run → decide_mode", fullgen.decide_mode, ("F:/ws",), {}),
        ("night.run → generate_contract_by_domain", fullgen.generate_contract_by_domain,
         ("F:/ws", None), dict(emit=None)),
        ("night.run → _lock_contract", fullgen._lock_contract,
         ("F:/ws",), dict(refresh=False, emit=None)),
        ("company → wholerepo.session", wholerepo.session,
         ("F:/ws", None), dict(emit=None, round_no=1, total_rounds=6, extra="")),
        ("company → report_from_state", wholerepo.report_from_state, (),
         dict(cap_cov={}, cap_detail={}, state={}, triage=None, orders=[])),
        ("company → openhouse.observe", openhouse.observe,
         ("F:/ws", None), dict(emit=None, only={"cap.x"})),
        ("company → openhouse.wire", openhouse.wire,
         ("F:/ws", None), dict(emit=None, route_funcs={})),
        ("company → iterate.inspect", iterate.inspect, ("F:/ws", {}), {}),
        ("fullgen.repair → author.author_module", author.author_module,
         ("F:/ws", "a.py", None, {}), dict(evidence="e", attempts=3, emit=None)),
        ("fullgen → author.author_modules", author.author_modules,
         ("F:/ws", ["a.py"], {}), dict(emit=None)),
        ("wholerepo → remote.complete", remote.complete,
         ("prompt",), dict(task_id="t", node_id="whole:repo", thinking="on",
                           max_out=1000, emit=None, workspace="F:/ws")),
        ("company → django_wire.check_project", django_wire.check_project, ("F:/ws",), {}),
        ("company → converge.judge", converge.judge,
         ([],), dict(fatal=0, done=0, total=0)),
        ("night/company → stack.activate", stack.activate, ("F:/ws",), {}),
    ]
    for label, fn, args, kwargs in cases:
        ok, why = _binds(fn, *args, **kwargs)
        ck(label, ok, why)

    print()
    print("[3] 允许 `**kwargs` 兜底的调用点（其余不许有，否则实参写错也不报错）")
    allowed = {"remote.complete"}       # 远端入口按设计收很多可选参数
    for label, fn, _a, _k in cases:
        name = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', '?')}"
        if name in allowed:
            continue
        ck(f"{label} 不吃 **kwargs", not _has_var_kw(fn),
           "有 **kwargs：实参名写错会被静默吞掉")

    print()
    print("[4] ErrorReport.render 的调用口径对得上")
    ok, why = _binds(errorreport.Report.render, object(), limit_per_level=60)
    ck("Report.render(limit_per_level=60) 可绑定", ok, why)

    print()
    print("[5] 包装器保留 __wrapped__（回溯/自检看到原函数）")
    ck("author_module 有 __wrapped__", hasattr(author.author_module, "__wrapped__"))
    ck("author_modules 有 __wrapped__ 或其本身就是原函数",
       hasattr(author.author_modules, "__wrapped__")
       or author.author_modules.__module__ == "core.author")

    print()
    print("[6] 新增/改名后不许有未定义名字（静态，等价 pyflakes undefined name）")
    import subprocess
    targets = ["core/night.py", "core/wholerepo.py", "cli.py", "core/company.py",
               "core/fullgen.py", "core/django_wire.py", "core/author.py",
               "core/openhouse.py", "core/remote.py"]
    try:
        r = subprocess.run([sys.executable, "-m", "pyflakes", *targets],
                           cwd=str(PROTO), capture_output=True, text=True, timeout=120)
        bad = [ln for ln in (r.stdout or "").splitlines() if "undefined name" in ln]
        ck("路径文件里没有 undefined name", not bad, "; ".join(bad[:4]))
    except Exception as exc:                             # noqa: BLE001
        ck("pyflakes 可用", False, f"{type(exc).__name__}: {exc}")

    print()
    print("[7] 同名参数不许互相劫持（--mode 既是全局权限模式、又曾是 full 的生成口径）")
    import cli as _cli
    _p = _cli.build()
    _a1 = _p.parse_args(["full", "--repo", "X", "--mode", "repair"])
    ck("`full --mode repair` 落在 gen_mode，不污染权限模式",
       getattr(_a1, "gen_mode", "") == "repair" and getattr(_a1, "mode", None) is None,
       f"gen_mode={getattr(_a1, 'gen_mode', '?')} mode={getattr(_a1, 'mode', '?')}")
    _a2 = _p.parse_args(["--mode", "developer", "full", "--repo", "X"])
    ck("全局 --mode developer 仍然生效",
       getattr(_a2, "mode", "") == "developer", getattr(_a2, "mode", "?"))
    _a3 = _p.parse_args(["night", "--repo", "X", "--goal", "g"])
    ck("night 不带 --mode（不会误设权限模式）",
       getattr(_a3, "mode", None) is None, getattr(_a3, "mode", "?"))
    ck("night 默认分发到 cmd_night", getattr(_a3, "fn", None).__name__ == "cmd_night",
       getattr(getattr(_a3, "fn", None), "__name__", "?"))

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过（模块归属 / 16 个调用点真绑定 / 无静默吞参 / 无未定义名字）")
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
