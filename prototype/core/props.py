"""引擎掌握的"真判定"：从需求文本抽示例、从语义抽性质（round-trip / 幂等），用它们验收产物。

为什么这是提真质量的关键：
- 模型自写的 pytest 是**弱测试**（实测 A1 全绿但产物 TypeError、A2 桩却判 done）；
- 但需求里常自带**可判定事实**：`int_to_roman(3) == 'III'`、`两者互为逆运算`；
- 这些事实由引擎抽取、引擎执行，不经过模型 → 通过它们才算真的对。

三类来源（全部机械抽取，不需要模型）：
1. example：`f(args) == 字面量` / `f(args) 应返回 字面量` / `f(args) 应抛 ValueError`
2. roundtrip：需求出现"互为逆运算/互逆"且点名两个函数 → 在定义域上采样验证 f(g(x))==x 与 g(f(x))==x
3. contract 边界：参数区间（如 `1..3999`、`n > 0`）→ 越界调用必须抛异常
"""
from __future__ import annotations

import ast
import itertools
import json
import keyword
import os
import random
import re
import subprocess
import sys
from pathlib import Path

# 判据临时目录的自增序号：保证每次判据都有干净目录，又不靠"删掉上一次"来实现
_SCRATCH_SEQ = itertools.count(1)

CALL_EQ = re.compile(
    r"([A-Za-z_]\w{2,})\s*\(([^()]*)\)\s*(?:==|等于|应返回|应等于|返回)\s*"
    r"(`[^`]+`|\"[^\"]*\"|'[^']*'|[-\w.\[\]{}]+)")
RAISES = re.compile(
    r"([A-Za-z_]\w{2,})\s*\(([^()]*)\)\s*[^。\n]{0,12}?(?:应|必须|需要)?\s*抛\s*([A-Za-z_]\w*)")
RANGE = re.compile(r"(\d+)\s*\.\.\s*(\d+)")
INVERSE = ("互为逆运算", "互逆", "互为逆", "inverse")

