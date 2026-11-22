"""自检：整仓会话（采全仓 → 提示词 → 解析 → 守坠落盘）。

为什么必须逐条钉死（2026-11-15）：
"整仓写权限"意味着远端**能改任何文件**，守卫一旦漏一条，
一次会话就能把仓库改成半坏状态（语法错、文件被截断、越界路径）。
所以这里把每条守卫都做成**可执行的断言**，而不是"代码里有这个函数"。

跑法: python checks/check_wholerepo.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROTO = HERE.parent
sys.path.insert(0, str(PROTO))

from core import wholerepo as W                                  # noqa: E402
from core.django_wire import MARK_BEGIN as W_ENGINE_MARK         # noqa: E402

FAILS: list[str] = []


def ck(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print("  PASS ", name)
    else:
        print("  FAIL ", name, "|", str(detail)[:300])
        FAILS.append(name)


def make_ws(root: Path) -> Path:
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "apps" / "demo").mkdir(parents=True, exist_ok=True)
    (root / "apps" / "demo" / "views.py").write_text(
        "def hello(name: str) -> dict:\n    return {'hi': name}\n", encoding="utf-8")
    (root / "apps" / "demo" / "empty.py").write_text(
        "def todo() -> dict:\n    raise NotImplementedError\n", encoding="utf-8")
    (root / "config" / "settings.py").write_text("SECRET_KEY = 'x'\n", encoding="utf-8")
    (root / "web" / "index.html").parent.mkdir(parents=True, exist_ok=True)
    (root / "web" / "index.html").write_text("<html>x</html>\n", encoding="utf-8")
    # 必须被排除的
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "junk.py").write_text("x = 1\n", encoding="utf-8")
    (root / "apps" / "demo" / "migrations").mkdir(exist_ok=True)
    (root / "apps" / "demo" / "migrations" / "0001.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".sixcoder").mkdir(exist_ok=True)
    (root / ".sixcoder" / "ledger.json").write_text("{}", encoding="utf-8")
    return root


class _Rep:
    def __init__(self, text: str) -> None:
        self._t = text

    def render(self, limit_per_level: int = 40) -> str:
        return self._t

    def render_short(self) -> str:
        return "E=2（P0=1 P1=1 P2=0 P3=0）"


def main() -> int:
    print("[1] 采集口径：收产品源码、排除缓存/迁移/账本")
    with tempfile.TemporaryDirectory() as td:
        ws = make_ws(Path(td))
        files, stats = W.collect(ws)
        ck("收到 apps/config/web 的源码", {"apps/demo/views.py", "config/settings.py",
                                          "web/index.html"} <= set(files), sorted(files))
        ck("不收 __pycache__/migrations/.sixcoder",
           not any(x.startswith(("__pycache__/", ".sixcoder/"))
                   or "/migrations/" in x for x in files), sorted(files))
        ck("统计字符数 > 0", stats["chars"] > 0, stats)

        print()
        print("[2] 提示词：整仓权限 + 归因优先 + 输出契约 + 报告 + 历史根因")
        br = W.brief(ws, _Rep("错误报告：共 2 条（P0=1 P1=1）\n### E-001 [P0/syntax] x.py"),
                     files=files, stats=stats,
                     history=[{"round": 1, "text": "RC1: 入口配置写死 app.settings"}],
                     round_no=2, total_rounds=6)
        ck("点明整仓视角（看整个仓库诊断，每轮提交受限但不只看报告点到的文件）",
           "整个仓库" in br and "整仓" in br, br[:200])
        ck("要求先归因、禁止直接改代码", "禁止直接开始改代码" in br and "根因" in br)
        ck("要求症状自动消失的不单独改", "自动消失" in br)
        ck("输出契约含 FILE/DELETE/DONE 三种标记",
           "=== FILE:" in br and "=== DELETE:" in br and "=== DONE ===" in br)
        ck("把整个仓库源码塞进去了（不是只给几个文件）",
           "=== FILE: config/settings.py ===" in br
           and "=== FILE: apps/demo/views.py ===" in br, len(br))
        ck("带上错误报告全文", "E-001" in br)
        ck("带上历史根因（不重复分析）", "RC1: 入口配置写死 app.settings" in br)
        ck("不许改锁定契约/账本", ".sixcoder/full_contract.txt" in br)
        # 小步限制：提示词必须把"每轮最多 2 文件"写死（钉调用路径：真跑 brief 看输出）
        import re as _re
        _cap_line = _re.search(r"每轮最多提交\s*2\s*个", br)
        ck("提示词写死每轮最多 2 文件（不是鼓励跨 8 文件一次改完）",
           bool(_cap_line) and "@MAXFILES@" not in br,
           br[:400])
        ck("提示词明确超限会被拒收顺延（远端知道守这约束）",
           "拒收并顺延" in br and "只挑最关键" in br, br[:600])

        print()
        print("[3] 解析：整文件块 / 删除块 / 根因 / DONE")
        reply = """<rootcause>
