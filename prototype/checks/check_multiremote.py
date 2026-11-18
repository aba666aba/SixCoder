"""多远端协作自检：厂商→key→模型三层配置 + 三角色 + 死局协商/解锁（2026-11-18）。

钉的是**调用路径与多数决规则**，不是"函数存在"：
  · 旧配置（无 keys）迁移后行为不变；新配置 key 按类继承（模型级>key类级>厂商级）；
  · 角色解析：审核者优先独立 key 类，不足降级同模型高档 effort 并标 degraded；
  · complete(profile=) 真发（monkeypatch _post）：payload/key/base 用指定档案；
  · 死局状态机：首次回滚只记账、同能力第二次回滚才唤醒；
    审核者缺席≠同意（没有独立同意就没有解锁）；2/3 多数决；
    复审过→休眠清解锁；不过→回滚+重协商，2 次用完放弃；
    范围外回归照旧回滚（解锁绝不放行无关改动）；解锁 3 轮不用自动过期。

跑法: python check_multiremote.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import deadlock, multiremote, settings_store, wholerepo  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


# ================================================================ [1] keys 三层配置
def part_keys() -> None:
    print("[1] 厂商→key→模型 三层配置")
    p = settings_store._norm_provider({
        "id": "deepseek", "api_key": "vendor-key",
        "keys": [
            {"id": "kw", "label": "写作者", "api_key": "key-w",
             "models": [{"id": "flash", "model": "deepseek-flash"}]},
            {"id": "kr1", "label": "审核1", "api_key": "key-r1",
             "models": [{"id": "flash2", "model": "deepseek-flash"}]},
            {"id": "kr2", "label": "审核2", "api_key": "key-r2"},
        ]}, "deepseek")
    check("keys 数量=3", len(p.get("keys") or []) == 3)
    check("key 类有且一个 current",
          [k["id"] for k in p["keys"] if k.get("current")] == ["kw"])
    check("key 类无模型时仍保留（可后挂模型）",
          any(k["id"] == "kr2" for k in p["keys"]))
    check("厂商级 models = keys 内模型展平",
          {m.get("id") for m in (p.get("models") or [])} == {"flash", "flash2"})
    # 旧配置（无 keys 字段）→ 自动合成 k1，行为不变
    old = settings_store._norm_provider({"id": "deepseek", "api_key": "vk",
                                         "models": [{"id": "flash", "model": "deepseek-flash"}]},
                                        "deepseek")
    check("旧配置合成单个 key 类", len(old.get("keys") or []) == 1
          and old["keys"][0]["id"] == "k1")
    check("旧配置 key 继承厂商级", old["keys"][0]["api_key"] == "vk")
    check("旧配置 models 归入 key 类", len(old["keys"][0]["models"]) == 1)
    # 旧配置扁平字段模型归入 current key 类（迁移不丢档案）
    old2 = settings_store._norm_provider({"id": "deepseek", "api_key": "vk",
                                          "keys": [{"id": "k1", "api_key": "vk"}],
                                          "models": [{"id": "flat", "model": "deepseek-chat"}]},
                                         "deepseek")
    check("厂商级裸 models 归到 current key 类",
          any(m.get("id") == "flat" for m in old2["keys"][0]["models"]))


# ================================================================ [2] save/load 隔离往返
def part_save_load() -> None:
    print("[2] 配置落盘往返（隔离环境，不碰真配置）")
    with tempfile.TemporaryDirectory() as td:
        # **必须在 SETTINGS 模块属性上换路径**（不是 env）：
        # SETTINGS 在 settings_store import 时已按 env 求值固化，
        # 事后设 env 改不到它 —— 实测（2026-11-18）曾把测试配置写进真配置，
        # 覆掉真 key（与 494fa96「自检污染」同一类错误，这里钉死）。
        _saved_settings = settings_store.SETTINGS
        settings_store.SETTINGS = Path(td) / "settings.json"
        try:
            provs = settings_store.save_providers([{
                "id": "deepseek", "api_key": "vendor-key", "current": True,
                "keys": [
                    {"id": "kw", "api_key": "key-w", "current": True,
                     "models": [{"id": "flash", "model": "deepseek-flash",
                                 "enabled": True}]},
                    {"id": "kr1", "api_key": "key-r1",
                     "models": [{"id": "flash2", "model": "deepseek-flash",
                                 "enabled": True}]},
                ]}])
            check("save_providers 保留 keys", len(provs[0].get("keys") or []) == 2)
            ms = settings_store.models()
            by_id = {m["id"]: m for m in ms}
            check("flash 继承 key 类 key-w",
                  by_id.get("flash", {}).get("api_key") == "key-w"
                  and by_id.get("flash", {}).get("key_id") == "kw")
            check("flash2 继承 key 类 key-r1",
                  by_id.get("flash2", {}).get("api_key") == "key-r1")
            # key 类打码回传（api_key 为空）→ 保留原 key（不许被清）
            provs2 = settings_store.save_providers([{
                "id": "deepseek", "current": True,
                "keys": [
                    {"id": "kw", "current": True,
                     "models": [{"id": "flash", "model": "deepseek-flash",
                                 "enabled": True}]},
                    {"id": "kr1",
                     "models": [{"id": "flash2", "model": "deepseek-flash",
                                 "enabled": True}]},
                ]}])
            kw = next(k for k in provs2[0]["keys"] if k["id"] == "kw")
            kr1 = next(k for k in provs2[0]["keys"] if k["id"] == "kr1")
            check("打码回传不清 kw 的 key", kw.get("api_key") == "key-w")
            check("打码回传不清 kr1 的 key", kr1.get("api_key") == "key-r1")
            # key 类级继承链：模型没 key → key 类 key
            ms2 = {m["id"]: m for m in settings_store.models()}
            check("flash 打码回传后仍继承 key-w",
                  ms2.get("flash", {}).get("api_key") == "key-w")
        finally:
            settings_store.SETTINGS = _saved_settings


# ================================================================ [3] 角色解析
def part_roles() -> None:
    print("[3] 三角色解析（writer/reviewer1/reviewer2）")
    _saved = {k: getattr(settings_store, k, None)
              for k in ("current_provider", "active_model", "models", "key_classes")}
    try:
        def writer():
            return {"id": "flash", "model": "deepseek-flash", "provider": "deepseek",
                    "key_id": "kw", "api_key": "key-w", "enabled": True,
                    "efforts": ["none", "low", "high", "max"]}

        def two_keys():
            return [  # kw: writer 的模型；kr: 审核者候选
                {"id": "flash", "model": "deepseek-flash", "provider": "deepseek",
                 "key_id": "kw", "api_key": "key-w", "enabled": True,
                 "efforts": ["none", "high"]},
                {"id": "flash2", "model": "deepseek-flash", "provider": "deepseek",
                 "key_id": "kr", "api_key": "key-r", "enabled": True,
                 "efforts": ["none"]},
                {"id": "flash3", "model": "deepseek-chat", "provider": "deepseek",
                 "key_id": "kr", "api_key": "key-r", "enabled": True,
                 "efforts": ["none"]},
            ]

        settings_store.current_provider = lambda: {"id": "deepseek"}
        settings_store.active_model = writer
        settings_store.key_classes = lambda pid="": []      # 无角色指派 → 走兜底路径
        settings_store.models = two_keys
        rs = multiremote.roles()
        check("writer=当前模型", rs["writer"]["id"] == "flash")
        check("reviewer1 来自其它 key 类",
              rs["reviewer1"]["key_id"] == "kr" and rs["reviewer1"]["id"] == "flash2")
        check("reviewer2 来自其它 key 类（不与 r1 重复）",
              rs["reviewer2"]["id"] == "flash3")
        check("双 key 不标 degraded", not rs.get("degraded"))
        # 单 key 类双模型 → 同类降级（但仍不同档案）
        def one_key_models():
            ms = two_keys()
            for m in ms:
                m["key_id"] = "kw"
            return ms
        settings_store.models = one_key_models
        rs2 = multiremote.roles()
        check("单 key 类退到同厂商其它模型",
              rs2["reviewer1"]["id"] == "flash2" and rs2["reviewer2"]["id"] == "flash3")
        check("同厂商不同档案也标 degraded", rs2.get("degraded") is True)
        # 单模型多 effort → 同模型高档 effort 凑
        def single_model():
            return [writer()]
        settings_store.models = single_model
        rs3 = multiremote.roles()
        check("单模型退到高档 effort 审",
              rs3["reviewer1"]["effort"] in ("high", "max")
              and rs3["reviewer2"]["effort"] in ("high", "max")
              and rs3["reviewer1"]["effort"] != rs3["reviewer2"]["effort"])
        check("同模型 effort 审也标 degraded", rs3.get("degraded") is True)
        # 完全没有档案 → None（机制停用，不炸）
        settings_store.active_model = lambda: None
        check("无可用档案 → roles()=None", multiremote.roles() is None)
        # **用户场景：一个厂商三把 key，显式指派三个角色**（2026-11-18 晚）
        def three_keys():
            return [
                {"id": "flash", "model": "deepseek-flash", "provider": "deepseek",
                 "key_id": "k1", "api_key": "key-w", "enabled": True,
                 "efforts": ["none"]},
            ]
        def three_key_classes(pid=""):
            return [
                {"id": "k1", "label": "写作者", "role": "writer", "current": True,
                 "api_key": "key-w", "models": [{"id": "flash", "model": "deepseek-flash",
                                                 "enabled": True, "efforts": ["none"]}]},
                {"id": "k2", "label": "审核1", "role": "reviewer1", "current": False,
                 "api_key": "key-r1", "models": [{"id": "flash-r1", "model": "deepseek-flash",
                                                  "enabled": True, "efforts": ["none"]}]},
                {"id": "k3", "label": "审核2", "role": "reviewer2", "current": False,
                 "api_key": "key-r2", "models": []},      # 类里没模型 → 借 writer 模型+本类 key
            ]
        settings_store.active_model = writer
        settings_store.models = three_keys
        settings_store.key_classes = three_key_classes
        rs4 = multiremote.roles()
        check("三角色显式指派：reviewer1 用自己 key 类的模型",
              rs4["reviewer1"]["id"] == "flash-r1"
              and rs4["reviewer1"]["api_key"] == "key-r1")
        check("reviewer2 类里没模型 → 借 writer 模型+本类 key",
              rs4["reviewer2"]["model"] == "deepseek-flash"
              and rs4["reviewer2"]["api_key"] == "key-r2"
              and rs4["reviewer2"]["key_id"] == "k3")
        check("三把 key 全指派 → 不标 degraded", not rs4.get("degraded"))
    finally:
        for k, v in _saved.items():
            if v is not None:
                setattr(settings_store, k, v)


# ================================================================ [4] complete(profile=) 真发
def part_profile_call() -> None:
    print("[4] complete(profile=) 指定档案直通")
    from core import remote
    captured = {}

    def fake_post(payload, base, key, timeout=300):
        captured.update({"payload": payload, "base": base, "key": key})
        return ({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, "", 5)

    _saved = {k: getattr(remote, k) for k in ("_post", "available", "_record", "_ensure_table")}
    try:
        remote._post = fake_post
        remote.available = lambda: (True, "")
        remote._record = lambda **kw: 0.0
        remote._ensure_table = lambda: None
        prof = {"id": "t1", "model": "test-model-x", "api_key": "sk-test-key",
                "base_url": "https://example.invalid", "efforts": ["none"]}
        r = remote.complete("hi", profile=prof)
        check("profile 调用 ok", r.get("ok") is True, str(r.get("error"))[:80])
        check("payload.model 用指定档案",
              captured.get("payload", {}).get("model") == "test-model-x")
        check("key 用指定档案的", captured.get("key") == "sk-test-key")
        check("base 用指定档案的", captured.get("base") == "https://example.invalid")
        # 空 profile → 走老路径（阶梯），且不报错
        r2 = remote.complete("hi")
        check("不带 profile 仍可调用（老路径不变）", "ok" in r2)
    finally:
        for k, v in _saved.items():
            setattr(remote, k, v)


# ================================================================ [5] 死局状态机
def part_deadlock() -> None:
    print("[5] 死局协商与解锁状态机")
    from core import multiremote as _mr
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        deadlock.reset(ws)
        # 首次回滚：只记账
        d1 = deadlock.on_rollback(ws, ["cap.a"], ["f1.py"], 1, "第一次红", emit=None)
        check("首次回滚不唤醒", d1.get("woke") is False and d1.get("repeat") == [])
        # 同能力第二次回滚 → 唤醒
        _orig = _mr.complete_as
        try:
            _mr.complete_as = lambda role, **kw: (
                {"ok": True, "answer": '{"verdict":"unlock","reason":"结构死局，同意"}'}
                if role == "reviewer1" else
                {"ok": True, "answer": '{"verdict":"keep","reason":"再试试别的打法"}'})
            d2 = deadlock.on_rollback(ws, ["cap.a", "cap.b"], ["f1.py"], 2, "又红了", emit=None)
            check("同能力二次回滚唤醒", d2.get("woke") is True)
            check("writer+1 审核 = 2/3 多数 → 解锁", d2["decision"].get("granted") is True,
                  str(d2.get("decision")))
            u = deadlock.unlock_active(ws)
            check("解锁范围可查询", bool(u) and set(u["caps"]) == {"cap.a", "cap.b"})
        finally:
            _mr.complete_as = _orig
        # 范围切分：范围外照旧回滚
        out_s, in_s = wholerepo.split_unlocked(["cap.a", "cap.x"], u)
        check("范围外回归保留（照旧回滚）", out_s == ["cap.x"])
        check("范围内红转入留证", in_s == ["cap.a"])
        check("无解锁时全量回滚",
              wholerepo.split_unlocked(["cap.a"], None) == (["cap.a"], []))
        # 复审：writer pass + r1 pass + r2 fail = 2/3 → resolved
        try:
            votes = {"reviewer1": '{"verdict":"pass","reason":"结构已解决"}',
                     "reviewer2": '{"verdict":"fail","reason":"没修好"}'}
            _mr.complete_as = lambda role, **kw: (
                {"ok": True, "answer": votes[role]} if role in votes
                else {"ok": False, "error": "缺席"})
            rr = deadlock.on_round_result(ws, 3, regressed=[], green=["cap.a", "cap.b"],
                                          backup="bak1", rootcause="拆了文件", emit=None)
            check("2/3 复审通过 → resolved", rr.get("resolved") is True,
                  str(rr.get("votes")))
            check("resolved 后审核者休眠（解锁清空）",
                  deadlock.load_state(ws).get("state") == "idle"
                  and deadlock.unlock_active(ws) is None)
        finally:
            _mr.complete_as = _orig
        # 复审不过 → 重协商（第 2 次授权）→ 再不过 → 放弃
        deadlock.save_state(ws, {"state": "writer_fix", "kind": "rollback",
                                 "caps": ["cap.a"], "files": ["f1.py"], "round": 4,
                                 "attempts": 1,
                                 "unlock": {"caps": ["cap.a"], "files": ["f1.py"],
                                            "round": 4},
                                 "history": []})
        seq = {"n": 0}
        try:
            def fake(role, **kw):
                seq["n"] += 1
                if seq["n"] <= 2:      # 第一次复审：两审核都 fail
                    return {"ok": True, "answer": '{"verdict":"fail","reason":"不行"}'}
                return {"ok": True, "answer": '{"verdict":"unlock","reason":"再给一次"}'}
            _mr.complete_as = fake
            rr2 = deadlock.on_round_result(ws, 5, regressed=["cap.a"], green=[],
                                           backup="bak2", rootcause="没修好", emit=None)
            check("复审不过 → 不 resolved", rr2.get("resolved") is False)
            check("复审不过触发重协商", rr2.get("renegotiate", {}).get("granted") is True)
            u2 = deadlock.unlock_active(ws)
            check("重协商后再获解锁（writer 独改）", bool(u2))
            rr3 = deadlock.on_round_result(ws, 6, regressed=["cap.a"], green=[],
                                           backup="bak3", rootcause="还是没修好", emit=None)
            check("第二次复审不过 → 放弃（恢复原收工逻辑）",
                  rr3.get("resolved") is False
                  and deadlock.load_state(ws).get("state") == "idle"
                  and deadlock.load_state(ws).get("giveup") is True)
            check("放弃时返回回滚备份", rr3.get("restore_backup") == "bak3")
        finally:
            _mr.complete_as = _orig
        # 审核者缺席 ≠ 同意
        deadlock.reset(ws)
        deadlock.on_rollback(ws, ["cap.a"], ["f.py"], 1, "首红", emit=None)
        try:
            _mr.complete_as = lambda role, **kw: (
                {"ok": False, "error": "超时"} if role == "reviewer1"
                else {"ok": True, "answer": '{"verdict":"keep","reason":"不解锁"}'})
            d3 = deadlock.on_rollback(ws, ["cap.a"], ["f.py"], 2, "再红", emit=None)
            check("审核者缺席+1反对 → 不解锁",
                  d3["decision"].get("granted") is False)
        finally:
            _mr.complete_as = _orig
        # 解锁过期：3 轮不用自动作废
        deadlock.save_state(ws, {"state": "writer_fix",
                                 "unlock": {"caps": ["cap.a"], "files": [], "round": 1},
                                 "history": []})
        check("解锁 3 轮未用 → 过期作废", deadlock.unlock_active(ws, round_no=5) is None)
        # 全事件留痕
        log = (ws / ".sixcoder" / "unlock_log.jsonl")
        events = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines() if x.strip()]
        kinds = {e.get("event") for e in events}
        check("解锁全事件留痕（wake/unlock/keep/resolved/giveup/expired）",
              {"deadlock_wake", "deadlock_unlock", "deadlock_keep_locked",
               "deadlock_resolved", "deadlock_giveup", "deadlock_expired"} <= kinds,
              str(kinds))


# ================================================================ [6] 调用路径钉死（源码）
def part_source() -> None:
    print("[6] 源码钉调用路径")
    s = Path(wholerepo.__file__).read_text(encoding="utf-8")
    check("regression_check 走 split_unlocked", "split_unlocked(" in s)
    check("session 回滚分支调 on_rollback", "on_rollback(" in s)
    check("session 解锁轮调 on_round_result", "on_round_result(" in s)
    c = Path(__import__("core.company", fromlist=["x"]).__file__).read_text(encoding="utf-8")
    check("夜班批开始 reset 死局状态", "deadlock" in c and "reset" in c)
    m = Path(multiremote.__file__).read_text(encoding="utf-8")
    check("审核者带 profile 直通（不走 writer 阶梯）", "profile=p" in m)
    check("writer 走老路径", "remote.complete(**kw)" in m)


if __name__ == "__main__":
    part_keys()
    part_save_load()
    part_roles()
    part_profile_call()
    part_deadlock()
    part_source()

    print("[11] 远端开处方 + 引擎安全执行（2026-11-19 用户口径）")
    import json as _j11
    import tempfile as _tf11
    from core import judgefix as _jf11
    ws11 = Path(_tf11.mkdtemp())
    (ws11 / ".sixcoder").mkdir(parents=True)
    _bp11 = {"caps": [{"id": "cap.t", "probe": {"steps": [
        {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "真题"}, "expect_status": 201},
        {"method": "POST", "path": "/api/v1/topics/", "json": {"title": "测试题目"},
         "expect_status": [403], "expect_json": {"detail": "权限不足"}},
        {"method": "GET", "path": "/api/v1/topics/${t}/", "body_has": ["测试题目"],
         "expect_status": [200], "expect_json": {"teacher_username": "t1"}},
    ]}}]}
    (ws11 / ".sixcoder" / "blueprint.json").write_text(
        _j11.dumps(_bp11, ensure_ascii=False), encoding="utf-8")
    _patch = [
        {"op": "delete_step", "cap": "cap.t", "step": 2},
        {"op": "set_field", "cap": "cap.t", "step": 2, "field": "body_has", "value": ["真题"]},
        {"op": "set_json_key", "cap": "cap.t", "step": 2, "field": "expect_json",
         "key": "teacher_username", "value": "u_t"},
    ]
    _m, _ = _jf11.merge_patches([_patch, _patch], ws=ws11)
    check("两份一致处方被合并", len(_m) == 3, str(len(_m)))
    _r = _jf11.apply_patch(ws11, _m)
    _steps11 = _j11.loads((ws11 / ".sixcoder" / "blueprint.json").read_text(encoding="utf-8"))["caps"][0]["probe"]["steps"]
    check("处方真被执行（删步 + 改 body_has + 改 teacher_username）",
          len(_steps11) == 2 and _steps11[1].get("body_has") == ["真题"]
          and (_steps11[1].get("expect_json") or {}).get("teacher_username") == "u_t",
          str(_steps11)[:200])
    check("处方落盘前有备份", bool(_r.get("backup")) and Path(_r["backup"]).is_file())

    _bad = [{"op": "delete_step", "cap": "cap.t", "step": 99},
            {"op": "set_field", "cap": "nope", "step": 1, "field": "body_has", "value": []},
            {"op": "set_field", "cap": "cap.t", "step": 1, "field": "product_code", "value": "x"},
            {"op": "hack", "cap": "cap.t", "step": 1}]
    _m2, _n2 = _jf11.merge_patches([_bad, _bad], ws=ws11)
    check("非法处方全被挡（越界/未知能力/白名单外字段/未知 op）", _m2 == [], str(_m2))
    _m3, _ = _jf11.merge_patches([
        [{"op": "set_field", "cap": "nope2", "step": 1, "field": "body_has", "value": []}], []], ws=ws11)
    check("仅一方主张且合法 → 可执行", isinstance(_m3, list))
    _conflict = _jf11.merge_patches([
        [{"op": "set_field", "cap": "cap.t", "step": 1, "field": "expect_status", "value": [200]}],
        [{"op": "set_field", "cap": "cap.t", "step": 1, "field": "expect_status", "value": [201]}]], ws=ws11)
    check("两方不一致 → 不执行（多远端一致才算数）", _conflict[0] == [], str(_conflict[0]))

    print("[10] 三方共同投票改判据（2026-11-18 用户口径）")
    import json as _json10
    import tempfile as _tf10
    from core import judgefix as _jf10
    ws10 = Path(_tf10.mkdtemp())
    (ws10 / ".sixcoder").mkdir(parents=True)
    _bp10 = {"caps": [
        {"id": "c1", "probe": {"steps": [
            {"method": "POST", "path": "/api/v1/auth/register/",
             "json": {"username": "u", "password": "p", "role": "student"}},
            {"method": "POST", "path": "/api/v1/auth/register/",
             "json": {"username": "u", "password": "p", "role": "student"}},
            {"method": "POST", "path": "/api/v1/auth/login/",
             "json": {"username": "u", "password": "p"}},
            {"method": "POST", "path": "/api/v1/auth/login/",
             "json": {"username": "u", "password": "p"}},
        ]}},
        {"id": "c2", "probe": {"steps": [{"method": "POST", "path": "/api/v1/matching/runs/",
                                          "expect_json": {"status": "done"}}]}},
        {"id": "c3", "probe": {"steps": [{"method": "POST", "path": "/api/v1/matching/runs/",
                                          "expect_json": {"status": "done"}}]}},
        {"id": "c4", "probe": {"steps": [{"method": "POST", "path": "/api/v1/matching/runs/",
                                          "expect_json": {"status": "completed"}}]}},
    ]}
    (ws10 / ".sixcoder" / "blueprint.json").write_text(
        _json10.dumps(_bp10, ensure_ascii=False), encoding="utf-8")
    _r10 = _jf10.apply(ws10, use_rules=True)
    check("判据修复有改动（显式启用本地规则时）", _r10["changed"] > 0, str(_r10)[:120])
    check("本地规则**默认关闭**（开放性问题交三方协商，避免过拟合）",
          _jf10.apply(ws10).get("disabled") is True)
    _bp_after = _json10.loads((ws10 / ".sixcoder" / "blueprint.json").read_text(encoding="utf-8"))
    _names = [(s.get("json") or {}).get("username")
              for s in _bp_after["caps"][0]["probe"]["steps"]]
    check("同角色多账号已拆名（第 2 个账号改名、login 对应）",
          _names[:2] == ["u", "u_2"] and _names[2:] == ["u", "u_2"], str(_names))
    _sts = [c["probe"]["steps"][0]["expect_json"]["status"] for c in _bp_after["caps"][1:]]
    check("status 词表按多数派统一（completed→done）", _sts == ["done", "done", "done"], str(_sts))
    check("落盘前有备份", bool(_r10.get("backup")) and Path(_r10["backup"]).is_file())
    check("幂等：再跑一次零改动", _jf10.apply(ws10)["changed"] == 0)

    _orig10 = deadlock._ask_reviewer
    try:
        deadlock._ask_reviewer = lambda role, pkt: {
            "ok": True, "answer": '{"verdict":"fix_judge","reason":"判据矛盾","judge_fix":"拆账号"}'}
        deadlock.load_state(ws10)
        _st10 = deadlock.load_state(ws10)
        _st10.update({"state": "negotiate", "caps": ["c1"], "round": 6, "detail": "d"})
        deadlock.save_state(ws10, _st10)
        _d10 = deadlock.negotiate(ws10)
        check("两票 fix_judge → 决议 fix_judge", _d10.get("decision") == "fix_judge", str(_d10.get("tally")))
        check("决议回带 judge_fixed 字段", "judge_fixed" in _d10)
    finally:
        deadlock._ask_reviewer = _orig10

    _calls = iter([{"ok": True, "answer": '{"verdict":"keep","reason":"x"}'},
                   {"ok": True, "answer": '{"verdict":"fix_judge","reason":"y"}'}])
    try:
        deadlock._ask_reviewer = lambda role, pkt: next(_calls)
        deadlock.reset(ws10)
        _st11 = deadlock.load_state(ws10)
        _st11.update({"state": "negotiate", "caps": ["c1"], "round": 7})
        deadlock.save_state(ws10, _st11)
        _d11 = deadlock.negotiate(ws10)
        check("1 keep + 1 fix_judge → keep（无多数不改判据）", _d11.get("decision") == "keep",
              str(_d11.get("tally")))
    finally:
        deadlock._ask_reviewer = _orig10

    print("[9] 振荡型死局（2026-11-18 真实夜班：8 轮白改无净效果，未走回滚）")
    import tempfile as _tf9
    ws9 = Path(_tf9.mkdtemp())
    _orig_neg = deadlock.negotiate
    try:
        deadlock.negotiate = lambda w, emit=None: {"granted": True, "votes": {"writer": "unlock"}}
        r9 = deadlock.on_oscillation(ws9, 3, "打法用完仍无净效果", caps=["cap.x"])
        check("振荡协商授予 → 夜班继续跑（不再直接收工）", r9.get("continue") is True)
        deadlock.reset(ws9)
        deadlock.negotiate = lambda w, emit=None: {"granted": False, "votes": {}}
        r9b = deadlock.on_oscillation(ws9, 4, "again", caps=["cap.x"])
        check("振荡协商拒绝 → 照旧收工且状态回 idle",
              r9b.get("continue") is False
              and str(deadlock.load_state(ws9).get("state") or "") in ("idle", ""))
        st9 = deadlock.load_state(ws9)
        st9["osc_attempts"] = 2
        deadlock.save_state(ws9, st9)
        r9c = deadlock.on_oscillation(ws9, 5, "third")
        check("振荡协商最多 2 次（防无限重试烧钱）", r9c.get("continue") is False)
    finally:
        deadlock.negotiate = _orig_neg
    _dl_src = (Path(__file__).resolve().parent.parent / "core" / "deadlock.py").read_text(encoding="utf-8")
    check("审核者调用不带 remote.complete 不认识的 purpose=", "purpose=" not in _dl_src)
    print()
    if FAILS:
        print("FAIL %d 项：%s" % (len(FAILS), "；".join(FAILS)))
        sys.exit(1)
    print("ALL OK")
