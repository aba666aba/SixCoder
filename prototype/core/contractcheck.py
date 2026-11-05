"""契约自查：示例本身对不对，先在本机判一遍，别让远端去追一个错的答案。

为什么必须做（实测事故）：
    计算器需求合成出的契约里写了 `integral([3.0, 0.0, 2.0]) == [0.0, 3.0, 0.0, 0.5]`，
    而正确答案是 0.6667（∫2x²dx = (2/3)x³）。系统于是：
    架构师每轮都诊断"末项应为 0.5 你给了 0.666" → 远端救单 11 次 → 全部失败 →
    6/7 收工。**烧的全是"追一个错答案"的钱。**

所以契约到手第一件事不是写代码，是**自查**。能机械判的就判，判不了的标"可疑"：
    · 硬判据（矛盾）：同一函数同参数不同期望、期望类型与返回注解不符、参数类型不符、
      参数个数不符、desc 明确承诺（常数项取 0 / 保留 N 位小数）与示例冲突；
    · 可疑（advisory）：desc 提到求导/积分时结果长度不合常理、浮点长尾超出承诺位数。

判出硬矛盾 → 回问远端一次（带具体矛盾）；仍矛盾 → 这个模块不打代码，直接写进交付报告
的"契约可疑"清单，**不再为它烧 rescue**。
"""
from __future__ import annotations

import ast
import re

# 数值类型的宽松判定（int 视为 float 的合法特例）
NUMERIC = (int, float)


def _parse_literal(text: str):
    try:
        return ast.literal_eval(str(text).strip())
    except Exception:                                    # noqa: BLE001
        return _MISS


class _Miss:
    def __repr__(self) -> str:
        return "<无法解析>"


_MISS = _Miss()


def _example_pairs(func) -> list[tuple[tuple, object, str]]:
    """把函数的所有示例拆成 [(参数元组, 期望值, 原始文本)]。

    contract.Example 的字段是 {func, args: list[str], expect: str}：
    args 是**每个参数的源码片段**（已按逗号/括号切好），expect 是期望值的源码字面量。
    """
    out = []
    for ex in (getattr(func, "examples", None) or []):
        if getattr(ex, "kind", "eq") != "eq":
            continue
        args_raw = list(getattr(ex, "args", None) or [])
        expect = getattr(ex, "expect", "") or ""
        if not expect:
            continue
        parsed = []
        ok = True
        for a in args_raw:
            v = _parse_literal(a)
            if v is _MISS:
                ok = False
                break
            parsed.append(v)
        if not ok:
            continue
        want = _parse_literal(expect)
        if want is _MISS:
            continue
        raw = f"{getattr(func, 'name', '')}({', '.join(args_raw)}) == {expect}"
        out.append((tuple(parsed), want, raw))
    return out


def _type_ok(declared: str, value) -> bool | None:
    """声明的类型与字面量值是否相容。返回 None 表示"该类型不检查"。"""
    d = str(declared or "").replace(" ", "").lower()
    if not d or d in ("object", "any"):
        return None
    if d in ("float", "number"):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if d == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if d == "str":
        return isinstance(value, str)
    if d == "bool":
        return isinstance(value, bool)
    if d == "dict":
        return isinstance(value, dict)
    if d.startswith("list["):
        return isinstance(value, list)
    return None


