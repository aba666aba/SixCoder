"""跨模块业务流判据（e2e）：把"清洗 → 入库 → 汇总"串成一条真业务流跑一遍。

为什么需要它（对齐传统软件工程的"集成/系统测试"）：
单元判据再多，也只证明"每个函数单独对"。真实故障里最常见的一类恰恰是
"每个函数都对，连起来不对"——字段名对不上、返回值形态换了、上一步的输出喂不进下一步。
本模块从**契约的类型**里认出一条业务流，用真 sqlite + 真实模块依赖跑一遍，
断言跨模块不变式（写进去的能读回来、清洗产物能直接入库、汇总口径与清洗结果一致）。

原则（与 props 一致）：
- 断言由引擎写，模型不参与；
- 认不出完整业务流就**如实跳过**，绝不猜一条假的流程来自我安慰；
- 跨模块不变式只断言契约能推出的东西（列名集合、条数、均值口径）。
"""
from __future__ import annotations

from pathlib import Path

from . import props

# 汇总口径里这些键名会被当作"人数/均值"，只用于生成断言，不做业务判断
COUNT_HINTS = ("count", "total", "n", "rows", "size", "人数", "总数", "条数")
AVG_HINTS = ("avg", "mean", "average", "平均")
GROUP_HINTS = ("domain", "by_", "dist", "group", "hist", "域名")


def _type_of(p) -> str:
    return str(getattr(p, "type", "") or "").replace(" ", "").lower()


def _ret_of(f) -> str:
    return str(getattr(f, "returns", "") or "").replace(" ", "").lower()


def _pick_key(keys: list[str], hints: tuple[str, ...]) -> str:
    for h in hints:
        for k in keys:
            if h in str(k).lower():
                return k
    return ""


def plan(contracts: dict, requirement: str = "") -> dict:
    """从契约里认业务流阶段。认不全返回 {"ok": False, "reason": ...}。"""
    stages: dict[str, tuple[str, object]] = {}
    for path, c in (contracts or {}).items():
        for f in getattr(c, "funcs", []):
            params = list(getattr(f, "params", []))
            pts = [_type_of(p) for p in params]
            ret = _ret_of(f)
            # 清洗：dict -> dict（逐行）或 list[dict] -> list[dict]（整表）
            if ("clean" not in stages and pts == ["dict"] and ret in ("dict", "")):
                stages["clean"] = (path, f)
            elif ("clean" not in stages and pts == ["list[dict]"] and ret == "list[dict]"):
                stages["clean"] = (path, f)
            # 入库：conn + list[dict] -> int
            elif ("store" not in stages and len(pts) == 2 and pts[1] == "list[dict]"
                  and props._conn_like(params[0].name)):
                stages["store"] = (path, f)
            # 建表：conn -> None（契约里显式的准备步骤）
            elif ("setup" not in stages and len(pts) == 1
                  and props._conn_like(params[0].name) and f.name.lower() == "create_table"):
                stages["setup"] = (path, f)
            # 读回：conn -> list[dict]
            elif ("read" not in stages and len(pts) == 1 and props._conn_like(params[0].name)
                  and ret.startswith("list[dict]")):
                stages["read"] = (path, f)
            # 汇总：list[dict] -> dict
            elif ("agg" not in stages and pts == ["list[dict]"] and ret == "dict"):
                stages["agg"] = (path, f)
    cols = props._table_cols(requirement or "")
    if not cols:
        # 调用方没给需求原文时，退回"各模块契约原文"里找建表语句（两者都来自需求）
        joined = "\n".join(str(getattr(c, "raw", "") or "") for c in (contracts or {}).values())
        cols = props._table_cols(joined)
    missing = [k for k in ("clean", "store", "agg") if k not in stages]
    if missing:
        # 一个阶段都认不出来 = 这个契约根本不是"读脏数据→清洗→入库→汇总"的管道
        # （计算器这类纯函数契约就是如此）。旧文案固定列出 clean/store/agg，
        # 等于告诉甲方"你的需求缺了这些阶段"，是误导：换个说法，且明确"不算通过"。
        if not stages:
            return {"ok": False, "skipped": True,
                    "reason": "契约里没有可机判的跨模块数据流（纯函数型）：e2e 判不了 —— "
                              "这**不算通过**，只是没覆盖"}
        return {"ok": False,
                "reason": f"契约里只认出 {', '.join(stages)}，拼不出完整的写入→读回链路"
                          f"（缺 {', '.join(missing)}）：e2e 判不了 —— 这**不算通过**，只是没覆盖"}
    if not cols:
        return {"ok": False, "reason": "需求里没有可解析的建表语句，e2e 无法建库（跳过，不猜）"}
    return {"ok": True, "stages": stages, "cols": cols,
            "table": (stages["store"][1], stages["store"][0])}


