# -*- coding: utf-8 -*-
"""成本归因 + 失败分诊的判据。

为什么这两块必须有判据（2026-11-15 用户定调）：
> 本地要干的是**同样质量下最少的钱**完成任务；本地工具提升生成质量的可信度。

- 归因错 → 报表看起来"每条能力很便宜"，其实是钱被摊到别处了 → 优化失去方向；
- 分诊错 → 要么白花钱（把本地能修的派给远端），要么漏派（该改的不改，永远红）。
两者都不能靠"看起来对"，必须拿**真实失败串**钉住。
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROTO))

from core import attribution as AT  # noqa: E402
from core import remote as RM  # noqa: E402
from core import triage as TR  # noqa: E402

FAILS = []
DB = PROTO / "data" / "proto.db"


def ck(name, ok, detail=""):
    print(("  PASS  " if ok else "  FAIL  ") + name
          + (("  — " + str(detail)[:260]) if detail and not ok else ""))
    if not ok:
        FAILS.append(name)


class _Cap:
    def __init__(self, cid, modules):
        self.id = cid
        self.modules = modules


class _BP:
    def __init__(self, caps):
        self.caps = caps


def main() -> int:
    print("[1] 归因上下文")
    AT.reset()
    ck("默认空（不猜）", AT.snapshot()["cap_id"] == "" and AT.snapshot()["phase"] == "")
    with AT.unit(module="apps/topics/views.py", phase="author_module", cap_ids=["c1", "c2"]):
        s = AT.snapshot()
        ck("单元内带 cap_id/module/phase",
           s["cap_id"] == "c1,c2" and s["module"].endswith("views.py")
           and s["phase"] == "author_module", s)
        with AT.unit(phase="inner"):
            ck("嵌套只改 phase，cap_id 继承", AT.snapshot()["cap_id"] == "c1,c2",
               AT.snapshot())
        ck("退出内层恢复 phase", AT.snapshot()["phase"] == "author_module")
    ck("退出外层清空", AT.snapshot()["cap_id"] == "")

    AT.set_blueprint(_BP([_Cap("cap.a", ["apps/topics/views.py"]),
                          _Cap("cap.b", ["apps/topics/views.py"]),
                          _Cap("cap.c", ["apps/other/views.py"])]))
    ck("模块→能力按蓝图机械映射",
       AT.caps_for_module("apps/topics/views.py") == ["cap.a", "cap.b"],
       AT.caps_for_module("apps/topics/views.py"))
    with AT.unit(module="apps/topics/views.py"):
        ck("只给 module 也能自动补 cap_id", AT.snapshot()["cap_id"] == "cap.a,cap.b")
    AT.reset()

    print()
    print("[2] 记账真的写进 cap_id / purpose")
    con = sqlite3.connect(str(DB))
    cols = [r[1] for r in con.execute("PRAGMA table_info(remote_usage)")]
    ck("remote_usage 有 cap_id 列", "cap_id" in cols, cols[-4:])
    con.close()

    # 必须用 with（或自己持有引用）：单独调 `.__enter__()` 而不持有对象，
    # 会被 GC 立刻回收 → finally 还原状态 → 记出来的 purpose 是空的
    # （2026-11-15 自检里就踩了一次，白排查一轮）。
    with AT.unit(phase="__selftest__", cap_ids=["cap.zz.test"]):
        RM._record(model="deepseek-flash", prompt_tokens=1000, completion_tokens=5,
                   cache_hit_tokens=900)
    AT.reset()
    RM._record(model="deepseek-flash", prompt_tokens=1000, completion_tokens=5,
               purpose="__selftest_nocap__")
    con = sqlite3.connect(str(DB))
    rows = list(con.execute(
        "SELECT purpose, cap_id FROM remote_usage ORDER BY id DESC LIMIT 2"))
    con.execute("DELETE FROM remote_usage WHERE purpose LIKE '__selftest%'")
    con.execute("DELETE FROM remote_usage WHERE purpose='__selftest__'")
    con.commit()
    con.close()
    ck("带上下文时写入 cap_id", any(r[1] == "cap.zz.test" for r in rows), rows)
    ck("上下文 phase 成为 purpose", any(r[0] == "__selftest__" for r in rows), rows)
    ck("无上下文写空串（不猜）", any(r[1] == "" for r in rows), rows)

    print()
    print("[3] 分诊：拿真实失败串判")
    cases = [
        ("第2步 GET /api/v1/people/teachers/me 期望 200，实得 404 Not Found",
         "local_wiring", "rewire"),
        ('HTTP 422 {"detail":"invalid_request"} 请求体缺 title', "local_wiring", "rewire"),
        ("NameError: name 'writer' is not defined", "local_wiring", "fix_imports"),
        ('第3步 JSON 字段不符：source（实际 {"source": None}，期望 manual）',
         "remote_code", "dispatch"),
        ("Connection refused 127.0.0.1:8760", "env", "report_env"),
        ("完全看不懂的一句话", "unknown", "dispatch"),
    ]
    for text, want_cls, want_act in cases:
        cls, act, why = TR.classify(text)
        ck("「%s…」→ %s" % (text[:16], want_cls), cls == want_cls and act == want_act,
           "%s/%s（%s）" % (cls, act, why))

    print()
    print("[4] 模块级合并：混合证据取最该远端修的")
    v = TR.classify_module(["404 路由未命中", 'JSON 字段不符 source 实际 None'])
    ck("404 + 字段不符 → remote_code（保守派单）", v["class"] == "remote_code", v)
    v2 = TR.classify_module(["404 路由未命中", "422 参数校验"])
    ck("全本地可修 → local_wiring", v2["class"] == "local_wiring", v2)
    ck("空证据 → unknown", TR.classify_module([])["class"] == "unknown")

    print()
    print("[5] 落盘与汇总")
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".sixcoder").mkdir()
        r = TR.triage_round(
            ws,
            {"apps/a/views.py", "apps/b/views.py", "apps/c/views.py"},
            {"apps/a/views.py": ["404 未命中"],
             "apps/b/views.py": ['JSON 字段不符 实际 {"x": None}'],
             "apps/c/views.py": ["Connection refused"]},
            caps_of=lambda m: ["cap." + Path(m).parent.name])
        f = ws / ".sixcoder" / "triage.jsonl"
        ck("triage.jsonl 写出来了", f.is_file())
        lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x]
        ck("三行记录", len(lines) == 3, len(lines))
        ck("带 cap_id", all(x["cap_id"].startswith("cap.") for x in lines), lines[:1])
        ck("分组正确（1 派单 / 1 本地 / 1 环境）",
           len(r["dispatch"]) == 1 and len(r["local"]) == 1 and len(r["env"]) == 1, r)
        ck("describe 可读", "红灯 3 个" in TR.describe(r), TR.describe(r))

    print()
    print("[6] 接线：钩子真的挂在调用路径上")
    co = (PROTO / "core" / "company.py").read_text(encoding="utf-8")
    ck("红灯后做了分诊", "triage_round(" in co)
    ck("派单有分诊闸门（只派 dispatch）",
       "_allow = set(triage.get(\"dispatch\")" in co, "没闸门 = 废单照旧")
    # 2026-11-19：派单过滤逻辑已从内联循环抽到纯函数 _cap_orders_from_red。
    # 旧断言钉 `_allow is not None and _m not in _allow`（内联变量名），
    # 重构后写做 `allow is not None and m not in allow` → 文本假红。
    # 钉两处真实语义：调用点把 _allow 传进去 + 纯函数里确实按 allow 过滤。
    ck("派单调用点把分诊闸门传进纯函数",
       "_cap_orders_from_red(" in co and "_allow)" in co)
    _cof = (PROTO / "core" / "company.py").read_text(encoding="utf-8")
    _co_fn = _cof.split("def _cap_orders_from_red(")[1].split("\ndef ")[0]
    ck("纯函数里跳过非 dispatch 的模块",
       "allow is not None and m not in allow" in _co_fn)
    ck("bp 加载后登记归因映射", "set_blueprint(bp)" in co)
    au = (PROTO / "core" / "author.py").read_text(encoding="utf-8")
    ck("写码入口被归因包裹",
       "author_module = _attributed(author_module)" in au
       and "author_bodies = _attributed(author_bodies)" in au)

    print()
    print("[7] 基线报表能跑")
    out = subprocess.run([sys.executable, "tools/cost_baseline.py", "--repo",
                          r"F:\WorkSpace\biyeshejishuangxuan", "--top", "3"],
                         cwd=str(PROTO), capture_output=True, text=True, timeout=180,
                         encoding="utf-8", errors="replace")
    txt = (out.stdout or "") + (out.stderr or "")
    ck("报表跑通", out.returncode == 0, txt[-400:])
    ck("报总花费", "总花费" in txt and "¥" in txt, txt[:200])
    ck("未归因单列（不摊进平均）", "未归因花费" in txt)
    ck("报缓存命中率", "缓存命中率" in txt)
    ck("报废单估算", "废单估算" in txt)

    print()
    if FAILS:
        print("FAILED: %d 项 —— %s" % (len(FAILS), "；".join(FAILS)))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