RUNNER = r'''
import importlib, json, os, sys, shutil, subprocess
# 探针脚本放在 .sixcoder/tmp/ 下、在**独立空目录**里运行（不再污染客户仓库），
# 所以把被测仓库根从参数传进来加回 sys.path。
sys.path.insert(0, sys.argv[3] if len(sys.argv) > 3 else os.getcwd())
mod_name, checks = sys.argv[1], json.loads(sys.argv[2])
rel_path = sys.argv[4] if len(sys.argv) > 4 else ""
ws_root = sys.argv[3] if len(sys.argv) > 3 else os.getcwd()
results = []
def _cmp(got, want, desc):
    """Response（FastAPI 的 JSONResponse 等）先拆成它承载的 JSON 再比。

    为什么（2026-11-10 23:00 实测）：`app/api/*.py` 端点返回 `JSONResponse(...)` 是
    FastAPI 的**合法写法**（引擎自己渲染的 api 层也这么写），但契约样例是
    `get_student_me('Bearer stu1') == {...}`，旧判据把 JSONResponse 判成"应为 dict"
    → **怎么改都过不了**；api 层 6 个函数吃掉 94 次远端调用后 heal_stop，
    还被记成"模型写不出"。这里只做**等价写法归一**，不放松任何标准：
    body 不是合法 JSON 就原样返回，该红还是红。
    """
    _b = getattr(got, "body", None)
    if _b is not None:
        try:
            got = json.loads(_b.decode("utf-8", "replace")
                             if isinstance(_b, (bytes, bytearray)) else _b)
        except Exception:
            pass
    _ok = got == want
    _d = f"{desc}: 得到 {got!r}，应为 {want!r}"
    if not _ok and isinstance(got, dict) and isinstance(want, dict):
        _ex = [k for k in got if k not in want]
        _mi = [k for k in want if k not in got]
        if _ex:
            _d += ("（多出键 " + "、".join(map(str, _ex))
                   + "：字典必须逐键相等，多余的键要删掉）")
        if _mi:
            _d += "（缺少键 " + "、".join(map(str, _mi)) + "）"
    return _ok, _d, got


def _load():
    """先按模块名 import；失败再**按文件路径**加载。

    为什么必须有回退：`cal/core/numeric.py` 这种嵌套路径，`import numeric` 必然
    ModuleNotFoundError（仓库根上根本没有叫 numeric 的模块），于是整模块被判红。
    实测（2026-11-05 自生成第一夜）：模型写对了 5 个函数，判据却 0/5 全红、连空转 6 轮。
    判据不该依赖"产物恰好把包结构建全"——按路径加载对任何布局都成立。
    """
    try:
        return importlib.import_module(mod_name)
    except Exception as first_exc:
        if not rel_path:
            raise
        import importlib.util
        path = os.path.join(ws_root, rel_path.replace("/", os.sep))
        if not os.path.exists(path):
            raise
        spec = importlib.util.spec_from_file_location(mod_name, path)
        if spec is None or spec.loader is None:
            raise first_exc
        m = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = m
        spec.loader.exec_module(m)
        return m
try:
    # 产物文件判据（contract.artifact_exists）不依赖被测模块 —— 全部检查都是它时
    # **不 import 模块**（模板文件存在与否，跟 app 包能不能导入无关）。
    # §20.6 C 档：语言级测试/类型检查是**全仓命令**，同样不 import 被测模块
    # （Go/Java 项目根本没有可 import 的 Python 模块，误 import 会整片判红）。
    _NO_MODULE_KINDS = ("artifact_exists", "lang_test", "lang_typecheck")
    _need_m = any(c.get("kind") not in _NO_MODULE_KINDS for c in checks)
    m = None
    if _need_m:
        m = _load()
except Exception as exc:
    msg = f"{type(exc).__name__}: {exc}"
    print(json.dumps({"ok": False,
                      "fails": [{"kind": "import", "got": type(exc).__name__,
                                 "detail": f"被测模块导入失败（{msg}）"}],
                      "passed": 0, "total": len(checks)}))
    raise SystemExit
passed = 0
advisories = []
for c in checks:
    kind = c["kind"]
    try:
        if kind == "robust":
            # 边界矩阵：业务异常（ValueError 等）算合格；崩溃型异常算不合格。
            # advisory（hard=False）只记进 advisories，不翻转 ok —— 避免把"允许崩"的语义判死。
            try:
                eval(c["expr"], {"m": m})
                ok, detail = True, ""
            except Exception as exc:
                bad = type(exc).__name__ in (c.get("exc_ok") or [])
                ok = not bad
                detail = (f"{c['desc']}: 边界输入把实现打崩了（{type(exc).__name__}: {exc}）"
                          if bad else "")
                if bad and not c.get("hard"):
                    advisories.append({"kind": kind, "detail": detail})
                    ok, detail = True, ""
        elif kind == "artifact_exists":
            # §20.4 A 档落点 2：契约声明的"必须产出的文件"。
            # 文件不存在 = 硬失败（不是提示）—— 这一条是本机制的核心价值：
            # 探针只测行为不测"需求点名的产物是否真的产出"，cap.page.home 就曾
            # 带着不存在的 app/templates/index.html 标成 done（done 骗人）。
            # 注意：这里在**字符串模板**里，不能调用 props.py 的模块级函数
            # （实测 2026-11-10：调了模块级的 _compare → NameError，全仓判据变红）。
            _fp = os.path.join(ws_root, str(c.get("path") or "").replace("/", os.sep))
            if not str(c.get("path") or "").strip():
                ok, detail = False, f"{c['desc']}: 产物文件路径为空（判据本身写坏了）"
            elif not os.path.isfile(_fp):
                ok = False
                detail = (f"{c['desc']}: 文件不存在：{c.get('path')}"
                          f"（契约要求必须产出这个文件，用 ```file:{c.get('path')} 围栏交内容）")
            else:
                try:
                    with open(_fp, "r", encoding="utf-8", errors="replace") as _fh:
                        _txt = _fh.read()
                except Exception as _e2:
                    ok, detail = False, f"{c['desc']}: 文件读不了（{_e2}）"
                else:
                    _miss = [k for k in (c.get("contains") or []) if str(k) not in _txt]
                    if _miss:
                        ok = False
                        detail = (f"{c['desc']}: 文件在，但缺要点 "
                                  + "、".join(f"「{k}」" for k in _miss)
                                  + "（页面上必须能找到这些内容）")
                    else:
                        ok, detail = True, ""
        elif kind in ("lang_test", "lang_typecheck"):
            # §20.6 C 档落点 1：真跑该语言的测试套件 / 类型检查。
            # **模板内必须自包含**（不能调 core/lang.py 的模块级函数，实测会 NameError），
            # 所以命令由判据 dict 的 cmd 字段带全。
            # 为什么 stdout 写文件而不是 capture_output：core/probe.py:945 踩过 ——
            # 命令再起孙进程（npm → node → worker）会继承管道，
            # communicate() 永不返回；写文件 + timeout 才不会把判据挂死。
            _cmd = [str(x) for x in (c.get("cmd") or [])]
            if not _cmd:
                ok, detail = False, f"{c['desc']}: 判据没带命令"
            else:
                _exe = sys.executable if _cmd[0] in ("python", "python3") \
                    else shutil.which(_cmd[0])
                if not _exe:
                    ok, detail = False, (f"{c['desc']}: 工具链 {_cmd[0]} 不在 PATH"
                                         "（引擎不猜，如实判红）")
                else:
                    _log = os.path.join(ws_root, ".sixcoder", "tmp",
                                        f"_lang_{os.getpid()}_{c.get('lang') or 'x'}.log")
                    try:
                        os.makedirs(os.path.dirname(_log), exist_ok=True)
                    except Exception:                    # noqa: BLE001
                        _log = os.path.join(os.getcwd(), "_lang_check.log")
                    try:
                        with open(_log, "w", encoding="utf-8", errors="replace") as _fh:
                            _r = subprocess.run([_exe] + _cmd[1:], cwd=ws_root,
                                                stdout=_fh, stderr=subprocess.STDOUT,
                                                timeout=240)
                        try:
                            with open(_log, "r", encoding="utf-8", errors="replace") as _fh:
                                _out = _fh.read()[-600:]
                        except Exception:                # noqa: BLE001
                            _out = ""
                        ok = _r.returncode == 0
                        detail = "" if ok else (f"{c['desc']}: {_cmd[0]} 退出码 "
                                                f"{_r.returncode}：{_out}")
                    except subprocess.TimeoutExpired:
                        ok, detail = False, f"{c['desc']}: 命令超时（240s）"
                    except Exception as _le:              # noqa: BLE001
                        ok, detail = False, (f"{c['desc']}: 命令跑不起来 "
                                             f"{type(_le).__name__}: {_le}")
        elif kind == "call_eq":
            # 实现抛异常 = 实现错，必须报成"调用抛了 X"，不能报成"检查本身出错"。
            # 实测（bench r2）：parse_date 误用 datetime.date → TypeError，
            # 旧代码把它归成"检查本身出错"，看起来像引擎的锅，分诊也就拿不到准信。
            try:
                got = eval(c["expr"], {"m": m})
            except Exception as exc:
                if not c.get("hard", True):
                    advisories.append({"kind": kind, "detail":
                                       "%s: 调用抛了 %s: %s" % (c["desc"],
                                                                type(exc).__name__, exc)})
                    ok, detail = True, ""
                else:
                    ok = False
                    detail = f"{c['desc']}: 调用抛了 {type(exc).__name__}: {exc}"
                    got = None
            else:
                try:
                    import ast as _ast
                    want = _ast.literal_eval(c["expect"])
                except Exception:
                    want = c["expect"]
                ok, detail, got = _cmp(got, want, c["desc"])
                if not ok and not c.get("hard", True):
                    advisories.append({"kind": kind, "detail": detail})
                    ok, detail = True, ""
        elif kind == "raises":
            try:
                eval(c["expr"], {"m": m})
                ok, detail = False, f"{c['desc']}: 应当抛 {c['exc']} 但没有抛"
            except Exception as exc:
                # FastAPI api 层把业务异常统一包成 HTTPException —— 同一语义，
                # 不是实现错误（实测 2026-11-08：auth_me("!!!") 抛 HTTPException
                # 被判"应为 ValueError"，怎么改都过不了）。崩溃型不算数。
                ok = (type(exc).__name__ == c["exc"]
                      or (type(exc).__name__ == "HTTPException"
                          and c["exc"] not in ("TypeError", "AttributeError")))
                detail = f"{c['desc']}: 抛了 {type(exc).__name__}，应为 {c['exc']}"
        elif kind == "keys":
            # 键形状判据是**软**的（示例才是硬的）：它靠参数类型猜一个调用，猜得对不对
            # 无从知道。实测两次冤判：
            #   · parse(argv) 被猜成 m.parse([]) → 实现正确地抛 ValueError（空参数非法）
            #     → 判红，模型怎么改都过不了（cli-tool 0/3）；
            #   · 契约里成功分支是 {"ok","value"}、失败分支还有 code/error，键取并集后
            #     与"示例精确相等"互斥 → 两版正确实现都被打回（api-layer 0/3）。
            try:
                got = eval(c["expr"], {"m": m})
            except Exception as exc:
                if not c.get("hard", True):
                    ok, detail = True, ""
                else:
                    ok, detail = False, "%s: 抛了 %s: %s" % (c["desc"], type(exc).__name__, exc)
            else:
                missing = [k for k in c["keys"] if not (isinstance(got, dict) and k in got)]
                if missing and not c.get("hard", True):
                    ok, detail = True, ""
                else:
                    ok = not missing
                    detail = "%s: 缺少键 %s" % (c["desc"], "/".join(missing)) if missing else ""
        elif kind == "thread_ok":
            # 子线程求值：抓"单线程单测全过、多线程才炸"的类（SQLite 跨线程，
            # 实测 2026-11-08 11:10：insert_college 判据 10/10，探针 ProgrammingError）。
            import threading as _th
            _box: dict = {}

            def _trun():
                try:
                    eval(c["expr"], {"m": m})
                    _box["ok"] = True
                except Exception as _e:              # noqa: BLE001
                    _box["err"] = _e
            _t = _th.Thread(target=_trun, daemon=True)
            _t.start()
            _t.join()
            ok = bool(_box.get("ok"))
            detail = ("" if ok else
                      f"{c['desc']}: 子线程抛 {type(_box.get('err')).__name__}: "
                      f"{_box.get('err')}")
        elif kind == "call_ok":
            # "按注解类型能调用"只问**类型接线**对不对，不问业务校验：
            # 传 {} 给 clean_row(row: dict) 时它抛 ValueError 是设计选择（拒绝空行），
            # 不算错；抛 AttributeError/KeyError/IndexError 才是"没想到"的崩溃。
            # 实测教训：口径不一致会把 night6 这种实现判死，而它其实只是不肯收空行。
            try:
                eval(c["expr"], {"m": m})
                ok, detail = True, ""
            except Exception as exc:
                bad = type(exc).__name__ in (c.get("exc_ok") or [])
                ok = not bad
                detail = ("%s: 抛了 %s: %s" % (c["desc"], type(exc).__name__, exc)
                          if bad else "")
        elif kind == "expr_seq":
            # 示例脚本：赋值行 exec、断言行 eval，**同一个命名空间按顺序**跑
            ns = {**getattr(m, "__dict__", {}), "m": m}
            ok, detail = True, ""
            for step in c.get("steps") or []:
                if step.get("op") in ("set", "call"):
                    try:
                        exec(step["code"], ns)
                    except BaseException as exc:   # argparse 抛 SystemExit（不是 Exception）
                        ok, detail = False, (f"{step['code']} 抛了 "
                                             f"{type(exc).__name__}: {exc}")
                        break
                    continue
                try:
                    g = eval(step["expr"], ns)
                except BaseException as exc:       # 同上：SystemExit 也要算判据红
                    ok, detail = False, (f"{step['expr']} 调用抛了 "
                                         f"{type(exc).__name__}: {exc}")
                    break
                try:
                    import ast as _ast4
                    w = _ast4.literal_eval(step["expect"])
                except Exception:
                    w = step["expect"]
                if g != w:
                    ok, detail = False, f"{step['expr']}: 得到 {g!r}，应为 {w!r}"
                    break
        elif kind == "expr_eq":
            # 整表达式判据（类 API / 链式调用）：左边整体求值，右边按字面量比较。
            # 名字要能从被测模块里解析（`LRU(1).keys()` 里的 LRU 就在模块里）。
            _ns = {**getattr(m, "__dict__", {}), "m": m}
            try:
                got_e = eval(c["expr"], _ns)
            except Exception as exc:
                ok, detail = False, "%s: 调用抛了 %s: %s" % (c["desc"],
                                                            type(exc).__name__, exc)
            else:
                try:
                    import ast as _ast3
                    want_e = _ast3.literal_eval(c["expect"])
                except Exception:
                    want_e = c["expect"]
                ok = got_e == want_e
                detail = f"{c['desc']}: 得到 {got_e!r}，应为 {want_e!r}"
        elif kind == "seq":
            # 共享资源的示例（同一个文件路径先写后读）必须在**同一个进程里按顺序**跑，
            # 否则 read 会跑到另一个空目录里。实测（实验台 file-io）：每个函数各跑一遍
            # → read_csv("t_two.csv") 必然 FileNotFoundError → 3 个函数全判红。
            ok, detail = True, ""
            for step in c.get("steps") or []:
                try:
                    step_got = eval(step["expr"], {"m": m})
                except Exception as exc:
                    ok = False
                    detail = "%s: 调用抛了 %s: %s" % (step.get("desc", step["expr"]),
                                                     type(exc).__name__, exc)
                    break
                try:
                    import ast as _ast2
                    step_want = _ast2.literal_eval(step["expect"])
                except Exception:
                    step_want = step["expect"]
                if step_got != step_want:
                    ok = False
                    detail = "%s: 得到 %r，应为 %r" % (step.get("desc", step["expr"]),
                                                     step_got, step_want)
                    break
        elif kind == "db_end2end":
            try:
                import sqlite3 as _sq
                conn = _sq.connect(":memory:")
                # 建表函数按契约给的名字调（create_table / create_tables 都行）；
                # 老的小写判据没带 create 字段时按 create_table 兜底。
                _ct = getattr(m, c.get("create") or "create_table", None)
                if callable(_ct):
                    _ct(conn)
                rows = c["rows"]
                _args = c.get("writer_args")
                if _args is None:
                    _args = [rows]
                w = getattr(m, c["writer"])(conn, *_args)
                got = getattr(m, c["reader"])(conn)
                key = c.get("key") or "id"
                if not isinstance(got, list):
                    ok, detail = False, "%s: 返回 %s，应为 list" % (c["desc"], type(got).__name__)
                elif all(isinstance(x, dict) and key in x for x in rows):
                    ids_want = sorted(str(x[key]) for x in rows)
                    ids_got = sorted(str(x.get(key)) for x in got if isinstance(x, dict))
                    want_cols = set(rows[0])
                    ok_rows = all(isinstance(x, dict) for x in got)
                    missing = sorted(want_cols - set(got[0])) if ok_rows and got else sorted(want_cols)
                    ok = ids_got == ids_want and not missing
                    detail = ""
                    if ids_got != ids_want:
                        detail = "%s: 写入 %s %s，读回 %s（写入返回 %r）" % (
                            c["desc"], key, ids_want, ids_got, w)
                    elif missing:
                        detail = "%s: 读回的行缺列 %s（写入时是 %s）" % (
                            c["desc"], "/".join(missing), "/".join(sorted(want_cols)))
                else:
                    ok = len(got) == len(rows)
                    detail = "" if ok else "%s: 写入 %d 行，读回 %d 行" % (
                        c["desc"], len(rows), len(got))
            except Exception as exc:
                ok, detail = False, "%s: 抛了 %s: %s" % (c["desc"], type(exc).__name__, exc)
        elif kind == "roundtrip":
            ok, detail = True, ""
            for x in c["samples"]:
                try:
                    inner = eval(c["forward"].replace("X", repr(x)), {"m": m})
                    back = eval(c["back"].replace("X", repr(inner)), {"m": m})
                except Exception as exc:
                    ok, detail = False, f"{c['desc']}: 输入 {x} 时 {type(exc).__name__}"
                    break
                if back != x:
                    ok, detail = False, f"{c['desc']}: 输入 {x} 往返得到 {back}"
                    break
        else:
            # 未知判据类型 = **失败**，不是通过（2026-11-24 修）。
            #
            # 原来是 ok=True：新加一种判据时，如果忘了在这里补分支，
            # 那条判据会**永远绿**——看起来"通过"，其实根本没检查。
            # 这是"报告不说谎"这个核心承诺的漏洞：
            # 一个静默通过的判据比一条失败的判据危险得多，
            # 因为它把"没验证"伪装成了"验证通过"。
            ok, detail = False, ("%s: 未知判据类型 %r（加新 kind 必须同时在这里补 RUNNER 分支）"
                                 % (c.get("desc"), kind))
    except Exception as exc:
        ok, detail = False, f"{c['desc']}: 检查本身出错 {type(exc).__name__}: {exc}"
    if ok:
        passed += 1
    elif detail:
        results.append({"kind": kind, "detail": detail})
# **结果输出必须抢回真正的 stdout**：示例脚本会临时把 sys.stdout 换成缓冲区（为了断言输出文本）。
# 万一清场语句没跑到，缓冲区还挂在 sys.stdout 上 —— 打结果等于打进黑洞：引擎看不到任何输出，
# 只能报"引擎自身跑不动"，而真正的错（argparse 拒绝参数）被彻底掩盖（实测卡了整整两轮）。
sys.stdout = sys.__stdout__
sys.stderr = sys.__stderr__
print(json.dumps({"ok": not results, "fails": results[:5], "passed": passed,
                  "total": len(checks), "advisories": advisories[:5]}))
'''


def _clean_literal(text: str) -> str:
    t = (text or "").strip().strip("`").strip()
    if not t:
        return ""
    if t.count("{") != t.count("}") or t.count("[") != t.count("]"):
        # 被 CALL_EQ 正则截断的容器字面量（dict/list）→ 断言不可信，丢弃而不是生成错断言
        return ""
    try:
        ast.literal_eval(t)
        return t
    except Exception:                                  # noqa: BLE001
        return repr(t)


