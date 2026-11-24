"""§20.6 C 档落点 2/3/4：工程级声明（数据/运行/质量层）验收自检。

为什么钉死这些行为（实测背景）：
    · 合成契约是模型翻译，散文段会被改写/丢掉 → 段丢了判据就永远不验收（§20.1 证据 2），
      所以 inject_sections 必须由引擎确定性注入、且幂等、且老契约逐字不变。
    · "文件存在就算过"是假绿：迁移 SQL 必须在 SQLite 里真跑、锁文件必须真锁版本、
      compose 结构必须自洽、健康端点必须在源码里真注册、可观测性不许只有注释。
    · 声明了就必须兑现（hard=True）：老契约没写新段 → 零判据、零影响、零开销。

本检查每条断言都真跑（临时目录真建文件 / 真调 run / 真调 inspect），不是只看字符串。
"""
import re
import sys
import tempfile
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import contract as C                                    # noqa: E402
from core import enterprise as E                                  # noqa: E402
from core import iterate as IT                                    # noqa: E402
from core import company as CO                                    # noqa: E402

FAILS = []
TOTAL = [0]


def check(name, cond, detail=""):
    TOTAL[0] += 1
    d = str(detail)[:180] if detail else ""
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{d}]" if d else ""))
    if not cond:
        FAILS.append(name)
    return bool(cond)


def S(kind, path, contains=None, owner=""):
    return E.EngSpec(kind=kind, path=path, contains=list(contains or []), owner=owner)


def detail_of(r):
    return "；".join(str(f.get("detail") or "") for f in (r.get("fails") or []))


KINDS = {"migration", "lockfile", "compose", "health", "observability"}


REQ = textwrap.dedent("""
    1) app/db.py：init_db()
    初始化数据库。

    【数据库迁移】
    - migrations/0001_init.sql   要点: CREATE TABLE, students

    【依赖锁定】
    - requirements.lock   要点: fastapi==
    - requirements.lock   要点: uvicorn==

    【服务编排】
    - docker-compose.yml   要点: services:

    2) app/web.py：health()
    健康检查。

    【健康检查】
    - GET /health   要点: 200, status

    【可观测性】
    - app/observability.py   要点: trace_id, metrics
""")