RC1: 入口把 settings 包名写死成 app（解释了 E-001/E-002）→ 涉及 config/asgi.py
自动消失: E-002
</rootcause>
=== FILE: config/asgi.py ===
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
=== FILE: apps/demo/views.py ===
def hello(name: str) -> dict:
    return {'hi': name}


def extra() -> int:
    return = 1
=== DELETE: apps/demo/old.py ===
=== DONE ===
"""
        p = W.parse(reply)
        ck("解出 2 个文件", set(p["files"]) == {"config/asgi.py", "apps/demo/views.py"},
           sorted(p["files"]))
        ck("文件体是整文件（含空行与缩进）",
           "return {'hi': name}" in p["files"].get("apps/demo/views.py", ""))
        ck("解出删除项", p["deletes"] == ["apps/demo/old.py"], p["deletes"])
        ck("解出根因", "RC1" in p["rootcause"] and "自动消失" in p["rootcause"],
           p["rootcause"][:80])
        ck("认出 DONE", p["done"] is True)
        p2 = W.parse("```file:apps/demo/views.py\ndef a():\n    return 1\n```")
        ck("兜底认 ```file:path``` 围栏", "apps/demo/views.py" in p2["files"], p2["files"])

        print()
        print("[4] 落盘守卫")
        ok_reply = ("=== FILE: apps/demo/views.py ===\n"
                    "def hello(name: str) -> dict:\n"
                    "    return {'hi': name, 'v': 2}\n")
        r = W.apply(ws, W.parse(ok_reply))
        ck("正常写入成功", r["ok"] is True and r["written"] == ["apps/demo/views.py"], r)
        ck("磁盘上真的变了", "'v': 2" in (ws / "apps/demo/views.py").read_text(encoding="utf-8"))
        ck("写了快照（可回滚）", bool(r["backup"]) and Path(r["backup"]).is_dir(), r["backup"])

        bad_reply = ("=== FILE: apps/demo/views.py ===\n"
                     "def hello(name: str) -> dict\n"
                     "    return {}\n")
        r2 = W.apply(ws, W.parse(bad_reply))
        ck("语法坏的文件被拒收（单文件全坏 → 不写入）",
           r2["ok"] is False and not r2["written"], r2)
        ck("坏回复没污染磁盘",
           "'v': 2" in (ws / "apps/demo/views.py").read_text(encoding="utf-8"))
        # 一好一坏：好的必须照写（以前整批连坐，19 个好文件陪葬）
        mixed = ("=== FILE: apps/demo/views.py ===\n"
                 "def hello(name: str) -> dict\n"
                 "    return {}\n"
                 "=== FILE: apps/demo/good.py ===\n"
                 "def ok() -> int:\n"
                 "    return 7\n")
        r2b = W.apply(ws, W.parse(mixed))
        ck("一好一坏 → 坏文件拒收、好文件照写（不连坐）",
           r2b["ok"] is True and r2b["written"] == ["apps/demo/good.py"]
           and any("拒收" in x for x in r2b["skipped"]), r2b)
        ck("好文件真的落盘了", (ws / "apps/demo/good.py").is_file()
           and "return 7" in (ws / "apps/demo/good.py").read_text(encoding="utf-8"))

        # 截断体检：原文 2000 字符，新版只给 200 字符
        big = "def big():\n" + "".join("    x%d = %d\n" % (i, i) for i in range(120))
        (ws / "apps/demo" / "big.py").write_text(big, encoding="utf-8")
        (ws / "apps/demo" / "big2.py").write_text(big, encoding="utf-8")
        trunc = ("=== FILE: apps/demo/big2.py ===\ndef big():\n    return 1\n")
        r3 = W.apply(ws, W.parse(trunc))
        ck("截断文件被跳过（不是覆盖成半截）",
           not r3["written"] and any("截断" in s for s in r3["skipped"]), r3)

        print()
        print("[4b] 小步硬闸：一轮最多写 2 个文件（钉 apply 真实写盘路径）")
        # 交 3 个都合法的文件 → 只应落盘前 2 个，第 3 个进 rejected（下一轮回流）
        for nm in ("s1", "s2", "s3"):
            (ws / "apps" / "demo" / (nm + ".py")).write_text(
                "def f():\n    return 0\n", encoding="utf-8")
        three = ("=== FILE: apps/demo/s1.py ===\ndef f():\n    return 1\n"
                 "=== FILE: apps/demo/s2.py ===\ndef f():\n    return 2\n"
                 "=== FILE: apps/demo/s3.py ===\ndef f():\n    return 3\n")
        rc = W.apply(ws, W.parse(three))
        ck("交 3 个合法文件 → 只写 2 个（写盘层硬截断）",
           rc["ok"] is True and len(rc["written"]) == 2, rc)
        ck("超出的第 3 个进 rejected（回流下一轮，不静默丢）",
           any(x.get("path") == "apps/demo/s3.py" for x in rc.get("rejected", [])),
           rc.get("rejected"))
        ck("超限原因写明顺延（远端知道该优先补交）",
           any("顺延" in str(x.get("why")) or "超限" in str(x.get("why"))
               for x in rc.get("rejected", [])), rc.get("rejected"))
        ck("第 3 个文件磁盘上没被改（仍是 return 0）",
           "return 0" in (ws / "apps/demo/s3.py").read_text(encoding="utf-8"))
        # 写 2 + 删 1：删除也算名额，总共只 2 个落地
        for nm in ("d1", "d2"):
            (ws / "apps" / "demo" / (nm + ".py")).write_text(
                "def f():\n    return 0\n", encoding="utf-8")
        two_w_one_d = ("=== FILE: apps/demo/d1.py ===\ndef f():\n    return 1\n"
                       "=== FILE: apps/demo/d2.py ===\ndef f():\n    return 2\n"
                       "=== DELETE: apps/demo/d1.py ===\n")
        rc2 = W.apply(ws, W.parse(two_w_one_d))
        ck("写 2 + 删 1 → 删除也被截断不执行（名额=写+删合计）",
           len(rc2["written"]) == 2 and not rc2["deleted"], rc2)
        # cap=None 时不限制（保留给显式调试）
        _old_cap = W.MAX_FILES_PER_ROUND
        try:
            W.MAX_FILES_PER_ROUND = None
            for nm in ("n1", "n2", "n3"):
                (ws / "apps" / "demo" / (nm + ".py")).write_text(
                    "def f():\n    return 0\n", encoding="utf-8")
            unlim = ("=== FILE: apps/demo/n1.py ===\ndef f():\n    return 1\n"
                     "=== FILE: apps/demo/n2.py ===\ndef f():\n    return 2\n"
                     "=== FILE: apps/demo/n3.py ===\ndef f():\n    return 3\n")
            ru = W.apply(ws, W.parse(unlim))
            ck("cap=None 时不限制（3 个全写，调试开关有效）",
               ru["ok"] is True and len(ru["written"]) == 3, ru)
        finally:
            W.MAX_FILES_PER_ROUND = _old_cap

        print()
        print("[4c] 已绿能力供料：行为契约 + 命门文件点名（真跑 report_from_state）")
        import json as _json
        bp_dir = Path(tempfile.mkdtemp())
        try:
            (bp_dir / ".sixcoder").mkdir(exist_ok=True)
            (bp_dir / ".sixcoder" / "blueprint.json").write_text(_json.dumps({
                "caps": [
                    {"id": "cap.demo.forbidden", "desc": "学生越权发题必须 403",
                     "probe": {"kind": "http", "steps": [
                         {"method": "POST", "path": "/api/v1/auth/login/",
                          "expect_status": 200},
                         {"method": "POST", "path": "/api/v1/topics/",
                          "expect_status": 403,
                          "expect_json": {"detail": "权限不足"}}]},
                     "modules": ["apps/demo/views.py", "apps/demo/topics.py"],
                     "status": "done"},
                    {"id": "cap.demo.profile", "desc": "教师档案回显 quota",
                     "probe": {"kind": "http", "steps": [
                         {"method": "POST", "path": "/api/v1/people/teachers/",
                          "expect_status": 201,
                          "expect_json": {"quota": 3}}]},
                     "modules": ["apps/demo/views.py"], "status": "done"},
                ]}, ensure_ascii=False), encoding="utf-8")
            hint = W.cap_contract_hint(
                bp_dir, ["cap.demo.forbidden", "cap.demo.profile"])
            ck("契约含能力描述与依赖文件",
               "cap.demo.forbidden" in hint and "学生越权发题必须 403" in hint
               and "apps/demo/views.py" in hint, hint[:200])
            ck("契约含行为断言（方法/路径/状态码/字段）",
               "POST /api/v1/topics/ → 403" in hint and "detail=权限不足" in hint,
               hint[:300])
            ck("命门文件点名（≥2 条已绿能力依赖）",
               "命门文件" in hint and "`apps/demo/views.py`：承载 2 条" in hint,
               hint[:300])
            ck("只给行为契约，不泄露探针账号/密码",
               "u_cap" not in hint and "password" not in hint)
            rep = W.report_from_state(
                ws=bp_dir, passed=["cap.demo.forbidden", "cap.demo.profile"])
            ck("report_from_state 带 ws 时把契约写进报告",
               "必须保持：POST /api/v1/topics/ → 403" in rep.render(),
               rep.render()[:200])
            ck("读不到 blueprint 时退回 ID 清单（不炸、不误导）",
               "cap.demo.forbidden" in W.report_from_state(
                   ws=Path(tempfile.mkdtemp()),
                   passed=["cap.demo.forbidden"]).render())
        finally:
            import shutil as _sh
            _sh.rmtree(bp_dir, ignore_errors=True)

        print()
        print("[5] 复验失败 → 整批回滚（含删除也恢复）")
        (ws / "apps" / "demo" / "victim.py").write_text("def v():\n    return 1\n",
                                                        encoding="utf-8")
        orig = (ws / "apps/demo/views.py").read_text(encoding="utf-8")
        _orig_check = W._project_check
        W._project_check = lambda w: (False, "manage.py check 报错：ImportError")
        try:
            r4 = W.apply(ws, W.parse(ok_reply + "=== DELETE: apps/demo/victim.py ===\n"))
            rolled = ("'v': 2" in (ws / "apps/demo/views.py").read_text(encoding="utf-8")
                      and (ws / "apps/demo/victim.py").is_file())
            ck("复验失败时回滚并报出原因",
               r4["ok"] is False and "回滚" in r4["detail"], r4.get("detail"))
            ck("回滚把内容恢复原样（含被删的文件）", rolled)
        finally:
            W._project_check = _orig_check
        ck("原内容确实恢复", (ws / "apps/demo/views.py").read_text(encoding="utf-8") == orig)

        print()
        print("[6] 越界路径一律拒绝")
        evil = "=== FILE: ../evil.py ===\nx = 1\n"
        r5 = W.apply(ws, W.parse(evil))
        ck("不写仓库外的路径", not r5["written"] and not (Path(td).parent / "evil.py").is_file(),
           r5)
        evil2 = "=== FILE: .sixcoder/ledger.json ===\n{}\n"
        r6 = W.apply(ws, W.parse(evil2))
        ck("不写 .sixcoder（账本/契约受保护）", not r6["written"], r6)

        print()
        print("[6b] 引擎路由块不许被删（接线层会重渲染它）")
        urls = ws / "apps" / "demo" / "urls.py"
        urls.write_text(
            "from django.urls import path\nurlpatterns: list = []\n\n"
            + W_ENGINE_MARK + "\npath('x/', None)\n# --- 引擎路由结束 ---\n",
            encoding="utf-8")
        keep_block = ("=== FILE: apps/demo/urls.py ===\nfrom django.urls import path\n"
                      "urlpatterns: list = [\n]\n")
        r7 = W.apply(ws, W.parse(keep_block))
        ck("删掉引擎块 → 拒收该文件",
           not r7["written"] and any("引擎补的路由" in s for s in r7["skipped"]), r7)
        ck("磁盘上那个块还在", W_ENGINE_MARK in urls.read_text(encoding="utf-8"))

        print()
        print("[6c] 超预算：降级成签名（不是截断），架构不丢")
        big = "def bigfn(a, b):\n" + "".join("    x%d = %d\n" % (i, i) for i in range(300))
        for nm in ("m1", "m2", "m3", "m4", "m5", "m6"):
            (ws / "apps" / "demo" / (nm + ".py")).write_text(big, encoding="utf-8")
        (ws / "apps" / "demo" / "urls.py").write_text(
            "from django.urls import path\nurlpatterns = [\n]\n" + big,
            encoding="utf-8")
        f2, st2 = W.collect(ws, limit=4000)
        ck("大文件被降级成签名版", st2["signatures_only"], st2)
        ck("降级后的文件**没有半截代码**（无「引擎截断」字样）",
           not any("引擎截断" in v for v in f2.values()))
        ck("受保护文件（urls.py）不降级",
           "apps/demo/urls.py" not in st2["signatures_only"], st2["signatures_only"])
        ck("keep_full 里的文件不降级",
           W.collect(ws, limit=4000, keep_full=["apps/demo/m1.py"])[1]["signatures_only"]
           .count("apps/demo/m1.py") == 0)
        mp = W.map_text(ws, f2, st2)
        ck("架构地图含签名行", "def bigfn(a, b): ..." in mp, mp[:200])
        ck("架构地图标明哪些只发签名", "只发了签名" in mp)

        print()
        print("[6d] NEED 协议：点名的文件下一轮全量发送")
        nd = W.parse("=== NEED: matching_core/da.py ===\n=== NEED: apps/demo/m2.py ===\n=== DONE ===")
        ck("解析出 NEED 列表", nd["need"] == ["matching_core/da.py", "apps/demo/m2.py"], nd)
        W.save_need(ws, nd["need"])
        ck("落盘可读回", W.load_need(ws) == nd["need"], W.load_need(ws))
        W.save_need(ws, [])
        ck("清空后文件被删掉", not (ws / W.NEED_FILE).is_file())

        print()
        print("[6e] 红线：已通过的能力不许变红（回归要单独列出来）")
        rep2 = W.report_from_state(
            cap_cov={"caps": {"x": {"status": "absent"}}},
            passed=["cap.a", "cap.b"],
            prev_passed=["cap.a", "cap.b", "cap.broken_by_you"])
        t2 = rep2.render()
        ck("报告含「已通过（红线）」段", "红线" in t2 and "cap.a" in t2)
        ck("报告含「回归」段，且只列被弄红的", "回归" in t2 and "cap.broken_by_you" in t2
           and "- cap.a" not in t2.split("⑤")[-1], t2[-300:])
        W.save_passed(ws, ["cap.x", "cap.y"])
        ck("已通过清单可落盘读回", W.load_passed(ws) == ["cap.x", "cap.y"], W.load_passed(ws))

        print()
        print("[6f] 回归护栏：改完当场重测，红了整批回滚")
        # 造一个"已通过"的能力，落盘；再用桩 observe 让它变红
        W.save_passed(ws, ["cap.demo.ok"])
        calls = {"n": 0}

        class _Cap:
            def __init__(self, cid, mods):
                self.id, self.modules = cid, mods

        class _BP:
            caps = [_Cap("cap.demo.ok", ["apps/demo/views.py"])]

        class _BPmod:
            @staticmethod
            def load(_ws):
                return _BP()

        class _OH:
            @staticmethod
            def observe(_ws, _bp, emit=None, only=None):
                calls["n"] += 1
                return {"caps": {"cap.demo.ok": {"status": "absent"}}}   # 桩：弄红了

        import core.blueprint as _real_bp
        import core.openhouse as _real_oh
        old_bp, old_oh = _real_bp.load, _real_oh.observe
        _real_bp.load, _real_oh.observe = _BPmod.load, _OH.observe
        try:
            R = W.regression_check(ws, ["apps/demo/views.py"])
        finally:
            _real_bp.load, _real_oh.observe = old_bp, old_oh
        ck("护栏真的去测了（observe 被调用）", calls["n"] == 1, calls)
        ck("能识别出被弄红的能力", R["regressed"] == ["cap.demo.ok"], R)

        # 默认**全测已通过的**（2026-11-15 23:46 修正）：
        # 只测"落点重叠"会漏检 —— 实测检查 10 条全绿、覆盖照样从 31.8 掉到 28.2，
        # 因为掉的那几条 modules 字段跟改动没交集（共享层一改，影响面更大）。
        calls["n"] = 0
        _real_bp.load, _real_oh.observe = _BPmod.load, _OH.observe
        try:
            R2 = W.regression_check(ws, ["matching_core/da.py"])
        finally:
            _real_bp.load, _real_oh.observe = old_bp, old_oh
        ck("无关改动也会全测（宁可慢一点，不许漏检）",
           calls["n"] >= 1 and R2["checked"] == ["cap.demo.ok"], R2)

        # 回滚：改坏→恢复
        vp = ws / "apps" / "demo" / "views.py"
        before = vp.read_text(encoding="utf-8")
        (ws / ".sixcoder").mkdir(exist_ok=True)
        bak = ws / ".sixcoder" / "snap-test"
        (bak / "apps" / "demo").mkdir(parents=True, exist_ok=True)
        (bak / "apps" / "demo" / "views.py").write_text(before, encoding="utf-8")
        (bak / "_manifest.json").write_text('{"apps/demo/views.py": "existed"}',
                                            encoding="utf-8")
        vp.write_text(before + "\n# broken\n", encoding="utf-8")
        n = W.restore_from_backup(ws, bak)
        ck("按快照回滚能恢复文件", n == 1 and vp.read_text(encoding="utf-8") == before, n)

        print()
        print("[7] 历史根因落盘（下一轮不必重新分析）")
        hist = [{"round": 1, "text": "RC1: x"}]
        W.save_history(ws, hist)
        ck("历史可读回", W.load_history(ws)[0]["text"] == "RC1: x", W.load_history(ws))
        ck("历史文件在 .sixcoder/rootcause.json",
           (ws / W.ROOTCAUSE_FILE).is_file())

        print()
        print("[8] 回滚反馈独立回喂（2026-11-18：塞进拒收框被当噪声，5 轮死循环）")
        info = {"regressed": ["cap.rules.audit_overflow"],
                "files": ["apps/rules/providers/local.py"],
                "detail": "检查 55 条：1 条被弄红 cap.rules.audit_overflow",
                "round": 2}
        txt = W.render_rollback(info)
        ck("回滚段说清'整批回滚/白改'（不是'文件被拒收'）",
           "整批回滚" in txt and "白改" in txt, txt[:160])
        ck("回滚段点名弄红的能力 + 被回滚的文件",
           "cap.rules.audit_overflow" in txt and "apps/rules/providers/local.py" in txt)
        ck("回滚段明确不要重做（远端不再重复无效改动）",
           "不要重做" in txt and "查清" in txt)
        ck("**逐文件禁改硬约束**（远端在 rootcause 里自洽也不许交命门文件）",
           "禁止提交对以下文件" in txt and "不许交" in txt
           and "apps/rules/providers/local.py" in txt, txt[:300])
        ck("无反馈时渲染空串（不误导）", W.render_rollback(None) == "")
        W.save_rollback(ws, info)
        ck("回滚反馈落盘并可读回", W.load_rollback(ws) == info)
        # brief 真渲染回滚段，且与拒收框分开
        W.save_reject(ws, [{"path": "x.py", "why": "语法错"}])
        brief_txt = W.brief(ws, W.SimpleReport("x", "x"),
                            files={"apps/rules/providers/local.py": "# x\n"},
                            stats={"order": ["apps/rules/providers/local.py"]})
        # 用回滚段独有的标题串定位："整批回滚"在开头硬约束里也出现，不能当标记
        MARK_RB = "上一轮你的改动被**整批回滚**"
        i_rb = brief_txt.find(MARK_RB)
        i_rj = brief_txt.find("被**拒收**")
        ck("brief 里回滚段确实出现", i_rb >= 0, brief_txt[-600:])
        ck("回滚段与拒收框分开（回滚不在'被拒收'标题下）",
           i_rb >= 0 and i_rj >= 0 and i_rb > i_rj,
           "rb=%d rj=%d" % (i_rb, i_rj))
        W.save_rollback(ws, None)
        ck("清空后 brief 不再挂旧回滚",
           MARK_RB not in W.brief(ws, W.SimpleReport("x", "x"),
                                 files={"apps/rules/providers/local.py": "# x\n"},
                                 stats={"order": ["apps/rules/providers/local.py"]}))
        # 调用路径钉子：session 回滚时真写独立文件，不再塞 reject
        src = (PROTO / "core" / "wholerepo.py").read_text(encoding="utf-8")
        ck("session 回滚分支调 save_rollback（不是 save_reject 假路径）",
           "save_rollback(ws, {\"regressed\"" in src
           and "（整批，已回滚）" not in src)
        ck("brief 真调用 render_rollback(load_rollback(ws))",
           "render_rollback(load_rollback(ws))" in src)

        print("[9] 项目方诊断通道（2026-11-19：人工已定位的结论要能送进简报）")
        _adv_path = ws / W.ADVICE_FILE
        ck("没有 advice.md 时读回空串（不硬造段落）",
           W.load_advice(ws) == "" and W.render_advice("") == "")
        _adv_path.parent.mkdir(parents=True, exist_ok=True)
        _adv_path.write_text("## ① 键优先级写反\n`_TOPIC_ID_KEYS` 把 id 排在 code 前面。\n",
                             encoding="utf-8")
        ck("有 advice.md 时能读回正文",
           "键优先级写反" in W.load_advice(ws), W.load_advice(ws)[:80])
        _r = W.render_advice(W.load_advice(ws))
        ck("渲染出独立段落 + 明确'优先采信/要求反证'",
           "项目方诊断" in _r and "优先采信" in _r and "反证" in _r, _r[:200])
        _adv_path.write_text("x" * (W.ADVICE_MAX_CHARS + 500), encoding="utf-8")
        ck("超长诊断被截断到上限（不撑爆简报）",
           len(W.load_advice(ws)) == W.ADVICE_MAX_CHARS, str(len(W.load_advice(ws))))
        _adv_path.write_text("## 结论：id 应排在 code 之后\n", encoding="utf-8")
        _brief_adv = W.brief(ws, W.SimpleReport("x", "x"),
                             files={"apps/rules/providers/local.py": "# x\n"},
                             stats={"order": ["apps/rules/providers/local.py"]})
        ck("brief 里真的带上了诊断段落",
           "项目方诊断" in _brief_adv and "id 应排在 code 之后" in _brief_adv,
           _brief_adv[-400:])
        ck("诊断段落排在源码之后（不毁前缀缓存）",
           _brief_adv.find("=== FILE: apps/rules/providers/local.py ===")
           < _brief_adv.find("项目方诊断"), "顺序错了")
        ck("brief 真调用 render_advice(load_advice(ws))",
           "render_advice(load_advice(ws))" in (PROTO / "core" / "wholerepo.py")
           .read_text(encoding="utf-8"))

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("全部通过（采集口径 / 提示词契约 / 解析 / 四条落盘守卫 / 历史根因）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