def _samples(kind: str, lo: int = 1, hi: int = 3999) -> list:
    rnd = random.Random(7)
    if kind == "int":
        pool = [lo, lo + 1, (lo + hi) // 2, hi - 1, hi]
        pool += [rnd.randint(lo, hi) for _ in range(3)]
        return [p for p in pool if lo <= p <= hi]
    return ["I", "IV", "IX", "LVIII", "MCMXCIV"]


def _probe_literal(num: str, ptype: str) -> str:
    """把数值探针按参数声明类型写对字面量。

    实测事故：契约写 normalize_age(x: str) 的越界判据，探针却给整数 -1 → 产物
    AttributeError（'int' has no 'strip'），引擎据此连判 6 轮"没过"，模型被迫去改
    一个本来正确的函数。判据的类型错比漏报更贵：它会烧掉整轮预算。
    """
    t = (ptype or "").replace(" ", "").lower()
    # 已经是字符串字面量就不要**再套一层引号**：旧实现把 `"!!!"` 变成 `""!"""`，
    # 生成 `m.f(""!"""")` 这种非法表达式 → 求值抛 SyntaxError → 判据说"没抛
    # ValueError"（实测 2026-11-07：app/api/batches.py::run_batch_endpoint 因此
    # 永远过不了，8/9 也算失败，整片白跑 ¥0.57）。
    if len(num) >= 2 and num[0] in ("'", '"') and num[-1] == num[0]:
        return num
    if t.startswith("str"):
        return '"%s"' % num
    if t.startswith(("int", "float", "bool")) or t in ("", "object", "any", "number"):
        return num
    return ""                                   # 类型拿不准 → 不造探针（宁可漏报）


def _name_tokens(s: str) -> set[str]:
    """把函数名/文本切成小写词元，用来做**按词**语义判断。

    不能再用子串（`"date" in func.lower()`）：实测 `validate_name` 里含 "date"，
    于是被当成日期函数，喂 `"2024-13-01"` 并**要求它抛 ValueError**，实现当然不抛 → 假红。
    同类隐患：`average` 含 "age"、`update` 含 "date"。一律按词元判。
    """
    return set(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _invalid_probes(func: str, text: str, ptype: str = "") -> list[str]:
    """按函数/参数的语义生成"该抛错"的非法输入（实测：只给整数探针时，
    parse_date('非法格式')、clean_email('nodomain@') 这类检查永远缺失，
    于是"非法输入应抛错"成了空话，7B 也能蒙过去）。"""
    low = (func or "").lower()
    toks = _name_tokens(low)
    near = text or ""
    if "date" in toks or "dates" in toks or ("日期" in near[:400] and ("date" in toks)):
        return ['"2024-13-01"', '"2024-02-30"', '"not-a-date"']
    if "email" in toks or "mail" in toks:
        return ['"nodomain@"', '"@nodomain"', '"plain"']
    if "url" in toks or "uri" in toks:
        return ['"not a url"', '""']
    if "age" in toks:
        out = [_probe_literal("-1", ptype), _probe_literal("151", ptype)]
        return [x for x in out if x]
    # 契约里"空列表/空字符串抛 X"这类话以前完全没法验：探针生成器只认函数名，
    # 于是 mean/median/first_match 这些声明一挂，永远没有判据（"非法输入应抛错"成了空话）。
    # 实测（实验台 batch2）：5 种系统类型里 4 个都挂着这条提示，白丢了一批真判据。
    low_txt = (text or "").lower()
    _pt = (ptype or "").replace(" ", "").lower()
    # "空串返回空串 / 空列表返回 0" = 空输入是**合法**的，不许再造"必须抛错"的探针。
    # 实测（实验台 codec）：from_hex 的契约写"空串返回空串"，探针却要求 from_hex('')
    # 抛 ValueError → 两版正确实现全被判红（0/5）。
    _empty_ok = any(k in low_txt for k in ("空串返回", "空字符串返回", "空列表返回",
                                           "空数组返回", "空串得", "空列表时"))
    if _empty_ok:
        return []
    if _pt.startswith("list") and any(k in low_txt for k in ("空列表", "空数组", "列表为空", "空 list")):
        return ["[]"]
    if _pt in ("str", "text", "string") and any(
            k in low_txt for k in ("空字符串", "空串", "空字符串或", "字符串为空")):
        return ['""']
    return []



# 契约里出现这些词 → 说明它真的规定了"格式/字符集"，此时给 str 参数造一条
# 明显不合法的串（`"!!!"`）才是真判据；没有这些词就只验空输入，绝不硬造假红。
_FMT_HINT = ("非法", "无效", "格式", "不能包含", "只允许", "仅允许", "必须是",
             "长度是奇数", "非十六进制", "校验和", "不合法")

def _exact_format_hint(expect: str) -> str:
    """这条判据是不是"逐字相等的格式活"？是则返回一句提示，喂回远端。

    为什么需要（2026-11-05 通用性测试）：契约要求 `render_table(...) == 'a | b\\n1 | 2'`，
    模型自己造了另一种表格样式（`a b\\n- -\\n1 2`）—— 逻辑没错，**格式没照规格**，
    于是判红、重试、烧钱。这类失败给模型一句"这是精确格式"比给它十行报错都管用，
    因为报错里只有"得到 X 应为 Y"，模型很容易理解成"反正差不多"。
    """
    raw = str(expect or "").strip()
    inner = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"" else raw
    if len(inner) < 4:
        return ""
    marks = [m for m in ("|", "\\n", "  ", ",", "\t", ":") if m in inner]
    if not marks:
        return ""
    return ("（精确字符串：必须逐字相等，注意分隔符 %s / 换行 / 补空格 —— "
            "不要换成别的表格样式）" % "、".join(m.replace("\\n", "\\n") for m in marks))


SAMPLE = {"str": '"abc"', "int": "7", "float": "1.5", "bool": "True",          "list": "[1, 2]", "dict": '{"a": 1}', "bytes": 'b"x"',
          "list[str]": '["a", "b"]', "list[int]": "[1, 2]",
          "list[dict]": '[{"name": "a", "age": 10, "email": "a@x.com"}]',
          "dict[str, int]": '{"a": 1}'}

# 示例**前置准备行**允许出现的名字：造夹具要用它们（写文件/序列化），
# 但它们不是判据来源 —— 断言行仍然必须引用本模块的符号（防跨模块泄漏）。
PREP_BUILTINS = frozenset({
    "open", "json", "csv", "os", "io", "Path", "pathlib", "time", "datetime",
    "StringIO", "tempfile", "shutil", "copy", "math", "re",
    "__import__", "contextlib", "sys",
})

# ---------------------------------------------------------------- 数据库端到端
# 为什么专门做一条：`conn` 类参数在契约里只是 object，过去的引擎判据直接跳过它，
# 于是 store 模块"8/8 验收通过"却是假的——真实 SQLite 裸连接执行 fetch_all 立刻
# TypeError（实测独立验收 16/17 的那一条）。判据必须用真 sqlite 才叫证据。
CONN_NAMES = ("conn", "connection", "cursor", "cur", "db", "database", "con")
WRITER_HINT = ("upsert", "insert", "save", "write", "add", "put", "store", "append")
READER_HINT = ("fetch_all", "fetchall", "list_all", "get_all", "select_all", "read_all",
               "load", "read", "all", "query_all")
TABLE_RE = re.compile(
    r"(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|建表|建表\s*语句)\s*[`\"']?(\w+)[`\"']?\s*\(([^)]*)\)",
    re.I)
# "建表 A(...)、B(...)" 连写时只有第一张表紧跟在关键字后：从关键字起扫所有"名字(...)"，
# 再用"列定义里出现 SQL 类型"把函数签名过滤掉（不能只靠关键字，否则漏表）。
_SQL_REGION = re.compile(r"CREATE\s+TABLE|建表", re.I)
_TABLE_DEF_RE = re.compile(r"(\w+)\s*\(([^()]*)\)")
_SQL_TYPE_HINT = ("TEXT", "INT", "REAL", "FLOAT", "DOUBLE", "DATE", "TIME",
                  "CHAR", "VARCHAR", "BLOB", "NUMERIC", "BOOL", "PRIMARY")
COL_TYPE_DEFAULT = (("PRIMARY", 1), ("INT", 10), ("REAL", 1.5), ("FLOAT", 1.5),
                    ("DOUBLE", 1.5), ("DATE", "2000-01-01"), ("TIME", "12:00:00"),
                    ("TEXT", "a"), ("CHAR", "a"), ("VARCHAR", "a"), ("BLOB", b"x"))
SQL_KEYWORDS = ("primary", "foreign", "unique", "check", "constraint")


def _conn_like(name: str) -> bool:
    return str(name or "").strip().lower() in CONN_NAMES


def _cols_of(piece_text: str) -> list[tuple[str, str]]:
    """把一条建表语句里的列定义拆成 (列名, 类型)；不足两列视为没抽到。"""
    cols: list[tuple[str, str]] = []
    for piece in (piece_text or "").split(","):
        bits = piece.split()
        if not bits:
            continue
        if bits[0].strip().lower() in SQL_KEYWORDS:
            continue
        # 需求原文常被分隔符/换行污染（契约里模块块用"；"连接），列名两侧要洗干净
        name = bits[0].strip("`\"'；;,、。()（）[]")
        if not re.match(r"^[A-Za-z_]\w*$", name):
            continue
        if name.lower() in SQL_KEYWORDS:
            continue
        typ = " ".join(bits[1:]).upper().rstrip("；;。")
        cols.append((name, typ))
    return cols if len(cols) >= 2 else []


def _table_cols(text: str) -> list[tuple[str, str]]:
    """第一条建表语句的列名与类型；抽不到就返回空（宁可不判，不乱判）。"""
    m = TABLE_RE.search(text or "")
    return _cols_of(m.group(2)) if m else []


def _tables(text: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """文本里**所有**建表语句 → [(表名, 列)]。

    为什么必须"所有"：一个模块可以一张表配一个读回函数（store 的 load_students /
    load_teachers）。只认第一条时，第二个读回函数永远拿不到机械证据——
    实测毕设双选系统就卡在 42/44，卡的两条全是"根本没判据"而不是"判红了"。

    为什么不用 TABLE_RE.finditer：需求原文常写成"建表 students(...)、teachers(...)、
    choices(...)"，只有第一张表前面有"建表"，后面几张会被 finditer 漏掉。
    所以从第一处建表关键字起，扫所有"名字(...)"，再用"列里出现 SQL 类型"过滤掉
    函数签名（save_all(conn: object, students: list[dict]) 这类）。
    """
    t = text or ""
    m = _SQL_REGION.search(t)
    start = m.start() if m else 0
    found: dict[str, list[tuple[str, str]]] = {}
    for hit in _TABLE_DEF_RE.finditer(t, start):
        cols = _cols_of(hit.group(2))
        if not cols:
            continue
        if not any(any(h in str(typ) for h in _SQL_TYPE_HINT) for _n, typ in cols):
            continue                                   # 不是建表语句（多半是函数签名）
        found.setdefault(hit.group(1), cols)
    return list(found.items())


def _rows_for(cols: list[tuple[str, str]], n: int = 2) -> list[dict]:
    rows: list[dict] = []
    for i in range(1, n + 1):
        row: dict = {}
        for name, typ in cols:
            if "PRIMARY" in typ or name.lower() in ("id", "pk", "key"):
                row[name] = i
                continue
            val: object = "x"
            for hint, sample in COL_TYPE_DEFAULT:
                if hint in typ:
                    val = sample
                    break
            if val == "a":
                val = "name%d" % i
            elif val == "2000-01-01":
                val = "2000-01-0%d" % i
            row[name] = val
        rows.append(row)
    return rows


_READER_PREFIX = ("load_", "read_", "get_", "fetch_", "list_", "select_", "query_")
_CREATE_PREFIX = ("create", "init", "ensure")


def _entity_of(name: str) -> str:
    """load_teachers → teachers、fetch_all → all（用来对上它该读哪张表）。"""
    low = str(name or "").lower()
    for pre in _READER_PREFIX:
        if low.startswith(pre):
            low = low[len(pre):]
            break
    for suf in ("_rows", "_all"):
        if low.endswith(suf):
            low = low[: -len(suf)]
    return low


def _same_entity(a: str, b: str) -> bool:
    """表名/参数名对得上（单复数视为同一个实体）。"""
    x, y = str(a or "").lower(), str(b or "").lower()
    return bool(x) and bool(y) and (x == y or x.rstrip("s") == y.rstrip("s"))


def _db_end2end(funcs: list, text: str = "") -> list[dict]:
    """写→读端到端判据：真实 sqlite3 连接，**每个读回函数各一条**。

    三条口径都是实测踩出来的（不是设计洁癖）：
    1. 建表函数按**前缀**认（create_table / create_tables / init_db）—— 写死
       `name == "create_table"` 时，契约里的 `create_tables` 永远拿不到证据；
    2. 每个"只吃 conn 的读回函数"各出一条 —— 只取第一个匹配会让 store.load_teachers
       这类第二个读回函数永远没有判据（实测 44 个函数卡在 42/44，卡的全是"没判据"）；
    3. 读哪张表按函数名对表名（load_teachers ↔ teachers）；对不上且全文只有一张表才兜底，
       多张表又对不上就**不判**（宁可不判，不乱判）。
    """
    params_of = lambda f: list(getattr(f, "params", []) or [])          # noqa: E731
    creates = [f for f in funcs
               if len(params_of(f)) == 1 and _conn_like(params_of(f)[0].name)
               and str(f.name).lower().startswith(_CREATE_PREFIX)]
    writers = [f for f in funcs
               if len(params_of(f)) > 1 and _conn_like(params_of(f)[0].name)
               and any(h in str(f.name).lower() for h in WRITER_HINT)]
    readers = [f for f in funcs
               if len(params_of(f)) == 1 and _conn_like(params_of(f)[0].name)
               and any(h in str(f.name).lower() for h in READER_HINT)]
    if not writers or not readers:
        return []
    tables = _tables(text or "")
    if not tables:
        return []
    checks: list[dict] = []
    for rd in readers:
        ent = _entity_of(rd.name)
        hit = [t for t in tables if _same_entity(ent, t[0])]
        if hit:
            tname, cols = hit[0]
        elif len(tables) == 1:
            tname, cols = tables[0]
        else:
            continue                                   # 多张表又对不上 → 不判
        rows = _rows_for(cols)
        if not rows:
            continue
        wr = next((w for w in writers
                   if any(_same_entity(ent, p.name) for p in params_of(w)[1:])), writers[0])
        rest = params_of(wr)[1:]
        named = [i for i, p in enumerate(rest) if _same_entity(ent, p.name)]
        # 写入函数可能一次写多张表（save_all(conn, students, teachers, ...)）：
        # 按参数名把这张表的行放对位置，其它表传空列表，否则会写到错误的表上。
        args = ([rows if i in named else [] for i in range(len(rest))] if named
                else [rows] + [[] for _ in rest[1:]])
        key = next((n for n, t in cols if "PRIMARY" in str(t).upper()), cols[0][0])
        covers = [nm for nm in ([creates[0].name] if creates else []) + [wr.name, rd.name] if nm]
        checks.append({
            "kind": "db_end2end", "writer": wr.name, "reader": rd.name,
            "create": creates[0].name if creates else "",
            "writer_args": args, "rows": rows, "table": tname, "key": key,
            "covers": covers,
            "desc": ("%s 写入 %d 行到 %s 表后 %s 必须读回同样的 %s"
                     "（真实 sqlite，未设 row_factory）"
                     % (wr.name, len(rows), tname, rd.name, key)),
        })
    return checks



_ARTIFACT_ARG = re.compile(r'^"([^"\\]{1,80}\.[A-Za-z0-9]{1,6})"$')


def _example_artifact_paths(ex) -> list[str]:
    """示例里当"磁盘文件路径"用的字面量实参（如 "t_two.csv"）。"""
    out: list[str] = []
    for a in getattr(ex, "args", []) or []:
        m = _ARTIFACT_ARG.match(str(a).strip())
        if m:
            out.append(m.group(1))
    return out


def shared_artifact_exprs(contracts: dict) -> set[str]:
    """示例之间共用同一个磁盘文件（先写后读）时，返回这些示例的表达式。

    这类示例单独跑必然失败（read 跑在 write 之前，或跑在另一个空目录里），
    所以逐函数验收要跳过它们，改用下面的按序判据。
    """
    skip: set[str] = set()
    for c in (contracts or {}).values():
        owners: dict[str, set[str]] = {}
        funcs = list(getattr(c, "funcs", []) or [])
        for f in funcs:
            for ex in getattr(f, "examples", []) or []:
                for path in _example_artifact_paths(ex):
                    owners.setdefault(path, set()).add(f.name)
        shared = {p for p, names in owners.items() if len(names) >= 2}
        if not shared:
            continue
        for f in funcs:
            for ex in getattr(f, "examples", []) or []:
                if any(p in shared for p in _example_artifact_paths(ex)):
                    skip.add("m.%s(%s)" % (f.name, ", ".join(ex.args)))
    return skip


def file_pipeline_checks(contracts: dict) -> list[dict]:
    """把"同一个文件先写后读"的示例合成一条**按序执行**的判据（覆盖涉及的函数）。

    实测（实验台 file-io）：write_csv/read_csv/summarize_csv 共用 t_two.csv，
    逐函数分别跑时 read 必然 FileNotFoundError，两版正确实现都被判红（0/3）。
    """
    out: list[dict] = []
    for rel, c in (contracts or {}).items():
        groups: dict[str, list] = {}
        for f in getattr(c, "funcs", []) or []:
            for ex in getattr(f, "examples", []) or []:
                for path in _example_artifact_paths(ex):
                    groups.setdefault(path, []).append((f.name, ex))
        for path, items in groups.items():
            if len({n for n, _ in items}) < 2:
                continue
            steps = []
            for name, ex in items:
                lit = _clean_literal(ex.expect)
                if not lit:
                    continue
                steps.append({"expr": "m.%s(%s)" % (name, ", ".join(ex.args)),
                              "expect": lit,
                              "desc": "%s(%s) == %s" % (name, ", ".join(ex.args), lit)})
            if len(steps) < 2:
                continue
            out.append({"kind": "seq", "steps": steps,
                        "covers": sorted({n for n, _ in items}),
                        "desc": "%s：共享文件 %s 按序先写后读（%s）"
                                % (rel, path, " → ".join(sorted({n for n, _ in items})))})
    return out


def example_expressions(text: str) -> list[tuple[str, str]]:
    """取「示例」段落里**整表达式**形式的示例（左边不是单个函数调用的那些）。

    存在的理由（双模式）：类 API 的契约没法表达成 `f(args)` ——
    `LRU(1).keys() == []` 这种示例只能整体求值。需求是函数库就用函数判据，
    是类/链式 API 就用整表达式判据，**按需求切换**，不强迫甲方改写法。
    """
    out: list[tuple[str, str]] = []
    raw = str(text or "")
    i = raw.find("示例")
    seg = raw[i:] if i >= 0 else raw
    for line in seg.splitlines():
        s = line.strip().strip("-*` ")
        if not s or "==" not in s:
            continue
        left, right = s.split("==", 1)
        left, right = left.strip(), right.strip().strip("。；;")
        if not left or not right:
            continue
        if not re.match(r"^[A-Za-z_][\w.\[\]\"'()]*\s*(==|$)", left) and "(" not in left:
            continue
        out.append((left, right))
    return out


def expr_checks(c) -> list[dict]:
    """模块级"整表达式"判据：给类 API / 链式调用这类写不成 f(args) 的契约用。

    **必须按模块过滤**：整篇需求里的示例是跨模块的（`make_student(...)` 属于 models.py），
    以前这里读 full_text 却不筛，于是每个模块都被塞进别人的示例 → api.py/app.py/auth.py
    全被判红，一次 244 秒的夜班 0/44（实测毕设双选系统那次）。口径：表达式里的
    第一个标识符必须是**本模块自己的**函数或类，否则不属于这里。
    """
    funcs = {f.name for f in getattr(c, "funcs", []) or []}
    owner = {getattr(f, "owner_class", "") for f in getattr(c, "funcs", []) or []}
    allowed = {x for x in (funcs | owner) if x}
    out: list[dict] = []
    _src = own_text(c) or getattr(c, "full_text", "") or getattr(c, "raw", "") or ""
    for left, right in example_expressions(_src):
        m = re.match(r"^([A-Za-z_]\w*)", left)
        head = m.group(1) if m else ""
        if re.search(r"(?<![A-Za-z0-9_])_[A-Za-z_]\w*", left):
            # 用到脚本脚手架变量（`('B 30' in _txt) == True`）的断言：离开脚本必 NameError，
            # 交给 expr_seq 那条判据，不能单独成条（实测：单独成条会把正确实现判红）
            continue
        if m and re.match(r"^[A-Za-z_]\w*\s*\(", left) and head in funcs:
            continue                      # 普通函数调用，已经有 per-function 判据
        if head and head not in allowed:
            continue                      # 别的模块的示例，不属于本模块
        lit = _clean_literal(right)
        if not lit:
            continue
        out.append({"kind": "expr_eq", "expr": left, "expect": lit,
                    "covers": [f.name for f in getattr(c, "funcs", []) or []
                               if getattr(f, "is_method", lambda: False)()],
                    "desc": f"{left} == {lit}"})
    for ch in out:
        ch["expr"] = ch["expr"].replace("m.", "")   # 表达式里不许带 m.
    return out


def artifact_checks(arts) -> list[dict]:
    """§20.4 落点 2：把契约的产物文件声明变成 `artifact_exists` 判据。

    语义（与 §20.4.2 定稿一致）：文件不存在 = **硬失败**；存在但缺要点 = 硬失败
    并点名缺哪个字。要点匹配用子串（与 body_has 同一口径，不引新机制）。
    """
    out: list[dict] = []
    for a in (arts or []):
        path = str(getattr(a, "path", "") or "").strip()
        if not path:
            continue
        out.append({"kind": "artifact_exists",
                    "path": path,
                    "contains": [str(k) for k in (getattr(a, "contains", None) or [])],
                    "desc": f"产物文件 {path}",
                    "hard": True})
    return out


def from_contract(c, skip_exprs: set[str] | None = None) -> list[dict]:
    """由结构化契约生成判据：示例 / 异常 / 返回键。全部来自契约，不经模型。"""
    checks: list[dict] = []
    src = getattr(c, "raw", "") or ""
    for f in getattr(c, "funcs", []):
        # 类方法（双模式）：`m.get(k)` 这种调用根本不成立（缺 self），
        # 逐函数判据一律不生成，改用整表达式判据（expr_eq）验收。
        if getattr(f, "is_method", lambda: False)():
            continue
        for ex in f.examples:
            lit = _clean_literal(ex.expect)
            if not lit:
                continue
            expr = "m.%s(%s)" % (f.name, ", ".join(ex.args))
            if skip_exprs and expr in skip_exprs:
                continue                       # 共享文件的示例改由 seq 判据按序覆盖
            # 连接参数用 `{}`/`[]`/`None` 当占位符的示例是**假的示例**：dict 不可能有
            # execute()。这类示例一旦当硬判据，模型只能写"检测不到 execute 就静默跳过"
            # 的防御性空转代码（实验台实测：store.py 因此长出 _execute/_executemany 一堆
            # 静默吞掉的壳子，8/8 全绿而代码更烂）。真证据交给 db_end2end。
            placeholder_conn = False
            for i, p_ in enumerate(f.params):
                if i < len(ex.args) and _conn_like(p_.name) and \
                        ex.args[i].strip() in ("{}", "[]", "None", "NoneType()"):
                    placeholder_conn = True
                    break
            checks.append({"kind": "call_eq", "expr": expr, "expect": lit,
                           "hard": not placeholder_conn,
                           "desc": "%s == %s%s%s" % (expr, lit,
                                                    "（连接占位符示例 → 只提示）"
                                                    if placeholder_conn else "",
                                                    _exact_format_hint(lit))})
        for exc in f.raises:
            ptype = (f.params[0].type if f.params else "")
            # **只在单参数函数上造非法输入探针**：多参数函数只传一个实参等于缺参调用，
            # 生成的是坏判据（TypeError 冒充"没抛 ValueError"）。
            # 实测（实验台 batch2）：render("") / snippet("") / histogram([]) 全被这条
            # 冤判成红，两版正确实现都被打回 → 0/4、0/3 收工。
            probes = (_invalid_probes(f.name, src, ptype) or []) if len(f.params) == 1 else []
            if not probes:
                # 单参数函数才造默认探针（多参数造出来是缺参 TypeError，坏判据）。
                #
                # 数值类型：-1 / 0 是天然的越界输入。
                # 字符串类型：**只有契约明说"格式/非法字符"时**才造一个明显不合法的串。
                # 为什么不一刀切给 str 也造探针：`validate_name(name: str)` 的契约是
                # "空则抛 ValueError"，而 `-1` 是合法名字 —— 硬要求它抛错就是假红
                # （实测：本地模型写对了 validate_name 仍被判 4/6 不通过）。
                # 反过来 `from_hex(s: str)` 契约写着"含非十六进制字符抛 ValueError"，
                # 这时 `"!!!"` 才是一条**真判据**（实测该用例曾因没有探针而漏判）。
                base: list[str] = []
                _ptype0 = (f.params[0].type if f.params else "")
                if len(f.params) == 1:
                    if _ptype0 in ("int", "float"):
                        base = ["-1", "0"]
                    elif _ptype0 in ("str", "text", "string"):
                        _hint = any(k in (src or "").lower() for k in _FMT_HINT)
                        if _hint:
                            base = ['"!!!"']
                probes = [x for x in (_probe_literal(b, ptype) for b in base) if x]
            for lit in probes:
                checks.append({"kind": "raises", "expr": "m.%s(%s)" % (f.name, lit),
                               "exc": exc,
                               "desc": "%s(%s) 应当抛 %s（契约声明）" % (f.name, lit, exc)})
        if not f.keys:
            continue
        if getattr(f, "is_method", lambda: False)():
            # 类方法（双模式）：`m.get(k)` 不成立（缺 self），键判据也无从发起
            continue
        if str(getattr(f, "returns", "") or "").lower().startswith(
                ("list", "str", "int", "float", "bool", "tuple", "set", "none")):
            continue                       # 返回列表/标量的函数没有"键"可言
        # 探针实参优先用**契约自己的示例**：那是契约保证过的合法调用。
        # 回退到按类型猜的样本（历史上正是这个猜法把 parse([]) 判成"实现崩了"）。
        args, ok = [], True
        for ex in f.examples:
            if len(ex.args) == len(f.params):
                args = list(ex.args)
                break
        if not args:
            for p_ in f.params:
                key = p_.type.replace(" ", "").lower()
                if key.startswith("list["):
                    key = "list[dict]" if key == "list[dict]" else "list"
                if key not in SAMPLE:
                    ok = False
                    break
                args.append(SAMPLE[key])
        # 互斥检测：契约自己的示例里若有字典缺某个键，说明返回形状按分支不同，
        # "必须包含这些键"与"结果精确相等"就不可能同时满足 → 降级为提示。
        conflict = False
        # 示例里出现 ≥2 种不同的键集合 = 返回形状分分支（成功/失败），取并集必然矛盾。
        _shapes = set()
        for ex in f.examples:
            _lit0 = _clean_literal(ex.expect)
            if not _lit0:
                continue
            try:
                _v0 = ast.literal_eval(_lit0)
            except Exception:                            # noqa: BLE001
                continue
            if isinstance(_v0, dict):
                _shapes.add(frozenset(_v0))
        if not _shapes:
            # 没有示例可参照 → 从文档里数"返回形状"：出现 ≥2 种键集合就说明分分支
            _doc = (getattr(f, "doc", "") or "")
            for _grp in re.findall(r"\{[^{}]*\}", _doc):
                _ks = frozenset(re.findall(r"[\"']([A-Za-z_]\w*)[\"']\s*:", _grp))
                if _ks:
                    _shapes.add(_ks)
        if len(_shapes) > 1:
            conflict = True
        for ex in f.examples:
            lit = _clean_literal(ex.expect)
            if not lit:
                continue
            try:
                val = ast.literal_eval(lit)
            except Exception:                        # noqa: BLE001
                continue
            if isinstance(val, dict) and any(k not in val for k in f.keys):
                conflict = True
                break
        if ok:
            _kexpr = "m.%s(%s)" % (f.name, ", ".join(args))
            if not (skip_exprs and _kexpr in skip_exprs):
                checks.append({"kind": "keys", "expr": _kexpr,
                               "keys": list(f.keys),
                               "hard": not conflict,
                               "desc": "%s 的返回必须包含键 %s%s"
                                       % (f.name, "/".join(f.keys[:5]),
                                          "（示例形状不含全部键 → 只提示）"
                                          if conflict else "")})
    # conn 类参数没有样例可造 → 过去整条跳过；用真 sqlite 连接补一条写→读端到端
    if not any(_conn_like(p.name) for f in getattr(c, "funcs", []) for p in f.params):
        pass
    else:
        checks.extend(_db_end2end(list(getattr(c, "funcs", [])), src))
    # 通用护栏：**生成的 raises 探针不许和契约自己的示例打架**。
    # 同一个表达式，示例说"应当等于 X"，探针说"应当抛 Y" —— 两条判据互斥，正确实现必红。
    # 示例是硬证据，探针是按文字猜的 → 冲突时一律砍探针。
    _ex = {c["expr"] for c in checks if c.get("kind") == "call_eq"}
    checks = [c for c in checks if not (c.get("kind") == "raises" and c["expr"] in _ex)]
    # 有状态 API / **带前置准备的示例**：示例里有赋值行或造文件的行 →
    # 合成一条按序执行的小脚本判据（示例脚本：赋值行 exec、断言行 eval）。
    #
    # 为什么不能只给"类方法"用（2026-11-06 实测）：全自主模式的验收判据是命令行，
    # 而命令行要跑通得先有输入文件。示例块里写
    #     open('d.csv','w',encoding='utf-8').write('team,amount\nA,10\n')
    #     main(['--file','d.csv','--group','team','--value','amount']) == 0
    # 就能把"真验收命令"机械绑进函数级判据；但过去这条路径只为类方法开着，
    # 于是前置行被静默丢掉 → 只判出 `main(...) == 0` 却没有夹具 → 正确实现也红
    # （实测提示：`m.main(['--file','sales.csv',...]) == 0: 得到 1，应为 0`）。
    _src_all = own_text(c) or getattr(c, "full_text", "") or getattr(c, "raw", "") or ""
    _funcs = [f.name for f in getattr(c, "funcs", []) or []]
    _methods = [f.name for f in getattr(c, "funcs", []) or []
                if getattr(f, "is_method", lambda: False)()]
    _allowed = set(_funcs) | set(_methods) | {
        getattr(f, "owner_class", "") for f in getattr(c, "funcs", []) or []} - {""}
    _script = example_script(_src_all, allowed=_allowed,
                             prep_builtins=PREP_BUILTINS if not _methods else frozenset())
    if _script:
        # 被脚本覆盖的裸判据必须**移除**：同一条断言在"有夹具"时有意义，
        # 单独拿出来跑就是必红（实测：`main(['--file','sales.csv']) == 0` 没有夹具 → 得到 1）。
        # 留存它等于让正确实现永远判不过，而脚本版本是它的严格加强版。
        _seq = {str(x.get("expr") or "").replace("m.", "").strip()
                for x in _script if x.get("op") == "eq"}
        checks = [ch for ch in checks
                  if not (ch.get("kind") in ("call_eq", "expr_eq")
                          and str(ch.get("expr") or "").replace("m.", "").strip() in _seq)]
        _covers = _methods or [s for s in _funcs
                               if any(s in (x.get("code") or "") + (x.get("expr") or "")
                                      for x in _script)]
        checks.append({"kind": "expr_seq", "steps": _script, "covers": _covers,
                       "desc": "示例脚本按序执行：" + " → ".join(
                           s.get("expr") or s.get("code") for s in _script[:4])})
        if not _methods:
            # 函数模块：脚本与整表达式判据**并集**（脚本不会让 expr_checks 失效）
            checks.extend(expr_checks(c))
    else:
        # 类 API / 链式示例：整表达式判据（与 per-function 判据并存，互不替代）
        checks.extend(expr_checks(c))
    # 以前这里 `return checks[:14]`：模块一多函数就**静默丢掉**后面的判据（等于少验收
    # 却看不出来）。现在不截断——判据条数由契约决定，不该由引擎偷偷砍。
    return checks


def typed_checks(source: str, exclude: set[str] | None = None) -> list[dict]:
    """按注解生成"类型冒烟"判据：用注解对应的样例参数调用一次，只要求不抛异常。

    抓的是"函数体假设了 str 却收到 int"这类错误（实测 summarize 对数字调 .strip()）。
    保守：只对"所有参数都有基础类型注解"的公开函数生成，不猜复杂形态。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out: list[dict] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") or node.decorator_list:
            continue
        args = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
        if not args or node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            continue
        samples, ok = [], True
        for a in args:
            if a.arg in ("self", "cls") or a.annotation is None:
                ok = False
                break
            try:
                ann = ast.unparse(a.annotation).replace(" ", "").lower()
            except Exception:
                ok = False
                break
            ann = ann.replace("typing.", "")
            key = ann
            if ann not in SAMPLE:
                if ann.startswith("list["):
                    inner = ann[5:-1]
                    key = "list[dict]" if inner == "dict" else ("list[int]" if inner == "int" else "list[str]")
                elif ann.startswith("dict["):
                    key = "dict[str, int]"
                elif ann.startswith("optional["):
                    key = "str"
                else:
                    ok = False
                    break
            samples.append(SAMPLE[key])
        if not ok:
            continue
        out.append({"kind": "call_ok",
                    "expr": "m.%s(%s)" % (node.name, ", ".join(samples)),
                    "exc_ok": sorted(CRASH_EXC),
                    "desc": "%s 按注解类型调用不报错" % node.name})
    return out[:6]


def _literal_args(func: str, args: str) -> str:
    """校验示例参数是不是**字面量**；带自由变量（a、b、x）的一律不认。

    为什么：需求里的函数签名说明 `divide(a, b)` 会被"应抛 ValueError"的抽取规则当成调用，
    生成出 `divide(a, b)` 这样的测试 → `NameError: name 'a' is not defined`，
    整条任务被自己的判据搞挂。示例必须能用字面量写出来，否则不叫示例。

    返回可用的参数串："字面量保留" / "原本就没有参数并且函数本身没问题时返回空串"。
    """
    text = (args or "").strip()
    if not text:
        return ""
    try:
        pieces = [p.strip() for p in _split_top_level(text)]
    except Exception:                                    # noqa: BLE001
        return ""
    for piece in pieces:
        if not piece:
            continue
        try:
            ast.literal_eval(piece)
        except Exception:                                # noqa: BLE001
            # 允许 `<literal>  # 注释` 之类的小尾巴
            head = piece.split("#")[0].strip()
            try:
                ast.literal_eval(head)
            except Exception:                            # noqa: BLE001
                return ""                                # 有自由变量 → 整条不算示例
    return text


def _split_top_level(text: str) -> list[str]:
    """按顶层逗号切参数（不进括号/引号内部）。"""
    out, buf, depth, quote = [], [], 0, ""
    for ch in text:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
        elif ch in "([{":
            depth += 1
            buf.append(ch)
        elif ch in ")]}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        out.append("".join(buf))
    return out


# 边界矩阵：按注解给每个参数一组"病态但合法"的取值。
# 目标不是"值等于什么"，而是**不崩、不静默出错**：
#   业务异常（ValueError/自定义）算合格；崩溃型（AttributeError/KeyError/IndexError/
#   TypeError/ZeroDivisionError/RecursionError）算不合格 —— 这类说明实现假设了输入形态。
CRASH_EXC = {"AttributeError", "KeyError", "IndexError", "TypeError", "ZeroDivisionError",
             "RecursionError", "UnboundLocalError", "StopIteration", "OverflowError"}
BOUNDARY_SAMPLE: dict[str, list[str]] = {
    "str": ['""', '"   "', '"αβ"', '"x" * 1000'],
    "int": ["0", "-1", "10 ** 9"],
    "float": ["0.0", "-1.5", "1e9"],
    "bool": ["True", "False"],
    "list": ["[]", "[1]", "list(range(1000))"],
    "dict": ["{}", '{"a": 1}'],
    "list[dict]": ["[]",
                   '[{"age": 10, "email": "a@x.com"}]',
                   '[{}]',                                  # 缺字段
                   '[{"age": None, "email": None}]',        # 字段是 None
                   '[{"age": "10", "email": "a@x.com"}]',   # 字段类型不对
                   '[{"age": i, "email": f"u{i}@x.com"} for i in range(1000)]'],
    "list[str]": ["[]", '["a"]', '[""]', '["x" * 500]'],
    "list[int]": ["[]", "[1]", "[0, -1]"],
    # list[float] 以前没有样本 → 统计类模块（mean/median/p95）一条边界判据都生成不出来，
    # 等于这类项目完全没被边界覆盖。实测（实验台干跑）才发现。
    "list[float]": ["[]", "[1.0]", "[0.0]", "[-1.5, 0.0]"],
    "dict[str, int]": ["{}", '{"a": 1}'],
}


def boundary_checks(source: str, exclude: set[str] | None = None,
                    hard: bool | None = None,
                    must_raise: dict[str, set[str]] | None = None) -> list[dict]:
    """按注解生成**边界矩阵**判据（属性驱动，不是只验示例）。

    为什么值钱：示例只覆盖需求作者想到的那一条，而真实代码崩在边界上。实测（离线跑本机
    5 个产物）：night2 的 `summarize` 在 `[{"age": None}]` 上 TypeError、
    night3 的 `summarize([])` 直接 ZeroDivisionError —— 而这两份产物当时的独立验收都是 19/19。
    也就是说：**只验示例的判据会漏掉空输入崩溃这种硬伤**。

    断言口径：
      - 业务异常（ValueError 等）→ 合格（是明确拒绝，不是崩）；
      - 崩溃型异常（AttributeError/KeyError/IndexError/TypeError/ZeroDivisionError…）→ 不合格。
    默认 hard=True（这类缺陷在任何标准下都算缺陷）；传 hard=False 可作为建议不阻塞。
    """
    try:
        tree = ast.parse(source or "")
    except SyntaxError:
        return []
    excl = exclude or set()
    out: list[dict] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_") or node.name in excl or node.decorator_list:
            continue
        args = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
        if not args or node.args.vararg or node.args.kwarg or node.args.kwonlyargs:
            continue
        anns: list[str] = []
        ok = True
        for a in args:
            if a.arg in ("self", "cls") or a.annotation is None:
                ok = False
                break
            try:
                ann = ast.unparse(a.annotation).replace(" ", "").lower()
            except Exception:                            # noqa: BLE001
                ok = False
                break
            anns.append(ann)
        if not ok or any(a not in BOUNDARY_SAMPLE for a in anns):
            continue
        # 基础组合：每个参数取"正常值"，然后逐个替换成病态值（one-at-a-time）
        base = [BOUNDARY_SAMPLE[a][1] if len(BOUNDARY_SAMPLE[a]) > 1 else BOUNDARY_SAMPLE[a][0]
                for a in anns]
        smallest = [BOUNDARY_SAMPLE[a][0] for a in anns]
        probes: list[tuple[list[str], str]] = [(smallest, "全部取最小/空输入")]
        for i, ann in enumerate(anns):
            for val in BOUNDARY_SAMPLE[ann]:
                if val == base[i]:
                    continue
                combo = list(base)
                combo[i] = val
                probes.append((combo, f"第{i + 1}个参数 = {val[:40]}"))
        # 每个函数单独截断，再整体截断。踩过：整体 out[:10] 会让靠后的函数一条边界判据都拿不到
        # （模块里先出现几个函数就把预算吃完了），于是"边界矩阵"对它们形同不存在。
        #
        # `[{}]`（一个键都没有的行）单独说明：它是这套判据里最严的一条，因为"一个字段都没有的
        # 行"到底算不算合法输入，需求里没写 —— 按 docs/13 的纪律，这种"需要替甲方猜意图"的输入
        # 不该当硬判据。所以它默认仍然硬判（要求用 .get 取值），但可以用环境变量降级：
        #     SIXCODER_BOUNDARY_NO_EMPTY_DICT=1   → 该条只作为提示，不判红
        # 实测依据：7B 在 summarize 上 0/3 都是栽在这一条（KeyError: 'age'）。
        lenient_empty = os.environ.get("SIXCODER_BOUNDARY_NO_EMPTY_DICT", "") not in ("", "0")
        hard_one = True if hard is None else bool(hard)
        # **不按数量截断，按"证据类型去重"**：一个 5 参数函数天然会产生 20+ 条组合，
        # 截到 8 条等于随机丢掉真正的边界（以前还踩过全局 out[:10] 让靠后函数饿死）。
        # 去重口径：同一函数下，同一"病态值"只保留第一次出现（它才是这条判据的信息量），
        # "全部取最小/空输入"永远保留。
        seen_val: set[str] = set()
        # 契约声明"这些实参必须抛异常"→ 它们不能再用"不许崩"来判。
        # 否则 div(1, 0) 既被要求抛 ZeroDivisionError、又被要求不崩，正确实现也红。
        forbid = {x.strip() for x in ((must_raise or {}).get(node.name) or set())}
        # 数值**极值**（-1 / -1.5 / 1e9 / 10**9）只提示、不判红。
        # 实测代价：计算器那个 `pow(0.0, -1)`（= float 底数取 0，指数取 int 的基准 -1）
        # 被当成"实现崩了"—— 而 0.0 的负次幂在数学上本来就无定义，契约也写明
        # "不支持负数底数"，正确实现必红 → 6 轮 oscillation 全部回滚，产物停在桩上。
        # 口径：0 是最经典的退化值（空/零长度/零除数），继续硬判；-1 和 1e9 是否合法
        # 完全取决于业务域，契约几乎不会枚举 —— 那是"替甲方猜意图"，按 docs/13 不该硬判。
        extreme = {"-1", "-1.5", "10 ** 9", "1e9"}
        for combo, why in probes:
            if any(str(a).strip() in forbid for a in combo):
                continue
            args_txt = ", ".join(combo)
            key = "|".join(combo)
            if key in seen_val:
                continue
            seen_val.add(key)
            soft = lenient_empty and "[{}]" in " ".join(combo)
            num_x = any(str(a).strip() in extreme for a in combo)
            out.append({"kind": "robust",
                        "expr": f"m.{node.name}({args_txt})",
                        "exc_ok": sorted(CRASH_EXC),
                        "hard": hard_one and not soft and not num_x,
                        "desc": f"{node.name} 边界输入不崩（{why}）"
                                + ("［数值极值·仅提示］" if num_x else "")
                                + ("（空字典行：仅提示）" if soft else "")})
    # 全局也不按数量砍：只去掉**完全重复的表达式**（同一函数同一组参数可能被两条规则各生成一次）
    uniq: dict[str, dict] = {}
    for c in out:
        uniq.setdefault(str(c.get("expr")), c)
    return list(uniq.values())


def from_requirement(requirement: str, available: set[str]) -> list[dict]:
    """从需求文本抽取可判定事实。available 是产物里实际存在的符号名集合。"""
    text = requirement or ""
    strict = bool(available)          # 产物还不存在时不做名字过滤（测试先行）
    checks: list[dict] = []
    for m in CALL_EQ.finditer(text):
        name, raw_args, expect = m.group(1), m.group(2).strip(), m.group(3)
        if strict and name not in available:
            continue
        lit = _clean_literal(expect)
        if not lit:
            continue
        # 参数必须是字面量（理由见 _literal_args）：带自由变量的"示例"不是示例
        good_args = _literal_args(name, raw_args)
        if raw_args and not good_args:
            continue
        expr = f"m.{name}({good_args})"
        checks.append({"kind": "call_eq", "expr": expr, "expect": lit,
                       "desc": f"{expr} == {lit}"})
    for m in RAISES.finditer(text):
        name, args, exc = m.group(1), m.group(2).strip(), m.group(3)
        if (strict and name not in available) or not args:
            continue
        if exc in ("e", "f", "x", "它", "错"):
            continue
        # 参数必须是**字面量**。实测事故："增加 divide(a, b)：当 b 为 0 时抛 ValueError"
        # 会被抽成 `m.divide(a, b)` 并且真的生成测试 → NameError: name 'a' is not defined，
        # 于是整条演示任务被自己的判据搞挂（判据自己出错，比漏报更贵，见 docs/16）。
        args = _literal_args(name, args)
        if not args:
            continue
        checks.append({"kind": "raises", "expr": f"m.{name}({args})", "exc": exc,
                       "desc": f"{name}({args}) 应当抛 {exc}"})
    # 修复契约的"导入期兼容"判据：`f() 无参调用必须成功` → call_ok。
    # 为什么要能从文本生成（实测 2026-11-08 run15）：工作区被"函数签名与调用点不匹配"
    # 毒化时，函数级判据全绿（它们带参调用）、verified 状态齐全 → 体检派不出单 →
    # 修复片空转。一条必然失败的 call_ok 才能让体检重新派单。
    for m in re.finditer(r"([A-Za-z_]\w{2,})\(\)[^。\n]{0,20}?(?:必须|应|要)[^。\n]{0,14}?(?:成功|不许崩|不抛|不报错)",
                         text):
        name = m.group(1)
        if strict and name not in available:
            continue
        checks.append({"kind": "call_ok", "expr": f"m.{name}()",
                       "exc_ok": sorted(CRASH_EXC),
                       "desc": f"{name}() 无参调用必须成功（导入期兼容）"})
    # 跨线程安全判据：`子线程调用 f(args) 必须成功` → thread_ok。
    # 单线程单测抓不住"连接在主线程建、请求线程用"这类缺陷（SQLite 跨线程实测）。
    for m in re.finditer(r"子线程调用\s*([A-Za-z_]\w{2,})\(([^)\n]*)\)\s*[^。\n]{0,10}?(?:必须|应|要)[^。\n]{0,10}?(?:成功|不许崩|不抛)",
                         text):
        name, raw_args = m.group(1), m.group(2).strip()
        if strict and name not in available:
            continue
        args = _literal_args(name, raw_args)
        if raw_args and not args:
            continue
        checks.append({"kind": "thread_ok", "expr": f"m.{name}({args})",
                       "desc": f"子线程调用 {name}({args}) 必须成功（跨线程安全）"})
    # round-trip：需求点名"互为逆运算"时，把前两个出现的函数当作一对
    if any(k in text for k in INVERSE):
        names = [n for n in re.findall(r"def\s+([A-Za-z_]\w{2,})", text)] or \
                [n for n in re.findall(r"([A-Za-z_]\w{2,})\s*\(", text)]
        names = [n for n in dict.fromkeys(names) if (not strict or n in available)]
        if len(names) >= 2:
            f, g = names[0], names[1]
            rng = RANGE.search(text)
            lo, hi = (int(rng.group(1)), int(rng.group(2))) if rng else (1, 999)
            checks.append({"kind": "roundtrip", "desc": f"{g}({f}(x)) == x",
                           "forward": f"m.{f}(X)", "back": f"m.{g}(X)",
                           "samples": _samples("int", lo, hi)})
            checks.append({"kind": "roundtrip", "desc": f"{f}({g}(x)) == x",
                           "forward": f"m.{g}(X)", "back": f"m.{f}(X)",
                           "samples": ["I", "IV", "IX", "LVIII", "MCMXCIV"]})
    # 需求里"非法输入（如 0）应抛 ValueError"这类写法：把字面量配上被点名的函数
    for m in re.finditer(r"(?:如|例如|比如)\s*([-\d.]+|'[^']*'|\"[^\"]*\")\s*[^。\n]{0,10}?"
                         r"(?:应|必须|需要)\s*抛\s*([A-Za-z_]\w*)", text):
        arg, exc = m.group(1), m.group(2)
        name = next((n for n in re.findall(r"([A-Za-z_]\w{2,})\s*\(", text) if (not strict or n in available)), "")
        if not name:
            continue
        checks.append({"kind": "raises", "expr": f"m.{name}({arg})", "exc": exc,
                       "desc": f"{name}({arg}) 应当抛 {exc}"})
    # 需求里"非法输入应抛 ValueError"这类**没给具体值**的写法：
    # 按参数类型与值域推断边界非法值（实测：不补这条，"非法输入"就成了空话，
    # 3B 写出 int_to_roman(0) 返回 '' 也能判 done）
    for m in re.finditer(r"(非法|越界|超出范围|不允许|无效)[^。\n]{0,24}?(?:应|必须|需要)?\s*抛\s*"
                         r"([A-Za-z_]\w*)", text):
        exc = m.group(2)
        name = next((n for n in re.findall(r"([A-Za-z_]\w{2,})\s*\(", text)
                     if not strict or n in available), "")
        if not name:
            continue
        rng = RANGE.search(text)
        lohi = (int(rng.group(1)), int(rng.group(2))) if rng else (1, 3999)
        probes = [lohi[0] - 1, 0, lohi[1] + 1]
        # 字符串类参数（date/email/url）用语义探针，比整数越界更贴近需求
        sem = _invalid_probes(name, text)
        if sem:
            for lit in sem:
                if not any(c.get("expr") == f"m.{name}({lit})" for c in checks):
                    checks.append({"kind": "raises", "expr": f"m.{name}({lit})", "exc": exc,
                                   "desc": f"{name}({lit}) 应当抛 {exc}（非法输入）"})
            continue
        for v in dict.fromkeys(probes):
            if lohi[0] <= v <= lohi[1]:
                continue
            if any(c.get("expr") == f"m.{name}({v})" for c in checks):
                continue
            checks.append({"kind": "raises", "expr": f"m.{name}({v})", "exc": exc,
                           "desc": f"{name}({v}) 应当抛 {exc}（越界输入）"})
    # 需求用一句"非法输入应抛 ValueError"覆盖多个函数时，要为每个字符串类函数分别生成语义探针
    all_exc = {m.group(2) for m in re.finditer(r"(非法|越界|超出范围|不允许|无效)[^。\n]{0,24}?"
                                               r"(?:应|必须|需要)?\s*抛\s*([A-Za-z_]\w*)", text)}
    if all_exc:
        exc1 = sorted(all_exc)[0]
        for m in re.finditer(r"([A-Za-z_]\w{2,})\s*\(\s*([A-Za-z_]\w*)\s*:\s*(\w+)", text):
            fname, _pname, ptype = m.group(1), m.group(2), m.group(3)
            if ptype.lower() not in ("str", "string"):
                continue
            for lit in _invalid_probes(fname, text):
                if any(c.get("expr") == f"m.{fname}({lit})" for c in checks):
                    continue
                checks.append({"kind": "raises", "expr": f"m.{fname}({lit})", "exc": exc1,
                               "desc": f"{fname}({lit}) 应当抛 {exc1}（非法输入）"})
    return checks[:12]


def _safe_doc(text: str) -> str:
    """净化描述以便塞进 docstring：结尾引号会拼出四个引号而语法错（实测 line 9）。"""
    t = str(text or "").replace("\n", " ")
    t = t.replace("\"\"\"", "'''").replace("\"", "'").rstrip("'")
    return t[:160]


def _is_expr(line: str) -> bool:
    """这一行是不是一个**能求值的表达式**（用来区分代码行和散文行）。

    散文（"返回两数之和，注意除零"）根本编译不过；`with ...:`、`for ...:` 之类语句
    编译成表达式也会失败。只有真表达式才当成脚本的一步去执行。
    """
    try:
        compile(line, "<prep>", "eval")
        return True
    except SyntaxError:
        return False


_MOD_HEAD_RE = re.compile(r"^\s*\d+\)\s*([\w./\\-]+\.[A-Za-z0-9]+)")


def own_text(c) -> str:
    """只取**本模块那一段**契约文本（模块标题行到下一个模块标题）。

    为什么必须切（实测 run 7 续跑）：契约对象带着全文（`full_text`/`raw`），
    于是入口模块的示例（夹具 + `main([...]) == 0` + 输出断言）会被复制进每个模块的判据
    —— `cli_args.py::parse_args` 被判 `'cost' in _txt` 红，整轮去重写三个不相干的模块。
    按模块标题切段是唯一稳的口径：示例属于它前面那个模块。
    """
    text = str(getattr(c, "full_text", "") or getattr(c, "raw", "") or "")
    if not text:
        return ""
    lines = text.splitlines()
    heads: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        m = _MOD_HEAD_RE.match(ln)
        if m:
            heads.append((i, m.group(1)))
    want = str(getattr(c, "path", "") or "").replace("\\", "/").split("/")[-1]
    if not want or not heads:
        return text
    for k, (i, p) in enumerate(heads):
        if p.replace("\\", "/").split("/")[-1] == want:
            end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
            return "\n".join(lines[i:end])
    return text


def example_script(text: str, allowed: set[str] | None = None,
                   prep_builtins: frozenset = frozenset()) -> list[dict]:
    """把「示例」段落解析成**按顺序执行的小脚本**（支持赋值语句与前置准备）。

    为什么需要（双模式·有状态 API）：`LRU(2)` 这种类要么跨多条示例保持同一个实例，
    要么只能写出 `LRU(1).keys() == []` 这种没信息量的示例 —— 实测"忘记更新最近使用"
    的坏实现照样 4/4 通过。允许 `c = LRU(2)` 这种设置行，示例就能表达状态序列：
        c = LRU(2)
        c.keys() == []
        c.put("a", 1)
        c.get("a") == 1

    prep_builtins：允许出现在**前置准备行**里的内建/标准库名字（open / json / csv …）。
    全自主模式的 CLI 验收需要先造输入文件；不清空这个白名单的话，
    `open('d.csv','w')...` 这种准备行会被"跨模块泄漏"过滤器一起丢掉。
    只对**准备行**放开，断言行仍然必须引用本模块的符号（防泄漏那条教训不能丢）。
    """
    raw = str(text or "")
    i = raw.find("示例")
    seg = raw[i:] if i >= 0 else ""
    steps: list[dict] = []

    def _ok(line_txt: str) -> bool:
        """这一行是不是"本模块的"状态脚本行（见 expr_checks 里的跨模块泄漏教训）。"""
        head = (re.match(r"^([A-Za-z_]\w*)", line_txt.strip()) or [None, ""])[1]
        if head and head.startswith("_"):
            return True          # `_` 开头的临时变量/脚手架：属于脚本自己，不是别人的 API
        if re.search(r"(?<![A-Za-z0-9_])_[A-Za-z_]\w*", line_txt):
            return True          # 行内用了脚本自己的临时变量（如 `('B 30' in _txt) == True`）
        if head and head in prep_builtins:
            return True                       # 前置准备行（造文件等）：只影响环境，不引入别人的断言
        if not allowed:
            return True
        if head in allowed:
            return True
        # 变量行（c = LRU(2) / c.put(...)）：看右边/后面出现的是不是本模块的名字
        return any(re.search(rf"\b{re.escape(n)}\s*\(", line_txt) for n in allowed)

    for line in seg.splitlines():
        s = line.strip().strip("-*` ")
        if not s or s.startswith(("示例", "必须满足", "用法")):
            continue
        if not _ok(s):
            continue
        if "==" in s:
            left, right = s.split("==", 1)
            lit = _clean_literal(right.strip().strip("。；;"))
            if lit and left.strip():
                steps.append({"op": "eq", "expr": left.strip().replace("m.", ""),
                              "expect": lit})
        elif re.match(r"^[A-Za-z_]\w*\s*=\s*\S", s) and "->" not in s:
            steps.append({"op": "set", "code": s.replace("m.", "")})
        elif "->" not in s and _is_expr(s):
            # 裸调用行（`c.put("a", 1)` / `open('d.csv','w').write(...)`）也是脚本的一步，必须执行。
            # 早先这里用的是窄正则（只认 `名字(参数)` 这种形状），于是带关键字参数、带逗号、
            # 带方法链的准备行全被丢掉（实测：造夹具的那一行 `open(...).write(...)` 直接消失，
            # 只留下断言 → 正确实现也被判红）。
            steps.append({"op": "call", "code": s.replace("m.", "")})
    # 只保留"有准备、也有断言"的脚本：只有准备行没有断言 = 判不了任何东西；
    # 没有准备行 = 普通单行示例，交给普通示例判据（不要在这里重复处理）。
    if not any(x["op"] in ("set", "call") for x in steps):
        return []                       # 没有设置/准备行 → 不是状态脚本
    eqs = [x for x in steps if x["op"] == "eq"]
    if not eqs:
        return []
    # **脚本必须真的调用本模块的函数**，否则它是别的模块的示例（跨模块泄漏）。
    # 实测（run 7 续跑）：入口模块的脚本被复制进每个模块的判据 → `cli_args.py::parse_args`
    # 被判红 → 整轮去重写三个不相干的模块。
    # 注意：要在**所有步骤**（set/call/eq）里找，不能只看断言行 —— 入口调用可能是一条
    # 不判退出码的裸调用（错误路径与成功路径都存在的探针），那只出现在 call 步里。
    if allowed and not any(re.search(rf"\b{re.escape(n)}\s*\(",
                                     (s.get("expr") or "") + (s.get("code") or ""))
                           for s in steps for n in allowed):
        return []
    return steps


def render_pytest(module: str, checks: list[dict], *, target_rel: str = "") -> str:
    """把引擎判据渲染成 pytest 文件（确定性模板，模型不参与写断言）。

    target_rel：被测产物相对仓库根的路径。带子目录时（如 app/services/audit.py），
    测试文件写在仓库根，`from audit import ...` 会 ModuleNotFoundError —— 实测
    2026-11-07 夜班：多模块 Django 项目的引擎测试 100% 失败、测试文件被删、
    能力验收拿不到证据。改为按绝对路径加载目标模块（不需要包结构、也不与
    同名模块串味）；根目录模块保持原来的 import 写法，行为不变。
    """
    names = sorted({m.group(1) for c in checks
                    for m in [re.search(r"m\.([A-Za-z_]\w{2,})\s*\(", c.get("expr")
                                        or c.get("forward") or "")] if m}
                   | {c[k] for c in checks if c.get("kind") == "db_end2end"
                      for k in ("create", "writer", "reader") if c.get(k)})
    # 只保留"像被测符号"的名字：测试函数名（test_*）绝不能出现在 import 里
    names = [n for n in names if not n.startswith("test_") and not n.startswith("_")
             and not keyword.iskeyword(n)]
    rel = str(target_rel or "").replace("\\", "/")
    parts = [p for p in Path(rel).parts if p not in (".", "")][:-1] if rel else []
    if parts and ".." not in parts:
        bind = ("\n".join(f"{n} = getattr(m, {n!r})" for n in names)
                if names else
                "globals().update({k: v for k, v in vars(m).items() if not k.startswith('_')})")
        head = ["import pytest",
                "import importlib.util as _ilu",
                "from pathlib import Path as _Path",
                f"_spec = _ilu.spec_from_file_location({module!r} + '__engine_target',",
                f"    str(_Path(__file__).resolve().parent / {rel!r}))",
                "m = _ilu.module_from_spec(_spec)",
                "_spec.loader.exec_module(m)",
                bind,
                ""]
    else:
        head = ["import pytest", f"from {module} import {', '.join(names) or '*'}", ""]
    body: list[str] = []
    for i, c in enumerate(checks):
        kind = c["kind"]
        # **只把硬判据渲染成断言**：advisory（hard=False）是"提示"，写成 assert 就成了
        # 假红 —— 实测 build 的键形状判据被标成"只提示"，渲染成测试后必失败，
        # 于是 index.py 的测试文件被删掉、交付物里一个测试都没有。
        if not c.get("hard", True):
            continue
        if kind == "expr_eq":
            # 不要在这里再写 `from x import *`：函数体内禁止星号导入（实测渲染出的测试
            # 直接语法错 → pytest 断言重写失败 → 测试文件被删、类 API 交付物没有测试）。
            body.append(f"def test_engine_{i}_expr():\n"
                        f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"\n"
                        f"    assert {c['expr']} == {c['expect']}\n")
        elif kind == "expr_seq":
            lines = [f"def test_engine_{i}_script():",
                     f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\""]
            for step in c.get("steps") or []:
                if step.get("op") in ("set", "call"):
                    lines.append(f"    {step['code']}")
                else:
                    lines.append(f"    assert {step['expr']} == {step['expect']}")
            body.append("\n".join(lines) + "\n")
        elif kind == "call_eq":
            expr = c["expr"].replace("m.", "")
            body.append(f"def test_engine_{i}_example():\n"
                        f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"\n"
                        f"    assert {expr} == {c['expect']}\n")
        elif kind == "raises":
            expr = c["expr"].replace("m.", "")
            body.append(f"def test_engine_{i}_raises():\n"
                        f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"\n"
                        f"    with pytest.raises({c['exc']}):\n"
                        f"        {expr}\n")
        elif kind == "thread_ok":
            # 子线程求值：抓"单线程单测全过、多线程才炸"的类（SQLite 跨线程，
            # 实测 2026-11-08 11:10：insert_college 判据 10/10，探针 ProgrammingError）。
            import threading as _th
            _box: dict = {}

            def _trun():
                try:
                    eval(c["expr"], {"m": m})
                    _box["ok"] = True
                except Exception as _e:              # noqa: BLE001
                    _box["err"] = _e
            _t = _th.Thread(target=_trun, daemon=True)
            _t.start()
            _t.join()
            ok = bool(_box.get("ok"))
            detail = ("" if ok else
                      f"{c['desc']}: 子线程抛 {type(_box.get('err')).__name__}: "
                      f"{_box.get('err')}")
        elif kind == "call_ok":
            expr = c["expr"].replace("m.", "")
            body.append(f"def test_engine_{i}_call_ok():\n"
                        f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"\n"
                        f"    {expr}\n")
        elif kind == "keys":
            expr = c["expr"].replace("m.", "")
            keys = list(c.get("keys") or [])[:6]
            body.append(f"def test_engine_{i}_keys():\n"
                        f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"\n"
                        f"    got = {expr}\n"
                        f"    assert isinstance(got, dict)\n"
                        f"    assert all(k in got for k in {keys!r}), sorted(got)\n")
        elif kind == "roundtrip":
            fwd = c["forward"].replace("m.", "")
            back = c["back"].replace("m.", "")
            inner = fwd.replace("X", "x")
            outer = back.replace("X", f"({inner})")
            body.append(f"def test_engine_{i}_roundtrip():\n"
                        f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"\n"
                        f"    for x in {c['samples']!r}:\n"
                        f"        assert {outer} == x\n")
        elif kind == "db_end2end":
            _args = c.get("writer_args")
            if _args is None:
                _args = [c["rows"]]
            _key = c.get("key") or "id"
            lines = [f"def test_engine_{i}_db_end2end():",
                     f"    \"\"\"引擎判据：{_safe_doc(c['desc'])}\"\"\"",
                     "    import sqlite3 as _sq",
                     "    conn = _sq.connect(':memory:')"]
            if c.get("create"):
                lines.append(f"    {c['create']}(conn)")
            lines += [f"    rows = {c['rows']!r}",
                      f"    {c['writer']}(conn, *{_args!r})",
                      f"    got = {c['reader']}(conn)",
                      "    assert isinstance(got, list), got",
                      f"    assert sorted(str(r[{_key!r}]) for r in got) =="
                      f" sorted(str(r[{_key!r}]) for r in rows), (got, rows)",
                      "    assert all(set(rows[0]) <= set(r) for r in got), got"]
            body.append("\n".join(lines) + "\n")
    return "\n".join(head + body) + "\n"


def module_symbols(ws: Path, rel: str) -> set[str]:
    p = Path(ws) / rel
    if not p.exists():
        return set()
    try:
        tree = ast.parse(p.read_text(encoding="utf-8-sig", errors="replace"))
    except SyntaxError:
        return set()
    return {n.name for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def _rmtree_force(p: Path) -> None:
    """Windows 上只读文件（.git 对象、某些缓存）会让 rmtree 静默半删，留下垃圾。"""
    import shutil
    import stat as _stat

    def _onerr(func, path, exc):                      # noqa: ANN001
        try:
            os.chmod(path, _stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    try:
        shutil.rmtree(p, onexc=_onerr)
    except TypeError:
        shutil.rmtree(p, onerror=_onerr)


def run(ws: Path, rel: str, checks: list[dict], timeout: int = 60) -> dict:
    if not checks:
        return {"ok": True, "skipped": True, "passed": 0, "total": 0, "fails": []}
    # **必须绝对化**：调用方常传相对仓库路径（`cli.py night --repo ..\projects\x`），
    # 而子进程的 cwd 也是它 —— 相对路径会被解析两遍，变成 projects\projects\x，
    # 探针脚本直接 not found。实测后果：整模块 5 个函数全被判红、夜班空转 6 轮，
    # 报告写"0/5"，而产物其实完全正确（假红）。
    ws = Path(ws).resolve()
    # **探针脚本不许写进客户仓库根目录**。实测后果：`_props_run.py` 留在交付目录里，
    # 于是 mypy 报"我们的临时脚本有类型错误"、健康度把模块数算成 2、
    # 甲方打开仓库看到莫名其妙的文件。改放 .sixcoder/tmp/，跑完即删。
    tmpd = ws / ".sixcoder" / "tmp"
    tmpd.mkdir(parents=True, exist_ok=True)
    runner = tmpd / "_props_run.py"
    runner.write_text(RUNNER, encoding="utf-8")
    # 这里**不再扫 __pycache__**：Python 自己按 mtime/size 校验缓存，陈旧 .pyc 无害；
    # 而每跑一次判据都清一批，会撞上外层"单轮删除 ≥50 个就拦下"的保护，
    # 把整次夜班直接打断（实测 2026-11-07：进程被静默 kill、连退出码都没写）。
    # 判据要在**独立空目录**里跑：契约示例里常有相对路径（write_csv("t.csv", ...)），
    # 边界矩阵还会拿 "αβ"、"x"*1000 当文件名去调用。实测后果：客户仓库里被留下
    # `t_two.csv`、`t_empty.csv`、还有一个真叫 `αβ` 的文件 —— 判据自己的垃圾。
    scratch = tmpd / f"probe-{os.getpid()}-{next(_SCRATCH_SEQ)}"
    scratch.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-B", str(runner), Path(rel).stem,
           json.dumps(checks, ensure_ascii=False), str(ws), str(rel).replace("\\", "/")]
    try:
        proc = subprocess.run(cmd, cwd=str(scratch), capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "passed": 0, "total": len(checks),
                "fails": [{"kind": "timeout", "detail": f"超过 {timeout}s（可能有死循环）"}]}
    finally:
        # **这里不删任何东西**：一次夜班要跑几十上百次判据，每次删目录+runner，
        # 很快撞上外层"单轮删除 ≥50 个就拦下"的保护，把运行直接打断
        # （实测 2026-11-07：SAFE_DELETE_BULK_CONFIRM_REQUIRED，进程 exit 1）。
        # 临时目录用"进程号+自增序号"保证不重样，脏文件下一个判据看不到它。
        pass
    lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip()]
    if not lines:
        # 探针一行输出都没有 = **引擎自己跑不动**，不是产物错。
        # 区别对待极其重要：判据自身出错被当成"函数有 bug"，夜班会反复重写正确代码
        # （实测：路径 bug 让 5/5 的产物被判 0/5，还空转了 6 轮远端调用）。
        return {"ok": False, "engine_error": True, "passed": 0, "total": len(checks),
                "fails": [{"kind": "no-output", "detail": (proc.stderr or "")[-300:]}]}
    try:
        return json.loads(lines[-1])
    except Exception:                                  # noqa: BLE001
        return {"ok": False, "engine_error": True, "passed": 0, "total": len(checks),
                "fails": [{"kind": "parse", "detail": lines[-1][:300]}]}


def as_feedback(result: dict) -> str:
    if result.get("skipped"):
        return "无可判定示例/性质（跳过）"
    head = f"引擎判据 {result.get('passed', 0)}/{result.get('total', 0)} 通过"
    if result.get("ok"):
        return head
    lines = [head + "："]
    for f in result.get("fails", [])[:4]:
        lines.append(f"  - {f.get('detail') or f.get('kind')}")
    return "\n".join(lines)