def _nums(value) -> list:
    """把嵌套结构里的数字都摊平（dict/list 里的值也要检查）。"""
    out: list = []
    if isinstance(value, bool):
        return out
    if isinstance(value, (int, float)):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            out += _nums(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            out += _nums(v)
    return out


def _decimals(value) -> int | None:
    if isinstance(value, float):
        s = repr(value)
        return len(s.split(".")[1]) if "." in s else 0
    return None


def check(contracts: dict) -> dict:
    """跑一遍契约自查。返回 {ok, hard: [...], advisory: [...], suspicious_funcs: [...]}"""
    hard: list[str] = []
    advisory: list[str] = []
    suspicious: set[str] = set()

    for path, c in (contracts or {}).items():
        # 这份契约的示例是不是"有状态脚本"（有赋值行/裸调用行）→ 见下面第 4 条
        try:
            from . import props as _props
            _stateful = bool(_props.example_script(
                getattr(c, "full_text", "") or getattr(c, "raw", "") or ""))
        except Exception:                                # noqa: BLE001
            _stateful = False
        for f in getattr(c, "funcs", []) or []:
            name = f.name
            desc = (getattr(f, "doc", "") or "")
            params = list(getattr(f, "params", None) or [])
            ret = str(getattr(f, "returns", "") or "")
            pairs = _example_pairs(f)

            # 1) 参数个数
            for args, want, raw in pairs:
                if len(args) != len(params):
                    hard.append(f"{path}::{name} 示例参数个数 {len(args)} ≠ 契约声明 {len(params)}"
                                f"（{raw[:60]}）")
                    break

            # 2) 参数类型
            for args, want, raw in pairs:
                for p, v in zip(params, args):
                    ok = _type_ok(getattr(p, "type", ""), v)
                    if ok is False:
                        hard.append(f"{path}::{name} 示例参数 {p.name}={v!r} 与声明 "
                                    f"{getattr(p, 'type', '')} 不符（{raw[:60]}）")
                        break

            # 3) 返回类型 vs 期望值
            for args, want, raw in pairs:
                ok = _type_ok(ret, want)
                if ok is False:
                    hard.append(f"{path}::{name} 示例期望 {want!r} 与返回声明 {ret} 不符"
                                f"（{raw[:60]}）")
                    break

            # 4) 同参数不同期望（自相矛盾）
            # **有状态脚本不算矛盾**：类 API 的示例常写
            #     c = LRU(2); c.get("a") == -1; c.put("a", 1); c.get("a") == 1
            # 同一个 `c.get("a")` 在不同时刻本来就该给出不同结果。实测（实验台
            # stateful-class）：这条规则把状态脚本判成"契约矛盾"，夜班直接拒绝开跑、0 花销。
            if _stateful:
                pass
            else:
                seen: dict[str, object] = {}
                for args, want, raw in pairs:
                    key = repr(args)
                    if key in seen and repr(seen[key]) != repr(want):
                        hard.append(f"{path}::{name} 同一组参数出现两个期望值："
                                    f"{seen[key]!r} vs {want!r}")
                    seen.setdefault(key, want)

            # 4.5) 参数无类型（object/未标注）却传了字面量示例 → 这一条示例证明不了任何东西，
            #      实测来源：裁判契约给 create_table({"db": "mem"}) == None，两臂都被它判死。
            for args, want, raw in pairs:
                for p2, v in zip(params, args):
                    declared = str(getattr(p2, "type", "") or "")
                    if declared.replace(" ", "").lower() in ("object", "", "any") and                             isinstance(v, (dict, list, str, tuple)):
                        hard.append(
                            f"{path}::{name} 参数 {p2.name} 没有真实类型（{declared or '未标注'}），"
                            f"却拿字面量 {type(v).__name__} 当示例（{raw[:60]}）——"
                            f"这类示例判不了真行为，应改用端到端判据或补类型")
                        break

            # 4.6) 连接/句柄类参数拿 `{}`/`[]`/`None` 当占位示例 → 语义上不可能
            #      （dict 没有 execute()）。实测（实验台 pipeline）：模型只能写"检测不到
            #      execute 就静默跳过"的防御性空转代码 → 8/8 全绿，而 store.py 长出
            #      _execute/_executemany/_commit 三个静默吞异常的壳子，比没实现更糟。
            for args, want, raw in pairs:
                hit = ""
                for p2, v in zip(params, args):
                    nm = str(getattr(p2, "name", "") or "").lower()
                    if not any(h in nm for h in ("conn", "cursor", "sock", "handle", "fh")):
                        continue
                    if v is None or (isinstance(v, (dict, list, str)) and not v):
                        hit = f"{p2.name}={v!r}"
                        break
                if hit:
                    advisory.append(
                        f"{path}::{name} 的连接/句柄参数用 {hit} 当示例 —— 真实连接不可能是"
                        f"空 dict/列表；建议删掉这条示例、把该参数类型写成 object，"
                        f"改由端到端判据覆盖（写 dict 会逼实现去 conn.get('conn')）（{raw[:50]}）")


            # 5) desc 承诺「常数项取 0」→ 期望的首项必须为 0 / 空
            if re.search(r"常数项(取|为|是)\s*0", desc) or "常数项取0" in desc.replace(" ", ""):
                for args, want, raw in pairs:
                    if isinstance(want, list) and want and want[0] not in (0, 0.0):
                        hard.append(f"{path}::{name} 声明「常数项取 0」但示例首项是 {want[0]!r}"
                                    f"（{raw[:60]}）")

            # 6) desc 承诺「保留 N 位小数」→ 期望值小数位不能超过 N
            m = re.search(r"保留\s*(\d+)\s*位小数", desc)
            if m:
                keep = int(m.group(1))
                for args, want, raw in pairs:
                    for v in _nums(want):
                        d = _decimals(v)
                        if d is not None and d > keep + 1:   # 浮点表示允许 1 位误差
                            advisory.append(
                                f"{path}::{name} 声明保留 {keep} 位小数，但示例值 {v!r} 小数位更多"
                                f"（浮点表示可能正常，标记为可疑）")

            # 7) 结构常识：求导长度 -1、积分长度 +1（advisory）
            for args, want, raw in pairs:
                if len(args) == 1 and isinstance(args[0], list) and isinstance(want, list):
                    n, m2 = len(args[0]), len(want)
                    if "求导" in desc and n > 1 and m2 != n - 1:
                        advisory.append(f"{path}::{name} 名为求导但长度 {n}→{m2}"
                                        f"（通常应是 {n - 1}）")
                    elif "积分" in desc and n >= 1 and m2 not in (n + 1, n):
                        advisory.append(f"{path}::{name} 名为积分但长度 {n}→{m2}"
                                        f"（通常应是 {n + 1}）")

            if any(name in h for h in hard):
                suspicious.add(name)

    # 8) 字段别名：跨模块同名异写必须先声明映射（这一条花一分钱之前就能判）
    hard += _alias_conflicts(contracts, hard)
    return {"ok": not hard, "hard": hard[:12], "advisory": advisory[:8],
            "suspicious_funcs": sorted(suspicious)}


# 出现在这些词后面的裸标识符，说明它是"字段名"而不是普通英文单词
_FIELD_CONTEXT = re.compile(
    r"(?:字段|键|列|每行含|含|保留|映射|改名|写成|叫)\s*[：:]?\s*([A-Za-z_][\w, /\u4e00-\u9fa5]*)")


def _table_cols(contracts: dict) -> set[str]:
    """从契约原文里取表结构列（建表语句里的列名）。"""
    out: set[str] = set()
    joined = "\n".join(str(getattr(c, "raw", "") or "") for c in (contracts or {}).values())
    for m in re.finditer(r"CREATE\s+TABLE[^(]*\(([^)]*)\)", joined, re.I):
        for piece in m.group(1).split(","):
            name = piece.strip().split()[0] if piece.strip() else ""
            if re.match(r"^[A-Za-z_]\w{0,40}$", name):
                out.add(name.lower())
    for m in re.finditer(r"[A-Za-z_]\w{1,30}\s*\(([^()]*(?:INTEGER|TEXT|REAL|BLOB)[^()]*)\)", joined):
        for piece in m.group(1).split(","):
            name = piece.strip().split()[0] if piece.strip() else ""
            if re.match(r"^[A-Za-z_]\w{0,40}$", name):
                out.add(name.lower())
    return out


def _alias_conflicts(contracts: dict, existing: list[str]) -> list[str]:
    """清洗阶段的示例里出现"表结构里没有的键"、且没声明别名 → 硬红。

    实测（night-v2 系列四次运行）：`clean_row({"age","date","email"})` 的示例用的是 `date`，
    而表结构是 `(id, name, age, email, birth)`。模型于是把两处统一成 `date`，
    建表就少了 birth 列 —— 单函数判据全绿（示例只测这三个键），只有 e2e 能抓到。
    这类矛盾模型猜不出来，必须在**开跑前**要求写一行「别名：date → birth」。

    只查"多出来的键"，不查"缺的键"：清洗函数按行调用、只见部分键是正常的。
    """
    cols = _table_cols(contracts)
    if not cols:
        return []
    aliases: dict[str, str] = {}
    for c in (contracts or {}).values():
        aliases.update(getattr(c, "aliases", {}) or {})
    images = {k.lower() for k in aliases} | {v.lower() for v in aliases.values()}
    out: list[str] = []
    for path, c in (contracts or {}).items():
        for f in getattr(c, "funcs", []) or []:
            params = list(getattr(f, "params", None) or [])
            if not params:
                continue
            if str(getattr(params[0], "type", "")).replace(" ", "").lower() not in (
                    "dict", "list[dict]", "dict[str,any]"):
                continue
            stray: set[str] = set()
            for args, _want, _raw in _example_pairs(f):
                for v in args:
                    items = v if isinstance(v, list) else [v]
                    for it in items:
                        if not isinstance(it, dict) or not it:
                            continue
                        keys = {str(k).lower() for k in it}
                        # **只有"像一行数据"的字典才查**：与表列有交集才可能是行。
                        # 实测（毕设双选系统）：teacher_rank({"t1": [...]}, "s1") 里的 t1 是
                        # 教师编号（映射表的键），跟表列毫无交集，却被判成"凭空字段"，
                        # 整份 15 模块的契约直接被拦住不开跑。
                        if not (keys & cols):
                            continue
                        for k in it:
                            key = str(k).lower()
                            if key not in cols and key not in images:
                                stray.add(str(k))
            if not stray or any(f.name in h for h in existing):
                continue
            a = sorted(stray)[0]
            out.append(
                f"{path}::{f.name} 的示例里出现了表结构没有的键 {'/'.join(sorted(stray))}"
                f"（表只有 {'/'.join(sorted(cols))}），而契约没声明别名 —— 模型只能猜，"
                f"猜错就是 e2e 红、单函数却全绿。请加一行「别名：{a} → <表里的规范列名>」")
    return out


def unfixable(hard_items: list[str]) -> list[str]:
    """哪些矛盾**不许自动修**：必须在开跑前问甲方。

    实测事故（实验台 contradictory）：契约写了两条互相打架的示例
    （calc_fee(100.0) == 1.0 与 == 1.5），系统让远端"修契约"，远端就**删掉其中一条**
    继续跑，最后交付报告写"验收 1/1 通过" —— 甲方要的是其中一个答案，系统替他选了，
    这正是"追错答案"。所以这类矛盾一律拒绝，不许猜。
    """
    out: list[str] = []
    for h in hard_items or []:
        if "同一组参数出现两个期望值" in str(h):
            out.append(h)
    return out


def render(result: dict) -> str:
    """渲染成一段可读文本（进日志/交付报告/回问远端）。"""
    lines = []
    for h in result.get("hard") or []:
        lines.append("【矛盾】" + h)
    for a in result.get("advisory") or []:
        lines.append("【可疑】" + a)
    return "\n".join(lines)