def _flow_rows(cols: list[tuple[str, str]], n: int = 2,
               aliases: dict[str, str] | None = None) -> list[dict]:
    """造"原始脏数据"行（模拟 CSV 读进来的字符串），让清洗阶段真的有活干。

    为什么除主键外都给字符串：清洗模块的契约是 `normalize_age(x: str)`、
    `parse_date(s: str)`、`clean_email(s: str)` —— 输入本来就是文本。
    过去这里给整数，业务流当场 `AttributeError: 'int' has no 'strip'`，
    但那是**我的样例行类型错**，不是产物错（判据自己出错比漏报更贵，见 docs/16）。

    别名（aliases: 别名 → 规范名）在这里必须生效：输入行用**别名**写，
    断言仍按**规范列**查。这样"清洗阶段有没有把别名映射成规范名"就成了
    一条真的跨模块不变式，而不是靠模型自觉。
    """
    alias_of: dict[str, str] = {}
    for a, canon in (aliases or {}).items():
        alias_of.setdefault(str(canon).lower(), str(a))
    rows: list[dict] = []
    for i in range(1, n + 1):
        row: dict = {}
        for name, typ in cols:
            low, up = name.lower(), typ.upper()
            key = alias_of.get(low, name)          # 输入里用别名（如 date），不是规范名（birth）
            if "PRIMARY" in up or low in ("id", "pk"):
                row[key] = i
            elif low in ("email", "mail", "邮箱"):
                row[key] = f" U{i}@X{i}.COM "          # 大小写 + 空格，等清洗
            elif "DATE" in up or "TIME" in up or low in ("birth", "date", "生日", "日期"):
                row[key] = f"2024/01/0{i}"              # 斜杠格式，等规范化
            elif "INT" in up or "REAL" in up or "FLOAT" in up or "DOUBLE" in up:
                row[key] = f" {20 + i * 10} "           # 带空格的数字字符串
            elif low in ("name", "姓名", "用户名"):
                row[key] = f" name{i} "
            else:
                row[key] = f" v{i} "
        rows.append(row)
    return rows


def build(contracts: dict, requirement: str = "") -> dict:
    """组装 e2e 计划（不落盘）：返回 {ok, stages, cols, rows, covers, agg_keys, ...}。"""
    p = plan(contracts, requirement)
    if not p.get("ok"):
        return p
    stages = p["stages"]
    cols = p["cols"]
    # 别名来自契约声明（跨模块唯一事实源）；没有就按规范名造行
    aliases: dict[str, str] = {}
    try:
        from . import contract as _cm
        aliases = _cm.parse_aliases(requirement or "") or {}
        if not aliases:
            for c in (contracts or {}).values():
                aliases.update(getattr(c, "aliases", {}) or {})
    except Exception:                                        # noqa: BLE001
        aliases = {}
    rows = _flow_rows(cols, aliases=aliases)
    agg_path, agg_f = stages["agg"]
    agg_keys = [str(k) for k in (getattr(agg_f, "keys", []) or [])]
    covers = [stages[k][1].name for k in ("clean", "setup", "store", "agg") if k in stages]
    if "read" in stages:
        covers.append(stages["read"][1].name)
    return {"ok": True, "stages": stages, "cols": cols, "rows": rows,
            "aliases": aliases,
            "covers": covers, "agg_keys": agg_keys,
            "count_key": _pick_key(agg_keys, COUNT_HINTS),
            "avg_key": _pick_key(agg_keys, AVG_HINTS),
            "group_key": _pick_key(agg_keys, GROUP_HINTS)}


