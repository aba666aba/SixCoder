"""生态包自检（2026-11-19 用户需求：协商补全前端 SPA/桌面/中间件等缺失能力）。

钉住的铁律：
  1. **AST 静态校验**：远端写的 adapter 会进引擎主进程，必须禁掉危险 import
     （os/subprocess/socket/shutil/importlib/pickle…）、禁 exec/eval/open、
     禁 write_text/unlink/rmtree 等写盘删除；同时**不能误杀合法字符串操作**
     （str.replace/remove 这类要保持可用 —— 2026-11-19 实测踩过）。
  2. **manifest 契约**：协商安装必须内联 files；stack 类必须有 adapter_class + adapter.py。
  3. **安装/回滚**：复验失败整包回滚，不留半装。
  4. **动态加载**：load_adapter 再校验一次后 import；能注册进 stack。
  5. **前端 SPA 包真存在**：templates 齐全、detect 识别、adapter 能力可用。
  6. **协商接线**：install_ecosystem 能被校验；3/3 全票才放行。
  本自检不跑 npm build（太慢/依赖网络）——真构建由独立冒烟验证过（vite build 通过）。
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import ecosystem as eco  # noqa: E402
from core import judgefix as jf    # noqa: E402
from core import deadlock as dl    # noqa: E402
from core import blueprint as bpmod  # noqa: E402

FAILS: list[str] = []
_TMP = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILS.append(name)


def _pack(name="t_eco", adapter_src=None, kind="stack", verify=None, files=None):
    if files is None:
        files = {"adapter.py": adapter_src or
                 "from core import stack\nclass T(stack.StackAdapter):\n    name = \"t\"\n"}
    return {"name": name, "kind": kind, "description": "自检包",
            "provides": ["stack:%s" % name], "needs": [],
            "adapter_class": "T", "files": files,
            "verify": verify or [sys.executable, "-c", "print(1)"]}


def main() -> int:
    print("[1] AST 静态校验：合法放行、危险拒绝")
    good = "from core import stack\nimport re, json\nclass A(stack.StackAdapter):\n    name = \"a\"\n"
    check("合法 adapter 放行", eco.ast_check(good)[0])
    # str.replace/remove 是合法字符串操作，不能被误杀（实测踩过）
    check("str.replace 不误杀", eco.ast_check(
        "s = 'a-b'.replace('-', '_')\n")[0])
    for bad, label in [
        ("import os\n", "import os"),
        ("import subprocess\n", "import subprocess"),
        ("import socket\n", "import socket"),
        ("import shutil\n", "import shutil"),
        ("import importlib\n", "import importlib"),
        ("import pickle\n", "import pickle"),
        ("exec('1')\n", "exec"),
        ("eval('1')\n", "eval"),
        ("open('f','w')\n", "open"),
        ("from pathlib import Path\nPath('x').write_text('y')\n", "write_text"),
        ("from pathlib import Path\nPath('x').unlink()\n", "unlink"),
        ("x.__class__\n", "__class__逃逸"),
    ]:
        check(f"拒绝 {label}", not eco.ast_check(bad)[0])
    check("语法错误也拒", not eco.ast_check("def f(:\n")[0])

    print("[2] manifest 契约")
    check("合法包通过（require_files）", eco.validate_manifest(_pack(), require_files=True)[0])
    check("缺 files 拒（协商安装）", not eco.validate_manifest(
        {**_pack(), "files": None}, require_files=True)[0])
    check("kind 非法拒", not eco.validate_manifest(
        {**_pack(), "kind": "weird"}, require_files=True)[0])
    check("stack 缺 adapter_class 拒", not eco.validate_manifest(
        {**_pack(), "adapter_class": ""}, require_files=True)[0])
    check("files 路径穿越拒", not eco.validate_manifest(
        {**_pack(), "files": {"../x.py": "y"}}, require_files=True)[0])
    check("adapter.py 含危险代码拒", not eco.validate_manifest(
        {**_pack(), "files": {"adapter.py": "import os\n"}}, require_files=True)[0])

    print("[3] 安装 / 动态加载 / 回滚")
    name = "t_ecoload"
    _TMP.append(name)
    r = eco.install(_pack(name))
    check("安装成功", r.get("ok"), str(r.get("errors")))
    check("目录已生成", eco.pack_dir(name).is_dir())
    inst, why = eco.load_adapter(name)
    check("load_adapter 拿到适配器", inst is not None, why)
    check("适配器 name 正确", getattr(inst, "name", "") == "t", str(getattr(inst, "name", "")))
    # 复验失败 → 回滚
    name2 = "t_ecobad"
    _TMP.append(name2)
    r2 = eco.install(_pack(name2, verify=[sys.executable, "-c", "import sys;sys.exit(9)"]))
    check("复验失败判 False", not r2.get("ok"))
    check("失败后目录不存在（回滚）", not eco.pack_dir(name2).is_dir())

    print("[4] 前端 SPA 包真实存在且可用")
    packs = [p["name"] for p in eco.list_packs()]
    check("frontend_spa 已登记", "frontend_spa" in packs, str(packs))
    fs_inst, fs_why = eco.load_adapter("frontend_spa")
    check("frontend_spa 适配器可加载", fs_inst is not None, fs_why)
    if fs_inst:
        check("前端不是 ASGI", fs_inst.asgi_app() is False)
        check("识别 JSX 为前端源码",
              fs_inst.is_app_source("export default function A(){return <div/>}") == "spa")
        check("不把 .py 认成前端",
              fs_inst.is_app_source("def f(): pass") == "")
        routes = fs_inst.parse_routes('<Route path="/a" element={<A/>}/>')
        check("解析出 JSX 路由", routes == ["/a"], str(routes))
        check("模块路径是 .jsx",
              fs_inst.module_for("about").endswith(".jsx"), fs_inst.module_for("about"))
    # templates 齐全
    tpl = eco.pack_dir("frontend_spa") / "templates"
    for rel in ("package.json", "vite.config.js", "index.html",
                "src/main.jsx", "src/App.jsx"):
        check(f"模板含 {rel}", (tpl / rel).is_file())
    # apply_templates + detect
    ws = Path(tempfile.mkdtemp())
    _TMP.append(ws)
    ar = eco.apply_templates("frontend_spa", ws)
    check("apply_templates 写出文件", ar.get("ok") and ar.get("wrote", 0) >= 5, str(ar))
    check("detect 识别为前端项目", eco.detect("frontend_spa", ws))
    check("骨架含 <div id=\"root\">",
          'id="root"' in (ws / "index.html").read_text(encoding="utf-8"))

    print("[4b] Electron 生态包（第二个包，验证机制可复制）")
    eco.load_all()          # 先注册进栈表（否则 from_workspace 拿不到 electron 适配器）
    check("electron 已登记", "electron" in packs, str(packs))
    el_inst, el_why = eco.load_adapter("electron")
    check("electron 适配器可加载", el_inst is not None, el_why)
    if el_inst:
        check("electron 不是 ASGI", el_inst.asgi_app() is False)
        check("electron 主进程入口是 .cjs",
              el_inst.entry_path.endswith(".cjs"), el_inst.entry_path)
        check("electron 识别主进程源码",
              el_inst.is_app_source("new BrowserWindow({})") == "electron-main")
        check("electron 渲染落点是 .jsx",
              el_inst.module_for("about").endswith(".jsx"))
    # detect 不混淆：Electron 项目 vs 前端项目
    from pathlib import Path as _P
    import tempfile as _t2
    _we = _P(_t2.mkdtemp(prefix="eco_el_"))
    _TMP.append(_we)
    (_we / "package.json").write_text("{}", encoding="utf-8")
    (_we / "electron").mkdir()
    (_we / "electron" / "main.cjs").write_text("x", encoding="utf-8")
    _wf = _P(_t2.mkdtemp(prefix="eco_fe_"))
    _TMP.append(_wf)
    (_wf / "package.json").write_text("{}", encoding="utf-8")
    (_wf / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    _stk2 = __import__("core.stack", fromlist=["detect"])
    check("Electron 项目判 electron", _stk2.detect(_we) == "electron",
          _stk2.detect(_we))
    check("前端项目判 frontend_spa（不被 electron 抢判）",
          _stk2.detect(_wf) == "frontend_spa", _stk2.detect(_wf))
    # 组件类栈判定不写死栈名（看 module_file_fmt 后缀）
    from core import fullgen as _fg2
    class _C2:
        def __init__(self):
            self.id = "c"
            self.status = "absent"
            self.modules = ["src/renderer/pages/H.jsx"]

    class _B2:
        def __init__(self):
            self.caps = [_C2()]
    check("electron 落点走组件分支", _fg2._layer_targets(
        _we, _B2(), "services", None) == ["src/renderer/pages/H.jsx"],
        str(_fg2._layer_targets(_we, _B2(), "services", None)))

    print("[4c] microservice 生态包（第三个包）+ 识别不误判")
    check("microservice 已登记", "microservice" in packs, str(packs))
    ms_inst, ms_why = eco.load_adapter("microservice")
    check("microservice 适配器可加载", ms_inst is not None, ms_why)
    if ms_inst:
        check("microservice 的 API 仍是 ASGI", ms_inst.asgi_app() is True)
        check("microservice 的 web_rules 含 sql 探针引导",
              "sql" in bpmod.web_rules("microservice"))
    # 识别不误判：Django + docker-compose 不能被判成 microservice
    from pathlib import Path as _P2
    import tempfile as _t3
    _wd = _P2(_t3.mkdtemp(prefix="eco_dj_"))
    _TMP.append(_wd)
    (_wd / "manage.py").write_text("x", encoding="utf-8")
    (_wd / "config").mkdir()
    (_wd / "config" / "settings.py").write_text("x", encoding="utf-8")
    (_wd / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    check("Django+docker-compose 仍判 django（不误判 microservice）",
          _stk2.detect(_wd) == "django", _stk2.detect(_wd))
    _wm = _P2(_t3.mkdtemp(prefix="eco_ms_"))
    _TMP.append(_wm)
    (_wm / "celery_app.py").write_text("x", encoding="utf-8")
    (_wm / "app").mkdir()
    (_wm / "app" / "main.py").write_text("x", encoding="utf-8")
    check("celery 项目判 microservice", _stk2.detect(_wm) == "microservice",
          _stk2.detect(_wm))
    _w3 = _P2(_t3.mkdtemp(prefix="eco_fa_"))
    _TMP.append(_w3)
    (_w3 / "app").mkdir()
    (_w3 / "app" / "main.py").write_text("x", encoding="utf-8")
    check("普通 FastAPI 仍判 fastapi（不被生态包抢）",
          _stk2.detect(_w3) == "fastapi", _stk2.detect(_w3))

    print("[4d] cli_tool / py_lib 生态包（非 Web 栈：cli / py_call 真探针）")
    for _n, _marker, _probe in (("cli_tool", "cli.py", "cli"),
                                ("py_lib", "lib/__init__.py", "py_call")):
        check(f"{_n} 已登记", _n in packs, str(packs))
        _inst, _why = eco.load_adapter(_n)
        check(f"{_n} 适配器可加载", _inst is not None, _why)
        if _inst:
            check(f"{_n} 非 ASGI（不走 http 探针）", _inst.asgi_app() is False)
            check(f"{_n} web_rules 引导 {_probe} 探针",
                  _probe in bpmod.web_rules(_n))
        _w = _P2(_t3.mkdtemp(prefix="eco_%s_" % _n))
        _TMP.append(_w)
        if "/" in _marker:
            _d = _w / _marker.split("/")[0]
            _d.mkdir()
            (_d / _marker.split("/")[1]).write_text("x", encoding="utf-8")
        else:
            (_w / _marker).write_text("x", encoding="utf-8")
        check(f"{_n} 项目被正确识别", _stk2.detect(_w) == _n, _stk2.detect(_w))
    _cli_only = _P2(_t3.mkdtemp(prefix="eco_cli_only_"))
    _TMP.append(_cli_only)
    (_cli_only / "cli.py").write_text("x", encoding="utf-8")
    (_cli_only / "pyproject.toml").write_text("[project]", encoding="utf-8")
    check("有 pyproject.toml 但无 lib/ → 仍判 cli_tool（marker 不共用抢判）",
          _stk2.detect(_cli_only) == "cli_tool", _stk2.detect(_cli_only))

    print("[4e] spring_boot / go_service / node_express 跨语言栈（cli.cmd 真探针）")
    for _n, _marker, _needle in (("spring_boot", "pom.xml", "mvn"),
                                 ("go_service", "go.mod", "go test"),
                                 ("node_express", "server.js", "npm")):
        check(f"{_n} 已登记", _n in packs, str(packs))
        _inst2, _why2 = eco.load_adapter(_n)
        check(f"{_n} 适配器可加载", _inst2 is not None, _why2)
        if _inst2:
            check(f"{_n} 非 ASGI（不走 http 探针）", _inst2.asgi_app() is False)
            check(f"{_n} web_rules 引导 cli.cmd 探针",
                  "cli.cmd" in bpmod.web_rules(_n), bpmod.web_rules(_n)[:80])
            check(f"{_n} web_rules 点名构建命令", _needle in bpmod.web_rules(_n))
        _w2 = _P2(_t3.mkdtemp(prefix="eco_%s_" % _n))
        _TMP.append(_w2)
        (_w2 / _marker).write_text("x", encoding="utf-8")
        check(f"{_n} 项目被正确识别", _stk2.detect(_w2) == _n, _stk2.detect(_w2))
    from core import probe as _pr2
    check("cmd 探针接受 mvn test",
          not _pr2.validate({"kind": "cli", "cmd": ["mvn", "-q", "test"],
                             "stdout_has": "BUILD SUCCESS"}))
    check("cmd 探针拒绝非白名单可执行",
          any("白名单" in x for x in _pr2.validate(
              {"kind": "cli", "cmd": ["rm", "-rf", "/"], "stdout_has": "x"})))
    check("cmd 探针拒绝只看退出码",
          any("stdout_has" in x for x in _pr2.validate(
              {"kind": "cli", "cmd": ["go", "test"]})))

    # 跨语言 cmd 探针**真跑**（本机有 node 时）：证明"识别 + 执行 + 断言"整条链通，
    # 而不只是结构自检。没有该工具链就如实跳过（不算通过）。
    import shutil as _sh
    if _sh.which("node"):
        _wn = _P2(_t3.mkdtemp(prefix="eco_node_run_"))
        _TMP.append(_wn)
        (_wn / "server.js").write_text("// node backend\n", encoding="utf-8")
        (_wn / "check.js").write_text("console.log('ok from node')\n", encoding="utf-8")
        _rn = _pr2.run(_wn, {"kind": "cli", "cmd": ["node", "check.js"],
                             "stdout_has": "ok from node"})
        check("node cmd 探针真跑通过", _rn.get("ok") is True,
              str(_rn.get("detail"))[:140])
        _rn2 = _pr2.run(_wn, {"kind": "cli", "cmd": ["node", "check.js"],
                              "stdout_has": "__NEVER__"})
        check("node cmd 探针断言不符判红", _rn2.get("ok") is False,
              str(_rn2.get("detail"))[:140])
    else:
        print("  SKIP  node 未安装，跨语言真跑跳过（不算通过）")

    print("[5] 协商接线：install_ecosystem")
    op = {"op": "install_ecosystem", "cap": "cap.x", "pack": _pack("t_wi")}
    check("valid_patch_op 放行合法生态包", jf.valid_patch_op(op)[0])
    check("valid_patch_op 拒绝危险 adapter", not jf.valid_patch_op(
        {"op": "install_ecosystem", "cap": "cap.x",
         "pack": _pack("t_bad", files={"adapter.py": "import os\n"})})[0])
    # 3/3 判定
    a, n = dl.decide_env_install("unlock", [[op], [op]], "install_ecosystem")
    check("三方一致 → 放行", a == ["t_wi"], str((a, n)))
    a, n = dl.decide_env_install("unlock", [[op], []], "install_ecosystem")
    check("仅一方提 → 不装", a == [], str((a, n)))
    a, n = dl.decide_env_install("keep", [[op], [op]], "install_ecosystem")
    check("提案人 keep → 不装", a == [], str((a, n)))

    # 清理自检包。**还要清备份/临时残留**（`_t_xxx.bak-*` / `.tmp-*`）：
    # 只删 pack_dir 会漏掉 install 覆盖时留下的 bak 目录（实测留下 2 个）。
    import shutil
    for n_ in _TMP:
        if isinstance(n_, str):
            shutil.rmtree(eco.pack_dir(n_), ignore_errors=True)
        else:
            shutil.rmtree(n_, ignore_errors=True)
    try:
        for _d in list(eco.lib_root().iterdir()):
            if _d.is_dir() and (_d.name.startswith("_t_")
                               or ".bak-" in _d.name or ".tmp-" in _d.name):
                shutil.rmtree(_d, ignore_errors=True)
    except OSError:
        pass

    print("[6] browser 探针（前端验收闭环）")
    from core import probe as _probe
    import inspect as _inspect
    check("PROBE_KINDS 含 browser", "browser" in _probe.PROBE_KINDS,
          str(_probe.PROBE_KINDS))
    # 缺 start_cmd / 无实质断言 → 拒收（空页面也能"能访问"，必须禁）
    check("缺 start_cmd 拒", bool(_probe.validate(
        {"kind": "browser", "url": "http://x/"})))
    check("只有 url 无断言拒", bool(_probe.validate(
        {"kind": "browser", "start_cmd": "npm run dev", "url": "http://x/"})))
    check("完整 browser 探针放行", not _probe.validate(
        {"kind": "browser", "start_cmd": "npm run dev", "url": "http://x/",
         "expect_dom": [{"selector": "#root"}]}))
    _run_src = _inspect.getsource(_probe.run)
    check("run() 分派含 browser 分支", 'kind == "browser"' in _run_src)
    check("_run_browser 存在且调 browser.visit",
          hasattr(_probe, "_run_browser")
          and "browser.visit" in _inspect.getsource(_probe._run_browser)
          or "_br.visit" in _inspect.getsource(_probe._run_browser))

    print("[6b] sql 探针（数据层真落库，零依赖 sqlite3）")
    check("PROBE_KINDS 含 sql", "sql" in _probe.PROBE_KINDS, str(_probe.PROBE_KINDS))
    check("sql 缺 db 拒", bool(_probe.validate({"kind": "sql", "steps": []})))
    check("sql 无判据拒", bool(_probe.validate(
        {"kind": "sql", "db": "a.db", "steps": [{"sql": "SELECT 1"}]})))
    check("sql 完整放行", not _probe.validate(
        {"kind": "sql", "db": "a.db",
         "steps": [{"sql": "SELECT 1", "expect_rows": 1}]}))
    import sqlite3 as _sq
    import tempfile as _t4
    _wq = Path(_t4.mkdtemp(prefix="eco_sql_"))
    _TMP.append(_wq)
    _conn = _sq.connect(str(_wq / "app.db"))
    _conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT)")
    _conn.executemany("INSERT INTO users(name) VALUES(?)", [("张三",), ("李四",)])
    _conn.commit()
    _conn.close()
    _r_ok = _probe.run(_wq, {"kind": "sql", "db": "app.db", "steps": [
        {"sql": "SELECT COUNT(*) AS n FROM users", "expect_first": {"n": 2}},
        {"sql": "SELECT name FROM users ORDER BY id", "expect_rows": 2,
         "expect_contains": {"name": "李四"}}]})
    check("sql 正向：查库断言通过", _r_ok.get("ok"), str(_r_ok.get("detail"))[:120])
    _r_bad = _probe.run(_wq, {"kind": "sql", "db": "app.db", "steps": [
        {"sql": "SELECT COUNT(*) AS n FROM users", "expect_first": {"n": 9}}]})
    check("sql 负向：值不符判红", not _r_bad.get("ok"),
          str(_r_bad.get("detail"))[:120])
    _r_drop = _probe.run(_wq, {"kind": "sql", "db": "app.db", "steps": [
        {"sql": "DROP TABLE users", "expect_rows": 0}]})
    check("sql 禁 DROP", not _r_drop.get("ok"), str(_r_drop.get("detail"))[:80])
    _r_nodb = _probe.run(_wq, {"kind": "sql", "db": "nope.db", "steps": [
        {"sql": "SELECT 1", "expect_rows": 1}]})
    check("sql 库不存在判红（不假装通过）", not _r_nodb.get("ok"))

    print("[7] 端到端链路：识别 → 契约规则 → 落点 → 骨架 → 文件路径放行")
    import tempfile as _tf
    from core import stack as _stk
    from core import blueprint as _bp
    from core import fullgen as _fg
    from core import author as _au
    # 必须先注册生态包（否则 detect 认得 frontend_spa，但 from_workspace 拿不到适配器）
    eco.load_all()
    _ws = Path(_tf.mkdtemp(prefix="eco_e2e_"))
    _TMP.append(_ws)
    (_ws / "package.json").write_text("{}", encoding="utf-8")
    (_ws / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
    check("识别前端项目为 frontend_spa", _stk.detect(_ws) == "frontend_spa",
          _stk.detect(_ws))
    check("前端栈的 web_rules 含 browser 探针",
          "browser" in _bp.web_rules("frontend_spa"))
    check("Django/FastAPI 的 web_rules 不含 browser（不串栈）",
          "browser" not in _bp.web_rules("django")
          and "browser" not in _bp.web_rules("fastapi"))

    class _Cap:
        def __init__(self, i, m):
            self.id = i
            self.status = "absent"
            self.modules = m

    class _BP:
        def __init__(self, caps):
            self.caps = caps

    _bpobj = _BP([_Cap("cap.ui.home", ["src/pages/Home.jsx", "src/components/Nav.jsx"])])
    _targets = _fg._layer_targets(_ws, _bpobj, "services", None)
    check("落点是 .jsx（按 modules，不写 Python 分层）",
          sorted(_targets) == ["src/components/Nav.jsx", "src/pages/Home.jsx"],
          str(_targets))
    _fg._ensure_module_files(_ws, _targets)
    _body = (_ws / "src/pages/Home.jsx").read_text(encoding="utf-8")
    check("前端骨架用 // 注释（不是 Python 的 \"\"\"）", _body.startswith("//"), _body[:20])
    check("author 收 .jsx/.tsx/.vue",
          all(_au._is_code_path(p) for p in
              ("src/a.jsx", "src/b.tsx", "src/c.vue")))
    check("author 仍拒 .html（防 HTML 当 Python 写）",
          not _au._is_code_path("index.html"))
    _files, _, _ = _au._parse_reply(
        '{"files":[{"path":"src/pages/H.jsx","content":"export default 1"}]}')
    check("整文件解析能接收 .jsx", len(_files) == 1 and _files[0]["path"].endswith(".jsx"),
          str(_files))
    # 提示词要引导前端写"整份组件"（不是 Python 函数体）
    check("AUTHOR_PROMPT 含前端写作引导", "前端组件" in _au.AUTHOR_PROMPT)
    # 模板必须能 format（加花括号若转义错会让整个 author 崩）
    import re as _re
    _names = set(_re.findall(r"\{([a-z_][a-z0-9_]*)\}", _au.AUTHOR_PROMPT))
    try:
        _au.AUTHOR_PROMPT.format(**{n: "X" for n in _names})
        check("AUTHOR_PROMPT.format 不崩（花括号转义正确）", True)
    except Exception as _e:                              # noqa: BLE001
        check("AUTHOR_PROMPT.format 不崩（花括号转义正确）", False, repr(_e))
    # 公司线：前端文件必须跳过 bodies（函数体模式）直接整文件
    _comp = (ROOT / "core" / "company.py").read_text(encoding="utf-8")
    check("company 前端文件跳过 bodies 走整文件",
          "_is_frontend_file" in _comp and '(".jsx", ".tsx", ".vue")' in _comp)

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} 项 —— " + "；".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
