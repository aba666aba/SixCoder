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
import json
import os
import random
import re
import subprocess
import sys
from pathlib import Path

CALL_EQ = re.compile(
    r"([A-Za-z_]\w{2,})\s*\(([^()]*)\)\s*(?:==|等于|应返回|应等于|返回)\s*"
    r"(`[^`]+`|\"[^\"]*\"|'[^']*'|[-\w.\[\]{}]+)")
RAISES = re.compile(
    r"([A-Za-z_]\w{2,})\s*\(([^()]*)\)\s*[^。\n]{0,12}?(?:应|必须|需要)?\s*抛\s*([A-Za-z_]\w*)")
RANGE = re.compile(r"(\d+)\s*\.\.\s*(\d+)")
INVERSE = ("互为逆运算", "互逆", "互为逆", "inverse")

RUNNER = r'''
import importlib, json, os, sys
# 探针脚本放在 .sixcoder/tmp/ 下、在**独立空目录**里运行（不再污染客户仓库），
# 所以把被测仓库根从参数传进来加回 sys.path。
sys.path.insert(0, sys.argv[3] if len(sys.argv) > 3 else os.getcwd())
mod_name, checks = sys.argv[1], json.loads(sys.argv[2])
results = []
try:
    m = importlib.import_module(mod_name)
except Exception as exc:
    print(json.dumps({"ok": False, "fails": [{"kind": "import", "got": type(exc).__name__}],
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
                ok = got == want
                detail = f"{c['desc']}: 得到 {got!r}，应为 {want!r}"
                if not ok and not c.get("hard", True):
                    advisories.append({"kind": kind, "detail": detail})
                    ok, detail = True, ""
        elif kind == "raises":
            try:
                eval(c["expr"], {"m": m})
                ok, detail = False, f"{c['desc']}: 应当抛 {c['exc']} 但没有抛"
            except Exception as exc:
                ok = type(exc).__name__ == c["exc"]
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
                    except Exception as exc:
                        ok, detail = False, (f"{step['code']} 抛了 "
                                             f"{type(exc).__name__}: {exc}")
                        break
                    continue
                try:
                    g = eval(step["expr"], ns)
                except Exception as exc:
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
            ok, detail = True, ""
    except Exception as exc:
        ok, detail = False, f"{c['desc']}: 检查本身出错 {type(exc).__name__}: {exc}"
    if ok:
        passed += 1
    elif detail:
        results.append({"kind": kind, "detail": detail})
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
    if t.startswith("str"):
        return '"%s"' % num
    if t.startswith(("int", "float", "bool")) or t in ("", "object", "any", "number"):
        return num
    return ""                                   # 类型拿不准 → 不造探针（宁可漏报）


def _invalid_probes(func: str, text: str, ptype: str = "") -> list[str]:
    """按函数/参数的语义生成"该抛错"的非法输入（实测：只给整数探针时，
    parse_date('非法格式')、clean_email('nodomain@') 这类检查永远缺失，
    于是"非法输入应抛错"成了空话，7B 也能蒙过去）。"""
    low = (func or "").lower()
    near = text or ""
    if "date" in low or "日期" in near[:400] and "date" in low:
        return ['"2024-13-01"', '"2024-02-30"', '"not-a-date"']
    if "email" in low:
        return ['"nodomain@"', '"@nodomain"', '"plain"']
    if "url" in low:
        return ['"not a url"', '""']
    if "age" in low:
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



SAMPLE = {"str": '"abc"', "int": "7", "float": "1.5", "bool": "True",
          "list": "[1, 2]", "dict": '{"a": 1}', "bytes": 'b"x"',
          "list[str]": '["a", "b"]', "list[int]": "[1, 2]",
          "list[dict]": '[{"name": "a", "age": 10, "email": "a@x.com"}]',
          "dict[str, int]": '{"a": 1}'}

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
    _src = getattr(c, "full_text", "") or getattr(c, "raw", "") or ""
    for left, right in example_expressions(_src):
        m = re.match(r"^([A-Za-z_]\w*)", left)
        head = m.group(1) if m else ""
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
                           "desc": "%s == %s%s" % (expr, lit,
                                                   "（连接占位符示例 → 只提示）"
                                                   if placeholder_conn else "")})
        for exc in f.raises:
            ptype = (f.params[0].type if f.params else "")
            # **只在单参数函数上造非法输入探针**：多参数函数只传一个实参等于缺参调用，
            # 生成的是坏判据（TypeError 冒充"没抛 ValueError"）。
            # 实测（实验台 batch2）：render("") / snippet("") / histogram([]) 全被这条
            # 冤判成红，两版正确实现都被打回 → 0/4、0/3 收工。
            probes = (_invalid_probes(f.name, src, ptype) or []) if len(f.params) == 1 else []
            if not probes:
                # 只在"单参数函数"上造默认探针：多参数函数（如 div(a, b)）里
                # `m.div(0)` 会变成缺参 TypeError，反而生成一条**坏判据**。
                # 多参数的情况下契约必须自己给可判示例，否则宁可不判（见 inspect 的提示）。
                base = ["-1", "0"] if (len(f.params) == 1 and
                                       f.params[0].type in ("int", "object", "str")) else []
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
    # 有状态 API：示例里有赋值行 → 合成一条按序执行的小脚本判据。
    # 有脚本时**不再单独生成整表达式判据**：`c.keys() == []` 离开设置行就会 NameError。
    _src_all = getattr(c, "full_text", "") or getattr(c, "raw", "") or ""
    # 同样按模块过滤：只有"本模块的类方法"才有状态脚本（否则别人的示例会漏进来）
    _methods = [f.name for f in getattr(c, "funcs", []) or []
                if getattr(f, "is_method", lambda: False)()]
    _script = example_script(_src_all, allowed=set(_methods) | {
        getattr(f, "owner_class", "") for f in getattr(c, "funcs", []) or []} - {""}) \
        if _methods else []
    if _script:
        _covers = [f.name for f in getattr(c, "funcs", []) or []
                   if getattr(f, "is_method", lambda: False)()]
        checks.append({"kind": "expr_seq", "steps": _script, "covers": _covers,
                       "desc": "示例脚本按序执行：" + " → ".join(
                           s.get("expr") or s.get("code") for s in _script[:4])})
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


def example_script(text: str, allowed: set[str] | None = None) -> list[dict]:
    """把「示例」段落解析成**按顺序执行的小脚本**（支持赋值语句）。

    为什么需要（双模式·有状态 API）：`LRU(2)` 这种类要么跨多条示例保持同一个实例，
    要么只能写出 `LRU(1).keys() == []` 这种没信息量的示例 —— 实测"忘记更新最近使用"
    的坏实现照样 4/4 通过。允许 `c = LRU(2)` 这种设置行，示例就能表达状态序列：
        c = LRU(2)
        c.keys() == []
        c.put("a", 1)
        c.get("a") == 1
    """
    raw = str(text or "")
    i = raw.find("示例")
    seg = raw[i:] if i >= 0 else ""
    steps: list[dict] = []

    def _ok(line_txt: str) -> bool:
        """这一行是不是"本模块的"状态脚本行（见 expr_checks 里的跨模块泄漏教训）。"""
        if not allowed:
            return True
        head = (re.match(r"^([A-Za-z_]\w*)", line_txt.strip()) or [None, ""])[1]
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
        elif re.match(r"^[A-Za-z_][\w.\[\]\"'()]*\s*\(.*\)$", s) and "->" not in s:
            # 裸调用行（c.put("a", 1)）：也是状态脚本的一步，必须执行
            steps.append({"op": "call", "code": s.replace("m.", "")})
    if not any(x["op"] == "set" for x in steps):
        return []                       # 没有设置行 → 不是状态脚本，交给普通示例判据
    return steps


def render_pytest(module: str, checks: list[dict]) -> str:
    """把引擎判据渲染成 pytest 文件（确定性模板，模型不参与写断言）。"""
    names = sorted({m.group(1) for c in checks
                    for m in [re.search(r"m\.([A-Za-z_]\w{2,})\s*\(", c.get("expr")
                                        or c.get("forward") or "")] if m}
                   | {c[k] for c in checks if c.get("kind") == "db_end2end"
                      for k in ("create", "writer", "reader") if c.get(k)})
    # 只保留"像被测符号"的名字：测试函数名（test_*）绝不能出现在 import 里
    names = [n for n in names if not n.startswith("test_") and not n.startswith("_")]
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
    cache = ws / "__pycache__"
    if cache.exists():
        for f in cache.glob("*.pyc"):
            try:
                f.unlink()
            except OSError:
                pass
    # 判据要在**独立空目录**里跑：契约示例里常有相对路径（write_csv("t.csv", ...)），
    # 边界矩阵还会拿 "αβ"、"x"*1000 当文件名去调用。实测后果：客户仓库里被留下
    # `t_two.csv`、`t_empty.csv`、还有一个真叫 `αβ` 的文件 —— 判据自己的垃圾。
    scratch = tmpd / f"probe-{os.getpid()}-{len(checks)}"
    if scratch.exists():
        _rmtree_force(scratch)
    scratch.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-B", str(runner), Path(rel).stem,
           json.dumps(checks, ensure_ascii=False), str(ws)]
    try:
        proc = subprocess.run(cmd, cwd=str(scratch), capture_output=True, text=True,
                              timeout=timeout, encoding="utf-8", errors="replace",
                              env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except subprocess.TimeoutExpired:
        return {"ok": False, "passed": 0, "total": len(checks),
                "fails": [{"kind": "timeout", "detail": f"超过 {timeout}s（可能有死循环）"}]}
    finally:
        for junk in (runner,):
            try:
                junk.unlink()
            except OSError:
                pass
        _rmtree_force(scratch)
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