def render(plan_dict: dict, ws: Path | None = None) -> str:
    """把计划渲染成 pytest 文件（确定性模板）。"""
    stages = plan_dict["stages"]
    cols = [c for c, _ in plan_dict["cols"]]
    rows = plan_dict["rows"]
    imports: dict[str, list[str]] = {}
    for key, (path, f) in stages.items():
        imports.setdefault(Path(path).stem, []).append(f.name)
    lines = ["\"\"\"跨模块业务流（引擎生成，勿手改）：清洗 → 入库 → 汇总。",
             "",
             "为什么有这份测试：单元判据证明不了\"连起来对不对\"（字段名、形态、口径）。",
             "它由引擎渲染、由真 sqlite 执行，模型不参与写断言。",
             "\"\"\"",
             "import sqlite3"]
    for mod, names in sorted(imports.items()):
        lines.append(f"from {mod} import {', '.join(sorted(set(names)))}")
    lines += ["", f"ROWS = {rows!r}", f"COLS = {cols!r}", ""]

    clean_path, clean_f = stages["clean"]
    store_path, store_f = stages["store"]
    agg_path, agg_f = stages["agg"]
    read = stages.get("read")
    per_row = _type_of(clean_f.params[0]) == "dict" if clean_f.params else False
    lines.append("def test_e2e_flow_clean_store_report():")
    if per_row:
        lines.append(f"    cleaned = [{clean_f.name}(dict(r)) for r in ROWS]")
    else:
        lines.append(f"    cleaned = {clean_f.name}([dict(r) for r in ROWS])")
    lines += [
        "    assert isinstance(cleaned, list) and len(cleaned) == len(ROWS), cleaned",
        "    # 跨模块不变式①：清洗产物必须含表里的每一列（否则入库会静默丢字段）",
        "    assert set(COLS) <= set(cleaned[0]), (sorted(COLS), sorted(cleaned[0]))",
        "    conn = sqlite3.connect(':memory:')",
    ]
    # 建表：若存在 create_table 就先调它（它是契约里的显式步骤）
    ct = None
    if "setup" in stages:
        ct = stages["setup"][1].name
    if ct:
        lines.append(f"    {ct}(conn)")
    lines += [
        f"    n = {store_f.name}(conn, cleaned)",
        "    # 跨模块不变式②：写入条数必须等于清洗后条数",
        "    assert n == len(cleaned), (n, len(cleaned))",
    ]
    if read:
        lines += [f"    got = {read[1].name}(conn)",
                  "    assert isinstance(got, list) and len(got) == n, got",
                  "    # 跨模块不变式③：写进去的必须原样读回来（列齐全 + id 集合一致）",
                  "    assert set(COLS) <= set(got[0]), (sorted(COLS), sorted(got[0]))",
                  "    assert sorted(r['id'] for r in got) == sorted(r['id'] for r in cleaned)"]
    lines += [f"    s = {agg_f.name}(cleaned)",
              "    assert isinstance(s, dict), s"]
    ck = plan_dict.get("count_key")
    if ck:
        lines += [f"    # 跨模块不变式④：汇总口径必须与清洗结果一致（count 键 = {ck}）",
                  f"    assert s[{ck!r}] == len(cleaned), ({ck!r}, s.get({ck!r}))"]
    ak = plan_dict.get("avg_key")
    int_cols = [c for c, t in plan_dict["cols"] if "INT" in t.upper()]
    if ak and int_cols:
        col = int_cols[-1]
        lines += [f"    ages = [r[{col!r}] for r in cleaned if isinstance(r.get({col!r}), int)]",
                  f"    if ages:",
                  f"        want = round(sum(ages) / len(ages), 1)",
                  f"        got_avg = s[{ak!r}]",
                  f"        assert abs(float(got_avg) - want) <= 0.051, (got_avg, want)"]
    gk = plan_dict.get("group_key")
    if gk and ck:
        lines += [f"    # 跨模块不变式⑤：分组计数的总数必须等于人数（丢行/丢字段都会被抓到）",
                  f"    if isinstance(s.get({gk!r}), dict):",
                  f"        assert sum(s[{gk!r}].values()) == s[{ck!r}], s[{gk!r}]"]
    lines.append("")
    return "\n".join(lines)


def write(ws: Path, contracts: dict, requirement: str = "",
          fname: str = "test_e2e_flow.py", run: bool = True) -> dict:
    """生成 e2e 测试并（默认）跑一遍；跑不过就删掉，绝不往仓库里留红测试。"""
    from . import gates

    ws = Path(ws)
    p = build(contracts, requirement)
    if not p.get("ok"):
        return {"ok": True, "skipped": True, "reason": p.get("reason", "认不出业务流")}
    fp = ws / fname
    try:
        fp.write_text(render(p, ws), encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "reason": f"写测试失败：{exc}"}
    if not run:
        return {"ok": True, "files": [fname], "covers": p["covers"]}
    tr = gates.gate_tests(ws, [fname], timeout=120)
    if not tr.ok:
        try:
            fp.unlink()
        except OSError:
            pass
        # 反馈要窄：留下"哪个断言、哪一行"，而不是整段 pytest banner
        why = gates.narrow_feedback(tr.detail or "") or (tr.detail or "")[:200]
        return {"ok": False, "reason": why[:300], "covers": p["covers"], "files": []}
    return {"ok": True, "files": [fname], "covers": p["covers"]}


def as_feedback(result: dict) -> str:
    if result.get("skipped"):
        return f"跳过（{result.get('reason')}）"
    if result.get("ok"):
        return "跑通：" + "、".join(result.get("covers") or [])
    return "未跑通：" + str(result.get("reason") or "")[:160]