def main() -> int:
    print("[C1] 解析：五类段 / 别名 / owner / 要点 / 去重 / 三种输出 API")
    specs = E.parse_sections(REQ)
    check("五类段各解析出 1 条", len(specs) == 5 and {s.kind for s in specs} == KINDS,
          str([(s.kind, s.path) for s in specs]))
    by = {s.kind: s for s in specs}
    check("要点按分隔符拆开",
          by["migration"].contains == ["CREATE TABLE", "students"], str(by["migration"].contains))
    check("同 path 去重（依赖锁定重复条目只留 1 条）",
          len([s for s in specs if s.kind == "lockfile"]) == 1)
    check("health 端点串带空格也能认", by["health"].path == "GET /health", by["health"].path)
    check("owner 归属：迁移/锁/编排 → app/db.py",
          all(by[k].owner == "app/db.py" for k in ("migration", "lockfile", "compose")),
          str([(k, by[k].owner) for k in ("migration", "lockfile", "compose")]))
    check("owner 归属：健康/可观测 → app/web.py",
          all(by[k].owner == "app/web.py" for k in ("health", "observability")),
          str([(k, by[k].owner) for k in ("health", "observability")]))
    alias = textwrap.dedent("""
        【迁移】
        - migrations/0001_init.sql   要点: CREATE TABLE

        【锁文件】
        - requirements.lock   要点: fastapi==

        【编排】
        - docker-compose.yml   要点: services:

        【健康端点】
        - GET /health   要点: 200

        【可观测】
        - app/observability.py   要点: trace_id
    """)
    alias_specs = E.parse_sections(alias)
    check("段头别名都认（5 类）", len(alias_specs) == 5
          and {s.kind for s in alias_specs} == KINDS, str([s.kind for s in alias_specs]))
    rendered = E.render_sections(specs)
    check("render_sections 能 round-trip 回 5 类",
          {s.kind for s in E.parse_sections(rendered)} == KINDS, rendered[:160])
    check("round-trip 后要点不被「（归属：x）」污染",
          E.parse_sections(rendered)[0].contains == ["CREATE TABLE", "students"],
          str([s.contains for s in E.parse_sections(rendered)]))
    cs = E.checks_for(specs)
    check("checks_for 5 条且每条 hard=True",
          len(cs) == 5 and all(c.get("hard") is True for c in cs), str(cs)[:160])
    check("sections_prompt 没段返回空串", E.sections_prompt([]) == "")
    pr = E.sections_prompt(specs)
    check("sections_prompt 带 file: 围栏与逐段要求",
          "```file:migrations/0001_init.sql" in pr and "数据库迁移" in pr
          and "健康检查" in pr, pr[:160])

    print("[C2] 老契约：零判据 / 零影响 / 零开销")
    old = "1) app/services/topics.py：create_topic(title) -> dict\n创建题目。\n"
    check("老契约没段 → parse_sections 返回 []", E.parse_sections(old) == [])
    check("老契约没段 → inject_sections 逐字返回原文",
          E.inject_sections(old, "随便一句需求，没有任何段") == old)
    empty = {"ok": True, "passed": 0, "total": 0, "fails": [], "skipped": 0}
    check("run(ws, []) 固定字典", E.run(Path(tempfile.gettempdir()), []) == empty)
    check("老契约没段 → run ok=True total=0",
          E.run(Path(tempfile.gettempdir()), E.parse_sections(old)) == empty)

    print("[C3] 确定性注入：幂等 / 已存在不重复")
    base = "1) app/main.py：main()\n入口。\n"
    goal = textwrap.dedent("""
        【数据库迁移】
        - migrations/0001_init.sql   要点: CREATE TABLE

        【健康检查】
        - GET /health   要点: 200, status
    """)
    t1 = E.inject_sections(base, goal)
    check("注入后含段", "【数据库迁移】" in t1 and "【健康检查】" in t1)
    check("注入幂等（调两次相同）", E.inject_sections(t1, goal) == t1)
    t2 = E.inject_sections(t1, goal)
    check("已存在的段不重复插",
          t1.count("【数据库迁移】") == 1 and t2.count("【健康检查】") == 1)
    check("注入后能被 parse_sections 重新解析（round-trip）",
          len(E.parse_sections(t1)) == 2, str(E.parse_sections(t1)))

    print("[C4] migration 判据：SQL 真跑（sqlite3 executescript）")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "migrations").mkdir()
        (ws / "migrations" / "0001_init.sql").write_text(
            "CREATE TABLE students (id INTEGER PRIMARY KEY, name TEXT);", encoding="utf-8")
        r = E.run(ws, [S("migration", "migrations/0001_init.sql", ["CREATE TABLE"])])
        check("migration：合法 CREATE TABLE → 绿",
              bool(r.get("ok")) and r.get("total") == 1, str(r))
        r = E.run(ws, [S("migration", "migrations/0001_init.sql", ["绝不存在的字"])])
        check("migration：要点子串缺失 → 红并点名",
              (not r.get("ok")) and "要点子串缺失" in detail_of(r), detail_of(r))
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "migrations").mkdir()
        (ws / "migrations" / "0001_bad.sql").write_text("DROP TABLE ghost;", encoding="utf-8")
        r = E.run(ws, [S("migration", "migrations/0001_bad.sql", [])])
        d = detail_of(r)
        check("migration：SQL 跑不通 → 红且 detail 带 SQLite 原文",
              (not r.get("ok")) and "SQL 执行失败" in d and "no such table" in d, d)

    print("[C5] 迁移版本链：重号 / 断层")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "migrations").mkdir()
        for n in ("0001_a.sql", "0001_b.sql"):
            (ws / "migrations" / n).write_text("CREATE TABLE t (id INT);", encoding="utf-8")
        r = E.run(ws, [S("migration", "migrations/0001_a.sql", [])])
        d = detail_of(r)
        check("版本链重号 → 红并点名", (not r.get("ok")) and "重号" in d and "0001" in d, d)
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "migrations").mkdir()
        for n in ("0001_a.sql", "0003_c.sql"):
            (ws / "migrations" / n).write_text("CREATE TABLE t (id INT);", encoding="utf-8")
        r = E.run(ws, [S("migration", "migrations/0001_a.sql", [])])
        d = detail_of(r)
        check("版本链断层 → 红并点名缺号",
              (not r.get("ok")) and "断层" in d and "缺 2" in d, d)

    print("[C6] lockfile 判据：真锁版本，只列包名不算")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "requirements.lock").write_text("fastapi\nuvicorn\n", encoding="utf-8")
        r = E.run(ws, [S("lockfile", "requirements.lock", [])])
        check("lockfile：只列包名 → 红（只是清单不是锁文件）",
              (not r.get("ok")) and "只是清单不是锁文件" in detail_of(r), detail_of(r))
        (ws / "requirements.lock").write_text(
            "fastapi==0.141.1\nuvicorn==0.30.0\n", encoding="utf-8")
        r = E.run(ws, [S("lockfile", "requirements.lock", ["fastapi=="])])
        check("lockfile：pip == 真锁版本 → 绿", bool(r.get("ok")), str(r))
        (ws / "package-lock.json").write_text(
            '{"lockfileVersion": 3, "name": "x"}', encoding="utf-8")
        r = E.run(ws, [S("lockfile", "package-lock.json", [])])
        check("lockfile：npm lockfileVersion → 绿", bool(r.get("ok")), str(r))

    print("[C7] compose 判据：结构自洽 + 每服务 image/build")
    bad_compose = "version: '3'\nservices:\n  web:\n    ports:\n      - '8000:8000'\n"
    good_compose = ("version: '3'\nservices:\n  web:\n    image: python:3.11\n"
                    "    ports:\n      - '8000:8000'\n")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "docker-compose.yml").write_text(bad_compose, encoding="utf-8")
        r = E.run(ws, [S("compose", "docker-compose.yml", ["services:"])])
        check("compose：有 services 但服务缺 image/build → 红并点名",
              (not r.get("ok")) and "缺 image:/build:" in detail_of(r), detail_of(r))
        (ws / "docker-compose.yml").write_text(good_compose, encoding="utf-8")
        r = E.run(ws, [S("compose", "docker-compose.yml", ["services:"])])
        check("compose：服务齐 image → 绿", bool(r.get("ok")), str(r))
        (ws / "docker-compose.yml").write_text(
            "version: '3'\nvolumes:\n  data:\n", encoding="utf-8")
        r = E.run(ws, [S("compose", "docker-compose.yml", [])])
        check("compose：缺 services: → 红",
              (not r.get("ok")) and "services:" in detail_of(r), detail_of(r))

    print("[C8] health 判据：端点串形态 + 源码里真注册")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app").mkdir()
        (ws / "app" / "main.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8")
        r = E.run(ws, [S("health", "GET /health", [])])
        check("health：源码没注册 /health → 红且点名",
              (not r.get("ok")) and "健康端点没注册" in detail_of(r), detail_of(r))
        (ws / "app" / "main.py").write_text(
            "from fastapi import APIRouter\nrouter = APIRouter()\n"
            '@router.get("/health")\n'
            'def health():\n    return {"status": "ok"}\n', encoding="utf-8")
        r = E.run(ws, [S("health", "GET /health", [])])
        check('health：@router.get("/health") → 绿', bool(r.get("ok")), str(r))
        r = E.run(ws, [S("health", "/health", [])])
        check("health：端点串没 METHOD → 红",
              (not r.get("ok")) and "METHOD /path" in detail_of(r), detail_of(r))

    print("[C9] observability 判据：去掉注释后仍有实质标记")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "app").mkdir()
        (ws / "app" / "observability.py").write_text(
            "# trace_id 用于追踪\n", encoding="utf-8")
        r = E.run(ws, [S("observability", "app/observability.py", [])])
        check("observability：只有注释提 trace_id → 红",
              (not r.get("ok")) and "去掉 # 注释" in detail_of(r), detail_of(r))
        (ws / "app" / "observability.py").write_text(
            "import json\nimport logging\nlogger = logging.getLogger(__name__)\n"
            "def emit(trace_id):\n    logger.info(json.dumps({'trace_id': trace_id}))\n",
            encoding="utf-8")
        r = E.run(ws, [S("observability", "app/observability.py", ["trace_id"])])
        check("observability：真代码里有日志/trace_id → 绿", bool(r.get("ok")), str(r))

    print("[C10] run 固定字典 + summary 措辞")
    with tempfile.TemporaryDirectory() as td:
        r = E.run(Path(td), [S("migration", "migrations/absent.sql", [])])
        check("run 返回固定字典键",
              set(r) == {"ok", "passed", "total", "fails", "skipped"}, str(sorted(r)))
        check("fails 元素含 kind/path/detail",
              all({"kind", "path", "detail"} <= set(f) for f in r["fails"]), str(r["fails"]))
    check("summary：ok 返回空串", E.summary({"ok": True, "fails": []}) == "")
    with tempfile.TemporaryDirectory() as td:
        r = E.run(Path(td), [S("migration", "migrations/nope.sql", []),
                             S("health", "GET /health", [])])
        s = E.summary(r)
        check("summary：失败点名 kind 和 path",
              (not r.get("ok")) and s.startswith("工程声明未兑现：")
              and "数据库迁移" in s and "migrations/nope.sql" in s
              and "健康检查" in s and "GET /health" in s, s)
        many = [S("lockfile", f"lock{i}.txt", []) for i in range(8)]
        s2 = E.summary(E.run(Path(td), many))
        check("summary：最多点名 6 条",
              "lock6.txt" not in s2 and "lock7.txt" not in s2, s2[:160])

    print("[C11] 接线断言：iterate / company 源码 + 真跑闭环")
    root = Path(__file__).resolve().parents[1]
    src_it = (root / "core" / "iterate.py").read_text(encoding="utf-8", errors="replace")
    src_co = (root / "core" / "company.py").read_text(encoding="utf-8", errors="replace")
    check("iterate.py 源码里出现 enterprise.run(", "enterprise.run(" in src_it)
    check("iterate.py 往 state 塞 engineering_missing", "engineering_missing" in src_it)
    check("company.py 源码里出现 engineering_missing", "engineering_missing" in src_co)
    m = re.search(r"def needs_work_state\b[\s\S]*?\n\ndef\b", src_co)
    body = m.group(0) if m else ""
    check("needs_work_state 函数体真的引用 engineering_missing（非文档字符串）",
          bool(m) and 'st.get("engineering_missing")' in body, body[-160:])
    check("company.py 收工理由用 enterprise.summary() 生成措辞",
          "enterprise.summary(" in src_co)
    # 只因 engineering_missing 就判欠活（把其它欠活条件全部置绿）
    fake = {"app/main.py": {"exists": True, "hard_failed": [], "untested": [],
                            "stubs": [], "missing": [], "artifacts_missing": [],
                            "verified": 1, "funcs": 1,
                            "engineering_missing": ["GET /health"]}}
    check("needs_work_state：只因 engineering_missing 就判欠活",
          CO.needs_work_state(fake, "app/main.py"))
    fake_clear = {"app/main.py": dict(fake["app/main.py"], engineering_missing=[])}
    check("needs_work_state：清空 engineering_missing → 不欠活",
          not CO.needs_work_state(fake_clear, "app/main.py"))
    # 真跑 inspect：契约带工程声明但产物没兑现 → engineering_missing 真的进 state
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        req = textwrap.dedent("""
            1) app/main.py：main() -> int
            入口。

            【健康检查】
            - GET /health   要点: 200
        """)
        contracts = C.parse_requirement(req)
        st = IT.inspect(ws, contracts)
        s1 = st.get("app/main.py") or {}
        check("inspect 真跑：标出 engineering_missing",
              "GET /health" in (s1.get("engineering_missing") or []),
              str(s1.get("engineering_missing")))
        check("inspect 真跑：needs_work_state 因工程声明未兑现判 True",
              CO.needs_work_state(st, "app/main.py"))

    # 2026-11-23 真 bug：`_iter_py` 的 `.tmp` 跳过看的是**整条绝对路径**，
    # 于是产物仓只要放在任何 `.tmp*` 目录下（自检自己就在 prototype/.tmp/ 里跑），
    # 所有源文件被静默跳过 → 健康端点判据把"已注册"报成"没注册"。
    # **漏报变误报**，比漏报更糟。三条一起钉：仓内正常、仓在 .tmp 下、仓内 .tmp 子目录仍跳过。
    print("[C12] 源文件遍历：.tmp 跳过只看仓内相对路径")
    with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1] / ".tmp")) as td:
        w = Path(td)
        (w / "main.py").write_bytes(b'@router.get("/health")\n')
        check("仓根下的源文件扫得到",
              E._check_health(w, S("health", "GET /health")) == [])
    with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1])) as td:
        w = Path(td)          # 父目录是 prototype/，所在盘符路径本身可能含 .tmp
        (w / "main.py").write_bytes(b'@router.get("/health")\n')
        rel = w.relative_to(Path(__file__).resolve().parents[1])
        check("仓所在的父目录名不影响遍历（旧版会误判没注册）",
              E._check_health(w, S("health", "GET /health")) == [], str(rel))
    with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1] / ".tmp")) as td:
        w = Path(td)
        (w / ".tmp_junk").mkdir()
        (w / ".tmp_junk" / "x.py").write_bytes(b'@router.get("/health")\n')
        check("仓内 .tmp* 子目录仍然跳过（不许扫临时产物）",
              E._check_health(w, S("health", "GET /health")) != [])
    with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1] / ".tmp")) as td:
        w = Path(td)
        (w / "main.py").write_bytes('# 中文注释\n@router.get("/health")\n'.encode("gbk"))
        check("非 UTF-8（GBK）源文件也能读到端点，不因坏字节整文失配",
              E._check_health(w, S("health", "GET /health")) == [])

    print()
    if FAILS:
        print(f"失败 {len(FAILS)} 项 / 共 {TOTAL[0]} 条断言：{FAILS}")
        return 1
    print(f"全部通过（{TOTAL[0]} 条断言：解析/注入/五类硬判据/summary/接线闭环）")
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
